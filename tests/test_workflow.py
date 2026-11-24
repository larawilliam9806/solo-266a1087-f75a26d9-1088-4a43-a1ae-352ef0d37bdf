import io

from archive_reconcile.canonical import Item, box_digest
from tests.helpers import (
    OLD_BOXES,
    board,
    csv_bytes,
    get_board_text,
    make_batch,
    replace_box,
    submit_detail,
)


def test_initial_board_lists_boxes_needing_detail_and_extra_boxes(client, store):
    new_boxes = {
        # B1 matches the old box exactly
        "B1": list(OLD_BOXES["B1"]),
        # B2 has a missing, an extra and a changed record
        "B2": [
            Item("B2", "B-001", "乙盒文件（改名）", "永久"),
            Item("B2", "B-003", "新多出来的", "10年"),
        ],
        # B3 absent from the new archive -> empty box comparison
        # B5 only in the new archive -> extra box
        "B5": [Item("B5", "E-001", "多录盒文件", "永久")],
    }
    batch_id = make_batch(client, new_boxes)
    view = board(client, store, batch_id)

    assert view.needs_detail == ["B1", "B2", "B3", "B4"]
    assert view.extra_boxes == ["B5"]
    statuses = {b.box_no: b.status for b in view.boxes}
    assert statuses == {
        "B1": "need_detail",
        "B2": "need_detail",
        "B3": "need_detail",
        "B4": "need_detail",
        "B5": "extra_box",
    }
    b3 = view.get("B3")
    assert b3.new_count == 0
    assert b3.in_manifest and not b3.in_new
    assert b3.manifest_count == 3

    page = get_board_text(client, batch_id)
    assert "须回旧库调明细" in page
    assert "多录盒" in page
    assert "B3" in page


def test_detail_matching_manifest_produces_per_archive_id_differences(client, store):
    new_boxes = {
        "B1": list(OLD_BOXES["B1"]),
        "B2": [
            Item("B2", "B-001", "乙盒文件（改名）", "永久"),
            Item("B2", "B-003", "新多出来的", "10年"),
        ],
        "B3": [],
    }
    batch_id = make_batch(client, new_boxes)
    version_id = board(client, store, batch_id).get("B2").new_version_id

    response = submit_detail(client, batch_id, "B2", OLD_BOXES["B2"], version_id)
    assert response.status_code == 302

    view = board(client, store, batch_id)
    b2 = view.get("B2")
    assert b2.status == "differences"
    assert b2.has_detail
    assert b2.detail_count == 2
    assert b2.missing == 1
    assert b2.extra == 1
    assert b2.changed == 1

    reconciliation = store.get_reconciliation(batch_id, "B2")
    differences = store.get_differences(reconciliation["id"])
    kinds = {(d["archive_id"], d["kind"]) for d in differences}
    assert kinds == {
        ("B-002", "missing"),
        ("B-003", "extra"),
        ("B-001", "changed"),
    }
    changed = next(d for d in differences if d["kind"] == "changed")
    assert changed["new_title"] == "乙盒文件（改名）"
    assert changed["old_title"] == "乙盒文件"
    assert changed["changes"] == ["title"]

    # B1 was submitted as an exact copy but no detail yet; B3 empty box still
    # waits for its detail.
    assert view.get("B1").status == "need_detail"
    assert view.get("B3").status == "need_detail"


def test_empty_new_box_versus_old_box_marks_all_missing(client, store):
    new_boxes = {"B1": list(OLD_BOXES["B1"])}
    batch_id = make_batch(client, new_boxes)
    response = submit_detail(client, batch_id, "B3", OLD_BOXES["B3"], 0)
    assert response.status_code == 302

    b3 = board(client, store, batch_id).get("B3")
    assert b3.status == "differences"
    assert b3.missing == 3
    assert b3.extra == 0
    assert b3.changed == 0


def test_matching_box_reports_match(client, store):
    new_boxes = {"B1": list(OLD_BOXES["B1"])}
    batch_id = make_batch(client, new_boxes)
    version_id = board(client, store, batch_id).get("B1").new_version_id
    response = submit_detail(client, batch_id, "B1", OLD_BOXES["B1"], version_id)
    assert response.status_code == 302

    b1 = board(client, store, batch_id).get("B1")
    assert b1.status == "match"
    assert b1.missing == b1.extra == b1.changed == 0
    page = client.get(f"/batches/{batch_id}/boxes/B1").data.decode("utf-8")
    assert "逐档号核对一致" in page


def test_detail_with_wrong_count_is_rejected_and_keeps_previous_state(client, store):
    batch_id = make_batch(client, {"B1": list(OLD_BOXES["B1"])})
    version_id = board(client, store, batch_id).get("B1").new_version_id

    good = submit_detail(client, batch_id, "B1", OLD_BOXES["B1"], version_id)
    assert good.status_code == 302

    bad_items = OLD_BOXES["B1"][:2]
    bad = submit_detail(client, batch_id, "B1", bad_items, version_id)
    assert bad.status_code == 409
    assert "条数与旧库清单不符" in bad.data.decode("utf-8")

    detail = store.get_detail(batch_id, "B1")
    assert detail.item_count == 3
    b1 = board(client, store, batch_id).get("B1")
    assert b1.status == "match"


def test_detail_with_wrong_digest_is_rejected(client, store):
    batch_id = make_batch(client, {"B1": list(OLD_BOXES["B1"])})
    version_id = board(client, store, batch_id).get("B1").new_version_id

    tampered = [
        Item("B1", "A-001", "关于甲的报告", "永久"),
        Item("B1", "A-002", "关于乙的批复", "30年"),
        Item("B1", "A-003", "会议纪要（改）", "10年"),
    ]
    response = submit_detail(client, batch_id, "B1", tampered, version_id)
    assert response.status_code == 409
    assert "摘要与旧库清单不符" in response.data.decode("utf-8")
    assert store.get_detail(batch_id, "B1") is None
    assert board(client, store, batch_id).get("B1").status == "need_detail"


def test_box_replacement_recomputes_only_that_box(client, store):
    new_boxes = {
        "B1": list(OLD_BOXES["B1"]),
        "B2": list(OLD_BOXES["B2"]),
    }
    batch_id = make_batch(client, new_boxes)
    view = board(client, store, batch_id)
    v1 = view.get("B1").new_version_id
    v2 = view.get("B2").new_version_id

    assert submit_detail(client, batch_id, "B1", OLD_BOXES["B1"], v1).status_code == 302
    assert submit_detail(client, batch_id, "B2", OLD_BOXES["B2"], v2).status_code == 302
    assert board(client, store, batch_id).get("B1").status == "match"
    assert board(client, store, batch_id).get("B2").status == "match"

    # Change B1 in the new archive.  Its old conclusion must disappear and the
    # recomputed result must be based on the new content.
    changed_b1 = [
        Item("B1", "A-001", "关于甲的报告", "永久"),
        Item("B1", "A-002", "关于乙的批复", "30年"),
        Item("B1", "A-003", "会议纪要（新）", "10年"),
    ]
    response = replace_box(client, batch_id, "B1", changed_b1, v1)
    assert response.status_code == 302

    view = board(client, store, batch_id)
    b1 = view.get("B1")
    assert b1.new_version == 2
    assert b1.status == "differences"
    assert b1.changed == 1
    assert b1.missing == 0 and b1.extra == 0
    # B2 keeps its conclusion untouched.
    assert view.get("B2").status == "match"
    assert view.get("B2").new_version == 1


def test_stale_replacement_is_rejected_and_keeps_data(client, store):
    batch_id = make_batch(client, {"B1": list(OLD_BOXES["B1"])})
    stale_version = board(client, store, batch_id).get("B1").new_version_id

    first = replace_box(
        client,
        batch_id,
        "B1",
        [Item("B1", "A-001", "甲一", "永久"), Item("B1", "A-009", "甲九", "10年")],
        stale_version,
    )
    assert first.status_code == 302
    current = board(client, store, batch_id).get("B1")
    assert current.new_version == 2
    first_items = store.load_version_items(current.new_version_id)
    assert {i.archive_id for i in first_items} == {"A-001", "A-009"}

    # A second tab still holding the stale version id must not clobber v2.
    second = replace_box(
        client,
        batch_id,
        "B1",
        [Item("B1", "A-777", "冲突写入", "永久")],
        stale_version,
    )
    assert second.status_code == 409
    assert "内容在核对途中已变更" in second.data.decode("utf-8")

    still = board(client, store, batch_id).get("B1")
    assert still.new_version == 2
    assert {i.archive_id for i in store.load_version_items(still.new_version_id)} == {
        "A-001",
        "A-009",
    }


def test_changed_box_withdraws_old_differences_and_rejects_stale_detail(client, store):
    batch_id = make_batch(client, {"B1": list(OLD_BOXES["B1"])})
    v1 = board(client, store, batch_id).get("B1").new_version_id
    assert submit_detail(client, batch_id, "B1", OLD_BOXES["B1"], v1).status_code == 302
    recon = store.get_reconciliation(batch_id, "B1")
    assert recon["outcome"] == "match"
    old_recon_id = recon["id"]

    # Box content changes: old conclusion withdrawn and recomputed at once.
    replace_box(
        client,
        batch_id,
        "B1",
        [
            Item("B1", "A-001", "关于甲的报告", "永久"),
            Item("B1", "A-002", "关于乙的批复", "30年"),
            Item("B1", "A-003", "会议纪要（新内容）", "10年"),
        ],
        v1,
    )
    b1 = board(client, store, batch_id).get("B1")
    new_recon = store.get_reconciliation(batch_id, "B1")
    assert new_recon["id"] != old_recon_id
    assert new_recon["version"] == 2
    assert b1.status == "differences"
    assert b1.changed == 1

    # A detail submission from the tab that still shows version 1 is refused
    # and leaves the refreshed conclusion in place.
    stale_detail = submit_detail(client, batch_id, "B1", OLD_BOXES["B1"], v1)
    assert stale_detail.status_code == 409
    after = store.get_reconciliation(batch_id, "B1")
    assert after["id"] == new_recon["id"]


def test_extra_only_box_can_be_shown_but_has_no_detail_needed(client, store):
    batch_id = make_batch(client, {"Z9": [Item("Z9", "X-1", "孤盒", "永久")]})
    view = board(client, store, batch_id)
    assert view.extra_boxes == ["Z9"]
    assert view.get("Z9").missing == 0

    response = client.get(f"/batches/{batch_id}/boxes/Z9")
    assert response.status_code == 200
    page = response.data.decode("utf-8")
    assert "多录" in page

    # Submitting a detail for a box that is not in the manifest is refused.
    detail = submit_detail(client, batch_id, "Z9", [Item("Z9", "X-1", "孤盒", "永久")], 0)
    assert detail.status_code == 409
    assert "不在旧库清单中" in detail.data.decode("utf-8")


def test_all_boxes_stay_browsable_while_one_box_changes(client, store):
    new_boxes = {box: list(items) for box, items in OLD_BOXES.items()}
    batch_id = make_batch(client, new_boxes)
    view = board(client, store, batch_id)
    for box_no in ("B1", "B2", "B3", "B4"):
        version_id = view.get(box_no).new_version_id
        assert submit_detail(client, batch_id, box_no, OLD_BOXES[box_no], version_id).status_code == 302

    v1 = board(client, store, batch_id).get("B2").new_version_id
    assert replace_box(client, batch_id, "B2", OLD_BOXES["B2"][:1], v1).status_code == 302

    for box_no in ("B1", "B3", "B4"):
        page = client.get(f"/batches/{batch_id}/boxes/{box_no}")
        assert page.status_code == 200
        assert "逐档号核对一致" in page.data.decode("utf-8")


def test_board_report_csv_has_required_lists(client, store):
    batch_id = make_batch(
        client,
        {"B1": list(OLD_BOXES["B1"]), "Z9": [Item("Z9", "X-1", "孤盒", "永久")]},
    )
    response = client.get(f"/batches/{batch_id}/report.csv")
    assert response.status_code == 200
    text = response.data.decode("utf-8-sig")
    assert "须回旧库调明细" in text
    assert "多录盒（仅新库有）" in text
    assert "Z9" in text


def test_detail_in_equivalent_csv_spelling_still_matches_manifest(client, store):
    batch_id = make_batch(client, {"B1": list(OLD_BOXES["B1"])})
    version_id = board(client, store, batch_id).get("B1").new_version_id

    # Reordered columns, CRLF, BOM, quoted fields and reversed row order.
    text = (
        "保管期限,题名,盒内唯一档号,盒号\r\n"
        '"10年","会议纪要","A-003","B1"\r\n'
        '"永久","关于甲的报告","A-001","B1"\r\n'
        '"30年","关于乙的批复","A-002","B1"\r\n'
    )
    response = client.post(
        f"/batches/{batch_id}/boxes/B1/detail",
        data={
            "detail": (io.BytesIO(("﻿" + text).encode("utf-8")), "detail.csv"),
            "expected_version_id": str(version_id),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert board(client, store, batch_id).get("B1").status == "match"


def test_invalid_replacement_keeps_existing_version_and_conclusion(client, store):
    batch_id = make_batch(client, {"B1": list(OLD_BOXES["B1"])})
    v1 = board(client, store, batch_id).get("B1").new_version_id
    assert submit_detail(client, batch_id, "B1", OLD_BOXES["B1"], v1).status_code == 302

    bad_csv = "盒号,盒内唯一档号\nB1,A-001\n".encode("utf-8")
    response = client.post(
        f"/batches/{batch_id}/boxes/B1/items",
        data={
            "items": (io.BytesIO(bad_csv), "items.csv"),
            "expected_version_id": str(v1),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert "缺少必需列" in response.data.decode("utf-8")

    b1 = board(client, store, batch_id).get("B1")
    assert b1.new_version == 1
    assert b1.status == "match"
    assert store.get_detail(batch_id, "B1") is not None


def test_replace_with_rows_from_another_box_is_rejected(client, store):
    batch_id = make_batch(client, {"B1": list(OLD_BOXES["B1"])})
    v1 = board(client, store, batch_id).get("B1").new_version_id
    response = replace_box(client, batch_id, "B1", OLD_BOXES["B2"], v1)
    assert response.status_code == 400
    assert "目标盒是 B1" in response.data.decode("utf-8")
    assert board(client, store, batch_id).get("B1").new_version == 1


def test_replacing_with_unchanged_content_creates_no_version(client, store):
    batch_id = make_batch(client, {"B1": list(OLD_BOXES["B1"])})
    v1 = board(client, store, batch_id).get("B1").new_version_id
    response = replace_box(client, batch_id, "B1", OLD_BOXES["B1"], v1)
    assert response.status_code == 302
    b1 = board(client, store, batch_id).get("B1")
    assert b1.new_version == 1
    assert len(store.version_history(batch_id, "B1")) == 1


def test_archived_batch_blocks_writes(client, store):
    batch_id = make_batch(client, {"B1": list(OLD_BOXES["B1"])})
    v1 = board(client, store, batch_id).get("B1").new_version_id
    assert client.post(f"/batches/{batch_id}/finalize").status_code == 302

    response = replace_box(client, batch_id, "B1", OLD_BOXES["B1"], v1)
    assert response.status_code == 409
    assert "已归档" in response.data.decode("utf-8")
    detail = submit_detail(client, batch_id, "B1", OLD_BOXES["B1"], v1)
    assert detail.status_code == 409
    assert board(client, store, batch_id).get("B1").status == "need_detail"


def test_bad_manifest_leaves_no_half_created_batch(client, store):
    bad_manifest = "盒号,条数\nB1,3\n".encode("utf-8")
    response = client.post(
        "/batches",
        data={
            "label": "坏清单",
            "manifest": (io.BytesIO(bad_manifest), "manifest.csv"),
            "items": (io.BytesIO(csv_bytes(list(OLD_BOXES["B1"]))), "items.csv"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert store.list_batches() == []


def test_manifest_count_and_digest_are_fixed_for_batch_lifetime(client, store):
    batch_id = make_batch(client, {"B1": list(OLD_BOXES["B1"])})
    v1 = board(client, store, batch_id).get("B1").new_version_id
    # Change the new-archive box; the old-side reference must not move.
    replace_box(client, batch_id, "B1", OLD_BOXES["B1"][:2], v1)
    manifest = store.get_manifest(batch_id)
    assert manifest["B1"]["item_count"] == 3
    assert manifest["B1"]["digest"] == box_digest("B1", OLD_BOXES["B1"])


def test_finalized_box_conclusion_is_withdrawn_when_content_changes(client, store):
    batch_id = make_batch(client, {"B1": list(OLD_BOXES["B1"])})
    v1 = board(client, store, batch_id).get("B1").new_version_id
    assert submit_detail(client, batch_id, "B1", OLD_BOXES["B1"], v1).status_code == 302
    assert (
        client.post(
            f"/batches/{batch_id}/boxes/B1/finalize",
            data={"expected_version_id": str(v1)},
        ).status_code
        == 302
    )
    assert board(client, store, batch_id).get("B1").finalized is True

    replace_box(client, batch_id, "B1", OLD_BOXES["B1"][:2], v1)
    b1 = board(client, store, batch_id).get("B1")
    assert b1.new_version == 2
    assert b1.finalized is False
    assert b1.status == "differences"
    assert b1.missing == 1
    assert store.get_reconciliation(batch_id, "B1")["version"] == 2


def test_new_boxes_without_old_detail_keep_need_detail_after_replacement(client, store):
    batch_id = make_batch(client, {"B1": list(OLD_BOXES["B1"])})
    v1 = board(client, store, batch_id).get("B1").new_version_id
    replace_box(client, batch_id, "B1", OLD_BOXES["B1"][:2], v1)
    b1 = board(client, store, batch_id).get("B1")
    assert b1.new_version == 2
    assert b1.status == "need_detail"
    assert store.get_reconciliation(batch_id, "B1") is None


def test_replace_box_with_empty_snapshot_compares_as_zero_items(client, store):
    text = "盒号,盒内唯一档号,题名,保管期限\r\n"
    batch_id = make_batch(client, {"B1": list(OLD_BOXES["B1"])})
    v1 = board(client, store, batch_id).get("B1").new_version_id
    response = client.post(
        f"/batches/{batch_id}/boxes/B1/items",
        data={
            "items": (io.BytesIO(text.encode("utf-8")), "items.csv"),
            "expected_version_id": str(v1),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    b1 = board(client, store, batch_id).get("B1")
    assert b1.new_version == 2
    assert b1.new_count == 0
    assert b1.status == "need_detail"

    assert submit_detail(client, batch_id, "B1", OLD_BOXES["B1"], b1.new_version_id).status_code == 302
    b1 = board(client, store, batch_id).get("B1")
    assert b1.status == "differences"
    assert b1.missing == 3
