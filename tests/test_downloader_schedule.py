"""Tests for how the downloader task keeps itself scheduled.

Same contract as the titledb chain: periodicity is a self-re-enqueuing chain, so
the thing to pin down is that it survives a failure and stops cleanly when the
feature is turned off - nothing else re-enqueues a dead chain.
"""
import datetime

import pytest

import db as db_mod
import tasks as tasks_mod
from app import create_app
from db import Task, db, init_db


@pytest.fixture
def queue(tmp_path, monkeypatch):
    """An app with an empty tasks table and a downloader job that does nothing."""
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(db_mod, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(db_mod, "TITLES_DB_FILE", str(config / "titles.db"))

    app = create_app(f"sqlite:///{config / 'ownfoil.db'}")
    init_db(app)

    settings = {
        "downloader": {
            "enabled": True,
            "interval": "2h",
            "jackett": {"url": "http://jackett:9117", "api_key": "k"},
            "qbittorrent": {"url": "http://qbt:8080"},
        },
    }
    monkeypatch.setattr(tasks_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks_mod.downloader_lib, "is_configured", lambda s: True)
    monkeypatch.setattr(tasks_mod.downloader_lib, "run_downloader_job", lambda *a, **kw: None)
    with app.app_context():
        yield app


def scheduled():
    """The pending downloader_run rows, and how far out each one is."""
    rows = Task.query.filter_by(task_name="downloader_run", status="pending").all()
    now = datetime.datetime.utcnow()
    return [(row.run_after - now).total_seconds() / 3600 for row in rows]


def test_success_schedules_the_next_run_at_the_configured_interval(queue):
    tasks_mod.downloader_run_task()

    assert len(scheduled()) == 1
    assert scheduled()[0] == pytest.approx(2, abs=0.1)


def test_failure_reschedules_instead_of_dropping_the_chain(queue, monkeypatch):
    def boom(*a, **kw):
        raise ConnectionError("jackett unreachable")

    monkeypatch.setattr(tasks_mod.downloader_lib, "run_downloader_job", boom)

    with pytest.raises(ConnectionError):
        tasks_mod.downloader_run_task()

    assert len(scheduled()) == 1, "a failed run must leave a follow-up queued"
    assert scheduled()[0] == pytest.approx(1, abs=0.1)


def test_repeated_runs_do_not_pile_up_rows(queue):
    """update_scheduled_task must move the pending run_after, not add a second row."""
    tasks_mod.downloader_run_task()
    db.session.query(Task).filter_by(task_name="downloader_run").update(
        {"run_after": datetime.datetime.utcnow() + datetime.timedelta(hours=99)})
    db.session.commit()
    tasks_mod.downloader_run_task()

    assert len(scheduled()) == 1
    assert scheduled()[0] == pytest.approx(2, abs=0.1)


def test_disabled_downloader_removes_the_scheduled_row(queue, monkeypatch):
    """Unconfigured: the run is a no-op and the scheduled row is deleted, not left behind."""
    tasks_mod.downloader_run_task()
    assert len(scheduled()) == 1

    monkeypatch.setattr(tasks_mod.downloader_lib, "is_configured", lambda s: False)
    tasks_mod.arm_downloader_schedule()

    assert scheduled() == []


def test_interval_zero_disables_the_chain(queue, monkeypatch):
    tasks_mod.downloader_run_task()
    assert len(scheduled()) == 1

    settings = tasks_mod.get_settings()
    settings["downloader"]["interval"] = "0"
    monkeypatch.setattr(tasks_mod, "get_settings", lambda: settings)
    tasks_mod.arm_downloader_schedule()

    assert scheduled() == []
