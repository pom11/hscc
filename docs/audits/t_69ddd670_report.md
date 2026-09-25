# t_69ddd670 — REPORT: RICH CLI hscc_daemon residual (raw sites -> themed, no-ANSI tests)

**Task:** PART 1 RICH CLI — hscc_daemon residual theming. Convert the REMAINING
raw `print(`/`json.dumps(` in NON-TEST hscc_daemon code to the themed Rich CLI
surface, reusing the existing hscc_daemon `cli_theme` module.
**Worker:** worker
**Branch:** `wt/t_69ddd670`
**Base:** `main`

## Status

IN PROGRESS — implementation + tests committed; suite green under BOTH
interpreters for the changed package (hscc_daemon: 1136 passed each). Pending:
full 8-pkg suite both interpreters, merge/push/deploy.

## Scope — what was themed, what was deliberately left raw

The earlier [1/5]..[5/5] epic themed 7 hscc_daemon files (cli, hscc, install,
autodown_cli, cluster_render, api_route_sweep, verify_chat_roundtrip). This
card themes the REMAINING **human CLI command-output** surfaces. Daemon
background / machine-JSON output is NOT a command result and was deliberately
left raw (documented in each site below).

**Themed (this card):**
- `hscc_daemon/kanban_cli.py` — `hscc kanban stale` human list (Rich Table),
  archive-confirm (status panel), help. `--json` stays raw/byte-identical.
- `hscc_daemon/kanban_blocked.py` — `hscc kanban blocked` human list (Rich
  Table with why/comments), recover-confirm (status panel), help. `--json`
  raw. Errors/warnings + unknown-subcommand stay plain on stderr (like
  autodown_cli).
- `hscc_daemon/event_driven.py` — standalone CLI `install`/`uninstall`/
  `status` views (peer-guarded `cli_theme` import + minimal rich fallback for
  standalone-script runs), `main()` help/unknown/error. Added module-level
  `import sys` (was only imported in `__main__`).

**Left raw (documented decisions — not command results / not restyled):**
- `event_driven.run_tests()` — the module's own self-test harness, not a themed
  command result; pass/fail debug output stays plain.
- `event_driven` line ~87 kqueue watcher callback; daemon background log.
- `daemon_ops.stream_watcher` + `daemon_ops.log()` — live daemon tail; cli.py
  + test_cli_no_ansi explicitly document these are NOT restyled (streaming).
- `health.py` `_SPARKRUN_STATUS_SCRIPT` (a string payload for sparkrun's
  interpreter) + the `/v1/chat/completions` request body — not render sites.
- `serving.py` import-time fallback stderr; `desktop.py` JSONL file write;
  `escalate_watcher.__main__` machine JSON dump.

## How ran

```
export HOME=/Users/desac
HSCC_TEST_PY=<interp> bash scripts/run_tests.sh
```

Full 8-package suite green under BOTH interpreters (serially, ~13 min each):

| Interpreter | Path | Result |
|---|---|---|
| A (hermes venv) | `~/.hermes/hermes-agent/venv/bin/python` | **ALL GREEN** (bootstrap 256, commands 69, roles 114, cluster 422, project 1351, **hscc_daemon 1136**, sparkrun-hermes 12, api 786 passed/1 skipped) |
| B (conda p313) | `/Users/desac/miniconda3/envs/p313/bin/python` | **ALL GREEN** (bootstrap 256, commands 69, roles 114, cluster 404 + 14 skipped, project 1351, **hscc_daemon 1136**, sparkrun-hermes 12, api 786/1 skipped) |

hscc_daemon (the package under change) alone: **1136 passed** under BOTH
interpreters. This card adds 13 no-ANSI regression tests (4 kanban stale, 4
kanban blocked, 5 event-driven CLI); the on-main baselines were already
green, and the pre-existing kanban group suites covered routing.

## Merge / push / deploy status

(pending — completes after both suites confirmed green)
