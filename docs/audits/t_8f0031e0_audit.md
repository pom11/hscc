# Audit note — t_8f0031e0: theme daemon/daemon_install/start/update/init CLI output (flights Rich epic, card 3a/21)

## What changed

Converted the human-facing output of 5 command files in `hscc-project/flightdeck/commands/`
from bare `print()` to the shared themed Rich layer (`./_theme` — the single
`_theme.py` helper introduced in card 1, which does a guarded lazy import of
`hscc_daemon.cli_theme`):

- `daemon.py` — status/check/log/install/notify/start/stop surfaces now render
  through `panel`/`status_panel`/`table` + `make_console()`.
- `daemon_install.py` — install dry-run and apply/uninstall reports themed.
- `start.py` — human RELEASE PLAN themed; `--json` path untouched (byte-identical).
- `update.py` — dry-run plan + `--apply` result themed.
- `init.py` — dry-run/apply + environment report + next-steps themed; literal
  `[ok]`/`[MISSING]`/`[UNVERIFIED]` marks are `escape()`d so they render as plain
  brackets (never swallowed as Rich markup).

All human text (dispatch messages, card titles, env details, repo paths) is
`escape()`d so literal Rich markup like `[dry-run]` renders literally. Error
paths stay plain `print(..., file=sys.stderr)` (deliberately NOT themed) —
matches cards 1/2 and the daemon `_error` handling.

No second theme layer was created; every file imports from `._theme`.

## Hard invariants — verified

1. `--json` output BYTE-IDENTICAL.
   - `start.py` is the only converted command with a `--json` path. Its
     `_print_json` is BYTE-IDENTICAL to main (verified by
     `diff <(git show main:...start.py | sed -n '/def _print_json/,/^def /p')`
     → empty; the only `json.dumps` diff line is a docstring sentence). It still
     `print(json.dumps(...))` raw, never through a Console.
   - Terminal proof (`docs/audits/t_8f0031e0_verify_e2e.py`): `start --json`
     output equals `json.dumps(canonical) + "\n"` exactly, no panel border, no
     ANSI.
   - The other 4 converted commands (update/init/all daemon subcommands) have
     no `--json` path — every line is human, themed.
2. Non-TTY piped output has NO ANSI (`\x1b`). Rich degrades to plain on a
   non-TTY console (`make_console()` recreates at width=200 when not a
   terminal — preserved). Terminal proof + the 43 no-ANSI unit tests assert
   no `\x1b[` AND no `\x1b`, content preserved.
3. Errors stay plain stderr (NOT themed) — checked in each converted file.
4. Did NOT touch sibling files for other sub-cards (review/roadmap/standup/why,
   ingest/sync/release/monitor/metrics, doctor/decompose/legacy/lint/verify).
   Modified only the 5 command files + the 4 test files for this card's commands.

## Regression tests added

43 new no-ANSI / --json tests across the 4 command test files
(`test_daemon.py`, `test_init.py`, `test_start.py`, `test_update.py`), each
capturing stdout as a NON-tty StringIO via `redirect_stdout`:

- daemon (safe read-only/no-side-effect paths): status-stopped, check-stream,
  log-empty, install-dry-run.
- init: dry-run and apply (with literal `[ok]` env mark).
- start: human plan (asserts literal `[dry-run]` renders) + `--json`
  byte-identity (`out == json.dumps(canonical)`), both no-ANSI.
- update: dry-run update-available, up-to-date, non-git — all no-ANSI.

Every test asserts neither `\x1b[` nor `\x1b` appear, and that expected content
is preserved.

## Main re-sync (standup flake)

The branch was originally based on `bc9b69a`. Main has since landed the
standup hermetic fix (`39fb82d`, from follow-up card t_d5780187) plus two docs
commits. Running the suite on the stale base failed 28 test_standup.py tests —
the known LIVE-STATE FLAKE. Rebased `wt/t_8f0031e0` onto main tip (`c9261a7`)
to pick up the hermetic fix; standup is now green on the branch. Standup is NOT
a real failure on main and is not traced to this card.

## Verification (suite numbers under BOTH interpreters)

Command: `HSCC_TEST_PY=<py> bash scripts/run_tests.sh` (per-dir pytest
processes, `HERMES_DELEGATED_CHILD_CONTEXT` unset — the documented harness
behaviour).

Interpreter A — `~/.hermes/hermes-agent/venv/bin/python`:
- hscc-bootstrap 248 passed, hscc-commands 59 passed, hscc-roles 101 passed,
  hscc-cluster 402 passed, hscc-project 1298 passed, hscc_daemon 1123 passed,
  sparkrun-hermes 8 passed, hscc-api 786 passed (1 skipped).
- ALL GREEN, exit 0.

Interpreter B — `/Users/desac/miniconda3/envs/p313/bin/python`:
- hscc-bootstrap 248 passed, hscc-commands 59 passed, hscc-roles 101 passed,
  hscc-cluster 384 passed (14 skipped — platform-dependent skips, green),
  hscc-project 1298 passed, hscc_daemon 1123 passed, sparkrun-hermes 8 passed,
  hscc-api 786 passed (1 skipped).
- ALL GREEN, exit 0.

Note: running pytest DIRECTLY (not via run_tests.sh) inside the delegated
worker context fails a handful of flightdeck tests with `unable to open
database file` at `kdb.connect(...)`. That is the documented
`HERMES_DELEGATED_CHILD_CONTEXT` read-fence artifact, not a code failure:
`run_tests.sh` unsets that var for exactly this reason (see the comment in
`scripts/run_tests.sh`). Re-running those tests with the var unset passes.

## Deliverable / git state

Branch `wt/t_8f0031e0`, commits ahead of main before merge (conversion + regressions):
- `401d029` flights(cli): theme daemon/daemon_install/start/update/init human output (Rich card 3a/21)
- `72dcad0` flights(cli): no-ANSI + --json byte-identity regressions for daemon/daemon_install/start/update/init (Rich card 3a/21)

(plus this audit note and the p313 numbers commit, both under docs/audits/).
All four must merge to main, push, then deploy via `python3 hscc-bootstrap/install_payload.py`.

## No AI attribution; no secrets; LAN host scrubbed (100.64.0.1 placeholder).
