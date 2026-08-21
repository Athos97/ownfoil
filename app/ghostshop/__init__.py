"""Ghost eShop PRO client: catalog search, and chunked downloads with resume.

Ported from the switch-library-updater project (src/providers/ghostland.py,
src/net.py), adapted to ownfoil: credentials come from settings.yaml instead
of an auth file, and progress is reported through a callback instead of a
console progress bar.
"""
from .types import (  # noqa: F401
    GhostshopAuthError,
    GhostshopError,
    CatalogEntry,
    DownloadInfo,
    GameCard,
    SOURCE_GHOSTESHOP,
)
from .provider import GhosteshopProvider, test_connection  # noqa: F401
from . import net  # noqa: F401
