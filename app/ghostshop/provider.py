"""Ghost eShop PRO provider (https://pro.nlib.cc).

Protocol highlights (reverse-engineered from the official web app; see the
switch-library-updater docs for the full write-up):

  - login: POST /api/auth/login with username/password Base64 (UTF-8) encoded;
    the session travels in the "session" cookie - no Bearer header, and the
    server rejects plaintext credentials.
  - catalog: GET /api/games/fetch?gameTid= returns every file of a family
    (base, all updates, all DLCs) in one request; each file's version is
    encoded in its name as "…[vN]".
  - search: GET /api/games/fetch-list?search=…&language=…&page=… lists games
    (not files) by text query.
  - download: POST /api/games/request-dl -> dlLink (an NXEnc page, valid ~6h);
    GET <dlhost>/api/download-info/<token> -> ordered chunks; the CDN requires
    the Referer of the /d/<token> page to serve each chunk.
"""
from __future__ import annotations

import base64
import re
from typing import List, Optional
from urllib.parse import urlsplit

import requests

from .types import (
    BASE, DLC, UPD,
    CatalogEntry, DownloadInfo, GameCard, SearchResult,
    GhostshopAuthError, GhostshopError,
)

DEFAULT_PORTAL = 'https://pro.nlib.cc'
REQUEST_TIMEOUT = (10, 30)

USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/126.0 Safari/537.36 ownfoil-ghostshop/1.0')

DOWNLOADER_HEADERS = {
    'Accept': '*/*',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'cross-site',
}

_VERSION_RE = re.compile(r'\[v(\d+)\]')


def normalize_base_url(url):
    url = (url or '').strip()
    if not url:
        return ''
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    return url.rstrip('/')


def _b64(value: str) -> str:
    return base64.b64encode(str(value).encode('utf-8')).decode('ascii')


# ------------------------------------------------------------- normalization
def entry_tid(entry: dict) -> str:
    value = entry.get('tid') or entry.get('titleId') or entry.get('basetid') or ''
    text = str(value).strip()
    if text.lower().startswith('0x'):
        text = text[2:]
    if len(text) == 16:
        try:
            return f'{int(text, 16):016X}'
        except ValueError:
            pass
    return ''


def entry_category(entry: dict) -> str:
    value = str(entry.get('type') or entry.get('category') or '').strip().lower()
    return {'base': BASE, 'game': BASE, 'update': UPD,
            'dlc': DLC, 'dlc_pack': DLC}.get(value, value.upper() or BASE)


def entry_version(entry: dict) -> int:
    """Numeric `version` field, or the [vN] suffix of the file name."""
    try:
        parsed = int(entry.get('version'))
        if parsed > 0:
            return parsed
    except (TypeError, ValueError):
        pass
    name = str(entry.get('name') or '')
    matches = _VERSION_RE.findall(name)
    if matches:
        try:
            return int(matches[-1])
        except ValueError:
            return 0
    return 0


def entry_size(entry: dict) -> int:
    try:
        parsed = int(entry.get('size') or entry.get('fileSize'))
        return parsed if parsed > 0 else 0
    except (TypeError, ValueError):
        return 0


def _normalize(entry: dict) -> CatalogEntry:
    return CatalogEntry(
        name=str(entry.get('name') or entry.get('fileName') or '').strip(),
        tid=entry_tid(entry),
        category=entry_category(entry),
        version=entry_version(entry),
        size=entry_size(entry),
    )


class GhosteshopProvider:
    """One provider instance per job/call: login() keeps the session's portal."""

    def __init__(self, settings: dict):
        self.portal = normalize_base_url(
            settings.get('url') or DEFAULT_PORTAL)
        self.username = str(settings.get('username') or '')
        self.password = str(settings.get('password') or '')
        self.language = str(settings.get('language') or 'en')
        self.verify_ssl = bool(settings.get('verify_ssl'))

    # ---- session
    def login(self) -> requests.Session:
        if not self.username or not self.password:
            raise GhostshopAuthError(
                'Ghost eShop username and password are required.')
        session = requests.Session()
        session.headers.update({
            'User-Agent': USER_AGENT,
            'Accept': 'application/json',
            'Origin': self.portal,
            'Referer': self.portal + '/login',
        })
        session.verify = self.verify_ssl
        if not self.verify_ssl:
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except ImportError:
                pass

        response = session.post(
            self.portal + '/api/auth/login',
            json={'username': _b64(self.username),
                  'password': _b64(self.password),
                  'longerSession': True},
            timeout=REQUEST_TIMEOUT)
        data = self._parse_json(response, '/api/auth/login')

        token = data.get('token') or data.get('accessToken')
        if isinstance(token, str) and token:
            session.cookies.set('session', token)

        user = self._parse_json(
            session.get(self.portal + '/api/user', timeout=REQUEST_TIMEOUT),
            '/api/user')
        if not user:
            raise GhostshopAuthError(
                'portal accepted the login but /api/user returned nothing')
        return session

    def _parse_json(self, response: requests.Response, what: str) -> dict:
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code == 401:
            if 'auth/login' in what:
                raise GhostshopAuthError(
                    'Ghost eShop credentials rejected (401)')
            raise GhostshopAuthError('Ghost eShop session expired (401)')
        if response.status_code == 403:
            detail = data.get('message') if isinstance(data, dict) else ''
            raise GhostshopAuthError(
                f'Ghost eShop access denied (403). Active PRO subscription? {detail or ""}'.strip())
        if response.status_code >= 400:
            detail = data.get('message') if isinstance(data, dict) else ''
            raise GhostshopError(
                f'HTTP {response.status_code} on {what}: {detail or ""}'.strip())
        return data if isinstance(data, dict) else {}

    # ---- catalog
    def fetch_game(self, session: requests.Session, base_tid: str,
                   language: Optional[str] = None) -> Optional[GameCard]:
        try:
            response = session.get(
                self.portal + '/api/games/fetch',
                params={'gameTid': base_tid,
                        'language': language or self.language},
                timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise GhostshopError(f'network error on /api/games/fetch: {exc}') from exc
        if response.status_code == 404:
            return None
        data = self._parse_json(response, '/api/games/fetch')
        if not data:
            return None
        entries: List[CatalogEntry] = []
        files = data.get('files') or {}
        for key in ('base', 'update', 'dlc'):
            seq = files.get(key)
            if isinstance(seq, list):
                entries.extend(_normalize(e) for e in seq if isinstance(e, dict))
        return GameCard(title=str(data.get('title') or ''), entries=entries)

    def search(self, session: requests.Session, text: str,
               page: int = 1, language: Optional[str] = None,
               limit: int = 50) -> List[SearchResult]:
        """Text search over the catalog (fetch-list). Returns games, not files.

        `itemsPerPage` is mandatory on the real endpoint: without it the query
        validator coerces the missing number to NaN and answers 400."""
        text = (text or '').strip()
        if not text:
            return []
        try:
            response = session.get(
                self.portal + '/api/games/fetch-list',
                params={'search': text, 'language': language or self.language,
                        'page': page, 'itemsPerPage': max(1, min(limit, 50))},
                timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise GhostshopError(f'network error on /api/games/fetch-list: {exc}') from exc
        data = self._parse_json(response, '/api/games/fetch-list')
        # Deployments wrap the rows differently: a bare list, or an object with
        # one known key (the live portal uses {"items": [...], "total": N}).
        items = data
        if isinstance(data, dict):
            items = None
            for key in ('items', 'results', 'games', 'data', 'list'):
                seq = data.get(key)
                if isinstance(seq, list):
                    items = seq
                    break
        if not isinstance(items, list):
            return []
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            tid = entry_tid(item)
            title = str(item.get('title') or item.get('name') or '').strip()
            if tid and title:
                out.append(SearchResult(tid=tid, title=title))
        return out

    # ---- download
    def request_download_link(self, session: requests.Session, name: str) -> str:
        file_ref = _b64(name)
        try:
            response = session.post(
                self.portal + '/api/games/request-dl',
                json={'fileRef': file_ref}, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise GhostshopError(f'network error on /api/games/request-dl: {exc}') from exc
        data = self._parse_json(response, '/api/games/request-dl')

        link = data.get('dlLink') or data.get('dl_link') or data.get('link') or ''
        if not isinstance(link, str) or not link:
            raise GhostshopError(f'portal returned no download link for: {name}')
        if link.startswith('//'):
            link = 'https:' + link
        elif link.startswith('/'):
            parts = urlsplit(self.portal)
            link = f'{parts.scheme}://{parts.netloc}{link}'
        return link

    def fetch_download_info(self, session: requests.Session,
                            dl_link: str) -> DownloadInfo:
        token = dl_link.rstrip('/').rsplit('/', 1)[-1]
        url = self._downloader_base(dl_link) + '/api/download-info/' + token
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise GhostshopError(f'network error on download-info: {exc}') from exc
        data = self._parse_json(response, 'download-info')
        chunks = data.get('chunks')
        if not isinstance(chunks, list) or not chunks:
            raise GhostshopError(
                f'portal returned no chunks for: {data.get("fileName", token)}')
        try:
            size = int(data.get('fileSize') or data.get('size') or 0)
        except (TypeError, ValueError):
            size = 0
        return DownloadInfo(
            file_name=str(data.get('fileName') or data.get('name') or ''),
            file_size=size,
            chunks=chunks,
            referer=dl_link,
        )

    def chunk_headers(self, dl_link: str) -> dict:
        headers = dict(DOWNLOADER_HEADERS)
        headers['Referer'] = dl_link
        parts = urlsplit(dl_link)
        headers['Origin'] = f'{parts.scheme}://{parts.netloc}'
        return headers

    def download_complete(self, session: requests.Session, dl_link: str) -> None:
        """Best-effort completion notice; never raises."""
        token = dl_link.rstrip('/').rsplit('/', 1)[-1]
        url = self._downloader_base(dl_link) + '/api/download-complete/' + token
        try:
            session.post(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            pass

    @staticmethod
    def _downloader_base(dl_link: str) -> str:
        parts = urlsplit(dl_link)
        return f'{parts.scheme}://{parts.netloc}'


def test_connection(settings: dict):
    """Validate the Ghost eShop settings by logging in. Returns (ok, message)."""
    try:
        provider = GhosteshopProvider(settings)
        provider.login()
        return True, 'Connected to Ghost eShop.'
    except GhostshopAuthError as e:
        return False, str(e)
    except GhostshopError as e:
        return False, str(e)
    except Exception as e:  # unreachable host, TLS garbage, ...
        return False, f'Ghost eShop error: {e}'
