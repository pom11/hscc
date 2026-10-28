# node-recovery part 5: mask snap firmware-notifier user service (t_61850b24)

Status: implementation in progress on branch `wt/t_61850b24`. Fleet apply is the
orchestrator's, not this card's. **No node was masked by this card** — every
probe recorded here is read-only.

## Why

`snap firmware-updater`'s **user** service crash-loops on the Sparks. Measured on
`.244` (read-only, via the sparkrun bridge, 2026-10-28):

```
snap.firmware-updater.firmware-notifier.service: Main process exited, code=exited, status=1/FAILURE
snap.firmware-updater.firmware-notifier.service: Failed with result 'exit-code'.
snap.firmware-updater.firmware-notifier.service: Scheduled restart job, restart counter is at 4178.
```

That restart counter is **5184** on `.244`, **6994** on `.247` and **6998** on
`.248` — every cluster node — and the journal holds **5967 `Main process exited`
lines in the last 24 h** on `.244` alone. `Restart=on-failure` +
`RestartUSec=100ms` turns one broken desktop notifier into a ~5-second restart
loop inside the spark user's `systemd --user` session: pure log churn and
process churn, forever.

The failure text the notifier itself prints is
`error: timeout waiting for snap system profiles to get updated`.

**Causation is NOT claimed.** Whether this loop contributed to the `.244` hang of
2026-10-27 is unproven, and nothing in this card asserts it. The card's honest
scope: the loop is real, it is measurable, it is noise, and masking one snap
user unit stops it.

## Design decisions (settled here; siblings must not re-decide them)

1. **MASK, not disable.** (Full justification paragraph — reproduced verbatim in
   the PR body; see "mask-vs-disable" below.)
2. **The firmware-updater SNAP is untouched.** No `snap remove`, no `snap
   refresh --hold`, nothing at the snap layer. `fwupd` updates remain wanted and
   the operator runs them by hand. The blast radius is exactly one systemd user
   unit.
3. **Transport is sparkrun's.** Reuses `hscc-cluster/sparkrun_bridge.py`'s
   `remote_cmd` — never a hand-rolled `ssh`.
4. **Root is never needed.** The mask lives in the *user's own*
   `~/.config/systemd/user/`, so this is an unprivileged operation for the ssh
   user (spark). No `sudo -n`, no escalation, no NOPASSWD requirement.
5. **Opt-out is `node_recovery.firmware_notifier_mask`**, default ON when
   absent, read per subkey so a sibling's `watchdog`/`autoreboot` subkey can
   never disarm it (and vice versa).

## mask-vs-disable: why MASK is the lever that survives a snap refresh

`systemctl --user disable` only removes the symlinks in the user's
`*.wants/` / `*.target.wants/` directories — it changes *who pulls the unit in*,
not whether the unit can run. The notifier is a snap-generated unit: snapd
regenerates `/etc/xdg/systemd/user/snap.firmware-updater.firmware-notifier.service`
(its header literally says `# Auto-generated, DO NOT EDIT`) and re-runs the user
generator on **every** snap refresh, in the snapd revision directory
(`WorkingDirectory=/var/snap/firmware-updater/168` — that `168` is a revision
number that changes on refresh). A `disable` is a symlink state that the
generator and snapd's own unit-file reconciliation are free to undo, so the
crash-loop silently returns the first time snapd refreshes the package. That is
the failure mode the operator asked to avoid: a fix that reads green today and
silently evaporates next week.

`mask` is a different mechanism: it puts a symlink at
`~/.config/systemd/user/snap.firmware-updater.firmware-notifier.service -> /dev/null`
in the user's own unit-search path, and systemd refuses to *load* any unit whose
name resolves to `/dev/null` — regardless of who asks, whether the ask comes
from the timer, from `snap run`, from the generator's `[Install]` section, or
from a human typing `systemctl --user start`. snapd regenerates the *fragment*
under `/etc/xdg/systemd/user/`, but the mask lives at a different path and masks
the unit *name*, so a refresh overwrites the fragment and leaves the mask intact.
The mask is also independent of the unit's `static` state: the measured
`UnitFileState=static` means `disable` would report success while changing
nothing at all, because a static unit has no `[Install]` section to unlink —
on this fleet `disable` is measurably a no-op.

Consequences we accept and record: a masked unit cannot be started by anything,
including a future snap that would have fixed it, and unmasking is the deliberate
undo (`hscc-cluster/notifier_mask.py --unmask`). Because it persists across
refreshes and reboots, the state is also *loud*: `hscc doctor` has its own check
that reads the mask back from every node and goes red if it is gone, so a mask
that silently disappeared cannot masquerade as protection.

## Measured node facts (read-only probe via sparkrun_bridge; hosts as last octet)

**Scope = the sparkrun CLUSTER nodes only** (operator ruling, card comment
2026-10-28 12:54). `sparkrun cluster list` resolves **3** nodes — `.244`, `.247`,
`.248` — and that resolved list *is* the complete denominator for both the mask
and the doctor check. A fourth Spark exists on the LAN but is a training node
outside the cluster, is configured by hand, and HSCC must never touch it; it
appears nowhere in this card's code, tests, or wording, and nothing here
maintains a host inventory of its own.

Observed 2026-10-28 on all three cluster nodes unless a row says otherwise:

| probe | result |
| --- | --- |
| ssh user / uid | `spark` / `1000` on every cluster node |
| `XDG_RUNTIME_DIR` over ssh | **already exported** (`/run/user/1000`) — the classic no-bus pitfall does NOT bite on this fleet's ssh path |
| `DBUS_SESSION_BUS_ADDRESS` | already `unix:path=/run/user/1000/bus` |
| `$XDG_RUNTIME_DIR/bus` | `BUS_SOCKET_PRESENT` on every cluster node |
| `systemctl --user is-enabled <notifier>` | `static` (rc=0) on all 3 |
| `systemctl --user is-enabled <timer>` | `enabled` on all 3 |
| `systemctl --user is-active <notifier>` | `active` on all 3 |
| `systemctl --user is-masked` | **not a verb on this systemd** — "Unknown command verb 'is-masked'" (rc=1). Read-back must use `is-enabled` -> `masked` |
| `Restart` / `RestartUSec` | `on-failure` / `100ms` on all 3 |
| `NRestarts` | 5184 (`.244`) / 6994 (`.247`) / 6998 (`.248`) |
| `Main process exited` in last 24 h | 5967 (`.244`) / 7033 (`.247`) / 7036 (`.248`) |
| `Main process exited` since boot | 84110 (`.244`) / 117273 (`.247`) / 117802 (`.248`) |
| boot time | `.244` 2026-10-27 19:34, `.247` 2026-10-14 10:48, `.248` 2026-10-08 14:13 |
| `FragmentPath` | `/etc/xdg/systemd/user/snap.firmware-updater.firmware-notifier.service` on all 3 |
| `[Install]` section in the notifier fragment | **absent** (`grep -c '\[Install\]'` = 0) → the unit is `static`: `disable` cannot affect it |
| driving unit | `snap.firmware-updater.firmware-notifier.timer`, `OnCalendar=*-*-* 00:00/3` (8 firings/day), `[Install] WantedBy=timers.target` |
| mask symlink in `~/.config/systemd/user/` | **absent on all 3** (service and timer) — nothing is masked yet, fleet-wide |
| system-level snap units | **0 unit files** — the notifier is user-session only |
| `ls /var/lib/systemd/linger/` | **empty on all 3** — `spark` has no linger. See "Linger and the bus" below |
| `fwupd` | `active` on every cluster node → firmware path stays wanted, snap untouched |
| no-bus failure text (measured with `env -i`) | `Failed to connect to bus: No medium found` (rc=1) — the string the doctor must treat as an ENV FAULT, not as "not masked" |
| systemd version | `systemd 255 (255.4-1ubuntu8.16)` on all 3 |

Two numbers move every time they are read — `NRestarts` and the 24 h exit count
are monotonic restart counters, not stable facts — so they are stated as
observed at 2026-10-28, and the doctor never thresholds on them.

**Linger and the bus.** `Linger` is off on all three nodes, yet
`XDG_RUNTIME_DIR` and the bus socket are present over the sparkrun ssh path
(measured above), so the user manager is reachable today. The module still
exports both variables with `${VAR:-default}` rather than trusting the
environment, because an empty linger directory means there is no guarantee the
user manager is up on a transport that does not go through the login session —
and a mask that silently talked to no bus would report "not masked" forever.

The invocation the module therefore uses, and why:

```
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
systemctl --user ...
```

Setting both explicitly, with `${VAR:-default}` rather than an unconditional
export, is belt-and-braces: it is a no-op on the measured fleet (both are
already set), and it keeps the command correct on a node or a transport where
they are not — e.g. a future ssh config with `ClearAllForwardings`/no PAM
session, which is exactly the case where the naive form dies with `Failed to
connect to bus`. `sudo -u` is deliberately NOT used: the ssh user already *is*
the desktop user, and `sudo -u spark` would lose the bus env again.

## Files changed

| File | What |
| --- | --- |
| `hscc-cluster/notifier_mask.py` | New. Install/probe module + CLI (`--dry-run`, `--unmask`, `--probe-only`, `--hosts`, `--config`, `--json`). |
| `hscc-cluster/tests/test_notifier_mask.py` | New. Fabricated-transport tests. |
| `hscc-bootstrap/notifier_doctor.py` | New. The doctor check (own check, per card). |
| `hscc-bootstrap/tests/test_notifier_doctor.py` | New. |
| `hscc-bootstrap/bootstrap.sh` | New stage `Install: snap firmware-notifier mask` (whole-block insertion; `--skip-node-recovery` honoured). |
| `hscc-bootstrap/tests/test_bootstrap_notifier_stage.py` | New. Stage wiring pins. |
| `changelog.d/t_61850b24.md` | New. |
| `docs/audits/node-recovery-notifier-mask-t_61850b24.md` | This report. |

## Findings so far

- (in progress)
