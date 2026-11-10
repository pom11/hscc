kind: Fixed
task: t_8eafef82

- **`autodown._load_kanban_db_or_default` no longer leaves the Hermes agent tree
  on `sys.path` for the life of the process — and no other module needs it to.**
  The function documented that it put the resolved runtime dir on `sys.path`
  "only for the duration of this one import" and then left it at `sys.path[0]`;
  `kanban_live._load_hermes_config` and `profile_roster._hermes_gate` carried
  their own copy of the same insert, and nothing took the entry back out. In a
  daemon that is inert (one real tree). It is not inert in a test process — the
  leak is what turned one planted fake `hermes_cli` into poison every later
  consumer re-imported, even after an evictor had cleaned `sys.modules`
  (`docs/audits/orch-waker-import-leak_t_2be3b04e.md` §6, the production half PR
  #270 left open) — and it is not inert in the venv `/opt/homebrew/bin/hscc`
  runs in, where `dispatcher_wedge`'s five bare `from hermes_cli import …`
  readers could only answer because some *earlier* check had leaked the tree.
  One module owns the window now: `hscc_daemon/hermes_import.py`, with
  `agent_tree_on_path()` (and its one-line form `import_hermes()`) inserting the
  resolved dir and restoring `sys.path` in its `finally` — same entries, same
  order, exception path included. It is refcounted per path under a lock,
  because the daemon runs autodown and the wedge loop on their own threads and
  an unrefcounted restore would let the first window to finish pull the path out
  from under the other (a `ModuleNotFoundError` nobody could attribute). Every
  reader now opens its own window — autodown, kanban_live, dispatcher_wedge
  (five sites), escalate_watcher, profile_roster — so no answer depends on who
  ran first, which is the GAP-D-4 lesson applied to the wedge gate. Where a site
  *calls* into Hermes the call is inside the window too: a loaded package
  resolves its submodules through its own `__path__`, so the returned module
  needs no path, but a fresh top-level import triggered at call time does.
- **Deliberate test-expectation change (not collateral).**
  `test_kanban_live_caps.py::test_load_hermes_config_resolves_the_agent_tree_once`
  asserted `str(agent) in sys.path` *after* the call — it pinned the leak. It is
  now `…_then_closes_the_window` and asserts the opposite plus the non-vacuity
  half (the module it resolved has its `__file__` inside the planted tree and the
  gate serves nothing else, so the window undeniably opened). The pre-fix "second
  call must not duplicate" assertion is kept: the pre-fix code passed it, which is
  why the leak survived it. `autodown` gains the matching guard — the test quotes
  the promise out of the live docstring and plants a package that records
  `sys.path` at import time, so one test shows the tree held during the import
  and `sys.path` byte-identical afterwards.

---

kind: Verified
task: t_8eafef82

- **The restore does not break a later Hermes call, measured rather than
  argued.** A probe drove the real chain against the real `~/.hermes/hermes-agent`
  tree in the daemon's own interpreter. Pre-fix the agent dir was `sys.path[0]`
  after a resolve; at this commit it is absent and the chain still works —
  `list_boards()` returned the operator's 23 boards, `board_db_paths()` resolved
  every per-board DB, a `LIMIT 1` probe read 480 tasks off the live board, and
  `list_boards()` returned 23 again with `hermes_cli` evicted from `sys.modules`.
  The same probe records every `find_spec` attempt for a `hermes*` name made
  during those calls: **zero**, so no consumer triggers a fresh top-level import
  at call time. In the keg venv `_review_dispatch_config_state()` now answers
  `True` in a fresh process, where pre-fix it answered `None` until something
  leaked; `kanban_live._read_caps()` reports `(None, None)` with an empty
  `reason` in both interpreters because `kanban.max_in_progress` is genuinely
  unset here, not because the read failed — the distinction GAP-D-4 exists to
  keep.
- **Canonical leg green.** `hscc_daemon/tests scripts/tests` under
  `~/.hermes/hermes-agent/venv/bin/python -m pytest -q -rfEs -p no:cacheprovider`,
  harness env (`scripts/run_tests.sh`'s `env -u` of the worker board pin plus
  `HERMES_DISABLE_LAZY_INSTALLS=1 HSCC_SKIP_COMPACT_PROBE=1`): `3979 passed,
  2 skipped in 761.17 s`, exit 0, at `a037d29e`.
- **Five mutation probes, control green, provenance asserted inside every run**
  (each on a copy, original restored before the next probe): M1 the `finally`
  never restores (the pre-fix observable) → 11/11 new tests red; M2 the refcount
  removed → the two-thread test and the nested test red; M3 `autodown` plain
  `sys.path.insert` alongside the window → the acceptance test red; M4 the wedge
  caps reader back to a bare import → its order-independence test red; M5 the
  wedge review-dispatch reader likewise → its order test red.
