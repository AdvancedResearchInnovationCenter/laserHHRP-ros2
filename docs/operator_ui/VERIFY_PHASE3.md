# Verifying Phase 3 yourself

Phase 3 = stable scan identity and reliable analysis finalization. Every physical scan is recorded under the
controller's scan UUID; the selected detector processes it on its own and says exactly once when it is done;
the backend completes a scan only on that evidence; the raw recording is linked and verified.

## What changed in existing ROS code (all additive; the original messages are unchanged)

| Node | Change | Why |
| --- | --- | --- |
| `scan_controller_node` | Publishes `scan/session_state` per scan transition: a UUID allocated on a successful start, a per-process `controller_instance_id`, `transition_seq` (+1 per transition, gaps detectable) and the **exact** stamp of the `scan/status` message of that transition | Scan identity; `scan/status` still alone drives acquisition |
| `synchronizer_node` | Publishes every frame also as `scan/frame_tagged` with its scan UUID (matched by the exact start stamp, either topic may arrive first; bounded 600-frame wait), then `scan/stream_closed` with the frames actually published | Detector and backend know which frames belong to which scan and how many there were |
| `recorder_node` | Folder names never collide (`<time>-2` ...); publishes `scan/recording_session` (opened / closed, exact stamp); after a stop keeps the session open for the scan's flushed last frames (until the stream close, at most 2 s) | **Two bugs found and fixed**: same-second scans overwrote `frames.bin` (Phase 0), and the last frame(s) of a scan were missing from `frames.bin` (found by the identity check: 181 of 182) |
| `hole_detector`, `rivet_detector` | New `tagged` mode (off by default; the UI's supervisor turns it on): one job per scan UUID, final pass on stream close without waiting for a next scan, numbered tagged results, exactly one `DetectionFinalized`, `out_dir/<scan UUID>/` made at once, missing frames fail the scan | Explicit, per-scan completion |

New messages: `ScanSessionState`, `TaggedScanFrame`, `ScanStreamClosed`, `TaggedInspectionResult`,
`DetectionFinalized`, `ScanRecordingSession` ([laserHHRP/msg](../../laserHHRP/msg)).

## Checks (all on the laptop, Humble, 2026-11-17)

All run on isolated ROS domains with a loopback DDS config and a scratch `HOME`; `HHRP_WS=<built workspace>`
reuses a build (C++ builds are limited to 2 jobs).

| Check | Command | Result |
| --- | --- | --- |
| Identity layer: core only (controller, synchronizer, recorder) | `tools/ui_verify/phase3_identity_check.sh` | **19/19**, twice |
| Tagged detector vs legacy, hole detector (synthetic plate at the laser's 120 frames/s) | `tools/ui_verify/phase3_detector_check.sh` | **16/16** |
| Same for the rivet detector | `CHECK_DETECTOR=rivet_detector tools/ui_verify/phase3_detector_check.sh` | **16/16** |
| End to end: core + backend + supervised tagged detector | `tools/ui_verify/phase3_ros_check.sh` | **28/28** |
| Old-bag adapter (a bag written in the current format, untagged) | `tools/ui_verify/phase3_bag_adapter_check.sh` | PASS (7 holes / 3 countersinks; 4 / 2) |
| Phase 0 baseline regression (legacy flows) | `tools/ui_baseline/run_baseline.sh` | INCOMPLETE as before (bags missing), **0 failures**; same-second scans now in 2 folders |
| Backend unit tests | `cd ui_backend && python3 -m pytest -q test/` | 103 passed |
| Planted bugs | `python3 tools/ui_verify/mutation_check.py` | **24/24** caught (2 new: raw recording called replayable without checking frames.bin; Retry without replay support) |

What the checks cover:
- **Identity:** status_stamp equals the exact `scan/status` stamp; tagged frames = legacy frames, indices
  0..n-1; stream close count; pause/resume keeps one UUID; same-second scans get two UUIDs, two folders, neither
  truncated; recorder events carry the right stamps and frame counts.
- **Detectors:** tagged mode reports **exactly** what the legacy node reports for the same frames; close before
  frames; redelivered frames; zero-frame scan (folder still made); missing frames → FAILED with the reason;
  late frames never reopen a finalized scan; a stream error → FAILED; clean exit on SIGINT.
- **End to end:** scan recorded under the controller's UUID; COMPLETE only from the detector's finalization;
  frame rate unaffected (119 of 120/s); raw recording linked and verified (frames.bin = published frames);
  UUID detector folder although nothing was found; two rapid presses → two scans, both complete, separate
  verified raw folders, the earlier one UNDECIDED; crash → NOT READY, explicit restart; scan without inspection
  → UNASSIGNED; mode switch → exactly one rivet_detector; SIGTERM leaves no detector.

> **Correction (2026-11-18).** The detector check above first compared tagged and legacy results *exactly*. Repeated
> runs showed that comparison was flaky, for a reason unrelated to tagging; see "Code review of 78004e1" in
> [VERIFY_PHASE4.md](VERIFY_PHASE4.md). It now runs `hole_detector` at `max_roundness` 0.10 in both modes and
> compares feature type exactly and position within 0.5 mm.

## Findings worth knowing

- **Frames are lost if a detector falls behind.** Fed at 400 frames/s (3× the laser's real rate) the rivet
  detector lost 211 of 1769 frames. Legacy mode then reported different rivets **without notice**; tagged mode
  failed the scan with "missing frames: 211 of 1769". At 120 frames/s nothing was lost. The Pi is slower: Phase 5
  must measure this on the Pi, and tagged mode makes any loss visible instead of silent.
- **Retry Analysis** stays refused with a stated reason: a raw recording can now be verified (replay-eligible),
  but replaying it into the detector is not implemented.
- The simulator detects nothing, so the end-to-end check completes scans with zero features; real detections
  through the tagged path are covered by the detector checks and the bag adapter.

## Not covered yet

Real hardware and real bags (none compatible on this laptop); the Pi (Jazzy) and its frame rates; replay for
Retry; the designed screens (Phase 4).
