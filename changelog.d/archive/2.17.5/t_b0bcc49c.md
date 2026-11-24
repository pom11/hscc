kind: Fixed
task: t_b0bcc49c

- **Nothing enforced the "green locally, red in the gate" property, so the
  flaky-teardown class kept coming back.** A case in
  `hscc-api/tests/test_api_server_stop_signal.py` or
  `hscc_daemon/tests/test_api_cli_stop_sidecar.py` passes on an idle box and
  fails on a loaded one — that is the whole class — and #628 hardened one of the
  two files while PR #673's queue gate still bounced on the other. There was no
  leg anywhere that ran these files under load, so every green stamp in the
  repo proved the test, not the product. Now there is one: `loadstress` is a
  registered `pytest.ini` marker carried by the 11 load-sensitive items of the
  api file and the 9 of the daemon file (the 16 static source pins deliberately
  are not marked — they measure the same thing on any machine),
  `scripts/loadstress_leg.py` runs them one pytest process per file per repeat
  under CPU pressure owned by `scripts/load_burners.sh` (no ad-hoc burner loop
  was written, which is what t_71ed3b1c exists to prevent), and every ordinary
  leg of `scripts/run_tests.sh` deselects the marker with `-m "not loadstress"`.
  The leg is selectable (`HSCC_TEST_LEGS=loadstress`) and deliberately absent
  from `DIRS`, so no full-suite or queue gate pays for the repeats. It exits
  **2** on an empty selection and on zero completed passes: a stress leg that
  selects nothing is a green lie. No product code touched.
---
kind: Verified
order: 1

- Stress leg, measured twice on a 14-core host (1-min load ~3 at rest), with
  the interpreter the runner selects (its Hermes venv python):
  **`20 items x 3 repeats at load 8.40 -> 60 passed, 0 failures`** (3ab28e4d) and
  **`... at load 7.83 -> 60 passed, 0 failures`** (fe272c1c) — 6 file-passes each,
  7 burners (`min(8, ceil(cores/2))`), peak 1-min load sampled every 2 s inside
  the window (52 / 51 samples over 107 / 105 s), **0 burner survivors** in both.
- Ordinary legs still deselect the marker, by count and not by claim:
  `HSCC_TEST_LEGS="hscc-api hscc_daemon scripts"` collected **1405** items for
  the api leg and ran **1394 (11 deselected)**, and collected **5074** for the
  daemon leg and ran **5065 (9 deselected)** — 11 and 9 being exactly the marked
  sets. Leg results: `5064 passed, 1 skipped, 9 deselected` and `1392 passed,
  1 skipped, 11 deselected, 1 xfailed`.
- `scripts/tests/test_loadstress_leg.py`: **16 arms**, run inside the ordinary
  `scripts` leg so rot reddens a gate rather than a stress run nobody
  invokes. Four proven by mutation probes — empty-selection guard disabled ->
  `1 failed`; a load-sensitive case silently un-marked -> `1 failed`; the
  deselect turned into a no-op -> `2 failed`; and the leg's signal forwarding
  removed, which came back **green** and is recorded as a weakness of that arm
  (the harness dies on `SIGPIPE` when the leg's pipe closes) rather than as a
  proof.
