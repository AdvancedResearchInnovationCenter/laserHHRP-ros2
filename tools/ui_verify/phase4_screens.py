#!/usr/bin/env python3
"""Phase 4 screen check: drives the built operator UI in a real headless Chromium-family browser at
800 x 480, by tapping the screen like an operator (the physical switch is the mock's press/release),
checks layout rules on every screen and saves a screenshot of each.

    tools/ui_verify/phase4_screens.sh [OUT_DIR]      # builds ui_web, starts a mock backend, runs this

    python3 tools/ui_verify/phase4_screens.py --base http://127.0.0.1:8097 --browser brave-browser --out DIR

Needs: python3 websocket-client, a Chromium-family browser (chromium, google-chrome, brave-browser).
The browser runs with a throwaway profile; nothing outside OUT_DIR is written.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket  # websocket-client

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> bool:
    RESULTS.append((bool(ok), what))
    print(f"{'PASS' if ok else 'FAIL'}  {what}", flush=True)
    return bool(ok)


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + "/api/v1" + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            return 0, {}                            # not up (yet): callers keep waiting

    def state(self):
        return self.call("GET", "/state")[1]

    def wait(self, pred, what: str, timeout=30.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            s = self.state()
            if s and pred(s):
                return s
            time.sleep(0.1)
        raise TimeoutError(f"backend never reached: {what} (screen {self.state().get('screen')})")


class Browser:
    """Minimal Chrome DevTools Protocol client."""

    def __init__(self, exe: str, profile: Path):
        self.proc = subprocess.Popen(
            [exe, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}", "--no-first-run",
             "--no-default-browser-check", "--disable-gpu", "--hide-scrollbars", "--window-size=800,480",
             "--disable-extensions", "--disable-sync", "--mute-audio", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = profile / "DevToolsActivePort"
        end = time.monotonic() + 20
        while not port_file.exists():
            if time.monotonic() > end or self.proc.poll() is not None:
                raise RuntimeError("browser did not start")
            time.sleep(0.1)
        port = int(port_file.read_text().split()[0])
        targets = []
        while not targets:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json") as r:
                targets = [t for t in json.load(r) if t["type"] == "page"]
            time.sleep(0.1)
        self.ws = websocket.create_connection(targets[0]["webSocketDebuggerUrl"], timeout=30)
        self.id = 0
        self.send("Emulation.setDeviceMetricsOverride", width=800, height=480, deviceScaleFactor=1, mobile=False)

    def send(self, method: str, **params):
        self.id += 1
        self.ws.send(json.dumps({"id": self.id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr: str):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(f"JS error in {expr[:80]}: {r['exceptionDetails']}")
        return r["result"].get("value")

    def text(self) -> str:
        return self.js("document.body.innerText") or ""

    def wait_text(self, needle: str, timeout=20.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if needle in self.text():
                return
            time.sleep(0.1)
        raise TimeoutError(f"screen never showed {needle!r}; it shows: {self.text()[:300]!r}")

    def wait_button(self, label: str, timeout=60.0, exact=True) -> None:
        cond = "t === L" if exact else "t.startsWith(L)"
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.js(f"""[...document.querySelectorAll('button')].some(b => {{ const t = b.innerText.trim();
                const L = {json.dumps(label)}; return ({cond}) && b.offsetParent !== null && !b.disabled; }})"""):
                return
            time.sleep(0.1)
        raise TimeoutError(f"no button {label!r} appeared; screen: {self.text()[:300]!r}")

    def click_at(self, x: float, y: float) -> None:
        for kind in ("mousePressed", "mouseReleased"):
            self.send("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", buttons=1, clickCount=1)
        time.sleep(0.08)

    def center(self, selector_js: str):
        return self.js(f"""(() => {{ const el = {selector_js}; if (!el) return null;
            const r = el.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; }})()""")

    def tap(self, label: str, exact=True) -> None:
        """Tap the visible button whose text is `label` (exact) or starts with it."""
        cond = "t === L" if exact else "t.startsWith(L)"
        pos = self.center(f"""[...document.querySelectorAll('button, a.button')].reverse().find(b => {{
            const t = b.innerText.trim(); const L = {json.dumps(label)};
            return ({cond}) && b.offsetParent !== null && !b.disabled; }})""")
        if pos is None:
            raise RuntimeError(f"no enabled button {label!r}; screen: {self.text()[:300]!r}")
        self.click_at(*pos)

    def type_keys(self, text: str) -> None:
        for c in text:
            self.tap(c)

    def shot(self, out: Path, name: str) -> None:
        time.sleep(0.35)                          # let React paint the last state
        data = self.send("Page.captureScreenshot", format="png")["data"]
        (out / f"{name}.png").write_bytes(base64.b64decode(data))

    def close(self) -> None:
        try:
            self.ws.close()
        finally:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


LAYOUT_JS = """(() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const small = [...document.querySelectorAll('button, a.button, select')]
    .filter(vis).filter(b => !b.classList.contains('dev-toggle'))
    .map(b => [b.innerText.trim() || b.getAttribute('aria-label') || b.className, b.getBoundingClientRect()])
    .filter(([_, r]) => r.height < 47.5 || r.width < 47.5)
    .map(([t, r]) => `${t} ${Math.round(r.width)}x${Math.round(r.height)}`);
  const outside = [...document.querySelectorAll('.app *')].filter(vis)
    .filter(el => !el.closest('svg') || el.tagName === 'svg')
    .filter(el => { for (let p = el.parentElement; p; p = p.parentElement) {     // inside a scrolling list: fine
        if (['auto', 'scroll'].includes(getComputedStyle(p).overflowY)) return false; } return true; })
    .map(el => [el, el.getBoundingClientRect()])
    .filter(([_, r]) => r.right > 800.5 || r.bottom > 480.5 || r.left < -0.5 || r.top < -0.5)
    .map(([el, r]) => `${el.tagName}.${el.className} ${Math.round(r.right)},${Math.round(r.bottom)}`);
  const clipped = [...document.querySelectorAll('button, .pill, .status-big, .count-big, .count-mid, .scan-state')]
    .filter(vis).filter(el => el.scrollWidth > el.clientWidth + 1)
    .map(el => el.innerText.trim().slice(0, 30));
  // a container that clips (overflow hidden) must not be hiding part of its content
  const hidden = [...document.querySelectorAll('.app *')].filter(vis)
    .filter(el => !(el instanceof SVGElement))
    .filter(el => ['hidden', 'clip'].includes(getComputedStyle(el).overflowY))
    .filter(el => el.scrollHeight > el.clientHeight + 1)
    .map(el => `${el.className} ${el.scrollHeight}>${el.clientHeight}`);
  return { scroll: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
           small, outside: outside.slice(0, 5), clipped, hidden };
})()"""


def layout_ok(b: Browser, screen: str) -> None:
    r = b.js(LAYOUT_JS)
    check(r["scroll"][0] <= 800 and r["scroll"][1] <= 480, f"{screen}: page fits 800x480 (is {r['scroll']})")
    check(not r["outside"], f"{screen}: nothing drawn outside the screen {r['outside'] or ''}")
    check(not r["small"], f"{screen}: touch targets >= 48 px {r['small'] or ''}")
    check(not r["clipped"], f"{screen}: no clipped labels {r['clipped'] or ''}")
    check(not r["hidden"], f"{screen}: no content cut off by its panel {r['hidden'] or ''}")


def screen(api: Api) -> str:
    return api.state()["screen"]


def run(base: str, browser_exe: str, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    api = Api(base)
    profile = Path(tempfile.mkdtemp(prefix="hhrp-ui-browser-"))
    b = Browser(browser_exe, profile)
    try:
        b.send("Page.navigate", url=base + "/?kiosk")
        no_fake_pass = []

        # ---- 1 Identification: typed on the on-screen keyboard ----
        b.wait_text("Operator identification")
        layout_ok(b, "identify")
        b.type_keys("ENG-1042")
        b.shot(out, "01-identify")
        check(b.js("document.querySelector('[data-testid=id-field]').innerText") == "ENG-1042",
              "identify: on-screen keyboard typed ENG-1042")
        b.tap("Continue")

        # ---- 2 Setup ----
        b.wait_text("Start inspection")
        b.tap("CSK Depth", exact=False)
        layout_ok(b, "setup")
        b.shot(out, "02-setup")
        b.tap("Start inspection")
        s = api.wait(lambda s: s["inspection"] is not None, "inspection created")
        check(s["inspection"]["mode"] == "csk_depth", "setup: CSK Depth inspection created by taps")

        # ---- 3 Ready: first inspection has no remembered count -> NOT READY until set ----
        b.wait_text("Enter the expected feature count")
        layout_ok(b, "not_ready (count)")
        b.shot(out, "03-not-ready-count")
        b.tap("Set")
        b.wait_text("Expected countersinks (1–500)")
        b.type_keys("20")
        b.shot(out, "04-keypad")
        b.tap("OK")
        b.wait_text("Hold the switch to scan")
        check(api.state()["inspection"]["next_expected_count"] == 20, "ready: keypad set expected count 20")
        layout_ok(b, "ready")
        b.shot(out, "05-ready")
        pos = b.center("document.querySelector('[aria-label=\"One more\"]')")
        b.click_at(*pos)
        api.wait(lambda s: s["inspection"]["next_expected_count"] == 21, "stepper +1")
        pos = b.center("document.querySelector('[aria-label=\"One less\"]')")
        b.click_at(*pos)
        api.wait(lambda s: s["inspection"]["next_expected_count"] == 20, "stepper -1")
        check(True, "ready: +/- steppers change the inspection's expected count")

        # ---- 4 Scanning (switch held) and Finishing Analysis (released, detections still arriving) ----
        api.call("POST", "/dev/mock/press")
        b.wait_text("Scanning")
        api.wait(lambda s: (s["current_scan"] or {}).get("detected_target_count", 0) >= 6, "6 detections")
        layout_ok(b, "scanning")
        b.shot(out, "06-scanning")
        circles = b.js("[...document.querySelectorAll('.circle')].map(c => c.dataset.state)")
        check(len(circles) == 20 and circles.count("found") >= 6, f"scanning: 20 circles, filled as detected ({circles.count('found')})")
        # hold until the device has rolled past the whole mock part (last feature row at y = 265 mm)
        api.wait(lambda s: (s["hardware"] or {}).get("distance_mm", 0) >= 290, "part fully scanned", 60)
        api.call("POST", "/dev/mock/release")
        b.wait_text("Finishing analysis")
        layout_ok(b, "finishing_analysis")
        b.shot(out, "07-finishing")

        # ---- 5 Results: map, tap a feature, Accept ----
        api.wait(lambda s: s["screen"] == "results", "results", 30)
        b.wait_button("Accept with", exact=False) if api.state()["current_scan"]["count_mismatch"] else b.wait_button("Accept")
        s = api.state()
        check(s["screen"] == "results" and s["current_scan"]["detected_target_count"] == 20,
              "results: all 20 countersinks, including those decided after release")
        layout_ok(b, "results")
        b.shot(out, "08-results")
        n_drawn = b.js("document.querySelectorAll('[data-feature]').length")
        check(n_drawn == len(s["current_scan"]["features"]), f"results: map draws every feature ({n_drawn})")
        geo = b.js("""[...document.querySelectorAll('.feat.target circle:not(.sel-ring)')].map(c =>
            [+c.getAttribute('cx'), +c.getAttribute('cy'), +c.getAttribute('r')])""")
        feats = [f for f in s["current_scan"]["features"] if f["is_target"]]
        # map contract: larger x right, larger y down, one uniform scale
        fx = sorted(zip(feats, geo), key=lambda p: p[0]["x_mm"])
        fy = sorted(zip(feats, geo), key=lambda p: p[0]["y_mm"])
        check(fx[0][1][0] < fx[-1][1][0] and fy[0][1][1] < fy[-1][1][1], "results map: +x rightward, +y downward")
        sx = (fx[-1][1][0] - fx[0][1][0]) / (fx[-1][0]["x_mm"] - fx[0][0]["x_mm"])
        sy = (fy[-1][1][1] - fy[0][1][1]) / (fy[-1][0]["y_mm"] - fy[0][0]["y_mm"])
        check(abs(sx - sy) / sy < 0.02, f"results map: uniform scale (x {sx:.3f}, y {sy:.3f} px/mm)")
        pos = b.center("document.querySelector('.feat.target')")
        b.click_at(*pos)
        b.wait_text("Not measured")
        layout_ok(b, "results (feature selected)")
        b.shot(out, "09-results-feature")
        no_fake_pass.append(b.text())
        b.tap("Accept")
        b.wait_text("Hold the switch to scan")
        check(api.state()["recent_scans"][0]["status"] == "accepted", "results: Accept recorded")

        # ---- 6 Count mismatch: 18 of 20 found, still acceptable with a warning ----
        st, _ = api.call("POST", "/dev/mock/layout", {"targets": 18})
        prev = (api.state()["current_scan"] or {}).get("scan_id")
        api.call("POST", "/dev/mock/scenario", {"name": "normal"})
        api.wait(lambda s: (s["current_scan"] or {}).get("scan_id") != prev
                 and s["screen"] in ("results", "analysis_failed"), "normal scan finished", 60)
        b.wait_text("Accept with warning", 60)
        layout_ok(b, "results (mismatch)")
        b.shot(out, "10-results-mismatch")
        check("2 fewer than expected" in b.text(), "mismatch: 18/20 shows '2 fewer than expected'")
        no_fake_pass.append(b.text())
        b.tap("Accept with warning")
        b.wait_text("Hold the switch to scan")
        check(api.state()["recent_scans"][0]["status"] == "accepted_with_warning", "mismatch: accepted with warning")

        # ---- 7 Partial feature (lifted mid-part) ----
        prev = (api.state()["current_scan"] or {}).get("scan_id")
        api.call("POST", "/dev/mock/scenario", {"name": "short"})
        api.wait(lambda s: (s["current_scan"] or {}).get("scan_id") != prev
                 and s["screen"] in ("results", "analysis_failed"), "short scan finished", 60)
        b.wait_button("Rescan")
        s = api.state()
        part = [f for f in s["current_scan"]["features"] if f["partial"]]
        check(bool(part) and "partial" in b.text(), f"partial: detector-flagged partial feature shown ({len(part)})")
        pos = b.center("document.querySelector('.feat.partial')")
        if pos:
            b.click_at(*pos)
        b.shot(out, "11-results-partial")
        b.tap("Rescan")
        b.wait_text("Hold the switch to scan")

        # ---- 8 More than 40 expected: 40 circles plus the remaining number ----
        insp = api.state()["inspection"]["uuid"]
        api.call("PATCH", f"/inspections/{insp}/next-expected-count", {"count": 50})
        api.call("POST", "/dev/mock/layout", {"targets": 46, "rows": 3})
        api.call("POST", "/dev/mock/press")
        api.wait(lambda s: (s["current_scan"] or {}).get("detected_target_count", 0) >= 12, "12 detections", 60)
        b.wait_text("/ 50")
        circles = b.js("[...document.querySelectorAll('.circle')].length")
        more = b.js("(document.querySelector('.circle-more') || {}).innerText")
        check(circles == 40 and more == "+10", f"overflow: 40 circles + '+10' for 50 expected ({circles}, {more})")
        layout_ok(b, "scanning (50 expected)")
        b.shot(out, "12-scanning-overflow")
        api.wait(lambda s: (s["current_scan"] or {}).get("detected_target_count", 0) >= 40, "40 detections", 90)
        api.call("POST", "/dev/mock/release")
        api.wait(lambda s: s["screen"] == "results", "results", 60)
        b.wait_button("Rescan")
        layout_ok(b, "results (46 features)")
        b.shot(out, "13-results-dense")
        b.tap("Rescan")
        b.wait_text("Hold the switch to scan")
        api.call("POST", "/dev/mock/layout", {"targets": 20, "rows": 2})
        api.call("PATCH", f"/inspections/{insp}/next-expected-count", {"count": 20})

        # ---- 9 Analysis failed: no Accept; Keep Incomplete asks first ----
        prev = (api.state()["current_scan"] or {}).get("scan_id")
        api.call("POST", "/dev/mock/scenario", {"name": "detector_failure"})
        api.wait(lambda s: (s["current_scan"] or {}).get("scan_id") != prev
                 and s["screen"] in ("results", "analysis_failed"), "detector_failure scan finished", 60)
        b.wait_text("Analysis failed", 60)
        layout_ok(b, "analysis_failed")
        b.shot(out, "14-analysis-failed")
        check(b.js("[...document.querySelectorAll('button')].some(x => x.innerText.trim().startsWith('Accept'))") is False,
              "failed: no Accept button")
        check("Retry unavailable" in b.text(), "failed: Retry not offered, with the reason")
        b.tap("Keep incomplete")
        b.wait_text("Keep as incomplete?")
        b.shot(out, "15-keep-incomplete-confirm")
        b.tap("Keep incomplete")
        b.wait_text("Hold the switch to scan")
        check(api.state()["recent_scans"][0]["status"] == "incomplete_retained", "failed: kept as incomplete")

        # ---- 10 Zero features: complete and honest ----
        prev = (api.state()["current_scan"] or {}).get("scan_id")
        api.call("POST", "/dev/mock/scenario", {"name": "zero"})
        api.wait(lambda s: (s["current_scan"] or {}).get("scan_id") != prev
                 and s["screen"] in ("results", "analysis_failed"), "zero scan finished", 30)
        b.wait_text("nothing found", 30)
        layout_ok(b, "results (zero)")
        b.shot(out, "16-results-zero")
        b.tap("Rescan")
        b.wait_text("Hold the switch to scan")

        # ---- 11 Fast next scan: A stays UNDECIDED, decided later from History ----
        api.call("POST", "/dev/mock/scenario", {"name": "fast_next"})
        api.wait(lambda s: len(s["recent_scans"]) >= 8 and s["screen"] == "results", "fast_next finished", 90)
        b.wait_button("Rescan")
        b.tap("History")
        b.wait_text("Download CSV")
        api_scans = api.call("GET", f"/inspections/{insp}/scans")[1]
        undecided = [x for x in api_scans if x["status"] == "undecided"]
        check(len(undecided) == 2, f"fast next: earlier scan and current scan both UNDECIDED ({len(undecided)})")
        b.tap(undecided[0]["display_name"], exact=False)
        b.wait_text("Undecided")
        layout_ok(b, "history")
        b.shot(out, "17-history")
        b.tap("Accept")
        api.wait(lambda s: any(x["scan_id"] == undecided[0]["scan_id"] and x["status"] == "accepted"
                               for x in s["recent_scans"]), "history accept")
        check(True, "history: an earlier UNDECIDED scan accepted from History")
        b.tap("USB export")
        b.wait_text("USB drive")
        b.shot(out, "18-usb-export")
        b.tap("Close")
        b.tap("‹ Back")
        b.wait_button("Rescan")
        b.tap("Rescan")
        b.wait_text("Hold the switch to scan")

        # ---- 12 Not ready: hardware reasons are listed ----
        api.call("POST", "/dev/mock/hardware", {"calibrated": False, "encoder_ok": False})
        b.wait_text("Calibration required")
        layout_ok(b, "not_ready (hardware)")
        b.shot(out, "19-not-ready-hardware")
        api.call("POST", "/dev/mock/hardware", {"calibrated": True, "encoder_ok": True,
                                                 "warnings": ["IMU: tilt above 15 degrees", "Speed above 60 mm/s"]})
        b.wait_text("Hold the switch to scan")
        b.wait_text("IMU: tilt above 15 degrees")
        check("+1 more" in b.text(), "warnings: the first one shown under the header, '+1 more'")
        layout_ok(b, "ready (warnings)")
        b.tap("⚠ 2")
        b.wait_text("Alerts and warnings")
        check("Speed above 60 mm/s" in b.text(), "warnings: the header button lists all of them")
        b.shot(out, "19b-warnings")
        b.tap("✕")
        api.call("POST", "/dev/mock/hardware", {"warnings": []})

        # ---- 13 End inspection, past inspections, a rivet inspection runs only rivet_detector ----
        b.tap("End inspection")
        b.wait_text("End inspection?")
        b.tap("End inspection")
        b.wait_text("Start inspection")
        b.tap("Past inspections")
        b.wait_text("Past inspections")
        layout_ok(b, "past inspections")
        b.shot(out, "20-past-inspections")
        b.tap("‹ Back")
        pos = b.center("document.querySelector('[data-testid=label-field]')")
        b.click_at(*pos)
        b.wait_text("Use this ID")
        b.type_keys("WING-L7")
        b.shot(out, "21-inspection-id-keyboard")
        b.tap("Use this ID")
        b.tap("Rivet Flushness", exact=False)
        b.tap("Start inspection")
        b.wait_text("Hold the switch to scan", 30)
        s = api.state()
        check(s["inspection"]["label"] == "WING-L7" and s["inspection"]["mode"] == "rivet_flushness"
              and s["inspection"]["next_expected_count"] == 20,
              "rivet inspection WING-L7 created; expected count remembered (20)")
        check(s["detector"]["mode"] == "rivet_flushness", "rivet inspection: only the rivet detector is selected")
        prev = (api.state()["current_scan"] or {}).get("scan_id")
        api.call("POST", "/dev/mock/scenario", {"name": "normal"})
        api.wait(lambda s: (s["current_scan"] or {}).get("scan_id") != prev
                 and s["screen"] in ("results", "analysis_failed"), "normal scan finished", 60)
        b.wait_button("Rescan")
        pos = b.center("document.querySelector('.feat.target')")
        b.click_at(*pos)
        b.wait_text("Flushness")
        layout_ok(b, "results (rivets)")
        b.shot(out, "22-results-rivets")
        check("Flushness" in b.text() and "Not measured" in b.text(), "rivets: flushness labelled Not measured")
        no_fake_pass.append(b.text())
        b.tap("Accept")
        b.wait_text("Hold the switch to scan")

        # ---- 14 Browser reload mid-analysis: same state, no duplicate ----
        api.call("POST", "/dev/mock/press")
        api.wait(lambda s: (s["current_scan"] or {}).get("detected_target_count", 0) >= 4, "4 detections", 60)
        before = api.state()["current_scan"]["scan_id"]
        b.send("Page.reload")
        b.wait_text("Scanning")
        check(api.state()["current_scan"]["scan_id"] == before, "reload while scanning: same scan, nothing restarted")
        api.call("POST", "/dev/mock/release")
        b.send("Page.reload")
        api.wait(lambda s: s["screen"] == "results", "results", 60)
        b.wait_button("Rescan")
        s = api.state()
        seqs = [f["result_seq"] for f in s["current_scan"]["features"]]
        dom = b.js("document.querySelectorAll('[data-feature]').length")
        check(len(seqs) == len(set(seqs)) == dom, f"reload during analysis: {dom} features, no duplicates")
        b.tap("Rescan")
        b.wait_text("Hold the switch to scan")

        # ---- 15 No fake pass anywhere ----
        joined = "\n".join(no_fake_pass)
        check(not any(w in joined for w in ("PASS", "Pass ", "passed", "In tolerance")),
              "no pass/fail wording on any results screen")

        return 0
    finally:
        b.close()
        shutil.rmtree(profile, ignore_errors=True)


def offline_shot(base: str, browser_exe: str, out: Path, kill) -> None:
    """Separate pass: open the UI, stop the backend, screenshot the offline notice."""
    profile = Path(tempfile.mkdtemp(prefix="hhrp-ui-browser-"))
    b = Browser(browser_exe, profile)
    try:
        b.send("Page.navigate", url=base + "/?kiosk")
        b.wait_text("Hold the switch to scan")
        kill()
        b.wait_text("Connection to the backend lost", 15)
        b.shot(out, "23-offline")
        check("OFFLINE" in b.text(), "backend stopped: OFFLINE and 'screen is not current' notice")
    finally:
        b.close()
        shutil.rmtree(profile, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--browser", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--backend-pid", type=int, default=None, help="stopped at the end for the offline screen")
    a = ap.parse_args()
    exe = a.browser or next((e for e in ("chromium", "chromium-browser", "google-chrome", "brave-browser")
                             if shutil.which(e)), None)
    if not exe:
        print("no Chromium-family browser found", file=sys.stderr)
        return 3
    out = Path(a.out)
    try:
        run(a.base, exe, out)
        if a.backend_pid:
            offline_shot(a.base, exe, out, lambda: os.kill(a.backend_pid, 15))
    except Exception as e:      # a stuck step is a failure with its reason, not a hang
        check(False, f"step failed: {e}")
    failed = [w for ok, w in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed; screenshots in {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
