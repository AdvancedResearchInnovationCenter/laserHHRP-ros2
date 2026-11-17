"""Validation, the detector-result contract and SQLite persistence."""

import sqlite3

import pytest

from laser_hhrp_ui import models
from laser_hhrp_ui.models import Mode, ValidationError
from laser_hhrp_ui.repository import MIGRATIONS, Repository


@pytest.mark.parametrize("value", ["ENG-1042", "a", "x" * 32, "op_7"])
def test_operator_ids_ok(value):
    assert models.operator_id(value) == value


@pytest.mark.parametrize("value", ["", "x" * 33, "eng 1", "ä", None, 42, "a/b"])
def test_operator_ids_rejected(value):
    with pytest.raises(ValidationError):
        models.operator_id(value)


def test_feature_contract_csk():
    detail = {"id": 7, "d_top_mm": 9.46, "rms_mm": 0.012, "ring_coverage": 0.93, "countersunk": True,
              "partial": "yes", "scanned_share": 1.0}
    f = models.feature_from_result(Mode.CSK_DEPTH, "countersink", 7, (0.0121, 0.1035, 0.0002), detail, -1.0)
    assert f["is_target"] and (f["x_mm"], f["y_mm"]) == (12.1, 103.5) and f["diameter_mm"] == 9.46
    assert f["partial"] is False                     # only a real boolean true counts
    assert f["measurement"]["status"] == "not_measured" and f["measurement"]["value_mm"] is None
    hole = models.feature_from_result(Mode.CSK_DEPTH, "hole", 8, (0, 0, 0), {"d_top_mm": 4.8})
    assert not hole["is_target"]
    with pytest.raises(ValidationError):
        models.feature_from_result(Mode.RIVET_FLUSHNESS, "countersink", 1, (0, 0, 0), {})


@pytest.mark.parametrize("flag, share, expected", [(True, 0.95, True), (False, 0.5, False), (None, 0.4, False)])
def test_partial_is_the_detector_flag_not_a_ui_threshold(flag, share, expected):
    detail = {"partial": flag, "scanned_share": share, "ring_coverage": 0.9}
    f = models.feature_from_result(Mode.CSK_DEPTH, "countersink", 1, (0, 0, 0), detail)
    assert f["partial"] is expected and f["quality"]["scanned_share"] == share


def test_measurement_never_reports_a_value_even_if_detector_sends_one():
    f = models.feature_from_result(Mode.RIVET_FLUSHNESS, "rivet", 1, (0, 0, 0), {"height_mm": 0.04}, 0.00004)
    assert f["measurement"] == {"name": "flushness", "value_mm": None, "unit": "mm", "status": "not_measured"}


def test_migrations_idempotent_wal_and_constraints(tmp_path):
    db = tmp_path / "x.db"
    r = Repository(db)
    assert r.schema_version == len(MIGRATIONS)
    assert r.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert r.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    r.close()
    r = Repository(db)                               # reopening does not re-run migrations
    assert r.schema_version == len(MIGRATIONS)
    with pytest.raises(sqlite3.IntegrityError):      # feature of an unknown scan
        r.db.execute("INSERT INTO features VALUES ('nope','csk_depth',1,0,'hole',0,0,0,0,NULL,0,'{}','{}','{}','t')")
    with pytest.raises(sqlite3.IntegrityError):
        r.db.execute("INSERT INTO inspections(uuid,label,label_key,mode,operator_session_id,operator_id,"
                     "status,created_at,clock_quality) VALUES ('u','l','l','other',1,'o','open','t','synced')")


def test_one_open_inspection_enforced_by_schema(tmp_path):
    r = Repository(tmp_path / "x.db")
    s = r.open_operator_session("op", "t", "synced")
    row = dict(mode="csk_depth", operator_session_id=s["id"], operator_id="op", status="open",
               created_at="t", clock_quality="synced")
    r.create_inspection(uuid="a", label="A", **row)
    with pytest.raises(sqlite3.IntegrityError):
        r.create_inspection(uuid="b", label="B", **row)


def test_failed_transaction_rolls_back(tmp_path):
    r = Repository(tmp_path / "x.db")
    with pytest.raises(RuntimeError):
        with r.tx():
            r.db.execute("INSERT INTO operator_sessions(operator_id, started_at, clock_quality) "
                         "VALUES ('x', 't', 'synced')")
            raise RuntimeError("boom")
    assert r.open_operator_session_row() is None
