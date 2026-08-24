"""Realtime topics for the task queue and the worker pool.

Tasks change in worker subprocesses and reach this process only through the database,
so both topics are polled and diffed rather than pushed.
"""
import json
import logging

import realtime
from db import db
from tasks import task_display_name

logger = logging.getLogger('main')

# Most a client is sent, however long the queue is: a library scan queues one task per
# file, and nobody reads the ten-thousandth row. Oldest first, which is both the order
# tasks are claimed in and the order the page lists them, so the window holds what is
# running and what is next. A parent always has a lower id than its children, so this can
# never keep a child while dropping its parent.
MAX_TASKS = 200

# File tasks label themselves with a filename rather than a row id, so the path is
# joined in here. Resolving it per task instead would mean a query per file task on every
# tick, several times a second.
#
# Completed rows are excluded outright: they are either about to be deleted (roots,
# and children of a finished parent) or retained only as a parked parent's progress
# bookkeeping until the whole pass ends - and a single big pass floods the window with
# the latter, starving the running work and the pending queue out of the feed. Their
# exclusion reaches the client as an ordinary `remove` event, whose fade-out handling
# already gives completed work its green tick.
_TASK_SQL = """
SELECT t.id, t.parent_id, t.task_name, t.status, t.completion_pct, t.input_json,
       t.error_message, t.created_at, t.started_at, t.run_after, t.worker_id,
       f.filepath, f.identified, f.organized
FROM tasks t
LEFT JOIN files f ON f.id = json_extract(t.input_json, '$.file_id')
WHERE t.status != 'completed'
ORDER BY t.id
LIMIT ?
"""

# Diff baselines, owned by the poller. Snapshots deliberately do not touch these: a
# subscriber joining mid-tick would otherwise absorb changes the poller has not emitted
# yet, and every other client would never see them. A snapshot up to one tick ahead of
# the stream is harmless, since updates carry the whole row.
_tasks_state = {}
_workers_state = {}


def _utc(value):
    """Render a stored UTC timestamp as ISO 8601 so browsers stop reading it as local."""
    if not value:
        return None
    return str(value).replace(' ', 'T') + 'Z'


def _read_tasks():
    """Current task rows as id -> serialisable dict."""
    connection = db.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(_TASK_SQL, (MAX_TASKS,))
        rows = cursor.fetchall()
    finally:
        connection.close()

    tasks = {}
    for (task_id, parent_id, task_name, status, pct, input_json,
         error_message, created_at, started_at, run_after, worker_id, filepath,
         identified, organized) in rows:
        try:
            input_data = json.loads(input_json) if input_json else {}
        except ValueError:
            input_data = {}
        # Set even when the join found nothing: the key's presence is what tells the
        # label builder the path is already resolved and must not be looked up per task.
        input_data.setdefault('filepath', filepath)
        tasks[task_id] = {
            'id': task_id,
            'parentId': parent_id,
            'taskName': task_name,
            'displayName': task_display_name(task_name, input_data),
            'status': status,
            'completionPct': pct or 0,
            'errorMessage': error_message,
            'createdAt': _utc(created_at),
            'startedAt': _utc(started_at),
            'runAfter': _utc(run_after),
            'workerId': worker_id,
            # File-task bookkeeping, so the client can tell WHICH stage a running
            # process_file is in. Null when the join found no file row (deleted, or
            # a task kind that carries no file_id); coerced because SQLite hands
            # back 0/1 rather than booleans.
            'fileIdentified': None if identified is None else bool(identified),
            'fileOrganized': None if organized is None else bool(organized),
        }
    return tasks


def _read_workers():
    """Worker pool state as id -> dict, joined with the task each one is running."""
    import app as app_mod

    pool = app_mod.pool
    if pool is None:
        return {}
    connection = db.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT worker_id, id FROM tasks "
                       "WHERE status = 'running' AND worker_id IS NOT NULL")
        running = dict(cursor.fetchall())
    finally:
        connection.close()
    workers = {}
    for worker_id, (proc, _stop_event) in pool.snapshot().items():
        workers[worker_id] = {
            'id': worker_id,
            'pid': proc.pid,
            'alive': proc.is_alive(),
            'taskId': running.get(worker_id),
        }
    return workers


def _diff(previous, current):
    """Emit add/update/remove events for what changed between two keyed snapshots."""
    events = []
    for key, value in current.items():
        if key not in previous:
            events.append(('add', value))
        elif previous[key] != value:
            events.append(('update', value))
    for key, value in previous.items():
        if key not in current:
            events.append(('remove', value))
    return events


def tasks_snapshot():
    return list(_read_tasks().values())


def tasks_poll():
    global _tasks_state
    current = _read_tasks()
    events = _diff(_tasks_state, current)
    _tasks_state = current
    return events


def workers_snapshot():
    return list(_read_workers().values())


def workers_poll():
    global _workers_state
    current = _read_workers()
    events = _diff(_workers_state, current)
    _workers_state = current
    return events


realtime.register_topic('tasks', access='admin', snapshot=tasks_snapshot, poll=tasks_poll)
realtime.register_topic('workers', access='admin', snapshot=workers_snapshot, poll=workers_poll)
