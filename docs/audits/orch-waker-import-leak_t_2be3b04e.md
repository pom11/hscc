# t_2be3b04e — order-dependent wake-ledger cluster: root cause + fix

Status: IN PROGRESS. Card: `test_orch_waker.py::test_enumerate_includes_legacy_default_and_named_boards`
fails `KeyError: 'beta'` when other files share the pytest process; passes alone.
Parent t_b014c2b8 measured the cluster at **7 tests** on the full canonical leg.

## Reproduction (measured, this card)

Worktree: `wt/t_2be3b04e` @ origin/main `1b5e9eb0`.
Python: `~/.hermes/hermes-agent/venv/bin/python`.

The card's 13-file combination reproduces in ~13 s:

```
env -u HERMES_DELEGATED_CHILD_CONTEXT ~/.hermes/hermes-agent/venv/bin/python -m pytest -q -rs \
  hscc_daemon/tests/test_kanban_live.py ... scripts/tests/test_dep_pr_watcher.py -p no:cacheprovider
-> 1 failed, 597 passed in 12.99s      # KeyError: 'beta' at test_orch_waker.py:439
```

## Evidence — the leaking module (asked for by the card)

Printed from inside the failing test in the failing combination
(`-p diag_plugin`, scratch `diag_plugin.py`):

```
sys.modules["hermes_cli"]            __file__ = /private/var/folders/.../pytest-of-desac/pytest-973/test_state_resets_to_ok_on_suc0/hermes-agent/hermes_cli/__init__.py
sys.modules["hermes_cli.kanban_db"]  __file__ = /private/var/folders/.../pytest-of-desac/pytest-973/test_state_resets_to_ok_on_suc0/hermes-agent/hermes_cli/kanban_db.py   list_boards=False
sys.modules["hermes_cli.kanban_db_connect"]._kb  -> .../test_resolves_from_hermes_agen0/hermes-agent/hermes_cli/kanban_db.py
sys.modules["hermes_cli.kanban_db_dispatch"]._kb -> .../test_state_resets_to_ok_on_suc0/hermes-agent/hermes_cli/kanban_db.py
autodown._KANBAN_LOAD = {'ok': True, ...}      # says "fine" while holding the fake
autodown._load_kanban_db_or_default() -> the TEMP fake, list_boards=False
sys.path[:2] = ['.../test_state_resets_to_ok_on_suc0/hermes-agent',
                '.../test_resolves_from_hermes_agen0/hermes-agent', ...]
HERMES_AGENT_PATH = None
```

Both temp dirs are named after their planting test:
`test_resolves_from_hermes_agent_path` and `test_state_resets_to_ok_on_success`
— both `hscc_daemon/tests/test_autodown.py::TestLoadKanbanDb`.

`hermes_cli.kanban_db` without `list_boards` is autodown's documented
"single-board shape" → `enumerate_boards()` returns one board → `beta` vanishes.

## Findings

(writing as I measure)
