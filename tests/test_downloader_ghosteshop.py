"""Ghost eShop lane of the downloader: destination paths, row lifecycle, queue flow."""
import json
import os

import pytest

import db as db_mod
import downloader as downloader_lib
import tasks as tasks_mod
import ghostshop
from app import create_app
from db import db, init_db, Download, Task, Titles, Apps
from ghostshop.types import CatalogEntry
from mock_ghostshop_portal import MockPortal, USER, PASS, blob_for

ZELDA_TID = '01007EF00011E000'
ZELDA_UPD_TID = '01007EF00011E800'
ZELDA_UPD_NAME = 'Zelda BOTW [01007EF00011E800][v1114112]'
ZELDA_UPD_SIZE = 300_000


@pytest.fixture(scope='module')
def portal():
    mock = MockPortal().start()
    yield mock
    mock.stop()


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


def ghost_settings(portal, **extra):
    base = {
        'enabled': True,
        'url': portal.url,
        'username': USER,
        'password': PASS,
        'library_path': '',
    }
    base.update(extra)
    return {'downloader': {'ghosteshop': base}}


def settings_with(library_path, ghost):
    ghost['downloader']['ghosteshop']['library_path'] = library_path
    return ghost


TARGET = {
    'title_id': ZELDA_TID,
    'app_id': ZELDA_UPD_TID,
    'app_version': '1114112',
    'app_type': 'UPDATE',
    'name': 'Zelda BOTW',
    'patch_level': 17,
}


def test_destination_lands_in_the_game_folder(library, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(downloader_lib, 'get_library_paths', lambda: ['/games'])
    monkeypatch.setattr(downloader_lib.titles_lib, 'get_game_info',
                        lambda tid: {'name': 'The Legend of Zelda: Breath of the Wild'})
    entry = CatalogEntry(name=ZELDA_UPD_NAME, tid=ZELDA_UPD_TID, category='UPDATE',
                         version=1114112, size=ZELDA_UPD_SIZE)
    dest = downloader_lib._ghost_destination(
        entry, TARGET, {'downloader': {'ghosteshop': {}},
                        'library': {'management': {'organizer': {}}}})
    parts = Path(dest).parts
    assert parts[-2] == 'The Legend of Zelda: Breath of the Wild'
    assert parts[-1] == ZELDA_UPD_NAME
    assert parts[0] == os.sep or Path(dest).is_absolute()


def test_download_target_success_writes_file_and_row(library, portal, tmp_path, monkeypatch):
    monkeypatch.setattr(downloader_lib.titles_lib, 'get_game_info',
                        lambda tid: {'name': 'Zelda BOTW'})
    settings = settings_with(str(tmp_path), ghost_settings(portal))
    ok = downloader_lib.download_target_ghosteshop(TARGET, settings)

    assert ok
    row = Download.query.filter_by(app_id=ZELDA_UPD_TID).one()
    assert row.status == 'downloading'
    assert row.source == 'ghosteshop'
    assert row.indexer == 'Ghost eShop'
    assert row.size == ZELDA_UPD_SIZE
    assert row.progress == 100

    dest = tmp_path / 'Zelda BOTW' / ZELDA_UPD_NAME
    assert dest.is_file()
    assert dest.read_bytes() == blob_for(ZELDA_UPD_NAME, ZELDA_UPD_SIZE)


def test_download_target_marks_row_completed_when_already_owned(library, portal, tmp_path):
    settings = settings_with(str(tmp_path), ghost_settings(portal))
    title = Titles(title_id=ZELDA_TID, have_base=True)
    db.session.add(title)
    db.session.flush()
    db.session.add(Apps(title_id=title.id, app_id=ZELDA_UPD_TID,
                        app_version='1114112', app_type='UPDATE', owned=True))
    db.session.commit()

    ok = downloader_lib.download_target_ghosteshop(TARGET, settings)
    assert ok
    assert list(tmp_path.rglob('*.part')) == [], "nothing downloaded: already owned"


def test_download_target_not_in_catalog(library, portal, tmp_path, monkeypatch):
    settings = settings_with(str(tmp_path), ghost_settings(portal))
    target = dict(TARGET, app_id='0100FF00FF00F800', app_version='65536')

    ok = downloader_lib.download_target_ghosteshop(target, settings)
    assert not ok
    row = Download.query.filter_by(app_id='0100FF00FF00F800').one()
    assert row.status == 'failed'
    assert 'catalog' in row.error


def test_download_target_bad_credentials(library, portal, tmp_path):
    ghost = ghost_settings(portal)
    ghost['downloader']['ghosteshop']['password'] = 'wrong'
    settings = settings_with(str(tmp_path), ghost)

    ok = downloader_lib.download_target_ghosteshop(TARGET, settings)
    assert not ok
    row = Download.query.filter_by(app_id=ZELDA_UPD_TID).one()
    assert row.status == 'failed'


def test_job_processes_queued_rows_before_missing(library, portal, tmp_path, monkeypatch):
    """The pass computes targets; each target then downloads as its own io task
    (here driven directly, the way the worker drives it in production)."""
    monkeypatch.setattr(downloader_lib.titles_lib, 'get_game_info',
                        lambda tid: {'name': 'Zelda BOTW'})
    settings = settings_with(str(tmp_path), ghost_settings(portal))
    monkeypatch.setattr(downloader_lib, 'get_missing_targets', lambda: [])
    monkeypatch.setattr(tasks_mod, 'get_settings', lambda: settings)

    downloader_lib.queue_ghosteshop_download(
        title_id=ZELDA_TID, app_id=ZELDA_UPD_TID, app_version='1114112',
        app_type='UPDATE', name='Zelda BOTW')

    targets = downloader_lib.prepare_ghosteshop_targets(settings)
    assert [(t['app_id'], t['app_version']) for t in targets] == \
        [(ZELDA_UPD_TID, '1114112')]

    for t in targets:
        tasks_mod.ghosteshop_download_task(
            app_id=t['app_id'], app_version=t['app_version'], name=t['name'])

    row = Download.query.filter_by(app_id=ZELDA_UPD_TID).one()
    assert row.status == 'downloading'
    assert row.progress == 100
    dest = tmp_path / 'Zelda BOTW' / ZELDA_UPD_NAME
    assert dest.is_file()
    assert dest.read_bytes() == blob_for(ZELDA_UPD_NAME, ZELDA_UPD_SIZE)


def test_orphan_downloading_rows_are_requeued(library, portal, tmp_path):
    """A 'downloading' row whose per-file task is gone (cancelled pass, restart)
    is healed back to queued and re-listed; one with a live task is left alone."""
    settings = settings_with(str(tmp_path), ghost_settings(portal))
    row = downloader_lib.add_download(
        title_id=ZELDA_TID, app_id=ZELDA_UPD_TID, app_version='1114112',
        app_type='UPDATE', name='Zelda BOTW', source='ghosteshop',
        status='downloading', progress=37)

    # No task alive -> requeued and listed.
    targets = downloader_lib.prepare_ghosteshop_targets(settings)
    assert (ZELDA_UPD_TID, '1114112') in [(t['app_id'], t['app_version']) for t in targets]
    assert row.status == 'queued'
    assert row.progress == 0

    # A live child task -> the row is respected and not re-listed.
    downloader_lib.update_download(row.id, status='downloading', progress=40)
    child_input = {'app_id': ZELDA_UPD_TID, 'app_version': '1114112'}
    db.session.add(Task(task_name=downloader_lib.GHOSTESHOP_DOWNLOAD_TASK,
                        status='pending',
                        input_json=json.dumps(child_input),
                        input_hash=tasks_mod.compute_input_hash(child_input)))
    db.session.commit()
    targets = downloader_lib.prepare_ghosteshop_targets(settings)
    assert (ZELDA_UPD_TID, '1114112') not in \
        [(t['app_id'], t['app_version']) for t in targets]
    assert row.status == 'downloading'


def test_sources_do_not_steal_each_others_rows(library, portal, tmp_path, monkeypatch):
    monkeypatch.setattr(downloader_lib.titles_lib, 'get_game_info',
                        lambda tid: {'name': 'Zelda BOTW'})
    """A row claimed by the ghosteshop source is invisible to the torrents pass:
    each source retries only its own failures."""
    dl_dir = tmp_path / 'dl'
    settings = settings_with(str(dl_dir), ghost_settings(portal))
    monkeypatch.setattr(downloader_lib, 'get_missing_targets', lambda: [TARGET])
    monkeypatch.setattr(downloader_lib, 'torrents_configured', lambda s: True)
    monkeypatch.setattr(downloader_lib.jackett, 'search',
                        lambda js, q, indexers=None: [])

    downloader_lib.queue_ghosteshop_download(
        title_id=ZELDA_TID, app_id=ZELDA_UPD_TID, app_version='1114112',
        app_type='UPDATE', name='Zelda BOTW')
    row = Download.query.filter_by(app_id=ZELDA_UPD_TID).one()

    downloader_lib.run_downloader_job(settings)
    assert row.status == 'queued', "the torrents pass must not touch ghosteshop rows"
    assert not dl_dir.exists() or list(dl_dir.rglob('*')) == [], \
        "the torrents pass must not download ghosteshop rows' content"


def test_ghosteshop_download_is_an_io_task():
    """The per-file download must share the Workers I/O budget with verification
    and compression - that is the whole point of splitting the pass."""
    assert tasks_mod.TASK_GROUPS.get(downloader_lib.GHOSTESHOP_DOWNLOAD_TASK) == 'io'
    # The pass itself orchestrates only: no group, so it never holds an I/O slot.
    assert downloader_lib.GHOSTESHOP_DOWNLOAD_TASK not in (
        tasks_mod.TASK_GROUPS.get('downloader_ghosteshop_run'),)


def test_parent_task_enqueues_one_child_per_target(library, portal, tmp_path, monkeypatch):
    settings = settings_with(str(tmp_path), ghost_settings(portal))
    monkeypatch.setattr(tasks_mod, 'get_settings', lambda: settings)
    monkeypatch.setattr(downloader_lib, 'get_missing_targets', lambda: [{
        'title_id': ZELDA_TID, 'app_id': ZELDA_UPD_TID, 'app_version': '1114112',
        'app_type': 'UPDATE', 'name': 'Zelda BOTW', 'patch_level': 17}])
    monkeypatch.setattr(tasks_mod, 'set_waiting_for_children', lambda: None)

    tasks_mod.downloader_ghosteshop_run_task()

    children = Task.query.filter_by(
        task_name=downloader_lib.GHOSTESHOP_DOWNLOAD_TASK).all()
    assert len(children) == 1
    child_input = json.loads(children[0].input_json)
    assert child_input['app_id'] == ZELDA_UPD_TID
    assert child_input['app_version'] == '1114112'


def test_child_creates_row_for_computed_missing_target(library, portal, tmp_path, monkeypatch):
    """A computed missing target arrives at the io task with no downloads row
    (only Add Content queues rows upfront); the task must create it, not no-op."""
    monkeypatch.setattr(downloader_lib.titles_lib, 'get_game_info',
                        lambda tid: {'name': 'Zelda BOTW'})
    settings = settings_with(str(tmp_path), ghost_settings(portal))
    ok = downloader_lib.download_ghosteshop_row(
        ZELDA_UPD_TID, '1114112', name='Zelda BOTW',
        title_id=ZELDA_TID, app_type='UPDATE', settings=settings)

    assert ok
    row = Download.query.filter_by(app_id=ZELDA_UPD_TID).one()
    assert row.source == 'ghosteshop'
    assert row.status == 'downloading'
    assert row.progress == 100
    assert (tmp_path / 'Zelda BOTW' / ZELDA_UPD_NAME).is_file()


def test_child_infers_family_fields_without_them(library, portal, tmp_path, monkeypatch):
    """Directly enqueued tasks may not carry title_id/app_type; DLC ids derive
    their base title by the same rule the filename parser uses."""
    monkeypatch.setattr(downloader_lib.titles_lib, 'get_game_info',
                        lambda tid: {'name': 'Death Howl'})
    settings = settings_with(str(tmp_path), ghost_settings(portal))
    downloader_lib.download_ghosteshop_row(
        '0100CF70241E8800', '131072', name='Death Howl update', settings=settings)
    row = Download.query.filter_by(app_id='0100CF70241E8800').one()
    assert row.title_id == '0100CF70241E8000'
    assert row.app_type == 'UPDATE'


def test_identification_completes_download_rows(library, portal, tmp_path):
    """The watcher identifying a fetched file is what completes its download
    row - immediately, not at the next downloader sync."""
    from db import complete_downloads_for_apps
    row = downloader_lib.add_download(
        title_id=ZELDA_TID, app_id=ZELDA_UPD_TID, app_version='1114112',
        app_type='UPDATE', name='Zelda', source='ghosteshop',
        status='downloading', progress=100)

    flipped = complete_downloads_for_apps([(ZELDA_UPD_TID, '1114112'),
                                           ('0100FF00FF00F800', '65536')])
    assert flipped == 1
    assert row.status == 'completed'
    assert row.error is None
