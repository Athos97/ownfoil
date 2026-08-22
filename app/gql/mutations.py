"""Mutation root: library-domain writes.

Scope is deliberately narrow. Settings, users and the keys upload stay on REST -
they are form-and-file shaped, not graph shaped. What lives here is everything a
library page needs to act on what it is displaying: enqueue work, cancel work, and
edit title metadata.

Two conventions differ from the query side, both on purpose:

- **Denial raises.** Queries return `None` for a field a role cannot read, which is
  the right shape for a partial result. A write that is silently ignored is not a
  partial result, it is a lie, so these raise instead.
- **Nothing is cached.** `view.graphql_dispatch` skips the ETag and the 304 path
  entirely for mutations - see `is_mutation` there.

Every resolver delegates; no business logic lives in this module.
"""
from typing import Optional

import strawberry
from strawberry.types import Info
from typing_extensions import Annotated

from constants import COMPRESS_EXT

from .docs import described, described_mutation
from .resolvers import resolve_task, resolve_title
from .types import (
    AppType, Download, DownloadSource, DownloadStatus, QueuedDownloadInput,
    Task, Title,
)


class NotAuthorized(Exception):
    """Raised when a role may not perform a write. Surfaces as a GraphQL error."""


class MutationFailed(Exception):
    """A write that was refused on its merits (unknown task, wrong file state)."""


def _require_admin(ctx) -> None:
    if not ctx.can_admin:
        raise NotAuthorized("Admin access is required for this operation.")


def _download_row(row) -> Download:
    """A downloads-table row as the Download type, shared by the row-returning
    download mutations so they all hydrate exactly like retryDownload."""
    from .resolvers import _iso, _version_or_zero
    try:
        source = DownloadSource(row.source) if row.source else None
    except ValueError:
        source = DownloadSource.TORRENTS
    return Download(
        id=strawberry.ID(str(row.id)),
        title_id=row.title_id or "",
        app_id=row.app_id or "",
        app_version=_version_or_zero(row.app_version),
        app_type=AppType(row.app_type) if row.app_type in ('BASE', 'UPDATE', 'DLC')
                 else AppType.UPDATE,
        name=row.name,
        search_query=row.search_query,
        torrent_hash=row.torrent_hash,
        torrent_name=row.torrent_name,
        indexer=row.indexer,
        size=row.size,
        seeders=row.seeders,
        source=source,
        progress=row.progress,
        status=DownloadStatus(row.status or 'queued'),
        error=row.error,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _task_by_id(task_id, info) -> Optional[Task]:
    """Re-read a task through the query resolver so a mutation returns exactly what
    `task(id:)` would - one shape for a task, however the client got there."""
    return resolve_task(str(task_id), info.context, info)


@described(strawberry.type)
class Mutation:
    """Library-domain writes: enqueue work, cancel work, edit title metadata.

    Deliberately narrow - settings, users and the keys upload stay on REST, being
    form-and-file shaped rather than graph shaped. Unlike the query side, a write a
    role may not perform raises rather than returning null: a silently ignored write
    is not a partial result, it is a lie. All of these require admin."""

    @described_mutation
    def enqueue_task(
        self, info: Info,
        name: Annotated[str, strawberry.argument(
            description="A registered task name, e.g. `process_library`. An "
                        "unknown name is refused.")],
        input: Annotated[Optional[str], strawberry.argument(
            description="The task's arguments as a JSON object string. Omit for a "
                        "task that takes none.")] = None,
    ) -> Optional[Task]:
        """Enqueue any registered task. `input` is a JSON object string, because the
        payload shape differs per task name. Enqueuing a duplicate returns the
        existing task rather than creating a second one."""
        import json
        import tasks as tasks_mod
        _require_admin(info.context)
        try:
            payload = json.loads(input) if input else {}
        except ValueError as e:
            raise MutationFailed(f"input is not valid JSON: {e}")
        if not isinstance(payload, dict):
            raise MutationFailed("input must be a JSON object")
        try:
            task, _created = tasks_mod.enqueue_task(name, payload)
        except ValueError as e:
            raise MutationFailed(str(e))
        return _task_by_id(task.id, info)

    @described_mutation
    def cancel_task(
        self, info: Info,
        id: Annotated[strawberry.ID, strawberry.argument(
            description="Primary key of the task to cancel.")],
    ) -> bool:
        """False when the task is unknown or already in a terminal state."""
        import tasks as tasks_mod
        _require_admin(info.context)
        return bool(tasks_mod.cancel_task(int(id)))

    @described_mutation
    def dismiss_task(
        self, info: Info,
        id: Annotated[strawberry.ID, strawberry.argument(
            description="Primary key of the failed task to remove.")],
    ) -> bool:
        """Clear one failed task. Failed tasks are kept so a failure survives a page
        reload, so this is how a task queue gets tidied once its failures have been
        read. False when the task is unknown or has not failed - a running or queued
        task is `cancelTask`'s job, not this one."""
        import tasks as tasks_mod
        _require_admin(info.context)
        return bool(tasks_mod.dismiss_task(int(id)))

    @described_mutation
    def purge_failed_tasks(self, info: Info) -> int:
        """Clear every failed task at once, returning how many were removed. Zero when
        there was nothing to clear, which is not an error."""
        import tasks as tasks_mod
        _require_admin(info.context)
        return tasks_mod.purge_failed_tasks()

    @described_mutation
    def scan_library(
        self, info: Info,
        path: Annotated[Optional[str], strawberry.argument(
            description="Absolute path of one configured library root. Omit to scan "
                        "every configured root.")] = None,
    ) -> Optional[Task]:
        """Scan one library, or every configured library when `path` is omitted. The
        all-libraries form returns the last task enqueued."""
        import tasks as tasks_mod
        from db import get_libraries
        _require_admin(info.context)
        if path:
            task, _ = tasks_mod.enqueue_task('scan_library', {'library_path': path})
            return _task_by_id(task.id, info)
        last = None
        for lib in get_libraries():
            last, _ = tasks_mod.enqueue_task('scan_library', {'library_path': lib.path})
        return _task_by_id(last.id, info) if last else None

    @described_mutation
    def compress_file(
        self, info: Info,
        file_id: Annotated[strawberry.ID, strawberry.argument(
            description="Primary key of the file to compress.")],
    ) -> Optional[Task]:
        """Compress one file to NSZ/XCZ. Same guards as the REST endpoint."""
        import tasks as tasks_mod
        from db import Files, db
        _require_admin(info.context)
        file = db.session.get(Files, int(file_id))
        if not file:
            raise MutationFailed("File not found")
        if file.compressed:
            raise MutationFailed("File is already compressed")
        if file.extension not in COMPRESS_EXT:
            raise MutationFailed("File type cannot be compressed")
        task, _ = tasks_mod.enqueue_task('compress_file', {'file_id': int(file_id)})
        return _task_by_id(task.id, info)

    @described_mutation
    def decompress_file(
        self, info: Info,
        file_id: Annotated[strawberry.ID, strawberry.argument(
            description="Primary key of the file to decompress.")],
    ) -> Optional[Task]:
        """Decompress one file back to NSP/XCI."""
        import tasks as tasks_mod
        from db import Files, db
        _require_admin(info.context)
        file = db.session.get(Files, int(file_id))
        if not file:
            raise MutationFailed("File not found")
        if not file.compressed:
            raise MutationFailed("File is not compressed")
        task, _ = tasks_mod.enqueue_task('decompress_file', {'file_id': int(file_id)})
        return _task_by_id(task.id, info)

    @described_mutation
    def verify_file(
        self, info: Info,
        file_id: Annotated[strawberry.ID, strawberry.argument(
            description="Primary key of the file to verify.")],
    ) -> Optional[Task]:
        """Re-verify one file at the configured depth. The stored verdicts are cleared
        first, so this re-checks a file that already has them rather than no-opping."""
        import tasks as tasks_mod
        from containers import verification as verification_lib
        from db import Files, db, reset_file_verification
        _require_admin(info.context)
        file = db.session.get(Files, int(file_id))
        if not file:
            raise MutationFailed("File not found")
        if file.extension not in verification_lib.VERIFY_EXT:
            raise MutationFailed("File type cannot be verified")
        reset_file_verification(file)
        db.session.commit()
        task, _ = tasks_mod.enqueue_task('verify_file', {'file_id': int(file_id)})
        return _task_by_id(task.id, info)

    @described_mutation
    def set_title_override(
        self, info: Info,
        title_id: Annotated[strawberry.ID, strawberry.argument(
            description="The 16-hex-digit title id to override.")],
        record: Annotated[str, strawberry.argument(
            description="A JSON object of metadata fields to override. Fields it "
                        "omits keep their downloaded values.")],
    ) -> Optional[Title]:
        """Write user-authored metadata for a title, winning over the downloaded
        titledb values field by field. `record` is a JSON object of the same shape the
        REST endpoint takes. Re-identification is enqueued, as there too."""
        import json
        import tasks as tasks_mod
        import titledb
        _require_admin(info.context)
        try:
            payload = json.loads(record)
        except ValueError as e:
            raise MutationFailed(f"record is not valid JSON: {e}")
        if not isinstance(payload, dict):
            raise MutationFailed("record must be a JSON object")
        ok, err = titledb.store.set_override(str(title_id), payload)
        if not ok:
            raise MutationFailed(err)
        tasks_mod.enqueue_task('process_library')
        return resolve_title(str(title_id), info.context, info)

    @described_mutation
    def delete_title_override(
        self, info: Info,
        title_id: Annotated[strawberry.ID, strawberry.argument(
            description="The 16-hex-digit title id whose override to drop.")],
    ) -> bool:
        """Drop the override, restoring the next metadata source down."""
        import titledb
        _require_admin(info.context)
        ok, _err = titledb.store.delete_override(str(title_id))
        return bool(ok)

    @described_mutation
    def blacklist_app(
        self, info: Info,
        app_id: Annotated[strawberry.ID, strawberry.argument(
            description="The 16-hex-digit application id to blacklist (case-insensitive, "
                        "0x prefix tolerated).")],
        note: Annotated[Optional[str], strawberry.argument(
            description="Why - free-form, shown next to the entry. Null keeps the "
                        "existing note on re-blacklisting.")] = None,
    ) -> bool:
        """Add (or annotate) one app id on the blacklist. Blacklisted content stops
        counting against its title's complete/up-to-date flags and is never a
        download target; every affected title's flags are recomputed right away."""
        from db import upsert_blacklisted_app
        import tasks as tasks_mod
        _require_admin(info.context)
        ok, err = upsert_blacklisted_app(str(app_id), note)
        if not ok:
            raise MutationFailed(err)
        tasks_mod.enqueue_task('update_titles')
        return True

    @described_mutation
    def unblacklist_app(
        self, info: Info,
        app_id: Annotated[strawberry.ID, strawberry.argument(
            description="The 16-hex-digit application id to take off the blacklist.")],
    ) -> bool:
        """Remove one app id from the blacklist, so its content counts as missing
        again. False when it was not blacklisted; flags are recomputed either way
        the entry existed."""
        from db import delete_blacklisted_app
        import tasks as tasks_mod
        _require_admin(info.context)
        if not delete_blacklisted_app(str(app_id)):
            return False
        tasks_mod.enqueue_task('update_titles')
        return True

    @described_mutation
    def pause_download(
        self, info: Info,
        id: Annotated[strawberry.ID, strawberry.argument(
            description="Primary key of the queued or downloading row to pause.")],
    ) -> Download:
        """Halt one unfinished download. A Ghost eShop transfer is cancelled (its
        partial file is kept for resuming) and the row reads `paused`; a torrent
        is paused in qBittorrent. Resuming is `resumeDownload`."""
        import downloader as downloader_lib
        from db import get_download_by_id
        _require_admin(info.context)
        ok, msg = downloader_lib.pause_download(int(id))
        if not ok:
            raise MutationFailed(msg)
        row = get_download_by_id(int(id))
        return _download_row(row) if row else None

    @described_mutation
    def resume_download(
        self, info: Info,
        id: Annotated[strawberry.ID, strawberry.argument(
            description="Primary key of the paused row to resume.")],
    ) -> Download:
        """Resume a paused download. A Ghost eShop row is requeued and picks up
        from its partial file when still valid (a stale token restarts the
        transfer from scratch); a torrent resumes in qBittorrent."""
        import downloader as downloader_lib
        from db import get_download_by_id
        _require_admin(info.context)
        ok, msg = downloader_lib.resume_download(int(id))
        if not ok:
            raise MutationFailed(msg)
        row = get_download_by_id(int(id))
        return _download_row(row) if row else None

    @described_mutation
    def pause_all_downloads(self, info: Info) -> int:
        """Pause every unfinished download - queued and transferring, both
        sources. Rows stay (pausing is reversible; resume is one click per row);
        nothing is deleted. Returns how many were paused."""
        import downloader as downloader_lib
        _require_admin(info.context)
        return downloader_lib.pause_all_downloads()

    @described_mutation
    def delete_completed_downloads(self, info: Info) -> int:
        """Remove the finished rows from the downloads log. Completed only:
        failed rows stay retryable and visible until dismissed. Returns how
        many went."""
        import downloader as downloader_lib
        _require_admin(info.context)
        return downloader_lib.delete_completed_downloads()

    @described_mutation
    def run_downloader(
        self, info: Info,
        source: Annotated[DownloadSource, strawberry.argument(
            description="Which source to run: torrents (Jackett + qBittorrent) or "
                        "ghosteshop (direct HTTP from Ghost eShop PRO).")],
    ) -> Optional[Task]:
        """Run one download source now: sync download statuses, compute missing
        updates/DLCs, and fetch each through that source - torrents by handing the
        best match to qBittorrent, Ghost eShop by direct chunked download into the
        game's folder. Queued Add Content rows of that source are processed first.
        A no-op when the source is not configured; progress and cancellation behave
        like any other task. Runs independently of the periodic schedule - it does
        not reset or wait for the next scheduled pass."""
        import tasks as tasks_mod
        _require_admin(info.context)
        task_names = {
            DownloadSource.TORRENTS: 'downloader_torrents_run',
            DownloadSource.GHOSTESHOP: 'downloader_ghosteshop_run',
        }
        # Distinct input from the scheduled row ({}), or the dedup in enqueue_task
        # would return that deferred row instead of starting work now.
        task, _created = tasks_mod.enqueue_task(
            task_names[source], {'manual': True})
        return _task_by_id(task.id, info)

    @described_mutation
    def queue_ghosteshop_downloads(
        self, info: Info,
        entries: Annotated[list[QueuedDownloadInput], strawberry.argument(
            description="The catalog entries chosen in Add Content. Bases, updates "
                        "and DLCs alike: queued rows are downloaded whatever their "
                        "type, unlike the periodic scan that only looks for missing "
                        "updates/DLCs.")],
    ) -> int:
        """Queue catalog entries for Ghost eShop and start them right away.

        Only the chosen entries download - one task per file, no library sweep;
        missing content stays the periodic pass's (or Update Library's) job.
        Returns the number of rows now queued - entries already owned (any
        version) or already queued are skipped."""
        import tasks as tasks_mod
        import downloader as downloader_lib
        from db import get_download_by_app, is_app_id_owned
        _require_admin(info.context)
        queued = 0
        for e in entries:
            if is_app_id_owned(e.app_id):
                continue  # already in the library (any version): not addable content
            if get_download_by_app(e.app_id, str(e.app_version)) is not None:
                continue
            downloader_lib.queue_ghosteshop_download(
                title_id=e.title_id, app_id=e.app_id,
                app_version=e.app_version, app_type=e.app_type.value,
                name=e.name)
            # Direct per-file task, matching the pass's child shape - never a
            # full pass, which would also sweep every missing target the user
            # did not ask for.
            tasks_mod.enqueue_task(downloader_lib.GHOSTESHOP_DOWNLOAD_TASK,
                                   {'app_id': e.app_id,
                                    'app_version': str(e.app_version),
                                    'name': e.name})
            queued += 1
        return queued

    @described_mutation
    def add_torrent(
        self, info: Info,
        download_url: Annotated[str, strawberry.argument(
            description="Magnet URI or .torrent URL, exactly as the search result "
                        "reported it.")],
    ) -> bool:
        """Hand one torrent to qBittorrent with the torrents source's save path and
        category. The manual lane of Add Content: no download row is created, the
        library picks the file up through the watcher like any other. False when
        qBittorrent is not configured or rejected the torrent."""
        import qbittorrent
        from settings import get_settings
        _require_admin(info.context)
        torrents = get_settings().get('downloader', {}).get('torrents', {}) or {}
        qbt_settings = torrents.get('qbittorrent', {}) or {}
        if not qbt_settings.get('url'):
            raise MutationFailed('qBittorrent is not configured.')
        client = qbittorrent.QbittorrentClient(qbt_settings)
        ok, err = client.login()
        if not ok:
            raise MutationFailed(f'qBittorrent login failed: {err}')
        ok, add_err, _hash = client.add_torrent(
            download_url,
            save_path=qbt_settings.get('save_path') or None,
            category=qbt_settings.get('category') or None,
        )
        if not ok:
            raise MutationFailed(add_err or 'qBittorrent rejected the torrent.')
        return True

    @described_mutation
    def retry_download(
        self, info: Info,
        id: Annotated[strawberry.ID, strawberry.argument(
            description="Primary key of the failed download to retry.")],
    ) -> Optional[Download]:
        """Re-run a failed download from scratch through its own source: a torrents
        row is re-searched on Jackett and handed to qBittorrent, a Ghost eShop row
        goes back to queued and downloads as its own task (never inside this
        request). The old row keeps its place - one row per (app id, version)
        target. Null when the row vanished mid-retry."""
        import tasks as tasks_mod
        from db import get_download_by_app, get_download_by_id
        from settings import get_settings
        import downloader as downloader_lib
        _require_admin(info.context)
        download = get_download_by_id(int(id))
        if not download:
            raise MutationFailed("Download not found")
        app_id, app_version = download.app_id, str(download.app_version)
        ok, _msg = downloader_lib.retry_download(int(id), get_settings())
        if not ok:
            raise MutationFailed("Retry found nothing downloadable")
        row = get_download_by_app(app_id, app_version)
        if row is None:
            return None
        return _download_row(row)

    @described_mutation
    def delete_download(
        self, info: Info,
        id: Annotated[strawberry.ID, strawberry.argument(
            description="Primary key of the download row to remove.")],
    ) -> bool:
        """Remove a download row and everything it fetched so far: for Ghost eShop
        rows the partial `.part` files are deleted from disk along with the row.
        qBittorrent keeps its own torrents and data. False when there was nothing
        to remove."""
        import downloader as downloader_lib
        _require_admin(info.context)
        return downloader_lib.delete_download_row(int(id))
