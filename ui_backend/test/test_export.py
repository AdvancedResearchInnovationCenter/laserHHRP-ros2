"""CSV download and USB export (plan sections 7, 8, 11.13)."""

import csv
import io
import os

import pytest

from harness import Harness
from laser_hhrp_ui.export import COLUMNS, ExportError, UsbExporter, _text, inspection_csv


def rows_of(text):
    return list(csv.DictReader(io.StringIO(text)))


def inspection_with_history(h):
    insp = h.ready_inspection(label="WING-7", count=3)
    a = h.complete_scan(3)                                   # complete, 3 features
    h.engine.decide(a, "rescan")
    b = h.press()                                            # zero features, complete
    h.release(b)
    h.close(b, 100)
    h.finalize(b, 100)
    h.engine.decide(b, "accept")
    c = h.press()                                            # partial feature, then failed
    h.result(c, partial=True)
    h.release(c)
    h.close(c, 100)
    h.finalize(c, 100, success=False, error="missing frames: 3 of 100")
    return insp, a, b, c


def test_csv_lists_every_scan_and_feature_without_fake_measurement():
    h = Harness()
    insp, a, b, c = inspection_with_history(h)
    name, text = inspection_csv(h.engine, insp["uuid"])
    assert name == "WING-7.csv"
    assert text.splitlines()[0].split(",") == COLUMNS
    rows = rows_of(text)
    assert [r["scan_id"] for r in rows] == [a, a, a, b, c]
    assert [r["scan_status"] for r in rows] == ["rescanned"] * 3 + ["accepted_with_warning", "failed"]
    b_row = rows[3]
    assert b_row["detected_target_count"] == "0" and b_row["feature_id"] == "" and b_row["kind"] == ""
    assert rows[4]["partial"] == "true"
    assert rows[4]["analysis_error"] == "detector reported failure: missing frames: 3 of 100"
    for r in rows:
        if r["feature_id"]:
            assert r["measurement_status"] == "not_measured" and r["measurement_value_mm"] == ""
    assert not any("pass" in c for c in COLUMNS)
    assert rows[0]["x_mm"] == "12.0" and rows[1]["y_mm"] == "40.0"


def test_csv_unknown_inspection_is_404():
    from laser_hhrp_ui.state import CommandError
    with pytest.raises(CommandError) as e:
        inspection_csv(Harness().engine, "nope")
    assert e.value.status == 404


def test_text_cells_cannot_become_formulas():
    assert _text("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert _text("-1+1") == "'-1+1" and _text("ENG-1") == "ENG-1" and _text(None) == ""


@pytest.fixture
def usb(tmp_path):
    root = tmp_path / "media"
    stick = root / "eslam" / "STICK"
    stick.mkdir(parents=True)
    (root / "eslam" / "notmounted").mkdir()
    mounts = {str(stick)}
    return UsbExporter([root], is_mount=lambda p: p in mounts), root, stick, mounts


def test_usb_targets_only_lists_mounted_drives_below_roots(usb):
    exporter, _, stick, _ = usb
    t = exporter.targets()
    assert [x.path for x in t] == [str(stick)] and t[0].writable and t[0].name == "STICK"


def test_usb_write_never_overwrites(usb):
    exporter, _, stick, _ = usb
    p1 = exporter.write(str(stick), "WING-7", "a\r\n")
    p2 = exporter.write(str(stick), "WING-7", "b\r\n")
    assert p1.endswith("WING-7.csv") and p2.endswith("WING-7-2.csv")
    assert open(p1).read() == "a\n" and open(p2).read() == "b\n"
    assert sorted(os.listdir(stick)) == ["WING-7-2.csv", "WING-7.csv"]     # no temporary files left


def test_usb_write_refuses_unapproved_locations(usb, tmp_path):
    exporter, root, stick, mounts = usb
    outside = tmp_path / "home"
    outside.mkdir()
    mounts.add(str(outside))
    for bad, code in [(str(outside), "not_allowed"), (str(stick / ".." / ".." / ".." / "home"), "not_allowed"),
                      (str(root), "not_allowed"), (str(root / "eslam" / "notmounted"), "not_mounted"),
                      ("", "invalid"), (None, "invalid")]:
        with pytest.raises(ExportError) as e:
            exporter.write(bad, "X", "x")
        assert e.value.code == code, bad
    with pytest.raises(ExportError) as e:
        exporter.write(str(stick), "../evil", "x")
    assert e.value.code == "invalid"
    assert os.listdir(outside) == [] and os.listdir(stick) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_usb_write_refuses_read_only_drive(usb):
    exporter, _, stick, _ = usb
    stick.chmod(0o555)
    try:
        assert not exporter.targets()[0].writable
        with pytest.raises(ExportError) as e:
            exporter.write(str(stick), "X", "x")
        assert e.value.code == "not_writable"
    finally:
        stick.chmod(0o755)


# ---- over HTTP ----
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from laser_hhrp_ui.clock import Clock  # noqa: E402
from laser_hhrp_ui.server import build  # noqa: E402

P = "/api/v1"


@pytest.fixture
def client_with_history(tmp_path, usb):
    db = tmp_path / "ui.db"
    h = Harness(path=str(db))
    insp, *_ = inspection_with_history(h)
    h.repo.close()
    app, engine, *_ = build(db, clock=Clock("synced"), usb=usb[0])
    with TestClient(app) as client:
        yield client, engine, insp, usb


def test_http_csv_download_has_no_side_effects(client_with_history):
    client, engine, insp, (_, root, stick, _) = client_with_history
    audits = len(engine.repo.audit_events())
    r = client.get(P + f"/inspections/{insp['uuid']}/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == 'attachment; filename="WING-7.csv"'
    assert len(rows_of(r.text)) == 5
    assert len(engine.repo.audit_events()) == audits and os.listdir(stick) == []
    assert client.get(P + "/inspections/nope/export.csv").status_code == 404


def test_http_usb_export_writes_and_audits(client_with_history):
    client, engine, insp, (_, root, stick, _) = client_with_history
    t = client.get(P + "/exports/usb").json()
    assert [x["path"] for x in t["targets"]] == [str(stick)]
    assert os.listdir(stick) == []                          # listing never writes
    r = client.post(P + f"/inspections/{insp['uuid']}/exports/usb", json={"mount": str(stick)})
    assert r.status_code == 200, r.text
    assert r.json() == {"path": str(stick / "WING-7.csv"), "rows": 5}
    assert len(rows_of(open(stick / "WING-7.csv", newline="").read())) == 5
    ok = engine.repo.audit_events("export_usb")
    assert len(ok) == 1 and str(stick / "WING-7.csv") in ok[0]["detail_json"]
    r = client.post(P + f"/inspections/{insp['uuid']}/exports/usb", json={"mount": "/etc"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "not_allowed"
    assert len(engine.repo.audit_events("export_usb_failed")) == 1
