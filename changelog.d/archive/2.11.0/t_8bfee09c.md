kind: Fixed
task: t_8bfee09c

- **`test_live_hscc_autodown_unchanged_by_suite` raced the live daemon's
  30s autodown heartbeat.** The guard byte-hashed the real
  `~/.hscc/autodown.json` before/after a sandboxed re-run of its module,
  but `run_autodown_loop` (interval=30s) rewrites that file every tick via
  `record_activity()`/`save_config()` — advancing `last_activity_iso`/
  `wake_source` — so any tick landing inside the read window failed the
  byte-identical assert false-positive (observed in the 2026-10-18
  merged-tree sweep; measured: the file's md5 changes within 33s on a live
  host). Replaced with the design the daemon-side `test_no_live_hscc_leak`
  already documents for exactly this reason: a planted-sandbox check (the
  module re-runs against a fake HOME holding a static seeded autodown.json —
  deterministic, no live file involved) plus a semantic live check on only
  the operator-decision keys (`enabled`, `idle_minutes`), which the tick can
  never rewrite. The nested run also gets `HSCC_DIR` stripped (it outranks
  HOME in `ensure_autodown`'s path resolution — an exported value would
  punch through the fake HOME), and `test_main_prints_json` delvars it for
  the same reason.
---
kind: Verified
order: 1

- Red-on-old replay: the original byte-identical assert FAILED with a tick
  forced into its window (flake reproduced deterministically); the new
  guard PASSED under the same forced tick (31s window).
- Mutation probes: a suite writing the planted sandbox config trips layer 1;
  flipping the live operator keys trips layer 2. Both restored after.
- `hscc-bootstrap` suite green with the guard in place.
