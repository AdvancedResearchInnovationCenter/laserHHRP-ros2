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
    assert f["measurement"]["status"] == "pending" and f["measurement"]["value_mm"] is None
    hole = models.feature_from_result(Mode.CSK_DEPTH, "hole", 8, (0, 0, 0), {"d_top_mm": 4.8})
    assert not hole["is_target"]
    with pytest.raises(ValidationError):
        models.feature_from_result(Mode.RIVET_FLUSHNESS, "countersink", 1, (0, 0, 0), {})


@pytest.mark.parametrize("flag, share, expected", [(True, 0.95, True), (False, 0.5, False), (None, 0.4, False)])
def test_partial_is_the_detector_flag_not_a_ui_threshold(flag, share, expected):
    detail = {"partial": flag, "scanned_share": share, "ring_coverage": 0.9}
    f = models.feature_from_result(Mode.CSK_DEPTH, "countersink", 1, (0, 0, 0), detail)
    assert f["partial"] is expected and f["quality"]["scanned_share"] == share


def test_detector_value_is_never_the_measurement():
    """The detector's own number (or its -1) is ignored: the value comes from inspection_node after the scan."""
    f = models.feature_from_result(Mode.RIVET_FLUSHNESS, "rivet", 1, (0, 0, 0), {"height_mm": 0.04}, 0.00004)
    assert f["measurement"] == {"name": "flushness", "value_mm": None, "unit": "mm", "status": "pending",
                                "reason": None, "verdict": None, "lower_mm": None, "upper_mm": None}


@pytest.mark.parametrize("mode, measured, value, reason, verdict", [
    (Mode.CSK_DEPTH, True, 0.000947, "", "in_limits"),          # both ends inclusive
    (Mode.CSK_DEPTH, True, 0.001147, "", "in_limits"),
    (Mode.CSK_DEPTH, True, 0.0011471, "", "out_of_limits"),
    (Mode.CSK_DEPTH, True, 0.0009, "", "out_of_limits"),
    (Mode.RIVET_FLUSHNESS, True, -0.00015, "", "in_limits"),
    (Mode.RIVET_FLUSHNESS, True, 0.00006, "", "out_of_limits"),   # proud beyond +0.05 mm
    (Mode.RIVET_FLUSHNESS, True, -0.0002, "", "out_of_limits"),   # deeper than -0.15 mm
])
def test_measured_value_verdict_against_inclusive_limits(mode, measured, value, reason, verdict):
    lo, hi = (0.000947, 0.001147) if mode == Mode.CSK_DEPTH else (-0.00015, 0.00005)
    m = models.measured_value(mode, measured, value, reason, lo, hi)
    assert m["status"] == "measured" and m["verdict"] == verdict and m["reason"] is None
    assert m["value_mm"] == round(value * 1000, 6) and (m["lower_mm"], m["upper_mm"]) == (round(lo * 1000, 6), round(hi * 1000, 6))


def test_not_measured_keeps_the_reason_code_and_no_verdict():
    m = models.measured_value(Mode.RIVET_FLUSHNESS, False, 0.0, "scan_distorted", -0.00015, 0.00005)
    assert (m["status"], m["reason"], m["value_mm"], m["verdict"]) == ("not_measured", "scan_distorted", None, None)
    unknown = models.measured_value(Mode.CSK_DEPTH, False, 0.0, "something new", 0.000947, 0.001147)
    assert unknown["reason"] == "failed"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0.02, -0.5, "1", None, True])
def test_implausible_values_are_refused(value):
    with pytest.raises(ValidationError):
        models.measured_value(Mode.CSK_DEPTH, True, value, "", 0.000947, 0.001147)


@pytest.mark.parametrize("lo, hi", [(0.002, 0.001), (float("nan"), 0.001), (0.0, float("inf"))])
def test_invalid_limits_are_refused(lo, hi):
    with pytest.raises(ValidationError):
        models.measured_value(Mode.CSK_DEPTH, True, 0.001, "", lo, hi)


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
