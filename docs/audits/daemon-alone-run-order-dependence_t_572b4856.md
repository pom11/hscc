# The `hscc_daemon` thin-sibling green is partly order-dependent: a file run ALONE can FAIL where the full leg passes

**Card:** t_572b4856 (opened from t_7c5f5051's review, which found and fixed one
instance of it). **Status: LOCATED and FIXED** — the enumeration the card asked
for is §2, the mechanism (which is *not* the one the card hypothesised) is §3,
the fix and its runtime pin are §5, §6 is the before/after on both interpreters,
§7 is the second order dependence the sweep then found *inside this fix*, and §8
is the final 131-files × 2-interpreters agreement.

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

This first enumeration was run under the thin sibling; the same sweep on the
canonical interpreter is §7, and it found five more files (of a different kind,
in the fix written for what is below). The re-run after both fixes, on both
interpreters, is §8.

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

The static rule should stay static, and the answer to "should it grow a runtime
half" is: it grew a *separate* runtime half, in the file that already owns import
isolation, because the seam rule's subject is "does this case reach the CLI
without a gate" and this one's is "does the process still look like it did before
the case ran". Two different questions, one file family, no shared predicate.

### 6. Verification

`-rs` reason text, thin `p313`, the same six sites, before and after. The point is
not "green" — it is that the leg and the alone run now print **the same answer**:

| site | pre-fix in-leg | pre-fix alone | post-fix (in-leg == alone) |
| --- | --- | --- | --- |
| `test_dispatcher_wedge.py:855` | `No module named 'utils'` | `No module named 'hermes_cli'` | `No module named 'hermes_cli'` |
| `:909` | `No module named 'utils'` | `No module named 'hermes_cli'` | `No module named 'hermes_cli'` |
| `:1197` | `No module named 'utils'` | `No module named 'toolsets'` | `No module named 'hermes_cli'` |
| `:1220` | `No module named 'utils'` | `No module named 'toolsets'` | `No module named 'hermes_cli'` |
| `:1250` | `No module named 'utils'` | `No module named 'toolsets'` | `No module named 'hermes_cli'` |
| `test_profile_roster_multiplex_t1c545ebb.py:210` | no skip there — the gate passed and the case died later at `real_hermes.py:114` (`hermes_yaml`) | `No module named 'hermes_constants'` | `No module named 'hermes_constants'` |

| run | pre-fix | post-fix |
| --- | --- | --- |
| `hscc_daemon` leg, thin `p313` (3.13.12) | 4024 passed, 23 skipped, rc0, 133.95 s | **4026 passed, 24 skipped, rc0, 132.87 s** (2 pins added; +1 passes, +1 skips honestly on the sibling — wall time unchanged) |
| `hscc_daemon` leg, canonical venv (3.14.10) | 4046 passed, 1 skipped, rc0 | **4049 passed, 1 skipped, rc0, 164.07 s** (both new pins RUN here) |
| per-file alone vs in-leg, all 7 suspect files | 1 collection error + 6 reason mismatches | **0 and 0** |
| `test_cli_orch_waker.py` alone | collection ERROR (`No module named 'hscc'`, rc=2) | 18 passed |

Runtime pin: RED on the pre-fix tree under **both** interpreters (thin `p313`
3.13.12 and canonical venv 3.14.10; file B's assert fires — `hermes_cli` reached
the next file), GREEN after the fix on both.

### 7. The canonical sweep found a second order dependence — in the fix itself

The thin sweep said "clean", so the honest move was to run the same sweep on the
canonical interpreter rather than infer it. It exited **1**: five files, **72
tests**, that pass inside the leg and FAIL alone —

| file | in-leg | alone (pre-fix-of-round-2) |
| --- | --- | --- |
| `test_unified_cli.py` | 98 passed | 39 failed |
| `test_toplevel_cli_help_f5.py` | 60 passed | 16 failed |
| `test_daemon_singleton.py` | 50 passed | 5 failed |
| `test_cli.py` | 22 passed | 11 failed |
| `test_chat_roundtrip_standalone.py` | 11 passed | 1 failed |

and the traceback was not about hermes at all:

```text
rich/panel.py:237: in __rich_console__ title_text = align_text(
rich/panel.py:182: in align_text return Text.assemble(
E   TypeError: Text.append() takes from 2 to 3 positional arguments but 24 were given
```

Two `rich.Text` classes in one process. Cause: **my own new rule**. The agent tree
on the canonical runner is `~/.hermes/hermes-agent`, and hermes' venv lives *inside
it* — `~/.hermes/hermes-agent/venv/lib/python3.14/site-packages`. A rule that
classifies residue by "is its `__file__` under the agent root" therefore called
`rich`, `typer`, `pydantic` and `_pytest` itself hermes residue and evicted them
from `sys.modules` after every test, exactly as it was supposed to evict a leaked
`hermes_cli`. Verified against the pre-fix tree: `test_daemon_singleton.py` alone
is 50 passed on `origin/main`, 5 failed on the branch, and the failure is gone when
the classifier refuses `sys.prefix`.

Nothing about this is visible from the leg: it prints `ALL GREEN` with the bug in
it on both interpreters. The bug in the fix for "a leg can be green while a file
fails alone" was caught by the one thing the card asked for — the sweep, run on
the *other* interpreter too. Two lessons kept together:

* a prefix under a root is not ownership: the tree that *contains* the
  interpreter's installation must be excluded, and that exclusion is
  `sys.prefix`/`base_prefix`/`site.getsitepackages()`, not a name list.
* a test that patches a module global read by an autouse fixture during *its own*
  teardown poisons the next test's setup — the first cut of this pin did that and
  produced `Could not obtain a node for scope "Scope.Session"`. The classifier now
  takes `root`/`interp_roots` as arguments instead.

Pins: `test_the_agent_tree_rule_excludes_a_venv_nested_in_the_tree` (synthetic
`<tree>/venv/lib/pythonX/site-packages`; the same call with `interp_roots=()`
asserts the venv path *does* read as residue, so the pin contains its own mutation
probe) and `test_installed_packages_are_never_reported_as_residue` (live: no module
resident under `sys.prefix` is ever reported; explicitly `skip`s on a sibling whose
prefix is outside the tree instead of passing quietly there).

### 8. Final state: alone == in-leg on both interpreters

After both fixes, the full 131-file sweep re-run against a fresh leg on each
interpreter:

```text
CANONICAL post-fix (4049p/1s) full 131-file sweep:   files whose ALONE run differs: 0   exit=0
THIN      post-fix (4026p/24s) full 131-file sweep:  files whose ALONE run differs: 0   exit=0
```

No collection errors, no non-zero exit, no status difference, no skip-reason
difference, on either interpreter.

### 9. The sweep is now a gate, not a document (t_6cd17e1c)

Both findings above had the same shape: the leg printed `ALL GREEN`, and only a
per-file alone run could see the problem. A measurement nobody re-runs is a
write-up, so the sweep is now routed:

* **`scripts/alone_sweep_gate.sh`** — the command a reviewer runs. It sweeps the
  leg with the audit's own tooling (it contains **no** pytest invocation of its
  own: the argv, the scrub and the enumeration stay single-sourced in
  `alone_sweep.sh`, because a second implementation is how a gate quietly starts
  measuring something adjacent), for both stamp interpreters, sequentially, with
  a verdict written per interpreter and a lock that refuses a concurrent pass —
  two pytest processes over one checkout trip the port/lock fixtures, and a flake
  from that reads exactly like an order effect. Exit codes distinguish the three
  outcomes the CI header insists on: `0` agreement, `1` a file answers alone
  differently (or its leg is red), `2` **not measured** (no interpreter, broken
  wiring, lock held) — never green because nothing ran.
* **`scripts/install_alone_sweep_gate.sh`** — registers it as a daily Hermes cron
  job (`hscc-alone-sweep-gate`, `30 4 * * *`, `--no-agent`). The installed job is
  a generated one-line wrapper that bakes the repo path, the interpreter list and
  the report root it resolved from the installing shell, then `exec`s the gate from
  git — so the nightly measures committed code, a copied script cannot go stale,
  and nothing is left to resolve at 04:30 under a bound profile home that is not
  the operator's `$HOME`. It refuses to register a job it knows cannot measure
  anything (no gate, no sweep tooling, or no visible interpreter) rather than
  installing a failure that nobody notices until a red goes unseen.
* **`scripts/tests/test_alone_sweep_gate.py`** — the half that stays in the per-PR
  lane, at milliseconds instead of minutes.

**Why scheduled and not per-PR.** Measured on this host (the same two interpreters
the stamp uses): the full gate — both interpreters, each with a fresh leg — takes
**10m23s**: leg 163 s + 131 alone sweeps 166 s on the canonical venv, leg 135 s +
sweep on the thin sibling. So ~4x the leg's own wall time, on a macOS runner
(`hscc_daemon` is macOS-only — `select.kqueue` at class scope dies collecting on
ubuntu; `leg_select.MACOS_LEGS`), which is CI's smallest pool at ~10x the minute
cost. The wall time is not by itself disqualifying; what settles it is that 131
processes are 131 chances for an environment hiccup to become a merge blocker, over
a property that only moves when the daemon or the harness moves. Per-PR it is also
slower than the suite it guards, which `.github/workflows/pytest-legs.yml` names as
the criterion for a gate people click through — worse than no gate. The two cheaper
shapes were rejected for measurable reasons:

* a **subset sweep inside the `scripts` leg** (option (b)) is red-by-environment
  there — that leg runs on ubuntu, where the daemon leg cannot even be collected,
  so the guard would `skip()` and report green while measuring nothing, which is
  the exact hole this card closes. Its reference side is a whole daemon leg
  (~4 min) anyway, i.e. more than the entire `scripts` leg today. And the at-risk
  subset is precisely what a bad fix changes: §7's regression surfaced in files no
  static rule would have flagged.
* the **full sweep in the enforce lane** (option (c)) buys minutes of lateness on
  every PR touching the daemon for a property that only changes when *something in
  the daemon or the harness* changes.

What the CI lane keeps instead is the guarantee that the scheduled gate can still
go red: the diff tool's teeth are re-measured on fixtures (status change,
collection error, a file absent from the leg, and the **skip-reason change whose
tally matches** — the §2 shape, invisible to junit), the alone side is pinned to
the runner's argv and `SUITE_ENV_UNSET` list (drift there makes every later
comparison a comparison of two different environments), the enumeration is pinned
to the dir-wide glob and to the real file count, and the gate is pinned to fail
closed on all three of its not-measured paths.

**Non-vacuity, measured rather than asserted.** A one-file mutation —
`import hscc` at module scope in `test_watchdog_gateway_only_breaker_b0811ce0.py`
(the §2 shape: it resolves in-leg only because `test_api_launchd.py:11` leaves
`<repo>/hscc_daemon` on `sys.path`) — run through the gate on both interpreters.
The legs stayed green on both (canonical `4049 passed, 1 skipped`; thin
`4026 passed, 24 skipped`), and each sweep named exactly one file:

```text
test_watchdog_gateway_only_breaker_b0811ce0: in-leg (15, 0, 0, 0) | alone (0, 0, 0, 1) | rc=2
```

→ `GATE RED`, gate exit 1, on **both** interpreters; the mutant reverted, the same
gate on the reverted tree is `GATE GREEN` / exit 0 with 131/131 alone runs at rc 0
on both. The cheapest re-check for a reviewer is the 20-second probe pair rather
than a sweep: `pytest -q test_api_launchd.py test_watchdog_gateway_only_breaker_b0811ce0.py`
answers `52 passed` while `pytest -q` on the mutant alone answers
`ModuleNotFoundError: No module named 'hscc'` and rc 2 — the same two commands on
both interpreters.

**The two module-level `sys.path.insert` sites stay.** `test_api_launchd.py:11`
and `test_hermes_python.py:16` insert `<repo>/hscc_daemon` and never remove it —
that is the leak that made the bare `import hscc` in §2 resolve for a *later*
file. They are not removable at zero cost: each is what lets its own file import
`api_launchd` / `hermes_python` bare, and the usual replacement (do the insert in
`conftest.py`) leaks it to **every** file in the process instead of only the ones
collected after. So the decision is recorded as **the gate is the guard**: what
stops a new file starting to depend on that residue is the nightly alone sweep,
and the pin that stops the *gate itself* from quietly shrinking is
`test_sweep_enumerates_the_whole_leg_dir`.

## What the stamp may claim now

The thin leg's `-rs` output is now a property of the interpreter, not of the file
order, for every file measured: 131 files swept alone on both interpreters against
a fresh leg on each, zero differences (§8). Two honest limits:

* The sweep compares the **leg's** order against **alone**. A third order (some
  other subset of files in one process) is not enumerated; what makes it safe is
  that the per-test guarantee is now unconditional, not that this pair of orders
  agrees.
* The residue rule recognises a module by a **path prefix** under the tree
  resolved at fixture setup, exactly as the plant rule recognises a plant by a
  temp-root prefix. A test that points `HERMES_AGENT_PATH` at a real hermes copy
  outside the temp root and outside the default path would be outside both rules.

## Follow-ups (not done here)

* ~~The two module-level `sys.path.insert(0, <repo>/hscc_daemon)` sites
  (`test_api_launchd.py:11`, `test_hermes_python.py:16`) still leak for the rest
  of the process.~~ **Done in t_6cd17e1c (§9).** The inserts stay — each is what
  lets its own file import `api_launchd` / `hermes_python` bare — and what stops a
  new file depending on the residue is the sweep, now routed as a daily job:
  `scripts/alone_sweep_gate.sh` (+ `scripts/install_alone_sweep_gate.sh`), with
  the cheap always-on half in `scripts/tests/test_alone_sweep_gate.py`. It has
  already paid for itself twice — once for the leg (§2), once for a bug in the
  code written to fix the leg (§7) — and in both cases the leg itself was green,
  so nothing else running today would have seen either.
* `hermes_import.agent_tree_on_path`'s `finally` has a documented "bookkeeping was
  reset under us" branch (`hermes_import.py:109-110`) that leaves the path in
  place if a test replaces `_WINDOWS` mid-window. Not observed in 804 events over
  this leg; it is the one remaining way a real path entry could survive.

## Reproduce

The sweep and the diff are shipped with this audit —
`docs/audits/support/t_572b4856/alone_sweep.sh` (and its `diff_alone_vs_leg.py`)
— so the recipe is one command per interpreter:

```sh
docs/audits/support/t_572b4856/alone_sweep.sh ~/miniconda3/envs/p313/bin/python hscc_daemon
docs/audits/support/t_572b4856/alone_sweep.sh ~/.hermes/hermes-agent/venv/bin/python hscc_daemon
```

It runs the leg once through `scripts/run_tests.sh` (so the reference gets the
runner's own environment, including the `SUITE_ENV_UNSET` scrub) with
`--junitxml` added — extra argv is forwarded to pytest by design — then runs every
test file alone with the same argv and the same scrub mirrored explicitly, so the
only variable is the file set. Exit 0 means every file answers alone exactly as it
does inside the leg.

The diff has teeth, measured rather than asserted: pointed at the pre-fix sweep
directory it exits 1 and names the three files (the `hscc` collection error, the
five `test_dispatcher_wedge.py` reason mismatches, and
`test_profile_roster_multiplex_t1c545ebb.py` — whose *tally* matches on both
sides, so only the skip-reason half of the diff can see it); pointed at the
post-fix directory it exits 0.




