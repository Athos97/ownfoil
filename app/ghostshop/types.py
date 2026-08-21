"""Types and errors for the Ghost eShop PRO client."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

# Download-row source marker used across the downloader for Ghost eShop rows.
SOURCE_GHOSTESHOP = 'ghosteshop'


class GhostshopError(Exception):
    """Any network/protocol failure talking to Ghost eShop."""


class GhostshopAuthError(GhostshopError):
    """Credentials rejected, or the session is not valid."""


BASE = 'BASE'
UPD = 'UPDATE'
DLC = 'DLC'


@dataclass(frozen=True)
class CatalogEntry:
    """One normalized catalog item (base, update or DLC).

    `name` is the exact file name the portal expects when requesting a
    download link - it is the opaque key for the whole download flow.
    """
    name: str
    tid: str = ''        # 16 uppercase hex digits, '' when unknown
    category: str = BASE  # BASE | UPDATE | DLC
    version: int = 0     # 0 when not applicable/known
    size: int = 0        # bytes, 0 when unknown


@dataclass
class GameCard:
    """Everything the catalog knows about one game family."""
    title: str
    entries: List[CatalogEntry] = field(default_factory=list)

    def of_category(self, category: str) -> List[CatalogEntry]:
        return [e for e in self.entries if e.category == category]

    def by_tid(self, tid: str) -> List[CatalogEntry]:
        wanted = (tid or '').upper()
        return [e for e in self.entries if e.tid and e.tid.upper() == wanted]


@dataclass
class DownloadInfo:
    """A download plan: ordered chunks that concatenate into the final file.

    The CDN serving the chunks requires the Referer of the /d/<token> page,
    carried in `referer`.
    """
    file_name: str
    file_size: int
    chunks: List[dict] = field(default_factory=list)  # [{url, size, ...}]
    referer: str = ''


@dataclass
class SearchResult:
    """One row of the portal's text search (fetch-list)."""
    tid: str
    title: str
