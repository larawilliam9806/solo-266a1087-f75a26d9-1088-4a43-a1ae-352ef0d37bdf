import pytest

from archive_reconcile.canonical import Item, box_digest
from tests.helpers import (
    OLD_BOXES,
    board,
    make_batch,
    replace_box,
    submit_detail,
)


def test_two_simultaneous_submissions_against_same_version_only_one_wins(client, store):
    batch_id = make_batch(client, {"B1": list(OLD_BOXES["B1"])})
    v1 = board(client, store, batch_id).get("B1").new_version_id
    current = store.get_current_version(batch_id, "B1")

    # Both pages were rendered with v1; the first save wins.
    first = store.import_snapshot(
        batch_id,
        {"B1": OLD_BOXES["B1"][:2]},
        source="box-replacement",
        expected_versions={"B1": v1},
    )
    assert first["B1"]["created"] is True

    from archive_reconcile.store import StaleBoxError

    with pytest.raises(StaleBoxError):
        store.import_snapshot(
            batch_id,
            {"B1": OLD_BOXES["B1"][:1]},
            source="box-replacement",
            expected_versions={"B1": v1},
        )
    after = store.get_current_version(batch_id, "B1")
    assert after.version == 2
    assert after.id == current.id + 1


def test_stale_detail_after_replacement_does_not_overwrite_new_conclusion(client, store):
    batch_id = make_batch(client, {"B1": list(OLD_BOXES["B1"])})
    v1 = board(client, store, batch_id).get("B1").new_version_id
    assert submit_detail(client, batch_id, "B1", OLD_BOXES["B1"], v1).status_code == 302
    replace_box(client, batch_id, "B1", OLD_BOXES["B1"][:2], v1)

    snapshot = store.get_reconciliation(batch_id, "B1")
    response = submit_detail(client, batch_id, "B1", OLD_BOXES["B1"], v1)
    assert response.status_code == 409
    after = store.get_reconciliation(batch_id, "B1")
    assert after["id"] == snapshot["id"]
    assert after["version"] == 2


def test_manifest_mismatch_never_creates_a_detail_row(client, store):
    batch_id = make_batch(client, {"B1": list(OLD_BOXES["B1"])})
    v1 = board(client, store, batch_id).get("B1").new_version_id
    response = submit_detail(client, batch_id, "B1", OLD_BOXES["B1"][:2], v1)
    assert response.status_code == 409
    assert store.get_detail(batch_id, "B1") is None
    assert store.get_reconciliation(batch_id, "B1") is None


def test_digest_and_count_mismatch_both_reported_but_count_checked_first(client, store):
    batch_id = make_batch(client, {"B1": list(OLD_BOXES["B1"])})
    v1 = board(client, store, batch_id).get("B1").new_version_id
    changed = [
        Item("B1", "A-001", "改过的题名", "永久"),
        Item("B1", "A-002", "关于乙的批复", "30年"),
    ]
    response = submit_detail(client, batch_id, "B1", changed, v1)
    assert response.status_code == 409
    page = response.data.decode("utf-8")
    assert "条数与旧库清单不符" in page
    assert "清单 3 条" in page


def test_extra_box_rows_do_not_affect_manifest_boxes(client, store):
    batch_id = make_batch(
        client,
        {"B1": list(OLD_BOXES["B1"]), "Z9": [Item("Z9", "X-1", "孤盒", "永久")]},
    )
    v1 = board(client, store, batch_id).get("B1").new_version_id
    assert submit_detail(client, batch_id, "B1", OLD_BOXES["B1"], v1).status_code == 302
    view = board(client, store, batch_id)
    assert view.get("B1").status == "match"
    assert view.get("Z9").status == "extra_box"
    assert view.counts["extra_boxes"] == 1
    assert view.counts["match"] == 1
    assert view.counts["need_detail"] == 3  # B2, B3, B4 still awaiting detail


def test_manifest_digest_is_verified_independently_of_row_order(client, store):
    batch_id = make_batch(client, {"B1": list(OLD_BOXES["B1"])})
    v1 = board(client, store, batch_id).get("B1").new_version_id
    reversed_rows = list(reversed(OLD_BOXES["B1"]))
    assert submit_detail(client, batch_id, "B1", reversed_rows, v1).status_code == 302
    assert box_digest("B1", reversed_rows) == box_digest("B1", OLD_BOXES["B1"])
    assert board(client, store, batch_id).get("B1").status == "match"
