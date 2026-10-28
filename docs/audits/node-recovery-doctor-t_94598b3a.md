# node-recovery doctor check — t_94598b3a

Status: IN PROGRESS (scaffold committed first; findings appended as they land).

## Scope

One new `hscc doctor` check, per cluster node, covering BOTH node-recovery layers:

1. Hardware watchdog armed (systemd `RuntimeWatchdogUSec` + sysfs state + `wdctl`).
2. Managed sysctls in sync with `/etc/sysctl.d/90-hscc-autoreboot.conf`.

Environment faults (node unreachable, unreadable sysctl, `sudo -n` refused) render as
an honest check ERROR on that node — never `ok`, never a silent skip.

## Parents (read first)

- t_ebbe3fe83 sysctl card -> PR #88 (MERGED) `hscc-bootstrap/install_autoreboot.py`
- t_ebbe38d1 watchdog card -> PR #89 (OPEN, merged into this branch) `hscc-cluster/node_recovery.py`

Findings: TBD
