kind: Fixed
task: t_2be3b04e

- **Board enumeration no longer silently shrinks to one board when the test
  process is shared.** `test_orch_waker.py::test_enumerate_includes_legacy_default_and_named_boards`
  failed `KeyError: 'beta'` in the canonical leg and passed alone. Root cause,
  measured (`docs/audits/orch-waker-import-leak_t_2be3b04e.md`):
  `autodown._load_kanban_db_or_default` does `sys.path.insert(0, <agent dir>)`
  and never removes it — despite its docstring promising "only for the duration
  of this one import" — so the FAKE `hermes_cli` tree that
  `test_autodown.py::TestLoadKanbanDb` plants under `tmp_path` stays at
  `sys.path[0]` for the rest of the process. pytest keeps a test's `tmp_path`
  alive until the next session, so the next test that touched the seam (the
  first `test_autodown_cli.py::TestStatus`) re-imported that fake, and — being
  a lib without `list_boards`, i.e. autodown's documented single-board shape —
  it was then the resident kanban lib for every later file, including the
  waker. `test_autodown`'s own evictor was written for this exact hazard but
  only removes `sys.modules` entries, never the `sys.path` entry that lets the
  fake walk back in.
  `hscc_daemon/tests/conftest.py` now pins the `hermes_cli` import cache per
  test, like the autouse fixtures here already pin the kanban root, the serving
  topology and sparkrun resolution: drop temp-rooted `sys.path` entries, and
  purge + restore the `hermes_cli*` subtree when a test poisons it. The purge is
  whole-subtree rather than planted-only because `hermes_cli/kanban_db_*.py`
  bind `import kanban_db as _kb` at import time, so a real module imported while
  a fake is resident keeps the fake — evicting the fake alone does not repair
  it. No production code changed and no assertion was relaxed; a test that only
  ever touches the real library pays nothing (re-import measured at ~5 ms).
  New `hscc_daemon/tests/test_hermes_cli_import_isolation.py` pins it with a
  nested real pytest run — one generated file plants through the production
  resolver, the next file in that process must still see hermes' real
  `list_boards` — plus unit tests for the poison detector, including the
  `real module holding _kb -> planted fake` shape a plain evict cannot catch.
