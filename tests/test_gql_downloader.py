"""Downloader additions to the GraphQL surface: query shape and mutation guards."""
import types

import pytest

import db as db_mod
import downloader as downloader_lib
import tasks as tasks_mod
from app import create_app
from db import Download, Task, db, init_db
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
        yield app


def run(library, document):
    resp = library.test_client().post("/api/graphql", json={"query": document})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def test_run_downloader_enqueues_the_task(library, monkeypatch):
    monkeypatch.setattr(tasks_mod.downloader_lib, 'run_downloader_job', lambda *a, **kw: None)
    body = run(library, 'mutation { runDownloader(source: TORRENTS) { taskName status } }')
    assert body['data']['runDownloader'] == {'taskName': 'downloader_torrents_run', 'status': 'PENDING'}
    body = run(library, 'mutation { runDownloader(source: GHOSTESHOP) { taskName status } }')
    assert body['data']['runDownloader'] == {'taskName': 'downloader_ghosteshop_run', 'status': 'PENDING'}


def test_run_downloader_does_not_wait_for_the_scheduled_row(library, monkeypatch):
    """A scheduled pass is pending with run_after 2h out; 'Run now' must not dedup
    onto that deferred row, or the button quietly does nothing for two hours."""
    import datetime
    from tasks import update_scheduled_task
    update_scheduled_task('downloader_torrents_run',
                          datetime.datetime.utcnow() + datetime.timedelta(hours=2))

    body = run(library, 'mutation { runDownloader(source: TORRENTS) { taskName status } }')
    task = body['data']['runDownloader']
    assert task['taskName'] == 'downloader_torrents_run'

    from db import Task
    manual = Task.query.filter_by(task_name='downloader_torrents_run', run_after=None).all()
    assert len(manual) == 1, "the manual run is its own immediate row"


def test_run_downloader_requires_a_known_source(library):
    body = run(library, 'mutation { runDownloader(source: CARRIER_PIDGEON) { id } }')
    assert body.get('errors'), "an unknown source must be a schema error"


def test_downloads_query_lists_rows_newest_first(library):
    db.session.add(Download(title_id='0100000000010000', app_id='0100000000010800',
                            app_version='65536', app_type='UPDATE', name='A',
                            status='downloading'))
    db.session.add(Download(title_id='0100000000020000', app_id='0100000000020100',
                            app_version='0', app_type='DLC', name='B',
                            status='failed', error='no results'))
    db.session.commit()

    body = run(library, """
        query { downloads { name status appVersion appType error } }
    """)
    items = body['data']['downloads']
    assert [i['name'] for i in items] == ['B', 'A']
    assert items[0]['appType'] == 'DLC'

    body = run(library, 'query { downloads(status: DOWNLOADING) { name } }')
    assert [i['name'] for i in body['data']['downloads']] == ['A']


def test_delete_download_removes_the_row(library):
    d = downloader_lib.add_download(title_id='0100000000010000',
                                    app_id='0100000000010800',
                                    app_version='65536', app_type='UPDATE',
                                    name='A', status='failed')
    body = run(library, f'mutation {{ deleteDownload(id: "{d.id}") }}')
    assert body['data']['deleteDownload'] is True
    assert db.session.get(Download, d.id) is None

    body = run(library, f'mutation {{ deleteDownload(id: "{d.id}") }}')
    assert body['data']['deleteDownload'] is False


def test_retry_download_unknown_id_errors(library):
    body = run(library, 'mutation { retryDownload(id: 999) { id } }')
    assert body.get('errors'), "an unknown row must raise, not return null"


def test_every_schema_element_is_documented_incl_downloads():
    from gql.schema import schema
    down = schema._schema.type_map['Download']
    assert down.description
    for field in down.fields.values():
        assert field.description
    status = schema._schema.type_map['DownloadStatus']
    assert status.description
    for value in status.values.values():
        assert value.description
