# t_18318fd9 — autodown status --json flake (live gh probe WARN on stdout)

Status: fix implemented + committed; full suite running.
Workspace: branch wt/t_18318fd9, commits de046cde + 78c00b6e on origin/main 16b04b0f.

## Task restatement

`hscc_daemon/tests/test_kanban_cli.py::TestStatusNamesBlockingTasks::test_status_json_names_blocking_tasks`
failed ~1 in 2 daemon-leg runs with `JSONDecodeError: Expecting ',' delimiter: line 1 column 6`
because a WARN line emitted by `daemon_ops.log` in "foreground mode" (no PID_FILE)
was prefixed to the JSON the test parses out of capsys stdout.

## Decision: (a) AND (b) — (b) alone is not defensible

Card asked to pick (a) or (b) with evidence, and to prove it is acceptable if the
real-world --json path is left able to emit a WARN prefix. It is not acceptable,
so the stdout channel itself was fixed (a) and the tests were made hermetic (b):

- The repo's own convention is machine output on stdout, diagnostics on stderr.
  Evidence: `hscc-bootstrap/doctor.py:513` and `hscc_daemon/cluster_live.py:248`
  (`print(..., file=sys.stderr)`), `api_cli.py` (10 stderr prints), and
  `autodown_cli._error` itself (`autodown_cli.py:171`). `daemon_ops.log`'s
  foreground `print(line)` (daemon_ops.py:514) was the only violation in the
  daemon's log path.
- No production consumer reads the daemon's stdout. `install.py:47-49` sets
  StandardOutPath and StandardErrorPath to the SAME `~/.hscc/daemon.log`, so the
  daemon's log is byte-identical after the change; the file write in `log()` is
  unconditional and untouched.
- No test asserted a log line landing on stdout (grepped all `*/tests/*.py` for
  `readouterr()` + WARN/ERROR/INFO and for the `] [ WARN]` line format; only
  fixtures writing log-file CONTENTS, not stdout).
- (b) alone would have left `hscc autodown status --json | jq` broken for every
  real caller on any host with an intermittent GitHub API — the flake was just
  the visible symptom.

## Root cause chain (each link verified in this run, not just the card's)

1. `autodown_cli._cmd_status` calls `autodown._has_active_pr_ci()` with no
   checker (autodown_cli.py:266) -> `autodown.py:1105` binds the real
   `_screen_prci`.
2. `_screen_prci` -> `_screen_prci_uncached` -> `_gh_count` x3 per repo over
   `DEFAULT_PRCI_REPOS` (4 repos = 12 live `gh` probes); ANY nonzero exit
   -> `_PRCI_UNREACHABLE` (autodown.py:1033-1034, fail-safe).
3. `_note_prci_unavailable` -> `daemon_ops.log(..., "WARN")` (autodown.py:1155);
   `log()` printed to stdout when `PID_FILE` absent (daemon_ops.py:513-514).
4. The daemon-leg conftest redirects `PID_FILE` to a per-test tmp path that
   never exists (conftest.py:243), so foreground mode is ALWAYS on in tests.
5. Deterministic repro (scratch script, `gh` stubbed to rc=1) reproduced the
   card's exact error:
   `stdout = "[2026-10-31T11:09:31Z] [ WARN] Autodown PR/CI interlock unevaluable: ...\n{...json...}"`
   -> `JSONDecodeError: Expecting ',' delimiter: line 1 column 6 (char 5)`.

## What changed

1. `hscc_daemon/daemon_ops.py` — foreground echo now `print(line, file=sys.stderr)`;
   `import sys` added; docstring records the machine-channel contract.
2. `hscc_daemon/tests/test_daemon_ops.py` — `TestLog` pins the stream contract
   both directions: foreground echo lands on stderr with stdout EMPTY and the
   file still written; daemon mode (PID file present) prints nothing at all.
3. `hscc_daemon/tests/test_kanban_cli.py` — autouse `_no_live_prci_probes`
   fixture (the file lacked the `_no_active_prci` autouse fixture
   `test_autodown_cli.py` already had — that asymmetry is why only this file's
   two `cmd_autodown` tests flaked). Teardown-assertion based (a raise inside
   the stub would be swallowed by `_has_active_pr_ci`'s `except Exception`).
4. `hscc_daemon/tests/conftest.py` — **GH PROBE FENCE**, package-wide, modelled
   on the existing sparkrun argv fence in the same file: injected-runner probes
   pass through to the real screen untouched; unstubbed probes are blocked
   (returning the production `_PRCI_UNREACHABLE` fail-safe), counted in the
   terminal summary (`GH PROBE FENCE: N ...`), and failed at teardown.
   Mutation-verified: a probe test that calls `_screen_prci()` unstubbed now
   FAILS (previously passed while burning 5.4s of real `gh` calls).
5. `hscc_daemon/autodown.py` — second defect found by the new e2e test:
   `_has_active_pr_ci` fell through to `_note_prci_active()` after logging the
   "unevaluable" WARN, clobbering ok=False/reason and setting blocking="PR/CI".
   Effect: `autodown status` claimed "PR/CI check OK — blocked by open PR /
   active CI run" when GitHub was never reached, and `prci_blocking_signal()`
   could never return the sentinel its own docstring promises, making
   `autodown_cli.py:269-271` ("PR/CI source unreachable — treating as active")
   plus `prci_ok:false`/`prci_reason` in --json dead code. Now records the
   sentinel. Fail-safe polarity unchanged (unreachable ⇒ active ⇒ never tears
   down). New `_note_prci_clear()` resets all four `_PRCI_LOAD` fields on a
   clear screen (the old `_note_prci_blocking(None)` left a stale ok=False
   behind after a recovered blip).
6. Two pre-existing NON-HERMETIC tests fixed (found by the fence; they ran 12
   live `gh` probes EVERY run via `cycle()`'s default probes ->
   `probe_prci_activity`, a second consumer of `_screen_prci` separate from
   `_has_active_pr_ci`): `TestAutodownLoop.test_tick_missing_cycle_is_noop` and
   `TestNoLiveHsccLeak.test_cycle_with_default_probes_does_not_touch_real_file`.
   Both stub the screen now; both keep their original subject.
7. New regression test `TestStatus::test_status_json_survives_a_real_gh_blip`
   (test_autodown_cli.py) drives the REAL chain — real `_screen_prci_uncached`
   probe loop, real sentinel, real `_note_prci_unavailable` WARN, real CLI JSON;
   only `subprocess.run` stubbed to rc=1 on one repo's in_progress probe.
   Asserts stdout is PURE JSON (`json.loads` ok, `prci_active` True,
   "unreachable" in blocked_by) AND the WARN is still on stderr AND in the log
   file. Its first version passed for the wrong reason (fake matched literal
   `"run list"` in argv; `_gh_count` passes `verb.split()`) — caught precisely
   because the test asserts the interlock is ACTIVE, not merely that stdout
   parses. That argv-shape trap is documented in the test.

## Verification so far

- Deterministic repro of the exact card error before the fix (above).
- `test_kanban_cli.py + test_daemon_ops.py` isolated: 75 passed.
- 4 affected files: 335 passed.
- Mutation probe of the fence: unstubbed `_screen_prci()` call → teardown
  AssertionError "GH PROBE FENCE GUARD" (guard proven to bite); blocked run
  1.2s vs 5.4s of real gh calls.
- `test_daemon_ops.py + test_no_live_hscc_leak.py` after fence fixes: 50
  passed, `GH PROBE FENCE: 0` (nothing in the leg touches GitHub).
- Pending: full `bash scripts/run_tests.sh` ALL GREEN, daemon leg x2
  consecutive + `address_guard --tracked` clean.

## Known pre-existing flake (NOT caused by this card)

First full-suite run of this branch: `1 failed, 2663 passed, 2 errors`. The 2
errors were the two non-hermetic tests named above (now fixed). The 1 failure
is `test_no_live_hscc_leak.py::test_suite_never_writes_live_hscc` (the
sandboxed subprocess run). It is being re-verified on the fixed tree; if it
persists it must be root-caused — this card does not complete with a red leg.
