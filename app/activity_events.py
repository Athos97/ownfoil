"""Realtime topic for the user activity feed.

Activity rows are written by the web process (shop requests, downloads, logins)
but reach this module's poller only through the database - the same poll-and-diff
shape as tasks and downloads, which also keeps a worker-originated event (none
today, but nothing forbids one) flowing for free.
"""
import logging

import realtime
from db import db

logger = logging.getLogger('main')

MAX_ACTIVITY = 500

_ACTIVITY_SQL = """
SELECT id, ts, kind, username, client, device_uid, ip, filename, size, detail
FROM (SELECT * FROM activity_events ORDER BY id DESC LIMIT ?)
ORDER BY id
"""

_activity_state = {}


def _utc(value):
    if not value:
        return None
    return str(value).replace(' ', 'T') + 'Z'


def _read_activity():
    """Current activity rows as id -> serialisable dict."""
    connection = db.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(_ACTIVITY_SQL, (MAX_ACTIVITY,))
        rows = cursor.fetchall()
    finally:
        connection.close()

    events = {}
    for (ev_id, ts, kind, username, client, device_uid, ip, filename, size,
         detail) in rows:
        events[ev_id] = {
            'id': ev_id,
            'ts': _utc(ts),
            'kind': kind,
            'username': username,
            'client': client,
            'deviceUid': device_uid,
            'ip': ip,
            'filename': filename,
            'size': size,
            'detail': detail,
        }
    return events


def _diff(previous, current):
    events = []
    for key, value in current.items():
        if key not in previous:
            events.append(('add', value))
    for key, value in previous.items():
        if key not in current:
            events.append(('remove', value))
    return events


def activity_snapshot():
    return list(_read_activity().values())


def activity_poll():
    global _activity_state
    current = _read_activity()
    events = _diff(_activity_state, current)
    _activity_state = current
    return events


realtime.register_topic('activity', access='admin', snapshot=activity_snapshot,
                        poll=activity_poll)
