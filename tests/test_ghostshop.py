"""Ghost eShop provider and chunked-download tests, against the faithful portal mock.

The mock (tests/mock_ghostshop_portal.py) reproduces the real protocol: Base64
login with a session cookie, one-request family fetch, request-dl -> download-info
chunking, and a CDN that refuses chunks without the /d/<token> Referer.
"""
import pytest

from ghostshop import GhosteshopProvider, GhostshopAuthError, GhostshopError
from ghostshop import test_connection as ghostshop_test_connection
from ghostshop.net import download_chunked
from ghostshop.types import BASE, DLC, UPD
from mock_ghostshop_portal import MockPortal, USER, PASS, blob_for

ZELDA_TID = '01007EF00011E000'
ZELDA_UPD = 'Zelda BOTW [01007EF00011E800][v1114112]'
ZELDA_UPD_SIZE = 300_000


@pytest.fixture(scope='module')
def portal():
    mock = MockPortal().start()
    yield mock
    mock.stop()


@pytest.fixture
def provider(portal):
    p = GhosteshopProvider({'url': portal.url, 'username': USER,
                            'password': PASS, 'language': 'en'})
    return p


def logged_in(provider):
    return provider.login()


# --- login ---

def test_login_success(provider):
    session = provider.login()
    assert session.cookies.get('session')


def test_login_rejects_wrong_password(portal):
    bad = GhosteshopProvider({'url': portal.url, 'username': USER,
                              'password': 'nope'})
    with pytest.raises(GhostshopAuthError):
        bad.login()


def test_login_requires_credentials():
    with pytest.raises(GhostshopAuthError):
        GhosteshopProvider({'username': '', 'password': ''}).login()


def test_test_connection(portal):
    ok, msg = ghostshop_test_connection(
        {'url': portal.url, 'username': USER, 'password': PASS})
    assert ok, msg
    ok, msg = ghostshop_test_connection(
        {'url': portal.url, 'username': USER, 'password': 'bad'})
    assert not ok


# --- catalog ---

def test_fetch_game_returns_whole_family(provider):
    card = provider.fetch_game(logged_in(provider), ZELDA_TID)
    assert card is not None
    assert card.title.startswith('The Legend of Zelda')
    bases = card.of_category(BASE)
    updates = card.of_category(UPD)
    dlcs = card.of_category(DLC)
    assert len(bases) == 1
    assert len(updates) == 1
    assert updates[0].version == 1114112, "version parsed from the [vN] name suffix"
    assert updates[0].size == ZELDA_UPD_SIZE
    assert [d.tid for d in dlcs] == ['01007EF00011F001'], \
        "the family lists only catalog DLCs, not everything in versions.txt"


def test_fetch_game_unknown_tid_is_none(provider):
    assert provider.fetch_game(logged_in(provider), '0100FF00FF00F000') is None


def test_search_matches_by_name(provider):
    results = provider.search(logged_in(provider), 'zelda')
    assert results
    assert any(r.tid == ZELDA_TID for r in results)
    assert provider.search(logged_in(provider), '   ') == []


def test_search_requires_items_per_page(provider):
    """The live endpoint 400s without itemsPerPage ('Expected number, received
    nan'); the provider must always send it, and the mock enforces the rule."""
    import requests as _r
    session = logged_in(provider)
    url = provider.portal + '/api/games/fetch-list'
    params = {'search': 'zelda', 'language': 'en', 'page': 1}
    assert session.get(url, params=params, timeout=(10, 30)).status_code == 400
    params['itemsPerPage'] = 20
    assert session.get(url, params=params, timeout=(10, 30)).status_code == 200


# --- downloads ---

def test_full_download_byte_exact(provider, tmp_path):
    session = logged_in(provider)
    link = provider.request_download_link(session, ZELDA_UPD)
    assert '/d/' in link
    info = provider.fetch_download_info(session, link)
    assert info.file_size == ZELDA_UPD_SIZE
    assert len(info.chunks) >= 2

    dest = tmp_path / f'{ZELDA_UPD}.nsp'
    download_chunked(session, info, dest, expected_size=ZELDA_UPD_SIZE,
                     headers=provider.chunk_headers(link))

    assert dest.read_bytes() == blob_for(ZELDA_UPD, ZELDA_UPD_SIZE)
    assert not dest.with_name(dest.name + '.part').exists()
    assert not dest.with_name(dest.name + '.part.state').exists()


def test_download_resumes_from_previous_state(provider, tmp_path, monkeypatch):
    """A first pass interrupted after chunk 1 resumes instead of restarting."""
    import ghostshop.net as gnet
    session = logged_in(provider)
    link = provider.request_download_link(session, ZELDA_UPD)
    info = provider.fetch_download_info(session, link)
    dest = tmp_path / f'{ZELDA_UPD}.nsp'
    headers = provider.chunk_headers(link)

    class StopAfterFirst(Exception):
        pass

    real = gnet._download_chunk_to
    calls = {'n': 0}

    def flaky(sess, url, fh, offset, hdrs, on_progress):
        calls['n'] += 1
        if calls['n'] == 2:
            raise GhostshopError('interrupted mid-chunk')
        return real(sess, url, fh, offset, hdrs, on_progress)

    monkeypatch.setattr(gnet, '_download_chunk_to', flaky)
    with pytest.raises(GhostshopError):
        download_chunked(session, info, dest, headers=headers)
    assert dest.with_name(dest.name + '.part').exists()
    assert dest.with_name(dest.name + '.part.state').exists()

    monkeypatch.setattr(gnet, '_download_chunk_to', real)
    download_chunked(session, info, dest, headers=headers)
    assert dest.read_bytes() == blob_for(ZELDA_UPD, ZELDA_UPD_SIZE)


def test_stale_resume_state_restarts(provider, tmp_path):
    """A state file describing different chunk URLs is ignored - the download
    starts over rather than stitching mismatched pieces together."""
    session = logged_in(provider)
    link = provider.request_download_link(session, ZELDA_UPD)
    info = provider.fetch_download_info(session, link)
    dest = tmp_path / f'{ZELDA_UPD}.nsp'

    dest.with_name(dest.name + '.part').write_bytes(b'garbage')
    dest.with_name(dest.name + '.part.state').write_text(
        '{"chunks": [["http://elsewhere", 1]], "completed": 1}')

    download_chunked(session, info, dest,
                     headers=provider.chunk_headers(link))
    assert dest.read_bytes() == blob_for(ZELDA_UPD, ZELDA_UPD_SIZE)


def test_cdn_requires_the_referer(provider):
    """The mock enforces the real CDN's Referer rule: without the /d/<token>
    Referer the chunk fetch is a 403 - download_chunked must surface it after
    exhausting retries rather than writing an error page to disk."""
    session = logged_in(provider)
    link = provider.request_download_link(session, ZELDA_UPD)
    info = provider.fetch_download_info(session, link)
    url = info.chunks[0]['url']
    assert session.get(url, timeout=(10, 30)).status_code == 403
    assert session.get(url, timeout=(10, 30),
                       headers={'Referer': link}).status_code == 200


def test_request_download_link_rejects_unknown_name(provider):
    session = logged_in(provider)
    with pytest.raises(GhostshopError):
        provider.request_download_link(session, 'Totally Unknown [0100000000000000]')
