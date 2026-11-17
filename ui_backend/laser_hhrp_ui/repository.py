"""SQLite persistence: the canonical inspection history (plan section 8).

Versioned migrations (PRAGMA user_version), foreign keys, WAL, explicit transactions. Used from one
thread (the asyncio loop); ROS callbacks never touch it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS = [
    # 1: initial schema
    """
    CREATE TABLE operator_sessions (
        id            INTEGER PRIMARY KEY,
        operator_id   TEXT NOT NULL,
        started_at    TEXT NOT NULL,
        ended_at      TEXT,
        clock_quality TEXT NOT NULL
    );
    CREATE TABLE inspections (
        uuid                TEXT PRIMARY KEY,
        label               TEXT NOT NULL,
        label_key           TEXT NOT NULL UNIQUE,          -- lower(label): case-insensitive unique
        mode                TEXT NOT NULL CHECK (mode IN ('csk_depth', 'rivet_flushness')),
        operator_session_id INTEGER NOT NULL REFERENCES operator_sessions(id),
        operator_id         TEXT NOT NULL,
        next_expected_count INTEGER CHECK (next_expected_count BETWEEN 1 AND 500),
        status              TEXT NOT NULL CHECK (status IN ('open', 'closed')),
        created_at          TEXT NOT NULL,
        closed_at           TEXT,
        clock_quality       TEXT NOT NULL,
        create_request_id   TEXT UNIQUE,
        last_scan_number    INTEGER NOT NULL DEFAULT 0
    );
    CREATE UNIQUE INDEX one_open_inspection ON inspections(status) WHERE status = 'open';
    CREATE TABLE scans (
        scan_id                TEXT PRIMARY KEY,               -- ROS UUID
        inspection_uuid        TEXT REFERENCES inspections(uuid),  -- NULL: unassigned
        scan_number            INTEGER,
        unassigned_reason      TEXT,
        operator_id            TEXT,
        mode                   TEXT,
        expected_count         INTEGER,
        controller_instance_id TEXT NOT NULL,
        transition_seq         INTEGER NOT NULL,
        start_stamp_ns         INTEGER NOT NULL DEFAULT 0,
        started_at             TEXT NOT NULL,
        ended_at               TEXT,
        clock_quality          TEXT NOT NULL,
        acquisition            TEXT NOT NULL,
        stop_state             TEXT,
        acquisition_note       TEXT,
        published_frame_count  INTEGER,
        emitted_result_count   INTEGER,
        detected_target_count  INTEGER,
        analysis               TEXT NOT NULL,
        analysis_error         TEXT,
        analysis_completed_at  TEXT,
        decision               TEXT CHECK (decision IN ('accepted', 'rescanned', 'incomplete_retained')),
        decided_by             TEXT,
        decided_at             TEXT,
        count_mismatch         INTEGER,
        raw_session_id         TEXT,
        raw_dir                TEXT,
        replay_eligible        INTEGER NOT NULL DEFAULT 0,
        UNIQUE (inspection_uuid, scan_number)
    );
    CREATE INDEX scans_by_inspection ON scans(inspection_uuid, scan_number);
    CREATE TABLE features (
        scan_id              TEXT NOT NULL REFERENCES scans(scan_id),
        detector_mode        TEXT NOT NULL,
        feature_id           INTEGER NOT NULL,
        result_seq           INTEGER NOT NULL,
        kind                 TEXT NOT NULL,
        is_target            INTEGER NOT NULL,
        x_mm REAL NOT NULL, y_mm REAL NOT NULL, z_mm REAL NOT NULL,
        diameter_mm          REAL,
        partial              INTEGER NOT NULL,
        quality_json         TEXT NOT NULL,
        measurement_json     TEXT NOT NULL,
        detail_json          TEXT NOT NULL,                 -- original detector detail (provenance)
        detected_at          TEXT NOT NULL,
        PRIMARY KEY (scan_id, detector_mode, feature_id),
        UNIQUE (scan_id, result_seq)
    );
    CREATE TABLE audit_events (
        id              INTEGER PRIMARY KEY,
        at              TEXT NOT NULL,
        clock_quality   TEXT NOT NULL,
        kind            TEXT NOT NULL,
        inspection_uuid TEXT,
        scan_id         TEXT,
        actor           TEXT,
        detail_json     TEXT NOT NULL
    );
    """,
    # 2: raw recording link (Phase 3): stop transition stamp, recorder session state, verification result
    """
    ALTER TABLE scans ADD COLUMN stop_stamp_ns INTEGER;
    ALTER TABLE scans ADD COLUMN raw_frame_count INTEGER;
    ALTER TABLE scans ADD COLUMN raw_state TEXT;
    ALTER TABLE scans ADD COLUMN raw_note TEXT
    """,
]

SCAN_FIELDS = {
    "inspection_uuid", "scan_number", "unassigned_reason", "operator_id", "mode", "expected_count",
    "ended_at", "acquisition", "stop_state", "acquisition_note", "published_frame_count",
    "emitted_result_count", "detected_target_count", "analysis", "analysis_error",
    "analysis_completed_at", "decision", "decided_by", "decided_at", "count_mismatch",
    "raw_session_id", "raw_dir", "replay_eligible", "stop_stamp_ns", "raw_frame_count", "raw_state", "raw_note"}


def default_db_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "laser_hhrp" / "ui" / "inspections.db"


class Repository:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
            self.db.execute("PRAGMA synchronous = FULL")   # an acknowledged write survives power loss
        self._migrate()

    # ---- infrastructure ----
    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            with self.tx():
                for statement in (s.strip() for s in script.split(";")):
                    if statement:
                        self.db.execute(statement)
                self.db.execute(f"PRAGMA user_version = {number}")

    @property
    def schema_version(self) -> int:
        return self.db.execute("PRAGMA user_version").fetchone()[0]

    @contextmanager
    def tx(self):
        """One transaction; nested use joins the outer one."""
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def health(self) -> dict:
        try:
            self.db.execute("SELECT 1").fetchone()
            writable = self.path == ":memory:" or os.access(self.path, os.W_OK)
            return {"ok": bool(writable), "path": self.path, "schema_version": self.schema_version}
        except sqlite3.Error as e:
            return {"ok": False, "path": self.path, "error": str(e)}

    def close(self) -> None:
        self.db.close()

    def _one(self, sql, *args):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql, *args):
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # ---- operator sessions ----
    def open_operator_session(self, operator_id: str, at: str, clock_quality: str) -> dict:
        with self.tx():
            cur = self.db.execute(
                "INSERT INTO operator_sessions(operator_id, started_at, clock_quality) VALUES (?, ?, ?)",
                (operator_id, at, clock_quality))
        return self._one("SELECT * FROM operator_sessions WHERE id = ?", cur.lastrowid)

    def close_operator_session(self, session_id: int, at: str) -> None:
        with self.tx():
            self.db.execute("UPDATE operator_sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                            (at, session_id))

    def open_operator_session_row(self) -> dict | None:
        return self._one("SELECT * FROM operator_sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1")

    # ---- inspections ----
    def create_inspection(self, **row) -> dict:
        row["label_key"] = row["label"].lower()
        cols = ", ".join(row)
        with self.tx():
            self.db.execute(f"INSERT INTO inspections({cols}) VALUES ({', '.join('?' * len(row))})",
                            tuple(row.values()))
        return self.inspection(row["uuid"])

    def inspection(self, uuid: str) -> dict | None:
        return self._one("SELECT * FROM inspections WHERE uuid = ?", uuid)

    def inspection_by_request(self, request_id: str) -> dict | None:
        return self._one("SELECT * FROM inspections WHERE create_request_id = ?", request_id)

    def open_inspection(self) -> dict | None:
        return self._one("SELECT * FROM inspections WHERE status = 'open'")

    def label_taken(self, label: str) -> bool:
        return self._one("SELECT 1 AS x FROM inspections WHERE label_key = ?", label.lower()) is not None

    def count_labels_like(self, prefix: str) -> int:
        return self._one("SELECT COUNT(*) AS n FROM inspections WHERE label LIKE ?", prefix + "%")["n"]

    def last_expected_count(self) -> int | None:
        row = self._one("SELECT next_expected_count AS n FROM inspections "
                        "WHERE next_expected_count IS NOT NULL ORDER BY created_at DESC, rowid DESC LIMIT 1")
        return row["n"] if row else None

    def update_inspection(self, uuid: str, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.tx():
            self.db.execute(f"UPDATE inspections SET {sets} WHERE uuid = ?", (*fields.values(), uuid))

    def list_inspections(self, limit: int = 50) -> list[dict]:
        return self._all("SELECT * FROM inspections ORDER BY rowid DESC LIMIT ?", limit)

    def allocate_scan_number(self, uuid: str) -> int:
        with self.tx():
            self.db.execute("UPDATE inspections SET last_scan_number = last_scan_number + 1 WHERE uuid = ?",
                            (uuid,))
            return self._one("SELECT last_scan_number AS n FROM inspections WHERE uuid = ?", uuid)["n"]

    # ---- scans ----
    def create_scan(self, **row) -> dict:
        cols = ", ".join(row)
        with self.tx():
            self.db.execute(f"INSERT INTO scans({cols}) VALUES ({', '.join('?' * len(row))})",
                            tuple(row.values()))
        return self.scan(row["scan_id"])

    def scan(self, scan_id: str) -> dict | None:
        return self._one("SELECT * FROM scans WHERE scan_id = ?", scan_id)

    def update_scan(self, scan_id: str, **fields) -> dict:
        bad = set(fields) - SCAN_FIELDS
        if bad:
            raise KeyError(f"not updatable: {bad}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.tx():
            self.db.execute(f"UPDATE scans SET {sets} WHERE scan_id = ?", (*fields.values(), scan_id))
        return self.scan(scan_id)

    def scans_of(self, inspection_uuid: str) -> list[dict]:
        return self._all("SELECT * FROM scans WHERE inspection_uuid = ? ORDER BY scan_number", inspection_uuid)

    def latest_scan_of(self, inspection_uuid: str) -> dict | None:
        return self._one("SELECT * FROM scans WHERE inspection_uuid = ? ORDER BY scan_number DESC LIMIT 1",
                         inspection_uuid)

    def scans_where(self, where: str, *args) -> list[dict]:
        return self._all(f"SELECT * FROM scans WHERE {where} ORDER BY rowid", *args)

    # ---- features ----
    def add_feature(self, scan_id: str, detector_mode: str, result_seq: int, feature: dict,
                    detail: dict, at: str) -> bool:
        """Insert one detection; False if this result (seq or feature id) is already stored."""
        try:
            with self.tx():
                self.db.execute(
                    "INSERT INTO features(scan_id, detector_mode, feature_id, result_seq, kind, is_target, "
                    "x_mm, y_mm, z_mm, diameter_mm, partial, quality_json, measurement_json, detail_json, "
                    "detected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (scan_id, detector_mode, feature["feature_id"], result_seq, feature["kind"],
                     int(feature["is_target"]), feature["x_mm"], feature["y_mm"], feature["z_mm"],
                     feature["diameter_mm"], int(feature["partial"]), json.dumps(feature["quality"]),
                     json.dumps(feature["measurement"]), json.dumps(detail, sort_keys=True), at))
            return True
        except sqlite3.IntegrityError:
            return False

    def same_feature(self, scan_id: str, detector_mode: str, result_seq: int, feature: dict, detail: dict) -> bool:
        """True only if the stored result with this sequence is identical (identity, geometry, detail)."""
        r = self._one("SELECT * FROM features WHERE scan_id = ? AND result_seq = ?", scan_id, result_seq)
        return bool(r) and (
            r["detector_mode"] == detector_mode and r["feature_id"] == feature["feature_id"]
            and r["kind"] == feature["kind"]
            and (r["x_mm"], r["y_mm"], r["z_mm"]) == (feature["x_mm"], feature["y_mm"], feature["z_mm"])
            and r["detail_json"] == json.dumps(detail, sort_keys=True))

    def features_of(self, scan_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM features WHERE scan_id = ? ORDER BY result_seq", scan_id)
        out = []
        for r in rows:
            out.append({
                "feature_id": r["feature_id"], "result_seq": r["result_seq"], "kind": r["kind"],
                "is_target": bool(r["is_target"]), "x_mm": r["x_mm"], "y_mm": r["y_mm"], "z_mm": r["z_mm"],
                "diameter_mm": r["diameter_mm"], "partial": bool(r["partial"]),
                "quality": json.loads(r["quality_json"]), "measurement": json.loads(r["measurement_json"]),
                "detected_at": r["detected_at"]})
        return out

    def feature_counts(self, scan_id: str) -> dict:
        row = self._one(
            "SELECT COUNT(*) AS total, COALESCE(SUM(is_target), 0) AS targets, "
            "COALESCE(SUM(is_target AND partial), 0) AS partial_targets, "
            "COALESCE(MAX(result_seq), -1) AS max_seq FROM features WHERE scan_id = ?", scan_id)
        return row

    # ---- audit ----
    def audit(self, kind: str, at: str, clock_quality: str, inspection_uuid=None, scan_id=None,
              actor=None, **detail) -> None:
        with self.tx():
            self.db.execute(
                "INSERT INTO audit_events(at, clock_quality, kind, inspection_uuid, scan_id, actor, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (at, clock_quality, kind, inspection_uuid, scan_id, actor, json.dumps(detail, sort_keys=True)))

    def audit_events(self, kind: str | None = None) -> list[dict]:
        if kind:
            return self._all("SELECT * FROM audit_events WHERE kind = ? ORDER BY id", kind)
        return self._all("SELECT * FROM audit_events ORDER BY id")
