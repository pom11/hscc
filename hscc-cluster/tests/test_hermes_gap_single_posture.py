"""One posture per hermes capability on the ``hscc-cluster`` leg (t_b884497b).

This leg was the fourth blind spot: ``hscc-cluster`` answered "can this
interpreter run hermes?" with a private shape in each of four files —
``_HAS_HERMES_CLI`` in ``test_lifecycle_hooks.py``, module-level
``pytest.importorskip("hermes_cli…")`` in ``test_dispatch_hook.py``,
``test_review_escalation.py`` and ``test_protocol_violation_bound.py`` — and the
runner's roster block could not see any of it, so a green ``hscc-cluster`` leg
said nothing about why 19 cases had not run. The fix is this leg's own probe
module (``hermes_gap.py``, four capabilities plus one child-process probe); this
file is what keeps the fix from rotting.

Doctrine, inherited and not re-decided (t_02c98915 → t_2cc3d482 → t_0fd2c149 →
t_74141b24):

  * ONE probe module per leg, one function per capability, called at RUNTIME.
    A collection-time answer is wrong on this leg for a reason that is measured,
    not theoretical: ``workflow._active_profile_name`` inserts
    ``~/.hermes/hermes-agent`` into ``sys.path`` the first time it runs
    (workflow.py:523, production's own import fence), so the answer at
    import time and the answer after the first lifecycle case are different
    answers. ``test_the_probes_answer_at_call_time_not_at_import`` below is the
    pin.
  * The skip carries the IMPORT ERROR and names the MODULE — the runner's stamp
    rule reads ``-rs`` and has to be able to tell "the sibling env is thin"
    (correct) from "hermes is there and still broken" (a finding).
  * A leg's guard sweeps ITS OWN leg only. ``hscc-api``'s and ``hscc-roles``'
    guards police their own dirs; this one polices ``hscc-cluster/tests``. The
    shared surface is ``scripts/run_tests.sh``'s roster block and its ``owners``
    list, not the sweep (t_0fd2c149 declined to fold legs together; that is why
    this file exists separately).
  * A capability that is present must RUN. Half of this card is the inverse
    failure — ``TestBlockedHandler``/``TestCompletedHandler`` and the two
    stub-injecting ``_setup_mock`` cases were being skipped for a hermes they
    never touch. ``test_a_stub_is_not_the_real_module`` keeps that from
    re-collapsing: the stub cases are exempt from gating, deliberately.
"""

import ast
import importlib
import re
import sys
from pathlib import Path

import pytest

import hermes_gap

REPO = Path(__file__).resolve().parents[2]
LEG = Path(__file__).resolve().parent
OWNER_NAME = "hermes_gap.py"
OWNER = LEG / OWNER_NAME
GUARD_NAME = Path(__file__).name
ROSTER = "scripts/run_tests.sh"
LEG_LABEL = "hscc-cluster"

#: ``_pvb_sim.py`` is not a test module — pytest never collects it; the case
#: spawns it with ``sys.executable``. Its module-level ``import hermes_cli`` is
#: the child's own import, not a collection-time posture here, so it is exempt
#: from the import-scope sweep (and it is the reason ``require_child_ran_hermes``
#: exists).
CHILD_PAYLOAD = "_pvb_sim.py"

#: ``TestOnKanbanTaskClaimedProfileName._setup_mock`` injects a
#: ``types.ModuleType("hermes_cli")`` stub into ``sys.modules`` to prove
#: ``workflow.on_kanban_task_claimed`` works without hermes. Those cases MUST run
#: on a thin interpreter — gating them would silence a test that can run, the
#: inverse of this card's bug — so a helper that only builds stubs is not
#: "touching real hermes".
STUB_HELPERS = frozenset({"_setup_mock"})

#: Names that mean "this file grew its own answer".
PRIVATE_PROBE_DEFS = re.compile(
    r"^def (_?require_[a-z_]+|_hermes_cli_gate|_hermes_gate|_hermes_available)\(",
    re.MULTILINE)
HERMES_ROOTS = ("hermes_cli", "hermes_state")
FLAG_SHAPE = re.compile(r"_HAS_HERMES\w*")


# --------------------------------------------------------------------------- #
# the leg, as syntax trees
# --------------------------------------------------------------------------- #

def _leg_files():
    """Every pytest-collected module in the leg, plus the probe owner.

    ``conftest.py`` is included: it runs before every case, so a hermes answer
    frozen there is the most expensive one to get wrong.
    """
    return sorted(p for p in LEG.rglob("*.py")
                  if "__pycache__" not in p.parts
                  and (p.name.startswith("test_") or p.name == "conftest.py"
                       or p.name == OWNER_NAME))


def _trees():
    return {p: ast.parse(p.read_text()) for p in _leg_files()}


def _imports_in(node):
    """Every module imported by *node* (its own scope and any nested scope)."""
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Import):
            out |= {a.name for a in sub.names}
        elif isinstance(sub, ast.ImportFrom) and sub.module:
            out.add(sub.module)
    return out


def _hermes_imports(modules):
    return {m for m in modules if m.split(".")[0] in HERMES_ROOTS}


def _calls_in(node):
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            out.add(f.attr if isinstance(f, ast.Attribute) else
                    (f.id if isinstance(f, ast.Name) else ""))
    return out


def _functions(trees):
    out = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                out.setdefault(node.name, node)
    return out


def _real_hermes_helpers(trees):
    """Functions that import hermes, plus everything that calls one of them.

    Transitive, because ``_board()`` imports nothing and still cannot run without
    the board. ``STUB_HELPERS`` is excluded at the seed AND the closure, so a
    case whose only hermes contact is an injected stub is not "real hermes".
    """
    funcs = _functions(trees)
    helpers = {n for n, nd in funcs.items()
               if _hermes_imports(_imports_in(nd)) and n not in STUB_HELPERS}
    changed = True
    while changed:
        changed = False
        for n, nd in funcs.items():
            if n in helpers or n in STUB_HELPERS:
                continue
            if _calls_in(nd) & helpers:
                helpers.add(n)
                changed = True
    return helpers


def _body_after_docstring(node):
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return body


def _is_owner_gate(call):
    """``hermes_gap.<anything>`` — a gate is a call on the owner module."""
    return (isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "hermes_gap")


# --------------------------------------------------------------------------- #
# 1 — the leg has ONE owner, and the runner's roster can see it
# --------------------------------------------------------------------------- #

def test_the_leg_has_one_probe_owner_and_the_runner_roster_names_it():
    """``hermes_gap.py`` exists, and the roster block names this leg and every
    probe it defines — both directions.

    A probe that exists but is not listed is a capability nobody reading a red
    sibling leg can find; a roster row with no function behind it is a roster
    that lies. ``hscc-api``'s guard pins the roster against ALL owners'
    definitions (that is where ``owners`` lives); this one pins the other
    direction for THIS leg, so an un-listed capability here goes red here.
    """
    assert OWNER.exists(), f"this leg has no probe module at {OWNER}"
    roster = (REPO / ROSTER).read_text()

    lines = roster.splitlines()
    start = [i for i, ln in enumerate(lines)
             if re.match(rf"^#\s+{LEG_LABEL}\s", ln)]
    assert start, (f"the runner's roster block does not name {LEG_LABEL} — a leg "
                   "with a probe module that is not in the block is invisible to "
                   "the two-way roster pin (t_02c98915's stale-roster failure)")
    entry = [lines[start[0]]]
    for ln in lines[start[0] + 1:]:
        # A new leg's entry (label column + the tests/… column) or the block's
        # trailing empty comment ends this leg's entry.
        if re.match(r"^#\s+[\w-]+\s+tests/", ln) or ln.strip() == "#":
            break
        entry.append(ln)
    chunk = "\n".join(entry)
    assert "tests/hermes_gap.py" in chunk, (
        f"{LEG_LABEL}'s roster entry does not name its probe module: {chunk!r}")

    advertised = set(re.findall(r"\b_?require_[a-z_]+\b", chunk))
    defined = {n for n in hermes_gap.PROBES if n.startswith("require_")}
    assert advertised == defined, (
        f"roster says {sorted(advertised)}, {OWNER_NAME} defines "
        f"{sorted(defined)} — the block is the live authority for what exists, so "
        "it has to agree with the module in both directions")

    assert GUARD_NAME in roster, (
        f"the roster's 'Enforced, not asserted' paragraph does not name this guard "
        f"({GUARD_NAME}) — a leg with a probe and no guard is how {LEG_LABEL} "
        "got four private postures")


# --------------------------------------------------------------------------- #
# 2 — the probe vocabulary lives in exactly one place
# --------------------------------------------------------------------------- #

def test_the_probe_vocabulary_has_one_definition_per_fact():
    """``PROBES``, ``CAPABILITY_MODULES``, ``CAPABILITY_WHY`` and
    ``CAPABILITIES`` agree, and they are the ONLY list of the leg's surfaces.

    Four tables is already the minimum for "one function per capability, one
    reason per capability"; a fifth copy in a guard, or a table that grows a row
    nothing implements, is how a roster goes stale by two families.
    """
    labels = [label for label, _ in hermes_gap.CAPABILITY_MODULES]
    assert len(labels) == len(set(labels)), "a capability label is listed twice"
    assert set(labels) == set(hermes_gap.CAPABILITY_WHY), (
        "a capability has no WHY, so its skip reason cannot say what the case "
        "needed — which is the difference between a readable -rs line and the one "
        "this card removed")
    assert set(hermes_gap.CAPABILITY_WHY) == {c[0] for c in hermes_gap.CAPABILITIES}
    for label, modules, require, getter in hermes_gap.CAPABILITIES:
        assert _hermes_imports(modules) == set(modules), (
            f"{label}: a capability probes a non-hermes module {modules}")
        for name in (require, getter):
            assert callable(getattr(hermes_gap, name, None)), (
                f"CAPABILITIES names {name!r} for {label}, which is not callable")
        assert modules == dict(hermes_gap.CAPABILITY_MODULES)[label], (
            f"{label}: the two tables disagree about what it imports")

    declared = {n for n in hermes_gap.PROBES}
    from_table = {c[2] for c in hermes_gap.CAPABILITIES} | {c[3] for c in hermes_gap.CAPABILITIES}
    # The child probe is the documented exception: it asks another interpreter.
    child = {"child_import_error", "require_child_ran_hermes"}
    assert declared == from_table | child, (
        f"PROBES={sorted(declared)} but the capability table+child probe give "
        f"{sorted(from_table | child)} — a probe nobody lists is unenforced")

    defined_in_code = set(re.findall(r"^def ([a-z_]+)\(", OWNER.read_text(),
                                     re.MULTILINE))
    # ``_first_import_error``/``_skip`` are shared plumbing, not probes.
    private = defined_in_code - declared - {"_first_import_error", "_skip"}
    assert not private, f"undocumented probe-shaped function(s): {sorted(private)}"


def test_the_probes_answer_at_call_time_not_at_import():
    """No module-level hermes import and no module-level cached answer.

    This is the pin the whole card is about. On this leg the answer provably
    changes mid-run — ``workflow._active_profile_name`` puts
    ``~/.hermes/hermes-agent`` on ``sys.path`` (workflow.py:523) the first
    time it runs — so a flag computed at import is not a conservative answer, it
    is a wrong one that nothing reports.
    """
    for path, tree in _trees().items():
        if path.name == CHILD_PAYLOAD:
            continue
        import_scope = list(tree.body)
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            import_scope += list(cls.body)     # a class body IS import time
        for node in import_scope:                    # module/class scope
            assert not _hermes_imports(
                {n.name for n in node.names} if isinstance(node, ast.Import) else
                ({node.module or ""} if isinstance(node, ast.ImportFrom) else set())), (
                f"{path.name}:{node.lineno} imports hermes at module scope — that "
                "is the collection-time event this leg removed; import it inside "
                "the case, after the gate")
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign):
                bad = _calls_in(stmt.value) & {
                    n for n in hermes_gap.PROBES if n.endswith("_import_error")}
                assert not bad, (
                    f"{path.name}:{stmt.lineno} freezes {sorted(bad)} into a "
                    "module-level name — the answer must be computed per call")

    for _label, _modules, require, _getter in hermes_gap.CAPABILITIES:
        fn = ast.parse(OWNER.read_text())
        body = next(n for n in ast.walk(fn)
                    if isinstance(n, ast.FunctionDef) and n.name == require)
        assert "_first_import_error" in _calls_in(body), (
            f"{require} does not ask the question itself — it reads a cached "
            "answer, which is the flag shape in a different outfit")


# --------------------------------------------------------------------------- #
# 3 — nobody grows a private answer
# --------------------------------------------------------------------------- #

def test_no_other_file_in_the_leg_grows_its_own_hermes_probe():
    """No second module defining ``require_*``/gate helpers, and no hand-rolled
    ``hasattr(hermes_cli…)`` / ``find_spec("hermes_cli…")`` probe.

    The flag was not the only shape this leg could drift into; a per-file
    ``_hermes_gate()`` would drift the same way, one file at a time.
    """
    offenders = []
    for path in _leg_files():
        if path.name == OWNER_NAME:
            continue
        src = path.read_text()
        if GUARD_NAME == path.name:
            continue                      # this file names them in prose
        for m in PRIVATE_PROBE_DEFS.finditer(src):
            offenders.append(f"{path.name}: def {m.group(1)}(")
        for pat in ("hasattr(hermes_cli", '__import__("hermes_cli',
                    "find_spec(\"hermes_cli", "find_spec('hermes_cli"):
            if pat in src:
                offenders.append(f"{path.name}: private probe {pat!r}")
    assert not offenders, (
        f"private hermes capability answer(s) in the leg: {offenders} — the leg has "
        f"one probe module, {OWNER_NAME}")


def test_the_collection_time_flag_shape_is_dead():
    """No ``_HAS_HERMES_*`` assignment and no ``skipif`` reading one.

    The anti-vacuity target: restoring the module-level flag this card deleted
    goes red HERE, naming this test id, rather than silently skipping 19 cases
    again. AST rather than text on purpose — the prose in ``hermes_gap.py`` and in
    these docstrings describes the old shape and must keep doing so.
    """
    offenders = []
    for path, tree in _trees().items():
        if path.name in (OWNER_NAME, GUARD_NAME):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and FLAG_SHAPE.fullmatch(tgt.id):
                        offenders.append(f"{path.name}:{node.lineno} {tgt.id} =")
            if isinstance(node, ast.Name) and FLAG_SHAPE.fullmatch(node.id):
                offenders.append(f"{path.name}:{node.lineno} reads {node.id}")
    assert not offenders, (
        f"collection-time hermes flag back in service: {offenders} — gate on "
        f"{OWNER_NAME}.require_* instead, at runtime")


def test_no_case_in_the_leg_silences_hermes_with_a_skip_it_wrote():
    """Every hermes-naming skip / importorskip / skipif in the leg is the OWNER's.

    A private ``pytest.skip("hermes_cli not installed in this env")`` is the
    fingerprint this family looks for: it names a capability, no module and no
    import error, so the stamp rule cannot read it — and it is the exact wording
    this card found in all four files.
    """
    offenders = []
    for path, tree in _trees().items():
        if path.name in (OWNER_NAME, GUARD_NAME):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (
                fn.id if isinstance(fn, ast.Name) else "")
            if name not in ("skip", "importorskip", "skipif"):
                continue
            text = " ".join(s.value for s in ast.walk(node)
                            if isinstance(s, ast.Constant) and isinstance(s.value, str))
            if "hermes" in text.lower():
                offenders.append(f"{path.name}:{node.lineno} pytest.{name}")
            if name == "importorskip" and _hermes_imports(
                    {node.args[0].value} if node.args and
                    isinstance(node.args[0], ast.Constant) else set()):
                offenders.append(f"{path.name}:{node.lineno} importorskip(hermes…)")
    assert not offenders, (
        f"hermes skip(s) outside the probe module: {offenders} — the reason has to "
        f"come from {OWNER_NAME}, which is where the module and the error live")


def test_a_stub_is_not_the_real_module():
    """The stub-injecting cases stay UNGATED, on purpose.

    ``TestOnKanbanTaskClaimedProfileName`` fakes ``hermes_cli`` with
    ``types.ModuleType`` to prove ``workflow`` copes with a missing/partial
    hermes. Gating those on a hermes capability would take four real tests away
    from the thin interpreter — the same card that just gave four cases BACK by
    un-gating the pure JSONL writers. If someone "tidies" them behind a gate, this
    goes red.
    """
    trees = _trees()
    gated = []
    for path, tree in trees.items():
        if path.name in (OWNER_NAME, GUARD_NAME):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not (_calls_in(node) & STUB_HELPERS):
                continue
            if any(_is_owner_gate(s.value) for s in _body_after_docstring(node)
                   if isinstance(s, ast.Expr)):
                gated.append(f"{path.name}:{node.lineno} {node.name}")
    assert not gated, (
        f"stub-only case(s) now gated on a hermes capability: {gated} — they inject "
        f"a types.ModuleType stub and run everywhere; {STUB_HELPERS} is the "
        "documented exemption")


# --------------------------------------------------------------------------- #
# 4 — the gate is real, and it is first
# --------------------------------------------------------------------------- #

def test_every_case_that_touches_real_hermes_gates_before_it_touches_it():
    """A case that imports hermes (or calls a helper that does) must call a
    ``hermes_gap`` gate as its first executable statement.

    "First" is the point, not decoration: a gate after ``kb.init_db()`` has
    already raised on the thin sibling, and a leg that reports 19 skips is
    indistinguishable from one that reports 19 failures only if the decision came
    before the work. Class-level and module-level gates are not accepted either —
    they are collection-time answers wearing a class costume.
    """
    trees = _trees()
    helpers = _real_hermes_helpers(trees)
    offenders = []
    for path, tree in trees.items():
        if path.name in (OWNER_NAME, GUARD_NAME, CHILD_PAYLOAD):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            touches = bool(_hermes_imports(_imports_in(node))) or bool(
                _calls_in(node) & helpers)
            if not touches:
                continue
            body = _body_after_docstring(node)
            if not (body and isinstance(body[0], ast.Expr)
                    and _is_owner_gate(body[0].value)):
                offenders.append(f"{path.name}:{node.lineno} {node.name}")
    assert not offenders, (
        f"hermes-touching case(s) with no owner gate as their first statement: "
        f"{offenders} — call hermes_gap.require_* first, so a thin interpreter "
        "skips with a reason instead of failing mid-work")


# --------------------------------------------------------------------------- #
# 5 — what a skip says, and what it must not say
# --------------------------------------------------------------------------- #

def test_a_skip_reason_names_the_module_and_carries_the_import_error(monkeypatch):
    """The stamp rule reads ``-rs``; the reason must survive that reading.

    Forces each capability to answer "missing" and checks the reason an operator
    would actually see: the module, the phrase ``not importable``, the exception
    text, and the capability's WHY. ``hermes_cli not installed in this env`` —
    what this card replaced — fails every one of those four.
    """
    probe_module = "hermes_cli.some_moved_module"
    probe_error = "ModuleNotFoundError: No module named 'hermes_cli.some_moved_module'"
    monkeypatch.setattr(hermes_gap, "_first_import_error",
                        lambda modules: (probe_module, probe_error))
    for label, _modules, require, _getter in hermes_gap.CAPABILITIES:
        with _assert_skipped() as caught:
            getattr(hermes_gap, require)()
        reason = str(caught.value)
        assert probe_module in reason, f"{require}: reason names no module: {reason}"
        assert hermes_gap.NOT_IMPORTABLE in reason, (
            f"{require}: reason does not use the leg's one phrase "
            f"{hermes_gap.NOT_IMPORTABLE!r}: {reason}")
        assert probe_error in reason, (
            f"{require}: reason drops the import error, so an operator cannot "
            f"tell a thin env from a broken one: {reason}")
        assert hermes_gap.CAPABILITY_WHY[label] in reason, (
            f"{require}: reason does not say what the case needed: {reason}")


def test_a_capability_that_is_importable_does_not_skip():
    """The probe's answer must match ``__import__``'s answer, per capability.

    Two mutations die here in opposite directions: a probe that always skips turns
    a green leg into a silenced one (``-rs`` would show 19 skips and nobody would
    notice they used to run), and a probe that lies "importable" sends the case
    into an ``AttributeError`` mid-work. Neither is visible in a summary line that
    just says "green", which is why this compares against the real import rather
    than against a remembered count.
    """
    for label, modules, require, getter in hermes_gap.CAPABILITIES:
        importable = all(_imports_here(m) for m in modules)
        reported = getattr(hermes_gap, getter)()
        assert (reported is None) == importable, (
            f"{label}: getter reported {reported!r} but importing "
            f"{[m for m in modules if not _imports_here(m)]} says otherwise")
        if importable:
            getattr(hermes_gap, require)()           # must not raise Skipped
        else:
            with _assert_skipped():
                getattr(hermes_gap, require)()


def test_the_child_probe_answers_from_the_child_not_from_here():
    """``require_child_ran_hermes`` skips only when the CHILD died of an import.

    ``test_protocol_violation_bound.py`` never imports hermes in this process — it
    spawns ``_pvb_sim.py`` with ``sys.executable``, which does not inherit this
    process's ``sys.path`` (the one ``workflow``'s fence extends), so the parent's
    answer is the wrong question. Asking the child's own stderr keeps the
    distinction the family cares about: a child that died of anything else is a
    FINDING, and must fall through to the assertion, not become environment.
    """
    assert hermes_gap.child_import_error(0, "boom") is None
    assert hermes_gap.child_import_error(
        1, "Traceback…\nsqlite3.OperationalError: no such table: tasks") is None, (
        "a real bug read as environment — the case must run and fail")
    died = "Traceback…\nModuleNotFoundError: No module named 'hermes_cli'"
    reported = hermes_gap.child_import_error(1, died)
    assert reported is not None and "ModuleNotFoundError" in reported
    assert hermes_gap.child_import_error(1, "") is None

    with _assert_skipped() as caught:
        hermes_gap.require_child_ran_hermes(1, died)
    reason = str(caught.value)
    assert "hermes_cli.kanban_db" in reason and "child" in reason, (
        f"the child skip must name the module and say it was the child: {reason}")
    with _assert_skipped():
        hermes_gap.require_child_ran_hermes(1, died)
    hermes_gap.require_child_ran_hermes(0, "")        # a good run never skips
    hermes_gap.require_child_ran_hermes(1, "assertion blew up")   # a bug never skips


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

class _assert_skipped:
    """``pytest.raises(pytest.skip.Exception)`` with the message in hand."""

    def __enter__(self):
        class _Caught:
            value = None
        self._caught = _Caught()
        return self._caught

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            raise AssertionError("expected a pytest.skip() and got none — the "
                                 "capability probe silently answered 'available'")
        if not isinstance(exc, pytest.skip.Exception):
            return False                      # a real error: let it propagate
        self._caught.value = exc
        return True


def _imports_here(module):
    """Can THIS process import *module*? Restores ``sys.modules`` afterwards.

    The oracle has to be in-process, because that is the question the probe asks:
    a gated case imports hermes in this process, on this ``sys.path``, which by
    now may hold ``~/.hermes/hermes-agent`` from ``workflow``'s fence
    (workflow.py:523). A child interpreter would answer a different question —
    it does not inherit ``sys.path`` — and that is why the child capability has its
    OWN probe (``require_child_ran_hermes``) instead of reusing these four.

    Restoring ``sys.modules`` is not tidiness: this guard sorts before
    ``test_lifecycle_hooks.py``, so a successful import left here would hand every
    later case an importable hermes the leg's own fence had not earned, and turn
    "19 skips on the thin sibling" into a number nobody measured.
    """
    before = set(sys.modules)
    try:
        importlib.import_module(module)
        return True
    except Exception:                       # noqa: BLE001 — any failure is the answer
        return False
    finally:
        for name in set(sys.modules) - before:
            sys.modules.pop(name, None)
