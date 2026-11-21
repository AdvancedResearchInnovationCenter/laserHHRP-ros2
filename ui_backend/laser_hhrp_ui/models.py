"""Validated values, enums and the detector-result -> UI-feature contract (plan sections 5, 7, 8).

Pure Python: no FastAPI/pydantic types, so it runs with any installed version (laptop pip, Pi apt).
"""

from __future__ import annotations

import math
import re
from enum import Enum


class ValidationError(ValueError):
    """Bad command input: HTTP 400 with the offending field."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


class Mode(str, Enum):
    CSK_DEPTH = "csk_depth"
    RIVET_FLUSHNESS = "rivet_flushness"


# One allowlisted detector executable per mode (plan section 6).
DETECTOR = {Mode.CSK_DEPTH: "hole_detector", Mode.RIVET_FLUSHNESS: "rivet_detector"}
# feature_type values each detector emits, and which of them count toward detected/expected.
KNOWN_TYPES = {Mode.CSK_DEPTH: {"countersink", "hole"}, Mode.RIVET_FLUSHNESS: {"rivet"}}
TARGET_TYPES = {Mode.CSK_DEPTH: {"countersink"}, Mode.RIVET_FLUSHNESS: {"rivet"}}
MEASUREMENT = {Mode.CSK_DEPTH: ("csk_depth", "countersink_depth_m"),
               Mode.RIVET_FLUSHNESS: ("flushness", "flushness_m")}
# detail key holding the feature's top/head diameter
DIAMETER_KEY = {"countersink": "d_top_mm", "hole": "d_top_mm", "rivet": "d_head_mm"}
COVERAGE_KEY = {"countersink": "ring_coverage", "hole": "ring_coverage", "rivet": "edge_coverage"}


class Acquisition(str, Enum):
    SCANNING = "scanning"
    PAUSED = "paused"
    ENDED = "ended"          # confirmed stop
    ERROR = "error"          # acquisition terminated by a hardware/controller error
    INTERRUPTED = "interrupted"  # backend restarted while the scan was acquiring


class Analysis(str, Enum):
    PENDING = "pending"
    COMPLETE = "complete"
    FAILED = "failed"
    INTERRUPTED = "interrupted"  # backend restarted before analysis finished


class Decision(str, Enum):
    ACCEPTED = "accepted"
    RESCANNED = "rescanned"
    INCOMPLETE_RETAINED = "incomplete_retained"


class Screen(str, Enum):
    IDENTIFY = "identify"
    SETUP = "setup"
    NOT_READY = "not_ready"
    READY = "ready"
    SCANNING = "scanning"
    FINISHING_ANALYSIS = "finishing_analysis"
    RESULTS = "results"
    ANALYSIS_FAILED = "analysis_failed"


ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
EXPECTED_MIN, EXPECTED_MAX = 1, 500


def _ident(field: str, value, max_len: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(field, f"{field} must be a string")
    value = value.strip()
    if not 1 <= len(value) <= max_len or not ID_RE.match(value):
        raise ValidationError(field, f"{field}: 1..{max_len} letters, digits, '_' or '-'")
    return value


def operator_id(value) -> str:
    return _ident("operator_id", value, 32)


def inspection_label(value) -> str:
    return _ident("label", value, 64)


def expected_count(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("count", "count must be an integer")
    if not EXPECTED_MIN <= value <= EXPECTED_MAX:
        raise ValidationError("count", f"count must be {EXPECTED_MIN}..{EXPECTED_MAX}")
    return value


def mode(value) -> Mode:
    try:
        return Mode(value)
    except ValueError:
        raise ValidationError("mode", f"mode must be one of {[m.value for m in Mode]}") from None


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) else None


# Reason codes of the measurement algorithms (MEASUREMENT_INTEGRATION.md); the UI words them for the operator.
MEASURE_REASONS = ("too_few_points", "surface_not_flat", "unexpected_shape", "scan_distorted", "partial", "failed")
MAX_ABS_MEASUREMENT_M = 0.01     # a depth or flushness beyond 10 mm is not a measurement: the scan fails


def measurement_of(m: Mode, raw_value_m=None) -> dict:
    """The inspection metric at detection time. The detector's own value (-1) is never used: the measurement comes
    from inspection_node after the scan (`measured_value`); until then it is pending."""
    name, _ = MEASUREMENT[m]
    return {"name": name, "value_mm": None, "unit": "mm", "status": "pending", "reason": None, "verdict": None,
            "lower_mm": None, "upper_mm": None}


def measured_value(m: Mode, measured: bool, value_m, reason: str, lower_m: float, upper_m: float) -> dict:
    """A target feature's measurement from inspection_node, with its GO / NO-GO verdict against the scan's limits
    (inclusive). Raises ValidationError for a value that cannot be a measurement."""
    name, _ = MEASUREMENT[m]
    lo, hi = _num(lower_m), _num(upper_m)
    if lo is None or hi is None or lo > hi:
        raise ValidationError("limits", f"invalid limits {lower_m!r} .. {upper_m!r}")
    out = {"name": name, "unit": "mm", "lower_mm": round(lo * 1000.0, 6), "upper_mm": round(hi * 1000.0, 6)}
    if not measured:
        code = reason if reason in MEASURE_REASONS else "failed"
        return {**out, "value_mm": None, "status": "not_measured", "reason": code, "verdict": None}
    v = _num(value_m)
    if v is None or abs(v) > MAX_ABS_MEASUREMENT_M:
        raise ValidationError("value_m", f"implausible measurement {value_m!r} m")
    return {**out, "value_mm": round(v * 1000.0, 6), "status": "measured", "reason": None,
            "verdict": "in_limits" if lo <= v <= hi else "out_of_limits"}


def feature_from_result(m: Mode, feature_type: str, feature_id: int, position_m, detail: dict,
                        raw_measurement_m=None) -> dict:
    """UI feature from one detector result. Raises ValidationError for types the mode does not know.
    Geometry observations stay separate from the inspection measurement; `passed` is ignored."""
    if feature_type not in KNOWN_TYPES[m]:
        raise ValidationError("feature_type", f"{feature_type!r} is not produced in mode {m.value}")
    x, y, z = (float(v) * 1000.0 for v in position_m)
    return {
        "feature_id": int(feature_id),
        "kind": feature_type,
        "is_target": feature_type in TARGET_TYPES[m],
        "x_mm": round(x, 3), "y_mm": round(y, 3), "z_mm": round(z, 3),
        "diameter_mm": _num(detail.get(DIAMETER_KEY[feature_type])),
        # only the detector's own boolean; no UI-side threshold (plan section 9)
        "partial": detail.get("partial") is True,
        "quality": {"coverage": _num(detail.get(COVERAGE_KEY[feature_type])),
                    "scanned_share": _num(detail.get("scanned_share")),
                    "rms_mm": _num(detail.get("rms_mm"))},
        "measurement": measurement_of(m, raw_measurement_m),
    }
