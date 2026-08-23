# Idle Autodown — implementation map (per-phase files/functions)

Companion to `docs/design/idle-autodown.md`. Lists, per phase, the exact files
and functions each implementation card touches. All paths relative to the repo
root. New files are marked (new).

## Phase 1 — config + core state module
- `hscc_daemon/autodown.py` (new): `load_config()`, `save_config()`,
  `record_activity(source)`, `classify(...)`, `_has_active_work(kanban_db)`.
- `hscc_daemon/tests/test_autodown.py` (new).

## Phase 2 — idle timer thread in the daemon loop
- `hscc_daemon/daemon_ops.py`: `run_daemon_loop()` (exists, line 167) — add
  `run_autodown_loop()` and start it alongside the watchdog thread (line 252).

## Phase 3 — idle evaluation + safety interlocks
- `hscc_daemon/autodown.py` (new, additions): `cycle()`.

## Phase 4 — teardown sequence + watchdog coordination
- `hscc_daemon/autodown.py` (new, additions): `teardown()`, internal step
  helpers for per-unit `sparkrun stop` + verify + rollback.
- Reads/writes via `hscc_daemon/lifecycle.py`: `save_watchdog_block()` (line
  141), `load_watchdog_block()` (line 132) — adds the `intentional` field.
- Reuses `hscc_daemon/serving.py`: `load_serving()` (line 37),
  `keepalive_units()` (line 172), `VLLM_STOP_CMD` (line 150).

## Phase 5 — wake sequence + first-message handling
- `hscc_daemon/autodown.py` (new, additions): `autoup()`,
  `_handle_http_wake()`, `_notify_waking()`.
- Reuses `hscc_daemon/serving.py`: `VLLM_START_CMD` (line 151),
  `orchestrator_recipe()` (line 82).
- Reuses `hscc_daemon/health.py`: `http_check()` (readiness polling).

## Phase 6 — activity-source probes for inbound signals
- `hscc_daemon/autodown.py` (new, additions): probe/dispatch glue tying the
  four sources (§1d) into `record_activity`.
- `hscc-api/api_server.py`: `ApiHandler._route()` / request handling (line
  412) — stamp `state.write_state('activity', ...)` on each authenticated
  request.
- `hscc_daemon/state.py`: `write_state()` (line 20) — used for the activity
  file.
- Telegram inbound stamp: contract hook in the always-on Telegram MCP daemon
  (`~/.hermes-tg/mcp_server.py`, external) — interop confirmed in this phase.

## Phase 7 — CLI verb group `hscc autodown`
- `hscc_daemon/autodown_cli.py` (new): `cmd_autodown(argv)`, subcommand
  handlers `_handle_status`, `_handle_enable`, `_handle_disable`,
  `_handle_wake`, `_handle_cancel`.
- `hscc_daemon/hscc.py`: `main()` (line 702) — add an `autodown` branch
  (mirror the `api` block, line 738); `_get_help_text()` (line 257),
  `COMMAND_HELP` (line 330).
- `hscc_daemon/tests/test_autodown_cli.py` (new).

## Phase 8 — daemon-start recovery (daemon died while down)
- `hscc_daemon/daemon_ops.py`: `run_daemon_loop()` (line 167) — startup block.
- `hscc_daemon/autodown.py` (new, additions): `resume_from_restart()`.

## Dependencies
1 → 2 → 3 → 4 → 5 sequential; 6 after 5; 7 after 1+4+5; 8 after 2+4.
