"""Task queue model, registry, and helpers."""
import hashlib
import json
import datetime
import logging
import os
from collections import namedtuple
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
import titles as titles_lib
import titledb
from containers import compression
from containers import verification as verification_lib
from constants import COMPRESS_EXT, DECOMPRESS_EXT
import downloader as downloader_lib
from db import (
    db, Task, Files, Apps, Libraries, get_library_id, get_library_path, get_library_file_paths,
    get_libraries, add_title_id_in_db, get_title_id_db_id, add_file_to_app,
    file_exists_in_db, update_file_path, delete_file_by_filepath,
    delete_files_under_dir, add_ignored_event, pop_ignored_event,
    add_temp_file, remove_temp_file, claim_temp_file, get_temp_file_paths, purge_temp_files,
    set_library_scan_time, remove_missing_files_from_db,
    remove_file_from_apps, reset_file_identification, reset_file_verification, create_file,
    verification_status, complete_downloads_for_apps,
)
from settings import get_settings
import settings as settings_mod
from utils import interval_string_to_timedelta, delete_empty_folders, human_size
from library import (
    add_missing_apps_for_title, update_title_flags,
    add_missing_apps_to_db, update_titles, organize_file,
    remove_outdated_update_files,
)

logger = logging.getLogger('main')

# How long to wait before retrying a titledb update that failed to reach the release
TITLEDB_RETRY_DELAY = datetime.timedelta(hours=1)

# Same idea for the downloader job: a failed run re-arms itself so one network blip
# cannot kill the chain until the next restart.
DOWNLOADER_RETRY_DELAY = datetime.timedelta(hours=1)

# --- Task Registry ---
TASK_REGISTRY = {}
TASK_CONTINUATIONS = {}
TASK_CLEANUP = {}
TASK_GROUPS = {}  # task_name -> concurrency-group name


def register_task(name, group=None):
    """Register a callable as a named task. `group` assigns it to a concurrency group whose
    parallelism is capped by worker.group_limits."""
    def decorator(func):
        TASK_REGISTRY[name] = func
        if group:
            TASK_GROUPS[name] = group
        return func
    return decorator


def blocked_task_names(running_task_names):
    """Task names that must not be claimed right now because their concurrency group is already
    at its configured limit, given the task_names currently running."""
    limits = get_settings().get('worker', {}).get('group_limits', {})
    if not limits:
        return set()
    running_per_group = {}
    for name in running_task_names:
        group = TASK_GROUPS.get(name)
        if group is not None:
            running_per_group[group] = running_per_group.get(group, 0) + 1
    full = {g for g, limit in limits.items() if running_per_group.get(g, 0) >= limit}
    return {name for name, group in TASK_GROUPS.items() if group in full}


def register_continuation(task_name):
    """Register a function to call when all children of a parent task complete."""
    def decorator(func):
        TASK_CONTINUATIONS[task_name] = func
        return func
    return decorator


def register_cleanup(task_name):
    """Register a function to call when a running task is cancelled.

    Receives the task's input_data as kwargs. Should be idempotent — the task
    may have been killed at any point, so any intermediate state (temp files,
    partial output) should be removed if present and ignored otherwise.
    """
    def decorator(func):
        TASK_CLEANUP[task_name] = func
        return func
    return decorator


def get_registered_task(name):
    return TASK_REGISTRY.get(name)


# --- Display names ---
def register_display(task_name):
    """Register a function building a task's human-readable label from its input kwargs."""
    def decorator(func):
        TASK_DISPLAY[task_name] = func
        return func
    return decorator


def _file_label(file_id=None, **kwargs):
    """Basename for a file task.

    A caller listing many tasks resolves the path in its own query and passes it in -
    including as None for a file that is gone, which is why the key being *present*
    is what suppresses the lookup here. Only the enqueue path, one task at a time,
    arrives without it.
    """
    if 'filepath' in kwargs:
        filepath = kwargs['filepath']
    else:
        file_obj = db.session.get(Files, file_id)
        filepath = file_obj.filepath if file_obj else None
    return os.path.basename(filepath) if filepath else f'file #{file_id}'


TASK_DISPLAY = {
    'startup': lambda **kw: 'Startup',
    'update_titledb': lambda **kw: 'Update TitleDB',
    'scan_libraries': lambda **kw: 'Scan all libraries',
    'scan_library': lambda library_path, **kw: f'Scan {library_path}',
    'add_file': lambda filepath, **kw: f'Add {os.path.basename(filepath)}',
    'process_file': lambda **kw: f'Process {_file_label(**kw)}',
    'process_library': lambda **kw: 'Process library files',
    'library_maintenance': lambda library_path=None, **kw: (
        f'Maintain {library_path}' if library_path else 'Library maintenance'),
    'add_missing_apps_for_title': lambda title_id, **kw: f'Add missing content for {title_id}',
    'update_titles_for_title': lambda title_id, **kw: f'Update title {title_id}',
    'remove_outdated_updates': lambda **kw: 'Remove outdated updates',
    'verify_file': lambda **kw: f'Verify {_file_label(**kw)}',
    'compress_file': lambda **kw: f'Compress {_file_label(**kw)}',
    'decompress_file': lambda **kw: f'Decompress {_file_label(**kw)}',
    'add_missing_apps': lambda **kw: 'Add missing content',
    'remove_missing_files': lambda **kw: 'Remove missing files',
    'update_titles': lambda **kw: 'Update titles',
    'remove_library': lambda library_path, **kw: f'Remove library {library_path}',
    'handle_file_added': lambda filepath, **kw: f'New file {os.path.basename(filepath)}',
    'handle_file_moved': lambda src_path, dest_path, **kw: (
        f'Moved {os.path.basename(src_path)} to {os.path.basename(dest_path)}'),
    'handle_file_deleted': lambda filepath, **kw: f'Deleted {os.path.basename(filepath)}',
    'handle_dir_deleted': lambda dirpath, **kw: f'Deleted folder {os.path.basename(dirpath)}',
    'downloader_torrents_run': lambda **kw: 'Download missing content (torrents)',
    'downloader_ghosteshop_run': lambda **kw: 'Download missing content (Ghost eShop)',
    'ghosteshop_download': lambda name=None, app_id=None, **kw: (
        f'Download {name or app_id} (Ghost eShop)'),
}


def task_display_name(task_name, input_data):
    """Human-readable label for a task, falling back to the humanised task name.

    input_json is persisted data that can outlive a change to a task's arguments, so a
    label that no longer builds must never break enqueueing, the worker loop or the UI.
    """
    build = TASK_DISPLAY.get(task_name)
    if build:
        try:
            return build(**(input_data or {}))
        except Exception as e:
            logger.debug(f"Could not build display name for '{task_name}': {e}")
    return task_name.replace('_', ' ').capitalize()


# --- Progress ---
_current_task_id = None


def _task_progress(task_id):
    """Return a callback that writes live percent to a task row, or None outside a task."""
    if task_id is None:
        return None
    engine = db.engine
    logged = [-1]

    def report(pct):
        connection = engine.raw_connection()
        try:
            cursor = connection.cursor()
            cursor.execute("UPDATE tasks SET completion_pct = ? WHERE id = ? AND status = 'running'",
                           (pct, task_id))
            connection.commit()
        finally:
            connection.close()
        if pct // 5 != logged[0]:
            logged[0] = pct // 5
            logger.debug(f"Task {task_id} progress: {pct}%")

    return report

# --- Child task helpers ---
def create_child_task(parent_id, task_name, input_data=None):
    """Create a child task, deduped against existing active children of the same parent."""
    if task_name not in TASK_REGISTRY:
        raise ValueError(f"Unknown task: {task_name}")
    input_data = input_data or {}
    input_json = json.dumps(input_data, sort_keys=True)
    input_hash = compute_input_hash(input_data)
    now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

    connection = db.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            "SELECT id FROM tasks WHERE parent_id = ? AND task_name = ? AND input_hash = ? "
            "AND status IN ('pending', 'running', 'waiting_for_children', 'completed') LIMIT 1",
            (parent_id, task_name, input_hash)
        )
        row = cursor.fetchone()
        if row:
            connection.commit()
            return row[0]
        cursor.execute(
            "INSERT INTO tasks (parent_id, task_name, status, completion_pct, input_json, input_hash, created_at) "
            "VALUES (?, ?, 'pending', 0, ?, ?, ?)",
            (parent_id, task_name, input_json, input_hash, now)
        )
        child_id = cursor.lastrowid
        # logger.debug(f"Enqueued task child '{task_name}' (id={child_id}) of parent_id={parent_id}")
        connection.commit()
        return child_id
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def enqueue_or_child(task_name, input_data=None):
    """Create as child of the running task, or top-level if called outside a task."""
    if _current_task_id is not None:
        return create_child_task(_current_task_id, task_name, input_data)
    return enqueue_task(task_name, input_data)[0].id


def set_waiting_for_children():
    """Mark the current task as waiting for its children to complete."""
    task = db.session.get(Task, _current_task_id)
    task.status = 'waiting_for_children'
    task.worker_id = None
    db.session.commit()


def on_task_completed(task_id, parent_id):
    """Called by the worker after any task completes. Updates parent progress and checks for completion."""
    if not parent_id:
        return
    _try_complete_parent(parent_id)


def _try_complete_parent(parent_id):
    """Atomically update parent progress and complete if all children are done."""
    connection = db.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")

        cursor.execute("SELECT status, task_name, input_json, parent_id FROM tasks WHERE id = ?", (parent_id,))
        row = cursor.fetchone()
        if not row or row[0] != 'waiting_for_children':
            connection.commit()
            return
        grandparent_id = row[3]

        # Count children atomically under the lock
        cursor.execute("SELECT COUNT(*) FROM tasks WHERE parent_id = ?", (parent_id,))
        total = cursor.fetchone()[0]
        cursor.execute(
            "SELECT COUNT(*) FROM tasks WHERE parent_id = ? AND status IN ('completed', 'failed')",
            (parent_id,)
        )
        done = cursor.fetchone()[0]
        pct = int(done * 100 / total) if total else 0

        if done < total:
            cursor.execute("UPDATE tasks SET completion_pct = ? WHERE id = ?", (pct, parent_id))
            connection.commit()
            return

        # All children done — mark parent complete
        now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute(
            "UPDATE tasks SET status = 'completed', completion_pct = 100, exit_code = 0, completed_at = ? WHERE id = ?",
            (now, parent_id)
        )
        connection.commit()

        # The parent's terminal outcome is history too; its children recorded
        # their own rows as they finished.
        try:
            from db import record_task_history
            cursor.execute(
                "SELECT task_name, input_json, started_at FROM tasks WHERE id = ?",
                (parent_id,))
            prow = cursor.fetchone()
            if prow:
                display = task_display_name(prow[0],
                                            json.loads(prow[1]) if prow[1] else {})
                # Raw cursors hand back DateTime columns as strings; the ORM
                # column would reject them and drop the whole history row.
                started = prow[2]
                if isinstance(started, str):
                    try:
                        started = datetime.datetime.strptime(
                            started[:19], '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        started = None
                record_task_history(parent_id, prow[0], display, 'completed',
                                    started_at=started)
        except Exception as hist_e:
            logger.warning(f"Parent history write for {parent_id} failed: {hist_e}")

        # Run continuation outside the transaction
        task_name = row[1]
        continuation = TASK_CONTINUATIONS.get(task_name)
        if continuation:
            input_data = json.loads(row[2])
            try:
                continuation(**input_data)
            except Exception as e:
                # The children all finished - the pass happened. A raising
                # continuation (e.g. a sync against a down qBittorrent) must not
                # leave the parent row behind forever or break the delete below.
                logger.error(f"Continuation of {task_name} (task {parent_id}) failed: {e}")

        # Delete parent and its children
        try:
            Task.query.filter_by(parent_id=parent_id).delete()
            Task.query.filter_by(id=parent_id).delete()
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f"Deleting completed parent {parent_id} failed: {e}")

        # Propagate completion up the chain
        if grandparent_id:
            _try_complete_parent(grandparent_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# --- Cancellation ---

def _cancel_atomic(task_id, removable=('pending', 'running', 'waiting_for_children')):
    """Delete the task and its descendants under one transaction.

    Descendants: pending and terminal rows are deleted, running rows are
    orphaned (parent_id=NULL) so they finish naturally and self-delete. Walking
    descendants for every removable status - not just waiting_for_children -
    keeps a parent that failed after enqueueing from leaving children behind,
    which would otherwise break the delete (FK) or accumulate completed orphans.

    `removable` is which statuses may be taken out, so cancelling (live work) and
    dismissing (a failed row) share one transaction and one set of descendant rules.
    """
    def _txn():
        connection = db.engine.raw_connection()
        try:
            cursor = connection.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "SELECT status, task_name, input_json, parent_id, worker_id FROM tasks WHERE id = ?",
                (task_id,)
            )
            row = cursor.fetchone()
            if not row:
                connection.commit()
                return False, None, None, None, None
            status, task_name, input_json, parent_id, worker_id = row
            if status not in removable:
                connection.commit()
                return False, None, None, None, None

            running_worker_id = worker_id if status == 'running' else None
            cancelled_task_name = task_name if status == 'running' else None
            cancelled_input_json = input_json if status == 'running' else None

            def _walk(pid):
                cursor.execute("SELECT id, status FROM tasks WHERE parent_id = ?", (pid,))
                for child_id, child_status in cursor.fetchall():
                    if child_status == 'pending':
                        cursor.execute("DELETE FROM tasks WHERE id = ?", (child_id,))
                    elif child_status == 'running':
                        cursor.execute("UPDATE tasks SET parent_id = NULL WHERE id = ?", (child_id,))
                    else:
                        # waiting_for_children recurses; completed/failed leaves go.
                        _walk(child_id)
                        cursor.execute("DELETE FROM tasks WHERE id = ?", (child_id,))

            _walk(task_id)
            cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            connection.commit()
            return True, parent_id, running_worker_id, cancelled_task_name, cancelled_input_json
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    return with_lock_retry(_txn)


def cancel_task(task_id):
    """Cancel a task. Returns True if cancelled, False if not found or already terminal.

    - pending: deleted.
    - running: worker is restarted (mid-task termination), cleanup hook runs.
    - waiting_for_children: pending descendants deleted, running descendants
      orphaned (allowed to finish), parent deleted.
    """
    found, parent_id, worker_id, task_name, input_json = _cancel_atomic(task_id)
    if not found:
        return False

    from db import record_task_history
    if task_name is not None:
        # A running cancellation: record it as such before the row vanishes.
        try:
            input_data = json.loads(input_json) if input_json else {}
            record_task_history(task_id, task_name,
                                task_display_name(task_name, input_data),
                                'cancelled')
        except ValueError:
            record_task_history(task_id, task_name, task_name, 'cancelled')

    if worker_id is not None:
        import app as app_mod
        if app_mod.pool is not None:
            app_mod.pool.restart_worker(worker_id)

    if task_name is not None:
        _run_cleanup_hook(task_name, input_json)

    if parent_id:
        _try_complete_parent(parent_id)
    return True


def dismiss_task(task_id):
    """Remove a failed task. Returns True if a row was removed, False otherwise.

    Failed tasks are kept on purpose so a failure is not lost between page loads, which
    makes this the only way to clear one. Nothing is running by definition, so unlike
    cancel there is no worker to restart and no cleanup hook to run - the failure path
    in the worker already ran it.
    """
    found, parent_id, _worker_id, _task_name, _input_json = _cancel_atomic(
        task_id, removable=('failed',))
    if not found:
        return False
    if parent_id:
        _try_complete_parent(parent_id)
    return True


def purge_failed_tasks():
    """Remove every failed task. Returns how many were removed."""
    connection = db.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT id FROM tasks WHERE status = 'failed'")
        task_ids = [r[0] for r in cursor.fetchall()]
    finally:
        connection.close()
    # One at a time rather than a bulk DELETE: each has descendants to unpick and a
    # parent that may now be able to complete, which dismiss_task already handles.
    return sum(1 for task_id in task_ids if dismiss_task(task_id))


def _run_cleanup_hook(task_name, input_json):
    """Run a task's registered @register_cleanup hook (idempotent) if it has one."""
    cleanup = TASK_CLEANUP.get(task_name)
    if not cleanup:
        return
    input_data = json.loads(input_json) if input_json else {}
    try:
        cleanup(**input_data)
    except Exception as e:
        logger.error(f"Cleanup hook for task '{task_name}' failed: {e}")


def reap_worker_task(worker_id):
    """Fail and clean up the task a worker was running when it was stopped mid-task."""
    task = Task.query.filter_by(status='running', worker_id=worker_id).first()
    if task is None:
        return
    task_name, input_json, parent_id = task.task_name, task.input_json, task.parent_id
    display = task_display_name(task_name, json.loads(input_json) if input_json else {})
    started = task.started_at
    task.status = 'failed'
    task.error_message = 'Interrupted by worker stop'
    task.exit_code = 1
    task.completed_at = datetime.datetime.utcnow()
    db.session.commit()
    from db import record_task_history
    record_task_history(task.id, task_name, display, 'failed',
                        error='Interrupted by worker stop', started_at=started)
    logger.info(f"Reaped task {task.id} ({task_name}) from stopped worker {worker_id}")
    _run_cleanup_hook(task_name, input_json)
    if parent_id:
        _try_complete_parent(parent_id)


# --- Startup cleanup ---

def cleanup_tasks():
    """Startup cleanup: clear the scheduled queue and fail interrupted tasks.

    Manually enqueued tasks (run_after NULL - an 'Update now', a queued Add
    Content pass) survive the restart: nobody re-issues them, so dropping them
    would silently lose user intent. Scheduled rows come back on their own via
    the startup re-arm of each chain."""
    # Remove completed tasks
    Task.query.filter_by(status='completed').delete()

    # Clear scheduled rows only; manual pending rows run on the new pool
    Task.query.filter(Task.status == 'pending',
                      Task.run_after.isnot(None)).delete()

    # Mark running/waiting tasks as failed — they can't survive a restart
    stale = Task.query.filter(Task.status.in_(['running', 'waiting_for_children'])).all()
    from db import record_task_history
    for task in stale:
        task.status = 'failed'
        task.error_message = 'Interrupted by application restart'
        task.exit_code = 1
        task.completed_at = datetime.datetime.utcnow()
        logger.info(f"Reset stale task {task.id} ({task.task_name})")
        record_task_history(task.id, task.task_name,
                            task_display_name(task.task_name,
                                              json.loads(task.input_json) if task.input_json else {}),
                            'failed', error='Interrupted by application restart',
                            started_at=task.started_at)

    db.session.commit()

    # Sweep leftover output from any (de)compression interrupted by the restart.
    purge_temp_files()

    # Drop watcher-event and temp-claim leftovers older than their TTL.
    from db import purge_stale_events
    purge_stale_events()

    # Ghost rows left mid-transfer go back to queued so the surviving manual
    # passes (or the re-armed schedule) pick them up with a cheap resume.
    from db import Download
    flipped = Download.query.filter_by(source='ghosteshop', status='downloading').update(
        {'status': 'queued', 'progress': 0})
    if flipped:
        db.session.commit()
        logger.info(f"Requeued {flipped} interrupted Ghost eShop download(s).")


# --- Helpers ---

def with_lock_retry(fn, attempts=3, base_delay=0.3):
    """Run a BEGIN IMMEDIATE helper again on 'database is locked'.

    The web process calls enqueue/cancel paths from request threads; with
    workers and pollers active, a busy_timeout miss surfaces as an HTTP 500.
    A short retry closes the gap - the writer holding the lock finishes in
    milliseconds, not tens of seconds."""
    import time as _time
    last = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            if 'locked' not in str(e).lower() or attempt == attempts - 1:
                raise
            last = e
            _time.sleep(base_delay * (attempt + 1))
    raise last


def compute_input_hash(input_data):
    canonical = json.dumps(input_data, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def enqueue_task(task_name, input_data=None, run_after=None):
    """Enqueue a task. Returns (task, created) — created is False if a duplicate exists."""
    if task_name not in TASK_REGISTRY:
        raise ValueError(f"Unknown task: {task_name}")

    input_data = input_data or {}
    input_hash = compute_input_hash(input_data)
    input_json = json.dumps(input_data, sort_keys=True)

    # Scheduled tasks only dedup against pending; immediate tasks dedup against running too
    if run_after:
        dedup_statuses = "('pending', 'waiting_for_children')"
    else:
        dedup_statuses = "('pending', 'running', 'waiting_for_children')"

    def _txn():
        connection = db.engine.raw_connection()
        try:
            cursor = connection.cursor()
            cursor.execute("BEGIN IMMEDIATE")

            cursor.execute(
                f"SELECT id FROM tasks WHERE task_name = ? AND input_hash = ? AND status IN {dedup_statuses}",
                (task_name, input_hash)
            )
            existing = cursor.fetchone()

            if existing:
                connection.commit()
                task = db.session.get(Task, existing[0])
                return task, False

            now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            run_after_str = run_after.strftime('%Y-%m-%d %H:%M:%S') if run_after else None
            cursor.execute(
                "INSERT INTO tasks (task_name, status, completion_pct, input_json, input_hash, run_after, created_at) "
                "VALUES (?, 'pending', 0, ?, ?, ?, ?)",
                (task_name, input_json, input_hash, run_after_str, now)
            )
            new_id = cursor.lastrowid
            connection.commit()

            if run_after:
                local_run_after = run_after + (datetime.datetime.now() - datetime.datetime.utcnow())
                schedule_info = f", run_after={local_run_after.strftime('%Y-%m-%d %H:%M:%S')}"
            else:
                schedule_info = ""
            logger.debug(f"Enqueued task '{task_display_name(task_name, input_data)}' "
                         f"(id={new_id}{schedule_info})")
            task = db.session.get(Task, new_id)
            return task, True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    return with_lock_retry(_txn)


def update_scheduled_task(task_name, run_after):
    """Update run_after on a pending scheduled task, delete if None, or create if missing."""
    def _txn():
        connection = db.engine.raw_connection()
        try:
            cursor = connection.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            if run_after is None:
                cursor.execute(
                    "DELETE FROM tasks WHERE task_name = ? AND status = 'pending' AND run_after IS NOT NULL",
                    (task_name,)
                )
                logger.debug(f"Deleted scheduled task '{task_name}' (disabled)")
            else:
                cursor.execute(
                    "UPDATE tasks SET run_after = ? WHERE task_name = ? AND status = 'pending' AND run_after IS NOT NULL",
                    (run_after.strftime('%Y-%m-%d %H:%M:%S'), task_name)
                )
                if cursor.rowcount == 0:
                    # No existing scheduled task — create one
                    now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                    input_hash = compute_input_hash({})
                    cursor.execute(
                        "INSERT INTO tasks (task_name, status, completion_pct, input_json, input_hash, run_after, created_at) "
                        "VALUES (?, 'pending', 0, '{}', ?, ?, ?)",
                        (task_name, input_hash, run_after.strftime('%Y-%m-%d %H:%M:%S'), now)
                    )
                    local_ra = run_after + (datetime.datetime.now() - datetime.datetime.utcnow())
                    logger.debug(f"Created scheduled task '{task_name}' run_after={local_ra.strftime('%Y-%m-%d %H:%M:%S')}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    return with_lock_retry(_txn)


def get_task(task_id):
    return db.session.get(Task, task_id)



@register_task('startup')
def startup_task(**kwargs):
    """Startup task: resume interrupted per-file work, then kick off the titledb update."""
    # Enqueued here rather than only from update_titledb_task, whose network fetch can fail:
    # recovery of an interrupted pipeline must not wait for the next scheduled titledb run.
    enqueue_task('process_library')
    try:
        update_titledb_task()
    except Exception:
        # A retry is already scheduled; scanning must not be held hostage to the network
        logger.exception('titledb update failed at startup')
    scan_libraries_task()
    arm_downloader_schedule()

# --- Periodic tasks ---
@register_task('update_titledb')
def update_titledb_task(**kwargs):
    settings = get_settings()
    try:
        titledb.update_titledb(settings)
        enqueue_task('process_library')
        add_missing_apps_to_db()
        update_titles()
    except Exception:
        # Without this the chain simply stops: nothing re-enqueues a failed task, so a single
        # network blip would leave titledb frozen until the next restart.
        update_scheduled_task('update_titledb', datetime.datetime.utcnow() + TITLEDB_RETRY_DELAY)
        raise
    # Re-enqueue for next scheduled run
    interval_str = settings.get('scheduler', {}).get('titledb_update_interval', '12h')
    delta = interval_string_to_timedelta(interval_str)
    if delta:
        update_scheduled_task('update_titledb', datetime.datetime.utcnow() + delta)


def arm_downloader_schedule(settings=None):
    """Create/refresh/delete the scheduled rows for both download sources.

    Each source is disabled by default, so unlike titledb there is no row until
    the feature is configured - and disabling a source removes its row rather
    than leaving a periodic no-op behind.
    """
    settings = settings or get_settings()
    torrents = settings.get('downloader', {}).get('torrents', {}) or {}
    ghost = settings.get('downloader', {}).get('ghosteshop', {}) or {}

    delta = interval_string_to_timedelta(torrents.get('interval', '1h'))
    run_after = None
    if downloader_lib.torrents_configured(settings) and delta:
        run_after = datetime.datetime.utcnow() + delta
    update_scheduled_task('downloader_torrents_run', run_after)

    delta = interval_string_to_timedelta(ghost.get('interval', '24h'))
    run_after = None
    if downloader_lib.ghosteshop_configured(settings) and delta:
        run_after = datetime.datetime.utcnow() + delta
    update_scheduled_task('downloader_ghosteshop_run', run_after)


@register_task('downloader_torrents_run')
def downloader_torrents_run_task(**kwargs):
    """Torrents pass: sync rows, search missing content on Jackett, hand each
    match to qBittorrent - then re-arm the next run."""
    settings = get_settings()
    try:
        downloader_lib.run_downloader_job(
            settings, progress=_task_progress(_current_task_id))
    except Exception:
        update_scheduled_task('downloader_torrents_run',
                              datetime.datetime.utcnow() + DOWNLOADER_RETRY_DELAY)
        raise
    arm_downloader_schedule(settings)


@register_task('downloader_ghosteshop_run')
def downloader_ghosteshop_run_task(**kwargs):
    """Ghost eShop pass: compute the work list and hand one io task per file.

    The pass itself is quick (one catalog fetch per target happens inside each
    child); the transfers run as `ghosteshop_download` children in the `io`
    concurrency group, sharing the Workers I/O budget with verification and
    compression instead of bypassing it."""
    settings = get_settings()
    try:
        targets = downloader_lib.prepare_ghosteshop_targets(settings)
    except Exception:
        update_scheduled_task('downloader_ghosteshop_run',
                              datetime.datetime.utcnow() + DOWNLOADER_RETRY_DELAY)
        raise
    if not targets:
        arm_downloader_schedule(settings)
        return
    for t in targets:
        enqueue_or_child(
            downloader_lib.GHOSTESHOP_DOWNLOAD_TASK,
            {'app_id': t['app_id'], 'app_version': t['app_version'],
             'name': t['name']})
    set_waiting_for_children()


@register_continuation('downloader_ghosteshop_run')
def _downloader_ghosteshop_done(**kwargs):
    """All Ghost eShop children finished: final sync, summary, next run armed."""
    settings = get_settings()
    downloader_lib.sync_downloads_status(settings)
    logger.info('Ghost eShop pass done.')
    arm_downloader_schedule(settings)
    _arm_ghosteshop_retry(settings)


# A failed transfer (network blip longer than the chunk retries) should not
# wait a full schedule interval (a day by default) for its second chance.
GHOSTESHOP_RETRY_DELAYS = (datetime.timedelta(minutes=15),
                           datetime.timedelta(hours=1),
                           datetime.timedelta(hours=6))


def _arm_ghosteshop_retry(settings):
    """Schedule an early retry pass while failed ghost rows remain, with a
    growing delay, so a temporarily-down portal gets re-tried without spinning.

    The scheduled row's dedup is (task_name, input_hash) over pending rows, and
    update_scheduled_task moves any existing scheduled row's run_after - so this
    never stacks passes, it just pulls the next one closer."""
    from db import Download
    failed = Download.query.filter_by(source='ghosteshop', status='failed').count()
    if not failed:
        return
    delay = GHOSTESHOP_RETRY_DELAYS[min(failed, len(GHOSTESHOP_RETRY_DELAYS)) - 1]
    update_scheduled_task('downloader_ghosteshop_run',
                          datetime.datetime.utcnow() + delay)
    logger.info(f'Ghost eShop: {failed} failed row(s), retry pass armed in {delay}.')


@register_task(downloader_lib.GHOSTESHOP_DOWNLOAD_TASK, group='io')
def ghosteshop_download_task(app_id, app_version, name=None, **kwargs):
    """Download one file from Ghost eShop into the game's folder, with resume.

    The downloads row is the source of truth: a vanished or foreign-lane row is
    a no-op, an owned target just flips to completed. Failures land on the row
    (visible on the Downloads page) rather than raising - a missing catalog
    entry is an expected outcome, not a task crash."""
    settings = get_settings()
    downloader_lib.download_ghosteshop_row(app_id, str(app_version),
                                           settings=settings,
                                           task_id=_current_task_id)


@register_cleanup(downloader_lib.GHOSTESHOP_DOWNLOAD_TASK)
def _ghosteshop_download_cleanup(app_id, app_version, name=None, **kwargs):
    """A cancelled/killed transfer puts its row back to queued instead of leaving
    it 'downloading' forever - the orphan healing only runs at the next pass,
    which can be a day out. The .part file stays: resuming is cheap. A row
    already 'paused' keeps that state - pausing wrote it before cancelling
    precisely so the cancellation would not requeue it."""
    from db import get_download_by_app, update_download
    row = get_download_by_app(app_id, str(app_version))
    if row is None or row.status not in ('downloading', 'queued'):
        return
    update_download(row.id, status='queued', progress=0,
                    error='Cancelled - will resume on the next pass')


@register_cleanup('downloader_ghosteshop_run')
@register_cleanup('downloader_torrents_run')
def _downloader_pass_cleanup(**kwargs):
    """A cancelled/killed pass must re-arm its schedule row, or the source stays
    dead until the next application restart - the re-arm otherwise only happens
    when the pass completes, fails its prepare step, or the app starts."""
    arm_downloader_schedule(get_settings())


# --- Scan pipeline ---
@register_task('scan_libraries')
def scan_libraries_task(**kwargs):
    """Scan all library paths for new files."""
    libraries = get_libraries()
    if not libraries:
        logger.info('No libraries to scan.')
        return
    for lib in libraries:
        enqueue_or_child('scan_library', {'library_path': lib.path})
    set_waiting_for_children()

@register_task('scan_library')
def scan_library_task(library_path, **kwargs):
    """Scan a library path for new files, creating a child task per file."""
    library_id = get_library_id(library_path)
    if not os.path.isdir(library_path):
        logger.warning(f'Library path {library_path} does not exist.')
        return

    logger.info(f'Scanning library path {library_path} ...')
    _, files = titles_lib.getDirsAndFiles(library_path)
    skip = set(get_library_file_paths(library_id)) | get_temp_file_paths()
    new_files = [f for f in files if f not in skip]

    if not new_files:
        logger.info(f'No new files found in {library_path}.')
        _scan_library_done(library_path=library_path)
        return

    enqueued = 0
    for fp in new_files:
        new_file = _insert_file(library_path, library_id, fp)
        if new_file is not None:
            enqueue_or_child('process_file', {'file_id': new_file.id})
            enqueued += 1

    if enqueued:
        set_waiting_for_children()
    else:
        _scan_library_done(library_path=library_path)


@register_continuation('scan_library')
def _scan_library_done(library_path, **kwargs):
    set_library_scan_time(get_library_id(library_path))
    enqueue_task('remove_missing_files')


def _insert_file(library_path, library_id, filepath):
    """Read file info from disk and insert a Files row. Returns the row, or None on failure."""
    file_display = filepath.replace(library_path, "").lstrip("/")
    logger.info(f'Getting file info: {file_display}')
    file_info = titles_lib.get_file_info(filepath)
    if file_info is None:
        logger.error(f'Failed to get info for file: {file_display}')
        return None
    return create_file(library_id, filepath, file_info)


@register_task('add_file')
def add_file_task(library_path, filepath, **kwargs):
    """Add a single file to the library DB."""
    library_id = get_library_id(library_path)
    if filepath in get_library_file_paths(library_id):
        return

    new_file = _insert_file(library_path, library_id, filepath)
    if new_file is None:
        raise ValueError(f'Failed to add file: {filepath}')

    enqueue_task('process_file', {'file_id': new_file.id})


# --- Per-file pipeline ---
#
# Every stage a file can need, in the order it needs them. A stage either runs inline in
# the driver (`run`) or is delegated to a registered task (`task`) that re-drives the file
# when it finishes — delegation is what buys a concurrency group, a cancel hook and a
# progress bar, so it is reserved for the stages that want them.
Stage = namedtuple('Stage', 'name applies run task')


def _needs_identify(file, mgmt):
    """The per-file form of library.get_files_to_identify."""
    if not file.identified and not file.identification_attempts:
        return True
    if bool(titles_lib.Keys.keys_loaded) and file.identification_type == 'filename':
        return True
    # Keys were loaded (or reloaded) after the last failed attempt: the failure
    # was probably a master key the old keys file lacked, which is now fixed -
    # worth exactly one more try per keys reload.
    if (not file.identified
            and settings_mod.KEYS_LOADED_AT is not None
            and file.last_attempt is not None
            and settings_mod.KEYS_LOADED_AT > file.last_attempt):
        return True
    return False


def _identify(file, mgmt):
    """Identify one file and upsert its Apps/Titles."""
    identified_title_ids = []
    filepath = file.filepath
    logger.info(f'Identifying file: {file.filename}')
    identification, success, file_contents, error = titles_lib.identify_file(filepath)

    if success and file_contents and not error:
        title_ids = list(dict.fromkeys([c['title_id'] for c in file_contents]))
        for title_id in title_ids:
            add_title_id_in_db(title_id)

        nb_content = 0
        for file_content in file_contents:
            logger.info(f'Found content Title ID: {file_content["title_id"]} App ID: {file_content["app_id"]} Type: {file_content["type"]} Version: {file_content["version"]}')
            title_id_in_db = get_title_id_db_id(file_content["title_id"])

            # Atomic owned-OR upsert: on conflict, flip owned=True without
            # clobbering an existing row's title_id/app_type.
            stmt = sqlite_insert(Apps.__table__).values(
                app_id=file_content["app_id"],
                app_version=file_content["version"],
                app_type=file_content["type"],
                owned=True,
                title_id=title_id_in_db,
            ).on_conflict_do_update(
                index_elements=['app_id', 'app_version'],
                set_={'owned': True},
            )
            db.session.execute(stmt)
            db.session.commit()

            add_file_to_app(file_content["app_id"], file_content["version"], file.id)
            nb_content += 1

        if nb_content > 1:
            file.multicontent = True
        file.nb_content = nb_content
        file.identified = True
        identified_title_ids = title_ids
        # Content the downloader fetched completes the moment the library holds
        # it - not at the next downloader sync, which for a scheduled Ghost
        # eShop pass can be a day away.
        complete_downloads_for_apps(
            [(c["app_id"], c["version"]) for c in file_contents])
    else:
        logger.warning(f"Error identifying file {file.filename}: {error}")
        file.identification_error = error
        file.identified = False

    file.identification_type = identification
    file.identification_attempts += 1
    file.last_attempt = datetime.datetime.now()
    db.session.commit()

    for title_id in identified_title_ids:
        enqueue_task('add_missing_apps_for_title', {'title_id': title_id})


def _needs_verify(file, mgmt):
    verification = mgmt['verification']
    if not verification['enabled'] or file.extension not in verification_lib.VERIFY_EXT:
        return False
    if not titles_lib.Keys.keys_loaded:
        return False
    if verification['depth'] == verification_lib.DEPTH_HASH:
        # Only "never verified" queues work. A False verdict stays - including
        # the error-path shape (hash_valid False, hash_modified None) produced
        # when the container cannot even be opened: that failure is
        # deterministic, and re-running it re-enqueued process_file forever
        # (the process_file <-> verify_file loop). A row lacking a verdict at
        # all (both None) is pre-hash_modified legacy data: verify it once.
        return file.hash_valid is None
    return file.signature_valid is None


def _needs_organize(file, mgmt):
    return mgmt['organizer']['enabled'] and file.identified and not file.organized


def _organize(file, mgmt):
    """Place one file under the organizer templates, holding the path claim across the move."""
    if not mgmt['organizer']['enabled']:
        return
    claimed = file.filepath
    if not claim_temp_file(claimed):
        return
    library_path = get_library_path(file.library_id)
    try:
        if organize_file(file, library_path, mgmt['organizer']):
            file.organized = True
            db.session.commit()
    finally:
        remove_temp_file(claimed)
    enqueue_task('library_maintenance', {'library_path': library_path})


def _needs_compress(file, mgmt):
    if not mgmt['compression']['enabled'] or file.compressed or file.extension not in COMPRESS_EXT:
        return False
    if verification_status(file) == verification_lib.STATUS_CORRUPT:
        return False
    target = compression.conversion_target(file)
    return Files.query.filter(Files.filepath == target, Files.id != file.id).first() is None


STAGES = [
    Stage('identify', _needs_identify, _identify, None),
    Stage('organize', _needs_organize, _organize, None),
    Stage('verify', _needs_verify, None, 'verify_file'),
    Stage('compress', _needs_compress, None, 'compress_file'),
]


@register_task('process_file')
def process_file_task(file_id, **kwargs):
    """Drive one file down the stage list: inline stages here, delegated stages by task."""
    done = set()
    while True:
        file = db.session.get(Files, file_id)
        if file is None:
            return
        if not os.path.exists(file.filepath):
            logger.warning(f'File {file.filename} no longer exists, deleting from database.')
            remove_file_from_apps(file_id)
            Files.query.filter_by(id=file_id).delete(synchronize_session=False)
            db.session.commit()
            return
        mgmt = get_settings()['library']['management']
        stage = next((s for s in STAGES if s.name not in done and s.applies(file, mgmt)), None)
        if stage is None:
            return
        done.add(stage.name)
        if stage.task:
            enqueue_task(stage.task, {'file_id': file_id})
            return
        stage.run(file, mgmt)


@register_task('process_library')
def process_library_task(**kwargs):
    """Drive every file that still has pipeline work."""
    mgmt = get_settings()['library']['management']
    files = [f for f in Files.query.all() if any(s.applies(f, mgmt) for s in STAGES)]
    logger.info(f'Processing library: {len(files)} file(s) with pending work.')
    for f in files:
        enqueue_or_child('process_file', {'file_id': f.id})
    if files:
        set_waiting_for_children()


@register_continuation('process_library')
def _process_library_done(**kwargs):
    enqueue_task('library_maintenance')
    enqueue_task('update_titles')


@register_task('library_maintenance')
def library_maintenance_task(library_path=None, **kwargs):
    """Post-organization GC: prune empty folders and outdated updates."""
    settings = get_settings()
    organizer = settings['library']['management']['organizer']
    if organizer.get('enabled') and organizer.get('remove_empty_folders'):
        paths = [library_path] if library_path else [lib.path for lib in get_libraries()]
        for path in paths:
            delete_empty_folders(path)
    if settings['library']['management']['delete_older_updates']:
        enqueue_task('remove_outdated_updates')


@register_task('add_missing_apps_for_title')
def add_missing_apps_for_title_task(title_id, **kwargs):
    """Per-title: expand missing base/update/DLC apps for one title, then enqueue update_titles_for_title."""
    add_missing_apps_for_title(title_id)
    enqueue_or_child('update_titles_for_title', {'title_id': title_id})
    set_waiting_for_children()


@register_task('update_titles_for_title')
def update_titles_for_title_task(title_id, **kwargs):
    """Per-title: recompute have_base / up_to_date / complete under BEGIN IMMEDIATE."""
    update_title_flags(title_id)


@register_task('remove_outdated_updates')
def remove_outdated_updates_task(**kwargs):
    """Remove outdated update files."""
    remove_outdated_update_files()
    enqueue_task('update_titles')


# --- Verification ---
@register_task('verify_file', group='io')
def verify_file_task(file_id, **kwargs):
    """Verify one file's signatures and, at hash depth, its NCA content hashes."""
    file_obj = db.session.get(Files, file_id)
    if not file_obj or file_obj.extension not in verification_lib.VERIFY_EXT:
        return
    if not os.path.exists(file_obj.filepath):
        return
    opts = get_settings()['library']['management']['verification']
    if not opts['enabled']:
        return
    depth = opts['depth']
    logger.info(f'Verifying file ({depth}): {file_obj.filename}')
    signature_valid, hash_valid, hash_modified, error = verification_lib.verify(
        file_obj.filepath, depth, progress=_task_progress(_current_task_id))

    file_obj.signature_valid = signature_valid
    if hash_valid is not None:
        file_obj.hash_valid = hash_valid
        file_obj.hash_modified = hash_modified
    file_obj.verification_error = error
    file_obj.verified_at = datetime.datetime.now()
    db.session.commit()

    if error:
        logger.warning(f'Verification failed for {file_obj.filename}: {error}')
    enqueue_task('process_file', {'file_id': file_id})


# --- Compression pipeline ---
def _finalize_conversion(file_obj, target, new_extension, compressed):
    """Flip the Files row onto the verified output, then drop the now-redundant source."""
    source = file_obj.filepath
    add_ignored_event(source, '')  # our own deletion of the source
    file_obj.filepath = target
    file_obj.extension = new_extension
    file_obj.size = os.path.getsize(target)
    file_obj.mtime = os.path.getmtime(target)
    file_obj.compressed = compressed
    db.session.commit()
    if os.path.abspath(source) != os.path.abspath(target):
        os.remove(source)


def _convert_file(file_obj, produce, new_extension, compressed):
    """Run a (de)compression: produce the verified output at its final path, then finalize.
    Returns whether the row was flipped onto the new file - a caller that re-drives the
    pipeline must not do so after a no-op, or it delegates the same stage forever."""
    source = file_obj.filepath
    target = compression.conversion_target(file_obj)
    if Files.query.filter(Files.filepath == target, Files.id != file_obj.id).first() is not None:
        logger.warning(f'Skipping conversion of {os.path.basename(source)}: '
                       f'{os.path.basename(target)} is already in the library.')
        return False
    if not claim_temp_file(source):
        logger.debug(f'Skipping conversion of {os.path.basename(source)}: file is busy.')
        return False
    before = file_obj.size
    add_temp_file(target)
    try:
        out = str(produce(source, os.path.dirname(source)))
        _finalize_conversion(file_obj, out, new_extension, compressed)
    finally:
        remove_temp_file(target)
        remove_temp_file(source)
    after = file_obj.size
    ratio = after / before if before else 0
    verb = 'compressing' if compressed else 'decompressing'
    logger.info(f'Finished {verb} {os.path.basename(target)}: '
                f'{human_size(before)} -> {human_size(after)} (ratio {ratio:.1%})')
    return True


@register_task('compress_file', group='io')
def compress_file_task(file_id, **kwargs):
    """Compress a single file in place: NSP->NSZ / XCI->XCZ, preserving its DB row."""
    file_obj = db.session.get(Files, file_id)
    if not file_obj or file_obj.compressed or file_obj.extension not in COMPRESS_EXT:
        return
    if not os.path.exists(file_obj.filepath):
        return
    logger.info(f'Compressing file: {file_obj.filename}')
    opts = get_settings()['library']['management']['compression']
    if not opts['enabled']:
        return
    progress = _task_progress(_current_task_id)
    if _convert_file(file_obj,
                     lambda source, out_dir: compression.compress_to(source, out_dir, opts, progress=progress),
                     COMPRESS_EXT[file_obj.extension], True):
        enqueue_task('process_file', {'file_id': file_id})


@register_task('decompress_file', group='io')
def decompress_file_task(file_id, **kwargs):
    """Decompress a single file in place: NSZ->NSP / XCZ->XCI, preserving its DB row."""
    file_obj = db.session.get(Files, file_id)
    if not file_obj or not file_obj.compressed or file_obj.extension not in DECOMPRESS_EXT:
        return
    if not os.path.exists(file_obj.filepath):
        return
    progress = _task_progress(_current_task_id)
    _convert_file(file_obj,
                  lambda source, out_dir: compression.decompress_to(source, out_dir, progress=progress),
                  DECOMPRESS_EXT[file_obj.extension], False)


@register_cleanup('compress_file')
@register_cleanup('decompress_file')
def _compression_cleanup(file_id, **kwargs):
    """Idempotent cancel/crash cleanup: clear the in-progress mark, remove the partial
    output if it isn't a committed file, and pop the source-deletion ignored event."""
    file_obj = db.session.get(Files, file_id)
    if not file_obj:
        return
    remove_temp_file(file_obj.filepath)  # release the source in-progress claim
    target = compression.conversion_target(file_obj)
    if target:
        if Files.query.filter_by(filepath=target).first() is None and os.path.exists(target):
            add_ignored_event(target, '')  # our own deletion of the partial output
            os.remove(target)
        remove_temp_file(target)
    pop_ignored_event(src_path=file_obj.filepath, dest_path='')

# --- Batch maintenance ---
@register_task('add_missing_apps')
def add_missing_apps_task(**kwargs):
    """Batch: expand missing apps for every title. Used post-titledb-update."""
    add_missing_apps_to_db()
    enqueue_task('update_titles')


@register_task('remove_missing_files')
def remove_missing_files_task(**kwargs):
    """Delete DB entries for files missing from disk, then recompute all title flags."""
    remove_missing_files_from_db()
    enqueue_task('update_titles')


@register_task('update_titles')
def update_titles_task(**kwargs):
    """Batch: recompute flags for every title. Used post-titledb-update."""
    update_titles()


# --- Library lifecycle ---
@register_task('remove_library')
def remove_library_task(library_path, **kwargs):
    """Delete a library and its files (flipping app ownership), then recompute titles."""
    library = Libraries.query.filter_by(path=library_path).first()
    if not library:
        return
    for file_id in [f.id for f in library.files]:
        remove_file_from_apps(file_id)
    db.session.delete(library)
    db.session.commit()
    logger.info(f"Removed library: {library_path}")
    enqueue_task('update_titles')


# --- Watcher event handlers ---
@register_task('handle_file_added')
def handle_file_added_task(library_path, filepath, **kwargs):
    file = Files.query.filter_by(filepath=filepath).first()
    if file is None:
        enqueue_task('add_file', {'library_path': library_path, 'filepath': filepath})
        return

    new_size = titles_lib.get_file_size(filepath)
    new_mtime = os.path.getmtime(filepath)
    if file.size == new_size and file.mtime == new_mtime:
        return

    logger.info(f'File changed on disk, re-identifying: {file.filename}')
    remove_file_from_apps(file.id)
    file.size = new_size
    file.mtime = new_mtime
    file.organized = False
    reset_file_identification(file)
    reset_file_verification(file)
    db.session.commit()
    enqueue_task('process_file', {'file_id': file.id})


@register_task('handle_file_moved')
def handle_file_moved_task(library_path, src_path, dest_path, **kwargs):
    if file_exists_in_db(src_path):
        update_file_path(library_path, src_path, dest_path)
    else:
        enqueue_task('add_file', {'library_path': library_path, 'filepath': dest_path})


@register_task('handle_file_deleted')
def handle_file_deleted_task(filepath, **kwargs):
    delete_file_by_filepath(filepath)
    enqueue_task('update_titles')


@register_task('handle_dir_deleted')
def handle_dir_deleted_task(dirpath, **kwargs):
    """A folder was moved out/removed: delete all its files from the library."""
    if delete_files_under_dir(dirpath):
        enqueue_task('update_titles')
