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
# qBittorrent <5 says pausedDL/pausedUP; 5+ says stoppedDL/stoppedUP.
QB_PAUSED_STATES = ('pausedDL', 'pausedUP', 'stoppedDL', 'stoppedUP')

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
    if d.app_type == APP_TYPE_UPD:
        name = (titles_lib.get_game_info(d.title_id) or {}).get('name')
    else:
        name = (titles_lib.get_game_info(d.app_id) or {}).get('name')
    if not name or name == 'Unrecognized':
        # Titledb does not know it: the row's display name (the game's title
        # for Add Content picks) names the folder better than the placeholder.
        name = d.name or d.title_id
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

    info = titles_lib.get_game_info(target.get('title_id')) or {}
    base_name = info.get('name')
    if not base_name or base_name == 'Unrecognized':
        # Not in titledb: the queue row's display name (the game's title for
        # Add Content picks) beats dumping the file into an Unrecognized folder.
        base_name = target.get('name') or target.get('title_id') or 'unknown'
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


# How many progress callbacks may pass without the liveness re-check of the
# driving task row (each callback is one chunk-block, so ~4MB of transfer).
CANCEL_CHECK_EVERY = 16


class TransferCancelled(ghostshop.GhostshopError):
    """The task driving this transfer was cancelled - abort the transfer."""


def _make_progress_cb(row_id, task_id=None, task_progress=None):
    """Throttled DB updates of the download row's progress percentage.

    With a task_id, also watches for cooperative cancellation: a cancelled task
    row disappears from the table, and noticing that aborts the transfer mid-
    stream instead of letting it run to completion orphaned.

    With task_progress (typically tasks._task_progress(task_id)), the same
    percentage is mirrored onto the driving Task row's completion_pct, so the
    Tasks page shows real transfer progress instead of sitting at 0% (rendered
    as a fake animated bar) for the whole download."""
    last = [0.0]
    ticks = [0]

    def cb(done, total):
        now = time.monotonic()
        if total and done < total and (now - last[0]) < PROGRESS_WRITE_INTERVAL:
            ticks[0] += 1
            if task_id is not None and ticks[0] % CANCEL_CHECK_EVERY == 0:
                _raise_if_task_cancelled(task_id)
            return
        last[0] = now
        ticks[0] += 1
        if task_id is not None and ticks[0] % CANCEL_CHECK_EVERY == 0:
            _raise_if_task_cancelled(task_id)
        pct = int(done * 100 / total) if total else 0
        update_download(row_id, progress=pct)
        if task_progress is not None:
            task_progress(pct)

    return cb


def _raise_if_task_cancelled(task_id):
    """Cheap liveness probe: the cancel path deletes the row outright."""
    from sqlalchemy import text as _text
    from db import db as _db
    row = _db.session.execute(
        _text("SELECT 1 FROM tasks WHERE id = :id AND status IN ('pending', 'running')"),
        {"id": task_id}).first()
    if row is None:
        raise TransferCancelled('cancelled by user')


def download_target_ghosteshop(target, settings, existing_row=None, task_id=None, progress=None):
    """Download one target straight from Ghost eShop into its game folder.

    With a task_id, the transfer aborts cooperatively when that task row is
    cancelled (deleted) mid-stream. With progress, the driving task's
    completion_pct is kept in sync with the actual transfer, not just the
    Download row's."""
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
            update_download(row.id, status='completed', error=CLEAR_ERROR, progress=100)
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

    # titledb can know a newer version than the catalog carries (its data comes
    # from a different upstream), so the entry fetched may not be the version
    # asked for. Completion is ownership-based: the row must track the bytes
    # actually fetched, or it can never flip to completed.
    requested_ver = str(target.get('app_version') or 0)
    entry_ver = str(entry.version or 0)
    if entry_ver not in (requested_ver, '0'):
        if is_app_owned(target.get('app_id'), entry_ver):
            # The catalog's best is already in the library: nothing to fetch -
            # complete the requested row instead of re-downloading every pass.
            row = add_download(**common, status='downloading', progress=0)
            update_download(row.id, torrent_name=entry.name, indexer='Ghost eShop',
                            size=entry.size, status='completed', progress=100,
                            error=f'Catalog best is v{entry.version}; already owned')
            logger.info(f"[ghosteshop] {target.get('app_id')}: catalog best "
                        f"v{entry.version} already owned - nothing to do.")
            return True
        logger.info(f"[ghosteshop] {target.get('app_id')}: requested v{requested_ver} "
                    f"not in catalog, fetching best available v{entry.version}.")

    row = add_download(**common, status='downloading', progress=0)
    if entry_ver not in (requested_ver, '0'):
        # Re-point the row at the version being fetched; one row per target.
        existing = get_download_by_app(target.get('app_id'), entry_ver)
        if existing is not None and existing.id != row.id:
            update_download(existing.id, status='downloading', progress=0)
            delete_download(row.id)
            row = existing
        else:
            update_download(row.id, app_version=entry_ver)
    # add_download returns an existing row untouched, so (re)apply the live fields.
    update_download(row.id, torrent_name=entry.name, indexer='Ghost eShop',
                    size=entry.size, seeders=None, status='downloading',
                    error=CLEAR_ERROR, progress=0)

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
            on_progress=_make_progress_cb(row.id, task_id=task_id, task_progress=progress))
        provider.download_complete(session, link)
        update_download(row.id, progress=100)
        logger.info(f"[ghosteshop] Downloaded {entry.name}")
        return True
    except TransferCancelled:
        # Cooperative abort. 'paused' was written before cancelling (that is how
        # pausing works) - keep it; any other cancellation goes back to queued
        # for the next pass, the .part surviving for a cheap resume either way.
        logger.info(f"[ghosteshop] Transfer of {entry.name} cancelled.")
        db.session.refresh(row)
        if row.status != 'paused':
            update_download(row.id, status='queued', progress=0,
                            error='Cancelled - will resume on the next pass')
        return False
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
    """Reconcile queued/downloading rows against qBittorrent and app ownership.

    Paused rows are deliberately untouched: pausing is a user decision, and
    reconciling them back to 'downloading' (or into a pass) would undo it. The
    one exception runs the other way - a torrent paused from qBittorrent's own
    UI flips our row to 'paused' so the two views agree."""
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

    # Also reconcile paused torrent rows: qBittorrent-side pauses surface here.
    paused_torrent_rows = [d for d in Download.query.filter_by(status='paused').all()
                           if (d.source or SOURCE_TORRENTS) == SOURCE_TORRENTS]
    if paused_torrent_rows and not needs_qbt:
        needs_qbt = True
        client = qbittorrent.QbittorrentClient(qbt_settings)
        ok, _ = client.login()
        if ok:
            for t in client.get_torrents(category=qbt_settings.get('category')):
                qb_states[(t.get('hash') or '').lower()] = t

    for d in in_progress:
        if is_app_owned(d.app_id, d.app_version):
            update_download(d.id, status='completed', error=CLEAR_ERROR, progress=100)
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
            elif state in QB_PAUSED_STATES:
                update_download(d.id, status='paused')
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
        update_download(row_id, status='queued', error=CLEAR_ERROR, progress=0)
        logger.info(f'[ghosteshop] Requeued orphaned download row {row_id} '
                    '(no live task for it).')


def _gc_orphan_part_files():
    """Delete .part/.part.state files whose download row is gone or completed:
    the resume machinery leaves them behind when a target is deleted, fails
    permanently, or completes by other means."""
    from pathlib import Path as _Path
    roots = {os.path.dirname(p) or '/' for p in _ghost_library_roots()}
    live_keys = set()
    for d in get_all_downloads():
        if d.source == SOURCE_GHOSTESHOP and d.status in ('queued', 'downloading'):
            live_keys.add(d.torrent_name or '')
    removed = 0
    for root in roots:
        try:
            candidates = list(_Path(root).rglob('*.part'))[:500]
        except OSError:
            continue
        for part in candidates:
            stem = part.name[:-len('.part')]
            if stem in live_keys:
                continue
            # Only Ghost eShop residue: its names are catalog file names.
            row = Download.query.filter_by(torrent_name=stem).first()
            if row is None or row.status in ('completed', 'failed'):
                try:
                    part.unlink(missing_ok=True)
                    part.with_name(part.name + '.state').unlink(missing_ok=True)
                    removed += 1
                except OSError as e:
                    logger.debug(f'[ghosteshop] Could not remove stale {part}: {e}')
    if removed:
        logger.info(f'[ghosteshop] Removed {removed} orphaned .part file(s).')


def _ghost_library_roots():
    settings = get_settings()
    path = _ghost_library_path(settings)
    return [path] if path else get_library_paths()


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
    _gc_orphan_part_files()
    try:
        from db import purge_stale_events
        purge_stale_events()
    except Exception as e:
        logger.debug(f'Stale event purge skipped: {e}')

    targets = []
    # Explicit queued rows first (Add Content) - bases included.
    for d in get_downloads_in_progress():
        if d.source == SOURCE_GHOSTESHOP and d.status == 'queued':
            targets.append({'app_id': d.app_id,
                            'app_version': str(d.app_version),
                            'name': d.name or d.app_id,
                            'title_id': d.title_id,
                            'app_type': d.app_type})
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
                        'name': target.get('name') or app_id,
                        'title_id': target.get('title_id'),
                        'app_type': target.get('app_type')})

    seen = set()
    out = []
    for t in targets:
        key = (t['app_id'], t['app_version'])
        if key not in seen:
            seen.add(key)
            out.append(t)
    logger.info(f'Ghost eShop pass: {len(out)} item(s) to download.')
    return out


def download_ghosteshop_row(app_id, app_version, name=None, title_id=None,
                            app_type=None, settings=None, task_id=None, progress=None):
    """Download one target via Ghost eShop - the body of the per-file io task.

    The downloads row drives the flow: a foreign-lane row is a no-op, an owned
    target flips to completed, and a computed missing target that has no row
    yet gets one here (only Add Content queues rows upfront)."""
    settings = settings or get_settings()
    row = get_download_by_app(app_id, str(app_version))
    if row is not None and (row.source or SOURCE_TORRENTS) != SOURCE_GHOSTESHOP:
        return False  # claimed by the other lane - nothing to do
    if row is None:
        # Computed missing target: infer the family fields when the pass did
        # not hand them over (e.g. a directly enqueued task).
        if not title_id or not app_type:
            suffix = (app_id or '')[-3:]
            app_type = (APP_TYPE_BASE if suffix == '000'
                        else APP_TYPE_UPD if suffix == '800' else APP_TYPE_DLC)
            base = app_id[:-3]
            title_id = (base + '000' if app_type != APP_TYPE_DLC else
                        f'{int(base, 16) - 1:013X}000')
        if not name:
            # Prefer titledb's name over the raw app id for the row's display.
            info = titles_lib.get_game_info(
                app_id if app_type == APP_TYPE_DLC else title_id) or {}
            titledb_name = info.get('name')
            if titledb_name and titledb_name != 'Unrecognized':
                name = titledb_name
        row = add_download(title_id=title_id, app_id=app_id,
                           app_version=str(app_version), app_type=app_type,
                           name=name or app_id, source=SOURCE_GHOSTESHOP,
                           status='downloading', progress=0)
    if is_app_owned(app_id, app_version):
        update_download(row.id, status='completed', error=CLEAR_ERROR, progress=100)
        return True
    target = rebuild_target_from_download(row)
    return download_target_ghosteshop(target, settings, existing_row=row,
                                      task_id=task_id, progress=progress)


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


def _live_ghost_task_id(app_id, app_version):
    """The pending/running per-file task driving this target, if any."""
    from db import Task
    return Task.query.filter(
        Task.task_name == GHOSTESHOP_DOWNLOAD_TASK,
        Task.status.in_(('pending', 'running')),
        Task.input_json.contains(f'"{app_id}"'),
    ).first()


def pause_download(download_id, settings=None):
    """Pause one unfinished download (queued or transferring). Returns (ok, msg).

    Ghost: the row flips to 'paused' FIRST, then its task is cancelled - the
    cooperative abort stops the transfer and both the cancellation handler and
    the cleanup hook leave an already-paused row alone, keeping the .part for a
    cheap resume. Torrents: qBittorrent pauses the torrent; if it is already
    gone from the client, the row fails with the reason."""
    settings = settings or get_settings()
    d = get_download_by_id(download_id)
    if d is None:
        return False, 'Download not found'
    if d.status not in ('queued', 'downloading'):
        return False, f'Cannot pause a {d.status} download'

    if (d.source or SOURCE_TORRENTS) == SOURCE_GHOSTESHOP:
        update_download(d.id, status='paused')
        task = _live_ghost_task_id(d.app_id, str(d.app_version))
        if task is not None:
            import tasks as tasks_mod
            try:
                tasks_mod.cancel_task(task.id)  # runs the cleanup hook, which
                # skips rows already paused
            except Exception as e:
                logger.warning(f"[pause] Cancelling task {task.id} failed: {e}")
        logger.info(f"[pause] {d.app_id} v{d.app_version} paused "
                    f"({'task cancelled' if task else 'was not running'}).")
        return True, 'Paused'

    # Torrents lane
    qbt_settings = ((settings.get('downloader', {}) or {}).get('torrents', {})
                    or {}).get('qbittorrent', {}) or {}
    client = qbittorrent.QbittorrentClient(qbt_settings)
    ok, err = client.login()
    if not ok:
        update_download(d.id, status='failed', error=f'qBittorrent: {err}')
        return False, err
    info_hash = d.torrent_hash or client.find_hash_by_name(d.torrent_name,
                                                           qbt_settings.get('category'))
    if info_hash:
        d.torrent_hash = info_hash
        db.session.commit()
    else:
        reason = 'Torrent no longer in qBittorrent - retry to re-search'
        update_download(d.id, status='failed', error=reason)
        return False, reason
    ok, err = client.pause_torrent(info_hash)
    if not ok:
        return False, err
    update_download(d.id, status='paused')
    logger.info(f"[pause] torrent {info_hash} paused.")
    return True, 'Paused'


def resume_download(download_id, settings=None):
    """Resume a paused download. Returns (ok, msg).

    Ghost: back to queued with a fresh per-file task - the existing chunk
    resume continues from the .part when its leading chunks still match, and
    restarts from scratch when they do not (expired token or changed plan).
    Torrents: qBittorrent resumes; a torrent that vanished fails the row with
    a reason (retry re-searches)."""
    settings = settings or get_settings()
    d = get_download_by_id(download_id)
    if d is None:
        return False, 'Download not found'
    if d.status != 'paused':
        return False, f'Cannot resume a {d.status} download'

    if (d.source or SOURCE_TORRENTS) == SOURCE_GHOSTESHOP:
        update_download(d.id, status='queued', progress=0, error=CLEAR_ERROR)
        import tasks as tasks_mod
        tasks_mod.enqueue_task(GHOSTESHOP_DOWNLOAD_TASK, {
            'app_id': d.app_id, 'app_version': str(d.app_version),
            'name': d.name, 'title_id': d.title_id, 'app_type': d.app_type})
        logger.info(f"[resume] {d.app_id} v{d.app_version} requeued.")
        return True, 'Resumed'

    qbt_settings = ((settings.get('downloader', {}) or {}).get('torrents', {})
                    or {}).get('qbittorrent', {}) or {}
    client = qbittorrent.QbittorrentClient(qbt_settings)
    ok, err = client.login()
    if not ok:
        return False, f'qBittorrent: {err}'
    info_hash = d.torrent_hash or client.find_hash_by_name(d.torrent_name,
                                                           qbt_settings.get('category'))
    if not info_hash:
        reason = 'Torrent no longer in qBittorrent - retry to re-search'
        update_download(d.id, status='failed', error=reason)
        return False, reason
    ok, err = client.resume_torrent(info_hash)
    if not ok:
        return False, err
    update_download(d.id, status='downloading')
    logger.info(f"[resume] torrent {info_hash} resumed.")
    return True, 'Resumed'


def pause_all_downloads():
    """Pause every unfinished download (queued and transferring). Rows stay -
    pausable is reversible; resuming is one click per row. Returns how many
    were paused."""
    paused = 0
    for d in Download.query.filter(Download.status.in_(['queued', 'downloading'])).all():
        try:
            ok, _msg = pause_download(d.id)
            paused += 1 if ok else 0
        except Exception as e:
            logger.warning(f"[pause-all] {d.app_id}: {e}")
    logger.info(f"[pause-all] Paused {paused} download(s).")
    return paused


def delete_completed_downloads():
    """Remove finished rows (completed only). Failed rows stay: they are
    retryable and the log of what went wrong. Returns how many went."""
    removed = Download.query.filter_by(status='completed').delete()
    db.session.commit()
    if removed:
        logger.info(f"[downloads] Deleted {removed} completed row(s).")
    return removed


def _delete_ghost_parts_for(d):
    """Remove this row's .part/.part.state files from the ghost library roots.

    Names are matched by exact comparison, not by glob pattern: catalog names
    carry glob-special characters ([titleId][version]) that a pattern match
    would misinterpret. Returns how many files went."""
    if (d.source or SOURCE_TORRENTS) != SOURCE_GHOSTESHOP or not d.torrent_name:
        return 0
    from pathlib import Path as _Path
    wanted = {d.torrent_name + '.part', d.torrent_name + '.part.state'}
    removed = 0
    for root in _ghost_library_roots():
        try:
            candidates = list(_Path(root).rglob('*.part*'))[:500]
        except OSError:
            continue
        for path in candidates:
            if path.name in wanted:
                try:
                    path.unlink(missing_ok=True)
                    removed += 1
                except OSError as e:
                    logger.warning(f"[downloads] Could not remove {path}: {e}")
    return removed


def delete_download_row(download_id):
    """Delete one downloads row - and, for Ghost eShop rows, the partial files
    it left on disk. A trashed download must not keep its half-fetched bytes
    (the orphan GC would only catch them at the next pass, a day out).

    qBittorrent keeps its own torrents and data: its queue is not ours to wipe."""
    d = get_download_by_id(download_id)
    if d is None:
        return False
    parts = _delete_ghost_parts_for(d)
    db.session.delete(d)
    db.session.commit()
    if parts:
        logger.info(f"[downloads] Deleted row {download_id} with {parts} "
                    "partial file(s) swept from disk.")
    return True


def retry_download(download_id, settings):
    """Re-run a failed download from scratch through its own source.

    Ghost eShop rows go back to queued and download as their own io task (the
    transfer must not run inside the caller's request thread); torrents rows
    are re-searched and handed to qBittorrent right away, as before."""
    d = get_download_by_id(download_id)
    if not d:
        return False, 'Download not found'
    if is_app_owned(d.app_id, d.app_version):
        update_download(d.id, status='completed', error=CLEAR_ERROR, progress=100)
        return True, 'Already owned'
    if (d.source or SOURCE_TORRENTS) == SOURCE_GHOSTESHOP:
        update_download(d.id, status='queued', progress=0, error=CLEAR_ERROR)
        import tasks as tasks_mod
        tasks_mod.enqueue_task(GHOSTESHOP_DOWNLOAD_TASK, {
            'app_id': d.app_id, 'app_version': str(d.app_version),
            'name': d.name, 'title_id': d.title_id, 'app_type': d.app_type})
        return True, 'Requeued'
    target = rebuild_target_from_download(d)
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
