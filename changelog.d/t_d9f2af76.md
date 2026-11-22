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
