kind: Fixed
task: t_c14d18cb

- **`health.py`'s gateway liveness probe used the transport #148 proved blind.**
  `_gateway_job_alive()` was the tree's SECOND gateway-liveness implementation and
  its last-resort leg was `pgrep -f "hermes.*gateway"`. On the operator machine
  macOS `pgrep` cannot SEE the gateway process at all (`pgrep -f .` → 1210 pids,
  `ps -axww -o pid=` → 1216; pid 1, the gateway and four argv-unreadable helpers
  are in the ps set only), so that leg answered False for a live gateway. It was
  masked only because the `launchctl` leg above it answered first — i.e. the
  GAP-F-3 failure was one probe failure away from repeating itself in the daemon.
  The same leg had a second, quieter bug: `run_cmd` renders a timeout as
  `{"ok": False, "output": "Command timed out after 5s"}` and the leg tested the
  *output*, so a hung `pgrep` returned True and the gateway read alive no matter
  what was actually running.
  The verdict now comes from ONE implementation, `hscc-bootstrap/gateway_liveness.py`
  (the transports #148 landed in the doctor row): supervisor first
  (`launchctl list ai.hermes.gateway` / `systemctl --user is-active
  hermes-gateway`), then a token scan of `ps -axww` argv. `pgrep` is not invoked
  anywhere, and the daemon reaches the shared file through
  `hscc_daemon/gateway_probe.py` (sibling-plugin loader, same shape as
  `guard_posture.py` → `install_hooks.py`). A probe that cannot run is now
  UNKNOWN rather than a verdict: the check publishes `gateway_job: null` plus a
  named `gateway_job_degraded` reason and does NOT claim the gateway is healthy —
  the fail-closed rule `check_dgx` already applies to an unverifiable workload
  list (t_b543e530), and what is new versus a bare failure is that the stream
  says which probe broke instead of blaming the gateway for it.
---
kind: Verified
order: 1
task: t_c14d18cb

- `hscc_daemon/tests/test_health.py`: `TestGatewayJobAlive` 6 -> **15** cases. The
  pinned acceptance case is the GAP-F-3 shape in the daemon's own copy — launchd
  unreadable, `pgrep` answered rc 1 (as it does here for a LIVE gateway), gateway
  visible only in `ps` argv → the stream must read alive — plus a
  `pgrep`-must-not-be-invoked probe, the timeout-still-reads-alive bug above,
  both supervisor legs, the argv fallback on macOS and Linux, a chat prompt that
  merely mentions the words staying unmatched, and the UNKNOWN paths (unloadable
  module, silent transports, and the cycle publishing `gateway_job: null` +
  `gateway_job_degraded` + an UNVERIFIED message rather than a false green).
- `hscc_daemon/tests/test_gateway_probe.py`: **new, 9** cases — the loader finds
  the sibling in both deployment layouts, validates the three names it consumes,
  and raises naming the paths it tried rather than returning a fake verdict.
- `hscc-bootstrap/tests/test_gateway_liveness.py`: **new, 43** cases covering the
  shared module directly (`gateway_states`, `job_is_alive`, `gateway_verdict`,
  argv matching) so the two renderers cannot drift from each other at the source.
- Live check on the operator machine (read-only; gateway untouched): both
  consumers report the real gateway — daemon `job_is_alive() -> (True, 'launchd
  pid <gateway-pid>')` and doctor `Check(ok=True, detail='running (launchd pid
  <gateway-pid>)')` — and the pgrep blindness was re-measured in the same run
  (pgrep 1237 pids vs ps 1244, gateway pid `pgrep_sees_it=False`).
