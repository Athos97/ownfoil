"""Realtime topic for the downloader queue.

Download rows change in worker subprocesses (downloader_run) and through the
GraphQL mutations, both of which reach this process only through the database -
so the topic is polled and diffed, exactly like the tasks topic.
"""
import logging

import realtime
from db import db

logger = logging.getLogger('main')

MAX_DOWNLOADS = 500

_DOWNLOAD_SQL = """
SELECT id, title_id, app_id, app_version, app_type, name, torrent_hash,
       torrent_name, indexer, size, seeders, source, progress, status, error,
       created_at, updated_at
FROM downloads
ORDER BY id
LIMIT ?
"""

_downloads_state = {}


def _utc(value):
    if not value:
        return None
    return str(value).replace(' ', 'T') + 'Z'


def _read_downloads():
    """Current download rows as id -> serialisable dict."""
    connection = db.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(_DOWNLOAD_SQL, (MAX_DOWNLOADS,))
        rows = cursor.fetchall()
    finally:
        connection.close()

    downloads = {}
    for (dl_id, title_id, app_id, app_version, app_type, name, torrent_hash,
         torrent_name, indexer, size, seeders, source, progress, status, error,
         created_at, updated_at) in rows:
        downloads[dl_id] = {
            'id': dl_id,
            'titleId': title_id,
            'appId': app_id,
            'appVersion': app_version,
            'appType': app_type,
            'name': name,
            'torrentHash': torrent_hash,
            'torrentName': torrent_name,
            'indexer': indexer,
            'size': size,
            'seeders': seeders,
            'source': source,
            'progress': progress,
            'status': status,
            'error': error,
            'createdAt': _utc(created_at),
            'updatedAt': _utc(updated_at),
        }
    return downloads


def _diff(previous, current):
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


def downloads_snapshot():
    return list(_read_downloads().values())


def downloads_poll():
    global _downloads_state
    current = _read_downloads()
    events = _diff(_downloads_state, current)
    _downloads_state = current
    return events


realtime.register_topic('downloads', access='admin', snapshot=downloads_snapshot,
                        poll=downloads_poll)
