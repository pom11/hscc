"""t_fdbc24f9 — the package's in-suite predicate must answer EXACTLY what the
bootstrap door's predicate answers (``hscc-bootstrap/doctor_rows.under_pytest``).

Why the comparison and not a shared import: the two callers live in two deployment
layouts that share no Python namespace. ``doctor_rows.py`` is a FLAT bootstrap module
whose imports resolve only when its own directory is ``sys.path[0]`` (the door and the
installer run ``doctor.py`` as a script), and a module inside ``hscc_daemon`` has no
bootstrap directory to find once the plugin is deployed standalone. Delegating the
other way would put a plugin-package import on the fence that guards bootstrap's
Stage-1 gate, which has to answer on a fresh install where ``hscc_daemon`` is not
deployed yet. The full ruling is in ``hscc_daemon/suite_fence.py``.

Duplication without a pin is how one copy quietly loses the marker half and becomes a
fence that guards the process nobody is asking about (the defect t_14eae4b5/t_eb768c2a
chased). So the copies are compared against the SAME four signal combinations AND
against the expected answer, which a bare "both agree" assertion would not catch: two
copies that are both wrong would agree happily.

Loading the bootstrap module by path (``importlib.util.spec_from_file_location``) is
legitimate here because the thing under test is the return value of a function on that
module, not a name another module must look up on it — the case where a spec-loaded
namespace makes instrumentation meaningless (t_0d991e1d measured that for ``runpy`` on
the door module, whose seams were doctor-internal names). ``doctor_rows`` imports
stdlib only, so it loads with no sibling on ``sys.path``.
"""

import importlib.util
import os
import sys

from hscc_daemon import suite_fence

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: The sibling predicate. Deliberately not imported: the directory that holds it is a
#: hyphenated plugin payload, importable only inside its own leg.
_DOCTOR_ROWS = os.path.join(_REPO, "hscc-bootstrap", "doctor_rows.py")


def _load_bootstrap_predicate():
    """The flat bootstrap module, by path, loaded only for the length of the exec.

    Registered in ``sys.modules`` DURING the exec and removed again afterwards: Python
    resolves a dataclass's defining module through ``sys.modules`` while building it
    (3.14 raises ``AttributeError: 'NoneType' object has no attribute '__dict__'``
    without it), and leaving a foreign name in this leg's namespace is exactly the
    cross-leg leak t_0c4989d7 is about. The predicate itself looks nothing up on its
    own module, so it keeps answering after the entry is gone.
    """
    assert os.path.exists(_DOCTOR_ROWS), (
        f"{_DOCTOR_ROWS} is gone — the predicate this file pins against moved, and "
        "the two copies are now unpinned")
    name = "doctor_rows_pin"
    spec = importlib.util.spec_from_file_location(name, _DOCTOR_ROWS)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    saved = sys.modules.get(name)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        if saved is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved
    return mod


def _answers(monkeypatch, *, in_modules: bool, marker: bool) -> tuple:
    """Both predicates, on one signal combination, with the process's own state put
    back afterwards (``monkeypatch`` restores both dicts)."""
    if in_modules:
        monkeypatch.setitem(sys.modules, "pytest", sys.modules.get("pytest", object()))
    else:
        monkeypatch.delitem(sys.modules, "pytest", raising=False)
    if marker:
        # What pytest itself exports per item, and what every child of that item
        # inherits — the only signal that crosses the spawn.
        monkeypatch.setenv(suite_fence.PYTEST_MARKER, "some.module::case (call)")
    else:
        monkeypatch.delenv(suite_fence.PYTEST_MARKER, raising=False)
    return (suite_fence.under_pytest(), _load_bootstrap_predicate().under_pytest())


def test_both_predicates_see_the_same_marker(monkeypatch):
    """A rename on one side must be loud, not a silent second channel."""
    bootstrap = _load_bootstrap_predicate()
    assert suite_fence.PYTEST_MARKER == bootstrap.PYTEST_MARKER == "PYTEST_CURRENT_TEST"


def test_the_two_predicates_answer_alike_for_every_signal_combination(monkeypatch):
    """Each copy is checked against the expected answer, not only against each other.

    The two single-signal rows are the whole point: ``marker only`` is the spawned
    child, and ``sys.modules only`` is the suite process between items. A copy that
    drops either half fails here — which is exactly the drift the duplication accepts
    by design and the pin makes impossible.
    """
    cases = (
        # (pytest in sys.modules, marker set) -> the answer, and what it means
        (False, False, False, "a plain operator run: the fence must not fire"),
        (False, True, True, "a child a suite item spawned: the defect this card fixed"),
        (True, False, True, "the suite process itself, between items"),
        (True, True, True, "a child spawned by a suite that also imported pytest"),
    )
    for in_modules, marker, expected, meaning in cases:
        mine, theirs = _answers(monkeypatch, in_modules=in_modules, marker=marker)
        assert mine == expected, (
            f"suite_fence.under_pytest() said {mine}, expected {expected} — "
            f"{meaning}")
        assert theirs == expected, (
            f"doctor_rows.under_pytest() said {theirs}, expected {expected} — "
            f"{meaning}")
        assert mine == theirs, (
            f"the two predicates disagree with pytest={in_modules} "
            f"marker={marker}: {mine} != {theirs} ({meaning})")


def test_the_row_asks_the_shared_predicate_and_not_its_own_copy(monkeypatch, tmp_path):
    """The fence in ``node_pause_exec`` must route through ``suite_fence``.

    A module-local re-implementation would pass every behavioural case above (the
    marker happens to be set here) while being free to drift from it later, which is
    the failure this card exists to prevent. So the seam itself is pinned: with pytest
    removed from this process and only the marker left, the row still refuses — and
    the refusal arrives from the shared predicate, not from a copy.
    """
    import hscc_daemon.node_pause_exec as npe

    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    monkeypatch.setenv(suite_fence.PYTEST_MARKER, "some.module::case (call)")
    calls = []
    monkeypatch.setattr(suite_fence, "under_pytest",
                        lambda: calls.append(1) or True)

    res = npe.doctor_check()

    assert calls, ("the default-root fence did not ask the shared predicate; a "
                   "module-local copy of the test is the drift this card rules out")
    assert res["ok"] is False and res["fatal"] is False
    assert "refused inside a pytest process" in res["detail"], res["detail"]
