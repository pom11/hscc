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
