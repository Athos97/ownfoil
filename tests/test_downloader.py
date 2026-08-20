"""Unit tests for the downloader: target selection, result ranking and sync."""
import pytest

import db as db_mod
import downloader as downloader_lib
import jackett
import qbittorrent
from app import create_app
from db import db, init_db, Download, Titles, Apps


@pytest.fixture
def library(tmp_path, monkeypatch):
    """An app with an empty database and a one-title library:
    base owned, latest update unowned, one DLC unowned."""
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(db_mod, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(db_mod, "TITLES_DB_FILE", str(config / "titles.db"))

    app = create_app(f"sqlite:///{config / 'ownfoil.db'}")
    init_db(app)
    with app.app_context():
        title = Titles(title_id="0100ABCDEFDEF000", have_base=True, up_to_date=False)
        db.session.add(title)
        db.session.flush()
        db.session.add(Apps(title_id=title.id, app_id="0100ABCDEFDEF000",
                            app_version="0", app_type="BASE", owned=True))
        db.session.add(Apps(title_id=title.id, app_id="0100ABCDEFDEF800",
                            app_version="65536", app_type="UPDATE", owned=False))
        db.session.add(Apps(title_id=title.id, app_id="0100ABCDEFDEF800",
                            app_version="196608", app_type="UPDATE", owned=False))
        db.session.add(Apps(title_id=title.id, app_id="0100ABCDEFDEF100",
                            app_version="0", app_type="DLC", owned=False))
        db.session.commit()
        yield app


TARGET = {
    'title_id': '0100ABCDEFDEF000',
    'app_id': '0100ABCDEFDEF800',
    'app_version': '196608',
    'app_type': 'UPDATE',
    'name': 'Some Game',
    'patch_level': 3,
}

FILTERS = {
    'min_seeders': 3,
    'preferred_ext': ['nsz', 'nsp', 'xcz', 'xci'],
    'max_size_gb': 0,
    'indexers': [],
}


def result(title, seeders=10, size=1024 ** 3, **extra):
    return {'title': title, 'download_url': 'magnet:?xt=urn:btih:' + 'a' * 40,
            'seeders': seeders, 'size': size, **extra}


# --- Ranking ---

def test_rank_requires_known_extension(library):
    best, err = downloader_lib.rank_results([result('Some Game [0100ABCDEFDEF800]')], TARGET, FILTERS)
    assert best is None, "no extension in the title means no Switch file"

    best, err = downloader_lib.rank_results(
        [result('Some Game [0100ABCDEFDEF800].iso')], TARGET, FILTERS)
    assert best is None


def test_rank_requires_the_target_in_the_title(library):
    best, err = downloader_lib.rank_results(
        [result('Unrelated Release [0100FFFFFFFFF800].nsp')], TARGET, FILTERS)
    assert best is None
    assert 'filters' in err


def test_rank_prefers_full_version_match_and_app_id(library):
    with_version = result('Some Game [0100ABCDEFDEF800][v196608].nsp')
    without_version = result('Some Game [0100ABCDEFDEF800].nsp')
    best, _ = downloader_lib.rank_results([without_version, with_version], TARGET, FILTERS)
    assert best is with_version


def test_rank_filters_seeders_and_tiny_files(library):
    few_seeders = result('Some Game [0100ABCDEFDEF800].nsp', seeders=1)
    tiny = result('Some Game [0100ABCDEFDEF800].nsp', size=1024)
    ok = result('Some Game [0100ABCDEFDEF800].nsp')
    best, _ = downloader_lib.rank_results([few_seeders, tiny, ok], TARGET, FILTERS)
    assert best is ok


def test_rank_respects_max_size(library):
    filters = dict(FILTERS, max_size_gb=0.5)
    best, err = downloader_lib.rank_results(
        [result('Some Game [0100ABCDEFDEF800].nsp', size=1024 ** 3)], TARGET, filters)
    assert best is None


def test_rank_skips_already_owned_versions(library):
    """A result advertising a version the library holds is a re-download, however
    well it scores - even the exact-version bonus must not resurrect it."""
    owned = result('Some Game [0100ABCDEFDEF800][v196608].nsz', seeders=50)
    other = result('Some Game [0100ABCDEFDEF800] Bundle.nsp', seeders=5)
    best, _ = downloader_lib.rank_results(
        [owned, other], TARGET, FILTERS, owned_versions=('196608', '65536'))
    assert best is other


# --- Targets ---

def test_get_missing_targets_picks_latest_unowned(library, monkeypatch):
    monkeypatch.setattr(downloader_lib.titles_lib, 'get_game_info',
                        lambda tid: {'name': f'Game {tid}'})
    targets = downloader_lib.get_missing_targets()
    assert len(targets) == 2
    upd = next(t for t in targets if t['app_type'] == 'UPDATE')
    assert upd['app_version'] == '196608', "only the latest update is a target"
    dlc = next(t for t in targets if t['app_type'] == 'DLC')
    assert dlc['app_id'] == '0100ABCDEFDEF100'


def test_owned_apps_are_not_targets(library, monkeypatch):
    monkeypatch.setattr(downloader_lib.titles_lib, 'get_game_info',
                        lambda tid: {'name': f'Game {tid}'})
    Apps.query.filter_by(app_id='0100ABCDEFDEF800', app_version='196608').update({'owned': True})
    db.session.commit()
    targets = downloader_lib.get_missing_targets()
    assert all(t['app_id'] != '0100ABCDEFDEF800' or t['app_version'] != '196608'
               for t in targets)


# --- Jackett / qBittorrent clients ---

def test_jackett_search_parses_results(library, monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {'Results': [
                {'Title': 'Game [0100ABCDEFDEF800].nsp',
                 'MagnetUri': 'magnet:?xt=urn:btih:' + 'b' * 40,
                 'Seeders': 5, 'Peers': 8, 'Size': 1024 ** 3,
                 'Tracker': 'tracker1', 'PublishDate': '2024-01-01',
                 'CategoryDesc': 'Games'},
                {'Title': 'Game.torrent-only', 'MagnetUri': '',
                 'Link': 'http://jackett:9117/dl/abc',
                 'Seeders': 1, 'Peers': 1, 'Size': 10, 'Tracker': 'tracker2'},
            ]}

    def fake_get(url, **kwargs):
        captured['url'] = url
        captured['params'] = kwargs.get('params')
        return FakeResponse()

    monkeypatch.setattr(jackett.requests, 'get', fake_get)
    out = jackett.search({'url': 'http://jackett:9117', 'api_key': 'k'}, 'query')

    assert captured['url'] == 'http://jackett:9117/api/v2.0/indexers/all/results'
    assert captured['params']['apikey'] == 'k'
    assert len(out) == 2
    magnet = out[0]
    assert magnet['download_url'].startswith('magnet:')
    assert magnet['leechers'] == 3
    link = out[1]
    assert 'apikey=k' in link['download_url'], "the .torrent link carries the api key"


def test_jackett_search_swallows_errors(library, monkeypatch):
    def boom(url, **kwargs):
        raise jackett.requests.exceptions.HTTPError('500')

    monkeypatch.setattr(jackett.requests, 'get', boom)
    assert jackett.search({'url': 'http://jackett:9117', 'api_key': 'k'}, 'q') == []


def test_qbittorrent_extract_info_hash():
    magnet = 'magnet:?xt=urn:btih:ABCDEF0123456789abcdef0123456789abcdef01&dn=x'
    assert qbittorrent.extract_info_hash(magnet) == 'abcdef0123456789abcdef0123456789abcdef01'
    assert qbittorrent.extract_info_hash('http://x/y.torrent') is None


# --- Sync ---

def test_sync_marks_completed_when_app_becomes_owned(library):
    d = downloader_lib.add_download(
        title_id='0100ABCDEFDEF000', app_id='0100ABCDEFDEF100',
        app_version='0', app_type='DLC', name='DLC', status='downloading')

    Apps.query.filter_by(app_id='0100ABCDEFDEF100', app_version='0').update({'owned': True})
    db.session.commit()
    downloader_lib.sync_downloads_status({'downloader': {}})

    assert db.session.get(Download, d.id).status == 'completed'


def test_sync_marks_failed_on_error_state(library, monkeypatch):
    d = downloader_lib.add_download(
        title_id='0100ABCDEFDEF000', app_id='0100ABCDEFDEF100',
        app_version='0', app_type='DLC', name='DLC', status='downloading',
        torrent_hash='a' * 40)

    class FakeClient:
        def __init__(self, settings):
            pass

        def login(self):
            return True, ''

        def get_torrents(self, hashes=None, category=None):
            return [{'hash': 'A' * 40, 'name': 'dlc', 'state': 'missingFiles'}]

    monkeypatch.setattr(downloader_lib.qbittorrent, 'QbittorrentClient', FakeClient)
    downloader_lib.sync_downloads_status({'downloader': {'qbittorrent': {}}})

    row = db.session.get(Download, d.id)
    assert row.status == 'failed'
    assert 'missingFiles' in row.error


# --- Job orchestration ---

class FakeQbt:
    def __init__(self, settings):
        pass

    def login(self):
        return True, ''

    def add_torrent(self, url, save_path=None, category=None):
        return True, 'ok', 'c' * 40

    def find_hash_by_name(self, name, category=None):
        return None

    def get_torrents(self, hashes=None, category=None):
        return []


def make_job(library, monkeypatch, search_results):
    """A run_downloader_job with Jackett/qBittorrent faked and the queries captured."""
    import jackett as jackett_mod
    calls = {'queries': []}

    def fake_search(jackett_settings, query, indexers=None):
        calls['queries'].append(query)
        return search_results.get(query, [])

    monkeypatch.setattr(downloader_lib.jackett, 'search', fake_search)
    monkeypatch.setattr(downloader_lib.qbittorrent, 'QbittorrentClient', FakeQbt)
    monkeypatch.setattr(downloader_lib, 'is_configured', lambda s: True)
    settings = {'downloader': {'filters': dict(FILTERS), 'qbittorrent': {}}}
    return settings, calls


def test_job_downloads_via_name_fallback(library, monkeypatch):
    """App-id query starves on name-only trackers; the pass must fall through to
    the game-name query and pick the torrent from there."""
    monkeypatch.setattr(downloader_lib.titles_lib, 'get_game_info',
                        lambda tid: {'name': 'Some Game'})
    magnet = 'magnet:?xt=urn:btih:' + 'c' * 40
    settings, calls = make_job(library, monkeypatch, {
        'Some Game': [{'title': 'Some Game [0100ABCDEFDEF800].nsp',
                       'download_url': magnet, 'seeders': 9, 'size': 1024 ** 3}],
    })

    downloader_lib.run_downloader_job(settings)

    assert '0100ABCDEFDEF800' in calls['queries'], "app id tried first"
    assert 'Some Game' in calls['queries'], "name fallback tried"
    rows = {d.app_id: d for d in Download.query.all()}
    assert rows['0100ABCDEFDEF800'].status == 'downloading'
    assert rows['0100ABCDEFDEF800'].search_query == 'Some Game'


def test_job_retries_failed_rows_and_skips_active(library, monkeypatch):
    """A failed row is re-searched on the next pass; an active one is left alone."""
    monkeypatch.setattr(downloader_lib.titles_lib, 'get_game_info',
                        lambda tid: {'name': 'Some Game'})
    settings, calls = make_job(library, monkeypatch, {
        'Some Game': [{'title': 'Some Game [0100ABCDEFDEF800].nsp',
                       'download_url': 'magnet:?xt=urn:btih:' + 'c' * 40,
                       'seeders': 9, 'size': 1024 ** 3}],
    })
    magnet = 'magnet:?xt=urn:btih:' + 'd' * 40
    dl = downloader_lib.add_download(
        title_id='0100ABCDEFDEF000', app_id='0100ABCDEFDEF800',
        app_version='196608', app_type='UPDATE', name='Some Game',
        status='failed', error='No results from Jackett.')
    active = downloader_lib.add_download(
        title_id='0100ABCDEFDEF000', app_id='0100ABCDEFDEF100',
        app_version='0', app_type='DLC', name='DLC', status='downloading')

    downloader_lib.run_downloader_job(settings)

    row = Download.query.filter_by(app_id='0100ABCDEFDEF800').one()
    assert row.status == 'downloading', "the failed row was retried into a torrent"
    assert row.id != dl.id, "retry replaced the row rather than reusing it"
    assert db.session.get(Download, active.id).status == 'downloading'
    assert '0100ABCDEFDEF100' not in [d.search_query for d in Download.query.all()], \
        "an in-progress row is not re-searched"
