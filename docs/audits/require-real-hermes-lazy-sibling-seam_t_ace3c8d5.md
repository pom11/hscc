# Does `require_real_hermes` gate on "the name imported" too? — measured, three postures, no red (t_ace3c8d5)

**Verdict: measured-clean. No code change, no new probe.** The predicate weakness the
card names is real and reproducible on a thin interpreter, but none of the five
`require_real_hermes` / `skip_without_real_hermes` gated files reaches it, and the
reason is structural rather than lucky. Card item 2 is the outcome; item 3 has no
site to apply to. Per the card's own instruction, no speculative capability probe
was added to `hscc_daemon/tests/real_hermes.py`.

Measured at `origin/main` `8eb96984` (PR #555 squash, the shipped content of the
parent `t_dd814180`) in a task worktree, one pytest process per file, sequential.

## 1. The gated roster (the files the sweep had to cover)

`grep -rn "require_real_hermes\|skip_without_real_hermes" hscc_daemon/tests` gives
five gated files plus the owner module itself:

| file | gate site(s) |
| --- | --- |
| `hscc_daemon/tests/test_orch_waker.py` | :509 |
| `hscc_daemon/tests/test_autodown.py` | :857 |
| `hscc_daemon/tests/test_hermes_cli_import_isolation.py` | :159 |
| `hscc_daemon/tests/test_orch_waker_state_collision.py` | :120, :254, :306 |
| `hscc_daemon/tests/test_orch_waker_dead_pane.py` | :491 (the `deployed` fixture) |

## 2. The three postures, measured (not assumed)

| interpreter | py | `pytest` | `ruamel.yaml` | `pydantic` | `hermes_cli` resolvable | agent tree on `sys.path` |
| --- | --- | --- | --- | --- | --- | --- |
| `/private/tmp/hscc-ci-venv` (CI-faithful) | 3.13.12 | Y | **Y** | Y | **Y** (tree exists on this host) | **n** |
| `~/miniconda3/envs/p313` (sanctioned sibling) | 3.13.12 | Y | **n** | n | n (fails at `ruamel`) | n |
| `~/.hermes/hermes-agent/venv` (canonical) | 3.14.10 | Y | Y | Y | Y | Y |

The parent's premise correction holds and was re-verified: `p313` carries no
`ruamel` (`importlib.util.find_spec("ruamel.yaml")` *raises* there — the parent
package is absent), so its gate fails one step earlier and skips.

## 3. Sweep — file-alone, `-q -rs`, one process per file per interpreter

`<python> -m pytest -q -rs hscc_daemon/tests/<file>.py -p no:cacheprovider`

| file | CI-faithful thin | `p313` | canonical |
| --- | --- | --- | --- |
| `test_orch_waker.py` | **41 passed** rc 0 | 39 passed, 2 skipped | 41 passed |
| `test_autodown.py` | **204 passed** rc 0 | 202 passed, 2 skipped | 204 passed |
| `test_hermes_cli_import_isolation.py` | 7 passed, 1 skipped rc 0 | 6 passed, 2 skipped | 8 passed |
| `test_orch_waker_state_collision.py` | **7 passed** rc 0 | 1 passed, 6 skipped | 7 passed |
| `test_orch_waker_dead_pane.py` | **26 passed** rc 0 | 25 passed, 1 skipped | 26 passed |

**Errors: 0 on every file on every interpreter.** Tallies are identical on the
fenced and unfenced postures (§6), so the result is not a host-env artifact.

`-rs` reason strings, verbatim (the alone-vs-in-leg sweep diffs these, so they are
recorded rather than summarised):

* thin — the only skip line across all five files is unrelated to this seam:
  `test_hermes_cli_import_isolation.py:420: sys.prefix '/private/tmp/hscc-ci-venv'
  is not inside the agent tree '~/.hermes/hermes-agent' — no nested-venv
  overlap to check here`
* `p313` — the not-importable reason, from the owner module
  (`real_hermes.py:86`): `real hermes_cli.kanban_db is not importable in this
  interpreter: could not import hermes_cli.kanban_db from
  '~/.hermes/hermes-agent': No module named 'ruamel'` (the `p313` run reports it
  with the absolute prefix; two more sites quote the older
  `test_orch_waker.py:426: real hermes_cli not present in this environment` /
  `test_autodown.py:807` wording).
* canonical — zero skip lines across the five files.

**The finding the sweep table hides, and it cuts against the card's framing:** on
the CI-faithful venv the gate does **not** skip. `require_real_hermes()` returns the
real library there (the tree exists on this host, so `_load_kanban_db_or_default`
resolves it inside its bounded window), and all five gated sets *run*. They run and
pass. So the posture the card worried about — "the library imports but the calls
cannot" — is exactly the posture these tests execute under, and they still do not
error.

## 4. The seam is real (reproduced three ways on the thin interpreter)

A gate that returns the library is weaker than a gate that can run it, and the
weakness reproduces:

1. **Sibling-by-name, after the loader returned the real lib** (thin):
   `hermes_state_wal` → `ModuleNotFoundError: No module named 'hermes_state_wal'`;
   `hermes_state` → `ModuleNotFoundError: No module named 'hermes_state'`;
   `agent.delegation_context` → **OK** (`~/.hermes/hermes-agent/agent/
   delegation_context.py`). `sys.path` shows no agent-tree entry before or after the
   load, so the window is closed on purpose, as `test_hermes_cli_import_isolation.py`
   pins. `agent.*` resolves because the loader's own import already left `agent`,
   `agent.deadline`, `agent.jiter_preload` resident, and a loaded package resolves
   submodules through its own `__path__`.
   → **Premise correction to the card's list of three sites:** `:723`
   `agent.delegation_context` is *not* a thin failure site for any process that ran
   the gate; only `:645` (`hermes_state_wal`) and `:766` (`hermes_state`) are.
2. **The function that holds `:645`, called directly** (thin):
   `hermes_cli.kanban_db_connect._open_configured(db, fn)` →
   `ModuleNotFoundError: No module named 'hermes_state_wal'`, deepest frame
   `hermes_cli/kanban_db_connect.py:645 _open_configured`. On canonical the same
   call returns normally.
3. **A gated-shaped test that enters the door** (scratch control, unfenced):
   loader gate, then `lib.connect_closing()` — the call `autodown.py:3338` and
   `board_pin.py:245` make — ends
   `ModuleNotFoundError: No module named 'hermes_state'` at
   `hermes_cli/kanban_db_connect.py:766` on thin, and passes on canonical (which
   creates `kanban.db` and leaves `hermes_state` + `hermes_state_wal` resident).
   The parent's red, at the parent's line, produced by a test in the gated files'
   own shape.

## 5. Why nothing errors: the gated tests never enter `connect()`

Counting errors is not enough — green can mean "unreached" or "reached and fine".
The instrument is a `sys.addaudithook` import-attempt recorder
(`t_seam_trace.py`, scratch): verified to fire for imports that then *raise*, so it
counts attempts, and an audit hook cannot change an import.

| file (thin) | `agent.delegation_context` | `hermes_state` | `hermes_state_wal` | `hermes_cli.kanban_db_connect` (import) |
| --- | --- | --- | --- | --- |
| `test_orch_waker.py` | **0** | 0 | 0 | 30 |
| `test_autodown.py` | **0** | 0 | 0 | 5 |
| `test_hermes_cli_import_isolation.py` | **0** | 0 | 0 | 1 |
| `test_orch_waker_state_collision.py` | **0** | 0 | 0 | 6 |
| `test_orch_waker_dead_pane.py` | **0** | 0 | 0 | 10 |
| control (`connect_closing()`, unfenced) | 1 | 1 | 0 thin / 1 canonical | 1 |

`from agent.delegation_context import kanban_path_is_fenced` is the **first
unconditional** lazy import in `connect()` (`kanban_db_connect.py:723`) — it runs
before the fenced/branch decision and before `:766`. A zero there therefore means
`connect()` was never called in that process, independent of any fencing or path
question. The `hermes_cli.kanban_db_connect` import counts (30 / 5 / 1 / 6 / 10) show
the module *is* loaded — `autodown.py:901` binds `connect`/`connect_closing` off it —
so the zeros are about calls, not about the module being absent. The control row is
the proof of teeth: the same interpreter and the same hook go non-zero the moment a
test calls the door.

The same counter over the wider "door-adjacent" set on thin — `test_autodown_cli`,
`test_board_pin`, `test_daemon_ops`, `test_dispatcher_wedge`,
`test_dispatcher_wedge_default_board_pin`, `test_escalate_watcher`,
`test_kanban_blocked(_no_ansi)`, `test_kanban_cli(_no_ansi)`,
`test_kanban_live(_caps)`, `test_kill_switch`, `test_merge_queue` — is also all-zero
(16 files, all rc 0). `test_board_pin.py` on thin is `20 passed, 2 skipped, 0
attempts`: the parent's fix makes that pair skip *before* the door, and on canonical
the same file records 12 real `connect()` calls.

Why structurally, not by luck:

* The gated files' production surface is the waker (`check_orch_waker`,
  `waker_tick`, `board_enumeration`) and the loader/`kanban_check_state`. Board
  discovery goes through `autodown.board_db_paths` → hermes' `list_boards()`, which
  only reads directory metadata (`read_board_metadata`, `_dir_holds_board`,
  `kanban_db_boards.py:122-143`) — it never connects.
* `orch_waker` then opens what the seam reported **with its own read-only sqlite
  URIs** (`orch_waker.py:404`, `:584`, `:763`), deliberately: the module docstring
  at `orch_waker.py:35-46` says it does not reuse `kanban_db.connect_closing`
  because that opens read-write and runs schema-init/migrations. The one exception
  it documents, `BoardWakeClaim`, opens `mode=rw` on an explicit path — still not
  hermes' `connect()`.
* Every test that exercises a `connect_closing`-using module
  (`test_kanban_live`, `test_kanban_cli`, `test_dispatcher_wedge`, `test_board_pin`)
  injects a **fake lib** that supplies its own `connect_closing`, so hermes' door is
  never reached from those files either.

The production roster that *does* reach hermes' `connect()` is
`autodown.probe_kanban_activity` (`:3309`, door at `:3338`), `board_pin.open_board`
(`:245-253`), `escalate_watcher.py:131`, `kanban_blocked.py:253+`,
`scripts/dep_pr_watcher.py:195-202` and `hscc-project/flightdeck/core/kanban.py`.
All of those run in the **deployed** daemon, on an interpreter that carries the
agent tree on `sys.path` — where the siblings resolve (§4 case 2/3 canonical). The
thin interpreter is a *test* posture, and in it no test walks to the door.

## 6. Measurement hazards worth keeping (three, all of them produced a fake result)

1. **A tracer that plants at `pytest_configure` is blind exactly where it matters.**
   My first counter patched `hermes_cli.kanban_db_connect` at configure time. On the
   thin interpreter that module is not importable then — which *is* the seam — so the
   patch silently failed and printed `connect=0` for every file. Those zeros were
   unplanted, not measured. Caught only because a `print("planted=True")` at
   configure time is swallowed by `-q` capture and had to be re-reported from
   `pytest_sessionfinish`.
2. **Hooking `builtins.__import__` to patch later corrupts the loader.** v2 fired
   while `hermes_cli.kanban_db_connect` was still executing, and reading
   `kdc.connect` off a partially-initialised module raised and made the loader
   return `None`: `could not import hermes_cli.kanban_db from
   '~/.hermes/hermes-agent': partially initialized module
   'hermes_cli.kanban_db_connect' ... has no attribute 'connect' (most likely due to
   a circular import)`. It turned a green run into a skip. An audit hook replaces it
   and cannot do this.
3. **`HERMES_DELEGATED_CHILD_CONTEXT` masks the seam.** This worker's shell exports
   `HERMES_DELEGATED_CHILD_CONTEXT=~/.hermes`, and
   `kanban_path_is_fenced` (`agent/delegation_context.py:144-165`) returns True for
   any path under that root — which includes every pytest board root (`TMPDIR` here
   is inside `~/.hermes`) and every live board. `connect()` then takes the
   read-only early-return branch (`kanban_db_connect.py:724-732`), which never
   imports `hermes_state`/`hermes_state_wal`, so a test that *did* enter `connect()`
   would pass on thin for the wrong reason. §3/§5 were therefore re-measured with
   `-u HERMES_DELEGATED_CHILD_CONTEXT -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD`,
   which is the runner's posture; both postures give the same all-zero counters, and
   only the unfenced control produces the `:766` error. **Any future work on this
   seam must state which fencing posture it measured in.**

Transparency: the first (fenced) control run resolved `kanban_db_path()` to the
operator's live board `~/.hermes/kanban/boards/hscc/kanban.db` — because
the fencing marker pinned it — and opened it read-only for one `select 1`. Fencing
makes that branch read-only by construction (`mode=ro`, and it raises rather than
initialising), and the run created nothing under the tmp board root it was given.

## 7. What would turn this red (for the orchestrator; deliberately not built now)

The clean result is one call wide: the moment a `require_real_hermes`-gated test
touches a module path that calls hermes' `connect()` — a `connect_closing()` on the
real lib (the `probe_kanban_activity` / `open_board` shape), or an `autodown`
writer test that stops injecting a fake lib — it will error on any thin interpreter
that also has hermes' deps, in the unfenced posture. The fix idiom already exists
and shipped in the parent: probe the *call*, skip naming the exception, and keep the
reason string distinct from the not-importable one. Do not pre-import the siblings
and do not widen the window (the card's constraints, both still correct).

## 8. Repro

```bash
WT=~/dev/hscc/.worktrees/t_ace3c8d5
S=~/.hermes/profiles/coder/cache/scratch/t_ace3c8d5      # probes + both tracers + control
# sweep, one process per file, runner's (unfenced) posture
for f in test_orch_waker test_autodown test_hermes_cli_import_isolation \
         test_orch_waker_state_collision test_orch_waker_dead_pane; do
  (cd $WT && env -u HERMES_DELEGATED_CHILD_CONTEXT -u HERMES_KANBAN_DB \
     -u HERMES_KANBAN_BOARD /private/tmp/hscc-ci-venv/bin/python \
     -m pytest -q -rs hscc_daemon/tests/$f.py -p no:cacheprovider)
done
(cd $WT && PYTHONPATH=$S /private/tmp/hscc-ci-venv/bin/python $S/probe_lazy_siblings.py)
(cd $WT && PYTHONPATH=$S /private/tmp/hscc-ci-venv/bin/python $S/probe_open_configured.py)
(cd $WT && PYTHONPATH=$S env -u HERMES_DELEGATED_CHILD_CONTEXT -u HERMES_KANBAN_DB \
   /private/tmp/hscc-ci-venv/bin/python -m pytest -q -s $S/test_thin_door_control.py -p t_seam_trace)
```

Not touched, per the card's constraints: `hermes_import.agent_tree_on_path`'s
window, `scripts/run_tests.sh` (roster block, `OWNER_PROBES`, `owners`/`ALL_OWNERS`),
`hscc_daemon/tests/real_hermes.py`, and all product code. The diff is this record,
its `docs/README.md` index line, and the changelog fragment.
