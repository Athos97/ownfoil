"""Blacklist behavior: flags, missing targets, GraphQL mutations, JSON import."""
import io
import json

import pytest

import db as db_mod
import titledb
import downloader as downloader_lib
import library as library_lib
from app import create_app, import_blacklist_api
from db import (db, init_db, Titles, Apps, BlacklistedApp,
                upsert_blacklisted_app,
                get_blacklisted_app_ids, normalize_app_id)
from gql import graphql_dispatch

TITLE_ID = "0100ABCDEFDEF000"
UPD_ID = "0100ABCDEFDEF800"
DLC_1 = "0100ABCDEFDEE001"
DLC_2 = "0100ABCDEFDEE002"

TITLEDB_JSON = {
    TITLE_ID: {"id": TITLE_ID, "name": "Some Game"},
    DLC_1: {"id": DLC_1, "name": "Some Game French Voices"},
    DLC_2: {"id": DLC_2, "name": "Some Game Bonus Pack"},
}


@pytest.fixture
def library(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    titledb_dir = tmp_path / "titledb"
    titledb_dir.mkdir()
    monkeypatch.setattr(db_mod, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(db_mod, "TITLES_DB_FILE", str(config / "titles.db"))
    monkeypatch.setattr(titledb.store, "TITLES_DB_FILE", str(config / "titles.db"))
    monkeypatch.setattr(titledb.store, "DB_FILE", str(config / "ownfoil.db"))

    app = create_app(f"sqlite:///{config / 'ownfoil.db'}")
    app.add_url_rule("/api/graphql", view_func=graphql_dispatch, methods=["GET", "POST"])
    app.add_url_rule("/api/blacklist/import", view_func=import_blacklist_api,
                     methods=["POST"])
    init_db(app)

    region_file = titledb_dir / "titles.US.en.json"
    region_file.write_text(json.dumps(TITLEDB_JSON))
    (titledb_dir / "cnmts.json").write_text("{}")
    (titledb_dir / "versions.json").write_text("{}")

    with app.app_context():
        titledb.store.import_from_json(str(region_file), "US.en")
        title = Titles(title_id=TITLE_ID, have_base=True, up_to_date=False,
                       complete=False)
        db.session.add(title)
        db.session.flush()
        # base owned; update v3 missing; two DLCs missing
        db.session.add(Apps(title_id=title.id, app_id=TITLE_ID,
                            app_version="0", app_type="BASE", owned=True))
        db.session.add(Apps(title_id=title.id, app_id=UPD_ID,
                            app_version="65536", app_type="UPDATE", owned=True))
        db.session.add(Apps(title_id=title.id, app_id=UPD_ID,
                            app_version="196608", app_type="UPDATE", owned=False))
        db.session.add(Apps(title_id=title.id, app_id=DLC_1,
                            app_version="0", app_type="DLC", owned=False))
        db.session.add(Apps(title_id=title.id, app_id=DLC_2,
                            app_version="0", app_type="DLC", owned=False))
        db.session.commit()
        yield app


def run(library, document, variables=None):
    resp = library.test_client().post("/api/graphql", json={
        "query": document, "variables": variables or {}})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


# --- flags ---

def test_blacklisted_dlc_does_not_block_complete(library):
    library_lib.update_title_flags(TITLE_ID)
    title = Titles.query.filter_by(title_id=TITLE_ID).one()
    assert title.complete is False, "both DLCs missing at first"

    upsert_blacklisted_app(DLC_1, "language pack")
    upsert_blacklisted_app(DLC_2)
    library_lib.update_title_flags(TITLE_ID)

    title = Titles.query.filter_by(title_id=TITLE_ID).one()
    assert title.complete is True, "every non-blacklisted DLC is owned-or-none"


def test_blacklisted_update_does_not_block_up_to_date(library):
    upsert_blacklisted_app(UPD_ID)  # the update app id as a whole
    library_lib.update_title_flags(TITLE_ID)
    title = Titles.query.filter_by(title_id=TITLE_ID).one()
    assert title.up_to_date is True


# --- missing targets ---

def test_missing_targets_skip_blacklisted(library, monkeypatch):
    monkeypatch.setattr(downloader_lib.titles_lib, 'get_game_info',
                        lambda tid: {'name': 'Some Game'})
    before = {(t['app_id'], t['app_type']) for t in downloader_lib.get_missing_targets()}
    assert (UPD_ID, 'UPDATE') in before
    assert (DLC_1, 'DLC') in before

    upsert_blacklisted_app(DLC_1)
    after = {(t['app_id'], t['app_type']) for t in downloader_lib.get_missing_targets()}
    assert (DLC_1, 'DLC') not in after
    assert (DLC_2, 'DLC') in after


# --- expansion ---

def test_add_missing_apps_skips_blacklisted(library, monkeypatch):
    """Expansion materializes known-but-unowned apps - except blacklisted ones, whose
    rows would only be noise (the flags/target queries filter by table anyway)."""
    NEW_DLC = '0100ABCDEFDEE004'
    KNOWN_DLC = '0100ABCDEFDEE003'   # neither row exists yet
    monkeypatch.setattr(library_lib.titles_lib, 'get_all_dlc_versions',
                        lambda tid: [(KNOWN_DLC, 0, None), (NEW_DLC, 0, None)])
    monkeypatch.setattr(library_lib.titles_lib, 'get_all_existing_versions',
                        lambda tid: [{'version': 0, 'release_date': None}])
    upsert_blacklisted_app(NEW_DLC)

    library_lib.add_missing_apps_for_title(TITLE_ID)

    ids = {a.app_id for a in Apps.query.all()}
    assert NEW_DLC not in ids, "blacklisted DLC must not be materialized"
    assert KNOWN_DLC in ids


# --- GraphQL surface ---

def test_blacklist_mutations_recompute_and_query(library):
    body = run(library, """
        mutation { blacklistApp(appId: "0100abcdefdee001", note: "JP voices") }
    """)
    assert body['data']['blacklistApp'] is True
    assert normalize_app_id("0100abcdefdee001") in get_blacklisted_app_ids()

    body = run(library, 'query { blacklistedApps { appId note } }')
    assert body['data']['blacklistedApps'] == [
        {'appId': DLC_1, 'note': 'JP voices'}]

    # The title's complete flag recomputed via the enqueued task path.
    title = Titles.query.filter_by(title_id=TITLE_ID).one()
    assert title.complete is False  # DEE002 still missing

    body = run(library, """
        mutation { blacklistApp(appId: "0100ABCDEFDEE002") }
    """)
    assert body['data']['blacklistApp'] is True

    body = run(library, """
        mutation { unblacklistApp(appId: "0100ABCDEFDEE001") }
    """)
    assert body['data']['unblacklistApp'] is True
    assert DLC_1 not in get_blacklisted_app_ids()

    # Unknown id shape refused, not silently normalized into garbage.
    body = run(library, 'mutation { blacklistApp(appId: "zzz") }')
    assert body.get('errors')


def test_app_query_carries_blacklisted_flag(library):
    upsert_blacklisted_app(DLC_1)
    body = run(library, """
        query { title(titleId: "0100ABCDEFDEF000") {
            apps { appId appType owned blacklisted }
        } }
    """)
    apps = {a['appId']: a for a in body['data']['title']['apps']}
    assert apps[DLC_1]['blacklisted'] is True
    assert apps[DLC_2]['blacklisted'] is False


# --- JSON import (switch-library-updater format) ---

def import_json(library, payload):
    return library.test_client().post(
        "/api/blacklist/import",
        data={"file": (__import__("io").BytesIO(json.dumps(payload).encode()), "blacklist.json")},
        content_type="multipart/form-data")


def test_import_updater_format_with_notes(library):
    resp = import_json(library, [
        {"id": DLC_1, "note": "Some Game — French voices"},
        {"id": "0x0100abcdefdee002"},   # updater tolerates 0x/case; so do we
        UPD_ID,
        "not-an-id",                     # reported invalid, import continues
    ])
    body = resp.get_json()
    assert resp.status_code == 200
    assert body['success'] is True
    assert body['imported'] == 3
    assert body['invalid'] == ["not-an-id"]

    rows = {b.app_id: b.note for b in BlacklistedApp.query.all()}
    assert rows[DLC_1] == "Some Game — French voices"
    assert rows[DLC_2] is None

    # Re-import with a new note updates, keeps the rest.
    resp = import_json(library, [{"id": DLC_2, "note": "Updated"}])
    assert resp.get_json()['imported'] == 1
    assert BlacklistedApp.query.get(DLC_2).note == "Updated"


def test_import_rejects_garbage(library):
    resp = library.test_client().post(
        "/api/blacklist/import",
        data={"file": (__import__("io").BytesIO(b"{not json"), "blacklist.json")},
        content_type="multipart/form-data")
    assert resp.get_json()['success'] is False
