"""Title-fragment search / verified per-box index helpers.

The persistence and transactional logic lives in :mod:`archive_reconcile.store`;
this module provides a functional surface for callers that prefer
``search.search_titles(store, ...)``.
"""

from __future__ import annotations

from .store import Record, SearchResult

__all__ = [
    "Record",
    "SearchResult",
    "search_index",
    "search_titles",
    "build_search",
    "rebuild_index",
    "backfill_index",
    "build_index",
    "pending_boxes",
    "indexed_boxes",
]


def search_index(store, batch_id: int, fragment: str = "", **kwargs) -> SearchResult:
    return store.search_index(batch_id, fragment, **kwargs)


search_titles = search_index
find_by_title = search_index


def build_search(store, batch_id: int, fragment: str = "", **kwargs) -> SearchResult:
    return store.search_index(batch_id, fragment, **kwargs)


def rebuild_index(store, batch_id: int, **kwargs) -> dict:
    return store.rebuild_index(batch_id, **kwargs)


backfill_index = rebuild_index
build_index = rebuild_index
ensure_index = rebuild_index
reindex = rebuild_index


def pending_boxes(store, batch_id: int) -> list[str]:
    return store.pending_boxes(batch_id)


def indexed_boxes(store, batch_id: int) -> list[str]:
    return store.indexed_boxes(batch_id)
