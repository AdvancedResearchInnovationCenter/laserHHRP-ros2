# Verifying Phase 1 yourself

Three independent checks, none of which require trusting the author.

| Check | Time | What it shows |
| --- | --- | --- |
| A. Hands-on walkthrough | 10 min | The workflow behaves as plan.md says, as seen in a browser |
| B. Requirement → test map | read | Which test asserts which plan rule; open the test and read the asserts |
| C. Mutation check | ~3 min | The tests catch realistic bugs (they are not just green) |

## A. Hands-on walkthrough (laptop, no ROS needed)

```bash
cd ui_web && npm ci && npm run build && cd ..                   # once
cd ui_backend && python3 -m laser_hhrp_ui.server --mock --static ../ui_web/dist --db /tmp/ui_demo.db
```

Open <http://127.0.0.1:8080>. The page is the Phase 1 **scaffold** (plain controls, not the final design).
The bottom bar exists only in mock mode: **Hold to scan** stands in for the physical switch (press = start,
release = stop), and the scenario menu runs scripted scans.

| # | Do | Expect | Plan |
| --- | --- | --- | --- |
| 1 | Enter `ENG-1042`, Continue | SETUP screen with CSK Depth / Rivet Flushness | §9.1 |
| 1b | (try `bad id!`) | red error, stays on IDENTIFY | §7 validation |
| 2 | Leave ID empty, tap **CSK Depth** | NOT READY; label `INS-<today>-001`; reason *Enter the expected feature count* (briefly also *Detector not ready*) | §9.2, §8 |
| 3 | Expected `20`, Set | READY | §4.1 |
| 4 | Scenario **normal** (holds the switch for 7 s, then releases) | SCANNING with distance and filling circles. After release: *Finishing analysis*, and the last circles still fill (late detections), then RESULTS **20 / 20** with 22 features listed (2 plain holes greyed, not counted), each *Not measured* | §1, §4.2, §5 filter |
| 5 | Accept | READY; history row *Scan 001 20/20 accepted* | §9.4 |
| 6 | Scenario **short** (released halfway along the part) | RESULTS with fewer than 20, orange mismatch warning, 2 features *partial* (the row cut by the scan end), none beyond it. Accept still works → *accepted_with_warning* | §11.3, §11.5 |
| 6b | Optional: press and hold **Hold to scan** yourself, release whenever | Same flow by hand. Whether late detections appear depends on where you release | §1 |
| 7 | Scenario **fast_next** | Two scans back to back. The screen shows the 2nd scan's results; the 1st is *undecided* in history with its own Accept / Rescan | §11.4, §4.4 |
| 8 | Scenario **detector_failure** | ANALYSIS FAILED with reason; **no Accept button**; Rescan / Keep incomplete | §4.5 |
| 9 | Scenario **error_stop** | ANALYSIS FAILED: *acquisition stopped by error* | §4.6 |
| 10 | Scenario **missing_result**, wait 2 min | *Analysis is taking longer than expected* after 10 s, failure *analysis timeout* at 120 s | §5.7 watchdog |
| 11 | Scenario **normal**, press F5 halfway | Reloaded page shows the same scan and progress; no restart, no duplicate features | §11.7 |
| 12 | Scenario **normal**, Ctrl+C the server mid-scan, start it again, reload | Earlier accepted scans intact; the cut scan is ANALYSIS FAILED (*backend restarted…*), never accepted | §11.9 |
| 13 | End inspection while a scan is analysing | error *Wait until scanning and analysis have finished* | §4.6 |
| 14 | Restart **without** `--mock`; `curl -X POST http://127.0.0.1:8080/api/v1/dev/mock/press` | 404: no endpoint can start a scan; UI shows *ROS bridge not running* | §7 forbidden |

Delete `/tmp/ui_demo.db` to start over. The database is plain SQLite: `sqlite3 /tmp/ui_demo.db
'select scan_number, analysis, decision, detected_target_count, expected_count from scans'`.

## B. Requirement → test map

Run `cd ui_backend && python3 -m pytest -v test/` to see every test by name. Acceptance scenarios (plan §11):

| §11 | Covered by | Not yet (phase) |
| --- | --- | --- |
| 1 Normal CSK | `test_state::test_normal_csk_with_late_detections_and_no_fake_pass`, `test_api::test_full_workflow_over_http_and_websocket` | real switch/ROS (2) |
| 2 Normal rivets | `test_state::test_normal_rivets_count_only_rivets_and_reject_other_mode_results`, `test_mock_scenarios::test_normal_scan_completes_with_all_features[rivet_flushness]` | only `rivet_detector` process running (2) |
| 3 Count mismatch | `test_state::test_count_mismatch_warns_but_can_be_accepted_and_overflow_never_stops` | |
| 4 Fast next scan | `test_state::test_fast_next_scan_keeps_late_results_with_first_scan`, `test_mock_scenarios::test_fast_next_scans_stay_separate` | real UUIDs from ROS (3) |
| 5 Zero / partial | `test_state::test_zero_feature_scan_completes_honestly`, `::test_partial_flag_comes_only_from_detector`, `test_mock_scenarios::test_short_scan_reports_only_scanned_features_and_partials` | |
| 6 Lost / late message | `test_state::test_finalization_waits_for_results_reordered_across_topics`, `::test_missing_frame_count_or_extra_results_fail`, `::test_missing_result_times_out_with_warning_first`, `test_mock_scenarios::test_reordered_delivery_still_completes` | detector-side index wait (3) |
| 7 Browser reload | `test_api::test_reconnect_gets_fresh_snapshot_mid_analysis` | real browser test (4) |
| 8 Detector change | `test_state::test_operator_and_inspection_guards` (end blocked while pending, detector stop requested) | process supervisor (2) |
| 9 Restart / fault | `test_state::test_restart_keeps_history_and_interrupts_unfinished`, `::test_detector_failure_and_error_stop_fail_and_cannot_be_accepted`, `::test_detector_crash_fails_pending_scans` | |
| 10 Performance / regression | | Pi (5); Phase 0 baseline unchanged (no legacy code touched) |
| 11 Quick press / undecided | `test_state::test_missed_stop_transition_ends_previous_scan_and_gap_is_flagged`, fast-next tests above | |
| 12 Last scan final pass | completion only on finalization (all §6 tests) | detector final pass (3) |
| 13 Data / visual | `test_models_repository::*`, `test_state::test_not_ready_reasons`, `::test_unverified_clock_uses_local_ids_and_warns`, guards test | map, export, visuals (4) |

Other plan rules:

| Rule | Test |
| --- | --- |
| No web endpoint starts/stops a scan (§7) | `test_api::test_no_scan_control_without_mock` |
| Not measured, no PASS (§1, §8) | `test_models_repository::test_measurement_never_reports_a_value_even_if_detector_sends_one` |
| One inspection, fixed mode (§1) | `test_state::test_mode_cannot_change_within_inspection`, `test_models_repository::test_one_open_inspection_enforced_by_schema` |
| Expected count 1..500, Ready only, remembered (§4.1, §7) | `test_state::test_expected_count_only_in_ready_and_validated`, `::test_expected_count_remembered_and_snapshotted_per_scan` |
| Idempotent commands / delivery (§7) | `test_state::test_decisions_idempotent_and_conflicting_rejected`, `::test_labels_unique_case_insensitive_and_request_id_idempotent`, `::test_duplicate_delivery_is_idempotent_and_conflict_fails` |
| Rescans never overwritten (§8) | `test_state::test_rescan_keeps_the_original_record` |
| Unassigned scans (§4.7) | `test_state::test_scan_without_inspection_or_detector_is_unassigned` |
| Queue overflow visible (§5.7) | `test_state::test_queue_overflow_fails_pending_analysis` |
| Ready policy (§9) | `test_state::test_not_ready_reasons` (7 cases), `::test_imu_warning_does_not_block_and_stale_hardware_does` |
| Retry only with verified data (§4.5) | `test_state::test_retry_only_with_verified_raw_data` |

## C. Mutation check

```bash
python3 tools/ui_verify/mutation_check.py
```

It copies `ui_backend` to a temp directory, plants **one** realistic bug at a time (each tied to a plan
rule), and runs the tests. A bug that leaves every test green has **SURVIVED**: that is a hole in the
tests. The source tree is never modified.

Result on the laptop (2026-11-17): **17/17 planted bugs caught**, each by a failing assertion.

| Planted bug | Caught by |
| --- | --- |
| Complete without waiting for all declared results | `test_faults_fail_explicitly[missing_result]` |
| Ignore frame-count mismatch | `test_missing_frame_count_or_extra_results_fail` |
| Hardware ERROR stop counted as success | `test_faults_fail_explicitly[error_stop]` |
| Accept allowed on a failed analysis | `test_failure_action_over_http` |
| Restart marks unfinished analysis complete | `test_restart_keeps_history_and_interrupts_unfinished` |
| Detections after release dropped | `test_full_workflow_over_http_and_websocket` |
| Watchdog never fails | `test_faults_fail_explicitly[missing_result]` |
| Complete once detected count reaches expected | `test_full_workflow_over_http_and_websocket` |
| Missed stop not ended by next start | `test_missed_stop_transition_ends_previous_scan_and_gap_is_flagged` |
| Early results for an unknown scan lost | `test_results_before_scan_start_are_buffered` |
| Plain holes counted as CSK targets | `test_full_workflow_over_http_and_websocket` |
| Measured value reported | `test_full_workflow_over_http_and_websocket` |
| UI-side partial threshold | `test_partial_is_the_detector_flag_not_a_ui_threshold` |
| Expected count editable while scanning | `test_expected_count_only_in_ready_and_validated` |
| Second inspection opened while one is open | `test_validation_and_conflict_status_codes` |
| Different second decision overwrites | `test_full_workflow_over_http_and_websocket` |
| Queue overflow does not fail pending analyses | `test_queue_overflow_fails_pending_analysis` |

What the first runs exposed, and what was fixed (so the result is not a first-try claim):
- The partial-threshold bug **survived**: the test data always made `partial` agree with `scanned_share`.
  Added `test_partial_is_the_detector_flag_not_a_ui_threshold`, where they disagree.
- Two bugs made the API test **hang** instead of fail (it waited on the WebSocket for an event that never
  came). The test now waits on HTTP state with a time limit and reads the WebSocket only up to an event
  that is certain to arrive.
- One planted bug was itself a no-op (`pass`) and was rewritten as a real behaviour change.

Limits: mutations only show the tests catch *these* bugs. They do not prove the absence of others, nor
anything about ROS, the Pi or the detectors (Phases 2–5).
