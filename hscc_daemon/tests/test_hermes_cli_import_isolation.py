"""t_2be3b04e — one file planting a FAKE ``hermes_cli`` must not decide what a
later file's board enumeration sees.

The bug, measured (full evidence in
``docs/audits/orch-waker-import-leak_t_2be3b04e.md``): ``test_autodown.py``
plants a fake ``hermes_cli`` under ``tmp_path`` and resolves it through
``autodown._load_kanban_db_or_default``. That resolver does
``sys.path.insert(0, <agent dir>)`` and never takes it back out, so the planted
``<tmp>/hermes-agent`` sits at the FRONT of ``sys.path`` for the rest of the
process — and pytest keeps tmp dirs alive until the NEXT session. The next test
that touches the seam therefore re-imports the fake (the finalizer that planted
it had already run and could not), a fake without ``list_boards`` becomes the
resident kanban lib — the documented "single-board shape" — and
``test_orch_waker.py::test_enumerate_includes_legacy_default_and_named_boards``
loses its ``beta`` board. Alone it passes; in a shared process it does not.

These tests pin both halves of the guarantee the conftest fixture now gives:

  * leak 1 — a planted tree never survives in ``sys.path``, and
  * leak 2 — a planted module, or a REAL module still holding one as ``_kb``
    (the shape a plain evict cannot repair), never survives in ``sys.modules``.

The end-to-end case runs a REAL nested pytest over two generated files, so the
proof is the cross-FILE leak the card is about, not a simulation of it.
"""

import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

from hscc_daemon.tests.hermes_cli_plant import agent_tree_root  # noqa: E402
from hscc_daemon.tests.real_hermes import require_real_hermes  # noqa: E402

#: The tree this process resolves ``hermes_cli`` from — asked of the harness
#: OWNER, never re-derived from ``~``. ``hermes_cli_plant.agent_tree_root`` is
#: itself production's ``hermes_import.resolve_agent_tree``, so the rule and the
#: shipped code cannot disagree about where hermes lives. A ``Path.home()`` copy
#: here read as "no such tree" under the roster-wide HOME seal (t_ce473bf0):
#: ``HOME`` had moved to an empty /tmp dir while the runner declared the real
#: tree through the documented ``HERMES_AGENT_PATH`` seam (PR #606).
REAL_AGENT = Path(agent_tree_root())

# The shape test_autodown.TestLoadKanbanDb plants: a kanban_db with no
# list_boards, i.e. exactly autodown's "single-board shape".
FAKE_KANBAN_DB = "connect_closing = None"

# File A of the nested suite: plant a fake THROUGH THE PRODUCTION RESOLVER and
# prove the plant took (so the probe below cannot pass for the wrong reason).
_PLANT_FILE = '''
import os
from pathlib import Path

from hscc_daemon import autodown


def test_plant_a_fake_agent_tree(tmp_path, monkeypatch):
    agent = tmp_path / "hermes-agent"
    cli = agent / "hermes_cli"
    cli.mkdir(parents=True)
    (cli / "__init__.py").write_text("")
    (cli / "kanban_db.py").write_text(%r)
    monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))
    lib = autodown._load_kanban_db_or_default()
    assert lib is not None, "the resolver did not take HERMES_AGENT_PATH"
    assert not hasattr(lib, "list_boards"), "the plant looks like the real lib"
    assert str(tmp_path) in lib.__file__, lib.__file__
    marker = os.environ.get("HSCC_T2BE3B04E_PLANT_MARKER")
    if marker:
        # stdout is captured in -q mode, so leave the proof on disk instead.
        Path(marker).write_text(lib.__file__)
    print("PLANTED " + lib.__file__)
''' % FAKE_KANBAN_DB

# File B: same process, NEXT FILE — what the card's failure hinged on.
_PROBE_FILE = '''
import os
import sys
from pathlib import Path

from hscc_daemon import autodown
# The canary asks the SAME question the rest of the suite asks, so it asks the
# SAME predicate. This nested process has the repo on PYTHONPATH and conftest
# loaded as a plugin, so file B had no licence to re-derive "is this path a
# plant" for itself (t_b62da70c) — a third rule here would be exactly the drift
# this file family exists to prevent.
from hscc_daemon.tests.hermes_cli_plant import (agent_tree_root,
                                                is_planted_path)

#: Same rule as the outer module and for the same reason: the tree is asked of
#: the owner, never re-derived from ``~``, because under the HOME seal the tree
#: is wherever the runner declared it (t_ce473bf0).
REAL_AGENT = Path(agent_tree_root())


def test_no_planted_agent_dir_survives_on_sys_path():
    strays = [p for p in sys.path
              if str(p).endswith("hermes-agent") and is_planted_path(p)]
    assert not strays, f"planted tree left on sys.path: {strays}"


def test_the_real_hermes_lib_is_visible_after_a_plant():
    lib = autodown._load_kanban_db_or_default()
    assert lib is not None, (
        "the real hermes_cli must resolve in a process that has only ever "
        "planted fakes: " + str(autodown.kanban_check_state()))
    assert hasattr(lib, "list_boards"), (
        "LEAK: the resident kanban lib is a planted fake: " + str(lib.__file__))
    assert os.path.realpath(lib.__file__).startswith(
        os.path.realpath(str(REAL_AGENT))), lib.__file__
'''


def _conftest():
    """The conftest pytest ALREADY loaded, looked up by name.

    Deliberately not an ``import``: a second import of conftest would run its
    module-level sparkrun-fence loader again and swap the live fence module out
    from under the running suite — the same class of bug this file is about.
    """
    for name in ("hscc_daemon.tests.conftest", "conftest"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "_hermes_cli_poison_reason"):
            return mod
    pytest.skip("conftest is not loaded under a known name in this run")


def _run_nested(tmp_path, a_file=_PLANT_FILE, b_file=_PROBE_FILE,
                marker_env=("HSCC_T2BE3B04E_PLANT_MARKER", "planted.txt"),
                names=("test_a_plant.py", "test_b_probe.py")):
    """Run a two-file pytest suite in its OWN process against this repo, with
    the real conftest loaded as a plugin. Returns (returncode, output)."""
    a_name, b_name = names
    (tmp_path / a_name).write_text(a_file)
    (tmp_path / b_name).write_text(b_file)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    env[marker_env[0]] = str(tmp_path / marker_env[1])
    # A worker shell's board pins must not steer the nested run (t_7e343e04).
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME",
                "HERMES_KANBAN_TASK", "HERMES_DELEGATED_CHILD_CONTEXT",
                "PYTEST_CURRENT_TEST"):
        env.pop(key, None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "hscc_daemon.tests.conftest",
         a_name, b_name],
        env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=300)
    return proc.returncode, proc.stdout + proc.stderr


def test_a_planted_fake_does_not_reach_the_next_file(tmp_path):
    """THE regression: file A plants a fake through the production resolver and
    file B — same process, next file — must still see hermes' real lib.

    Nested rather than simulated: the card's failure is a cross-file leak inside
    ONE pytest process, and only a real second file exercises it.

    Gated (t_02c98915): file B's assertion IS "the real hermes_cli resolves",
    which no interpreter lacking Hermes' own runtime deps can satisfy — the
    nested child runs under ``sys.executable``, i.e. the same thin interpreter
    that failed the outer assert. The leak this test pins is about the PLANT
    leaving, not about Hermes being installed.
    """
    require_real_hermes()
    rc, out = _run_nested(tmp_path)
    # Non-vacuity: file A really did plant a fake through the production
    # resolver and really did hold it (stdout is captured under -q, so file A
    # leaves the proof on disk).
    marker = tmp_path / "planted.txt"
    assert marker.exists(), (
        "file A never planted (or never resolved) a fake hermes_cli — this "
        f"test would be green for the wrong reason:\n{out}")
    planted = marker.read_text()
    assert planted.endswith("hermes_cli/kanban_db.py"), planted
    assert "hermes-agent" in planted, planted
    assert rc == 0, (
        "a planted hermes_cli leaked out of the file that planted it:\n" + out)


# ── t_572b4856: the OTHER leak — what a CLOSED window leaves in sys.modules ──
#
# The pair above pins the planted tree. This pair pins its sibling, which is
# invisible to that fixture: ``hermes_import.agent_tree_on_path`` restores
# ``sys.path`` (804 events over a leg, zero residue on the path) but cannot
# un-import what the block imported. A resident ``hermes_cli`` carries
# ``__path__`` into ``~/.hermes/hermes-agent``, so every LATER ``import
# hermes_cli.<anything>`` resolves with the tree nowhere on ``sys.path`` — and a
# failing submodule import is enough to create it, because the parent succeeds.
# Measured, both interpreters:
# ``docs/audits/daemon-alone-run-order-dependence_t_572b4856.md``.
#
# File A: open a window through the PRODUCTION entry (not a hand-rolled import)
# and record the cache it found vs the cache it ended with — the marker is what
# stops this pin going green by never opening a window at all.
_WINDOW_FILE = '''
import json
import os
import sys
from pathlib import Path

from hscc_daemon import autodown


def _cli_slice():
    return sorted(n for n in sys.modules
                  if n == "hermes_cli" or n.startswith("hermes_cli."))


def test_open_a_real_import_window(tmp_path):
    # The window must open on the tree THIS process resolves hermes from, and
    # the nested run inherits the parent's environment — which under the
    # roster-wide HOME seal carries the runner's DECLARED HERMES_AGENT_PATH
    # (PR #606). The `delenv` that used to open this case was a defensive no-op
    # while nothing exported the pin and became a landmine once the runner did:
    # with HOME moved to an empty /tmp dir the default resolves to
    # '<sealed-home>/.hermes/hermes-agent', the production loader imports
    # nothing, and the non-vacuity assert below reddens a hermetic-runner change
    # as if the t_572b4856 leak were back (measured sealed, t_ce473bf0). Asking
    # the seam keeps file A's premise and the outer gate's premise one and the
    # same question.
    from hscc_daemon import hermes_import
    tree = hermes_import.resolve_agent_tree()
    assert os.path.isdir(tree), (
        "no agent tree to open a window on here — the outer gate should have "
        f"skipped this nested run instead of running it: {tree!r}")
    before = _cli_slice()
    lib = autodown._load_kanban_db_or_default()
    after = _cli_slice()
    marker = os.environ.get("HSCC_T572B4856_WINDOW_MARKER")
    if marker:
        Path(marker).write_text(json.dumps(
            {"before": before, "after": after, "resolved": lib is not None}))
    assert after != before or lib is not None, (
        "the production loader imported nothing through its window — this pin "
        "would be green for the wrong reason")
'''

# File B: same process, NEXT FILE. The answer must be the one the file would get
# ALONE — the cache file A found, not the cache file A's window left behind.
_WINDOW_PROBE_FILE = '''
import json
import os
import sys
from pathlib import Path


def _cli_slice():
    return sorted(n for n in sys.modules
                  if n == "hermes_cli" or n.startswith("hermes_cli."))


def test_the_closed_window_left_nothing_behind():
    marker = os.environ.get("HSCC_T572B4856_WINDOW_MARKER")
    seen = json.loads(Path(marker).read_text()) if marker else {}
    expected = set(seen.get("before", []))
    now = set(_cli_slice())
    assert now == expected, (
        f"hermes_cli modules outlived the window that imported them: "
        f"{sorted(now - expected)} (file A found {sorted(expected)}, ended with "
        f"{sorted(seen.get('after', []))}). A resident hermes_cli carries "
        "__path__ into the real agent tree, so every later import of a hermes "
        "submodule resolves through it and an importorskip answers a question "
        "about test order, not about this interpreter.")
'''


def test_a_closed_import_window_leaves_no_residue_for_the_next_file(tmp_path):
    """THE t_572b4856 regression, run the way it happens: one process, two files.

    Not gated on hermes being *importable* — on thin the point is precisely that
    the parent package imports and the submodule does not, which is the shape
    that used to hand ``hermes_cli`` to the next file. The only thing skipped is
    a host with no agent tree at all, where there is nothing to leak.
    """
    from hscc_daemon import hermes_import
    if not os.path.isdir(hermes_import.resolve_agent_tree()):
        pytest.skip("no Hermes agent tree on this host — no window to leak from")
    rc, out = _run_nested(
        tmp_path, a_file=_WINDOW_FILE, b_file=_WINDOW_PROBE_FILE,
        marker_env=("HSCC_T572B4856_WINDOW_MARKER", "window.json"),
        names=("test_a_window.py", "test_b_window_probe.py"))
    marker = tmp_path / "window.json"
    assert marker.exists(), (
        f"file A never ran — this test would be green for the wrong reason:\n{out}")
    seen = json.loads(marker.read_text())
    assert set(seen["after"]) - set(seen["before"]), (
        f"file A's window imported nothing ({seen}) — the probe below proves "
        "nothing either:\n" + out)
    assert rc == 0, (
        "a hermes_cli imported inside one test's import window reached the "
        f"next file:\n{seen}\n{out}")


def test_no_planted_agent_dir_is_on_sys_path_right_now():
    """Canary for leak 1, checked in the CANONICAL process.

    This file runs after ``test_autodown.py`` in the canonical leg — exactly
    where the fake used to be resident. If a test ever leaves a planted tree on
    the path again, this goes red instead of silently shrinking a later file's
    board enumeration.
    """
    cf = _conftest()
    strays = [p for p in sys.path if cf._is_planted_path(p)]
    assert not strays, (
        "a planted hermes-agent tree is on sys.path for the rest of this "
        f"process: {strays}\n"
        "autodown._load_kanban_db_or_default inserts the resolved agent dir "
        "and never removes it, so whatever sits at sys.path[0] wins for every "
        "later import of hermes_cli.")


def _temp_planted_module(name, tmp_path, rel):
    """A module object whose ``__file__`` really is under a pytest temp root.

    Built under ``tmp_path`` (so it is cleaned with the test) but still inside
    the temp root the detector recognises — that is the whole point: a path
    under the temp root IS what a planted fake looks like.
    """
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    mod = types.ModuleType(name)
    mod.__file__ = str(path)
    assert os.path.realpath(str(path)).startswith(
        os.path.realpath(tempfile.gettempdir())), path
    return mod


def test_poison_detector_names_a_planted_module(tmp_path):
    """Leak 1 half of the detector: a module whose own file is under a tmp dir."""
    cf = _conftest()
    planted = _temp_planted_module("hermes_cli._t2be_planted", tmp_path,
                                   "hermes_cli/kanban_db.py")
    sys.modules["hermes_cli._t2be_planted"] = planted
    try:
        reason = cf._hermes_cli_poison_reason()
        assert reason is not None and "hermes_cli._t2be_planted" in reason, reason
    finally:
        sys.modules.pop("hermes_cli._t2be_planted", None)


def test_poison_detector_names_a_real_module_holding_a_planted_kb(tmp_path):
    """Leak 2 — the shape a plain evict CANNOT repair.

    The real ``hermes_cli.kanban_db_connect`` imported while a fake is resident
    binds ``_kb = <fake>`` for the life of the process; evicting the fake leaves
    a real file with a fake inside it. The detector must catch that even though
    the holder's own ``__file__`` is real.
    """
    cf = _conftest()
    fake = _temp_planted_module("hermes_cli._t2be_fake", tmp_path,
                                "hermes_cli2/kanban_db.py")
    holder = types.ModuleType("hermes_cli._t2be_holder")
    holder.__file__ = str(REAL_AGENT / "hermes_cli" / "kanban_db_connect.py")
    setattr(holder, "_kb", fake)
    # The fake is deliberately NOT in sys.modules: that is the point. The evictor
    # already removed it by name, yet this real module still resolves through it.
    sys.modules["hermes_cli._t2be_holder"] = holder
    try:
        assert "hermes_cli._t2be_fake" not in sys.modules
        reason = cf._hermes_cli_poison_reason()
        assert reason is not None and "_t2be_holder._kb" in reason, reason
    finally:
        sys.modules.pop("hermes_cli._t2be_holder", None)


def test_purging_hands_back_the_pre_test_cache_by_identity():
    """``_purge_planted_hermes_cli`` must not strand the process without the
    modules its earlier tests were given."""
    cf = _conftest()
    kept = types.ModuleType("hermes_cli._t2be_kept")
    sys.modules["hermes_cli._t2be_kept"] = kept
    sys.modules["hermes_cli._t2be_new"] = types.ModuleType("hermes_cli._t2be_new")
    saved = {"hermes_cli._t2be_kept": kept}
    try:
        cf._purge_planted_hermes_cli(saved)
        assert "hermes_cli._t2be_new" not in sys.modules
        assert sys.modules.get("hermes_cli._t2be_kept") is kept
    finally:
        for name in ("hermes_cli._t2be_kept", "hermes_cli._t2be_new"):
            sys.modules.pop(name, None)


# ── the interpreter's own installation is NOT residue (t_572b4856) ────────────
# The first cut of the agent-tree rule classified by prefix alone. On the
# canonical runner the venv lives INSIDE the agent tree
# (~/.hermes/hermes-agent/venv/lib/python3.14/site-packages), so `rich`, `typer`
# and friends read as "hermes residue" and were evicted after every test. The
# next test re-imported a second copy while the first was still referenced and
# rich blew up inside Panel rendering — 5 CLI files / 72 tests, canonical only.


def test_the_agent_tree_rule_excludes_a_venv_nested_in_the_tree(tmp_path):
    """Synthetic version: a `<tree>/venv/lib/pythonX/site-packages` package is
    the interpreter's own, a `<tree>/hermes_cli` module is residue.

    Both halves are asserted against the SAME synthetic tree, so the last line is
    the mutation probe built into the pin: with the interpreter exclusion removed
    (`interp_roots=()`) the venv path reads as residue again, which is exactly the
    bug this test exists to keep dead.
    """
    from hscc_daemon.tests import hermes_cli_plant as plant

    root = tmp_path / "hermes-agent"
    site_pkgs = root / "venv" / "lib" / "python3.14" / "site-packages" / "rich"
    site_pkgs.mkdir(parents=True)
    (site_pkgs / "__init__.py").write_text("")
    src = root / "hermes_cli"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")

    r = str(root.resolve())
    nested_venv = (str(root.resolve() / "venv"),)

    assert plant.is_agent_tree_path(str(src / "__init__.py"), root=r,
                                    interp_roots=nested_venv) is True
    assert plant.is_agent_tree_path(str(site_pkgs / "__init__.py"), root=r,
                                    interp_roots=nested_venv) is False
    assert plant.is_agent_tree_path(str(site_pkgs / "__init__.py"), root=r,
                                    interp_roots=()) is True
    # A package is judged by __path__ too — that is what makes submodules
    # resolvable, so it is the half that must not read as residue either.
    pkg = types.ModuleType("rich")
    pkg.__path__ = [str(site_pkgs)]
    assert plant._is_agent_tree_module(pkg, r, nested_venv) is False


def test_installed_packages_are_never_reported_as_residue():
    """The live version of the same rule, on the interpreter actually running.

    Non-vacuous exactly where the bug was possible: a venv nested under the agent
    tree (the canonical layout). On a sibling interpreter whose prefix is outside
    the tree there is no such overlap, so it says so instead of passing quietly.
    """
    from hscc_daemon.tests import hermes_cli_plant as plant

    root = plant.agent_tree_root()
    nested = str(sys.prefix) == root or str(sys.prefix).startswith(root + os.sep)
    if not nested:
        pytest.skip(f"sys.prefix {sys.prefix!r} is not inside the agent tree "
                    f"{root!r} — no nested-venv overlap to check here")
    under_prefix = {n for n, m in list(sys.modules.items())
                    if getattr(m, "__file__", "")
                    and str(m.__file__).startswith(sys.prefix + os.sep)}
    assert under_prefix, "no installed package resident? the sweep would be vacuous"
    reported = set(plant.resident_under_agent_tree())
    assert reported & under_prefix == set(), (
        f"the interpreter's own packages read as hermes residue: "
        f"{sorted(reported & under_prefix)[:5]}")

