kind: Changed
task: t_5b560adf

- **`hscc-bootstrap`'s real-Hermes-tree locator is a seam, not a `~`-derived
  gate.** Thirty-five cases (24 installer cases in `test_bootstrap_dry_run.py`,
  `test_bootstrap_isolated.py`, `test_bootstrap_main_orch_e2e.py`,
  `test_bootstrap_node_recovery_stage.py`; 11 resolver cross-checks in
  `test_skill_resolver_xval.py`) gated on
  `Path.home()/".hermes"/"hermes-agent"/venv/bin/python` existing — one candidate,
  frozen at import, no way for a machine to declare the checkout it has. Under the
  roster-wide HOME seal that retired all 35 as *skips* while the leg printed green,
  and that subset alone took 230.35 s unsealed against 2.74 s sealed: an ~80x
  faster leg bought by deleting coverage. `tests/hermes_tree.py` is the seam in the
  shape #604 proved for the sparkrun checkout: `HERMES_AGENT_PATH` is the documented
  pin and, when set, the answer (nothing else consulted, so a pinned skip reason
  cannot name a home); unpinned the candidates are a repo-relative sibling checkout
  then the login-home default, in that order; the gate is the interpreter, not the
  directory name; the paths the cases spawn resolve at CALL time. Same variable
  production already honours (`hscc_daemon/hermes_import.py`, `autodown.py`,
  `kanban_live.py`, `hscc-roles/hscc.py`) and the same one `scripts/run_tests.sh`
  declares for the machine's tree before it seals, so no new runner surface is
  needed — and no `SUITE_ENV_UNSET` change, so `alone_sweep.sh`'s `-u` mirror needs
  no move. `HERMES_HOME` is never consulted: on a dispatched worker it names a
  profile dir, and that join is the bug t_8579fba4 / t_417610e6 removed from
  `apply_patches.py` / `ensure_review_feature.py`.
---
kind: Fixed
task: t_5b560adf

- **`doctor`'s preflight crashed the installer on a host with no default cluster.**
  `sparkrun cluster default --json` prints the literal `null` when it resolves no
  `~/.config/sparkrun` (the empty-HOME posture, i.e. every CI runner and every fresh
  host), `json.loads` accepts that, and
  `_sparkrun_default_cluster_matches_serving` then did `data.get("hosts")` on `None` —
  `AttributeError` inside `Prerequisites (doctor)`, so `bootstrap.sh` died before any
  stage ran. The check already refused an *empty* answer and an *unparseable* one; this
  is the third shape a real host produces, and it is now report-only
  ("not a mapping — skipped") like every other branch there. Found because this card's
  seam stopped the four `test_bootstrap_isolated.py` installer cases from skipping under
  the roster-wide HOME seal — with the old `~`-derived gate they were invisible
  everywhere the crash could bite.
---
kind: Verified
order: 1
task: t_5b560adf

- Two arms, one sha (`7220b5eb` = #606's seal head `58653473` + this branch's
  `c920b708`), canonical `HSCC_TEST_LEGS=hscc-bootstrap bash scripts/run_tests.sh -rsf`:
  sealed **1 failed, 1343 passed, 0 skipped** (227.86 s leg) against
  `HSCC_TEST_NO_SEAL=1` **1344 passed, 0 skipped** (234.05 s). The seal no longer
  retires anything on this leg, and the leg's wall clock went UP by ~4x, which is the
  point: the same tree with a *bogus* pin retires the coverage in 46.60 s
  (1306 passed, 36 skipped, each reason naming the bogus pin), and #606 without this
  seam sealed to 1296 passed / 35 skipped in 53.97 s at `58653473`. The runner's own
  log line proves the declaration reached the child
  (`HOME seal: declared HERMES_AGENT_PATH=…`).
  The 1 is not the seam's cost and not the seal's coverage — it is the same machine
  dependency one layer further in: `test_fresh_bootstrap_leaves_every_orchestrator_reviewable`
  asserts `*-orch` profiles exist, and `hscc-roles/hscc.py generate` derives its agent
  list from state the sealed home does not have, so it provisions none. Attributed and
  carded (`t_edc71fcb`, narrowed to that one case) — not skipped, not weakened.
  Pre-lane reference for this leg, `96a9f38a` (t_2b2a6c3b's roster): `1329 passed`
  unsealed → `1294 passed, 35 skipped` sealed, the subset `230.35 s → 2.74 s`.
- `test_hermes_agent_tree_seam.py`: 11 cases green in the runner. Mutation battery
  over `hermes_tree.py` + one consumer — **8/8 CAUGHT, 0 survived** (pin
  short-circuit deleted, candidate order swapped, pasteable `PIN=<path>` clause
  weakened, HOME leaked into a pinned skip reason, gate weakened to the directory,
  pin re-frozen at import (compound), resolver fabricates a path instead of
  skipping, consumer swaps in a blanket reason). Green control first, pristine
  reset + byte-compare per probe, `compile()` every mutant, anchors `count == 1`,
  in-run provenance gate (a `-p` plugin writing the imported `hermes_tree.__file__`,
  asserted to be inside the copy), `dirty after: ''`.
- `scripts` leg, sealed arm at `0fd66683`: **635 passed, 5 skipped** — the skip
  count #606's `HSCC_TAP_DIR` declaration already made the leg delta-free; no new
  seam built or needed there.
- `changelog_fragments.py check`: OK (132 fragment entries). Diff grepped for
  address / token shapes: clean.
---
