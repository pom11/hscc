kind: Added
task: gpu-clock-cap

- **Every cluster node's GPU clock is now capped at 2200 MHz, persistently.**
  New bootstrap stage `Install: GPU clock cap` (`hscc-bootstrap/install_gpu_clock_cap.py`)
  installs a systemd oneshot unit that runs `nvidia-smi -lgc 0,<cap>` at boot
  (undo: `nvidia-smi -rgc`, or `--uninstall`). A public two-Spark sweep put the
  knee at 2200 MHz: decode 100 %, uncached prefill 95 %, overall 99.6 % of
  uncapped, for -32 % GPU power and ~-5 C mean GPU temperature. Applies live
  with no model restart. Read-back verified per node (unit present with the
  wanted value, enabled, active, SM clock at or under the cap); `sudo -n` only,
  sparkrun transport only. Value or opt-out: `node_recovery.gpu_clock_cap_mhz`
  (integer, or `false`) in `~/.hscc/cluster.json`. New non-fatal doctor check
  `GPU clock cap (node-recovery)`.

kind: Fixed
task: gpu-clock-cap

- **The snap firmware-notifier mask stage now reaches every node.** bootstrap
  passes `--hosts` as one comma-joined argument, and `install_notifier_mask.py`
  took it as a single host name, so the stage never masked the real nodes. The
  CLI now splits on commas (space-separated still works).
