# The `hscc_daemon` thin-sibling green is partly order-dependent: a file run ALONE can FAIL where the full leg passes

**Card:** t_572b4856 (opened from t_7c5f5051's review, which found and fixed one
instance of it). **Status: IN PROGRESS** — this page is being filled as the
measurement lands.

## Question

`hscc_daemon` prints `ALL GREEN` on the thin sibling
(`HSCC_TEST_PY=~/miniconda3/envs/p313/bin/python HSCC_TEST_LEGS=hscc_daemon`),
which the dual-interpreter stamp reads as "no regression". That reading assumes
the leg's answer is a property of the **interpreter**. If a file that passes
inside the leg FAILS when run alone under the same interpreter, the leg is
answering a question about test **order**.

The known mechanism: `hermes_cli` is not importable at all on the thin sibling
(no `__editable__.hermes_agent-0.0.0.pth`), but something during a full-leg run
leaves the real agent tree on `sys.path`, after which `hermes_cli` resolves for
the rest of the process. `conftest._hermetic_hermes_cli_imports`
(t_2be3b04e) purges *planted temp-root* paths and modules; the residue here is
the **real** agent tree, which that fixture does not and should not remove.

## Method

`scripts/run_tests.sh` runs a leg as ONE pytest process over the whole dir:

```
"$PY" -m pytest -q -rs "$ROOT/hscc_daemon/tests" -p no:cacheprovider
```

So the reference state is that command, and the comparison state is one process
per file:

```
"$PY" -m pytest -q -rs "$ROOT/hscc_daemon/tests/test_X.py" -p no:cacheprovider
```

Both sides run with `--junit-xml` (report-only; it does not touch execution) so
the diff is per-file **and** per-nodeid, not just a tail line. 131 test files.

## Findings

### 1. The reference run

The leg reference is the canonical runner's own command, not a re-implementation
of it (`run_tests.sh:402` is `env "${ENV_UNSET_ARGS[@]}" "$PY" -m pytest -q -rs
"$ROOT/$d/tests" -p no:cacheprovider "$@"`; extra argv is forwarded to pytest, so
the only thing added here is `--junitxml`):

```
HSCC_TEST_PY=~/miniconda3/envs/p313/bin/python HSCC_TEST_LEGS=hscc_daemon \
  bash scripts/run_tests.sh --junitxml=…            # 4024 passed, 23 skipped, rc0, ALL GREEN
```

The alone side mirrors that argv and that env scrub (`-u` for the five
`SUITE_ENV_UNSET` names at `scripts/run_tests.sh:348`), one process per file,
131 files:

```
env -u HERMES_DELEGATED_CHILD_CONTEXT -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD \
    -u HERMES_KANBAN_WORKSPACES_ROOT -u HERMES_KANBAN_ATTACHMENTS_ROOT \
  "$PY" -m pytest -q -rs <file> -p no:cacheprovider --junitxml=…
```

Both sides are compared **per nodeid** from junit xml, and separately **per skip
site + reason text**, because the stamp rule is read off `-rs` and a skip whose
*reason* changed is a different answer even when the count did not.

### 2. What differs — the enumeration

**Status differences: one file in 131.**

| file | in-leg | alone |
| --- | --- | --- |
| `hscc_daemon/tests/test_cli_orch_waker.py` | 18 passed | **collection ERROR**, `test_cli_orch_waker.py:28: import hscc as unified → ModuleNotFoundError: No module named 'hscc'`, rc=2 |

It is not a flake and it is not hermes: `test_cli_orch_waker.py` is the only file
in the leg that imports the **bare** name `hscc` (`:28` — measured: 1 file does,
26 files use `import hscc_daemon.hscc …`, e.g. `test_cli_orch_group.py:39`,
`test_cli_h_shortflag.py:54`, `test_cluster_cli_help.py:49`,
`test_bootstrap_verb.py:19`). Bare `hscc` resolves only because an
**earlier file in the same process** put a directory that holds a top-level
`hscc.py` on `sys.path` — `test_api_launchd.py:11` and `test_hermes_python.py:16`
do `sys.path.insert(0, <repo>/hscc_daemon)` at **module import time**, i.e. at
collection, and nothing takes it back. Both sort before `test_cli_orch_waker.py`,
so the leg is green and the file alone cannot even be imported.

**Skip-reason differences: two files, six sites — the counts match, the answer
does not.**

| site | in-leg reason | alone reason |
| --- | --- | --- |
| `test_dispatcher_wedge.py:855` | `could not import 'hermes_cli.kanban_db_dispatch': No module named 'utils'` | `…: No module named 'hermes_cli'` |
| `test_dispatcher_wedge.py:909` | `…: No module named 'utils'` | `…: No module named 'hermes_cli'` |
| `test_dispatcher_wedge.py:1197` | `…: No module named 'utils'` | `…: No module named 'toolsets'` |
| `test_dispatcher_wedge.py:1220` | `…: No module named 'utils'` | `…: No module named 'toolsets'` |
| `test_dispatcher_wedge.py:1250` | `…: No module named 'utils'` | `…: No module named 'toolsets'` |
| `test_profile_roster_multiplex_t1c545ebb.py:210` | **no skip at that site at all** — the gate at :210 passes in-leg and the test skips later, at `real_hermes.py:114` (`real hermes_cli.profiles.profile_is_standalone cannot run …: No module named 'hermes_yaml'`) | `could not import 'hermes_constants': No module named 'hermes_constants'` — the test never reaches the second gate |

That last row is the masking the card predicted, in its purest shape: one
`pytest.importorskip` answers **"not importable here"** alone and **"importable,
carry on"** in-leg. The case ends up skipped either way (a later gate stops it),
so the leg's `-rs` count hides it — but the *first* gate's verdict about this
interpreter flipped, and a case that cannot import hermes on its own is running
deeper into hermes code in-leg than it ever can alone.

Same interpreter, same file, same line: three different answers across the two
runs. The leg is answering a question about order.

### 3. The residue is not `sys.path`

The card's mechanism said the agent tree "gets left on `sys.path`". Measured,
that is **not what happens**, and the distinction decides the fix. A probe plugin
(`resid_probe.py`, loaded with `-p resid_probe` before any conftest) wrapped
`sys.path` in a logging `list` subclass and installed a `meta_path` finder that
fires once per first import of a hermes name, then ran the whole leg under thin:

```
real tree already on sys.path at plugin import: False        # thin has no editable .pth
sys.path events touching the real agent tree: 804            # every window open AND close
FINAL sys.path entries under the real tree: []               # <-- nothing left behind
FINAL hermes_cli in sys.modules: True  __path__=['<home>/.hermes/hermes-agent/hermes_cli']
FINAL sys.modules entries rooted in the real tree: 14
    hermes_cli, hermes_cli.profiles, hermes_cli.profiles_cache, hermes_cli.profiles_config,
    hermes_cli.kanban_workflow, hermes_cli.archive_safe, hermes_cli.config_backend,
    hermes_cli.home_data_layout, hermes_cli.profiles_remove,
    hermes_constants, toolsets, agent, agent.file_safety, agent.jiter_preload
```

`hermes_import.agent_tree_on_path` does exactly what it documents: 804 events,
zero residue on the path. What survives is **`sys.modules`**: anything a window
imported successfully outlives the window. Its own docstring says the quiet part
— *"once `hermes_cli` is in `sys.modules` its submodules resolve through the
parent's `__path__` (no `sys.path` needed)"* — and that is the whole effect: the
resident parent package holds `__path__` pointing into the agent tree, so from
then on `import hermes_cli.<anything>` resolves with the tree nowhere on
`sys.path`. A failed submodule import is enough to produce it: `from hermes_cli
import kanban_db` on a thin interpreter imports the **parent** successfully,
fails on the child, and leaves the parent resident.

The opener, from the probe's own stacks — not one call, any call that opens a
window. The first `hermes_cli` lookup in the leg:

```
[import 237] name=hermes_cli
  hscc_daemon/tests/test_autodown.py:793  in test_resolves_from_hermes_agent_path
      kb1 = ad._load_kanban_db_or_default()
  hscc_daemon/autodown.py:883  from hermes_cli import kanban_db
```

and the same shape at `dispatcher_wedge.py:350` / `:384`
(`_review_dispatch_config_state`, which is what makes the wedge file's *own*
skips answer differently at :855 and at :1197 **inside one file**), plus
`require_real_hermes()` → `autodown._load_kanban_db_or_default()` for every
gated case. The first `hermes_constants` lookup is the autouse
`_hermetic_kanban_root` (`conftest.py:496`), which fails on thin — that one is
innocent; the resident copy comes from a window opened by one of the calls above.

So `conftest._hermetic_hermes_cli_imports` (t_2be3b04e) is not "missing the real
tree": it purges **planted temp-root** modules and paths, and there is nothing
for it to purge on `sys.path`. Its gap is different and narrower — it snapshots
the `hermes_cli*` slice at test start and restores it **only when the slice is
poisoned by a plant**. A slice that merely GREW during the test (a real package
imported inside a window) is never taken back, so it is handed to every later
test in the process.

### 4. What the stamp may claim (pre-fix)

Pre-fix the thin `hscc_daemon` green was a statement about *this file order*, not
about the interpreter, for the six sites above and for `test_cli_orch_waker.py`'s
18 cases. The failure mode is silent in both directions: a case can be
skipped-alone/run-in-leg (the `hermes_constants` row) and a case can pass in-leg
only because a previous file made an import answer "found" (the wedge case
t_7c5f5051 fixed).

### 5. The fix

Two independent residues, two small fixes, both pinned at runtime.

**(a) `sys.modules` residue — the conftest fixture's snapshot was one-way.**
`_hermetic_hermes_cli_imports` (t_2be3b04e) snapshots the `hermes_cli*` slice at
test start and restores it **only when a plant poisoned it**. A slice that merely
*grew* — a real package imported inside a window — was never taken back, so it was
handed to every later test in the process. The fixture now restores symmetrically:
each test gets back exactly the cache it found, via a second rule in
`hermes_cli_plant.py` that recognises a residue module by where its own file lives
(`agent_tree_root` / `is_agent_tree_path` / `resident_under_agent_tree` /
`restore_agent_tree_cache`). It sits *beside* the plant rule rather than inside
it, because the plant rule is deliberately narrow — "a temp root is a plant, the
real tree is never one" — and folding the two together would make a real module
look like a fake.

Cost is the reason the rule is written the way it is. The first version called
`os.path.realpath` on every entry of `sys.modules`, twice per test: on a
1000-test nested run that is ~10k `realpath` calls per test and it took the
suite from 21 s to "still spinning four minutes later" (measured before killing
it). The shipped rule puts a plain string prefix test in front of every syscall
and caches the verdict per module *object* (`_VERDICT_CACHE`, identity-keyed — a
module's `__file__` does not change), which brings the fixture back to free:
`test_autodown.py` 10.44 s → 10.45 s, `test_no_live_hscc_leak.py`'s nested
11-file run 21.36 s → 21.27 s, under thin.

**(b) The bare-`hscc` residue — fixed at the consumer, not at the leaker.**
`test_cli_orch_waker.py` now imports `hscc_daemon.hscc`, the spelling the other
26 files in the leg use, so the file resolves in any order. The two
module-level `sys.path.insert` sites that were load-bearing for it
(`test_api_launchd.py:11`, `test_hermes_python.py:16`) are left alone: they are
how those files reach their own bare module names, the same pattern
`scripts/tests` uses and `run_tests.sh:170-181` defends as the reason a leg needs
its own process. What the pin below does is make it impossible for a *new* file
to lean on that insert unnoticed — the sweep in this audit is how it was caught,
and the fix removes the only case that was leaning.

**Runtime pin (the "runtime half" the card asked about).** The static
CLI-seam rule from t_7c5f5051 cannot see this: nothing in the offending file's
source changed between green and red, only what ran before it. So the coverage
goes in `test_hermes_cli_import_isolation.py`, next to the planted-tree pair, as
`test_a_closed_import_window_leaves_no_residue_for_the_next_file` — a nested
two-file pytest run in which file A opens a window **through the production
loader** (`autodown._load_kanban_db_or_default`) and records the cache it found
vs the cache it ended with, and file B must see exactly what A found. The marker
is the anti-vacuity: if A's window imported nothing, the pin fails instead of
passing quietly. Measured: **RED on the pre-fix tree on both interpreters**
(thin `p313` and canonical venv — file B's assert fires, `hermes_cli` reached the
next file), GREEN after the fix on both.

The static rule should stay static and the answer to "should it grow a runtime
half" is: it grew a *separate* runtime half, in the file that already owns import
isolation, because the seam rule's subject is "does this case reach the CLI
without a gate" and this one's is "does the process still look like it did before
the case ran". Two different questions, one file family, no shared predicate.


