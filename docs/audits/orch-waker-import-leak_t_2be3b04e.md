# t_2be3b04e — the wake-ledger cluster: a planted fake `hermes_cli` decided what the next file saw

**Card:** t_2be3b04e (opened from t_b014c2b8's suite attribution). **Verdict: LOCATED**
for the enumeration failure; the remaining six of the cluster are tracked in
§5 with their own evidence.

## 1. The symptom

`hscc_daemon/tests/test_orch_waker.py::test_enumerate_includes_legacy_default_and_named_boards`
fails `KeyError: 'beta'` (`boards = ow.enumerate_boards()` has no `beta`) when
other files share the pytest process, and passes when run alone. The canonical
leg runs all of `hscc_daemon/tests` in ONE process, so it is red there today.

Measured on `origin/main` `1b5e9eb0` (worktree `wt/t_2be3b04e`, python
`~/.hermes/hermes-agent/venv/bin/python`):

| run | result |
| --- | --- |
| full canonical leg `pytest -q -rs hscc_daemon/tests scripts/tests` | **7 failed, 3923 passed, 4 skipped in 656.06 s** (the 8th failure in my own log was `test_repo_readmes_pass_the_lint`, caused by this doc landing before its index entry — not pre-existing) |
| the card's 13-file combination | **1 failed, 597 passed in 12.99 s** |
| `test_orch_waker.py` alone | 40 passed |
| the three wake-ledger files alone (`test_orch_waker`, `_dead_pane`, `_state_collision`) | 71 passed |
| `test_autodown.py` + `test_orch_waker.py` | 243 passed — the plant alone is NOT enough |

## 2. The evidence the card asked for

Printed from **inside** the failing test in the failing combination
(`-p diag_plugin`, a throwaway pytest plugin that dumps `sys.modules`):

```
sys.modules["hermes_cli"]                  __file__ = /private/var/folders/…/pytest-of-desac/pytest-973/test_state_resets_to_ok_on_suc0/hermes-agent/hermes_cli/__init__.py
sys.modules["hermes_cli.kanban_db"]        __file__ = …/test_state_resets_to_ok_on_suc0/hermes-agent/hermes_cli/kanban_db.py   list_boards = False
sys.modules["hermes_cli.kanban_db_connect"]._kb      -> …/test_resolves_from_hermes_agen0/hermes-agent/hermes_cli/kanban_db.py
sys.modules["hermes_cli.kanban_db_dispatch"]._kb     -> …/test_state_resets_to_ok_on_suc0/hermes-agent/hermes_cli/kanban_db.py
autodown._KANBAN_LOAD = {'ok': True, 'reason': '', 'warned': True}     # reports "fine" while holding the fake
autodown._load_kanban_db_or_default() -> the temp fake; list_boards = False
sys.path[:2] = ['…/test_state_resets_to_ok_on_suc0/hermes-agent',
                '…/test_resolves_from_hermes_agen0/hermes-agent', …]
HERMES_AGENT_PATH = None
```

The two temp dirs are named after the tests that planted them:
`test_resolves_from_hermes_agent_path` and `test_state_resets_to_ok_on_success`
— both `hscc_daemon/tests/test_autodown.py::TestLoadKanbanDb` (`:773`, `:832`).

A `hermes_cli.kanban_db` **without `list_boards`** is autodown's documented
*single-board shape*, so `enumerate_boards()` returns one board and `beta`
vanishes. That is the whole failure.

## 3. The mechanism, in order

A per-test trace of the failing process (`-p trace_plugin`, 331 tests reported)
names the first polluter as
`test_autodown_cli.py::TestStatus::test_never_enabled_reports_disabled_and_creates_nothing`
— a test that never plants anything itself. The chain:

1. `test_autodown.py::TestLoadKanbanDb._write_fake_hermes_cli` plants
   `<tmp>/hermes-agent/hermes_cli/{__init__,kanban_db}.py` and resolves it
   through the **production** resolver by setting `HERMES_AGENT_PATH`.
2. `autodown._load_kanban_db_or_default` does
   `sys.path.insert(0, hermes_path)` and **never removes it** — its own docstring
   says the path is added "only for the duration of this one import", which the
   code does not do (`hscc_daemon/autodown.py:769-771`). So
   `<tmp>/hermes-agent` sits at `sys.path[0]` for the rest of the process.
3. The class fixture `_evict_planted_hermes_cli` (autodown's own finalizer,
   `test_autodown.py:753`) correctly evicts the planted modules… and cannot
   touch `sys.path`.
4. pytest keeps a test's `tmp_path` on disk until the NEXT session, so the
   planted tree is still importable. The next test that touches the seam
   (`test_autodown_cli.py::TestStatus`, via `cmd_autodown(["status"])` →
   `_has_active_work` → `_load_kanban_db_or_default`) finds `hermes_cli` absent
   from `sys.modules`, walks `sys.path`, hits the planted tree at index 0, and
   imports the fake.
5. Nobody evicts *that* one — its own class finalizer never ran, because it
   never planted anything. It is now the resident kanban lib for the rest of the
   process, so `test_orch_waker.py`'s enumeration test gets the fake.

Two properties made this hard to see and are worth keeping in mind:

- **It survives the evictor that exists.** `TestLoadKanbanDb`'s finalizer was
  written for exactly this hazard (its docstring names board enumeration) but it
  only removes `sys.modules` entries. Removing the leak needs the `sys.path`
  half too.
- **Evicting the fake is not sufficient in general.** `hermes_cli/kanban_db_*.py`
  bind `import kanban_db as _kb` at import time, so a *real* module imported
  while a fake is resident keeps the fake (`_kb` above points at a **different**
  test's temp tree than the resident fake). The same shape cost t_b014c2b8
  `8e2283a7`. A fix must therefore purge the whole `hermes_cli*` subtree, not
  just the temp-rooted entries.
- `_KANBAN_LOAD` says `ok: True` throughout: a fake *is* a successfully resolved
  lib, so `hscc autodown status` has no idea this happened. (Not a production
  problem — a production process never plants a fake — which is why this stayed
  a test-isolation bug.)

## 4. The fix

Test-side, as the card directed: fix the leak, not the assertion. The assertion
is untouched.

`hscc_daemon/tests/conftest.py` gains an autouse fixture alongside the suite's
other hermeticity pins (`_hermetic_kanban_root`, `_hermetic_topology`,
`_hermetic_sparkrun_resolution`):

- `_hermetic_hermes_cli_imports` — per test, snapshot the `hermes_cli*` slice of
  `sys.modules`; at teardown drop every `sys.path` entry that lives under a
  pytest temp root (leak 1) and, if the resident cache is poisoned, purge the
  whole `hermes_cli*` subtree and put the snapshot back (leak 2).
- `_hermes_cli_poison_reason()` names WHY the cache is unusable — a planted
  module, **or** a real module whose `_kb`/`kanban_db` still points at one.
- The conftest helper `_evict_planted_hermes_cli` (used by the `real_hermes_cli`
  fixture) now shares that predicate instead of carrying a second, slightly
  different definition of "is this a plant". Two more private copies of the same
  rule survive in `test_autodown.py:753` and `test_board_pin.py:326`; converging
  them onto conftest's is a small follow-up, deliberately not smuggled into this
  fix.

Cost measured: the real subtree re-imports in ~5 ms once CPython has the source
cached (33 ms cold), and a test that only ever touched the real library pays
nothing at all (no purge, no re-import). The card's combination ran in 13.11 s
after the fix vs 12.99 s before.

`hscc_daemon/tests/test_hermes_cli_import_isolation.py` pins it five ways,
including a **nested real pytest run** over two generated files — file A plants
through the production resolver, file B (same process, next file) must still see
hermes' real `list_boards`. File A writes its resolved `__file__` to a marker, so
the regression cannot go green by never planting.

## 5. The other six of the cluster

(still open — see the card comment; these are the `_dead_pane` /
`_state_collision` wake-ledger tests, whose failure text is about watermarks and
pending batches, not enumeration, and none of which reproduce in the
combinations tried so far.)
