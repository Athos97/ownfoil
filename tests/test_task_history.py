"""Task history capture: every terminal transition records an outcome row."""
import datetime
import json

import pytest

import db as db_mod
import tasks as tasks_mod
import worker as worker_mod
from app import create_app
from db import (db, init_db, Task, TaskHistory, record_task_history,
                TASK_HISTORY_MAX)


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(db_mod, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(db_mod, "TITLES_DB_FILE", str(config / "titles.db"))
    app = create_app(f"sqlite:///{config / 'ownfoil.db'}")
    init_db(app)
    with app.app_context():
        yield app


def _history():
    db.session.expire_all()
    return TaskHistory.query.order_by(TaskHistory.id).all()


def test_record_and_prune(env):
    for i in range(TASK_HISTORY_MAX + 25):
        record_task_history(i, 'verify_file', f'Verify game {i}', 'completed')
    rows = _history()
    assert len(rows) == TASK_HISTORY_MAX
    # The survivors are the newest ones.
    assert rows[0].task_id == 25
    assert rows[-1].task_id == TASK_HISTORY_MAX + 24
    assert rows[-1].duration_ms is None  # no start time given


def test_duration_computed(env):
    started = datetime.datetime.utcnow() - datetime.timedelta(seconds=90)
    record_task_history(1, 'compress_file', 'Compress X', 'completed',
                        started_at=started)
    row = _history()[-1]
    assert row.duration_ms is not None
    assert 85000 <= row.duration_ms <= 100000


def test_cancel_records_history(env):
    t = Task(task_name='verify_file', status='running', worker_id=1,
             input_hash='h', input_json='{"file_id": 1}',
             started_at=datetime.datetime.utcnow())
    db.session.add(t)
    db.session.commit()
    task_id = t.id

    assert tasks_mod.cancel_task(task_id)

    rows = _history()
    assert len(rows) == 1
    assert rows[0].status == 'cancelled'
    assert rows[0].task_id == task_id
    assert rows[0].display_name  # resolved via TASK_DISPLAY


def test_reap_records_history(env):
    t = Task(task_name='verify_file', status='running', worker_id=3,
             input_hash='h', input_json='{}',
             started_at=datetime.datetime.utcnow())
    db.session.add(t)
    db.session.commit()

    tasks_mod.reap_worker_task(3)

    rows = _history()
    assert len(rows) == 1
    assert rows[0].status == 'failed'
    assert 'worker stop' in rows[0].error


def test_worker_success_and_failure_record_history(env, monkeypatch):
    """The worker's terminal paths write history alongside the accounting."""
    ok = Task(task_name='add_missing_apps', status='pending', input_hash='h1',
              input_json='{}')
    db.session.add(ok)
    db.session.commit()

    tw = worker_mod.TaskWorker(env, poll_interval=0.01, worker_id=1)
    monkeypatch.setitem(tasks_mod.TASK_REGISTRY, 'add_missing_apps',
                        lambda **kw: None)
    tw.execute_task(ok.id)

    rows = _history()
    assert [r.status for r in rows] == ['completed']
    assert rows[0].display_name == 'Add missing content'

    boom = Task(task_name='add_missing_apps', status='pending', input_hash='h2',
                input_json='{}')
    db.session.add(boom)
    db.session.commit()

    def kaboom(**kw):
        raise RuntimeError('disk exploded')
    monkeypatch.setitem(tasks_mod.TASK_REGISTRY, 'add_missing_apps', kaboom)
    tw.execute_task(boom.id)

    rows = _history()
    assert [r.status for r in rows] == ['completed', 'failed']
    assert 'disk exploded' in rows[1].error


def test_startup_cleanup_records_stale_as_failed(env):
    t = Task(task_name='verify_file', status='running', worker_id=1,
             input_hash='h', input_json='{}')
    db.session.add(t)
    db.session.commit()

    tasks_mod.cleanup_tasks()

    rows = _history()
    assert len(rows) == 1
    assert rows[0].status == 'failed'
    assert 'restart' in rows[0].error


def test_graphql_task_history_query(env):
    from gql import graphql_dispatch
    app = env
    app.add_url_rule('/api/graphql', view_func=graphql_dispatch,
                     methods=['GET', 'POST'])
    record_task_history(1, 'verify_file', 'Verify A', 'completed')
    record_task_history(2, 'compress_file', 'Compress B', 'failed',
                        error='no space')

    resp = app.test_client().get('/api/graphql', query_string={
        'query': 'query { taskHistory { taskName displayName status error } }'})
    items = resp.get_json()['data']['taskHistory']
    assert [i['taskName'] for i in items] == ['compress_file', 'verify_file']
    assert items[0]['error'] == 'no space'
    assert items[1]['status'] == 'completed'


def test_old_admin_routes_redirect(env):
    from app import manage_library_page, update_library_page, add_content_page
    env.add_url_rule('/admin/manage', view_func=manage_library_page)
    env.add_url_rule('/admin/update', view_func=update_library_page)
    env.add_url_rule('/admin/add-content', view_func=add_content_page)

    resp = env.test_client().get('/admin/update')
    assert resp.status_code == 302 and resp.location.endswith('/admin/manage')
    resp = env.test_client().get('/admin/add-content')
    assert resp.status_code == 302 and resp.location.endswith('/admin/manage')


def test_parent_completion_history_parses_raw_cursor_dates(env):
    """_try_complete_parent reads started_at through a raw cursor, which hands
    back a string; the history row must survive the round trip (the TypeError
    dropped every parent entry - 'Startup', 'Process library files'...)."""
    import datetime as _dt
    parent = Task(task_name='scan_libraries', status='waiting_for_children',
                  input_hash='h', input_json='{}',
                  started_at=_dt.datetime(2026, 8, 22, 12, 0, 0))
    db.session.add(parent)
    db.session.flush()
    child = Task(task_name='scan_library', status='completed', input_hash='hc',
                 input_json='{"library_path": "/games"}', parent_id=parent.id)
    db.session.add(child)
    db.session.commit()
    parent_id = parent.id

    tasks_mod._try_complete_parent(parent_id)

    rows = _history()
    assert any(r.task_id == parent_id and r.status == 'completed'
               and r.duration_ms is not None for r in rows), \
        "the parent's history row carries its parsed start time"


def test_parent_completion_with_a_failed_child_is_recorded_distinctly(env):
    """A batch where every child reaches a terminal state, but not all of them
    successfully, used to record the parent as plain 'completed' - a clean tick
    in the history tab for a pass that partially failed. The live child rows
    are deleted the moment the parent completes, so this history row is the
    only lasting trace of the failure."""
    parent = Task(task_name='downloader_ghosteshop_run', status='waiting_for_children',
                  input_hash='h', input_json='{}')
    db.session.add(parent)
    db.session.flush()
    db.session.add(Task(task_name='ghosteshop_download', status='completed',
                        input_hash='hc1', input_json='{}', parent_id=parent.id))
    db.session.add(Task(task_name='ghosteshop_download', status='failed',
                        input_hash='hc2', input_json='{}', parent_id=parent.id))
    db.session.commit()
    parent_id = parent.id

    tasks_mod._try_complete_parent(parent_id)

    rows = _history()
    row = next(r for r in rows if r.task_id == parent_id)
    assert row.status == 'completed_with_errors'
    assert row.error == '1 of 2 sub-task(s) failed'
