import re
import logging
import datetime
import titles as titles_lib
import jackett
import qbittorrent
from constants import *
from db import *
from settings import load_settings

logger = logging.getLogger('main')

SWITCH_EXTS = ('nsp', 'nsz', 'xci', 'xcz')

QB_ERROR_STATES = ('error', 'missingFiles', 'unknown')
QB_ACTIVE_STATES = ('downloading', 'uploading', 'stalledDL', 'stalledUP',
                    'queuedDL', 'queuedUP', 'checkingDL', 'checkingUP',
                    'metaDL', 'forcedDL', 'forcedUP', 'moving')


def _norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def _ext_of(title):
    m = re.search(r'\b(nsp|nsz|xci|xcz)\b', (title or '').lower())
    return m.group(1) if m else None


def _with_titledb(func):
    def wrapper(*args, **kwargs):
        titles_lib.load_titledb()
        try:
            return func(*args, **kwargs)
        finally:
            titles_lib.identification_in_progress_count -= 1
            titles_lib.unload_titledb()
    return wrapper


@_with_titledb
def get_missing_targets():
    targets = []
    titles = get_all_titles()
    for title in titles:
        title_id = title.title_id
        apps = get_all_title_apps(title_id)
        base_info = titles_lib.get_game_info(title_id) or {}
        base_name = base_info.get('name') or title_id

        upd_apps = [a for a in apps if a.get('app_type') == APP_TYPE_UPD]
        if upd_apps:
            best = max(upd_apps, key=lambda a: int(a.get('app_version') or 0))
            if not best.get('owned'):
                ver = str(best.get('app_version'))
                targets.append({
                    'title_id': title_id,
                    'app_id': best.get('app_id'),
                    'app_version': ver,
                    'app_type': APP_TYPE_UPD,
                    'name': base_name,
                    'patch_level': titles_lib.get_update_number(ver),
                })

        dlc_apps = [a for a in apps if a.get('app_type') == APP_TYPE_DLC]
        by_id = {}
        for a in dlc_apps:
            aid = a.get('app_id')
            cur = by_id.get(aid)
            if cur is None or int(a.get('app_version') or 0) > int(cur.get('app_version') or 0):
                by_id[aid] = a
        for aid, best in by_id.items():
            if not best.get('owned'):
                ver = str(best.get('app_version'))
                dlc_info = titles_lib.get_game_info(aid) or {}
                dlc_name = dlc_info.get('name') or base_name
                targets.append({
                    'title_id': title_id,
                    'app_id': aid,
                    'app_version': ver,
                    'app_type': APP_TYPE_DLC,
                    'name': dlc_name,
                    'patch_level': titles_lib.get_update_number(ver),
                })
    return targets


@_with_titledb
def rebuild_target_from_download(d):
    name = None
    if d.app_type == APP_TYPE_UPD:
        name = (titles_lib.get_game_info(d.title_id) or {}).get('name')
    else:
        name = (titles_lib.get_game_info(d.app_id) or {}).get('name')
    name = name or d.title_id
    return {
        'title_id': d.title_id,
        'app_id': d.app_id,
        'app_version': str(d.app_version),
        'app_type': d.app_type,
        'name': name,
        'patch_level': titles_lib.get_update_number(str(d.app_version)),
    }


def build_query(target):
    return target.get('app_id') or target.get('name') or target.get('title_id')


def rank_results(results, target, filters):
    if not results:
        return None, 'No results from Jackett.'

    preferred = filters.get('preferred_ext') or list(SWITCH_EXTS)
    pref_index = {ext: i for i, ext in enumerate(preferred)}
    min_seeders = int(filters.get('min_seeders') or 0)
    try:
        max_size_gb = float(filters.get('max_size_gb') or 0)
    except (TypeError, ValueError):
        max_size_gb = 0

    name_norm = _norm(target.get('name'))
    title_id_norm = _norm(target.get('title_id'))
    app_id_norm = _norm(target.get('app_id'))

    candidates = []
    for r in results:
        title = r.get('title') or ''
        tnorm = _norm(title)
        ext = _ext_of(title)
        if not ext or ext not in pref_index:
            continue
        if not any(tok and tok in tnorm for tok in (name_norm, title_id_norm, app_id_norm)):
            continue

        seeders = int(r.get('seeders') or 0)
        if seeders < min_seeders:
            continue

        size = int(r.get('size') or 0)
        if max_size_gb and size > max_size_gb * 1024 ** 3:
            continue
        if size and size < 1024 * 1024:
            continue

        score = 0
        if target.get('app_type') == APP_TYPE_UPD:
            ver = _norm(str(target.get('app_version')))
            pl = _norm(str(target.get('patch_level')))
            if ver and ver in tnorm:
                score += 100
            elif pl and len(pl) > 1 and pl in tnorm:
                score += 50
        if title_id_norm and title_id_norm in tnorm:
            score += 30
        if app_id_norm and app_id_norm in tnorm:
            score += 30
        score += (len(pref_index) - pref_index[ext])
        score += min(seeders, 200)
        candidates.append((score, r))

    if not candidates:
        return None, 'No results matched the filters (name/extension/seeders/size).'
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1], None


def download_target(target, settings):
    downloader = settings.get('downloader', {})
    filters = downloader.get('filters', {}) or {}
    jackett_settings = downloader.get('jackett', {}) or {}
    qbt_settings = downloader.get('qbittorrent', {}) or {}

    query = build_query(target)
    results = jackett.search(jackett_settings, query, indexers=filters.get('indexers'))
    best, reason = rank_results(results, target, filters)

    common = dict(
        title_id=target.get('title_id'),
        app_id=target.get('app_id'),
        app_version=str(target.get('app_version')),
        app_type=target.get('app_type'),
        name=target.get('name'),
        search_query=query,
    )

    if best is None:
        add_download(**common, status='failed', error=reason or 'No matching results')
        logger.info(f"[downloader] No match for {target.get('app_id')} v{target.get('app_version')}: {reason}")
        return False

    client = qbittorrent.QbittorrentClient(qbt_settings)
    ok, login_err = client.login()
    if not ok:
        logger.error(f"[downloader] qBittorrent login failed: {login_err}")
        add_download(**common, status='failed', error=f'qBittorrent: {login_err}')
        return False

    ok, add_err, info_hash = client.add_torrent(
        best['download_url'],
        save_path=qbt_settings.get('save_path') or None,
        category=qbt_settings.get('category') or None,
    )
    if not ok:
        add_download(**common, torrent_name=best.get('title'), indexer=best.get('indexer'),
                     size=best.get('size'), seeders=best.get('seeders'),
                     status='failed', error=add_err or 'qBittorrent rejected torrent')
        return False

    if not info_hash:
        info_hash = client.find_hash_by_name(best.get('title'), qbt_settings.get('category'))

    add_download(**common, torrent_hash=info_hash, torrent_name=best.get('title'),
                 indexer=best.get('indexer'), size=best.get('size'), seeders=best.get('seeders'),
                 status='downloading' if info_hash else 'queued')
    logger.info(f"[downloader] Added torrent for {target.get('app_id')} v{target.get('app_version')}: {best.get('title')}")
    return True


def sync_downloads_status(settings):
    in_progress = get_downloads_in_progress()
    if not in_progress:
        return
    qbt_settings = settings.get('downloader', {}).get('qbittorrent', {}) or {}
    client = qbittorrent.QbittorrentClient(qbt_settings)
    qb_states = {}
    qb_by_name = {}
    ok, _ = client.login()
    if ok:
        for t in client.get_torrents(category=qbt_settings.get('category')):
            qb_states[(t.get('hash') or '').lower()] = t
            nm = (t.get('name') or '').strip().lower()
            if nm:
                qb_by_name[nm] = t

    for d in in_progress:
        if is_app_owned(d.app_id, d.app_version):
            update_download(d.id, status='completed', error=None)
            continue
        h = (d.torrent_hash or '').lower()
        t = qb_states.get(h) if h else None
        if not t and d.torrent_name:
            resolved = qb_by_name.get((d.torrent_name or '').strip().lower())
            if resolved and resolved.get('hash'):
                update_download(d.id, torrent_hash=resolved['hash'].lower())
                t = resolved
        if t:
            state = t.get('state') or ''
            if state in QB_ERROR_STATES:
                update_download(d.id, status='failed', error=f'qBittorrent state: {state}')
            elif state in QB_ACTIVE_STATES:
                update_download(d.id, status='downloading')


def is_configured(settings):
    d = settings.get('downloader', {}) or {}
    if not d.get('enabled'):
        return False
    j = d.get('jackett', {}) or {}
    q = d.get('qbittorrent', {}) or {}
    return bool(j.get('url') and j.get('api_key') and q.get('url'))


def run_downloader_job():
    settings = load_settings()
    if not is_configured(settings):
        logger.info('Downloader not enabled/configured, skipping.')
        return
    logger.info('Starting downloader job...')
    try:
        sync_downloads_status(settings)
        targets = get_missing_targets()
        logger.info(f'Downloader: {len(targets)} missing target(s).')
        added = 0
        for target in targets:
            if get_download_by_app(target.get('app_id'), str(target.get('app_version'))):
                continue
            try:
                if download_target(target, settings):
                    added += 1
            except Exception as e:
                logger.error(f"[downloader] Error processing {target.get('app_id')}: {e}")
        sync_downloads_status(settings)
        logger.info(f'Downloader job done. Added {added} torrent(s).')
    except Exception as e:
        logger.error(f'Downloader job failed: {e}')


def retry_download(download_id, settings):
    d = get_download_by_id(download_id)
    if not d:
        return False, 'Download not found'
    if is_app_owned(d.app_id, d.app_version):
        update_download(d.id, status='completed', error=None)
        return True, 'Already owned'
    delete_download(download_id)
    target = rebuild_target_from_download(d)
    ok = download_target(target, settings)
    return ok, 'Re-searched' if ok else 'No match found'


def _serialize_download(d):
    row = to_dict(d)
    for key in ('created_at', 'updated_at'):
        val = row.get(key)
        if isinstance(val, datetime.datetime):
            row[key] = val.isoformat()
    return row


def get_downloads_status():
    return [_serialize_download(d) for d in get_all_downloads()]
