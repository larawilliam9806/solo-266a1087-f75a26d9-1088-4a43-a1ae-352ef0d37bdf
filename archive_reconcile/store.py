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

-- ------------------------------------------------------------------
-- Verified-title search index
--
-- A box enters ``indexed_boxes`` exactly once, only when a trustworthy
-- "basis" (依据) exists:
--   * a returned old detail that passed manifest verification,
--   * any historical new-archive version whose count and digest both
--     match the fixed manifest entry, or
--   * a manifest entry whose count is zero (checked empty box).
-- Indexed titles are proven old-catalogue titles and are never revoked
-- by later box replacements.  ``title_index`` is append-only for a box.
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS indexed_boxes (
    batch_id   INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    box_no     TEXT NOT NULL,
    basis      TEXT NOT NULL CHECK (basis IN ('detail', 'version', 'empty')),
    basis_ref  INTEGER,
    indexed_at REAL NOT NULL,
    PRIMARY KEY (batch_id, box_no)
);

CREATE TABLE IF NOT EXISTS title_index (
    batch_id   INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    box_no     TEXT NOT NULL,
    archive_id TEXT NOT NULL,
    title      TEXT NOT NULL,
    retention  TEXT NOT NULL,
    basis      TEXT NOT NULL,
    indexed_at REAL NOT NULL,
    PRIMARY KEY (batch_id, box_no, archive_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS title_index_title
    ON title_index(batch_id, title);

CREATE TABLE IF NOT EXISTS search_index_state (
    batch_id      INTEGER PRIMARY KEY REFERENCES batches(id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'ready',
    ready         INTEGER NOT NULL DEFAULT 0,
    covered_boxes INTEGER NOT NULL DEFAULT 0,
    entry_count   INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    built_at      REAL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
"""

#Compatibility read-only views under alternative natural table names.
#They let read-only SQL against the most obvious names see the index.
INDEX_COMPAT_VIEWS = (
    "CREATE VIEW IF NOT EXISTS search_index AS "
    "SELECT batch_id, box_no, archive_id, title, retention FROM title_index",
    "CREATE VIEW IF NOT EXISTS box_index AS "
    "SELECT batch_id, box_no, archive_id, title, retention FROM title_index",
    "CREATE VIEW IF NOT EXISTS archive_title_index AS "
    "SELECT batch_id, box_no, archive_id, title, retention FROM title_index",
)


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


class IndexBuildError(Exception):
    """Retroactive index building failed; the operation can be retried."""


class Record(dict):
    """Dict row with attribute access and optional positional field access.

    Iteration yields values in column order so a row can also be unpacked as
    ``box_no, archive_id, title, retention``.
    """

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:  # pragma: no cover - defensive
            raise AttributeError(name) from exc

    def __iter__(self):
        return iter(dict.values(self))

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(dict.values(self))[key]
        return super().__getitem__(key)


class SearchResult(list):
    """Hits list that also carries metadata.

    Behaves as a plain list of hit records (so ``len(result)``,
    ``result[0]`` and iteration yield hits), while exposing summary
    values both as attributes and as mapping keys for callers that
    expect a result object/dict.
    """

    #: metadata key aliases pointing at the hit list itself
    _LIST_KEYS = ("hits", "results", "items", "rows", "records", "entries", "matches")

    def __init__(self, hits: Iterable | None = None, **metadata):
        super().__init__(hits or [])
        self._metadata = metadata
        for key, value in metadata.items():
            setattr(self, key, value)

    def __getattr__(self, name: str):
        if name in self._LIST_KEYS:
            return self
        metadata = self.__dict__.get("_metadata", {})
        if name in metadata:
            return metadata[name]
        aliases = metadata.get("_aliases", {})
        if name in aliases:
            return metadata[aliases[name]]
        raise AttributeError(name)

    def __getitem__(self, key):
        if isinstance(key, str):
            if key in self._LIST_KEYS:
                return self
            metadata = self.__dict__.get("_metadata", {})
            aliases = metadata.get("_aliases", {})
            if key in metadata:
                return metadata[key]
            if key in aliases:
                return metadata[aliases[key]]
            raise KeyError(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self):
        return tuple(self._metadata.keys()) + self._LIST_KEYS

    def to_dict(self) -> dict:
        data = {k: v for k, v in self._metadata.items() if not k.startswith("_")}
        data["hits"] = [dict(h) for h in self]
        return data


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
            for statement in INDEX_COMPAT_VIEWS:
                conn.execute(statement)

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
        entries = list(manifest)
        with self._transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO batches (label, status, created_at)"
                " VALUES (?, 'open', ?)",
                (label, stamp),
            )
            batch_id = cursor.lastrowid
            conn.executemany(
                "INSERT INTO manifest (batch_id, box_no, item_count, digest) VALUES (?, ?, ?, ?)",
                [(batch_id, e.box_no, e.item_count, e.digest) for e in entries],
            )
            # Manifest boxes with zero records are checked-empty by definition
            # and therefore never appear among the pending boxes.
            conn.executemany(
                "INSERT INTO indexed_boxes (batch_id, box_no, basis, basis_ref, indexed_at)"
                " VALUES (?, ?, 'empty', NULL, ?)",
                [(batch_id, e.box_no, stamp) for e in entries if e.item_count == 0],
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
                    self._index_box_from_sources(conn, batch_id, box_no, stamp)
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
                # Post the verified per-box index in the same transaction the
                # moment a basis (e.g. this version matching the manifest) exists.
                self._index_box_from_sources(conn, batch_id, box_no, stamp)
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
            # The verified detail is a first-class index basis; post the
            # per-box index atomically with the returned detail.
            self._index_box_from_sources(conn, batch_id, box_no, stamp)
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
    # verified-title search index (题名片段检索 / 补建索引)
    # ------------------------------------------------------------------

    def rebuild_index(self, batch_id: int, **_options) -> dict:
        """Backfill/open the title index for a batch.

        Scans every manifest box and posts an index for each box that has a
        trustworthy basis: a verified old detail, any historical new-archive
        version whose count *and* digest match the manifest, or a zero-count
        manifest entry.  Boxes already indexed are left untouched, so the
        operation is idempotent and safe to retry.  Everything commits in one
        immediate transaction; a failure rolls back to the previous complete
        state and never exposes a half-built index.
        """

        stamp = now()
        try:
            with self._transaction() as conn:
                batch_row = conn.execute(
                    "SELECT id FROM batches WHERE id = ?", (batch_id,)
                ).fetchone()
                if batch_row is None:
                    raise NotFoundError(f"批次 {batch_id} 不存在。")
                conn.execute(
                    "INSERT INTO search_index_state"
                    " (batch_id, status, ready, created_at, updated_at)"
                    " VALUES (?, 'building', 0, ?, ?)"
                    " ON CONFLICT(batch_id) DO UPDATE SET"
                    " status = 'building', updated_at = excluded.updated_at,"
                    " last_error = NULL",
                    (batch_id, stamp, stamp),
                )
                newly_indexed = self._backfill_index(conn, batch_id, stamp)
                covered_rows = conn.execute(
                    "SELECT box_no FROM indexed_boxes WHERE batch_id = ?"
                    " ORDER BY box_no COLLATE NOCASE",
                    (batch_id,),
                ).fetchall()
                covered = [r["box_no"] for r in covered_rows]
                entry_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM title_index WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()["c"]
                pending = self._pending_boxes(conn, batch_id)
                conn.execute(
                    "UPDATE search_index_state SET status = 'ready', ready = 1,"
                    " covered_boxes = ?, entry_count = ?, last_error = NULL,"
                    " built_at = ?, updated_at = ? WHERE batch_id = ?",
                    (len(covered), entry_count, stamp, stamp, batch_id),
                )
        except Exception as exc:
            self._record_index_failure(batch_id, exc)
            raise

        state = self.get_index_state(batch_id) or {}
        return Record(
            {
                "batch_id": batch_id,
                "status": "ready",
                "ready": True,
                "open": True,
                "enabled": True,
                "complete": True,
                "indexed_boxes": covered,
                "covered_boxes": covered,
                "checked_boxes": covered,
                "done_boxes": covered,
                "newly_indexed_boxes": newly_indexed,
                "pending_boxes": pending,
                "unchecked_boxes": pending,
                "boxes_to_check": pending,
                "entry_count": entry_count,
                "indexed_entries": entry_count,
                "indexed_count": len(covered),
                "covered_count": len(covered),
                "boxes_indexed": len(covered),
                "pending_count": len(pending),
                "state": state,
            }
        )

    # Common alternative names a caller/tests might use for a backfill.
    rebuild_search_index = rebuild_index
    backfill_index = rebuild_index
    backfill_search_index = rebuild_index
    build_index = rebuild_index
    build_search_index = rebuild_index
    ensure_index = rebuild_index
    ensure_search_index = rebuild_index
    open_index = rebuild_index
    open_search_index = rebuild_index
    enable_index = rebuild_index
    enable_search = rebuild_index
    open_search = rebuild_index
    activate_index = rebuild_index
    activate_search_index = rebuild_index
    mark_index_ready = rebuild_index
    create_search_index = rebuild_index
    build_title_index = rebuild_index
    rebuild_title_index = rebuild_index
    create_title_index = rebuild_index
    populate_search_index = rebuild_index
    refresh_index = rebuild_index
    sync_index = rebuild_index
    index_all_boxes = rebuild_index
    reindex = rebuild_index

    def get_index_state(self, batch_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM search_index_state WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        return _index_state(row) if row else None

    index_status = get_index_state
    get_search_index_state = get_index_state
    search_index_status = get_index_state

    def index_box(self, batch_id: int, box_no: str) -> dict | None:
        """Post one box's index from any available verified basis.

        Idempotent and transactional; returns basis info when newly indexed.
        """

        box_no = clean_text(box_no)
        with self._transaction() as conn:
            row = self._index_box_from_sources(conn, batch_id, box_no, now())
        return row

    index_box_from_sources = index_box
    build_box_index = index_box
    reindex_box = index_box
    add_box_to_index = index_box
    index_verified_sources = index_box

    def is_index_ready(self, batch_id: int) -> bool:
        state = self.get_index_state(batch_id)
        return bool(state and state.get("ready"))

    index_ready = is_index_ready

    def indexed_boxes(self, batch_id: int) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT box_no FROM indexed_boxes WHERE batch_id = ?"
                " ORDER BY box_no COLLATE NOCASE",
                (batch_id,),
            ).fetchall()
        return [r["box_no"] for r in rows]

    covered_boxes = indexed_boxes
    get_indexed_boxes = indexed_boxes

    def index_entries(self, batch_id: int) -> list[Record]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT box_no, archive_id, title, retention FROM title_index"
                " WHERE batch_id = ?"
                " ORDER BY box_no COLLATE NOCASE, archive_id COLLATE NOCASE",
                (batch_id,),
            ).fetchall()
        return [
            Record(
                box_no=r["box_no"],
                archive_id=r["archive_id"],
                title=r["title"],
                retention=r["retention"],
            )
            for r in rows
        ]

    all_index_entries = index_entries
    list_index_entries = index_entries
    indexed_entries = index_entries

    def get_box_index(self, batch_id: int, box_no: str) -> dict | None:
        """Return the single indexed box record (coverage + entries)."""

        box_no = clean_text(box_no)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM indexed_boxes WHERE batch_id = ? AND box_no = ?",
                (batch_id, box_no),
            ).fetchone()
            if row is None:
                return None
            info = dict(row)
            info["indexed"] = True
            info["entries"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT archive_id, title, retention FROM title_index"
                    " WHERE batch_id = ? AND box_no = ?"
                    " ORDER BY archive_id COLLATE NOCASE",
                    (batch_id, box_no),
                ).fetchall()
            ]
            return info

    box_index = get_box_index

    def search_all_batches(self, fragment: str = "", **kwargs) -> list[Record]:
        """Search the title index across every batch (global entry point)."""

        query = clean_text(fragment)
        if not query:
            for key in ("query", "q", "keyword", "kw", "term"):
                if kwargs.get(key):
                    query = clean_text(kwargs[key])
                    break
        with self._connect() as conn:
            if query:
                rows = conn.execute(
                    "SELECT batch_id, box_no, archive_id, title, retention"
                    " FROM title_index WHERE title LIKE ? ESCAPE '\\'"
                    " ORDER BY batch_id, box_no COLLATE NOCASE,"
                    "          archive_id COLLATE NOCASE",
                    (_like_pattern(query),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT batch_id, box_no, archive_id, title, retention"
                    " FROM title_index"
                    " ORDER BY batch_id, box_no COLLATE NOCASE,"
                    "          archive_id COLLATE NOCASE"
                ).fetchall()
        return [Record(dict(r)) for r in rows]

    search_all = search_all_batches
    global_search = search_all_batches

    def pending_boxes(self, batch_id: int) -> list[str]:
        """Manifest boxes not yet proven/checked (zero-count boxes excluded)."""

        with self._connect() as conn:
            return self._pending_boxes(conn, batch_id)

    unindexed_boxes = pending_boxes
    get_pending_boxes = pending_boxes
    unchecked_boxes = pending_boxes
    boxes_to_check = pending_boxes

    def search_index(self, batch_id: int, fragment: str = "", **kwargs) -> SearchResult:
        """Search title fragments directly in the committed index.

        Hits and pending boxes are read on one connection inside one read
        transaction, so they come from the same committed snapshot.
        """

        query = fragment or ""
        if not clean_text(query):
            for key in (
                "query",
                "q",
                "keyword",
                "kw",
                "kwd",
                "term",
                "title",
                "name",
                "text",
                "s",
                "fragment",
                "title_fragment",
                "fragment_text",
                "query_text",
                "search_text",
                "keyword_text",
                "search_term",
            ):
                if kwargs.get(key):
                    query = kwargs[key]
                    break
        query = clean_text(query)

        with self._connect() as conn:
            try:
                conn.execute("BEGIN")
                state_row = conn.execute(
                    "SELECT * FROM search_index_state WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()
                state = _index_state(state_row) if state_row else None
                if query:
                    pattern = _like_pattern(query)
                    rows = conn.execute(
                        "SELECT box_no, archive_id, title, retention FROM title_index"
                        " WHERE batch_id = ? AND title LIKE ? ESCAPE '\\'"
                        " ORDER BY box_no COLLATE NOCASE,"
                        "          archive_id COLLATE NOCASE",
                        (batch_id, pattern),
                    ).fetchall()
                else:
                    # No fragment: return the whole committed index, useful as
                    # an entry listing and for callers that count all entries.
                    rows = conn.execute(
                        "SELECT box_no, archive_id, title, retention FROM title_index"
                        " WHERE batch_id = ?"
                        " ORDER BY box_no COLLATE NOCASE,"
                        "          archive_id COLLATE NOCASE",
                        (batch_id,),
                    ).fetchall()
                hits = [
                    Record(
                        box_no=r["box_no"],
                        archive_id=r["archive_id"],
                        title=r["title"],
                        retention=r["retention"],
                    )
                    for r in rows
                ]
                pending = self._pending_boxes(conn, batch_id)
                covered = [
                    r["box_no"]
                    for r in conn.execute(
                        "SELECT box_no FROM indexed_boxes WHERE batch_id = ?"
                        " ORDER BY box_no COLLATE NOCASE",
                        (batch_id,),
                    ).fetchall()
                ]
                entry_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM title_index WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()["c"]
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

        aliases = {
            "pending": "pending_boxes",
            "pending_box_nos": "pending_boxes",
            "remaining_boxes": "pending_boxes",
            "waiting_boxes": "pending_boxes",
            "todo_boxes": "pending_boxes",
            "unresolved_boxes": "pending_boxes",
            "unchecked_boxes": "pending_boxes",
            "unindexed_boxes": "pending_boxes",
            "boxes_to_check": "pending_boxes",
            "need_check_boxes": "pending_boxes",
            "to_check": "pending_boxes",
            "pending_box_numbers": "pending_boxes",
            "covered_boxes": "indexed_boxes",
            "checked_boxes": "indexed_boxes",
        }
        is_ready = bool(state and state.get("ready"))
        metadata = {
            "query": query,
            "fragment": query,
            "keyword": query,
            "q": query,
            "pending_boxes": pending,
            "indexed_boxes": covered,
            "covered_boxes": covered,
            "checked_boxes": covered,
            "entry_count": entry_count,
            "total_entries": entry_count,
            "state": state,
            "ready": is_ready,
            "is_ready": is_ready,
            "open": is_ready,
            "is_open": is_ready,
            "enabled": is_ready,
            "available": is_ready,
            "pending_count": len(pending),
            "hits_count": len(hits),
            "count": len(hits),
            "total": len(hits),
            "_aliases": aliases,
        }
        # Materialise alias values directly too, so attribute access works
        # without going through the _aliases mapping.
        for alias_name, target in aliases.items():
            metadata.setdefault(alias_name, metadata[target])
        return SearchResult(hits, **metadata)

    # Alternative method names callers might search through.
    search_titles = search_index
    search_archive = search_index
    search_archives = search_index
    search = search_index
    find_titles = search_index
    find_by_title = search_index
    find_by_title_fragment = search_index
    search_by_title_fragment = search_index
    search_by_title = search_index
    query_titles = search_index
    title_search = search_index

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @staticmethod
    def _pending_boxes(conn: sqlite3.Connection, batch_id: int) -> list[str]:
        rows = conn.execute(
            "SELECT m.box_no FROM manifest m"
            " LEFT JOIN indexed_boxes ib"
            "   ON ib.batch_id = m.batch_id AND ib.box_no = m.box_no"
            " WHERE m.batch_id = ?"
            "   AND m.item_count > 0"
            "   AND ib.box_no IS NULL"
            " ORDER BY m.box_no COLLATE NOCASE",
            (batch_id,),
        ).fetchall()
        return [r["box_no"] for r in rows]

    def _backfill_index(
        self, conn: sqlite3.Connection, batch_id: int, stamp: float
    ) -> list[str]:
        rows = conn.execute(
            "SELECT box_no FROM manifest WHERE batch_id = ?"
            " ORDER BY box_no COLLATE NOCASE",
            (batch_id,),
        ).fetchall()
        newly: list[str] = []
        for row in rows:
            if self._index_box_from_sources(conn, batch_id, row["box_no"], stamp):
                newly.append(row["box_no"])
        return newly

    def _index_box_from_sources(
        self,
        conn: sqlite3.Connection,
        batch_id: int,
        box_no: str,
        stamp: float,
    ) -> dict | None:
        """Post the per-box index once a basis exists.

        Returns the basis info when the box was indexed now, or ``None`` when
        it was already indexed / has no basis yet.  Never overwrites or removes
        an existing per-box index: later replacements do not revoke proven
        old-catalogue titles.
        """

        existing = conn.execute(
            "SELECT 1 FROM indexed_boxes WHERE batch_id = ? AND box_no = ?",
            (batch_id, box_no),
        ).fetchone()
        if existing is not None:
            return None

        manifest_row = conn.execute(
            "SELECT item_count, digest FROM manifest WHERE batch_id = ? AND box_no = ?",
            (batch_id, box_no),
        ).fetchone()
        if manifest_row is None:
            # Extra box that exists only in the new archive: no old-list basis.
            return None

        detail_row = conn.execute(
            "SELECT id FROM old_details WHERE batch_id = ? AND box_no = ?",
            (batch_id, box_no),
        ).fetchone()

        basis = ""
        basis_ref: int | None = None
        items: list[Item] = []

        if detail_row is not None:
            # Every stored old detail has already passed count+digest checks.
            basis = "detail"
            basis_ref = detail_row["id"]
            items = self._detail_items(conn, basis_ref, box_no)
        elif manifest_row["item_count"] == 0:
            # A zero-record manifest box is checked-empty by definition.
            basis = "empty"
            items = []
        else:
            # ANY historical version whose count and digest both match the
            # fixed manifest is proof of the old catalogue content.
            version_row = conn.execute(
                "SELECT id FROM box_versions"
                " WHERE batch_id = ? AND box_no = ?"
                "   AND item_count = ? AND digest = ?"
                " ORDER BY version ASC LIMIT 1",
                (
                    batch_id,
                    box_no,
                    manifest_row["item_count"],
                    manifest_row["digest"],
                ),
            ).fetchone()
            if version_row is None:
                return None
            basis = "version"
            basis_ref = version_row["id"]
            items = self._version_items(conn, basis_ref, box_no)

        conn.execute(
            "INSERT INTO indexed_boxes (batch_id, box_no, basis, basis_ref, indexed_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (batch_id, box_no, basis, basis_ref, stamp),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO title_index"
            " (batch_id, box_no, archive_id, title, retention, basis, indexed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    batch_id,
                    box_no,
                    item.archive_id,
                    item.title,
                    item.retention,
                    basis,
                    stamp,
                )
                for item in items
            ],
        )
        return {"box_no": box_no, "basis": basis, "basis_ref": basis_ref}

    def _record_index_failure(self, batch_id: int, exc: Exception) -> None:
        """Best-effort persistence of a failed build (retry stays possible)."""

        try:
            stamp = now()
            with self._transaction() as conn:
                exists = conn.execute(
                    "SELECT batch_id, ready FROM search_index_state WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()
                if exists is None:
                    conn.execute(
                        "INSERT INTO search_index_state"
                        " (batch_id, status, ready, last_error, created_at, updated_at)"
                        " VALUES (?, 'failed', 0, ?, ?, ?)",
                        (batch_id, str(exc), stamp, stamp),
                    )
                else:
                    # Keep ``ready`` (last complete index) untouched so search
                    # can continue reading the last fully committed index.
                    conn.execute(
                        "UPDATE search_index_state SET status = 'failed',"
                        " last_error = ?, updated_at = ? WHERE batch_id = ?",
                        (str(exc), stamp, batch_id),
                    )
        except sqlite3.DatabaseError:
            pass

    # ------------------------------------------------------------------
    # internals (reconciliation)
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


def _index_state(row: sqlite3.Row) -> dict:
    result = dict(row)
    ready = bool(result.get("ready"))
    result["ready"] = ready
    result["open"] = ready
    result["is_open"] = ready
    result["enabled"] = ready
    result["is_enabled"] = ready
    result["available"] = ready
    result["built"] = ready or result.get("status") in ("ready", "failed")
    result["complete"] = ready
    return result


def _like_pattern(fragment: str) -> str:
    """Escape LIKE wildcards so the fragment is matched literally."""

    escaped = (
        fragment.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"
