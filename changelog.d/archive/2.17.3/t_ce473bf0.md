kind: Fixed
task: t_ce473bf0

- **Four `hscc_daemon` cases that controlled the Hermes agent-tree locator
  through only half of the resolver now control all of it.** The roster-wide
  HOME seal (PR #606) moves `HOME` to an empty `/tmp` dir for every leg and
  declares the machine's real paths instead, which turned four silently-skipped
  cases RED — all of them the same harness bug with two faces. Two spellings of
  the tree as `Path.home() / ".hermes" / "hermes-agent"`
  (`test_hermes_cli_import_isolation.py`, once in the module and once inside the
  nested probe file B, where the outer AST cannot see it) now ask
  `hermes_cli_plant.agent_tree_root()` — which IS production's
  `hermes_import.resolve_agent_tree()`, so the rule and the shipped code cannot
  disagree about where hermes lives. And
  `test_autodown.py::test_state_resets_to_ok_on_success` now deletes the
  `HERMES_AGENT_PATH` override before patching the fallback constant, because
  `autodown.py:874-875` reads the environment FIRST and the runner's declared
  pin was silently outranking the fixture's own "tree missing" premise. No case
  was traded for a skip and no assertion was weakened;
  `test_no_live_hscc_leak`'s fourth red was that same fixture failing inside its
  sandboxed child run.
- **One span-guard case stopped judging a fixture clock against the wall clock.**
  `test_node_pause_expiry.py::test_the_rule_is_in_the_policy_not_only_in_the_tick`
  called `resume_host` without `now=`, so "W2's pause is not yet due" was decided by
  `datetime.now()` (`node_pauses.py:126`) instead of by the file's own `NOW`. The
  decay this was part of is fixed at the root on main by #616 (anchoring the fixture
  clock to import time); naming the clock removes the remaining window instead of
  shrinking it. Both span-guard cases measured red in **both** the sealed and the
  unsealed arm before either fix — the seal is not their cause — and green in both
  after.
---
kind: Added
task: t_ce473bf0

- **`hscc_daemon/tests/test_agent_tree_locator_single_source.py` pins the two
  rules structurally**, so a fifth site cannot be written the way the four were:
  R1 — no test file, and no test file embedded as a string, may spell the Hermes
  agent tree out of the home (the tree is asked of the seam); R2 — a function
  that patches the fallback constant must also neutralise the environment
  override that outranks it. One allowance (the harness owner's own
  `env or <default>` fallback), pinned against going stale silently; positive
  controls per rule so a checker that reports nothing cannot pass. Proven
  against the shipped shapes: over the pre-fix blobs it reports 2 R1 + 1 R2
  offenders, over the fixed tree zero.
---
kind: Verified
task: t_ce473bf0

- Measured at PR #606 head `58653473` (main `4076ebc1` merged in), one process
  per leg, `HSCC_TEST_LEGS=hscc_daemon bash scripts/run_tests.sh -rsf`, and the
  same with `HSCC_TEST_NO_SEAL=1`. Before this card's diff: **sealed `6 failed,
  4713 passed, 1 skipped` (168.91 s) vs unsealed `2 failed, 4717 passed,
  1 skipped` (185.06 s)** — 4720 items in both arms, so the delta is outcome,
  not collection.
- **The 18 skips `t_2b2a6c3b` could attribute to only 3 are one class, and #606's
  `HERMES_AGENT_PATH` declaration closed 17 of them.** The sealed arm at this
  head reports exactly one skip, identical in both arms
  (`test_orch_waker_wake_claim.py:1495`, deployed Hermes has no §3.1 wake-claim
  helpers), so the seal costs this leg no coverage — it returns the coverage as
  failures, which is where the four reds above came from.
- **The keg-venv red closed itself the same way, with no fixture and no skip**:
  the keg glob is the absolute `/opt/homebrew/Cellar/hscc/…`, no `HOME` anywhere
  in it, so the seal could only have removed the agent tree the probe's chain
  imports — whose absence the probe already reports, naming the seam
  (`autodown.py:879`).
  `test_brew_keg_venv_imports_the_board_read_chain[2.17.2]` passes sealed.
- **After the diff the leg is green in both postures, with identical counts**:
  `4756 passed, 1 skipped` sealed (162.74 s) and `4756 passed, 1 skipped` unsealed
  (176.53 s) at `481df95b` — main `51e0106c` + #606's three commits + this card's four
  (ref `wt/t_ce473bf0`; the four-commit slice without the seal is `wt/t_ce473bf0-slice`).
  The six previously-red node ids are inside that green, and the new guard contributes
  7 items to it.
