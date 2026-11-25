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
- Full `hscc_daemon` leg under that live posture: `5376 passed, 2 skipped` (0
  failed, exit 0) at 5378 collected.
- Mutation probe on the two new pins: removing the seam injection from the status
  case bites (1 failed); answering "unit loaded" instead of "no such job" bites
  (1 failed) — the reason that case also asserts there is NO supervisor row,
  since launchd's own "state = not running" contains the phrase it already
  asserts; closing the supervisor gate so the seam is never consulted bites too
  (3 failed), which is what the one-`launchctl print`-per-verb call-count pin is
  for. File byte-identical after every mutation.
- The four other cases that reach the verbs without mentioning the seam
  (`test_api_cli_detach_stdio.py`, `test_api_multibind.py`, `test_autodown_cli.py`,
  `test_cli_h_shortflag.py`) are green today — 212 passed — so that leak is latent,
  not red, and is left as its own card.
