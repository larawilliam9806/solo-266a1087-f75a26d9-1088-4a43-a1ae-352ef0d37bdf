"""Old-archive side export: derive a per-box manifest from an item CSV.

Run from the old archive host (no network or Flask needed)::

    python -m archive_reconcile.export_manifest old-items.csv -o manifest.csv

The output has one row per box: 盒号, 条数, SHA-256摘要.  The digest is stable
under row reordering and equivalent CSV spellings, so the new-archive side can
verify returned details against it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .canonical import (
    CsvError,
    ManifestEntry,
    box_digest,
    group_items,
    manifest_to_csv,
    parse_items_csv,
)


def build_manifest(rows_text: str) -> list[ManifestEntry]:
    items = parse_items_csv(rows_text)
    grouped = group_items(items)
    return [
        ManifestEntry(
            box_no=box_no,
            item_count=len(grouped[box_no]),
            digest=box_digest(box_no, grouped[box_no]),
        )
        for box_no in sorted(grouped, key=str.casefold)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从旧库题录 CSV 导出每盒条数与 SHA-256 摘要"
    )
    parser.add_argument("items", help="旧库题录 CSV 路径，或 - 表示标准输入")
    parser.add_argument("-o", "--output", help="输出清单 CSV 路径（默认标准输出）")
    parser.add_argument(
        "--no-bom",
        action="store_true",
        help="不写 UTF-8 BOM（默认写，便于 Excel 打开）",
    )
    args = parser.parse_args(argv)

    try:
        if args.items == "-":
            data = sys.stdin.buffer.read()
        else:
            data = Path(args.items).read_bytes()
        entries = build_manifest(data)
    except (CsvError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    text = manifest_to_csv(entries, include_bom=not args.no_bom)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8", newline="")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
