"""Auto-download missing updates and DLCs from the configured sources.

Two sources coexist, each with its own settings block, schedule and manual
trigger, sharing one target list and one `downloads` table:

  - torrents: search Jackett, rank the results, hand the best torrent to
    qBittorrent downloading straight into a library path - the file watcher
    then identifies it and app ownership flips the row to `completed`.
    The pass itself only talks to the APIs; the disk writes are qBittorrent's,
    outside ownfoil's I/O budget by necessity.
  - ghosteshop: log into Ghost eShop PRO, resolve the exact catalog entry,
    and chunked-download it (with resume) into the game's own folder inside
    a library path - completion flows through the watcher the same way.
    Each file downloads as its own `ghosteshop_download` task in the `io`
    concurrency group, sharing the Workers I/O budget with verification and
    compression: `prepare_ghosteshop_targets` computes the list, the tasks
    do the transferring.

One row per (app_id, app_version) target: whichever source claims it first,
the other leaves it alone. Failed rows are retried only by their own source.
"""
import os
import re
import time
import logging
import titles as titles_lib
import jackett
import qbittorrent
import ghostshop
import ghostshop.net
from sqlalchemy import text
from constants import *
from db import *
from settings import get_settings, get_library_paths
from utils import sanitize_filename, trim_name

logger = logging.getLogger('main')

SWITCH_EXTS = ('nsp', 'nsz', 'xci', 'xcz')

SOURCE_TORRENTS = 'torrents'
SOURCE_GHOSTESHOP = ghostshop.SOURCE_GHOSTESHOP

# The per-file io task the Ghost eShop pass hands its work to (registered in
# tasks.py). The name lives here too because orphan healing matches it against
# the tasks table.
GHOSTESHOP_DOWNLOAD_TASK = 'ghosteshop_download'

QB_ERROR_STATES = ('error', 'missingFiles', 'unknown')
QB_ACTIVE_STATES = ('downloading', 'uploading', 'stalledDL', 'stalledUP',
                    'queuedDL', 'queuedUP', 'checkingDL', 'checkingUP',
                    'metaDL', 'forcedDL', 'forcedUP', 'moving')

# Progress rows are written at most this often while a Ghost eShop file streams;
# the realtime topic polls the table at 0.25s, so faster writes buy nothing.
PROGRESS_WRITE_INTERVAL = 1.0


def _norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def _ext_of(title):
    m = re.search(r'\b(nsp|nsz|xci|xcz)\b', (title or '').lower())
    return m.group(1) if m else None


def get_missing_targets():
    """Latest-version unowned UPDATE/DLC apps, with a display name from titledb.

    Blacklisted apps are skipped: they are deliberately not wanted content, so no
    source should ever search for or download them."""
    targets = []
    blacklisted = get_blacklisted_app_ids()
    titles = get_all_titles()
    for title in titles:
        title_id = title.title_id
        apps = get_all_title_apps(title_id)
        base_info = titles_lib.get_game_info(title_id) or {}
        base_name = base_info.get('name') or title_id

        upd_apps = [a for a in apps if a.get('app_type') == APP_TYPE_UPD]
        if upd_apps:
            best = max(upd_apps, key=lambda a: int(a.get('app_version') or 0))
            if not best.get('owned') and best.get('app_id') not in blacklisted:
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
            if aid in blacklisted:
                continue
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


def build_queries(target):
    """Search queries to try, most specific first.

    Scene releases on id-indexing trackers carry the app id; most trackers only
    match on the game's name, so the app-id query alone starves them. Duplicates
    removed, order preserved.
    """
    queries = []
    app_id = target.get('app_id')
    name = target.get('name')
    title_id = target.get('title_id')
    if app_id:
        queries.append(app_id)
    if name:
        queries.append(name)
        if target.get('app_type') == APP_TYPE_UPD:
            queries.append(f'{name} update')
    if title_id:
        queries.append(title_id)
    return list(dict.fromkeys(q for q in queries if q))


def rank_results(results, target, filters, owned_versions=()):
    """Pick the best Jackett result for a target, or explain why none qualified.

    `owned_versions` are version strings the library already holds for this app id:
    a result advertising one of them is a re-download of something owned, however
    well it scores, so it is dropped outright.
    """
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
    owned_norms = [_norm(str(v)) for v in owned_versions if v is not None]

    candidates = []
    for r in results:
        title = r.get('title') or ''
        tnorm = _norm(title)
        ext = _ext_of(title)
        if not ext or ext not in pref_index:
            continue
        if not any(tok and tok in tnorm for tok in (name_norm, title_id_norm, app_id_norm)):
            continue
        if any(ov and ov in tnorm for ov in owned_norms):
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


# ------------------------------------------------------------------ torrents

def download_target_torrents(target, settings, added_cache=None):
    """Search, rank and hand one target's torrent to qBittorrent.

    `added_cache` (url -> info_hash, shared across one pass) lets several targets
    satisfied by the same bundle torrent reuse a single add: qBittorrent would
    deduplicate them anyway, but each repeated add re-downloads the .torrent from
    Jackett and burns an API call.
    """
    downloader = settings.get('downloader', {})
    torrents = downloader.get('torrents', {}) or {}
    filters = torrents.get('filters', {}) or {}
    jackett_settings = torrents.get('jackett', {}) or {}
    qbt_settings = torrents.get('qbittorrent', {}) or {}

    common = dict(
        title_id=target.get('title_id'),
        app_id=target.get('app_id'),
        app_version=str(target.get('app_version')),
        app_type=target.get('app_type'),
        name=target.get('name'),
        source=SOURCE_TORRENTS,
    )
    owned_versions = get_owned_app_versions(target.get('app_id'))

    best, reason, query = None, 'No results from Jackett.', None
    for query in build_queries(target):
        results = jackett.search(jackett_settings, query, indexers=filters.get('indexers'))
        best, reason = rank_results(results, target, filters, owned_versions)
        if best is not None:
            break

    common['search_query'] = query

    if best is None:
        add_download(**common, status='failed', error=reason or 'No matching results')
        logger.info(f"[torrents] No match for {target.get('app_id')} v{target.get('app_version')}: {reason}")
        return False

    url = best['download_url']
    if added_cache is not None and url in added_cache:
        ok, add_err, info_hash = True, None, added_cache[url]
        logger.info(f"[torrents] Torrent already added this pass, reusing: {best.get('title')}")
    else:
        client = qbittorrent.QbittorrentClient(qbt_settings)
        ok, login_err = client.login()
        if not ok:
            logger.error(f"[torrents] qBittorrent login failed: {login_err}")
            add_download(**common, status='failed', error=f'qBittorrent: {login_err}')
            return False

        ok, add_err, info_hash = client.add_torrent(
            url,
            save_path=qbt_settings.get('save_path') or None,
            category=qbt_settings.get('category') or None,
        )
        if not ok:
            add_download(**common, torrent_name=best.get('title'), indexer=best.get('indexer'),
                         size=best.get('size'), seeders=best.get('seeders'),
                         source=SOURCE_TORRENTS, status='failed',
                         error=add_err or 'qBittorrent rejected torrent')
            return False

        if not info_hash:
            info_hash = client.find_hash_by_name(best.get('title'), qbt_settings.get('category'))
        if added_cache is not None:
            added_cache[url] = info_hash

    add_download(**common, torrent_hash=info_hash, torrent_name=best.get('title'),
                 indexer=best.get('indexer'), size=best.get('size'), seeders=best.get('seeders'),
                 status='downloading' if info_hash else 'queued')
    logger.info(f"[torrents] Added torrent for {target.get('app_id')} "
                f"v{target.get('app_version')}: {best.get('title')}")
    return True


# ---------------------------------------------------------------- ghosteshop

def _ghost_settings(settings):
    return (settings.get('downloader', {}) or {}).get('ghosteshop', {}) or {}


def _ghost_library_path(settings):
    """Where Ghost eShop files land: the configured path, or the first library."""
    configured = _ghost_settings(settings).get('library_path') or ''
    if configured:
        return configured
    paths = get_library_paths()
    return paths[0] if paths else ''


def _ghost_destination(entry, target, settings):
    """<library>/<sanitized game name>/<sanitized catalog file name>."""
    library_path = _ghost_library_path(settings)
    windows_compatible = bool(
        (settings.get('library', {}).get('management', {})
         .get('organizer', {}) or {}).get('windows_compatible'))

    base_name = (titles_lib.get_game_info(target.get('title_id')) or {}).get('name') \
        or target.get('name') or target.get('title_id') or 'unknown'
    game_folder = trim_name(sanitize_filename(base_name, windows_compatible),
                            MAX_NAME_WINDOWS)
    filename = sanitize_filename(entry.name or f"{entry.tid}.nsp", windows_compatible)
    return os.path.join(library_path, game_folder, filename)


def _resolve_ghost_entry(provider, session, target, settings):
    """Find the catalog entry for this (app_id, version) target.

    Exact version preferred; when the catalog only knows a newer version the
    newest entry wins - the catalog is the download source, so its latest is
    at least as new as what titledb knew.
    """
    base_tid = target.get('title_id') or ''
    try:
        card = provider.fetch_game(session, base_tid)
    except ghostshop.GhostshopError as e:
        logger.warning(f"[ghosteshop] fetch_game failed for {base_tid}: {e}")
        return None
    if card is None:
        return None
    want_tid = (target.get('app_id') or '').upper()
    candidates = [e for e in card.entries if e.tid and e.tid.upper() == want_tid]
    if not candidates:
        return None
    want_ver = int(target.get('app_version') or 0)
    exact = [e for e in candidates if e.version == want_ver]
    return max(exact or candidates, key=lambda e: e.version)


def _make_progress_cb(row_id):
    """Throttled DB updates of the download row's progress percentage."""
    last = [0.0]

    def cb(done, total):
        now = time.monotonic()
        if total and done < total and (now - last[0]) < PROGRESS_WRITE_INTERVAL:
            return
        last[0] = now
        pct = int(done * 100 / total) if total else 0
        update_download(row_id, progress=pct)

    return cb


def download_target_ghosteshop(target, settings, existing_row=None):
    """Download one target straight from Ghost eShop into its game folder."""
    ghost = _ghost_settings(settings)

    common = dict(
        title_id=target.get('title_id'),
        app_id=target.get('app_id'),
        app_version=str(target.get('app_version')),
        app_type=target.get('app_type'),
        name=target.get('name'),
        source=SOURCE_GHOSTESHOP,
    )

    if is_app_owned(target.get('app_id'), str(target.get('app_version'))):
        row = existing_row or get_download_by_app(
            target.get('app_id'), str(target.get('app_version')))
        if row:
            update_download(row.id, status='completed', error=None, progress=100)
        return True

    try:
        provider = ghostshop.GhosteshopProvider(ghost)
        session = provider.login()
    except ghostshop.GhostshopError as e:
        add_download(**common, status='failed', error=str(e))
        logger.error(f"[ghosteshop] login failed: {e}")
        return False

    entry = _resolve_ghost_entry(provider, session, target, settings)
    if entry is None:
        reason = 'Not found in the Ghost eShop catalog'
        add_download(**common, status='failed', error=reason)
        logger.info(f"[ghosteshop] No match for {target.get('app_id')} "
                    f"v{target.get('app_version')}")
        return False

    row = add_download(**common, status='downloading', progress=0)
    # add_download returns an existing row untouched, so (re)apply the live fields.
    update_download(row.id, torrent_name=entry.name, indexer='Ghost eShop',
                    size=entry.size, seeders=None, status='downloading',
                    error=None, progress=0)

    destination = _ghost_destination(entry, target, settings)
    if not destination:
        update_download(row.id, status='failed',
                        error='No library path configured for Ghost eShop downloads')
        return False
    logger.info(f"[ghosteshop] Downloading {target.get('app_id')} "
                f"v{target.get('app_version')} -> {destination}")

    try:
        link = provider.request_download_link(session, entry.name)
        info = provider.fetch_download_info(session, link)
        ghostshop.net.download_chunked(
            session, info, destination, expected_size=entry.size,
            headers=provider.chunk_headers(link),
            on_progress=_make_progress_cb(row.id))
        provider.download_complete(session, link)
        update_download(row.id, progress=100)
        logger.info(f"[ghosteshop] Downloaded {entry.name}")
        return True
    except ghostshop.GhostshopError as e:
        update_download(row.id, status='failed', error=str(e))
        logger.error(f"[ghosteshop] download failed for {entry.name}: {e}")
        return False


def queue_ghosteshop_download(title_id, app_id, app_version, app_type, name):
    """Add Content: create a queued row the next Ghost eShop pass will process.

    Works for BASE targets too - the periodic job only computes missing
    updates/DLC, but queued rows are downloaded whatever their type.
    """
    return add_download(
        title_id=title_id,
        app_id=app_id,
        app_version=str(app_version),
        app_type=app_type,
        name=name,
        source=SOURCE_GHOSTESHOP,
        status='queued',
    )


# -------------------------------------------------------------------- shared

def sync_downloads_status(settings):
    """Reconcile queued/downloading rows against qBittorrent and app ownership."""
    in_progress = get_downloads_in_progress()
    if not in_progress:
        return

    qb_states = {}
    qb_by_name = {}
    qbt_settings = ((settings.get('downloader', {}) or {}).get('torrents', {})
                    or {}).get('qbittorrent', {}) or {}
    needs_qbt = any(d.torrent_hash or (d.source or SOURCE_TORRENTS) == SOURCE_TORRENTS
                    for d in in_progress)
    if needs_qbt:
        client = qbittorrent.QbittorrentClient(qbt_settings)
        ok, _ = client.login()
        if ok:
            for t in client.get_torrents(category=qbt_settings.get('category')):
                qb_states[(t.get('hash') or '').lower()] = t
                nm = (t.get('name') or '').strip().lower()
                if nm:
                    qb_by_name[nm] = t

    for d in in_progress:
        if is_app_owned(d.app_id, d.app_version):
            update_download(d.id, status='completed', error=None, progress=100)
            continue
        if d.source == SOURCE_GHOSTESHOP:
            # Progress comes from the job itself; completion arrives via ownership.
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
                progress = int(float(t.get('progress') or 0) * 100)
                update_download(d.id, status='downloading', progress=progress)


def torrents_configured(settings):
    d = (settings.get('downloader', {}) or {}).get('torrents', {}) or {}
    if not d.get('enabled'):
        return False
    j = d.get('jackett', {}) or {}
    q = d.get('qbittorrent', {}) or {}
    return bool(j.get('url') and j.get('api_key') and q.get('url'))


def ghosteshop_configured(settings):
    g = _ghost_settings(settings)
    return bool(g.get('enabled') and g.get('url') and g.get('username')
                and g.get('password'))


def is_configured(settings):
    """Kept for callers/tests: either source being configured counts."""
    return torrents_configured(settings) or ghosteshop_configured(settings)


# ---------------------------------------------------------------- ghosteshop pass

def _requeue_orphan_ghosteshop_rows():
    """Ghost rows left 'downloading' by a cancelled or interrupted pass - one whose
    per-file task is no longer alive - go back to 'queued' so this pass re-enqueues
    them. The .part file on disk makes resuming cheap."""
    rows = db.session.execute(text(f"""
        SELECT d.id FROM downloads d
        WHERE d.source = :source AND d.status = 'downloading'
          AND NOT EXISTS (
            SELECT 1 FROM tasks t
            WHERE t.task_name = :task_name
              AND t.status IN ('pending', 'running')
              AND json_extract(t.input_json, '$.app_id') = d.app_id
              AND json_extract(t.input_json, '$.app_version') = d.app_version
          )
    """), {'source': SOURCE_GHOSTESHOP,
           'task_name': GHOSTESHOP_DOWNLOAD_TASK}).all()
    for (row_id,) in rows:
        update_download(row_id, status='queued', error=None, progress=0)
        logger.info(f'[ghosteshop] Requeued orphaned download row {row_id} '
                    '(no live task for it).')


def prepare_ghosteshop_targets(settings=None):
    """Compute the Ghost eShop work list for one pass. Downloading is NOT done
    here: each target becomes its own `ghosteshop_download` io task, so transfers
    share the Workers I/O budget with verification and compression.

    Returns [{app_id, app_version, name}] - Add Content rows first, then the
    computed missing targets, deduped by (app_id, app_version)."""
    settings = settings or get_settings()
    if not ghosteshop_configured(settings):
        logger.info('Ghost eShop source not enabled/configured, skipping.')
        return []

    sync_downloads_status(settings)
    _requeue_orphan_ghosteshop_rows()

    targets = []
    # Explicit queued rows first (Add Content) - bases included.
    for d in get_downloads_in_progress():
        if d.source == SOURCE_GHOSTESHOP and d.status == 'queued':
            targets.append({'app_id': d.app_id,
                            'app_version': str(d.app_version),
                            'name': d.name or d.app_id})
    # Then the computed missing updates/DLCs.
    for target in get_missing_targets():
        app_id = target.get('app_id')
        ver = str(target.get('app_version'))
        row = get_download_by_app(app_id, ver)
        if row is not None:
            if (row.source or SOURCE_TORRENTS) != SOURCE_GHOSTESHOP:
                continue  # the other lane owns this target
            if row.status != 'failed':
                continue  # queued already listed; downloading rows were healed above
            # A failed row would otherwise block the target forever: content can
            # appear after the first miss, so every pass retries failed targets
            # from scratch - same rule as the torrents lane.
            logger.info(f'[ghosteshop] Retrying failed download for {app_id} v{ver}.')
            delete_download(row.id)
        targets.append({'app_id': app_id, 'app_version': ver,
                        'name': target.get('name') or app_id})

    seen = set()
    out = []
    for t in targets:
        key = (t['app_id'], t['app_version'])
        if key not in seen:
            seen.add(key)
            out.append(t)
    logger.info(f'Ghost eShop pass: {len(out)} item(s) to download.')
    return out


def download_ghosteshop_row(app_id, app_version, settings=None):
    """Download one downloads row via Ghost eShop - the body of the per-file io
    task. The download row is the source of truth: a vanished or foreign-lane
    row is a no-op, an owned target flips to completed."""
    settings = settings or get_settings()
    row = get_download_by_app(app_id, str(app_version))
    if row is None or (row.source or SOURCE_TORRENTS) != SOURCE_GHOSTESHOP:
        return False  # row deleted or claimed elsewhere - nothing to do
    if is_app_owned(app_id, app_version):
        update_download(row.id, status='completed', error=None, progress=100)
        return True
    target = rebuild_target_from_download(row)
    return download_target_ghosteshop(target, settings, existing_row=row)


# ---------------------------------------------------------------- torrents pass

def run_downloader_job(settings=None, progress=None):
    """One torrents pass: sync download rows against qBittorrent, compute the
    missing targets, and hand the best torrent for each to qBittorrent. The
    pass only talks to the Jackett/qBittorrent APIs - the disk writes happen
    in qBittorrent, outside ownfoil's I/O budget.

    The Ghost eShop source does not run through here; see
    prepare_ghosteshop_targets / download_ghosteshop_row."""
    settings = settings or get_settings()
    if not torrents_configured(settings):
        logger.info('Downloader source "torrents" not enabled/configured, skipping.')
        return
    logger.info('Starting downloader job (torrents)...')
    try:
        sync_downloads_status(settings)
        missing = get_missing_targets()
        logger.info(f'Downloader (torrents): {len(missing)} missing target(s).')
        added = 0
        added_cache = {}  # download url -> info_hash, so a bundle torrent is added once
        for i, target in enumerate(missing):
            existing = get_download_by_app(target.get('app_id'),
                                           str(target.get('app_version')))
            if existing is not None:
                if (existing.source or SOURCE_TORRENTS) != SOURCE_TORRENTS:
                    continue  # the other source owns this target
                if existing.status != 'failed':
                    continue
                # A failed row would otherwise block the target forever: content
                # appears on trackers (or FlareSolverr comes online) after the first
                # miss, so every pass re-searches failed targets from scratch.
                logger.info(f"[torrents] Retrying failed download for "
                            f"{target.get('app_id')} v{target.get('app_version')}.")
                delete_download(existing.id)
            try:
                if download_target_torrents(target, settings, added_cache):
                    added += 1
            except Exception as e:
                logger.error(f"[torrents] Error processing {target.get('app_id')}: {e}")
            if progress:
                progress(int((i + 1) * 100 / max(1, len(missing))))
        sync_downloads_status(settings)
        logger.info(f'Downloader job (torrents) done. Added {len(added_cache)} '
                    f'torrent(s) covering {added} target(s).')
    except Exception as e:
        logger.error(f'Downloader job (torrents) failed: {e}')


def retry_download(download_id, settings):
    d = get_download_by_id(download_id)
    if not d:
        return False, 'Download not found'
    if is_app_owned(d.app_id, d.app_version):
        update_download(d.id, status='completed', error=None, progress=100)
        return True, 'Already owned'
    target = rebuild_target_from_download(d)
    if (d.source or SOURCE_TORRENTS) == SOURCE_GHOSTESHOP:
        ok = download_target_ghosteshop(target, settings)
        return ok, ('Re-downloaded' if ok else 'Ghost eShop download failed')
    delete_download(download_id)
    ok = download_target_torrents(target, settings)
    return ok, ('Re-searched' if ok else 'No match found')


def _serialize_download(d):
    row = to_dict(d)
    for key in ('created_at', 'updated_at'):
        val = row.get(key)
        if hasattr(val, 'isoformat'):
            row[key] = val.isoformat()
    return row


def get_downloads_status():
    return [_serialize_download(d) for d in get_all_downloads()]
