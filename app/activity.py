"""User activity capture: who connects to the shop, who downloads what, who logs in.

Written at the three points where the full context (user, client, device, IP, file)
is available, then read by the admin Activity page through the realtime topic and
the GraphQL query. Each write prunes the table to its retention cap, so there is no
cleanup schedule to forget.
"""
import logging
import time
from functools import wraps

from flask import Request

from db import record_activity
from utils import client_address

logger = logging.getLogger('main')

# One connect event per (user, device, client) per window: a client refresh walks
# several endpoints in a burst, which is one visit, not five. Failed shop logins get
# their own registry - sharing one with successes would let a failed attempt's
# throttle window swallow the successful retry that follows it (basic_auth() still
# resolves request.user for a *known* username with the *wrong* password, so the
# two events would otherwise collide on the same (username, device, client) key).
CONNECT_THROTTLE_SECONDS = 60
_connect_registry: dict = {}
_authfail_registry: dict = {}
_connect_lock = __import__('threading').Lock()


def _throttled(registry, key):
    now = time.monotonic()
    with _connect_lock:
        last = registry.get(key)
        if last is None or (now - last) >= CONNECT_THROTTLE_SECONDS:
            registry[key] = now
            return True
        return False


def _throttled_connect(key):
    return _throttled(_connect_registry, key)


def _throttled_authfail(key):
    return _throttled(_authfail_registry, key)


def _safe_record(**kwargs):
    """Audit must never break the action being audited."""
    try:
        record_activity(**kwargs)
    except Exception as e:
        logger.warning(f'Could not record activity event: {e}')


def _client_of_request(request: Request, client_name=None):
    return (client_name or '').lower() or None


def record_shop_connect(request: Request, client_name, username=None, device_uid=None):
    """A shop client fetched the catalogue. Throttled per (user, device, client)."""
    username = username or (request.user.user if getattr(request, 'user', None) else None)
    device_uid = device_uid or request.headers.get('Uid') if request else None
    key = (username, device_uid, (client_name or '').lower())
    if not _throttled_connect(key):
        return
    _safe_record(
        kind='shop_connect',
        username=username,
        client=(client_name or '').lower() or None,
        device_uid=device_uid,
        ip=client_address(request) if request else None,
    )


def record_shop_auth_failed(request: Request, client_name, attempted_username=None, detail=None):
    """A shop client's Basic Auth (or, once past that, its Hauth host check) was
    rejected. Without this, a bad Tinfoil/CyberFoil password showed up on Activity
    identically to a normal visit - 'Connected', no error, no way to tell the two
    apart (the live incident: a mistyped username read as a successful connection
    until the real one arrived). Throttled per (username, device, client) like
    record_shop_connect, but through its own registry - see the note above."""
    device_uid = request.headers.get('Uid') if request else None
    key = (attempted_username, device_uid, (client_name or '').lower())
    if not _throttled_authfail(key):
        return
    _safe_record(
        kind='login_failed',
        username=attempted_username,
        client=(client_name or '').lower() or None,
        device_uid=device_uid,
        ip=client_address(request) if request else None,
        detail=detail,
    )


def record_download(request: Request, filepath, size=None, client_name=None,
                    username=None, counted=True):
    """A file was served for download. `counted` mirrors the throttled download
    counter: False when the same (file, host) pair was already counted seconds ago,
    which is a resumed transfer rather than a new download."""
    if not counted:
        return
    _safe_record(
        kind='download',
        username=username,
        client=_client_of_request(request, client_name),
        device_uid=(request.headers.get('Uid') if request else None),
        ip=client_address(request) if request else None,
        filename=filepath.rsplit('/', 1)[-1] if filepath else None,
        size=size,
    )


def record_login(username, success, ip=None, detail=None):
    """A web login attempt (success or failure)."""
    _safe_record(
        kind='login' if success else 'login_failed',
        username=username,
        client='web',
        ip=ip,
        detail=detail,
    )
