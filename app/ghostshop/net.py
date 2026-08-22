"""Chunked HTTP downloads with cross-run resume.

Ported from switch-library-updater's src/net.py. Progress is reported through
an `on_progress(downloaded, total)` callback; resume state lives next to the
destination as `<name>.part` + `<name>.part.state`, so an interrupted download
continues on the next attempt even when the portal hands out a fresh token —
as long as the already-downloaded leading chunks still match by URL and size.
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import time
from pathlib import Path
from typing import Optional

import requests

from .types import GhostshopError

CHUNK_SIZE = 256 * 1024
RETRY_BACKOFF = (0.5, 1.5, 3.0)
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
CHUNK_ATTEMPTS = 4

# Head-room beyond the file size, so a nearly-full volume does not fail on the
# last block of metadata (state file, final rename).
DISK_HEADROOM = 64 * 1024 * 1024


class DiskFullError(GhostshopError):
    """No space for this file - not transient, retrying will not help."""


def _check_disk_space(destination: Path, needed: int):
    try:
        free = shutil.disk_usage(destination.parent).free
    except OSError:
        return  # cannot tell: proceed and let the write surface any problem
    if needed and free < needed + DISK_HEADROOM:
        raise DiskFullError(
            f'not enough disk space for {destination.name}: '
            f'{free // (1024 * 1024)} MiB free, {needed // (1024 * 1024)} MiB needed')


def _http_get_stream(session: requests.Session, url: str,
                     headers: Optional[dict] = None,
                     timeout: tuple = (10, 120)) -> requests.Response:
    """Streaming GET with retries on transient errors."""
    attempts = len(RETRY_BACKOFF) + 1
    last_error = None
    for attempt in range(attempts):
        try:
            response = session.get(url, headers=headers, timeout=timeout, stream=True)
            if response.status_code in RETRY_STATUS and attempt < attempts - 1:
                response.close()
                time.sleep(RETRY_BACKOFF[attempt])
                continue
            if response.status_code >= 400:
                code = response.status_code
                response.close()
                raise GhostshopError(f'HTTP {code} on chunk fetch')
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(RETRY_BACKOFF[attempt])
                continue
            raise GhostshopError(f'network error on chunk fetch: {exc}') from exc
    raise GhostshopError(f'exhausted retries on chunk fetch: {last_error}')


def _download_chunk_to(session, url, fh, offset, headers, on_progress) -> int:
    """Download one chunk into `fh` at `offset`, with retries. Returns bytes."""
    last_error = None
    for attempt in range(CHUNK_ATTEMPTS):
        got = 0
        try:
            response = _http_get_stream(session, url, headers=headers)
            with response:
                fh.seek(offset)
                for block in response.iter_content(chunk_size=CHUNK_SIZE):
                    if block:
                        fh.write(block)
                        got += len(block)
                        if on_progress:
                            on_progress(got)
            if got:
                return got
            raise GhostshopError('empty chunk')
        except DiskFullError:
            raise  # ENOSPC never resolves by retrying
        except (GhostshopError, requests.RequestException, OSError) as exc:
            if getattr(exc, 'errno', None) == errno.ENOSPC:
                raise DiskFullError(f'no space left while downloading: {exc}') from exc
            last_error = exc
            if attempt < CHUNK_ATTEMPTS - 1:
                time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
    raise GhostshopError(f'chunk failed after {CHUNK_ATTEMPTS} attempts ({last_error})')


def download_chunked(session: requests.Session, plan, destination: Path,
                     expected_size: int = 0,
                     headers: Optional[dict] = None,
                     on_progress=None) -> Path:
    """Download a chunked file per a DownloadInfo-like plan.

    `plan` is provider-agnostic: any object with `.chunks` (ordered list of
    {url, size}), `.file_name` and `.file_size`. Chunks are downloaded IN
    ORDER and concatenated into the `.part` file, whose `.part.state`
    companion tracks the chunks completed so far. `on_progress`, when given,
    is called as on_progress(downloaded_bytes, total_bytes) as the transfer
    advances.
    """
    chunks = getattr(plan, 'chunks', None) or []
    if not chunks:
        raise GhostshopError('no chunks in the download plan')
    try:
        total = int(getattr(plan, 'file_size', 0) or 0)
    except (TypeError, ValueError):
        total = 0
    total = total or expected_size or sum(int(c.get('size') or 0) for c in chunks)

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Before touching disk: a multi-GB .part on a nearly-full volume helps
    # nobody - fail fast with an actionable error instead.
    _check_disk_space(destination, total)
    partial_file = destination.with_name(destination.name + '.part')
    state_file = destination.with_name(destination.name + '.part.state')

    chunk_headers = dict(headers or {})
    referer_url = getattr(plan, 'referer', '')
    if referer_url:
        chunk_headers.setdefault('Referer', referer_url)

    signature = [[c.get('url'), int(c.get('size') or 0)] for c in chunks]

    # ---- resume: previous state is valid while the completed leading chunks match
    completed = 0
    if partial_file.exists() and state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding='utf-8'))
            done = int(state.get('completed', 0))
            prev = state.get('chunks')
            if (isinstance(prev, list) and 0 <= done <= len(signature)
                    and prev[:done] == signature[:done]
                    and partial_file.stat().st_size == sum(s for _, s in signature[:done])):
                completed = done
            else:
                raise ValueError('stale state')
        except (ValueError, OSError):
            completed = 0

    downloaded = sum(size for _, size in signature[:completed])
    if downloaded and on_progress:
        on_progress(downloaded, total)

    mode = 'r+b' if (completed and partial_file.exists()) else 'wb'
    try:
        with open(partial_file, mode) as fh:
            offset = downloaded
            for index in range(completed, len(chunks)):
                chunk = chunks[index]
                url = chunk.get('url') or ''
                chunk_size = int(chunk.get('size') or 0)
                if not url:
                    raise GhostshopError(f'chunk {index + 1} has no URL')
                got = _download_chunk_to(
                    session, url, fh, offset, chunk_headers,
                    (lambda n: on_progress(downloaded + n, total)) if on_progress else None)
                downloaded += got
                offset += got
                if on_progress:
                    on_progress(downloaded, total)
                if chunk_size and got != chunk_size:
                    raise GhostshopError(
                        f'chunk {index + 1} incomplete ({got} of {chunk_size} bytes)')
                fh.flush()
                os.fsync(fh.fileno())
                state_file.write_text(json.dumps(
                    {'chunks': signature, 'completed': index + 1}), encoding='utf-8')
            fh.truncate(offset)
    except (OSError, requests.RequestException) as exc:
        raise GhostshopError(
            f'download interrupted ({exc}); will resume on retry') from exc

    if total and partial_file.stat().st_size != total:
        raise GhostshopError(
            f'incomplete download ({partial_file.stat().st_size} of {total} bytes); '
            'will resume on retry')

    state_file.unlink(missing_ok=True)
    if destination.exists():
        destination.unlink()
    os.replace(partial_file, destination)
    return destination
