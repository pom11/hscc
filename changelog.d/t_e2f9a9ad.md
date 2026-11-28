kind: Fixed
task: t_e2f9a9ad

- **A dead worker proxy is now detected from the port OR from sparkrun's
  state, and restarted by replaying the argv hscc actually started.** The
  2026-11-27 shape was `No proxy state found` with nothing listening on :4000;
  `check_proxy` decided health only from its `/v1/models` probe and rebuilt a
  launch command when it did repair. `check_proxy` now reads two independent
  liveness signals before its catalogue verdict — the listener on the port and
  sparkrun's own `proxy status --json` record — and either missing signal is a
  dead proxy. A confirmed dead proxy also no longer sleeps out the normal
  relaunch cooldown.
- **The restart is a detached replay with an observed verdict.** hscc records
  the exact start argv in its own durable `proxy_launch` stream (the proxy check
  rewrites `proxy.json` every tick, so the seed cannot live there). On death the
  tick writes the detection and the issued argv to state BEFORE spawning, runs
  the recorded argv in a new session, then polls the port. `restart_state` is
  `verified` only when a listener is observed again; otherwise it stays
  `issued`/pending and the next tick finishes the observation. A watchdog that
  loses its own inference path mid-restart therefore cannot erase the report or
  claim a recovery it did not see.
- **Both hscc start paths seed the replay.** The daemon's relaunch/repair paths
  record the argv they execute, and `hscc cluster apply`'s `install_proxy`
  records `sparkrun proxy start --port <port> --cluster hscc` after a successful
  start, so the port and cluster survive a later replay.
- **A replay still starting is observed, not spawned again.** The first replay
  tick could only wait `PROXY_REPAIR_VERIFY_WAIT_S` (20 s); a litellm-through-uvx
  launch often takes longer, so the next tick found the port free and started a
  SECOND detached proxy. While the durable `restart_state: issued` row is
  younger than `PROXY_RELAUNCH_DOWN_COOLDOWN_S` and its `restart_pid` is alive,
  `check_proxy` now observes the port only — no lease, no strike, no spawn — and
  writes `verified` if the child has bound. A dead child with a free port still
  replays (the dead-entrypoint case), and a verified successful listener resets
  the streak.
- **Replay attempts are bounded by the existing durable streak.** An argv that
  never binds used to spawn once per tick forever. The replay path now counts
  against `relaunch_streak` / `PROXY_RELAUNCH_MAX_CONSECUTIVE`: at the cap it
  stops spawning and raises the existing `proxy.relaunch.stalled` alarm instead,
  so a broken replay pages once rather than storming.
- **The replay executes the same sparkrun binary that was recorded.** `install_proxy`
  records the bare `sparkrun` name from an interactive shell, but the daemon runs
  under a launchd PATH where that name can resolve to a different install or not
  at all. The recorded list is kept verbatim and `argv[0]` is resolved at REPLAY
  time through `hscc_daemon.sparkrun_bin.exec_argv`; an absolute `argv[0]` passes
  through unchanged and every recorded argument (port, cluster) stays
  byte-identical. The resolved argv is what `restart_argv` carries.
---
kind: Verified
task: t_e2f9a9ad

- `hscc_daemon/tests/test_health.py`: new `TestProxyLivenessReplay` pins the
  port-dead and state-missing signals independently, exact-argv replay,
  durable-before-spawn ordering, no restart on a healthy proxy, pending (not
  "back") when nothing is observed, and the recorded argv surviving a state
  rewrite. Follow-up cases pin the slow binder (tick 1 spawns; tick 2 with the
  PID alive spawns nothing; a later tick verifies), a dead child with a free port
  (exactly one new spawn), an always-failing argv (spawn count stops at
  `PROXY_RELAUNCH_MAX_CONSECUTIVE` and the stall alarm fires), and replay-time
  binary resolution (bare name resolved through a fake `exec_argv`, absolute
  name untouched, arguments byte-identical).
- `hscc-cluster/tests/test_apply.py`: `install_proxy` records the exact
  `--port 4000 --cluster hscc` argv for the daemon's replay.
- Run: `HSCC_TEST_LEGS=hscc_daemon bash scripts/run_tests.sh` and
  `HSCC_TEST_LEGS=hscc-cluster bash scripts/run_tests.sh` — both green.
