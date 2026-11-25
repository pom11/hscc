kind: Fixed
task: t_db4d8a74

- **Three `hscc api` presentation cases were asking the operator's live launchd
  whether an API was running.** `hscc_daemon/tests/test_api_cli_no_ansi.py`'s
  status/stop cases declared no supervisor posture, so the verbs asked the host:
  `_launchd_matters()` opens on the real
  `~/Library/LaunchAgents/com.hscc.api.plist`, and on any box where the unit was
  ever installed — a workstation, and the box the merge-queue gate runs on — it
  opened inside those cases. The t_8bf34130 fence then refused to answer inside a
  pytest process, `status` printed "state unknown (supervisor not consulted)"
  where the case asserts "not running", and `stop` answered fail-closed with rc 1
  where it asserts its normal report: `3 failed` on every gate leg on such a box,
  identical on a pristine `origin/main`, so nobody's diff and permanently red.
  Each case now declares the posture it draws through the seam the refusal itself
  names — `launchd_status(_run_probe=...)` — and pins the two ambient inputs that
  decide whether the seam is consulted at all (`sys.platform`,
  `api_launchd.API_PLIST_PATH`). The fence is untouched: a suite that forgets the
  seam still gets the visible refusal, still pinned where it is pinned.
---
kind: Verified
task: t_db4d8a74

- Same sha, same file, both ambient postures (`hscc_daemon/tests/test_api_cli_no_ansi.py`,
  13 cases): pre-fix — live API + loaded unit `3 failed, 10 passed`; unit never
  installed (throwaway HOME with no LaunchAgents entry) `13 passed`, i.e. green by
  accident, which is the same ambient read in the other direction. Post-fix —
  `13 passed` on the live box, `13 passed` with the unit never installed, and
  `13 passed` with `HSCC_LAUNCHD_PROBE=1` forcing the question while the live unit
  sits loaded. Ground truth recorded with the run: the real plist present and
  `launchctl print gui/$UID/com.hscc.api` answering `state = running`.
- Full `hscc_daemon` leg, both tallies labelled by posture and by deselection, so
  they cannot be read as contradicting each other. Gate-shaped (`HSCC_TEST_LEGS=
  hscc_daemon bash scripts/run_tests.sh`, so `-m "not loadstress"` applies), under
  the posture that was red — operator's real HOME, live API, loaded unit:
  `5368 passed, 1 skipped, 9 deselected in 343.19s`, exit 0, ALL GREEN. Same tree,
  same posture, nothing deselected: `5376 passed, 2 skipped` of 5378 collected,
  exit 0. The two reconcile — 5378 collected minus the 9 deselected = 5369 run, and
  those 9 split 8 passed + 1 skipped.
- Mutation probes on the pins, re-run at this revision, measured tree confirmed as
  the worktree's own `hscc_daemon/api_cli.py` (not the deployed plugin copy), file
  byte-identical after every plant: removing the seam injection from the status case
  bites (1 failed); answering "unit loaded" instead of "no such job" bites (1
  failed) — the reason that case also asserts there is NO supervisor row, since
  launchd's own "state = not running" contains the phrase it already asserts; and a
  plant that fakes the call record so the count pin is satisfied by a lie while the
  verb never consults the seam bites too (1 failed, on `rc == 0`).
- The file's own claims are measured, not asserted: a plugin attributes every
  supervisor question to the case that asked it (`SEAM_JSON` attribution). It
  confirms the three lifecycle cases inject the seam in every posture and that four
  others do not — which is exactly what the module docstring now says, and what it
  previously over-claimed. It also confirms the canned answer carries launchd's real
  value for an unloaded job (rc 113, `Bad request.` plus a could-not-find-service
  line, measured against a throwaway label), matching the value
  `test_api_cli_launchd.py` already pins; `launchd_status` reads any `rc!=0` as
  available-and-not-loaded, so the correction changes no verdict.
- The five entries that reach the verbs without declaring a posture are left as
  their own cards, green today and latent rather than red: four other files
  (`test_api_cli_detach_stdio.py`, `test_api_multibind.py`, `test_autodown_cli.py`,
  `test_cli_h_shortflag.py`) plus the four unsheamed cases in this file itself, all
  five listed on t_6c4c17fc. A separately measured gap in `_handle_stop`'s
  `available=False` row — renaming it reddens 0 of 238 cases — is t_cf8416dd.
