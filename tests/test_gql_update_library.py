"""GraphQL surface for the Update Library / Add Content pages and the stats
unidentified-files detail: query shape, admin gating, queue semantics."""
import json

import pytest

import db as db_mod
import downloader as downloader_lib
from app import create_app
from db import db, init_db, Download, Files, Titles, Apps
from gql import graphql_dispatch


@pytest.fixture
def library(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(db_mod, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(db_mod, "TITLES_DB_FILE", str(config / "titles.db"))

    app = create_app(f"sqlite:///{config / 'ownfoil.db'}")
    app.add_url_rule("/api/graphql", view_func=graphql_dispatch, methods=["GET", "POST"])
    init_db(app)
    with app.app_context():
        title = Titles(title_id="0100ABCDEFDEF000", have_base=True, up_to_date=False)
        db.session.add(title)
        db.session.flush()
        db.session.add(Apps(title_id=title.id, app_id="0100ABCDEFDEF000",
                            app_version="0", app_type="BASE", owned=True))
        db.session.add(Apps(title_id=title.id, app_id="0100ABCDEFDEF800",
                            app_version="196608", app_type="UPDATE", owned=False))
        db.session.commit()
        yield app


def run(library, document):
    resp = library.test_client().post("/api/graphql", json={"query": document})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


# --- missing targets & source status ---

def test_missing_targets_lists_latest_unowned(library, monkeypatch):
    monkeypatch.setattr(downloader_lib, 'get_missing_targets', lambda: [{
        'title_id': '0100ABCDEFDEF000', 'app_id': '0100ABCDEFDEF800',
        'app_version': '196608', 'app_type': 'UPDATE',
        'name': 'Some Game', 'patch_level': 3,
    }])
    body = run(library, 'query { missingTargets { appId appVersion appType name patchLevel } }')
    items = body['data']['missingTargets']
    assert len(items) == 1
    assert items[0]['appId'] == '0100ABCDEFDEF800'
    assert items[0]['appType'] == 'UPDATE'
    assert items[0]['patchLevel'] == 3


def test_downloader_status_reports_both_sources(library, monkeypatch):
    import datetime
    from tasks import update_scheduled_task
    monkeypatch.setattr(downloader_lib, 'torrents_configured', lambda s: True)
    monkeypatch.setattr(downloader_lib, 'ghosteshop_configured', lambda s: False)
    update_scheduled_task('downloader_torrents_run',
                          datetime.datetime.utcnow() + datetime.timedelta(hours=2))

    body = run(library, 'query { downloaderStatus { source configured enabled nextRun } }')
    by_source = {s['source']: s for s in body['data']['downloaderStatus']}
    assert set(by_source) == {'TORRENTS', 'GHOSTESHOP'}
    assert by_source['TORRENTS']['configured'] is True
    assert by_source['TORRENTS']['nextRun'] is not None
    assert by_source['GHOSTESHOP']['configured'] is False
    assert by_source['GHOSTESHOP']['nextRun'] is None


# --- add content queueing ---

QUEUE = """
    mutation Queue($entries: [QueuedDownloadInput!]!) {
        queueGhosteshopDownloads(entries: $entries)
    }
"""

ENTRY = {
    'titleId': '01007EF00011E000',
    'appId': '01007EF00011E800',
    'appVersion': 1114112,
    'appType': 'UPDATE',
    'name': 'Zelda update',
    'fileName': 'Zelda [01007EF00011E800][v1114112]',
}


def test_queue_downloads_creates_one_row_per_target(library):
    body = run(library, QUEUE)
    assert body.get('errors'), "entries is required"

    # GraphQL layer: pass through variables to exercise the input coercion path.
    resp = library.test_client().post("/api/graphql", json={
        'query': QUEUE, 'variables': {'entries': [ENTRY]}})
    assert resp.status_code == 200
    assert resp.get_json()['data']['queueGhosteshopDownloads'] == 1

    row = Download.query.filter_by(app_id='01007EF00011E800').one()
    assert row.status == 'queued'
    assert row.source == 'ghosteshop'
    assert row.app_type == 'UPDATE'

    # Queueing means downloading: a manual Ghost eShop pass starts right away,
    # not at the next scheduled run (which can be a day out).
    from db import Task
    passes = Task.query.filter_by(task_name='downloader_ghosteshop_run').all()
    assert len(passes) == 1
    assert json.loads(passes[0].input_json) == {'manual': True}

    # Re-queueing the same target is a no-op, not a second row or a second pass.
    resp = library.test_client().post("/api/graphql", json={
        'query': QUEUE, 'variables': {'entries': [ENTRY]}})
    assert resp.get_json()['data']['queueGhosteshopDownloads'] == 0
    assert Download.query.filter_by(app_id='01007EF00011E800').count() == 1
    assert Task.query.filter_by(task_name='downloader_ghosteshop_run').count() == 1


def test_queue_skips_owned_targets(library):
    # The fixture already owns the base app (0100ABCDEFDEF000 v0).
    owned_entry = dict(ENTRY, appId='0100ABCDEFDEF000', appVersion=0,
                       appType='BASE', name='Some Game base')
    resp = library.test_client().post("/api/graphql", json={
        'query': QUEUE, 'variables': {'entries': [owned_entry]}})
    assert resp.get_json()['data']['queueGhosteshopDownloads'] == 0
    assert Download.query.filter_by(app_id='0100ABCDEFDEF000').count() == 0


def test_downloads_query_carries_source_and_progress(library):
    db.session.add(Download(title_id='0100000000010000', app_id='0100000000010800',
                            app_version='65536', app_type='UPDATE', name='A',
                            source='ghosteshop', progress=42,
                            status='downloading'))
    db.session.commit()

    body = run(library, 'query { downloads { source progress } }')
    item = body['data']['downloads'][0]
    assert item['source'] == 'GHOSTESHOP'
    assert item['progress'] == 42


# --- stats: unidentified files detail ---

def test_stats_lists_unidentified_files_with_reasons(library):
    from db import Libraries
    lib = Libraries(path='/games')
    db.session.add(lib)
    db.session.flush()
    db.session.add(Files(filepath='/games/broken.nsz', filename='broken.nsz',
                         library_id=lib.id, extension='nsz', size=123,
                         identified=False, identification_type='cnmt',
                         identification_error='No Pfs0 sections found.',
                         identification_attempts=1))
    db.session.commit()

    body = run(library, """
        query { stats { unidentifiedFilesDetail { filename error attempts size } } }
    """)
    items = body['data']['stats']['unidentifiedFilesDetail']
    assert len(items) == 1
    assert items[0]['filename'] == 'broken.nsz'
    assert 'Pfs0' in items[0]['error']
    assert items[0]['attempts'] == 1
    assert items[0]['size'] == 123
