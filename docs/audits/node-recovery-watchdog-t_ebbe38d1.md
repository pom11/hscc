# node-recovery part 1: arm the SBSA hardware watchdog cluster-wide (t_ebbe38d1)

Status: IN PROGRESS (this file is written incrementally; findings appended as landed).

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

<!-- filled below as they are implemented -->

## Files changed

<!-- appended as they land -->

## Per-node dry-run diff sample

PLACEHOLDER HOSTS ONLY (10.0.0.x convention). Filled from a real `--dry-run`.

## Tests

<!-- appended with real counts -->
