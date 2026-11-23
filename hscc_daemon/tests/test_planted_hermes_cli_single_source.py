"""Structural guard — exactly ONE "is this a planted fake ``hermes_cli``?" rule.

t_b62da70c's own acceptance criterion is "one predicate, no duplication", and a
fix that only moves text is not closed until re-copying it fails. This suite
already paid for the missing check twice: PR #270 folded the rule into conftest
after one file's planted fake decided a later file's board enumeration, and two
private copies of the same rule survived that fix (``test_autodown``
matched its own tmp dir plus a substring of the pytest tmp-dir marker;
``test_board_pin`` matched the marker alone). Same posture as
``scripts/tests/test_pane_matcher_single_source.py`` and
``test_no_real_addresses_committed.py``: a sweep judges every tracked file and
passes only while the predicate lives in its ONE owner.

Owner: ``hscc_daemon/tests/hermes_cli_plant.py``.

What counts as a copy:

  * a ``def`` of any of the owner's function names outside the owner — the only
    shape that can drift into a second rule;
  * the pytest tmp-dir marker literal anywhere else in the package — a
    substring match on the tmp-dir path IS the private-copy shape the two
    survivors used, and nothing else in ``hscc_daemon`` needs it;
  * the macOS private temp root literal, the other tell of the temp-root RULE
    (an ordinary temp-file test uses ``tempfile.gettempdir()`` and never names
    it).

Fragments are ASSEMBLED AT RUNTIME (the ``address_guard`` / ``ledger_scrub``
idiom): this file sweeps for those literals, so carrying one contiguously would
make the sweeper its own first hit.

Scope decisions (stated, not hidden):

  * The sweep is ``hscc_daemon/**/*.py`` — the package whose harness plants
    these fakes and whose conftest owns the autouse guarantee.
  * ``hscc-cluster/tests/sparkrun_fence.py::_under_pytest_tmp`` also names the
    tmp-dir marker and is deliberately NOT folded in: it answers a different
    question (is this ``sparkrun`` argv a per-test fake?) and requires a SECOND
    marker (a ``test_`` component) precisely so the shared pytest root still
    counts as the machine's real CLI. Making it call ``is_planted_path`` would
    widen the fence and start fencing the live CLI. Recorded here so the next
    reader does not "fix" it.
  * A test may of course *build* a plant — that is constructing a path under a
    temp root, which is what the owner's own helpers and
    ``test_hermes_cli_import_isolation._temp_planted_module`` do. What is banned
    is a second rule for deciding it.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

OWNER = "hscc_daemon/tests/hermes_cli_plant.py"
SWEEP_ROOT = "hscc_daemon"

#: The owner's API. A ``def`` of one of these names anywhere else is a fork.
OWNER_FUNCTIONS = ("is_planted_path", "hermes_cli_names", "poison_reason",
                   "purge_planted", "evict_planted", "restore_planted")

#: Every file that evicts a plant must obtain the rule by import.
SITES = ("hscc_daemon/tests/conftest.py",
         "hscc_daemon/tests/test_autodown.py",
         "hscc_daemon/tests/test_board_pin.py",
         # the nested probe file B, which used to re-derive the temp-root rule
         "hscc_daemon/tests/test_hermes_cli_import_isolation.py")


def _frag(*parts):
    """Assemble a swept literal at runtime — see the module docstring."""
    return "".join(parts)


TMP_DIR_MARKER = _frag("pytest-", "of-")
PRIVATE_TEMP_ROOT = _frag("/private", "/var/folders")


def _tracked_py_files():
    out = subprocess.run(["git", "ls-files", "-z", "--", SWEEP_ROOT],
                         cwd=str(REPO), capture_output=True, text=True,
                         check=True).stdout
    files = sorted(p for p in out.split("\0") if p.endswith(".py"))
    assert OWNER in files, (
        f"{OWNER} is not tracked — the guard would silently sweep nothing")
    return files


def test_the_predicate_is_defined_in_exactly_one_file():
    """No second file may ``def`` any part of the planted-fake predicate."""
    offenders = []
    for rel in _tracked_py_files():
        if rel == OWNER:
            continue
        for n, line in enumerate((REPO / rel).read_text(errors="ignore")
                                 .splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("def "):
                continue
            name = stripped[len("def "):].split("(")[0].strip()
            if name in OWNER_FUNCTIONS:
                offenders.append(f"{rel}:{n}: {stripped}")
    assert not offenders, (
        "the planted-hermes_cli predicate has more than one definition — it "
        "lives in " + OWNER + ":\n" + "\n".join(offenders))


def test_no_other_file_carries_the_rule_literals():
    """The literals are the rule's fingerprints; a copy names at least one.

    ``TMP_DIR_MARKER`` is what both private copies substring-matched, and
    ``PRIVATE_TEMP_ROOT`` is the second half of the one temp-root tuple — an
    ordinary test needs neither.
    """
    hits = []
    for rel in _tracked_py_files():
        if rel == OWNER:
            continue
        text = (REPO / rel).read_text(errors="ignore")
        for label, frag in (("pytest tmp-dir marker", TMP_DIR_MARKER),
                            ("macOS private temp root", PRIVATE_TEMP_ROOT)):
            if frag in text:
                hits.append(f"{rel}: {label}")
    assert not hits, (
        "a competing copy of the planted-hermes_cli rule was found (call "
        + OWNER + " instead):\n" + "\n".join(hits))


def test_every_site_that_evicts_a_plant_imports_the_owner():
    """Textual, so it holds whether or not a given file is collected here."""
    for rel in SITES:
        text = (REPO / rel).read_text(errors="ignore")
        assert "hermes_cli_plant import" in text, (
            f"{rel} handles a planted hermes_cli but does not import the one "
            f"predicate from {OWNER}")


def test_conftest_rebinds_the_owner_objects_rather_than_copying_them():
    """The acceptance claim is ONE predicate, not two that look alike.

    conftest must not be *imported* to check this (a second import re-runs its
    sparkrun-fence loader), so the already-loaded module object is looked up by
    name — the same trick ``test_hermes_cli_import_isolation._conftest`` uses.
    """
    from hscc_daemon.tests import hermes_cli_plant

    cf = None
    for name in ("hscc_daemon.tests.conftest", "conftest"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "_hermes_cli_poison_reason"):
            cf = mod
            break
    if cf is None:  # pragma: no cover — only reachable outside a pytest run
        pytest.skip("conftest is not loaded under a known name in this run")
    assert cf is not None, "unreachable: pytest.skip does not return"

    # Identity, not equality: an alias proves the fixture and the predicate are
    # the same code, a re-implementation would not be.
    assert cf._is_planted_path is hermes_cli_plant.is_planted_path
    assert cf._hermes_cli_names is hermes_cli_plant.hermes_cli_names
    assert cf._hermes_cli_poison_reason is hermes_cli_plant.poison_reason
    assert cf._purge_planted_hermes_cli is hermes_cli_plant.purge_planted
    assert cf._evict_planted_hermes_cli is hermes_cli_plant.evict_planted


def test_importing_the_owner_cannot_disturb_the_running_suite():
    """Why the rule lives in a module and not in conftest.

    A test module cannot ``import conftest`` to reach the predicate: a second
    import of conftest re-runs its module-level sparkrun-fence loader and swaps
    the live fence out from under the suite. The shared module must therefore be
    genuinely inert on import — asserted in a FRESH interpreter, because the
    point of the card is that import side effects are what break a leg.
    """
    script = (
        "import sys\n"
        "path_before = list(sys.path)\n"
        "mods_before = set(sys.modules)\n"
        "import hscc_daemon.tests.hermes_cli_plant as plant\n"
        "new = sorted(set(sys.modules) - mods_before)\n"
        "assert list(sys.path) == path_before, (\n"
        "    'importing the predicate changed sys.path: %r' % sys.path)\n"
        "bad = [m for m in new if m.startswith('hermes_cli')\n"
        "       or 'conftest' in m or 'sparkrun_fence' in m]\n"
        "assert not bad, 'importing the predicate pulled in %r' % (bad,)\n"
        "assert plant.is_planted_path('/nope/x.py') is False\n"
        "print('INERT-IMPORT')\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(REPO),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "INERT-IMPORT" in proc.stdout, proc.stdout


# --------------------------------------------------------------------------- #
# 6 — every case that reaches the REAL hermes_cli carries a seam (t_7c5f5051)
# --------------------------------------------------------------------------- #

#: The test dir this rule polices. Test POSTURE is the subject (a case that
#: reaches the real package FAILS, not skips, on a thin interpreter); the
#: package's own production imports are deliberately out of scope, exactly as
#: in ``hscc-api``'s twin rule.
TEST_LEG = "hscc_daemon/tests"

#: The real CLI package — ASSEMBLED, because this file sweeps for it.
REAL_CLI = _frag("hermes", "_cli")

#: This guard, and the leg's two owner modules, are not judged by the rule.
SELF = TEST_LEG + "/" + Path(__file__).name
EXCLUDED = (OWNER, TEST_LEG + "/" + _frag("real_", "hermes") + ".py", SELF)

#: Context managers this leg uses to SIMULATE a ``hermes_cli`` that is absent
#: or planted. An import inside one is not a reach for the real package: it is
#: either an assertion of absence (``pytest.raises(ModuleNotFoundError)``) or a
#: resolution into a plant (``_HermesCliGate`` / ``_Gate`` /
#: ``hermes_import.agent_tree_on_path``). Measured: every one of these windows
#: is what makes the leg's four plant/negative-control cases interpretable.
WINDOW_MARKERS = (_frag("rais", "es"), _frag("_Hermes", "CliGate"),
                  _frag("_", "Gate"), _frag("agent_tree_on_", "path"))


def _swept_test_files():
    out = subprocess.run(["git", "ls-files", "-z", "--", TEST_LEG],
                         cwd=str(REPO), capture_output=True, text=True,
                         check=True).stdout
    files = [f for f in sorted(out.split("\0")) if f.endswith(".py")]
    files = [f for f in files if f not in EXCLUDED]
    assert files, f"nothing tracked under {TEST_LEG} — the sweep sweeps nothing"
    return files


def _is_cli(mod):
    return mod == REAL_CLI or mod.startswith(REAL_CLI + ".")


def _cli_imports(tree):
    """``(node, module)`` for every import rooted at the real CLI package."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _is_cli(node.module or ""):
            yield node, node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _is_cli(alias.name):
                    yield node, alias.name


def _parents(tree):
    par = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            par[child] = node
    return par


def _scope_of(node, par, tree):
    """The innermost function an import sits in (``(None, tree)`` at module level).

    Per-CASE, not per-file, and that is the whole point of this rule being
    separate from ``hscc-api``'s: measured on the tree that carried the bug,
    ``hscc-api``'s per-file seam predicate CREDITS
    ``test_dispatcher_wedge.py`` — the same ``sys.modules`` write that shadows
    the submodule satisfies it — while the case three lines above it fails on a
    thin interpreter. A file-level seam hides exactly the bug it is for.
    """
    cur = node
    while cur in par:
        nxt = par[cur]
        if isinstance(nxt, ast.FunctionDef):
            return nxt, nxt
        cur = nxt
    return None, tree


def _callee_name(func):
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _cli_literal(node):
    """A string literal in *node* that IS the CLI package or one of its parts."""
    return any(isinstance(c, ast.Constant) and isinstance(c.value, str)
               and _is_cli(c.value) for c in ast.walk(node))


def _calls_skip(node):
    return any(isinstance(n, ast.Call) and _callee_name(n.func) == "skip"
               for n in ast.walk(node))


def _simulated(node, par, scope_node):
    """Whether the import sits inside a declared simulation window.

    Stops at the scope boundary so an unrelated window in a sibling case cannot
    excuse this one.
    """
    cur = par.get(node)
    while cur is not None and cur is not scope_node:
        if isinstance(cur, ast.With):
            ctx = " ".join(ast.unparse(item.context_expr) for item in cur.items)
            if any(marker in ctx for marker in WINDOW_MARKERS):
                return True
        if isinstance(cur, ast.Try) and any(_calls_skip(h) for h in cur.handlers):
            return True
        cur = par.get(cur)
    return False


def _seam_route(scope):
    """Which sanctioned route lets this case reach the package, or ``None``.

    TWO routes, each one measured as actually used in this leg (the referent
    pins in the rule below go red if a route loses its user):

      * ``pytest.importorskip("<cli…>")`` — the leg's dominant capability skip
        (five sites in ``test_dispatcher_wedge.py`` alone);
      * the package name handled as a VALUE (a path component, a cache key),
        which is how this leg builds or evicts a plant — a bare
        ``from hermes_cli import x`` contains no such literal, so this route
        cannot excuse the offender shape.

    Two routes are deliberately NOT credited, and both were measured rather than
    assumed:

      * the ``hscc-api`` owner route (``real_hermes``). Nothing in this leg
        gates a CLI reach through it today, and a credit route with no referent
        is un-firable coverage. When a case does start gating that way the rule
        names it and the route gets added deliberately.
      * a synthetic ``sys.modules`` install. A first cut of this predicate
        credited it and — measured — that alone hid the real bug: the offender's
        own ``setitem`` shadowing ``hermes_cli.kanban_db_dispatch`` earned it
        credit while the case failed on a thin interpreter. Even narrowed to the
        top-level key with a non-``None`` value the route has no user here
        (t_7c5f5051's own fix deleted the import instead of faking the package),
        so it is not sanctioned. A case that wants it must add it with a
        referent, not inherit it from a sibling leg.
    """
    for node in ast.walk(scope):
        if (isinstance(node, ast.Call)
                and _callee_name(node.func) == "importorskip"
                and node.args and _cli_literal(node.args[0])):
            return "importorskip"
        if isinstance(node, (ast.BinOp, ast.Compare)) and _cli_literal(node):
            return "plant-value"
    return None


def _seamed(scope):
    return _seam_route(scope) is not None


def _reaches(tree):
    """``(scope_key, scope_node, modules)`` for every LIVE reach in a file."""
    par = _parents(tree)
    groups = {}
    for node, mod in _cli_imports(tree):
        scope_obj, scope_node = _scope_of(node, par, tree)
        key = id(scope_obj) if scope_obj is not None else "<module>"
        g = groups.setdefault(key, {"scope": scope_node, "live": set(),
                                    "simulated": set()})
        (g["simulated"] if _simulated(node, par, scope_node) else g["live"]).add(mod)
    return [(k, g["scope"], g["live"], g["simulated"]) for k, g in groups.items()]


def test_every_case_that_reaches_real_hermes_cli_carries_a_seam():
    """A case that imports the real ``hermes_cli`` must say how it can.

    The bug this rule exists for, measured at ``829fc32c``:
    ``test_dispatcher_wedge.py::TestReviewDispatchCapabilityProbe::
    test_unimportable_gate_reads_as_unknown`` opened with a bare
    ``import hermes_cli``. Four sibling cases in that same file open with
    ``pytest.importorskip("hermes_cli.kanban_db_dispatch")``; this one did not,
    so on the thin ``p313`` sibling it raised ``ModuleNotFoundError`` and the
    case FAILED rather than skipped — the #504 shape, in a leg that had no rule
    against it. Worse, the full leg reported green: an earlier file leaves the
    real agent tree on ``sys.path`` (the leak t_2be3b04e is about), so the
    import resolved in-process. The two answers are distinguishable in the
    leg's own skip reasons — ``No module named 'utils'`` in-leg versus ``No
    module named 'hermes_cli'`` file-alone.

    So this rule is static (an ordering accident cannot hide from an AST) and
    per-case (a sibling case's seam cannot credit this one).

    The antecedent is NOT empty by construction and the assertion below is what
    keeps it honest: every seam route and every window exclusion is pinned to a
    live referent, the same discipline ``hscc-api``'s twin carries.
    """
    offenders, routes, windows = [], set(), set()
    for rel in _swept_test_files():
        tree = ast.parse((REPO / rel).read_text())
        for key, scope, live, simulated in _reaches(tree):
            if simulated:
                windows.add(rel)
            if not live:
                continue
            route = _seam_route(scope)
            if route:
                routes.add(route)
                continue
            offenders.append(f"{rel}::{key} imports {sorted(live)}")
    assert not offenders, (
        "these cases import the real hermes CLI with no seam — on an "
        "interpreter with no hermes they FAIL instead of skipping, and in a "
        "shared process they may pass only because an earlier file leaked the "
        "agent tree onto sys.path:\n" + "\n".join(sorted(offenders)) +
        f"\n— importorskip the package, install a fake under a literal "
        f"sys.modules key, or build/evict a plant by name (see _seamed)")

    # Referent pins: each route this rule credits, and the window it excludes,
    # must still have a user in the tree. A rule that claims a route nothing
    # uses reads as coverage and is worse than none.
    assert routes >= {"importorskip", "plant-value"}, (
        f"seam route(s) {sorted({'importorskip', 'plant-value'} - routes)} "
        "have no referent left in this leg — either the idiom has been dropped "
        "(then the route and its paragraph in _seam_route both go) or the sweep "
        "has stopped seeing it")
    assert windows, (
        "no case in this leg imports the CLI inside a simulation window any "
        "more — the exclusion half of the rule is now un-firable")


def test_the_cli_rule_fires_on_the_shape_it_polices():
    """Anti-vacuity, on source strings rather than on hope.

    Every branch is exercised, in both directions: the exact shape this card
    removed must be an OFFENDER, every credit route must credit it, and prose
    that merely mentions the package must NOT.
    """
    cli = REAL_CLI

    def live(source):
        tree = ast.parse(source)
        return [(mods, _seamed(scope))
                for _, scope, mods, sim in _reaches(tree)
                if mods and not sim]

    # The removed bug, verbatim in shape: the predicate catches it and the seam
    # predicate refuses to credit it.
    got = live(f"def test_x():\n    import {cli}\n"
               "    assert x\n")
    assert got == [({cli}, False)], got

    # An ImportFrom reports the module it imports FROM (`hermes_cli`), an Import
    # the dotted name it names — the same convention hscc-api's predicate uses.
    # Either way the reach is the same package, which is the question.
    got = live(f"def test_x():\n    from {cli} import kanban_db as kb\n")
    assert got == [({cli}, False)], got

    # Each credit route, on that same shape.
    assert live(f"def test_x():\n"
                f"    pytest.importorskip('{cli}.kanban_db_dispatch')\n"
                f"    from {cli} import kanban_db_dispatch\n")[0][1] is True
    assert live(f"def test_x(tmp_path):\n"
                f"    cli = tmp_path / '{cli}'\n"
                f"    import {cli}\n")[0][1] is True

    # A ``sys.modules`` write is NOT a seam in this leg — measured, not
    # assumed. The write that made the real bug invisible to a first cut of this
    # predicate shadows a SUBMODULE (with ``None``, or with a stub) to simulate
    # the submodule being unimportable, which presupposes the real package
    # resolves; and even a genuine fake of the TOP-LEVEL key has no referent
    # here, because t_7c5f5051's fix deleted the import instead of faking the
    # package. An un-referenced credit route only ever makes the rule more
    # permissive, so none of these credit a bare reach.
    assert live(f"def test_x(hermes_cli, monkeypatch):\n"
                f"    monkeypatch.delattr(hermes_cli, 'kanban_db_dispatch', raising=False)\n"
                f"    monkeypatch.setitem(sys.modules, '{cli}.kanban_db_dispatch', None)\n"
                f"    import {cli}\n")[0][1] is False
    assert live(f"def test_x(hermes_cli, monkeypatch):\n"
                f"    monkeypatch.setitem(sys.modules, '{cli}.kanban_db_dispatch', stub)\n"
                f"    import {cli}\n")[0][1] is False
    assert live(f"def test_x(monkeypatch):\n"
                f"    monkeypatch.setitem(sys.modules, '{cli}', pkg)\n"
                f"    from {cli} import kanban_db\n")[0][1] is False
    assert live(f"def test_x(monkeypatch):\n"
                f"    sys.modules['{cli}'] = pkg\n"
                f"    from {cli} import kanban_db\n")[0][1] is False

    # Prose alone is NOT a seam: an assert message that names the package must
    # not excuse a bare import (this is what keeps the plant-value route from
    # widening into "any mention of hermes_cli").
    assert live(f"def test_x():\n"
                f"    import {cli}\n"
                f"    assert x, 'the {cli}.kanban_db_dispatch gate moved'\n")[0][1] is False

    # A variable key earns no credit either way: the plant route needs the name
    # as a literal VALUE in a BinOp/Compare, not a computed one.
    assert live(f"def test_x(name):\n"
                f"    cli = tmp_path / name\n"
                f"    import {cli}\n")[0][1] is False

    # The windows: neither is a live reach.
    assert live(f"def test_x():\n"
                f"    with pytest.raises(ModuleNotFoundError):\n"
                f"        import {cli}.config\n") == []
    assert live(f"def test_x(agent):\n"
                f"    with _HermesCliGate(agent):\n"
                f"        import {cli}.config\n") == []
    assert live(f"def test_x():\n"
                f"    try:\n"
                f"        from {cli} import kanban_db_dispatch as kbd\n"
                f"    except Exception:\n"
                f"        pytest.skip('not here')\n") == []

    # A window in a SIBLING case must not excuse this one.
    assert live(f"def test_a(agent):\n"
                f"    with _HermesCliGate(agent):\n"
                f"        import {cli}.config\n"
                f"def test_b():\n"
                f"    import {cli}\n") == [({cli}, False)]
