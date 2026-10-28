# node-recovery doctor check — t_94598b3a

Node-recovery batch part 3. `hscc doctor` gets ONE new per-node check covering
BOTH install layers the parent cards landed. Read-only; nothing in this diff
writes to a node, and no test or command in this card contacted a node.

## What landed

- `hscc-bootstrap/doctor_node_recovery.py` (new) — the check: two layer probes
  per node, verdicts `ok` / `fail` / `error` / `opted_out`, plus a standalone CLI.
- `hscc-bootstrap/doctor.py` — one check function + registration (added to the
  existing fleet-check block, gated by the same `fleet_checks` auto-detect the
  other fleet checks use).
- `hscc-bootstrap/tests/test_doctor_node_recovery.py` — 44 fabricated-transport
  tests.
- `changelog.d/t_94598b3a.md`, `hscc-bootstrap/README.md` (Files table row).

## The two layers, and where each fact comes from

Watchdog (parent t_ebbe38d1, `hscc-cluster/node_recovery.py`): the doctor calls
`probe_node()` and maps its outcome. It does NOT re-derive "armed" — the
parent's `is_armed_state()` + `ARMED_OUTCOMES` decide, so the 30s target
(`RUNTIME_WATCHDOG_SEC`), the sysfs-state leg and the `wdctl` leg are the
installer's own. Zero constants re-declared.

Sysctls (parent t_1fc3fe83, `hscc-bootstrap/install_autoreboot.py`): the doctor
sends the parent's `probe_command()` byte-for-byte and rebuilds the expected set
from `MANAGED_KEYS` + `HARDLOCKUP_KEY`/`HARDLOCKUP_VALUE` + `build_dropin()`, so
`kernel.panic=10`, `panic_on_oops=1`, `softlockup_panic=1`, `hung_task_panic=1`
and `hung_task_timeout_secs=600` (the parent's decision value) cannot drift into
a second copy here. `hardlockup_panic` is expected only where the node's kernel
exposes it — the parent's conditional rule, read off the probe's own `KNOB`
lines.

## Two design decisions worth the reviewer's eye

1. `KNOB` lines vs `VAL` lines. `parse_probe()` keeps only the values, which
   collapses "this kernel does not expose the knob" (parent skips it) with "the
   knob is there and the node would not say what it holds" (a read fault). The
   check reads the `KNOB` lines itself for the existence set, so the two stay
   distinct: the first is a visible note, the second is a fault. A probe that
   named NO knob at all is an ERROR (`/proc/sys unreadable`) — never "nothing to
   check, therefore nothing is wrong".

2. The parent's `probe_node` answers `error` in two different situations and the
   doctor must not blur them: a node it reached and read as *not armed* (a data
   fact -> the card's red line, `fail`), versus a probe that never returned state
   at all — unreachable, no `HSCC_RC`, `sudo -n` refused (an environment fault ->
   `error`, worded "UNVERIFIABLE ... NOT verified"). The distinguishing signal is
   the parent's own `state["transport_ok"]`, so it is not a new invention.

## Honest-signal rules now pinned by tests

- unreachable node / refused `sudo -n` / half-run probe / unreadable `/proc/sys`
  -> `error` naming the reason. Never `ok`, never a silent skip
  (cf. `hscc_daemon/health.py` `HermesRuntimeUnavailable`).
- a node that opted out of a layer -> `opted_out` (`--`), NOT green. Both
  subkeys are read through the parent's `node_recovery_config()`, so a
  non-boolean value (typo) keeps the layer ON *and* fails the check as a config
  fault — a silently-dead opt-out cannot look like a working one.
- a knob the kernel does not enforce -> printed as a note on the row, so an
  otherwise-green node still shows the gap.
- zero nodes resolved -> the check fails with "cannot verify node recovery on
  anything". An empty fleet is never an all-clear.
- fleet checks off (the pytest / non-CLI default) -> the row prints
  `skipped (fleet checks off)`, matching the lan-reachability convention, rather
  than disappearing.

## Isolation

The check sits inside the existing `fleet_checks` gate, so `run_doctor()` in a
pytest process never reaches a node. Two extra belts: `_default_ssh_factory()`
returns a refuse-only transport inside a pytest process (a suite that forgot to
inject `_ssh_factory` gets an ERROR row, not a live ssh), and `_topology_hosts()`
returns `[]` under pytest so `detect_cluster()` cannot shell out to the live
sparkrun CLI. Tests inject a scripted node; no test opens a socket or resolves a
host. Addresses in fixtures/reports are the documented `10.0.0.x` placeholders.

## Sample render (from `test_sample_render_green_and_red`, fabricated nodes)

    1/2 node(s) fully protected (watchdog armed + panic sysctls in sync); 1 NOT protected
          [ok] 10.0.0.1 ok: 10.0.0.1: watchdog RuntimeWatchdogUSec=30s, device state=armed, device timeout=30s, wdctl Timeout=30seconds (target 30s); drop-in present
          [ok] 10.0.0.1 10.0.0.1: 6 panic knob(s) verified live
          [XX] 10.0.0.2 fail: 10.0.0.2: watchdog error — RuntimeWatchdogUSec='0', expected 30s (target RuntimeWatchdogSec=30s)
          [XX] 10.0.0.2 10.0.0.2/kernel.panic: live '0', expected '10'

## Deferrals

Neither parent deferred a layer this check needs: both landed the install plus a
read-back verification, and the watchdog card explicitly shipped `probe_node()` /
`--probe-only` as the doctor's entry point. Two things remain the orchestrator's,
not this card's, and this check reports them honestly until they happen:

- Fleet APPLY (real arming / real sysctl install) has not run on any node — both
  parents measured every live node unarmed (`RuntimeWatchdogUSec=0`, sysfs
  `inactive`) and unconfigured. On today's fleet this check will print RED for
  every node. That is the correct reading, not a false alarm: the layers are
  implemented but not yet enforced.
- Deliberate crash/reboot testing was left to the operator by both parents.

## Live read-only evidence (no node written, no escalation)

`python3 hscc-bootstrap/doctor_node_recovery.py` on the operator's machine
(sparkrun's own remote execution, the two read-only probes only; addresses
redacted to the documented placeholder here and everywhere in this repo):

    XX node recovery (watchdog + panic-to-reboot): 0/3 node(s) fully protected (watchdog armed + panic sysctls in sync); 3 NOT protected
          [XX] 10.0.0.x fail: 10.0.0.x: watchdog NOT ARMED — RuntimeWatchdogUSec='0', expected 30s; /etc/systemd/system.conf.d/90-hscc-watchdog.conf absent (target RuntimeWatchdogSec=30s)
          [XX] 10.0.0.x 10.0.0.x/kernel.hardlockup_panic: live '0', expected '1'
          [XX] 10.0.0.x 10.0.0.x/kernel.hung_task_panic: live '0', expected '1'
          [XX] 10.0.0.x 10.0.0.x/kernel.hung_task_timeout_secs: live '120', expected '600'
          [XX] 10.0.0.x 10.0.0.x/kernel.panic: live '0', expected '10'
          [XX] 10.0.0.x 10.0.0.x/kernel.panic_on_oops: live '0', expected '1'
          [XX] 10.0.0.x 10.0.0.x/kernel.softlockup_panic: live '0', expected '1'
          [XX] 10.0.0.x 10.0.0.x: /etc/sysctl.d/90-hscc-autoreboot.conf absent — panic-to-reboot is NOT configured on this node
          ... (same 8 lines per node, 3 nodes) exit=1

Independent of the parents' own probes, this confirms the fleet is currently
unenforced on both layers on every node: `RuntimeWatchdogUSec=0`, the watchdog
drop-in absent, every panic knob at its distro default, `hung_task_timeout_secs`
at the 120s default (not the card's pinned 600), and the sysctl drop-in absent.
It also confirms the conditional-knob path end to end on real hardware:
`kernel.hardlockup_panic` EXISTS on these kernels (so it is expected and reported
as drift at live '0'), which is exactly the branch the parent's conditional logic
exists for.

Two facts worth the orchestrator's attention:

- **The check covers the hosts `sparkrun cluster list` reports — currently 3, not
  the 4 in the card text.** The card names four nodes (.244/.246/.247/.248);
  sparkrun's default cluster resolves three hosts, and the watchdog parent's own
  measured evidence agrees ("all live nodes", "protected 3/3"). The fourth is
  therefore outside every sparkrun-scoped operation in this batch — bootstrap's
  install stages included, since both parents iterate the same host list. If that
  node is meant to be covered, the fix is the cluster config, not this check.
- This run is read-only and unprivileged for the sysctl leg (`cat /proc/sys/...`);
  the watchdog leg is the parent's `state` mode, which likewise writes nothing.
  No `sudo` was attempted by either leg.

## Not in this card

- The snap-notifier check — sibling t_61850b24 owns it; nothing here duplicates it.
- Fleet apply / merge / release — orchestrator.

## Test + guard results (as measured)

- `hscc-bootstrap/tests/test_doctor_node_recovery.py`: **45 passed** (fabricated
  transport; no socket, no host resolution, no `/etc`).
- `scripts/run_tests.sh`: **10 of 11 legs green** — hscc-bootstrap 571,
  hscc-commands, hscc-cluster, hscc-project, hscc_daemon, sparkrun-hermes,
  hscc-api 938+1s, memori_byodb 11, scripts 262+1s, .github/scripts 129. The
  sole red is `hscc-roles::test_every_hscc_owned_worker_profile_has_role_spec`,
  which fails because a live operator profile has no role spec; it reproduces
  identically on pristine `origin/main` and no file in this diff touches
  `hscc-roles` — environmental, not introduced here.
- `python3 scripts/address_guard.py --tracked` and `--staged`: rc=0. The full
  diff against `origin/main` contains no IPv4 literal other than the
  `10.0.0.x` placeholders.
- Note on suite order: an earlier bootstrap-leg run reported 4 failures in
  `test_bootstrap_node_recovery_stage` / `test_bootstrap_isolated` (dry-run
  bootstrap tests that exec `bootstrap.sh` against a fabricated cluster). Those
  tests pass in isolation (16 passed) and the leg is green on re-run (571
  passed). I did NOT reproduce the failure, so I cannot attribute it: another
  card's pytest leg (`rb90`) was running concurrently on this host, and those
  tests exec a real bash installer against temp homes, so host contention is a
  plausible mechanism. Nothing in this diff touches `bootstrap.sh`
  (`git diff --name-only origin/main...HEAD` lists 6 files, none of them
  bootstrap.sh), and none of my new tests exec it.
