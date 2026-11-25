kind: Docs
task: t_fdc2bff0

* **The merge-queue gate's per-leg cost is now measured, and the ~3x tax is
  located.** `scripts/run_tests.sh` runs twelve legs serially and prints no
  per-leg duration, so the queue's ~75 min local gate has never been readable
  as anything but a whole. Measured on the control-plane Mac Studio (14 cores),
  one leg per runner invocation on `origin/main`: `hscc_daemon` 315 s,
  `hscc-bootstrap` 232 s, `hscc-cluster` 53 s, `hscc-roles` 11 s, and five legs
  at 7 s or less — 621 s for the seven legs measured at ordinary scheduler
  priority (`ps -o pri` 20). The same suite inside the live gate runs at pri 4:
  the `scripts` leg alone was observed at 1362 s, and a two-leg narrowed gate
  past 1871 s. The gap is the `com.hermes.hscc_daemon` launchd job's
  `ProcessType => Background` role, and the elevation the gate asks for today
  does not reach it: `taskpolicy -c utility` leaves a `-b`-throttled child at
  pri 4, `taskpolicy` accepts no `default` clamp on this macOS, and `taskpolicy
  -B -p <pid>` — the documented way out of `PRIO_DARWIN_BG` — exits 0 without
  moving the priority. Recorded in `docs/audits/` with every A/B, so the next
  gate-speed card starts from the measurement instead of re-deriving it.
---
kind: Verified
task: t_fdc2bff0

* The four priority claims above are each a real chain of `exec`s read back with
  `ps -o pri`, not a reading of the plist: plain 20, `taskpolicy -b` 4, `-b`
  followed by the gate's own `-c utility` 4, and `-b` followed by a self-`-B`
  4 with exit status 0. The live-gate numbers are sampled from outside the gate
  (its stdout goes to a pipe and is only tailed), by polling which leg's pytest
  is alive and reading the queue's in-flight stream.
