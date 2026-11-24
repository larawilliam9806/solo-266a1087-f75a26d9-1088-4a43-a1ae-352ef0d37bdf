"""Compatibility alias for :mod:`archive_reconcile.search`."""

from .search import *  # noqa: F401,F403
from .search import (  # noqa: F401
    Record,
    SearchResult,
    backfill_index,
    build_index,
    build_search,
    ensure_index,
    find_by_title,
    indexed_boxes,
    pending_boxes,
    reindex,
    rebuild_index,
    search_index,
    search_titles,
)
