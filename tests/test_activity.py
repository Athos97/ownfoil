"""User activity capture: retention, login/download instrumentation, GraphQL read."""
import pytest

import activity as activity_mod
import db as db_mod
from app import create_app
from db import (db, init_db, ActivityEvent, record_activity, User,
                ACTIVITY_MAX_EVENTS)
from auth import auth_blueprint  # noqa: F401 -- registered by create_app
from gql import graphql_dispatch


@pytest.fixture
def web(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(db_mod, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(db_mod, "TITLES_DB_FILE", str(config / "titles.db"))

    app = create_app(f"sqlite:///{config / 'ownfoil.db'}")
    app.add_url_rule("/api/graphql", view_func=graphql_dispatch, methods=["GET", "POST"])
    init_db(app)
    # No admin account -> auth disabled -> the admin gate opens for the test client.
    activity_mod._connect_registry.clear()
    with app.app_context():
        yield app


def run(web, document, variables=None):
    resp = web.test_client().post("/api/graphql", json={
        "query": document, "variables": variables or {}})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


# --- retention ---

def test_activity_prunes_to_the_retention_cap(web):
    for _ in range(ACTIVITY_MAX_EVENTS + 150):
        record_activity(kind='login', username='u', client='web')
    assert ActivityEvent.query.count() == ACTIVITY_MAX_EVENTS
    # And the survivors are the newest ones.
    oldest = ActivityEvent.query.order_by(ActivityEvent.id).first()
    assert oldest.id == 151


def test_record_activity_never_raises(web, monkeypatch):
    """The audit trail must not break the action being audited: a DB failure inside
    record_* is swallowed by the activity layer."""
    def boom():
        raise RuntimeError('db on fire')
    monkeypatch.setattr(db.session, 'commit', boom)
    activity_mod.record_login('u', success=True)  # must not raise


# --- login instrumentation ---

def test_web_login_records_success_and_failure(web):
    from werkzeug.security import generate_password_hash
    db.session.add(User(user='alice', password=generate_password_hash('right'),
                        admin_access=True, shop_access=True, backup_access=True))
    db.session.commit()

    client = web.test_client()
    client.post('/login', data={'user': 'alice', 'password': 'wrong'})
    client.post('/login', data={'user': 'alice', 'password': 'right'})

    events = [(e.kind, e.username) for e in
              ActivityEvent.query.order_by(ActivityEvent.id).all()]
    assert events == [('login_failed', 'alice'), ('login', 'alice')]


def test_web_login_rate_limits_repeated_failures_by_ip(web, monkeypatch):
    """After LOGIN_MAX_ATTEMPTS failures from the same IP, even the right
    password is refused until the window passes - and a different IP is
    unaffected, since the limiter is keyed by IP, not username."""
    from werkzeug.security import generate_password_hash
    import auth as auth_mod
    monkeypatch.setattr(auth_mod, '_login_attempts', {})
    db.session.add(User(user='alice', password=generate_password_hash('right'),
                        admin_access=True, shop_access=True, backup_access=True))
    db.session.commit()

    client = web.test_client()
    for _ in range(auth_mod.LOGIN_MAX_ATTEMPTS):
        client.post('/login', data={'user': 'alice', 'password': 'wrong'})
    client.post('/login', data={'user': 'alice', 'password': 'right'})

    events = ActivityEvent.query.order_by(ActivityEvent.id).all()
    assert len(events) == auth_mod.LOGIN_MAX_ATTEMPTS + 1
    assert all(e.kind == 'login_failed' for e in events)
    assert events[-1].detail == 'rate limited'

    # A different client (distinct X-Forwarded-For) is a different rate-limit
    # key and logs in normally.
    resp = client.post('/login', data={'user': 'alice', 'password': 'right'},
                       headers={'X-Forwarded-For': '203.0.113.5'})
    assert ActivityEvent.query.order_by(ActivityEvent.id.desc()).first().kind == 'login'


# --- GraphQL read ---

def test_activity_query_lists_newest_first(web):
    record_activity(kind='shop_connect', username='bob', client='tinfoil',
                    device_uid='UID-1', ip='10.0.0.9')
    record_activity(kind='download', username='bob', client='sphaira',
                    filename='game.nsp', size=1234)

    body = run(web, """
        query { activity {
            kind username client deviceUid ip filename size
        } }
    """)
    items = body['data']['activity']
    assert [i['kind'] for i in items] == ['DOWNLOAD', 'SHOP_CONNECT']
    assert items[0]['filename'] == 'game.nsp'
    assert items[0]['size'] == 1234
    assert items[1]['client'] == 'tinfoil'
    assert items[1]['deviceUid'] == 'UID-1'

    body = run(web, 'query { activity(kind: SHOP_CONNECT) { kind } }')
    assert [i['kind'] for i in body['data']['activity']] == ['SHOP_CONNECT']


# --- connect throttle ---

class FakeRequest:
    def __init__(self, user=None, uid=None):
        self.headers = {'Uid': uid} if uid else {}
        self.user = user
        self.remote_addr = '127.0.0.1'


def test_connect_throttle_one_event_per_window(web):
    req = FakeRequest(user=type('U', (), {'user': 'bob'})(), uid='UID-1')
    activity_mod.record_shop_connect(req, 'Tinfoil')
    activity_mod.record_shop_connect(req, 'Tinfoil')
    activity_mod.record_shop_connect(req, 'Tinfoil')
    req2 = FakeRequest(user=type('U', (), {'user': 'bob'})(), uid='UID-2')
    activity_mod.record_shop_connect(req2, 'Tinfoil')

    events = ActivityEvent.query.all()
    assert len(events) == 2, "one event per (user, device) per window"
    assert {e.device_uid for e in events} == {'UID-1', 'UID-2'}


def test_download_not_counted_is_not_recorded(web):
    activity_mod.record_download(FakeRequest(), '/games/x.nsp', size=1, counted=False)
    assert ActivityEvent.query.count() == 0
