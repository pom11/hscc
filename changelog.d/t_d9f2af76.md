kind: Added
task: t_d9f2af76

- **No leg test may spawn a PATH-resolved interpreter or a home-prefixed path.**
  `scripts/hermetic_spawn_guard.py` is an AST sweep of spawn-call arguments, and
  every leg now carries a `tests/test_hermetic_subprocess_spawn.py` that applies
  it to its own dir: `subprocess.{run,Popen,check_output,check_call}` may not
  take argv0 `"python"`/`"python3"` (a PATH lookup — on the runner that is the
  setup-python base interpreter, which has none of the project's deps), and no
  spawn argument may be a string constant rooted in a user home (it exists only
  on the machine that owns it, so the child exercises a different checkout there
  and dies with `FileNotFoundError` on a runner). Both are the t_bb057914 class
  and both are invisible to every environment posture, because the trigger is a
  source literal. It is an AST sweep rather than a substring ban because
  `hscc-project/tests/test_registry.py` and `test_kanban.py` embed home-prefixed
  paths as path-parsing *fixture data* — the sweep distinguishes a machine read
  from data by position in the tree, so those cases stay green. Fail-closed: an
  unparsable source is a violation, a missing detector raises, a sweep of zero
  files fails, and no guard may skip (a watcher that can skip is a watcher a
  mutant can silence).
---
kind: Fixed
task: t_d9f2af76

- **`hscc-bootstrap/tests/test_restart_daemon.py` read the machine's `PATH`.**
  The sanity arm of `test_import_package_stub_succeeds_with_pythonpath` spawned
  argv0 `"python3"`, so which interpreter reproduced the `ModuleNotFoundError`
  was a property of whichever `python3` came first on `PATH`; the env it was
  handed also inherited `PYTHONPATH`, while the arm claims "no PYTHONPATH".
  It now spawns `sys.executable` with `PYTHONPATH` dropped — same failure, for
  the same reason, on every host.
---
kind: Verified
task: t_d9f2af76

- **Guard proven to bite: 7/7 mutants caught, plus a green false-positive arm.**
  Planting argv0 `"python3"`, a home-prefixed `cwd`, or argv0 hidden behind a
  local name into `hscc-cluster/tests` each reddened that leg's guard naming the
  planted file; deleting `BANNED_ARGV0` or `is_home_prefixed` from the detector
  reddened its own unit tests *and* every leg guard's non-vacuity arm; pointing a
  guard's sweep root at an empty dir failed the `scanned > 0` assertion instead
  of passing. Adding `pytest.skip` to a guard left that guard reporting
  `1 passed, 1 skipped` — silenced, exactly as the card warned — while the
  no-skip belt reddened. Planting home-prefixed paths as *fixture data* (the
  `test_registry.py` shape) kept the guard green, which is the arm that
  distinguishes the AST sweep from the substring ban that would have been deleted.
  Control green and provenanced, tree clean after every restore.
- **It catches the bug it was written for.** The detector reports all three faults
  in the pre-fix `hscc-cluster` source at `df7d517e` (both argv0 `"python"` and the
  home-prefixed `cwd`) and goes clean on the fixed source at `96a9f38a`. The first
  attempt at this arm read `origin/main` and printed zero violations — PR #597 had
  merged mid-card, so "main" already carried the fix; a base arm must pin a sha,
  not a moving ref.
