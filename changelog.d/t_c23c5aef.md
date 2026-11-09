kind: Fixed
task: t_c23c5aef

- **The unified `hscc cluster down` still printed `ERROR` for a fleet that was
  already down — and for a clean stop as well.** t_1244b3b4 (PR #253) taught the
  *standalone* view (`hscc-cluster/hscc.py::_human_lines`) to read the additive
  `benign_no_workloads` / `benign_note` keys, but `hscc cluster down` has two
  human renderers over the same engine dict, and the one the operator actually
  types into — `hscc_daemon/cluster_render.py::render_down` → `_render_fleet_op`
  — read `success` alone. Two defects, both measured on `f2cbae3f` by rendering
  faked `_run_or_print` dicts (no live `cluster down`, no ssh, no sparkrun):

  1. `{success: False, returncode: 1, benign_no_workloads: True, …}` → `ERROR`/1.
     Fixed by the same two-line read the standalone view uses:
     `benign = (not ok) and bool(result.get("benign_no_workloads"))`,
     `shown_ok = ok or benign`, and the note is printed under the OK verdict.
  2. `{success: True, returncode: 0, error: ""}` → `ERROR`/1 — a *clean*
     teardown, red. `_run_or_print` always sets `error` (captured stderr, `""`
     when there was none) and `_object_error` hands that `""` back, so the old
     `err is None` test read an empty stderr as a failure. Nothing upstream can
     rescue that shape: `_benign_no_workloads_note` returns None for any rc-0 run
     by design, so no benign key ever exists there. The veto is now on a
     NON-EMPTY error string, which is the only thing that key has ever meant.

  **No second classifier.** `hscc-orch`'s constraint on this card was that the
  renderer must not grow its own copy of `_ALREADY_DOWN_MARKERS` /
  `_REAL_FAILURE_MARKERS` — two self-consistent tables in two files can
  disagree about whether a fleet is down with no test able to see it (the
  `2f90f711` kinds-vs-labels split). It needed none: the verdict is read from
  the keys the engine sets, and `cluster_render.py` never imports the engine or
  reads sparkrun's text at all. The seam the new tests use to prove agreement is
  the production one — `hscc_daemon/hscc.py::_load_cluster_engine` — so the
  corpus drives the engine's own `_benign_no_workloads_note` and then asserts the
  unified panel, the standalone `_human_lines` tag and the returned exit code all
  agree across nine shapes (clean stop, three already-down spellings,
  `could not query`, ssh permission-denied, already-down-plus-real-failure,
  unknown wording, hard failure). Two guards back it up: the verdict is asserted
  invariant to the `output` *text* at fixed keys (so a future text-based classifier
  in the renderer flips a test instead of drifting silently), and no
  marker-shaped name may be referenced by the renderer's code.

  Untouched, deliberately: `success` still means "sparkrun exited 0" (the
  `--json` dict the daemon, scripts and the iOS console dump raw is unchanged,
  and the renderer does not mutate it), `hscc-cluster/hscc.py` is not edited, and
  a genuine failure is still red — ssh/docker/auth/timeout, a host sparkrun could
  not query, or wording no marker knows. One asymmetry survives and is pinned in
  that direction: the unified view may be STRICTER than the standalone one
  (`{success: True, error: "…"}` reads OK in `_human_lines`, ERROR here), never
  softer.

- **Verified:** 25 new tests in
  `hscc_daemon/tests/test_cluster_down_benign_render.py`, green; `hscc_daemon`
  leg green at the merged HEAD (3441 passed) and `scripts` leg green there too
  (350 passed, 2 skipped); at the pre-merge commit `45f8e8d9` the whole
  `bash scripts/run_tests.sh` ran **11/12 legs green**, the single red being
  `test_a_table_identical_under_sibling_interpreters`' own 300 s subprocess cap —
  which raises before its asserts, and which `origin/main` had already fixed in
  #260 after my branch point. 7/7 mutation probes caught with a pristine CONTROL
  green (each on a fresh copy at an absolute scratch path, every substitution
  asserted to have applied, including restoring `cluster_render.py` to `f2cbae3f`
  and watching the new file go red).
