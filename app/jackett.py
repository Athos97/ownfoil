import requests
import logging
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode

logger = logging.getLogger('main')

REQUEST_TIMEOUT = 30


def _normalize_url(url):
    url = (url or '').strip().rstrip('/')
    if not url:
        return ''
    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'http://' + url
    return url


def _append_apikey(link, api_key):
    if not link or not api_key:
        return link
    parsed = urlparse(link)
    params = dict(parse_qsl(parsed.query))
    if 'apikey' not in params:
        params['apikey'] = api_key
    new_query = urlencode(params)
    return parsed._replace(query=new_query).geturl()


def test_connection(jackett_settings):
    url = _normalize_url(jackett_settings.get('url'))
    api_key = jackett_settings.get('api_key')
    if not url or not api_key:
        return False, 'Jackett URL and API key are required.'
    try:
        r = requests.get(
            urljoin(url + '/', 'api/v2.0/indexers/all/results/torznab/api'),
            params={'t': 'caps', 'apikey': api_key},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
        )
        if r.status_code in (301, 302):
            return False, 'Jackett redirected to login (check API key).'
        if r.status_code in (401, 403):
            return False, 'Invalid API key (forbidden).'
        r.raise_for_status()
        return True, 'Connected to Jackett.'
    except requests.exceptions.ConnectionError:
        return False, f'Could not reach Jackett at {url}.'
    except Exception as e:
        return False, f'Jackett error: {e}'


def search(jackett_settings, query, indexers=None, categories=None):
    url = _normalize_url(jackett_settings.get('url'))
    api_key = jackett_settings.get('api_key')
    if not url or not api_key or not query:
        return []

    indexer_path = 'all'
    params = {
        'apikey': api_key,
        'query': query,
    }
    if indexers:
        first = indexers[0] if isinstance(indexers, list) else indexers
        if isinstance(first, list) and first:
            indexer_path = ','.join(first)
        elif isinstance(first, str):
            indexer_path = ','.join(indexers)

    if categories:
        if isinstance(categories, list):
            for cat in categories:
                params.setdefault('Category[]', [])
                if isinstance(params['Category[]'], list):
                    params['Category[]'].append(cat)
        else:
            params['Category'] = categories

    endpoint = urljoin(url + '/', f'api/v2.0/indexers/{indexer_path}/results')
    try:
        r = requests.get(endpoint, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f'Jackett search failed for "{query}": {e}')
        return []

    results = []
    for item in data.get('Results', []) or []:
        magnet_uri = item.get('MagnetUri') or ''
        link = item.get('Link') or ''
        download_url = magnet_uri or _append_apikey(link, api_key)
        if not download_url:
            continue
        seeders = int(item.get('Seeders') or 0)
        peers = int(item.get('Peers') or 0)
        results.append({
            'title': item.get('Title') or '',
            'download_url': download_url,
            'magnet_uri': magnet_uri,
            'seeders': seeders,
            'leechers': max(0, peers - seeders),
            'size': int(item.get('Size') or 0),
            'indexer': item.get('Tracker') or item.get('TrackerId') or '',
            'publish_date': item.get('PublishDate') or '',
            'category': item.get('CategoryDesc') or item.get('Category') or '',
        })
    return results
