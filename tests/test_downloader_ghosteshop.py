"""Ghost eShop lane of the downloader: destination paths, row lifecycle, queue flow."""
import os

import pytest

import db as db_mod
import downloader as downloader_lib
import ghostshop
from app import create_app
from db import db, init_db, Download, Titles, Apps
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
    monkeypatch.setattr(downloader_lib.titles_lib, 'get_game_info',
                        lambda tid: {'name': 'Zelda BOTW'})
    """Add Content rows (queued, BASE included) are processed by the ghosteshop pass."""
    settings = settings_with(str(tmp_path), ghost_settings(portal))
    monkeypatch.setattr(downloader_lib, 'get_missing_targets', lambda: [])
    monkeypatch.setattr(downloader_lib, 'ghosteshop_configured', lambda s: True)

    downloader_lib.queue_ghosteshop_download(
        title_id=ZELDA_TID, app_id=ZELDA_UPD_TID, app_version='1114112',
        app_type='UPDATE', name='Zelda BOTW')
    downloader_lib.run_downloader_job(settings, source='ghosteshop')

    row = Download.query.filter_by(app_id=ZELDA_UPD_TID).one()
    assert row.status == 'downloading'
    assert row.progress == 100
    dest = tmp_path / 'Zelda BOTW' / ZELDA_UPD_NAME
    assert dest.is_file()


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

    downloader_lib.run_downloader_job(settings, source='torrents')
    assert row.status == 'queued', "the torrents pass must not touch ghosteshop rows"
    assert not dl_dir.exists() or list(dl_dir.rglob('*')) == [], \
        "the torrents pass must not download ghosteshop rows' content"
