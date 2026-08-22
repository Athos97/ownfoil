"""Process-resilience behavior: cancellation cleanup, orphan healing end-to-end,
watchdog-adjacent claim logic, and retry wiring."""
import json

import pytest

import db as db_mod
import downloader as downloader_lib
import tasks as tasks_mod
import worker as worker_mod
from app import create_app
from db import (db, init_db, Task, Download, update_download,
                get_download_by_app)
from mock_ghostshop_portal import MockPortal, USER, PASS

ZELDA_TID = '01007EF00011E000'
ZELDA_UPD_TID = '01007EF00011E800'
ZELDA_UPD_NAME = 'Zelda BOTW [01007EF00011E800][v1114112]'


@pytest.fixture
def library(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(db_mod, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(db_mod, "TITLES_DB_FILE", str(config / "titles.db"))
    app = create_app(f"sqlite:///{config / 'ownfoil.db'}")
    init_db(app)
    with app.app_context():
        yield app


@pytest.fixture(scope='module')
def portal():
    mock = MockPortal().start()
    yield mock
    mock.stop()


def ghost_settings(portal, path):
    return {'downloader': {'ghosteshop': {
        'enabled': True, 'url': portal.url, 'username': USER,
        'password': PASS, 'library_path': str(path)}}}


# --- F1: cleanup hook ---

def test_cancelled_transfer_task_requeues_its_row(library, portal, tmp_path):
    """The incident this pins: cancelling a running ghosteshop_download left its
    downloads row 'downloading' forever (healing only at the next pass). The
    cleanup hook puts it back to queued, keeping the .part for resume."""
    monkeypatch_settings = ghost_settings(portal, tmp_path)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(tasks_mod, 'get_settings', lambda: monkeypatch_settings)

    row = downloader_lib.queue_ghosteshop_download(
        title_id=ZELDA_TID, app_id=ZELDA_UPD_TID, app_version='1114112',
        app_type='UPDATE', name='Zelda BOTW')
    task = tasks_mod.enqueue_task(
        downloader_lib.GHOSTESHOP_DOWNLOAD_TASK,
        {'app_id': ZELDA_UPD_TID, 'app_version': '1114112'})[0]
    update_download(row.id, status='downloading', progress=42)

    # What cancel_task runs when the row was running: delete + cleanup hook.
    tasks_mod._run_cleanup_hook(downloader_lib.GHOSTESHOP_DOWNLOAD_TASK,
                                json.dumps({'app_id': ZELDA_UPD_TID,
                                            'app_version': '1114112'}))

    row = get_download_by_app(ZELDA_UPD_TID, '1114112')
    assert row.status == 'queued'
    assert row.progress == 0
    assert 'Cancelled' in (row.error or '')
    monkeypatch.undo()


def test_cleanup_hook_leaves_terminal_rows_alone(library, portal, tmp_path):
    """A completed/failed row must not be 'cleaned' back to queued."""
    row = downloader_lib.queue_ghosteshop_download(
        title_id=ZELDA_TID, app_id=ZELDA_UPD_TID, app_version='1114112',
        app_type='UPDATE', name='Zelda BOTW')
    update_download(row.id, status='completed', progress=100)
    tasks_mod._run_cleanup_hook(downloader_lib.GHOSTESHOP_DOWNLOAD_TASK,
                                json.dumps({'app_id': ZELDA_UPD_TID,
                                            'app_version': '1114112'}))
    assert get_download_by_app(ZELDA_UPD_TID, '1114112').status == 'completed'


# --- F2: pass cancellation re-arms the schedule ---

def test_cancelled_pass_rearms_schedule(library, monkeypatch):
    settings = {'downloader': {
        'torrents': {'enabled': True, 'interval': '2h',
                     'jackett': {'url': 'u', 'api_key': 'k'},
                     'qbittorrent': {'url': 'q'}},
        'ghosteshop': {'enabled': True, 'interval': '5h', 'url': 'x',
                       'username': 'u', 'password': 'p'}}}
    monkeypatch.setattr(tasks_mod, 'get_settings', lambda: settings)
    monkeypatch.setattr(tasks_mod.downloader_lib, 'torrents_configured',
                        lambda s: True)
    monkeypatch.setattr(tasks_mod.downloader_lib, 'ghosteshop_configured',
                        lambda s: True)

    tasks_mod._run_cleanup_hook('downloader_ghosteshop_run', '{}')

    row = Task.query.filter_by(task_name='downloader_ghosteshop_run',
                               status='pending').first()
    assert row is not None and row.run_after is not None, \
        "cancelling a pass must leave a scheduled row behind"


# --- F3: claim ignores rows of dead workers ---

class FakePool:
    def __init__(self, live):
        self._live = live

    def live_worker_ids(self):
        return self._live


def test_claim_skips_running_rows_of_dead_workers(library, monkeypatch):
    """A ghost 'running' row from a crashed worker must not pin the io group:
    with io=1 and a dead owner, another worker still has to claim io tasks."""
    import app as app_mod
    task = Task(task_name='ghosteshop_download', status='running',
                worker_id=7, input_hash='h',
                input_json='{"app_id": "x", "app_version": "1"}')
    db.session.add(task)
    pending = Task(task_name='ghosteshop_download', status='pending',
                   input_hash='h2', input_json='{"app_id": "y", "app_version": "2"}')
    db.session.add(pending)
    db.session.commit()

    from worker import TaskWorker
    tw = TaskWorker(library, poll_interval=0.01, worker_id=9)

    # Pool says worker 7 is dead -> the ghost row does not block the group.
    monkeypatch.setattr(app_mod, 'pool', FakePool(live={9}))
    claimed = tw.claim_task()
    assert claimed == pending.id

    # No pool knowledge (standalone worker) -> the ghost running row still
    # counts and holds the only io slot: nothing is claimable.
    from sqlalchemy import text as _text
    db.session.execute(_text(
        "UPDATE tasks SET status='pending' WHERE id = :id"), {"id": pending.id})
    db.session.commit()
    monkeypatch.setattr(app_mod, 'pool', None)
    assert tw.claim_task() is None


# --- F4: cooperative cancellation aborts the transfer ---

def test_transfer_aborts_when_task_row_disappears(library, portal, tmp_path, monkeypatch):
    """Deleting the task row mid-transfer (what cancel does) must abort the
    transfer via the progress callback instead of running to completion."""
    settings = ghost_settings(portal, tmp_path)
    monkeypatch.setattr(downloader_lib.titles_lib, 'get_game_info',
                        lambda tid: {'name': 'Zelda BOTW'})

    task = Task(task_name='ghosteshop_download', status='running', worker_id=1,
                input_hash='h', input_json='{"app_id": "%s", "app_version": "1114112"}' % ZELDA_UPD_TID)
    db.session.add(task)
    db.session.commit()

    row = downloader_lib.queue_ghosteshop_download(
        title_id=ZELDA_TID, app_id=ZELDA_UPD_TID, app_version='1114112',
        app_type='UPDATE', name='Zelda BOTW')

    cb = downloader_lib._make_progress_cb(row.id, task_id=task.id)
    monkeypatch.setattr(downloader_lib, 'CANCEL_CHECK_EVERY', 1)
    cb(100, 1000)   # row alive: fine

    Task.query.filter_by(id=task.id).delete()
    db.session.commit()

    with pytest.raises(downloader_lib.TransferCancelled):
        cb(500, 1000)  # row gone: abort


# --- F7: startup requeues mid-transfer rows ---

def test_startup_requeues_interrupted_ghost_rows(library):
    db.session.add(Download(title_id=ZELDA_TID, app_id=ZELDA_UPD_TID,
                            app_version='1114112', app_type='UPDATE',
                            name='Zelda', source='ghosteshop',
                            status='downloading', progress=55))
    db.session.commit()

    tasks_mod.cleanup_tasks()

    row = get_download_by_app(ZELDA_UPD_TID, '1114112')
    assert row.status == 'queued'
    assert row.progress == 0


# --- per-file retry pass arming ---

def test_failed_rows_arm_an_early_retry(library, monkeypatch):
    settings = {'downloader': {'ghosteshop': {'enabled': True, 'interval': '24h',
                                              'url': 'x', 'username': 'u',
                                              'password': 'p'}}}
    monkeypatch.setattr(tasks_mod, 'get_settings', lambda: settings)
    monkeypatch.setattr(tasks_mod.downloader_lib, 'ghosteshop_configured',
                        lambda s: True)
    db.session.add(Download(title_id=ZELDA_TID, app_id=ZELDA_UPD_TID,
                            app_version='1114112', app_type='UPDATE',
                            name='Zelda', source='ghosteshop',
                            status='failed', error='network down'))
    db.session.commit()

    tasks_mod._arm_ghosteshop_retry(settings)

    row = Task.query.filter_by(task_name='downloader_ghosteshop_run',
                               status='pending').first()
    assert row is not None
    assert row.run_after is not None
    from datetime import datetime as _dt
    delta = row.run_after.replace(tzinfo=None) - _dt.utcnow()
    assert delta.total_seconds() < 16 * 60, "first retry within ~15 minutes"


# --- stop & wipe ---

def test_stop_all_cancels_tasks_wipes_rows_and_parts(library, portal, tmp_path):
    """'Stop & delete all': tasks cancelled, every downloads row gone (history
    included) and the .part residue wiped from disk."""
    settings = ghost_settings(portal, tmp_path)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(tasks_mod, 'get_settings', lambda: settings)
    monkeypatch.setattr(downloader_lib, 'get_settings', lambda: settings)

    downloader_lib.queue_ghosteshop_download(
        title_id=ZELDA_TID, app_id=ZELDA_UPD_TID, app_version='1114112',
        app_type='UPDATE', name='Zelda BOTW')
    pass_task = tasks_mod.enqueue_task('downloader_ghosteshop_run', {'manual': True})[0]
    child = tasks_mod.create_child_task(
        pass_task.id, downloader_lib.GHOSTESHOP_DOWNLOAD_TASK,
        {'app_id': ZELDA_UPD_TID, 'app_version': '1114112'})
    db.session.add(Task(task_name='ghosteshop_download', status='running',
                        worker_id=99, input_hash='h2',
                        input_json='{"app_id": "0100AA000000E800", "app_version": "65536"}'))
    db.session.commit()

    part = tmp_path / 'Zelda' / (ZELDA_UPD_NAME + '.part')
    part.parent.mkdir(exist_ok=True)
    part.write_bytes(b'partial bytes')
    (tmp_path / 'Zelda' / (ZELDA_UPD_NAME + '.part.state')).write_text('{}')

    removed = downloader_lib.stop_all_downloads()

    assert removed >= 1
    assert Download.query.count() == 0
    assert Task.query.filter(Task.task_name.in_(
        ['downloader_ghosteshop_run', 'ghosteshop_download'])).count() == 0
    assert not part.exists()
    assert not (tmp_path / 'Zelda' / (ZELDA_UPD_NAME + '.part.state')).exists()
    monkeypatch.undo()


# --- add content: owned by any version ---

def test_queue_skips_content_owned_under_another_version(library):
    from db import Apps, Titles as TitlesRow, is_app_id_owned
    title = TitlesRow(title_id=ZELDA_TID, have_base=True)
    db.session.add(title)
    db.session.flush()
    db.session.add(Apps(title_id=title.id, app_id=ZELDA_UPD_TID,
                        app_version='65536', app_type='UPDATE', owned=True))
    db.session.commit()

    assert is_app_id_owned(ZELDA_UPD_TID)
    assert not is_app_id_owned('0100FF00FF00F800')

    # The mutation path: queueing version 131072 (not the owned 65536) is a no-op.
    from gql import graphql_dispatch
    app = library
    app.add_url_rule('/api/graphql', view_func=graphql_dispatch,
                     methods=['GET', 'POST'])
    resp = app.test_client().post('/api/graphql', json={
        'query': 'mutation($e: [QueuedDownloadInput!]!) { queueGhosteshopDownloads(entries: $e) }',
        'variables': {'e': [{
            'titleId': ZELDA_TID, 'appId': ZELDA_UPD_TID, 'appVersion': 131072,
            'appType': 'UPDATE', 'name': 'Zelda', 'fileName': 'x.nsz'}]}})
    assert resp.get_json()['data']['queueGhosteshopDownloads'] == 0
    assert Download.query.filter_by(app_id=ZELDA_UPD_TID).count() == 0
