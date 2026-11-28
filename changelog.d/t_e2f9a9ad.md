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
---
kind: Verified
task: t_e2f9a9ad

- `hscc_daemon/tests/test_health.py`: new `TestProxyLivenessReplay` pins the
  port-dead and state-missing signals independently, exact-argv replay,
  durable-before-spawn ordering, no restart on a healthy proxy, pending (not
  "back") when nothing is observed, and the recorded argv surviving a state
  rewrite.
- `hscc-cluster/tests/test_apply.py`: `install_proxy` records the exact
  `--port 4000 --cluster hscc` argv for the daemon's replay.
- Run: `HSCC_TEST_LEGS=hscc_daemon bash scripts/run_tests.sh`.
