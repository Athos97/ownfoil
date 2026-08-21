"""Settings-side tests: downloader layout migration and the keys-reload retry."""

import datetime

import pytest

import settings as settings_mod
import tasks as tasks_mod
from constants import DEFAULT_SETTINGS


# --- downloader settings migration ---

def test_migrate_moves_flat_layout_under_torrents():
    settings = {
        'downloader': {
            'enabled': True,
            'interval': '3h',
            'jackett': {'url': 'http://jackett:9117', 'api_key': 'k'},
            'qbittorrent': {'url': 'http://qbt:8080', 'password': 'p'},
            'filters': {'min_seeders': 5},
        },
    }
    assert settings_mod.migrate_downloader_settings(settings) is True
    torrents = settings['downloader']['torrents']
    assert torrents['enabled'] is True
    assert torrents['interval'] == '3h'
    assert torrents['jackett']['api_key'] == 'k'
    assert torrents['qbittorrent']['password'] == 'p'
    assert torrents['filters'] == {'min_seeders': 5}
    assert set(settings['downloader']) == {'torrents'}


def test_migrate_is_idempotent_and_skips_new_layout():
    # Already migrated: no-op.
    settings = {'downloader': {'torrents': {'enabled': False}, 'ghosteshop': {}}}
    assert settings_mod.migrate_downloader_settings(settings) is False
    # Empty downloader block: nothing to move.
    assert settings_mod.migrate_downloader_settings({'downloader': {}}) is False


def test_defaults_already_carry_both_sources():
    downloader = DEFAULT_SETTINGS['downloader']
    assert {'torrents', 'ghosteshop'} <= set(downloader)
    assert 'jackett' not in downloader, "the flat layout must not linger in defaults"
    assert downloader['ghosteshop']['interval'] == '24h'


# --- A-2: identification retry after a keys reload ---

class FakeFile:
    def __init__(self, identified, identification_type, last_attempt):
        self.identified = identified
        self.identification_type = identification_type
        # A file reaching the retry question has always been attempted once;
        # the "never attempted" case is covered by its own test below.
        self.identification_attempts = 1
        self.last_attempt = last_attempt


MGMT = {'organizer': {'enabled': False}}


def test_needs_identify_retries_after_keys_reload(monkeypatch):
    """The bug this pins: a file that failed CNMT identification while keys were
    missing stays failed forever, even after a correct keys.txt is loaded - the
    watcher only re-identifies on file changes. A keys load newer than the last
    attempt must earn exactly one retry."""
    failed_before_keys = FakeFile(False, 'cnmt',
                                  datetime.datetime(2026, 8, 20, 12, 0, 0))
    monkeypatch.setattr(settings_mod, 'KEYS_LOADED_AT',
                        datetime.datetime(2026, 8, 21, 12, 0, 0))
    monkeypatch.setattr('nsz.nut.Keys.keys_loaded', True, raising=False)

    assert tasks_mod._needs_identify(failed_before_keys, MGMT) is True


def test_needs_identify_no_retry_without_new_keys(monkeypatch):
    """Keys loaded before the last attempt: the failure already saw these keys."""
    monkeypatch.setattr(settings_mod, 'KEYS_LOADED_AT',
                        datetime.datetime(2026, 8, 19, 12, 0, 0))
    file = FakeFile(False, 'cnmt', datetime.datetime(2026, 8, 20, 12, 0, 0))
    assert tasks_mod._needs_identify(file, MGMT) is False

    # Never loaded keys at all (keys.txt absent).
    monkeypatch.setattr(settings_mod, 'KEYS_LOADED_AT', None)
    assert tasks_mod._needs_identify(file, MGMT) is False


def test_needs_identify_filename_still_upgrades_to_cnmt(monkeypatch):
    """The pre-existing rule: a file identified by name is re-identified by
    CNMT once keys exist. Still in force."""
    monkeypatch.setattr(settings_mod, 'KEYS_LOADED_AT', None)
    by_name = FakeFile(True, 'filename', None)
    monkeypatch.setattr('nsz.nut.Keys.keys_loaded', True, raising=False)
    assert tasks_mod._needs_identify(by_name, MGMT) is True

    monkeypatch.setattr('nsz.nut.Keys.keys_loaded', False, raising=False)
    assert tasks_mod._needs_identify(by_name, MGMT) is False


def test_needs_identify_unattempted_file_always_runs():
    fresh = FakeFile(False, None, None)
    fresh.identification_attempts = 0
    assert tasks_mod._needs_identify(fresh, MGMT) is True
