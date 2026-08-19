import re
import logging
import requests
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger('main')

REQUEST_TIMEOUT = 30
MAGNET_HASH_RE = re.compile(r'urn:btih:([a-fA-F0-9]{40}|[A-Za-z2-7]{32})', re.IGNORECASE)


def _normalize_url(url):
    url = (url or '').strip().rstrip('/')
    if not url:
        return ''
    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'http://' + url
    return url


def extract_info_hash(magnet_uri):
    if not magnet_uri:
        return None
    match = MAGNET_HASH_RE.search(magnet_uri)
    return match.group(1).lower() if match else None


class QbittorrentClient:
    def __init__(self, qbt_settings):
        self.base_url = _normalize_url(qbt_settings.get('url'))
        self.username = qbt_settings.get('username')
        self.password = qbt_settings.get('password')
        self.session = requests.Session()

    def _api(self, path):
        return self.base_url.rstrip('/') + path

    def login(self):
        if not self.base_url:
            return False, 'qBittorrent URL is required.'
        try:
            r = self.session.post(
                self._api('/api/v2/auth/login'),
                data={'username': self.username, 'password': self.password},
                headers={'Referer': self.base_url},
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code == 204:
                return True, 'Login successful.'
            if r.status_code == 200 and 'ok' in r.text.strip().lower():
                return True, 'Login successful.'
            if r.status_code == 403:
                return False, 'Forbidden: too many failed attempts (temporarily banned).'
            return False, f'Login failed: {r.status_code} {r.text.strip()}'
        except requests.exceptions.ConnectionError:
            return False, f'Could not reach qBittorrent at {self.base_url}.'
        except Exception as e:
            return False, f'qBittorrent error: {e}'

    def add_torrent(self, download_url, save_path=None, category=None):
        if not download_url:
            return False, 'No download URL provided.', None
        data = {'urls': download_url}
        if save_path:
            data['savepath'] = save_path
        if category:
            data['category'] = category
        try:
            r = self.session.post(
                self._api('/api/v2/torrents/add'),
                data=data,
                headers={'Referer': self.base_url},
                timeout=REQUEST_TIMEOUT,
            )
            info_hash = extract_info_hash(download_url)
            ok = False
            if r.status_code in (200, 202):
                try:
                    resp = r.json()
                    if isinstance(resp, dict) and ('failure_count' in resp or 'pending_count' in resp):
                        ok = int(resp.get('failure_count', 0)) == 0
                        ids = resp.get('added_torrent_ids') or []
                        if ids and not info_hash:
                            info_hash = str(ids[0]).lower()
                    else:
                        ok = 'ok' in str(resp).lower()
                except ValueError:
                    ok = r.text.strip().lower() == 'ok.'
            if not ok:
                return False, f'qBittorrent rejected the torrent: {r.text.strip()[:200]}', info_hash
            return True, 'Torrent added.', info_hash
        except Exception as e:
            return False, f'Error adding torrent: {e}', None

    def get_torrents(self, hashes=None, category=None):
        params = {}
        if hashes:
            params['hashes'] = hashes
        if category:
            params['category'] = category
        try:
            r = self.session.get(
                self._api('/api/v2/torrents/info'),
                params=params,
                headers={'Referer': self.base_url},
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.warning(f'qBittorrent get_torrents failed: {e}')
            return []

    def find_hash_by_name(self, name, category=None):
        if not name:
            return None
        needle = name.strip().lower()
        for t in self.get_torrents(category=category):
            cand = (t.get('name') or '').strip().lower()
            if cand and (cand == needle or needle in cand or cand in needle):
                return (t.get('hash') or '').lower() or None
        return None


def test_connection(qbt_settings):
    client = QbittorrentClient(qbt_settings)
    return client.login()
