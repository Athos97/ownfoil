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
                get_download_by_app, get_download_by_id)
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


class ExplodingPool:
    """A pool whose restart_worker always raises, standing in for a transient
    failure (e.g. a lock timeout) while a worker process is being replaced."""
    def restart_worker(self, worker_id):
        raise RuntimeError('boom')


def test_cancel_survives_a_worker_restart_failure(library, monkeypatch):
    """cancel_task used to let a restart_worker exception escape uncaught,
    skipping the cleanup hook and the parent re-check that follow it - leaking
    the task's cleanup side-effects and potentially stranding its parent in
    waiting_for_children. A restart failure must not stop cancellation."""
    import app as app_mod
    from db import record_task_history  # noqa: F401 (imported for readability)

    cleaned_up = []
    monkeypatch.setitem(tasks_mod.TASK_CLEANUP, 'ghosteshop_download',
                        lambda **kw: cleaned_up.append(kw))

    parent = Task(task_name='downloader_ghosteshop_run', status='waiting_for_children',
                 input_hash='hp', input_json='{}')
    db.session.add(parent)
    db.session.flush()
    child = Task(task_name='ghosteshop_download', status='running', worker_id=7,
                parent_id=parent.id, input_hash='hc',
                input_json='{"app_id": "x", "app_version": "1"}')
    db.session.add(child)
    db.session.commit()
    parent_id, child_id = parent.id, child.id

    monkeypatch.setattr(app_mod, 'pool', ExplodingPool())

    assert tasks_mod.cancel_task(child_id)

    assert cleaned_up == [{"app_id": "x", "app_version": "1"}]
    # The child is gone and was the parent's only child, so the parent - whose
    # completion re-check must still have run despite the restart failure -
    # completed right behind it instead of being stranded.
    assert db.session.get(Task, child_id) is None
    assert db.session.get(Task, parent_id) is None


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


def test_progress_cb_mirrors_onto_the_driving_task(library, monkeypatch):
    """A ghosteshop_download task's completion_pct used to sit at 0% for the
    whole transfer (verify_file/compress_file were the only task types that
    reported real progress) - the Tasks page papered over it with a fake
    full-width animated bar. task_progress must receive the same percentage
    _make_progress_cb writes onto the Download row."""
    row = downloader_lib.queue_ghosteshop_download(
        title_id=ZELDA_TID, app_id=ZELDA_UPD_TID, app_version='1114112',
        app_type='UPDATE', name='Zelda BOTW')

    reported = []
    cb = downloader_lib._make_progress_cb(row.id, task_progress=reported.append)
    cb(50, 100)  # first call: elapsed-since-last-write is huge, so this always writes

    assert reported == [50]
    assert get_download_by_id(row.id).progress == 50


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


# --- pause / resume / bulk row management ---

def test_pause_ghost_marks_row_and_cancels_task(library, portal, tmp_path):
    """Pausing a Ghost download: the row flips to paused FIRST, then its task is
    cancelled - and the cancellation's cleanup leaves the paused row alone
    (that is the whole point of writing paused before cancelling)."""
    settings = ghost_settings(portal, tmp_path)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(tasks_mod, 'get_settings', lambda: settings)
    monkeypatch.setattr(downloader_lib, 'get_settings', lambda: settings)

    row = downloader_lib.queue_ghosteshop_download(
        title_id=ZELDA_TID, app_id=ZELDA_UPD_TID, app_version='1114112',
        app_type='UPDATE', name='Zelda BOTW')
    update_download(row.id, status='downloading', progress=30)
    task = tasks_mod.enqueue_task(downloader_lib.GHOSTESHOP_DOWNLOAD_TASK,
                                  {'app_id': ZELDA_UPD_TID,
                                   'app_version': '1114112'})[0]

    ok, msg = downloader_lib.pause_download(row.id)

    assert ok
    row = get_download_by_app(ZELDA_UPD_TID, '1114112')
    assert row.status == 'paused'
    assert row.progress == 30, "paused keeps where it was for the UI"
    assert tasks_mod.Task.query.filter_by(id=task.id).first() is None, \
        "the driving task is cancelled away"
    monkeypatch.undo()


def test_resume_ghost_requeues_and_encoles_task(library, portal, tmp_path):
    """Resuming: row back to queued and a fresh per-file task enqueued."""
    settings = ghost_settings(portal, tmp_path)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(tasks_mod, 'get_settings', lambda: settings)
    monkeypatch.setattr(downloader_lib, 'get_settings', lambda: settings)

    row = downloader_lib.queue_ghosteshop_download(
        title_id=ZELDA_TID, app_id=ZELDA_UPD_TID, app_version='1114112',
        app_type='UPDATE', name='Zelda BOTW')
    update_download(row.id, status='paused', progress=30)

    ok, _msg = downloader_lib.resume_download(row.id)

    assert ok
    row = get_download_by_app(ZELDA_UPD_TID, '1114112')
    assert row.status == 'queued'
    assert row.progress == 0
    tasks = tasks_mod.Task.query.filter_by(
        task_name=downloader_lib.GHOSTESHOP_DOWNLOAD_TASK).all()
    assert len(tasks) == 1
    assert json.loads(tasks[0].input_json)['app_id'] == ZELDA_UPD_TID
    monkeypatch.undo()


def test_cleanup_hook_keeps_paused_rows(library):
    """The cancellation cleanup must not requeue a row the user paused."""
    row = downloader_lib.queue_ghosteshop_download(
        title_id=ZELDA_TID, app_id=ZELDA_UPD_TID, app_version='1114112',
        app_type='UPDATE', name='Zelda BOTW')
    update_download(row.id, status='paused', progress=30)

    tasks_mod._run_cleanup_hook(downloader_lib.GHOSTESHOP_DOWNLOAD_TASK,
                                json.dumps({'app_id': ZELDA_UPD_TID,
                                            'app_version': '1114112'}))
    assert get_download_by_app(ZELDA_UPD_TID, '1114112').status == 'paused'


class FakeQbtClient:
    """Stands in for qbittorrent.QbittorrentClient in the pause/resume paths."""
    def __init__(self, settings):
        self.calls = []
        self.torrents = FakeQbtClient.known
        FakeQbtClient.calls = self.calls

    known = [{'hash': 'a' * 40, 'name': 'Some Game', 'state': 'downloading',
              'progress': 0.4}]

    def login(self):
        return True, None

    def get_torrents(self, hashes=None, category=None):
        return self.torrents

    def find_hash_by_name(self, name, category=None):
        return 'a' * 40 if self.torrents else None

    def pause_torrent(self, info_hash):
        self.calls.append(('pause', info_hash))
        return True, None

    def resume_torrent(self, info_hash):
        self.calls.append(('resume', info_hash))
        return True, None


def _torrent_row(**kw):
    defaults = dict(title_id='0100ABCDEFDEF000', app_id='0100ABCDEFDEF800',
                    app_version='65536', app_type='UPDATE', name='Some Game',
                    source='torrents', torrent_hash='a' * 40,
                    torrent_name='Some Game', status='downloading', progress=40)
    defaults.update(kw)
    row = Download(**defaults)
    db.session.add(row)
    db.session.commit()
    return row


def test_pause_and_resume_torrent_calls_qbt(library, monkeypatch):
    settings = {'downloader': {'torrents': {'qbittorrent': {'url': 'http://q'}}}}
    monkeypatch.setattr(downloader_lib.qbittorrent, 'QbittorrentClient', FakeQbtClient)

    row = _torrent_row()
    ok, _msg = downloader_lib.pause_download(row.id, settings)
    assert ok
    assert get_download_by_id(row.id).status == 'paused'
    assert FakeQbtClient.calls == [('pause', 'a' * 40)]

    ok, _msg = downloader_lib.resume_download(row.id, settings)
    assert ok
    assert get_download_by_id(row.id).status == 'downloading'
    assert FakeQbtClient.calls[-1] == ('resume', 'a' * 40)


def test_resume_missing_torrent_fails_with_reason(library, monkeypatch):
    settings = {'downloader': {'torrents': {'qbittorrent': {'url': 'http://q'}}}}
    FakeQbtClient.known = []  # qBittorrent no longer has it
    monkeypatch.setattr(downloader_lib.qbittorrent, 'QbittorrentClient', FakeQbtClient)

    row = _torrent_row(status='paused', torrent_hash=None)
    ok, msg = downloader_lib.resume_download(row.id, settings)
    assert not ok
    refreshed = get_download_by_id(row.id)
    assert refreshed.status == 'failed'
    assert 'no longer' in refreshed.error.lower()
    FakeQbtClient.known = [{'hash': 'a' * 40, 'name': 'Some Game',
                            'state': 'downloading', 'progress': 0.4}]


def test_pause_all_pauses_unfinished_only(library, monkeypatch):
    settings = {'downloader': {'torrents': {'qbittorrent': {'url': 'http://q'}}}}
    monkeypatch.setattr(downloader_lib.qbittorrent, 'QbittorrentClient', FakeQbtClient)

    active = _torrent_row()
    queued = _torrent_row(app_id='0100ABCDEFDEF801', torrent_hash='b' * 40,
                          status='queued', progress=0)
    done = _torrent_row(app_id='0100ABCDEFDEF802', torrent_hash='c' * 40,
                        status='completed', progress=100)

    paused = downloader_lib.pause_all_downloads()

    assert paused == 2
    statuses = {r.app_id: r.status for r in Download.query.all()}
    assert statuses[active.app_id] == 'paused'
    assert statuses[queued.app_id] == 'paused'
    assert statuses[done.app_id] == 'completed'


def test_delete_completed_removes_only_completed(library):
    done = _torrent_row(status='completed', progress=100)
    failed = _torrent_row(app_id='0100ABCDEFDEF803', torrent_hash='d' * 40,
                          status='failed', error='no match')
    done_id, failed_id = done.id, failed.id

    removed = downloader_lib.delete_completed_downloads()

    assert removed == 1
    assert get_download_by_id(done_id) is None
    assert get_download_by_id(failed_id) is not None, "failed rows stay retryable"


def test_sync_maps_qbittorrent_paused_state(library, monkeypatch):
    """A torrent paused from qBittorrent's own UI surfaces as a paused row."""
    settings = {'downloader': {'torrents': {'qbittorrent': {'url': 'http://q'}}}}
    FakeQbtClient.known = [{'hash': 'a' * 40, 'name': 'Some Game',
                            'state': 'pausedDL', 'progress': 0.4}]
    monkeypatch.setattr(downloader_lib.qbittorrent, 'QbittorrentClient', FakeQbtClient)
    row = _torrent_row()

    downloader_lib.sync_downloads_status(settings)

    assert get_download_by_id(row.id).status == 'paused'
    FakeQbtClient.known = [{'hash': 'a' * 40, 'name': 'Some Game',
                            'state': 'downloading', 'progress': 0.4}]


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


# --- trash removes the partial content ---

def test_delete_paused_row_sweeps_its_part_files(library, portal, tmp_path):
    """Trashing a paused Ghost download removes the row AND the half-fetched
    bytes from disk - not at the next pass's orphan GC, now."""
    settings = ghost_settings(portal, tmp_path)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(downloader_lib, 'get_settings', lambda: settings)

    row = downloader_lib.queue_ghosteshop_download(
        title_id=ZELDA_TID, app_id=ZELDA_UPD_TID, app_version='1114112',
        app_type='UPDATE', name='Zelda BOTW')
    downloader_lib.update_download(row.id, status='paused', progress=30,
                                   torrent_name=ZELDA_UPD_NAME)

    part = tmp_path / 'Zelda' / (ZELDA_UPD_NAME + '.part')
    state = tmp_path / 'Zelda' / (ZELDA_UPD_NAME + '.part.state')
    part.parent.mkdir(exist_ok=True)
    part.write_bytes(b'half a game')
    state.write_text('{}')

    assert downloader_lib.delete_download_row(row.id)

    assert get_download_by_app(ZELDA_UPD_TID, '1114112') is None
    assert not part.exists(), "the partial file went with the row"
    assert not state.exists()
    monkeypatch.undo()


def test_delete_torrent_row_leaves_qbittorrent_data_alone(library, tmp_path):
    """Torrents rows delete the row only: qBittorrent's data is its own."""
    row = _torrent_row(status='paused')
    # A same-named file existing in a library root must survive - it is not
    # ownfoil's to remove.
    stray = tmp_path / 'stray.nsp'
    stray.write_bytes(b'not ours')

    assert downloader_lib.delete_download_row(row.id)
    assert get_download_by_id(row.id) is None
    assert stray.exists()


def test_delete_row_without_part_is_just_the_row(library):
    """A queued row that never transferred (no torrent_name) sweeps nothing."""
    row = downloader_lib.queue_ghosteshop_download(
        title_id=ZELDA_TID, app_id='01007EF00011F009', app_version='0',
        app_type='DLC', name='Never started')
    assert downloader_lib.delete_download_row(row.id)
    assert Download.query.filter_by(app_id='01007EF00011F009').count() == 0
