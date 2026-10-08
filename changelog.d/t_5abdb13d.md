kind: Added
task: t_5abdb13d

- **`hscc check --repo <path>` — the commit-time address guard's TRUE posture
  per checkout.** `core.hooksPath` lives in the COMMON git config and is
  relative, so the config value alone lies per checkout: measured during the
  t_ec2c2f95 review, the primary plus every pre-merge worktree reported
  "armed" while resolving no hook at all. `hscc check --repo <path>` (default:
  cwd's top-level) now calls `posture()` from the shipped
  `hscc-bootstrap/install_hooks.py` — same single-implementation rule that
  governs the detector itself; `hscc_daemon/guard_posture.py` locates and loads
  that file (repo layout and deployed-plugin layout are siblings, so the
  lookup works in both) and passes its dict through untouched. `--json` prints
  the posture verbatim (plain print, never Rich). Exit code is 0 only for
  `armed`; `unarmed`, `armed-but-absent` (config advertises protection while
  THIS checkout resolves no runnable hook — the fail-open line the operator
  asked to see) and `not-a-repo` all exit non-zero for scripts/cron. A missing
  or unloadable `install_hooks.py` is reported as `guard posture UNVERIFIED`
  and exits non-zero — fail closed; a status line that cannot see the guard
  must not advertise that it can.
---
kind: Fixed
task: t_5abdb13d

- **`posture()` mislabeled a fresh clone as the fail-open state.** Found while
  wiring `check --repo`: `unarmed` was classified `not armed and not runnable`,
  so a checkout carrying both halves but with `core.hooksPath` unset — every
  fresh clone before bootstrap, advertising nothing — landed in
  `armed-but-absent`, the name reserved for "the config advertises protection
  and this checkout delivers none". Both states exit non-zero, so no existing
  test caught it, but `armed-but-absent` is exactly the alarm an operator keys
  cron/scripts on: false alarms there erode the real one. The config is the
  only thing that arms the guard, so the config alone now decides armedness
  (`unarmed` / `armed` / `armed-but-absent` / `not-a-repo` are disjoint), and
  the detail line names the missing key (`core.hooksPath unset`). Pinned by
  `test_posture_four_states_are_disjoint_and_config_decides_armedness`.
