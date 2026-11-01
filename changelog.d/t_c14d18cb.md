kind: Fixed
task: t_c14d18cb

- **`health.py`'s gateway liveness probe used the transport #148 proved blind.**
  `_gateway_job_alive()` was the tree's SECOND gateway-liveness implementation and
  its last-resort leg was `pgrep -f "hermes.*gateway"`. On the operator machine
  macOS `pgrep` cannot SEE the gateway process at all (`pgrep -f .` → 1210 pids,
  `ps -axww -o pid=` → 1216; pid 1, the gateway and four argv-unreadable helpers
  are in the ps set only), so that leg answered False for a live gateway. It was
  masked only because the `launchctl` leg above it answered first — i.e. the
  GAP-F-3 failure was one probe failure away from repeating itself in the daemon,
  where a red gateway stream drives an auto-heal relaunch.
  The verdict now comes from ONE implementation, `hscc-bootstrap/gateway_liveness.py`
  (the same transports #148 landed in the doctor row): supervisor first
  (`launchctl list ai.hermes.gateway` / `systemctl --user is-active
  hermes-gateway`), then a token scan of `ps -axww` argv. `pgrep` is not invoked
  anywhere, and the daemon reaches the shared file through
  `hscc_daemon/gateway_probe.py` (sibling-plugin loader, same shape as
  `guard_posture.py` → `install_hooks.py`). A probe that cannot run is now
  UNKNOWN rather than "down": `check_gateway()` keeps the last answer the
  transports really gave, publishes `gateway_job: null` plus a named
  `gateway_job_degraded` reason, and does not turn a probe outage into a relaunch.
---
kind: Verified
order: 1
task: t_c14d18cb

- `hscc_daemon/tests/test_health.py`: `TestGatewayJobAlive` 6 -> **16** cases. The
  pinned acceptance case is the GAP-F-3 shape in the daemon's own copy — launchd
  unreadable, `pgrep` answered rc 1 (as it does here for a LIVE gateway), gateway
  visible only in `ps` argv → the stream must read alive — alongside a
  `pgrep`-must-not-be-invoked probe, both supervisor legs, the argv fallback on
  macOS and Linux, a chat prompt that merely mentions the words staying unmatched,
  and the UNKNOWN paths (unloadable module, silent transports, and a degraded
  cycle publishing `gateway_job: null` + reason instead of a verdict).
- `hscc_daemon/tests/test_gateway_probe.py`: **new, 9** cases — the loader finds
  the sibling in both deployment layouts, validates the three names it consumes,
  and raises naming the paths it tried rather than returning a fake verdict;
  plus the contract that `check_gateway()` never lets the raise escape.
- `hscc-bootstrap/tests/test_gateway_liveness.py`: **new, 43** cases covering the
  shared module directly (`gateway_states`, `job_is_alive`, `gateway_verdict`,
  argv matching) so the two renderers cannot drift from each other at the source.
- Live check on the operator machine (read-only; gateway untouched): both
  consumers report the real gateway — daemon `job_is_alive() -> (True, 'launchd
  pid <gateway-pid>')` and doctor `Check(ok=True, detail='running (launchd pid
  <gateway-pid>)')` — and the pgrep blindness was re-measured at the same moment
  (pgrep 1237 pids vs ps 1244, gateway pid `pgrep_sees_it=False`).
