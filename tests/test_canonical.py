import pytest

from archive_reconcile.canonical import (
    CsvError,
    Item,
    box_digest,
    items_to_csv,
    manifest_to_csv,
    parse_items_csv,
    parse_manifest_csv,
)
from archive_reconcile.export_manifest import build_manifest


def item(box, archive_id, title="题名", retention="永久"):
    return Item(box, archive_id, title, retention)


BOX = [
    item("B1", "A-001", "关于甲的报告", "永久"),
    item("B1", "A-002", "关于乙的批复", "30年"),
    item("B1", "A-003", "会议纪要", "10年"),
]
MANIFEST = build_manifest(items_to_csv(BOX))


def test_digest_is_independent_of_row_order():
    forward = box_digest("B1", BOX)
    backward = box_digest("B1", list(reversed(BOX)))
    assert forward == backward


def test_digest_is_independent_of_equivalent_csv_spelling():
    canonical = box_digest("B1", BOX)

    # quoted fields, CRLF, BOM, spaces after commas, reordered columns, UTF-8
    spellings = [
        "盒号,盒内唯一档号,题名,保管期限\r\n"
        'B1, "A-001", "关于甲的报告", "永久"\r\n'
        "B1, A-003, 会议纪要, 10年\r\n"
        "B1, A-002, 关于乙的批复, 30年\r\n",
        "保管期限,题名,盒号,盒内唯一档号\n"
        "永久,关于甲的报告,B1,A-001\n"
        "30年,关于乙的批复,B1,A-002\n"
        "10年,会议纪要,B1,A-003\n",
        "﻿盒号,盒内唯一档号,题名,保管期限\n"
        "B1,A-002,关于乙的批复,30年\n"
        "B1,A-001,关于甲的报告,永久\n"
        "B1,A-003,会议纪要,10年\n",
    ]
    for text in spellings:
        encoded = text.encode("utf-8")
        parsed = parse_items_csv(encoded)
        assert box_digest("B1", parsed) == canonical


def test_digest_changes_when_content_changes():
    changed = [item("B1", i.archive_id, i.title, i.retention) for i in BOX]
    changed[0] = item("B1", "A-001", "关于甲的补充报告", "永久")
    assert box_digest("B1", changed) != box_digest("B1", BOX)


def test_gbk_input_is_readable():
    text = "盒号,盒内唯一档号,题名,保管期限\nB1,A-001,关于甲的报告,永久\n"
    parsed = parse_items_csv(text.encode("gb18030"))
    assert parsed[0].archive_id == "A-001"
    assert parsed[0].title == "关于甲的报告"


def test_manifest_export_round_trips():
    manifest = parse_manifest_csv(manifest_to_csv(MANIFEST).encode("utf-8"))
    assert manifest["B1"].item_count == 3
    assert manifest["B1"].digest == box_digest("B1", BOX)


def test_duplicate_archive_id_rejected():
    text = "盒号,盒内唯一档号,题名,保管期限\nB1,A-001,X,永久\nB1,A-001,Y,永久\n"
    with pytest.raises(CsvError) as excinfo:
        parse_items_csv(text)
    assert excinfo.value.code == "duplicate_archive_id"


def test_missing_column_rejected():
    text = "盒号,题名,保管期限\nB1,X,永久\n"
    with pytest.raises(CsvError) as excinfo:
        parse_items_csv(text)
    assert excinfo.value.code == "missing_columns"


def test_wrong_box_rejected_when_expected():
    with pytest.raises(CsvError) as excinfo:
        parse_items_csv(items_to_csv(BOX), expected_box="B2")
    assert excinfo.value.code == "wrong_box"


def test_empty_box_has_stable_digest():
    assert box_digest("B9", []) == box_digest("B9", [])
    assert box_digest("B9", []) != box_digest("B8", [])


def test_digest_golden_vector_is_stable_across_releases():
    # Pinning the exact byte layout of the canonical payload ensures old-side
    # manifests exported by any version stay verifiable.
    assert (
        box_digest("G1", [item("G1", "A-1", "题名一", "永久")])
        == "0a1c9d12fd8caa7c072351cd4a9461a2f01f774f37a460ab25786a8983922c3a"
    )
