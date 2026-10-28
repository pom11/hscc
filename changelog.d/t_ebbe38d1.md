kind: Added
task: t_ebbe38d1

- **Nodes can now self-recover from a hard hang.** A DGX Spark has no BMC, so
  when `.244` froze solid on 2026-10-27 (journal stops mid-sentence, no panic,
  no oops, no softlockup line) the only recovery was a human at the power
  button ~2h later. `hscc-cluster/node_recovery.py` arms the GB10's unused
  SBSA hardware watchdog through systemd —
  `/etc/systemd/system.conf.d/90-hscc-watchdog.conf` with
  `RuntimeWatchdogSec=30s` + `RebootWatchdogSec=2min`, then `daemon-reexec` —
  so PID1 pets `/dev/watchdog0` and a hang resets the SoC instead of wedging.
  Installed by bootstrap (`Install: node hardware watchdog`, non-fatal,
  `--skip-node-recovery`); transport is sparkrun's own remote execution, root
  via `sudo -n` only, never a password prompt. Idempotent (content-compares,
  atomic same-dir rename), and it will not steal a device: an already-held
  `/dev/watchdog0` or an operator-edited drop-in is refused/reported as drift.
  Opt out with `node_recovery: {watchdog: false}` in `~/.hscc/cluster.json`
  (default ON when absent; subkeys are read independently, so the sysctl
  sibling's `autoreboot` can never disarm this). Probe surface for the doctor
  check: `probe_node()` / `is_armed_state()`.
---
kind: Fixed
task: t_ebbe38d1

- **"Watchdog configured" no longer counts as "watchdog armed".** systemd can
  adopt `RuntimeWatchdogSec` and still never open the device — e.g. a driver
  that does not advertise `WDIOF_CARDRESET`, or a PID1 that refused the reexec.
  A check on the config value alone would report those nodes protected while a
  hard hang wedges them exactly as before. `node_recovery.py` now reads the
  watchdog core's own sysfs `state` (and `wdctl`) and refuses to call a node
  armed while it reads `inactive`, in both `--arm` and `--probe`.
