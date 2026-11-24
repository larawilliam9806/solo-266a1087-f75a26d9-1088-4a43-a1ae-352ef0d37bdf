import io
import re

from archive_reconcile.canonical import (
    Item,
    box_digest,
    items_to_csv,
    manifest_to_csv,
)
from archive_reconcile.views import build_board

OLD_BOXES = {
    "B1": [
        Item("B1", "A-001", "关于甲的报告", "永久"),
        Item("B1", "A-002", "关于乙的批复", "30年"),
        Item("B1", "A-003", "会议纪要", "10年"),
    ],
    "B2": [
        Item("B2", "B-001", "乙盒文件", "永久"),
        Item("B2", "B-002", "乙盒补充", "30年"),
    ],
    "B3": [
        Item("B3", "C-001", "丙盒文件", "永久"),
        Item("B3", "C-002", "丙盒文件二", "30年"),
        Item("B3", "C-003", "丙盒文件三", "10年"),
    ],
    "B4": [
        Item("B4", "D-001", "丁盒文件", "永久"),
    ],
}


def manifest_csv(boxes=OLD_BOXES):
    from archive_reconcile.canonical import ManifestEntry

    entries = [
        ManifestEntry(box, len(items), box_digest(box, items))
        for box, items in sorted(boxes.items())
    ]
    return manifest_to_csv(entries).encode("utf-8")


def csv_bytes(items, *, bom=True):
    return items_to_csv(items, include_bom=bom).encode("utf-8")


def make_batch(client, new_boxes, manifest=None):
    data = {
        "label": "测试批次",
        "manifest": (io.BytesIO(manifest or manifest_csv()), "manifest.csv"),
    }
    all_items = [item for items in new_boxes.values() for item in items]
    data["items"] = (io.BytesIO(csv_bytes(all_items)), "items.csv")
    response = client.post("/batches", data=data, content_type="multipart/form-data")
    assert response.status_code == 302, response.data.decode("utf-8")[:2000]
    match = re.search(r"/batches/(\d+)", response.headers["Location"])
    return int(match.group(1))


def get_board_text(client, batch_id):
    response = client.get(f"/batches/{batch_id}")
    assert response.status_code == 200
    return response.data.decode("utf-8")


def board(client, store, batch_id):
    return build_board(store, batch_id)


def submit_detail(client, batch_id, box_no, items, version_id=0):
    return client.post(
        f"/batches/{batch_id}/boxes/{box_no}/detail",
        data={
            "detail": (io.BytesIO(csv_bytes(items)), "detail.csv"),
            "expected_version_id": str(version_id),
        },
        content_type="multipart/form-data",
    )


def replace_box(client, batch_id, box_no, items, version_id=0):
    return client.post(
        f"/batches/{batch_id}/boxes/{box_no}/items",
        data={
            "items": (io.BytesIO(csv_bytes(items)), "items.csv"),
            "expected_version_id": str(version_id),
        },
        content_type="multipart/form-data",
    )
