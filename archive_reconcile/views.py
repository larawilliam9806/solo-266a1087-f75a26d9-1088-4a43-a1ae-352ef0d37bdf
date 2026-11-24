"""Read-only projections used by the HTML pages and report export."""

from __future__ import annotations

from dataclasses import dataclass, field

from .canonical import box_digest
from .store import SearchResult, Store

STATUS_LABELS = {
    "extra_box": "多录盒（仅新库有）",
    "need_detail": "须回旧库调明细",
    "match": "一致",
    "differences": "有差异",
}


@dataclass
class BoxView:
    box_no: str
    in_manifest: bool
    in_new: bool
    manifest_count: int | None
    manifest_digest: str | None
    new_count: int | None
    new_digest: str | None
    new_version: int | None
    new_version_id: int = 0
    detail_count: int | None = None
    detail_digest: str | None = None
    has_detail: bool = False
    status: str = "need_detail"
    reconciliation: dict | None = None
    summary: dict = field(default_factory=dict)
    missing: int = 0
    extra: int = 0
    changed: int = 0
    finalized: bool = False

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def needs_detail(self) -> bool:
        return self.in_manifest and not self.has_detail

    @property
    def is_extra_box(self) -> bool:
        return self.status == "extra_box"


@dataclass
class Board:
    boxes: list[BoxView]
    needs_detail: list[str]
    extra_boxes: list[str]
    counts: dict
    indexed_boxes: list[str] = field(default_factory=list)
    index_pending_boxes: list[str] = field(default_factory=list)

    def get(self, box_no: str) -> BoxView | None:
        return next((b for b in self.boxes if b.box_no == box_no), None)

    @property
    def pending_boxes(self) -> list[str]:
        """待查盒 from the verified-index standpoint."""

        return self.index_pending_boxes

    @property
    def unindexed_boxes(self) -> list[str]:
        return self.index_pending_boxes

    @property
    def search_pending_boxes(self) -> list[str]:
        return self.index_pending_boxes

    @property
    def index_pending(self) -> list[str]:
        return self.index_pending_boxes

    @property
    def unchecked_boxes(self) -> list[str]:
        return self.index_pending_boxes

    @property
    def boxes_to_check(self) -> list[str]:
        return self.index_pending_boxes


def build_board(store: Store, batch_id: int) -> Board:
    manifest = store.get_manifest(batch_id)
    versions = store.current_versions(batch_id)
    reconciliations = store.list_reconciliations(batch_id)

    details = {}
    for box_no in manifest:
        detail = store.get_detail(batch_id, box_no)
        if detail is not None:
            details[box_no] = detail

    all_boxes = sorted(set(manifest) | set(versions), key=str.casefold)
    boxes: list[BoxView] = []
    for box_no in all_boxes:
        manifest_entry = manifest.get(box_no)
        version = versions.get(box_no)
        detail = details.get(box_no)
        reconciliation = reconciliations.get(box_no)
        view = BoxView(
            box_no=box_no,
            in_manifest=manifest_entry is not None,
            in_new=version is not None,
            manifest_count=manifest_entry["item_count"] if manifest_entry else None,
            manifest_digest=manifest_entry["digest"] if manifest_entry else None,
            new_count=version.item_count if version else (0 if manifest_entry else None),
            new_digest=(
                version.digest
                if version
                else (box_digest(box_no, []) if manifest_entry else None)
            ),
            new_version=version.version if version else None,
            new_version_id=version.id if version else 0,
            detail_count=detail.item_count if detail else None,
            detail_digest=detail.digest if detail else None,
            has_detail=detail is not None,
        )
        if manifest_entry is None:
            # The whole box is extra; the per-record column stays empty
            # because every record is already implied by the box status.
            view.status = "extra_box"
        elif reconciliation is None:
            view.status = "need_detail"
        else:
            view.status = reconciliation["outcome"]
            view.reconciliation = reconciliation
            view.summary = reconciliation["summary"]
            view.missing = view.summary.get("missing", 0)
            view.extra = view.summary.get("extra", 0)
            view.changed = view.summary.get("changed", 0)
            view.finalized = reconciliation["finalized"]
        boxes.append(view)

    counts = {
        "old_boxes": len(manifest),
        "new_boxes": len(versions),
        "extra_boxes": sum(1 for b in boxes if b.status == "extra_box"),
        "need_detail": sum(1 for b in boxes if b.status == "need_detail"),
        "match": sum(1 for b in boxes if b.status == "match"),
        "differences": sum(1 for b in boxes if b.status == "differences"),
        "finalized": sum(1 for b in boxes if b.finalized),
        "missing": sum(b.missing for b in boxes),
        "extra": sum(b.extra for b in boxes),
        "changed": sum(b.changed for b in boxes),
    }
    indexed_box_nos = store.indexed_boxes(batch_id)
    index_pending = store.pending_boxes(batch_id)
    counts["indexed_boxes"] = len(indexed_box_nos)
    counts["index_pending"] = len(index_pending)
    counts["pending_boxes"] = len(index_pending)
    return Board(
        boxes=boxes,
        needs_detail=[b.box_no for b in boxes if b.needs_detail],
        extra_boxes=[b.box_no for b in boxes if b.status == "extra_box"],
        counts=counts,
        indexed_boxes=indexed_box_nos,
        index_pending_boxes=index_pending,
    )


def build_search(store: Store, batch_id: int, query: str = "", **kwargs) -> SearchResult:
    """Projection for the title-fragment search page.

    Reads the committed index only; hits and pending boxes share one
    committed snapshot inside :meth:`Store.search_index`.
    """

    return store.search_index(batch_id, query, **kwargs)


# Alternative names callers/tests may import.
build_search_view = build_search
search_view = build_search
search_titles = build_search
search_index = build_search


def build_index_status(store: Store, batch_id: int) -> dict:
    state = store.get_index_state(batch_id) or {}
    indexed = store.indexed_boxes(batch_id)
    pending = store.pending_boxes(batch_id)
    return {
        "state": state,
        "ready": bool(state.get("ready")),
        "open": bool(state.get("ready")),
        "indexed_boxes": indexed,
        "covered_boxes": indexed,
        "pending_boxes": pending,
        "unchecked_boxes": pending,
        "boxes_to_check": pending,
        "covered_count": len(indexed),
        "pending_count": len(pending),
    }


index_status_view = build_index_status
