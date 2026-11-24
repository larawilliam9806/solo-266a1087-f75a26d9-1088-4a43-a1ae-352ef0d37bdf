"""SQLite persistence for reconciliation batches.

Design notes:

* The old-side manifest is written once when the batch is created and is never
  modified.  A batch therefore has a fixed reference for every box.
* New-archive imports are append-only box *versions*.  The latest version is
  the current content of a box.  Re-importing an unchanged box is a no-op.
* Accepted old-side details are stored separately from conclusions.  They are
  valid as long as the manifest is (i.e. until the batch is deleted), so a new
  import can be compared against them immediately.
* A conclusion is deleted as soon as the box content changes and recomputed
  inside the same transaction.  Saving a conclusion requires the version the
  caller saw; a mismatch aborts the whole transaction.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .canonical import Item, box_digest, clean_text
from .reconcile import compare_box, verify_detail_against_manifest

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    label        TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('open', 'finalized')),
    created_at   REAL NOT NULL,
    finalized_at REAL
);

CREATE TABLE IF NOT EXISTS manifest (
    batch_id   INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    box_no     TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    digest     TEXT NOT NULL,
    PRIMARY KEY (batch_id, box_no)
);

CREATE TABLE IF NOT EXISTS box_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    box_no      TEXT NOT NULL,
    version     INTEGER NOT NULL,
    item_count  INTEGER NOT NULL,
    digest      TEXT NOT NULL,
    imported_at REAL NOT NULL,
    UNIQUE (batch_id, box_no, version)
);

CREATE TABLE IF NOT EXISTS box_items (
    version_id INTEGER NOT NULL REFERENCES box_versions(id) ON DELETE CASCADE,
    archive_id TEXT NOT NULL,
    title      TEXT NOT NULL,
    retention  TEXT NOT NULL,
    PRIMARY KEY (version_id, archive_id)
);

CREATE TABLE IF NOT EXISTS box_imports (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    box_no           TEXT NOT NULL,
    version          INTEGER NOT NULL,
    previous_version INTEGER,
    item_count       INTEGER NOT NULL,
    digest           TEXT NOT NULL,
    source           TEXT NOT NULL,
    imported_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS old_details (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    box_no      TEXT NOT NULL,
    item_count  INTEGER NOT NULL,
    digest      TEXT NOT NULL,
    received_at REAL NOT NULL,
    UNIQUE (batch_id, box_no)
);

CREATE TABLE IF NOT EXISTS old_detail_items (
    detail_id  INTEGER NOT NULL REFERENCES old_details(id) ON DELETE CASCADE,
    archive_id TEXT NOT NULL,
    title      TEXT NOT NULL,
    retention  TEXT NOT NULL,
    PRIMARY KEY (detail_id, archive_id)
);

CREATE TABLE IF NOT EXISTS reconciliations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    box_no        TEXT NOT NULL,
    version       INTEGER,
    version_id    INTEGER REFERENCES box_versions(id) ON DELETE CASCADE,
    detail_id     INTEGER REFERENCES old_details(id) ON DELETE CASCADE,
    detail_count  INTEGER NOT NULL,
    detail_digest TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    summary_json  TEXT NOT NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    finalized_at  REAL,
    UNIQUE (batch_id, box_no)
);

CREATE TABLE IF NOT EXISTS differences (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    reconciliation_id INTEGER NOT NULL REFERENCES reconciliations(id) ON DELETE CASCADE,
    archive_id        TEXT NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('missing', 'extra', 'changed')),
    new_title         TEXT,
    new_retention     TEXT,
    old_title         TEXT,
    old_retention     TEXT,
    changes_json      TEXT
);
CREATE INDEX IF NOT EXISTS differences_reconciliation
    ON differences(reconciliation_id, archive_id);
"""


class StaleBoxError(Exception):
    """The box changed between rendering a page and saving its result."""

    def __init__(self, box_no: str, expected_version_id: int, current_version: int | None):
        super().__init__(f"box {box_no} changed")
        self.box_no = box_no
        self.expected_version_id = expected_version_id
        self.current_version = current_version


class ManifestMismatchError(Exception):
    """A returned detail does not match the fixed old-side manifest."""

    def __init__(self, box_no: str, problem: str, summary: dict):
        super().__init__(f"detail for {box_no} rejected: {problem}")
        self.box_no = box_no
        self.problem = problem
        self.summary = summary


class BatchLockedError(Exception):
    """The batch is finalized."""


class NotFoundError(Exception):
    pass


@dataclass
class Batch:
    id: int
    label: str
    status: str
    created_at: float
    finalized_at: float | None

    @property
    def is_open(self) -> bool:
        return self.status == "open"


@dataclass
class BoxVersion:
    id: int
    batch_id: int
    box_no: str
    version: int
    item_count: int
    digest: str
    imported_at: float


@dataclass
class Detail:
    id: int
    batch_id: int
    box_no: str
    item_count: int
    digest: str
    received_at: float


def now() -> float:
    return time.time()


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._uri = False
        self._keeper: sqlite3.Connection | None = None
        if self.path == ":memory:":
            # Every connection to ":memory:" gets a private database.  Use a
            # shared-cache named in-memory database and keep one connection
            # open for the lifetime of the store so the schema survives.
            token = f"archive_reconcile_{id(self)}"
            self.path = f"file:{token}?mode=memory&cache=shared"
            self._uri = True
            self._keeper = sqlite3.connect(self.path, uri=True, isolation_level=None)
            self._keeper.row_factory = sqlite3.Row
            self._keeper.execute("PRAGMA foreign_keys = ON")
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self.path, timeout=30, isolation_level=None, uri=self._uri
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # batches
    # ------------------------------------------------------------------

    def create_batch(self, label: str, manifest: Iterable) -> Batch:
        stamp = now()
        with self._transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO batches (label, status, created_at)"
                " VALUES (?, 'open', ?)",
                (label, stamp),
            )
            batch_id = cursor.lastrowid
            conn.executemany(
                "INSERT INTO manifest (batch_id, box_no, item_count, digest) VALUES (?, ?, ?, ?)",
                [(batch_id, e.box_no, e.item_count, e.digest) for e in manifest],
            )
            row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
            return _batch(row)

    def get_batch(self, batch_id: int) -> Batch | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        return _batch(row) if row else None

    def ping(self) -> bool:
        with self._connect() as conn:
            conn.execute("SELECT 1 FROM batches LIMIT 1").fetchall()
        return True

    def list_batches(self) -> list[Batch]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM batches ORDER BY id DESC").fetchall()
        return [_batch(r) for r in rows]

    def finalize_batch(self, batch_id: int) -> bool:
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE batches SET status = 'finalized', finalized_at = ?"
                " WHERE id = ? AND status = 'open'",
                (now(), batch_id),
            )
            return cursor.rowcount == 1

    def reopen_batch(self, batch_id: int) -> bool:
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE batches SET status = 'open', finalized_at = NULL"
                " WHERE id = ? AND status = 'finalized'",
                (batch_id,),
            )
            return cursor.rowcount == 1

    def delete_batch(self, batch_id: int) -> bool:
        """Delete a batch.  Used to undo a partially created batch."""

        self._checkpoint()
        with self._transaction() as conn:
            cursor = conn.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
            return cursor.rowcount == 1

    def _checkpoint(self) -> None:
        """Fold the WAL into the main DB so the file is portable and small."""
        try:
            with self._connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        except sqlite3.DatabaseError:
            pass

    @staticmethod
    def _require_open(conn: sqlite3.Connection, batch_id: int) -> None:
        row = conn.execute("SELECT status FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"批次 {batch_id} 不存在。")
        if row["status"] != "open":
            raise BatchLockedError("批次已归档，不能再换交题录或修改核对结果。")

    # ------------------------------------------------------------------
    # manifest / read projections
    # ------------------------------------------------------------------

    def get_manifest(self, batch_id: int) -> dict[str, dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT box_no, item_count, digest FROM manifest WHERE batch_id = ?"
                " ORDER BY box_no COLLATE NOCASE",
                (batch_id,),
            ).fetchall()
        return {
            r["box_no"]: {
                "box_no": r["box_no"],
                "item_count": r["item_count"],
                "digest": r["digest"],
            }
            for r in rows
        }

    def current_versions(self, batch_id: int) -> dict[str, BoxVersion]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT v.* FROM box_versions v"
                " JOIN (SELECT box_no, MAX(version) AS mv FROM box_versions"
                "        WHERE batch_id = ? GROUP BY box_no) m"
                "   ON m.box_no = v.box_no AND m.mv = v.version"
                " WHERE v.batch_id = ?",
                (batch_id, batch_id),
            ).fetchall()
        return {r["box_no"]: _version(r) for r in rows}

    def get_current_version(self, batch_id: int, box_no: str) -> BoxVersion | None:
        with self._connect() as conn:
            row = self._current_version_row(conn, batch_id, box_no)
        return _version(row) if row else None

    def version_history(self, batch_id: int, box_no: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM box_versions WHERE batch_id = ? AND box_no = ?"
                " ORDER BY version",
                (batch_id, box_no),
            ).fetchall()
        return [dict(r) for r in rows]

    def load_version_items(self, version_id: int, box_no: str = "") -> list[Item]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT archive_id, title, retention FROM box_items"
                " WHERE version_id = ? ORDER BY archive_id COLLATE NOCASE",
                (version_id,),
            ).fetchall()
        return [Item(box_no, r["archive_id"], r["title"], r["retention"]) for r in rows]

    def get_detail(self, batch_id: int, box_no: str) -> Detail | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM old_details WHERE batch_id = ? AND box_no = ?",
                (batch_id, box_no),
            ).fetchone()
        return _detail(row) if row else None

    def load_detail_items(self, detail_id: int, box_no: str = "") -> list[Item]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT archive_id, title, retention FROM old_detail_items"
                " WHERE detail_id = ? ORDER BY archive_id COLLATE NOCASE",
                (detail_id,),
            ).fetchall()
        return [Item(box_no, r["archive_id"], r["title"], r["retention"]) for r in rows]

    def get_reconciliation(self, batch_id: int, box_no: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM reconciliations WHERE batch_id = ? AND box_no = ?",
                (batch_id, box_no),
            ).fetchone()
        return _reconciliation(row) if row else None

    def list_reconciliations(self, batch_id: int) -> dict[str, dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM reconciliations WHERE batch_id = ?", (batch_id,)
            ).fetchall()
        return {r["box_no"]: _reconciliation(r) for r in rows}

    def get_differences(self, reconciliation_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM differences WHERE reconciliation_id = ?"
                " ORDER BY kind, archive_id COLLATE NOCASE",
                (reconciliation_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["changes"] = json.loads(item.pop("changes_json")) if item["changes_json"] else []
            result.append(item)
        return result

    def box_imports(self, batch_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM box_imports WHERE batch_id = ? ORDER BY id", (batch_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # new-archive imports
    # ------------------------------------------------------------------

    def import_snapshot(
        self,
        batch_id: int,
        items_by_box: dict[str, list[Item]],
        *,
        source: str = "snapshot",
        expected_versions: dict[str, int] | None = None,
    ) -> dict[str, dict]:
        """Import one or more complete box snapshots atomically.

        Boxes absent from ``items_by_box`` are left untouched.  When
        ``expected_versions`` is given for a box, the import is rejected with
        :class:`StaleBoxError` unless that box is still at the expected version
        (``0`` means "no content yet").  Nothing is written on rejection.
        """

        stamp = now()
        results: dict[str, dict] = {}
        with self._transaction() as conn:
            self._require_open(conn, batch_id)
            for box_no in sorted(items_by_box, key=str.casefold):
                items = items_by_box[box_no]
                digest = box_digest(box_no, items)
                current = self._current_version_row(conn, batch_id, box_no)
                current_id = 0 if current is None else current["id"]
                if expected_versions is not None and box_no in expected_versions:
                    if current_id != expected_versions[box_no]:
                        raise StaleBoxError(
                            box_no,
                            expected_versions[box_no],
                            None if current is None else current["version"],
                        )
                if current is not None and current["digest"] == digest:
                    results[box_no] = {
                        "box_no": box_no,
                        "version": current["version"],
                        "created": False,
                        "item_count": current["item_count"],
                        "digest": digest,
                    }
                    continue
                version_no = 1 if current is None else current["version"] + 1
                cursor = conn.execute(
                    "INSERT INTO box_versions"
                    " (batch_id, box_no, version, item_count, digest, imported_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (batch_id, box_no, version_no, len(items), digest, stamp),
                )
                version_id = cursor.lastrowid
                conn.executemany(
                    "INSERT INTO box_items (version_id, archive_id, title, retention)"
                    " VALUES (?, ?, ?, ?)",
                    [(version_id, i.archive_id, i.title, i.retention) for i in items],
                )
                conn.execute(
                    "INSERT INTO box_imports"
                    " (batch_id, box_no, version, previous_version, item_count,"
                    "  digest, source, imported_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        batch_id,
                        box_no,
                        version_no,
                        None if current is None else current["version"],
                        len(items),
                        digest,
                        source,
                        stamp,
                    ),
                )
                # Content changed: withdraw the old conclusion and, when the
                # old-side detail is available, immediately recompute it from
                # the new content.
                self._recompute(conn, batch_id, box_no)
                results[box_no] = {
                    "box_no": box_no,
                    "version": version_no,
                    "created": True,
                    "item_count": len(items),
                    "digest": digest,
                }
        return results

    # ------------------------------------------------------------------
    # old-side detail + comparison
    # ------------------------------------------------------------------

    def submit_detail(
        self,
        batch_id: int,
        box_no: str,
        items: list[Item],
        *,
        expected_version_id: int,
    ) -> dict:
        """Validate and store a returned detail, then compare the box.

        Raises :class:`ManifestMismatchError` when count/digest do not match the
        fixed manifest and :class:`StaleBoxError` when the new-archive content
        changed since ``expected_version_id`` was read.  Both leave all existing
        data untouched.
        """

        box_no = clean_text(box_no)
        stamp = now()
        with self._transaction() as conn:
            self._require_open(conn, batch_id)
            manifest_row = conn.execute(
                "SELECT * FROM manifest WHERE batch_id = ? AND box_no = ?",
                (batch_id, box_no),
            ).fetchone()
            if manifest_row is None:
                raise NotFoundError(
                    f"盒 {box_no} 不在旧库清单中，无须回旧库调明细。"
                )
            manifest = dict(manifest_row)

            current = self._current_version_row(conn, batch_id, box_no)
            current_id = 0 if current is None else current["id"]
            if current_id != expected_version_id:
                raise StaleBoxError(
                    box_no,
                    expected_version_id,
                    None if current is None else current["version"],
                )

            digest, problem, summary = verify_detail_against_manifest(
                box_no, items, manifest
            )
            if problem:
                raise ManifestMismatchError(box_no, problem, summary)

            existing = conn.execute(
                "SELECT id FROM old_details WHERE batch_id = ? AND box_no = ?",
                (batch_id, box_no),
            ).fetchone()
            if existing is not None:
                conn.execute("DELETE FROM old_details WHERE id = ?", (existing["id"],))
            cursor = conn.execute(
                "INSERT INTO old_details"
                " (batch_id, box_no, item_count, digest, received_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (batch_id, box_no, len(items), digest, stamp),
            )
            detail_id = cursor.lastrowid
            conn.executemany(
                "INSERT INTO old_detail_items (detail_id, archive_id, title, retention)"
                " VALUES (?, ?, ?, ?)",
                [(detail_id, i.archive_id, i.title, i.retention) for i in items],
            )
            reconciliation = self._recompute(conn, batch_id, box_no)
        return {
            "box_no": box_no,
            "detail_count": len(items),
            "detail_digest": digest,
            "outcome": reconciliation["outcome"],
            "summary": reconciliation["summary"],
        }

    def finalize_box(
        self,
        batch_id: int,
        box_no: str,
        *,
        expected_version_id: int,
    ) -> None:
        box_no = clean_text(box_no)
        with self._transaction() as conn:
            self._require_open(conn, batch_id)
            current = self._current_version_row(conn, batch_id, box_no)
            current_id = 0 if current is None else current["id"]
            if current_id != expected_version_id:
                raise StaleBoxError(
                    box_no,
                    expected_version_id,
                    None if current is None else current["version"],
                )
            row = conn.execute(
                "SELECT * FROM reconciliations WHERE batch_id = ? AND box_no = ?",
                (batch_id, box_no),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"盒 {box_no} 还没有可确认的核对结论。")
            conn.execute(
                "UPDATE reconciliations SET finalized_at = ?, updated_at = ?"
                " WHERE id = ?",
                (now(), now(), row["id"]),
            )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @staticmethod
    def _current_version_row(
        conn: sqlite3.Connection, batch_id: int, box_no: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM box_versions WHERE batch_id = ? AND box_no = ?"
            " ORDER BY version DESC LIMIT 1",
            (batch_id, box_no),
        ).fetchone()

    def _recompute(
        self, conn: sqlite3.Connection, batch_id: int, box_no: str
    ) -> dict:
        """Delete the box conclusion and rebuild it from current data."""

        conn.execute(
            "DELETE FROM reconciliations WHERE batch_id = ? AND box_no = ?",
            (batch_id, box_no),
        )
        manifest_row = conn.execute(
            "SELECT * FROM manifest WHERE batch_id = ? AND box_no = ?",
            (batch_id, box_no),
        ).fetchone()
        if manifest_row is None:
            # Box only exists in the new archive: listed as an extra box, no
            # conclusion row is needed.
            return {"outcome": "extra_box", "summary": {}}
        detail_row = conn.execute(
            "SELECT * FROM old_details WHERE batch_id = ? AND box_no = ?",
            (batch_id, box_no),
        ).fetchone()
        if detail_row is None:
            return {"outcome": "need_detail", "summary": {}}

        version_row = self._current_version_row(conn, batch_id, box_no)
        new_items = (
            self._version_items(conn, version_row["id"], box_no) if version_row else []
        )
        old_items = self._detail_items(conn, detail_row["id"], box_no)
        comparison = compare_box(
            box_no,
            new_items,
            old_items,
            {
                "item_count": manifest_row["item_count"],
                "digest": manifest_row["digest"],
            },
        )
        stamp = now()
        cursor = conn.execute(
            "INSERT INTO reconciliations"
            " (batch_id, box_no, version, version_id, detail_id, detail_count,"
            "  detail_digest, outcome, summary_json, created_at, updated_at, finalized_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                batch_id,
                box_no,
                None if version_row is None else version_row["version"],
                None if version_row is None else version_row["id"],
                detail_row["id"],
                detail_row["item_count"],
                detail_row["digest"],
                comparison.outcome,
                json.dumps(comparison.summary, ensure_ascii=False, sort_keys=True),
                stamp,
                stamp,
            ),
        )
        reconciliation_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO differences"
            " (reconciliation_id, archive_id, kind, new_title, new_retention,"
            "  old_title, old_retention, changes_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    reconciliation_id,
                    d["archive_id"],
                    d["kind"],
                    d["new_title"],
                    d["new_retention"],
                    d["old_title"],
                    d["old_retention"],
                    json.dumps(d["changes"], ensure_ascii=False),
                )
                for d in comparison.differences
            ],
        )
        row = conn.execute(
            "SELECT * FROM reconciliations WHERE id = ?", (reconciliation_id,)
        ).fetchone()
        return _reconciliation(row)

    @staticmethod
    def _version_items(
        conn: sqlite3.Connection, version_id: int, box_no: str
    ) -> list[Item]:
        rows = conn.execute(
            "SELECT archive_id, title, retention FROM box_items WHERE version_id = ?",
            (version_id,),
        ).fetchall()
        return [Item(box_no, r["archive_id"], r["title"], r["retention"]) for r in rows]

    @staticmethod
    def _detail_items(
        conn: sqlite3.Connection, detail_id: int, box_no: str
    ) -> list[Item]:
        rows = conn.execute(
            "SELECT archive_id, title, retention FROM old_detail_items WHERE detail_id = ?",
            (detail_id,),
        ).fetchall()
        return [Item(box_no, r["archive_id"], r["title"], r["retention"]) for r in rows]


def _batch(row: sqlite3.Row) -> Batch:
    return Batch(
        id=row["id"],
        label=row["label"],
        status=row["status"],
        created_at=row["created_at"],
        finalized_at=row["finalized_at"],
    )


def _version(row: sqlite3.Row) -> BoxVersion:
    return BoxVersion(
        id=row["id"],
        batch_id=row["batch_id"],
        box_no=row["box_no"],
        version=row["version"],
        item_count=row["item_count"],
        digest=row["digest"],
        imported_at=row["imported_at"],
    )


def _detail(row: sqlite3.Row) -> Detail:
    return Detail(
        id=row["id"],
        batch_id=row["batch_id"],
        box_no=row["box_no"],
        item_count=row["item_count"],
        digest=row["digest"],
        received_at=row["received_at"],
    )


def _reconciliation(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["summary"] = json.loads(result.pop("summary_json") or "{}")
    result["finalized"] = result["finalized_at"] is not None
    return result
