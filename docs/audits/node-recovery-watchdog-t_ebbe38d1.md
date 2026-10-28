# node-recovery part 1: arm the SBSA hardware watchdog cluster-wide (t_ebbe38d1)

Status: implementation complete on branch `wt/t_ebbe38d1`; fleet apply is the
orchestrator's, not this card's.

## Why

`.244` froze solid on 2026-10-27: the journal stops at 17:31:53 with no panic,
oops or softlockup line, the node answered nothing on ping/ssh/IB for ~2h, and
the operator power-cycled it by hand at 19:34. DGX Spark nodes have no BMC, so
nothing outside the SoC can reset it. The GB10 does expose a hardware watchdog
(`/dev/watchdog0`, "SBSA Generic Watchdog") that is currently unused: if PID1
owns and pets it, a hard hang resets the SoC on timeout.

This card installs that arm-up through systemd — `RuntimeWatchdogSec=30s` +
`RebootWatchdogSec=2min` in a drop-in — over the existing bootstrap ssh path,
idempotently, with an opt-out. The doctor coverage for it is the sibling card
(t_94598b3a); the sysctl layer is t_1fc3fe83.

## Design decisions (settled here; siblings must not re-decide them)

1. **systemd owns the device, not a userspace daemon.** `RuntimeWatchdogSec` in
   `[Manager]` + `daemon-reexec`. PID1 opens and pets `/dev/watchdog0` and
   closes it on clean shutdown, so a reboot does not trigger a spurious reset.
   The `watchdog(8)` package is explicitly NOT used: two petters on one core is
   how a watchdog becomes a lie.
2. **`30s` / `2min` are the only constants**, defined once in
   `hscc-cluster/node_recovery.py` (`RUNTIME_WATCHDOG_SEC`,
   `REBOOT_WATCHDOG_SEC`, `DROPIN_PATH`). The doctor card imports them; it must
   not re-declare them.
3. **"Armed" is defined once** — `is_armed_state()` — and is derived, never
   asserted by the code path that produced it. The installer and the doctor
   cannot drift into disagreeing about what protected means.
4. **Transport is sparkrun's, root is `sudo -n`.** The module reuses the
   bootstrap's existing remote-exec bridge (`sparkrun_bridge`); it never
   hand-rolls `ssh`. Escalation is `sudo -n` only, and a refusal fails that one
   node with a message naming the exact NOPASSWD commands needed. It can never
   hang on a password prompt.
5. **Never steal a device.** A `/dev/watchdog0` held by a foreign consumer, or a
   watchdog-less node, is REFUSED before any write — an armed timeout nobody can
   pet is worse than an unarmed one.
6. **Never clobber operator intent.** The drop-in is HSCC-managed only while its
   sha256 matches ours. Any other content at that path is reported as drift and
   left byte-for-byte alone, including by `--disarm`.
7. **Opt-out is `node_recovery.watchdog`**, default ON when absent, read per
   subkey so the sysctl sibling's `autoreboot` in the same block can never
   disarm the watchdog (and vice versa). Unknown keys stay ignorable.
8. **The install stage is non-fatal but never silent.** A node that is down,
   refuses sudo, or lacks a driver WARNs and continues the install; a protected
   fleet is the point, so the doctor keeps printing red until it is fixed.

## Files changed

| File | What |
| --- | --- |
| `hscc-cluster/node_recovery.py` | New. The installer/probe: generates one self-contained bash script per node (modes `state`/`install`/`uninstall`), ships it over the sparkrun remote-exec bridge, verifies from the node's own answers. CLI: default (arm) plus `--dry-run`, `--disarm`, `--probe-only`, `--hosts`, `--config`, `--timeout`, `--json`. |
| `hscc-cluster/tests/test_node_recovery.py` | New. 76 cases: unit tests over a scripted fake transport + an end-to-end suite that runs the **real generated script** against a bash fake node (systemctl emulator, sha256/sudo shims, sysfs stand-ins). |
| `hscc-bootstrap/bootstrap.sh` | New stage `Install: node hardware watchdog (self-recovery)` + `--skip-node-recovery`. Runs the deployed `node_recovery.py` with hosts as **argv** (never shell-eval'd). |
| `hscc-bootstrap/tests/test_bootstrap_node_recovery_stage.py` | New. 11 cases pinning the stage: runs on every node, skips on flag/env, dry-run writes nothing, missing module warns, non-zero exit does not abort the install. |
| `hscc-bootstrap/tests/test_bootstrap_isolated.py` | The sandboxed install now passes `--skip-node-recovery` — an isolated pytest run must never escalate to root over ssh on the live fleet. |
| `changelog.d/t_ebbe38d1.md` | `Added` + `Fixed` fragments. |
| `docs/audits/node-recovery-watchdog-t_ebbe38d1.md` | This report. |

Shared surface handed to the doctor card (t_94598b3a): `probe_node()`,
`is_armed_state()`, `device_is_active()`, `ARMED_OUTCOMES`, `DROPIN_PATH`,
`RUNTIME_WATCHDOG_SEC`, `REBOOT_WATCHDOG_SEC`, `NodePaths`.

## Measured node reality (read-only, live fleet — hosts placeholdered)

Probed through the module's own read-only path before writing anything. Every
node in the live sparkrun cluster (`10.0.0.1`, `10.0.0.2`, `10.0.0.3`):

```
HSCC_WATCHDOG_CLASS: present          HSCC_WATCHDOG_DEVICES: watchdog0
HSCC_WATCHDOG0_IDENTITY: SBSA Generic Watchdog
HSCC_RUNTIME_WATCHDOG_USEC: 0         # unarmed today
HSCC_WATCHDOG0_STATE: inactive        # nobody owns the core
HSCC_WATCHDOG0_TIMEOUT: 10            HSCC_WATCHDOG0_MIN_TIMEOUT: 1
HSCC_WATCHDOG0_MAX_TIMEOUT: 0         HSCC_WDCTL: present
HSCC_FUSER: (no holder)               # SBSA driver is free
```

Two findings that changed the implementation, both from this probe:

- **The sysfs attribute is `identity`, not `name`.** A probe reading
  `/sys/class/watchdog/watchdog0/name` returns nothing on this kernel, so it
  would have reported an empty watchdog identity on every node. The probe reads
  `identity` first and falls back to `name`.
- **`options=0x81a0` includes `WDIOF_CARDRESET`** (`0x0080`) — the bit systemd
  requires before it will arm a device at all. Worth recording: a driver without
  it accepts the config, `RuntimeWatchdogUSec` reads back `30s`, and nothing
  ever pets the core. That is the false green handled below.

Also measured: `.246` (the training box named in the card) is **not in the live
sparkrun cluster**, so the fleet-wide apply covers 3 nodes today, not 4. The
code takes hosts from sparkrun, so it self-corrects when `.246` is re-added —
flagged here rather than hardcoded.

## Per-node dry-run diff sample

`python3 hscc-cluster/node_recovery.py --dry-run` (writes nothing), real
output with hosts rewritten to the `10.0.0.x` convention:

```
node-recovery watchdog — DRY RUN (writes nothing)
  target: /etc/systemd/system.conf.d/90-hscc-watchdog.conf RuntimeWatchdogSec=30s RebootWatchdogSec=2min
  [ok] 10.0.0.1 applied: would create /etc/systemd/system.conf.d/90-hscc-watchdog.conf with:
  │ # Managed by HSCC (node-recovery watchdog; hscc bootstrap).
  │ # PID1 (systemd) opens and pets /dev/watchdog0 and closes it on clean
  │ # shutdown, so a hard hang resets the SoC instead of wedging until someone
  │ # power-cycles the box. To stop HSCC managing this, set
  │ #   node_recovery: {watchdog: false}
  │ # in ~/.hscc/cluster.json and re-run bootstrap, then remove this file.
  │ [Manager]
  │ RuntimeWatchdogSec=30s
  │ RebootWatchdogSec=2min
  │ then: sudo -n systemctl daemon-reexec
  │ current RuntimeWatchdogUSec=0; watchdog devices=[watchdog0]
  ... identical for 10.0.0.2 and 10.0.0.3 ...
  protected: 3/3; opted out: 0
```

Per the card, `systemctl show -p RuntimeWatchdogUSec` is captured per node (all
read `0` pre-apply, above) and `wdctl` presence/output is captured per node.
The post-apply `RuntimeWatchdogUSec=30s` + `wdctl` Active reading comes from the
orchestrator's fleet apply; this card deliberately did not arm the live fleet.

## What "armed" verification means here

Verification is three-legged, all read back **from the node** after the write:

1. `systemctl show -p RuntimeWatchdogUSec` == `30s` (systemd took the config),
2. sysfs `state` is not `inactive` (PID1 actually opened the core),
3. `wdctl` timeout reported (advisory — `wdctl` is absent on some images).

A node that passes (1) but not (2) is reported **ERROR**, not green: the config
is in place and the node is still unprotected. Both the arm path and the
read-only probe path (`--probe-only`, which is what the doctor check will call)
apply this, and the result carries the post-write observation so the discrepancy
is visible in the payload. Tests pin both paths.

## Tests

```
hscc-cluster/tests/test_node_recovery.py                      76 passed
hscc-bootstrap/tests/test_bootstrap_node_recovery_stage.py    11 passed
```

`test_node_recovery.py` splits in two deliberately. The scripted-transport
cases cover the decision table (exact drop-in bytes, idempotent no-op,
operator-edited → drift not clobbered, opt-out → skip, dry-run writes nothing,
foreign holder → refused, no watchdog core → refused, missing `/dev/watchdog0`
→ refused, `sudo -n` refusal → clear per-node failure). The `FakeNode` cases
then run the **generated bash script itself** under bash against a sandbox node,
because a bug in the shell layer is invisible to a Python-level fake: the
emulated `systemctl` only flips `RuntimeWatchdogUSec` **and** sysfs `state` on
`daemon-reexec`, so an install that writes bytes without re-execing cannot pass
as armed. No test touches the network or the live fleet.

Full-suite and address-guard results are in the completion metadata / card
comment.

## Not in this card

- `hscc doctor` check — sibling t_94598b3a (this card exposes the probeable
  state it needs, listed above).
- `kernel.panic`/`panic_on_oops`/softlockup sysctls — sibling t_1fc3fe83.
  `--skip-node-recovery` is shared with it deliberately; both stages honour it
  and the diff on the shared lines was kept insertion-only so the two merge
  cleanly.
- Fleet apply (real arming) + merge/release — orchestrator.
- Deliberate crash/reboot testing — operator's call, not run here.
