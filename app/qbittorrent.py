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
            ok = r.status_code == 200 and 'ok' in r.text.strip().lower()
            info_hash = extract_info_hash(download_url)
            if not ok:
                return False, f'qBittorrent rejected the torrent: {r.text.strip()}', info_hash
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


def test_connection(qbt_settings):
    client = QbittorrentClient(qbt_settings)
    return client.login()
