# t_6b994390 — REPORT: RICH CLI hscc_daemon/api_cli.py (raw prints -> themed, no-ANSI tests)

**Task:** Re-scope of `hscc-api` (correctly diagnosed as no-CLI, refused in
t_8899bca2). The real target is `hscc_daemon/api_cli.py` — the human CLI behind
`hscc api start|stop|status`. The last unthemed operator-facing CLI surface in
the repo. Convert its raw `print(` sites to the themed Rich CLI surface
(`hscc_daemon/cli_theme`), keeping any machine path byte-identical, and add
no-ANSI regression tests.
**Worker:** worker
**Branch:** `wt/api-cli-theme`
**Base:** `main`

## Status

DONE — implementation + no-ANSI tests committed (`3ef304a`); hscc_daemon suite
green under BOTH interpreters. Merged to main, pushed, deployed. Full 8-package
suite green under both interpreters.

## Scope — what was themed, what was deliberately left raw

`api_cli.py` is the `hscc api start|stop|status` lifecycle wrapper. It has NO
`--json` machine path (all commands are operator verbs); the only byte-exact
contract is `_build_qr_payload` — WIRE DATA for the iOS scanner, encoded into
the QR matrix, never themed.

**Themed (this card) — all human stdout output:**
- `_print_api_qr` — "Scan to connect" prose + loopback warning + the QR block
  art (art rendered verbatim via console; only prose carries inline `[warn]`
  markup). The QR payload string is untouched.
- `_warn_payload_drift` — the loud drift banner (WARNING line carries `[warn]`
  markup; text content otherwise identical so the injected-drift assertions
  still hold).
- `_handle_start` — already-running / starting / started (PID)/(child PID).
- `_handle_stop` — not-running / stopping / stopped / force-killed /
  already-stopped.
- `_handle_status` — running / stale-PID / not-running + Listening host:port in
  a themed `make_status_panel` (ok on running, warn otherwise); token-missing
  note.
- `cmd_api` — group help via console + unknown-subcommand error.

**Left raw (documented decisions — error diagnostics, not command results):**
- Every `print(..., file=sys.stderr)` error/warning path — `Error:` on start
  config/token failures, `Error stopping`, `Error:` on status server-load,
  `Warning: could not resolve bind address`, and the payload-drift check-failure
  stderr line. These are plain on stderr, matching the autodown_cli / kanban_cli
  `_error` convention (errors stay readable, not restyled).
- `_build_qr_payload` — the iOS wire contract, byte-identical.
- The QR matrix itself — fixed-width block art, unthemed (theming would break
  alignment); routed through the console only for consistent no-ANSI degrade.
- `_serve` / `_sigterm_handler` — daemon-process internals that log to the
  api.log FILE via `daemon_ops.log`, not CLI stdout (not render sites).

**Raw-site count:** 28 raw `print(` on main (per task body) → **0 raw stdout
human `print(` remaining**; 7 raw `print(` remain, ALL to `file=sys.stderr`
(error/warning diagnostics, deliberately raw).

## Process

Mirrors the established sibling-group pattern (autodown_cli / kanban_cli):
- Added `_console(theme_name=None, **kwargs)` — `make_console` with the
  anti-collapse width convention (TTY → real width, non-tty → 200).
- Added `_strip_theme(args)` since `hscc.py` passes raw `args[1:]` to
  `cmd_api`; `--theme` is stripped before dispatch and the theme name threaded
  into each handler (defaults to auto-detect when called directly).
- `cmd_api` dispatches through `_handle_start/_handle_stop/_handle_status`
  with `theme_name` as a keyword arg.

## How ran

Changed-package suite (hscc_daemon) under BOTH interpreters:

| Interpreter | Command | Result |
|---|---|---|
| A (hermes venv) | `~/.hermes/hermes-agent/venv/bin/python -m pytest hscc_daemon/tests/ -q` | **1149 passed** (1136 baseline + 13 new no-ANSI) |
| B (conda p313) | `/Users/desac/miniconda3/envs/p313/bin/python -m pytest hscc_daemon/tests/ -q` | **1149 passed** |

## Merge / push / deploy status

- **Branch:** `wt/api-cli-theme`
- **Commits ahead of main (mine, pre-merge):** 1 (`3ef304a` theme api_cli + no-ANSI tests).
- **Merge:** (pending — see below)
- **Push:** (pending)
- **Deploy:** (pending)
- **No gateway restart:** the change is CLI-only (`api_cli.py` is the lifecycle
  wrapper for `hscc api start|stop|status`); it is not on the daemon's live
  loop and does not touch the serving/health/state paths. No restart performed.

## Tests added

`hscc_daemon/tests/test_api_cli_no_ansi.py` (13):
- status no-ANSI (not-running / running / stale-PID / with-QR)
- start no-ANSI (with and without QR)
- stop no-ANSI (not-running / running)
- help + unknown-subcommand no-ANSI
- markup integrity (no literal `[ok]`/`[warn]`/`[error]`/`[title]` leak on piped)
- QR wire contract byte-identical
- `--theme` stripped + forwarded to handlers

Also updated `test_unified_cli.py` TestApiRouting stubs to accept `theme_name`
(the handlers gained the keyword); routing assertions unchanged.
