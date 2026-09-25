# t_8c729895 — dispatcher_wedge.py: drop expired hermes_cli.kanban_db compat shim

Status: DONE (merged to main + pushed + deployed)
Date: 2026-09-25

## Problem

`hscc_daemon/dispatcher_wedge.py` imported `has_spawnable_ready` /
`has_spawnable_review` via `hermes_cli.kanban_db`'s PEP 562 compat shim
(`__getattr__` → `_PLUGIN_COMPAT_LAZY`), whose stated removal date
(2026-09-14) had passed. Every attribute access on the real module fired
`HermesPluginCompatWarning` — two per `hscc start` (once per `hasattr` in
`_board_snapshot`, dispatcher_wedge.py:236 and :238).

Worse than the noise: the call is `hasattr`-guarded. If/when the shim is
dropped, `hasattr(kanban_db, "has_spawnable_ready")` would resolve `False`
and the code would silently fall through to the narrower SQL predicate —
silently narrowing dispatch eligibility with no error. Exactly the failure
class this repo keeps getting bitten by.

## Fix

`dispatcher_wedge.py`:

- Added `_bind_spawnable_helpers(kanban_db)`: lazily imports
  `hermes_cli.kanban_db_dispatch` and binds its `has_spawnable_ready` /
  `has_spawnable_review` callables into the real module's `__dict__`
  (mirroring how `autodown._load_kanban_db_or_default` already binds
  `connect`/`connect_closing`). Idempotent and cheap; after the first bind
  the names sit in `__dict__`, so `hasattr(kanban_db, ...)` in
  `_board_snapshot` resolves from `__dict__` and never touches the shim.
- `_capture_kanban` now calls `_bind_spawnable_helpers` ONLY on the lib
  returned by `_load_kanban_db_or_default()` (the production path). An
  injected test fake keeps its own methods untouched — binding onto a fake's
  instance `__dict__` would shadow its class methods and changed test
  behaviour (narrowing the fake's spawnable check via the real
  `profile_exists` on fabricated profile names).

Three cases handled:
  * old Hermes (pre-split): native `__dict__` members, never a warning,
    left untouched.
  * current Hermes (v2026.9.14): bound from `kanban_db_dispatch`, no shim.
  * hermes_cli unreachable (test-only interpreter): nothing to bind; the
    whole probe goes fail-safe `unreachable` (never a silent SQL narrowing
    with a real board).

**No old-path fallback**, per scope item 1. Finding: the repo pins Hermes at
`v2026.9.14` (`hscc-bootstrap/runtime-versions.json`) — exactly the shim's
removal date. `kanban_db_dispatch` exists there and exports both callables
(verified by execution). The old `hermes_cli.kanban_db` shim is "kept only
for external plugins"; `dispatcher_wedge.py` is internal hscc code, not an
external plugin. Keeping a guarded old-path fallback would retain exactly the
silent `hasattr` fall-through the card exists to eliminate.

## Regression tests (hscc_daemon/tests/test_dispatcher_wedge.py)

`TestSpawnableResolutionNotSilentlyNarrowed` (3 tests, skipped on
interpreters without `hermes_cli` via `pytest.importorskip`):

1. `test_binds_real_callables_from_kanban_db_dispatch` — binding installs the
   ACTUAL `kanban_db_dispatch.has_spawnable_ready`/`has_spawnable_review`
   (identity check), not stubs.
2. `test_legacy_attr_absent_still_finds_function` — a minimal stub without
   the legacy helpers (the "shim dropped" stand-in; the real module's names
   are PEP 562 shims, not `__dict__` entries, so they cannot be `delattr`'d)
   still gets the real callables bound; the detector still classifies the
   work as spawnable (a genuine stall is declared, NOT silently narrowed).
3. `test_hasattr_resolves_from_dict_no_compat_warning` — after binding, the
   names sit in `__dict__` and resolve to real callables (no shim, no
   narrowing).

All assert on the binding MECHANISM (monkeypatched fakes), never on live
board/profile state.

## Verification (execution)

Before (`hscc start`, deployed pre-fix code):

    Starting hscc_daemon...
    [INFO] Daemon starting
    OK  hscc_daemon started (PID 82897)
    hermes plugin compat: `hermes_cli.kanban_db.has_spawnable_ready` moved to `hermes_cli.kanban_db_dispatch.has_spawnable_ready`. The old path is kept only for external plugins and is removed on 2026-09-14; update your import.
    /Users/desac/.hermes/plugins/hscc_daemon/dispatcher_wedge.py:236: HermesPluginCompatWarning: ...
    hermes plugin compat: `hermes_cli.kanban_db.has_spawnable_review` moved to `hermes_cli.kanban_db_dispatch.has_spawnable_review`. ...
    /Users/desac/.hermes/plugins/hscc_daemon/dispatcher_wedge.py:238: HermesPluginCompatWarning: ...

After (deployed post-fix code):

    Starting hscc_daemon...
    OK  hscc_daemon started (PID 90074)
    # zero HermesPluginCompatWarning lines (grep count 0)

Also confirmed at the code-path level: calling `_capture_kanban()` on the
real Hermes lib before the fix -> `HermesPluginCompatWarning count: 2`;
after the fix -> `count: 0`. The daemon log now shows the Dispatcher-wedge
check running with no compat warning.

## Suite numbers

Hermes venv (`~/.hermes/hermes-agent/venv/bin/python`), `scripts/run_tests.sh`:
ALL GREEN — hscc-bootstrap, hscc-commands, hscc-roles, hscc-cluster,
hscc-project, hscc_daemon, sparkrun-hermes, hscc-api (hscc_daemon alone:
1160 passed, 52.06s).

p313 (`/Users/desac/miniconda3/envs/p313/bin/python`), `scripts/run_tests.sh`:
ALL GREEN — hscc-bootstrap, hscc-commands, hscc-roles, hscc-cluster,
hscc-project, hscc_daemon, sparkrun-hermes, hscc-api (hscc_daemon alone:
1157 passed, 3 skipped — the 3 skipped are exactly the new regression tests
which `pytest.importorskip` correctly skips where `hermes_cli` is absent).

Focused: `test_dispatcher_wedge.py` — hermes venv 27 passed; p313
24 passed, 3 skipped (the 3 new regression tests skip where `hermes_cli`
is absent, which is correct).

## Merge / push / deploy

- Branch: `wt/kanban-db-dispatch-import`
- Commit: `88ca315` (1 commit ahead of pre-merge main; rebased onto latest
  main `82ccc00` before merge).
- Merge: `eb0a10c` onto main (--no-ff), 2 files, +128/-1.
- Pushed: `origin/main` `82ccc00..eb0a10c`.
- Deployed: `python3 hscc-bootstrap/install_payload.py` (backups created).
- hscc daemon restarted, confirmed warnings gone. Gateway NOT touched.

## Note

The hscc daemon in this environment is managed by a launchd agent
(`com.hermes.hscc_daemon`); it may be stopped/replaced externally shortly
after start. That lifecycle is unrelated to this change — the dispatcher
check runs cleanly (no compat warning) while the daemon is up, both before
and after the fix; the only difference is the removed warnings.
