# t_8eafef82 — the Hermes agent tree now leaves `sys.path` when the import ends

`autodown._load_kanban_db_or_default` documented that it puts the resolved
Hermes runtime dir on `sys.path` "only for the duration of this one import" and
then left it at `sys.path[0]` for the life of the process. `kanban_live
._load_hermes_config` and `profile_roster._hermes_gate` carried their own copy
of the same insert. Nothing took the entry back out.

In a daemon process that is inert (one real tree, so the leaked entry is the
tree every later consumer wants anyway). It is not inert in a test process, and
it is not inert in the `hscc` CLI's venv:

* the leak is what turned one planted fake `hermes_cli` into process-wide poison
  — a fake `<tmp>/hermes-agent` sitting at `sys.path[0]` is re-imported by every
  later consumer, even after an evictor cleaned `sys.modules`
  (`docs/audits/orch-waker-import-leak_t_2be3b04e.md` §3, §6 — the production
  half of that finding, which PR #270 deliberately left open).
* `dispatcher_wedge` has five bare `from hermes_cli import …` readers with no
  resolver of their own. Measured here: under the brew keg venv (`/opt/homebrew
  /bin/hscc`) `hermes_cli` resolves for them **only** because some earlier check
  in the same process leaked the tree — `_review_dispatch_config_state()`
  answers `True` after an autodown resolve and `None` (unknown) before one. Same
  order-dependence GAP-D-4 was filed for.

## What changed

One owner for the window: `hscc_daemon/hermes_import.py`.

`agent_tree_on_path()` (and its one-line form `import_hermes`) puts the resolved
agent dir on `sys.path` for the duration of the block and restores `sys.path` in
its `finally` — same entries, same order, on the exception path too. It is
refcounted per path under a lock because the daemon runs several of these
consumers on their own threads: an unrefcounted `finally` would let the first
window to finish pull the path out from under the other, and the loser's import
would raise `ModuleNotFoundError` (a flake nobody could attribute).

Routed through it: `autodown._load_kanban_db_or_default`, `kanban_live
._load_hermes_config`, `dispatcher_wedge`'s five hermes readers, `escalate_watcher
`'s two imports, `profile_roster._hermes_gate`. The `dispatcher_wedge` /
`escalate_watcher` sites are not scope creep — they are what the leak was
load-bearing for, and each one now opens its own window so it can no longer
depend on who ran first (the GAP-D-4 lesson, applied to the wedge gate).

Where a site *calls* into Hermes, the call is inside the window too: a loaded
package resolves its submodules through its own `__path__`, so the returned
module needs no path, but a fresh **top-level** import triggered at call time
(`import hermes_constants` from inside a hermes function) is the one thing that
still does.

## The deliberate test-expectation change

`test_kanban_live_caps.py::test_load_hermes_config_resolves_the_agent_tree_once`
asserted `str(agent) in sys.path` **after** the call — it pinned the leak. It is
now `test_load_hermes_config_resolves_the_agent_tree_then_closes_the_window` and
asserts the opposite (`str(agent) not in sys.path`, `sys.path == before`), plus
the non-vacuity half: the module it resolved has its `__file__` inside the
planted tree, and the gate serves nothing else, so the import could not have
succeeded without the window opening. The pre-fix "second call must not
duplicate" assertion is kept — the pre-fix code passed it, which is exactly why
the leak survived. `test_load_hermes_config_leaves_no_trace_when_the_import_fails`
adds the exception path.

`autodown` gains the matching guard: the test quotes the docstring phrase "only
for the duration of this one import" out of the live function, plants a fake
tree that records `sys.path` at import time (so the snapshot proves the window
was open during the import), and then requires `sys.path` to be byte-identical
to before the call.

## Measured, not reasoned

Two probes (scripts in the card thread) run the real chain against the real
`~/.hermes/hermes-agent` tree in the daemon's own interpreter. Pre-fix the agent
dir was `sys.path[0]` after a resolve; at this commit it is absent, and the chain
still works: `list_boards()` returned the operator's 23 boards,
`board_db_paths()` resolved every per-board DB, a `LIMIT 1` probe read 480 tasks
off the real board, and re-running `list_boards()` with `hermes_cli` evicted from
`sys.modules` returned 23 again. The probe also records every `find_spec` attempt
for any `hermes*` name made during those calls: **zero** — so no consumer
triggers a fresh top-level import at call time, which is the only way restoring
`sys.path` could have broken a later call. (That is also why the sites that *call*
into Hermes keep the call inside the window: it costs nothing and does not rely
on that measurement staying true.)

Five mutation probes, each on a copy of the tree with the original restored
before the next probe, provenance asserted inside every run, control green:

| mutation | what it restores | result |
|---|---|---|
| M1 | `finally` never restores (the pre-fix observable) | 11/11 new tests red |
| M2 | refcount dropped, naive `finally` | the two-thread test + the nested test red |
| M3 | `autodown` plain `sys.path.insert` alongside the window | the acceptance test red |
| M4 | wedge caps reader back to a bare import | its order-independence test red |
| M5 | wedge review-dispatch reader back to a bare import | its order test red |

And in the keg venv (the one `/opt/homebrew/bin/hscc` runs in) `_review_dispatch
_config_state()` now answers `True` in a fresh process, where pre-fix it answered
`None` until something leaked. `kanban_live._read_caps()` reports `(None, None)`
with an empty `reason` in both interpreters because `kanban.max_in_progress` is
genuinely unset on this box (measured through `hermes_cli.config.load_config()`),
not because the read failed — the distinction GAP-D-4 exists to keep.

