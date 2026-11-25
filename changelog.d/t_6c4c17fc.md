kind: Fixed
task: t_6c4c17fc

- **Four more `hscc api` presentation cases were asking the operator's live
  launchd whether an API was running, and passing anyway.** The residual
  t_db4d8a74 left in its own file — `test_status_running_no_ansi`,
  `test_status_stale_pid_no_ansi`, `test_status_no_ansi_with_qr`,
  `test_no_raw_markup_leak` in `hscc_daemon/tests/test_api_cli_no_ansi.py` —
  drove `status`/`stop` with no seam injected, so `_launchd_matters()` opened on
  the real `~/Library/LaunchAgents/com.hscc.api.plist`, the t_8bf34130 fence
  refused, and the cases rendered the refusal instead of the screen they name.
  They were green only because their assertions never read a supervisor row:
  `assert "stale PID file" in out` is satisfied by the refusal line too ("stale
  PID file; state unknown (supervisor not consulted)"), and the markup sweep
  discarded the rc its `stop` failed closed with. Each now declares the posture
  through the seam the refusal names, pins that the seam was consulted, and pins
  the supervisor row that must be ABSENT. `test_api_multibind.py`'s two status
  cases already had a posture — `sys.platform` pinned to "linux", so the gate
  declines — but it was implicit and unwatched, so it is declared now: a probe
  that must be called zero times, answering the supervised row if it ever is.
  The fence is untouched; a suite that forgets the seam still sees the refusal.
---
kind: Verified
task: t_6c4c17fc

- The survey this card came from was a grep, and grep over-called: with a plugin
  that attributes every supervisor question to the case that asked it, the five
  surveyed files at the pre-fix revision read `records=225 unsheamed=4
  declared=3 never_asked=218` on a box with the unit installed and
  `launchctl print gui/$UID/com.hscc.api` answering `state = running` — and all
  four unsheamed ones are in `test_api_cli_no_ansi.py`. `test_autodown_cli.py`
  mentions `cmd_api` only in a comment (its real-CLI cases run `autodown <sub>
  --help` in a sandboxed HOME), `test_cli_h_shortflag.py` replaces `cmd_api`
  with a recorder, and `test_api_cli_detach_stdio.py` runs `start`/`start --help`
  only — `start` never consults the supervisor. All three need no posture; the
  measurement is in the card's comment thread.
- Same plugin, same posture, at this head: `225 passed`,
  `records=225 unsheamed=0 declared=9 never_asked=216` — the two changed files
  contribute the nine declared cases and zero refusals.
- Both changed files (20 cases) green in all three postures the card demands:
  `20 passed` on the live box (unit loaded, `state = running`), `20 passed` with
  `HOME` at a throwaway directory whose `Library/LaunchAgents` is empty (so
  "no unit installed" is a fact of the run, not of the host), and `20 passed`
  with `HSCC_LAUNCHD_PROBE=1` forcing the question while the live unit sits
  loaded.
- Four mutation plants on the new pins, each self-proved by the run that went
  red and each leaving the file byte-identical: a canned answer that says the
  unit is loaded and running bites all four `status` cases (never aimed at a
  `stop` case — `_stop_launchd` reaches the unfenced `_launchctl_run`, which
  would bootout a live unit); a fabricated probe record that satisfies the count
  pin while the seam is never injected bites on the absent-refusal-row pin;
  dropping the `sys.platform` pin bites the multibind case; and forcing
  `_launchd_matters` True through the never-asked probe bites on both of its
  pins.
