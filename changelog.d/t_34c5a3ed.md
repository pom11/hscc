kind: Fixed
task: t_34c5a3ed
order: 1

- **`test_the_per_leg_logs_do_not_pile_up_in_tmp` failed because a *sibling worker*
  was alive, not because the leg littered.** It snapshotted
  `set(Path("/tmp").glob("hscc-legs-*"))` before the pack and asserted the set was
  unchanged after — a verdict on a global namespace the test does not own. A pack's
  log dir lives for the whole run, and this fleet runs several `scripts/run_tests.sh`
  on one Mac (measured mid-fix: `run_tests.sh` processes from the merge queue, two
  worker shells and a container leg all live at once), so a sibling's dir is born
  between the two snapshots whenever the windows overlap. Reproduced by inducing the
  overlap — a fake sibling creating a `hscc-legs-*` dir every 0.2 s: the old assert
  died on `AssertionError: tmp litter` at `scripts/tests:458` while the line ABOVE it,
  the one about the directory the run actually caused, passed. That is the shape of
  the bug: the assertion that could see real litter was never the one that failed, and
  the run had littered nothing.
  **Ruling:** the claim is asserted about the directory the runner *names* in its own
  banner, because that is the only directory the run can be blamed for. Two pins keep
  the scoping from hiding litter — exactly one `mktemp -d /tmp/hscc-legs-` creation
  site in the runner (so nothing can make a dir it did not advertise) and exactly one
  banner per run — and the per-leg logs (`0-<leg>.log`, `<n>.rc`, `legs.tsv`) are all
  written *inside* that dir, so one `not exists()` is the whole `N files per run`
  claim rather than a fraction of it. Teeth proven by mutation, not by reading: drop
  the runner's cleanup trap and the test goes red on `left behind: /tmp/hscc-legs-…`;
  add a second unadvertised creation site and it goes red on the site pin. A gate
  whose red can be caused by a neighbour's liveness is a gate someone re-runs until
  green, which is the real cost this was paying.
---
kind: Verified
task: t_34c5a3ed
order: 1

- Induced the overlap instead of waiting for it: a fake sibling creating a
  `hscc-legs-*` dir every 0.2 s turned the old test red on demand —
  `AssertionError: tmp litter` at `scripts/tests/test_run_tests_leg_parallelism.py:458`,
  `1 failed, 23 deselected` — while the assert one line ABOVE it, the one about the
  directory the run itself caused, passed. That pairing is the proof the red was a
  neighbour's liveness and not litter. The rewritten test is green against the same
  churn (`1 passed`, with 10 sibling dirs created mid-run).
- Teeth by mutation, each red with its own message: delete the runner's
  `trap 'rm -rf "$LEG_TMP" …' EXIT` → `AssertionError: left behind: /tmp/hscc-legs-…`;
  add a second unadvertised `mktemp -d /tmp/hscc-legs-` site → red on the creation-site
  pin. `run_tests.sh` restored clean (`git status --porcelain` empty after both).
- Definition of done measured on the fleet, not simulated: two `scripts/tests`
  suites alive at once, in two worktrees at the same commit (the real sibling shape
  — shared `/tmp`, separate trees), reported `1 failed, 733 passed, 4 skipped`
  **each** at ~186 s, and in both the sole failure was
  `test_load_burners.py::test_signal_to_the_harness_takes_every_burner_with_it[2-130]`
  — which passes alone (`1 passed in 0.63s`), is not on this diff, and is carded
  separately (t_f8e6fe38). The test under fix passed in both concurrent suites.
  Serial run of this card's own command on the branch: **734 passed, 4 skipped in
  160.6 s**, rc 0.
- `/tmp` left as found: the 2 pre-existing dirs the card documented are untouched,
  every dir this work caused was removed, and the obvious `rm -rf /tmp/hscc-legs-*`
  shortcut was refused on purpose — a live sibling pack was watched creating and
  cleaning its own dir during verification, which is the whole reason this test may
  not judge that namespace.
