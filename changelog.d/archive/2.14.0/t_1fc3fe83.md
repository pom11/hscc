kind: Added
task: t_1fc3fe83

- **Panic-to-reboot sysctls installed cluster-wide by bootstrap.** A node
  hard-froze on 2026-10-27 with the journal dead mid-sentence and no BMC to
  reach — recovery was a human at the power button. `hscc bootstrap` now
  installs `/etc/sysctl.d/90-hscc-autoreboot.conf` on every sparkrun cluster
  node over the existing ssh path (`sudo -n`, never a password prompt):
  `kernel.panic=10`, `panic_on_oops=1`, `softlockup_panic=1`,
  `hung_task_panic=1` with `hung_task_timeout_secs=600` pinned alongside it
  (the deliberate choice against the 120s distro default — an ordinary NFS
  failover must not reboot a serving node), and `hardlockup_panic=1` only on
  kernels that expose the knob. Writes are atomic + content-compared
  (operator-edited file => drift report, never clobbered), every value is
  READ BACK and a mismatch fails that node loudly, `--dry-run` touches
  nothing, and `{"node_recovery": {"autoreboot": false}}` in cluster.json
  opts out. Doctor coverage arrives in the sibling card;
  `install_autoreboot.py` exports the expected-set constants for it.
---
kind: Verified
order: 1

- `hscc-bootstrap/tests/test_install_autoreboot.py`: 27 fabricated-node cases
  (stubbed ssh seam — drop-in content exact, hardlockup conditional both
  ways, read-back mismatch fails the node, idempotent no-op, drift-never-
  clobbered, opt-out skip, dry-run clean, sudo-refused/unreachable honest
  failures, fleet fan-out, main() rc contract).
- `hscc-bootstrap/tests/test_bootstrap_autoreboot_stage.py`: 7 cases exec'ing
  the bootstrap stage's embedded summariser extracted from `bootstrap.sh`
  (no false-green: any failed node or zero armed renders WARN; stderr noise
  cannot hide the JSON).
- `test_bootstrap_isolated.py` now runs with `--skip-node-recovery` so the
  suite never arms reboot-on-panic on the operator's live nodes.
