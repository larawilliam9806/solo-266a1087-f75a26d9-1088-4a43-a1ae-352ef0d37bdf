"""Reconciliation rules.

A reconciliation for one box compares the new-archive snapshot with the
old-side detail returned by the archive reading room.  The returned detail must
first match the old-side manifest for that box (item count + SHA-256 digest);
only then are per-archive-id differences produced.

Difference kinds (from the perspective of the new archive):

* ``missing`` – present in the old detail, absent in the new archive
* ``extra``   – present in the new archive, absent in the old detail
* ``changed`` – same archive id, different title or retention
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .canonical import Item, box_digest, clean_text


@dataclass
class BoxComparison:
    box_no: str
    outcome: str
    outcome_label: str
    summary: dict
    differences: list[dict] = field(default_factory=list)


def verify_detail_against_manifest(
    box_no: str,
    detail_items: Iterable[Item],
    manifest_entry: dict | None,
) -> tuple[str, str, dict]:
    """Check a returned detail against the imported old-side manifest.

    Returns ``(detail_digest, problem, summary)``.  ``problem`` is one of
    ``"missing_manifest"``, ``"count_mismatch"``, ``"digest_mismatch"`` or
    ``""`` when the detail matches.
    """

    items = list(detail_items)
    digest = box_digest(box_no, items)
    summary = {
        "box_no": box_no,
        "detail_count": len(items),
        "detail_digest": digest,
    }
    if manifest_entry is None:
        return digest, "missing_manifest", summary
    summary.update(
        {
            "manifest_count": manifest_entry["item_count"],
            "manifest_digest": manifest_entry["digest"],
        }
    )
    if len(items) != manifest_entry["item_count"]:
        summary["count_matches"] = False
        summary["digest_matches"] = digest == manifest_entry["digest"]
        return digest, "count_mismatch", summary
    summary["count_matches"] = True
    if digest != manifest_entry["digest"]:
        summary["digest_matches"] = False
        return digest, "digest_mismatch", summary
    summary["digest_matches"] = True
    return digest, "", summary


def compare_box(
    box_no: str,
    new_items: Iterable[Item],
    detail_items: Iterable[Item],
    manifest_entry: dict | None,
) -> BoxComparison:
    """Compare one box.  Assumes the detail already matched the manifest."""

    box_no = clean_text(box_no)
    new_by_id = {
        item.archive_id: item
        for item in new_items
        if not item.box_no or item.box_no == box_no
    }
    old_by_id = {
        item.archive_id: item
        for item in detail_items
        if not item.box_no or item.box_no == box_no
    }

    differences: list[dict] = []
    for archive_id in sorted(old_by_id.keys() - new_by_id.keys()):
        old = old_by_id[archive_id]
        differences.append(
            {
                "archive_id": archive_id,
                "kind": "missing",
                "new_title": None,
                "new_retention": None,
                "old_title": old.title,
                "old_retention": old.retention,
                "changes": ["title", "retention"],
            }
        )
    for archive_id in sorted(new_by_id.keys() - old_by_id.keys()):
        new = new_by_id[archive_id]
        differences.append(
            {
                "archive_id": archive_id,
                "kind": "extra",
                "new_title": new.title,
                "new_retention": new.retention,
                "old_title": None,
                "old_retention": None,
                "changes": ["title", "retention"],
            }
        )
    for archive_id in sorted(new_by_id.keys() & old_by_id.keys()):
        new = new_by_id[archive_id]
        old = old_by_id[archive_id]
        changed_fields = []
        if new.title != old.title:
            changed_fields.append("title")
        if new.retention != old.retention:
            changed_fields.append("retention")
        if changed_fields:
            differences.append(
                {
                    "archive_id": archive_id,
                    "kind": "changed",
                    "new_title": new.title,
                    "new_retention": new.retention,
                    "old_title": old.title,
                    "old_retention": old.retention,
                    "changes": changed_fields,
                }
            )

    manifest_count = manifest_entry["item_count"] if manifest_entry else None
    manifest_digest = manifest_entry["digest"] if manifest_entry else None
    digest_matches = manifest_entry is not None and box_digest(box_no, list(detail_items)) == manifest_digest

    counts = {"missing": 0, "extra": 0, "changed": 0}
    for difference in differences:
        counts[difference["kind"]] += 1

    summary = {
        "box_no": box_no,
        "manifest_count": manifest_count,
        "manifest_digest": manifest_digest,
        "detail_count": len(old_by_id),
        "detail_digest": box_digest(box_no, list(detail_items)),
        "new_count": len(new_by_id),
        "old_count": len(old_by_id),
        "count_matches": manifest_entry is not None and len(old_by_id) == manifest_entry["item_count"],
        "digest_matches": digest_matches,
        "missing": counts["missing"],
        "extra": counts["extra"],
        "changed": counts["changed"],
        "differences_total": len(differences),
    }

    if differences:
        outcome = "differences"
        outcome_label = (
            f"有差异：漏录 {counts['missing']}、多录 {counts['extra']}、字段变更 {counts['changed']}"
        )
    else:
        outcome = "match"
        outcome_label = "一致（条数、摘要逐档号均相符）"
    return BoxComparison(box_no, outcome, outcome_label, summary, differences)
