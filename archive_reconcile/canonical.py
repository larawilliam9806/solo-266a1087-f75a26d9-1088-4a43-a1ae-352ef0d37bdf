"""CSV parsing and canonical box digests.

The digest is deliberately insensitive to row order and to equivalent CSV
spellings (quoting, line endings, a UTF-8 BOM, optional spaces after the
delimiter, column order).  A box digest covers the box number and one record
per archive id, sorted by archive id, serialised as compact canonical JSON and
hashed with SHA-256.

Canonical record payload::

    {"format":"archive-box-v1","box_no":"B1",
     "items":[{"archive_id":"...","retention":"...","title":"..."}, ...]}

All text is NFC-normalised and trimmed; CRLF inside fields is converted to LF.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

DIGEST_FORMAT = "archive-box-v1"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class CsvError(ValueError):
    """A user-facing CSV validation error."""

    def __init__(self, message: str, *, code: str = "invalid_csv", details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class Item:
    box_no: str
    archive_id: str
    title: str
    retention: str

    def as_dict(self) -> dict:
        return {
            "box_no": self.box_no,
            "archive_id": self.archive_id,
            "title": self.title,
            "retention": self.retention,
        }


@dataclass(frozen=True)
class ManifestEntry:
    box_no: str
    item_count: int
    digest: str

    def as_dict(self) -> dict:
        return {
            "box_no": self.box_no,
            "item_count": self.item_count,
            "digest": self.digest,
        }


def clean_text(value) -> str:
    if value is None:
        return ""
    value = str(value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace(" ", " ")
    value = unicodedata.normalize("NFC", value)
    return value.strip()


def decode_csv_bytes(data: bytes) -> str:
    if not isinstance(data, (bytes, bytearray)):
        raise CsvError("上传内容不是文件字节流。")
    data = bytes(data)
    if b"\x00" in data:
        raise CsvError("CSV 含有 NUL 字节，不像文本文件。")
    # UTF-8 (with or without BOM) is the canonical exchange encoding.  Fall
    # back to GB18030 because legacy archive exports are often GBK encoded.
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return data.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise CsvError("无法按 UTF-8 或 GB18030 解码 CSV。") from exc


def _normalise_header(value: str) -> str:
    value = clean_text(value).lower()
    return re.sub(r"[\s_\-()（）:：]+", "", value)


def _normalise_headers(headers: Sequence[str]) -> list[str]:
    return [_normalise_header(h) for h in headers]


@dataclass(frozen=True)
class _FieldSpec:
    key: str
    label: str
    aliases: frozenset[str]


def _spec(key: str, label: str, aliases: Iterable[str]) -> _FieldSpec:
    return _FieldSpec(key, label, frozenset(_normalise_header(a) for a in aliases))


_ITEM_FIELDS = (
    _spec("box_no", "盒号", ("盒号", "box_no", "box", "boxno")),
    _spec(
        "archive_id",
        "盒内唯一档号",
        ("盒内唯一档号", "档号", "文件档号", "archive_id", "archive_no", "archiveid", "archiveno"),
    ),
    _spec("title", "题名", ("题名", "名称", "title")),
    _spec(
        "retention",
        "保管期限",
        ("保管期限", "保管期", "retention", "retention_period", "retentionperiod"),
    ),
)

_MANIFEST_FIELDS = (
    _spec("box_no", "盒号", ("盒号", "box_no", "box", "boxno")),
    _spec("item_count", "条数", ("条数", "件数", "记录数", "count", "item_count", "itemcount")),
    _spec(
        "digest",
        "SHA-256 摘要",
        ("摘要", "sha256摘要", "sha256", "摘要sha256", "哈希", "digest", "checksum"),
    ),
)


def _match_header(row: Sequence[str], specs: Sequence[_FieldSpec]) -> tuple[bool, dict[str, int]]:
    headers = _normalise_headers(row)
    alias_to_index: dict[str, int] = {}
    for index, header in enumerate(headers):
        if not header:
            continue
        if header in alias_to_index:
            return False, {}
        alias_to_index[header] = index
    indexes: dict[str, int] = {}
    for spec in specs:
        found = next((alias_to_index[a] for a in spec.aliases if a in alias_to_index), None)
        if found is None:
            return False, {}
        indexes[spec.key] = found
    return True, indexes


def _read_rows(text: str, specs: Sequence[_FieldSpec]) -> list[dict[str, str]]:
    stream = io.StringIO(text, newline="")
    reader = csv.reader(stream, delimiter=",", skipinitialspace=True)
    raw_rows = list(reader)
    if not raw_rows:
        raise CsvError("CSV 是空的。", code="empty_csv")

    # Tolerate short preamble lines (export timestamps, totals) by accepting
    # the first row that carries every required column.
    header_index = None
    indexes: dict[str, int] = {}
    first_non_empty = None
    checked = 0
    for index, row in enumerate(raw_rows):
        if not any(clean_text(cell) for cell in row):
            continue
        if first_non_empty is None:
            first_non_empty = index
        checked += 1
        matched, matched_indexes = _match_header(row, specs)
        if matched:
            header_index = index
            indexes = matched_indexes
            break
        if checked >= 10:
            break
    if header_index is None:
        if first_non_empty is None:
            raise CsvError("CSV 没有表头。", code="empty_csv")
        raw_headers = raw_rows[first_non_empty]
        shown = "、".join(clean_text(h) for h in raw_headers) or "（无）"
        required = "、".join(spec.label for spec in specs)
        raise CsvError(
            f"CSV 缺少必需列。需要：{required}。现有表头：{shown}。",
            code="missing_columns",
            details={"missing": [spec.label for spec in specs], "headers": [clean_text(h) for h in raw_headers]},
        )

    raw_headers = raw_rows[header_index]
    result: list[dict[str, str]] = []
    for row_number, row in enumerate(raw_rows[header_index + 1 :], start=header_index + 2):
        if not any(clean_text(cell) for cell in row):
            continue
        record = {}
        for spec in specs:
            index = indexes[spec.key]
            record[spec.key] = clean_text(row[index]) if index < len(row) else ""
        record["_line"] = row_number
        result.append(record)
    return result


def _as_text(data: bytes | str) -> str:
    if isinstance(data, (bytes, bytearray)):
        return decode_csv_bytes(bytes(data))
    text = str(data)
    return text[1:] if text.startswith("﻿") else text


def parse_items_csv(data: bytes | str, *, expected_box: str | None = None) -> list[Item]:
    """Parse an item-list CSV into items.

    Every row must belong to ``expected_box`` when it is supplied.  Archive ids
    must be unique inside each box.
    """

    text = _as_text(data)
    expected = clean_text(expected_box) if expected_box is not None else None
    rows = _read_rows(text, _ITEM_FIELDS)
    items: list[Item] = []
    seen: dict[str, dict[str, int]] = {}
    for row in rows:
        box_no = clean_text(row["box_no"])
        archive_id = clean_text(row["archive_id"])
        line = row["_line"]
        if not box_no:
            raise CsvError(f"第 {line} 行缺少盒号。", code="missing_box_no")
        if not archive_id:
            raise CsvError(
                f"第 {line} 行（盒 {box_no}）缺少盒内唯一档号。",
                code="missing_archive_id",
            )
        if expected is not None and box_no != expected:
            raise CsvError(
                f"第 {line} 行的盒号是 {box_no}，但本次换交/回传的目标盒是 {expected}。",
                code="wrong_box",
                details={"line": line, "box_no": box_no, "expected_box": expected},
            )
        per_box = seen.setdefault(box_no, {})
        if archive_id in per_box:
            raise CsvError(
                f"盒 {box_no} 内档号 {archive_id} 重复（第 {per_box[archive_id]}、{line} 行）。",
                code="duplicate_archive_id",
                details={"box_no": box_no, "archive_id": archive_id, "lines": [per_box[archive_id], line]},
            )
        per_box[archive_id] = line
        items.append(
            Item(
                box_no=box_no,
                archive_id=archive_id,
                title=clean_text(row["title"]),
                retention=clean_text(row["retention"]),
            )
        )
    return items


def group_items(items: Iterable[Item]) -> dict[str, list[Item]]:
    grouped: dict[str, list[Item]] = {}
    for item in items:
        grouped.setdefault(item.box_no, []).append(item)
    return grouped


def canonical_payload(box_no: str, items: Iterable[Item]) -> bytes:
    box_no = clean_text(box_no)
    records = [
        {
            "archive_id": item.archive_id,
            "title": item.title,
            "retention": item.retention,
        }
        for item in items
        # Items loaded from storage may not carry a box number; they are
        # already scoped to the box being hashed.
        if not item.box_no or item.box_no == box_no
    ]
    # Archive ids are unique inside a box, so this is a total order.  The extra
    # keys only guard against malformed upstream data.
    records.sort(key=lambda r: (r["archive_id"], r["title"], r["retention"]))
    payload = {
        "format": DIGEST_FORMAT,
        "box_no": box_no,
        "items": records,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def box_digest(box_no: str, items: Iterable[Item]) -> str:
    return hashlib.sha256(canonical_payload(box_no, items)).hexdigest()


def digest_for_box_map(box_no: str, items_by_box: Mapping[str, Sequence[Item]]) -> str:
    return box_digest(box_no, items_by_box.get(clean_text(box_no), ()))


def normalise_digest(value: str) -> str:
    digest = clean_text(value).lower()
    if digest.startswith("sha256:"):
        digest = digest[7:].strip()
    if not _HEX64.fullmatch(digest):
        raise CsvError(
            f"摘要格式无效：{value!r}（应为 64 位十六进制 SHA-256）。",
            code="invalid_digest",
        )
    return digest


def parse_manifest_csv(data: bytes | str) -> dict[str, ManifestEntry]:
    text = _as_text(data)
    rows = _read_rows(text, _MANIFEST_FIELDS)
    manifest: dict[str, ManifestEntry] = {}
    for row in rows:
        line = row["_line"]
        box_no = clean_text(row["box_no"])
        if not box_no:
            raise CsvError(f"第 {line} 行缺少盒号。", code="missing_box_no")
        if box_no in manifest:
            raise CsvError(
                f"清单中盒号 {box_no} 重复（第 {line} 行附近）。",
                code="duplicate_box_no",
                details={"box_no": box_no, "line": line},
            )
        raw_count = clean_text(row["item_count"]).replace(",", "")
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise CsvError(
                f"盒 {box_no} 的条数不是整数：{row['item_count']!r}。",
                code="invalid_count",
                details={"box_no": box_no, "line": line},
            ) from exc
        if count < 0:
            raise CsvError(
                f"盒 {box_no} 的条数不能为负数。",
                code="invalid_count",
                details={"box_no": box_no, "line": line},
            )
        digest = normalise_digest(row["digest"])
        manifest[box_no] = ManifestEntry(box_no=box_no, item_count=count, digest=digest)
    return manifest


def items_to_csv(items: Iterable[Item], *, include_bom: bool = True) -> str:
    """Write items in the exchange format (used by the old-side tool/tests)."""

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["盒号", "盒内唯一档号", "题名", "保管期限"])
    for item in items:
        writer.writerow([item.box_no, item.archive_id, item.title, item.retention])
    text = buffer.getvalue()
    return ("﻿" + text) if include_bom else text


def manifest_to_csv(entries: Iterable[ManifestEntry], *, include_bom: bool = True) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["盒号", "条数", "SHA-256摘要"])
    for entry in entries:
        writer.writerow([entry.box_no, entry.item_count, entry.digest])
    text = buffer.getvalue()
    return ("﻿" + text) if include_bom else text
