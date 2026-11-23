kind: Added
task: t_c920b0ca

* `hscc-cluster`: new read-only `main_status` agent tool — the configured main
  orchestrator (name/profile/board) plus its live shape: the tmux pane and
  busy/idle/not-running/ambiguous state, and the daemon waker's
  enabled/freshness/board state. The main half calls the CLI's own verb
  `hscc_daemon.main_cli.cmd_main(["status", "--json"])`, so the tool and
  `hscc main status` cannot disagree about the pane or the identity; that identity
  comes from the ONE resolver `hscc-roles/main_orch.py` (the module
  `hscc main status` and `orch_message` read), never a hard-coded name. The
  `waker` block is `orch_waker.waker_status()`, the function the
  `hscc orch waker status` verb renders. Lifecycle stays CLI/operator-only:
  there is no tool for `hscc main start`, `hscc main wakeups` or a rename.
---
kind: Verified
task: t_c920b0ca

* `hscc-cluster/tests/test_main_status_tool.py` (new, 23 tests green): payload
  parity with `hscc main status --json` for the main fields, resolver-default and
  stored/renamed identity (proving the name is not baked into the module), live
  pane shapes (idle/busy/not-running/ambiguous/unreadable), the waker block from
  a planted config + state ledger, read-only state snapshots across both doors,
  structural bans on lifecycle/waker writers and transports, and the schema +
  manifest pins. Hermetic: every `~/.hscc` / `~/.hermes` read redirected to a
  per-test tmp dir, pane discovery injected, no fleet or subprocess reached.
* `hscc_daemon/tests/test_plugin_manifest_drift.py`: manifest/registry receipt
  bumped 29 -> 30 for this tool, then 30 -> 31 after rebasing onto main's
  `heal_status` bump (t_14b86753), with a guard asserting no
  `main_start`/`main_wakeups`/rename tool is declared.
* `hscc_daemon/tests/test_skill_tool_coverage_t_cb167686.py` +
  `install/hscc-skills/hscc-cluster/SKILL.md`: the bundled skill documents the
  ninth read tool; `main start`/`wakeups` are recorded as the refused lifecycle
  half, and the deferred D9 HTTP read routes are named as routes, not tools.
