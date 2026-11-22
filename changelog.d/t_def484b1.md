kind: Fixed
task: t_def484b1

- **A reboot of a node you parked no longer claims it relaunched units it never
  launched.** `hscc cluster up` skips a parked node's units and, when the whole
  plan it was handed is parked, reports `result: "all-paused"` with `success:
  True` — "there was nothing to do", not "it failed" (t_3b909c61 / #576). The
  post-boot relaunch reduced that one return to a single boolean, so the narrow
  case where the operator ran `hscc cluster up` and **parked the node
  afterwards** (the `expected_up` marker still lists the parked unit, because
  only fleet-up and the autodown wake rewrite it) made that node's next reboot
  report and desktop-notify `relaunched [unit] via hscc cluster up` for units
  nothing had started. No bad start came of it and no page loop — baselines
  advance on success, so the next pass has no candidate — but an operator
  reading "relaunched w1" about a machine they had parked could not tell the
  two states apart. The pass now reads that return as a THIRD outcome, ahead of
  the success/failure split: `result="skipped-paused"`, the parked unit ids plus
  `hscc node resume <host> --apply` in the message, `actions` empty (the pass
  claims no launch), and ONE **low**-priority notice in place of a
  high-priority page. Baselines stay ADVANCED: there is no retry to re-decide,
  because the node is parked by the operator's own record, and restoring them
  would re-offer the same parked unit to every later pass. The branch is gated
  on `ok`, so a return that carried the label *and* `success: False` — a
  contract violation, not a skip — still takes `launch-failed`. The launched
  and launch-failed arms are untouched.
- **`skipped-paused` is an ok stream, so a pause cannot turn `hscc verify` red
  through the back door.** `post_boot._STREAM_OK_RESULTS` is a closed set: a
  result string it does not list writes `ok=False`, which is what makes
  `hscc verify` red and lets the trigger engine fire. Shipping the new outcome
  without adding it there would have page-looped the operator about their own
  pause — the exact failure mode the design note rejects for "a skip reported
  as a failed unit". Pinned as a row in the stream-outcome table test.
---
kind: Verified
task: t_def484b1

- **8 new cases in `hscc_daemon/tests/test_post_boot.py`, 71 passed at
  `d66bc2e0`** (the file collected 63 before). Every case injects `up_fn` — no
  subprocess, no real cluster-up — on the file's `192.0.2.x` placeholders, and
  the launched / launch-failed arms are pinned in the same class so the shared
  split cannot be mutated quietly. The overlay is never touched: the daemon
  conftest already seals `node_pauses.HSCC_DIR` *and* `os.path.expanduser`, so
  the guard-assert reads the real home out of the passwd entry (the only witness
  a monkeypatch cannot move) before pinning that an overlay read explodes.
- **7/7 mutation probes caught, control green at 71, restore byte-verified.**
  M1 the new branch never fires (the pre-fix shape) → 5 of the new tests red.
  M2 the `ok and` gate dropped, so the label alone decides → only
  `test_all_paused_label_never_masks_a_real_failure`. M3 the outcome flipped back
  to `launched` → 4. M4 the notice promoted to `high` → only the low-priority
  test. M5 `_restore_baselines` restored on the new path → only the
  advance-and-never-re-decide test. M6 `skipped-paused` dropped from
  `_STREAM_OK_RESULTS` → only the stream-outcome table test (which is the
  `hscc verify` red that a closed ok-set would otherwise have introduced). M7 a
  control on the pinned arms: the **launched** notice demoted to `low` → only
  `test_plain_success_still_reports_launched_with_a_high_notice`, so the pinned
  arms really do bite.
