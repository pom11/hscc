kind: Added
task: t_5da7fd56

- **`hscc orch inject <profile> "<message>"` now dispatches from the unified
  CLI.** The verb itself landed with t_75f17c83, but only reachable through the
  standalone engine entry (`python3 hscc-cluster/hscc.py orch inject ...`):
  `hscc_daemon/hscc.py main()` had no `orch` group branch, so the command the
  operator asked for printed `Unknown command: orch` and exited 1. `main()` now
  routes `orch` to the cluster engine's `cmd_orch` router through
  `_load_cluster_engine` — the same shape `_handle_cluster` uses — and owns
  only rendering + exit codes: the plain view prints the receipt line (a dry
  run prints `repl_inject.format_plan` verbatim), `--json` dumps the raw
  receipt exactly as the standalone entry does, and every refusal
  (`InjectError` no-pane/ambiguous/empty/bad-mode, `ValueError` bad argv or
  unknown verb) becomes one reported line + exit 1, never a traceback. The
  branch holds ZERO pane logic — mode vocabulary, verb prefixes and the busy
  read stay in `repl_inject`, so the two surfaces cannot drift. Also:
  `COMMAND_HELP["orch"]`, the `hscc --help` banner line, the group listing
  (`hscc orch` / `orch --help` / `orch inject --help`, intercepted before the
  engine loads so help runs nothing — on the RAW tail, so `--theme --help`
  cannot eat the flag) and the unknown-command verb list. A flag-shaped token
  after `--theme` (e.g. `--theme --dry-run`, which the stripper would swallow
  and turn a planned send into a real one) refuses with exit 1 before the
  engine loads — the F6 fail-closed rule `_handle_cluster` uses for its
  destructive verbs. `hscc orch waker …` (orch-waker W3) lands on this same
  branch.
---
kind: Verified
task: t_5da7fd56

- `hscc_daemon/tests/test_cli_orch_group.py` (new): dispatch through a stub
  engine (tail forwarded verbatim, `--theme` stripped, `--json` kept, plain vs
  JSON rendering, refusals → exit 1 with no traceback, missing engine → exit
  1), help inertness (seven help shapes × a loader landmine: a help request
  that imports the engine fails the test; `-h`/`--help` byte parity; 78-col
  gate budget on the printed block, `COMMAND_HELP["orch"]` and the banner), and
  the acceptance end-to-end — the REAL `hscc-cluster/hscc.py` loaded via
  `_load_cluster_engine` with only `repl_inject` stubbed, proving `hscc orch
  inject demo-orch "msg" --dry-run` reaches `cmd_orch_inject` → `inject()`
  with `dry_run=True` while `subprocess.run`/`Popen`, the engine's `run_cmd`
  and the stub's `paste`/`deliver`/`run_tmux` are rigged to fail on any send
  (no real tmux; fixtures fictional: `demo-orch`, `s1:0.1`).
