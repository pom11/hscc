"""Tests for scripts/hermetic_spawn_guard.py — the shared spawn-hermeticity detector.

The per-leg files (``<leg>/tests/test_hermetic_subprocess_spawn.py``) apply the
detector to one leg dir each; these cover the detector's own contract, which all
twelve share. Loaded by path with importlib, the same way
``test_address_guard.py`` loads its detector, so nothing here mutates ``sys.path``
— a suite with a documented history of path leaks should not add one.

What has to be true for the ban to be worth having:

  * it fires on the two shapes it exists for, in every spelling that reaches a
    spawn (inline list, name-indirected, shell string, aliased import);
  * it does NOT fire on the same strings as *data* — several legs embed
    home-prefixed paths as path-parsing fixture data, and a ban that reddens
    those gets deleted rather than fixed (this is the false-positive arm, and
    the reason the sweep is an AST walk over spawn-call arguments and never a
    substring scan);
  * the home threshold does not come from the runtime ``HOME`` alone, because
    ``scripts/tests/conftest.py`` seals ``HOME`` for its whole leg — a detector
    that read only ``Path.home()`` would go blind exactly where it is needed;
  * it fails CLOSED: an unparsable or unreadable source is a violation, and a
    guard that skips is a guard that silences its own watcher.
"""

import importlib.util
import ast
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "hermetic_spawn_guard_under_test",
    REPO / "scripts" / "hermetic_spawn_guard.py")
assert _spec is not None and _spec.loader is not None
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _scan(src, rel="sample.py"):
    return guard.scan_source(textwrap.dedent(src), rel)


def _kinds(src):
    return [v.kind for v in _scan(src)]


# --------------------------------------------------------------------------- #
# rule 1 — a PATH-resolved interpreter argv0
# --------------------------------------------------------------------------- #

def test_argv0_python_is_a_violation():
    src = '''
        import subprocess
        subprocess.run(["python", "script.py"])
        '''
    assert _kinds(src) == [guard.KIND_INTERPRETER]


def test_argv0_python3_is_a_violation():
    src = '''
        import subprocess
        subprocess.Popen(["python3", "-c", "pass"])
        '''
    assert _kinds(src) == [guard.KIND_INTERPRETER]


def test_argv0_python_in_a_shell_string_is_a_violation():
    """`run("python -c ...")` has no list to inspect; argv0 is the first token."""
    src = '''
        import subprocess
        subprocess.run("python -c import rich", shell=True)
        '''
    assert _kinds(src) == [guard.KIND_INTERPRETER]


def test_argv0_reached_through_a_local_name_is_a_violation():
    """The common `cmd = [...]` then `run(cmd)` shape must not be an escape."""
    src = '''
        import subprocess
        cmd = ["python3", "cli.py", "start"]
        subprocess.run(cmd, capture_output=True)
        '''
    # The leading newline of the triple-quoted source makes line 1 blank:
    # 2 = import, 3 = the literal, 4 = the spawn.
    viols = _scan(src)
    assert [v.kind for v in viols] == [guard.KIND_INTERPRETER]
    # The violation reports at the spawn site and names the literal's own line,
    # so a developer sees both the call and the value that made it illegal.
    assert viols[0].lineno == 4
    assert viols[0].origin == "sample.py:3"


def test_aliased_and_from_imported_spawn_names_are_violations():
    """An import alias is not an escape hatch."""
    aliased = '''
        import subprocess as sp
        sp.check_call(["python", "-c", "pass"])
        '''
    from_imported = '''
        from subprocess import run as go
        go(["python3", "-c", "pass"])
        '''
    assert _kinds(aliased) == [guard.KIND_INTERPRETER]
    assert _kinds(from_imported) == [guard.KIND_INTERPRETER]


def test_argv0_through_a_concatenated_literal_is_a_violation():
    src = '''
        import subprocess
        subprocess.run(["py" + "thon3", "x.py"])
        '''
    assert _kinds(src) == [guard.KIND_INTERPRETER]


def test_argv1_being_python_is_not_an_argv0_violation():
    """`[sys.executable, "python"]` is a script that names python, not a lookup."""
    src = '''
        import subprocess, sys
        subprocess.run([sys.executable, "python"])
        '''
    assert _kinds(src) == []


def test_a_name_that_is_not_subprocess_is_not_a_spawn_site():
    """`runner.run(["python"...])` and `Fake.run(...)` are not subprocess."""
    src = '''
        class Fake:
            def run(self, cmd): pass
        Fake().run(["python", "x"])
        '''
    assert _kinds(src) == []


def test_sys_executable_is_clean():
    src = '''
        import subprocess, sys
        subprocess.run([sys.executable, "script.py"], cwd="/tmp/work")
        '''
    assert _kinds(src) == []


def test_a_named_venv_interpreter_is_clean():
    """An explicit interpreter PATH is not a PATH lookup, even if it is python."""
    src = '''
        import subprocess
        subprocess.run(["/usr/bin/python3", "-c", "pass"])
        '''
    assert _kinds(src) == []


# --------------------------------------------------------------------------- #
# rule 2 — a home-prefixed path used as cwd / script
# --------------------------------------------------------------------------- #

def test_home_prefixed_cwd_is_a_violation():
    src = '''
        import subprocess
        subprocess.run(["/usr/bin/true"], cwd="/Users/operator/dev/hscc")
        '''
    assert _kinds(src) == [guard.KIND_HOME_PATH]


def test_home_prefixed_script_path_is_a_violation():
    src = '''
        import subprocess
        subprocess.run(["/usr/bin/python3", "/home/runner/work/hscc/cli.py"])
        '''
    assert _kinds(src) == [guard.KIND_HOME_PATH]


def test_linux_operator_home_shape_is_caught_on_a_mac_runner():
    """The arm that makes the guard useful on CI.

    On the runner ``Path.home()`` is ``/home/runner`` and the passwd record says
    the same, so a hardcoded macOS operator path matches neither — yet that
    literal is exactly what t_bb057914 shipped. The ``/Users/<name>`` spelling is
    what catches it.
    """
    src = '''
        import subprocess
        subprocess.run(["x"], cwd="/Users/someone/dev/hscc")
        '''
    assert _kinds(src) == [guard.KIND_HOME_PATH]


def test_home_shape_arm_works_with_a_foreign_root_set():
    """Pass roots explicitly: the verdict must not depend on this host's home."""
    assert guard.is_home_prefixed("/Users/x/dev/hscc", roots=frozenset({"/home/runner"}))
    assert guard.is_home_prefixed("/home/runner/x", roots=frozenset({"/home/runner"}))
    assert not guard.is_home_prefixed("/tmp/work", roots=frozenset({"/home/runner"}))
    assert not guard.is_home_prefixed("/tmp/Users/x", roots=frozenset({"/home/runner"}))


def test_home_threshold_survives_a_sealed_home(monkeypatch):
    """``scripts/tests/conftest.py`` seals HOME for every test in this leg.

    A threshold read only from ``Path.home()`` would become the sandbox and stop
    matching the operator's path, so the detector would go blind on precisely the
    legs that seal hardest. The passwd record and the account-home spellings are
    the arms that keep it working; this pins that the verdict does not move when
    HOME moves.
    """
    sandbox = "/private/tmp/hscc-sealed-home"
    monkeypatch.setenv("HOME", sandbox)
    # The seal is real: the env-derived home moved, so a Path.home()-only
    # threshold would now be this sandbox.
    assert str(Path.home()) == sandbox
    src = '''
        import subprocess
        subprocess.run(["x"], cwd="/Users/operator/dev/hscc")
        '''
    assert _kinds(src) == [guard.KIND_HOME_PATH]


def test_relative_and_temp_paths_are_clean():
    src = '''
        import subprocess
        subprocess.run(["x"], cwd="work")
        subprocess.run(["x"], cwd="/tmp/hscc-test")
        subprocess.run(["/bin/sh", "-c", "true"], cwd="/")
        '''
    assert _kinds(src) == []


# --------------------------------------------------------------------------- #
# the false-positive arm — the same strings as DATA are not machine reads
# --------------------------------------------------------------------------- #

FIXTURE_DATA_SRC = '''
    """Why ~/dev/hscc and /Users/operator/hscc must never be spawned directly."""

    HOME_DIR = "/Users/operator"
    REGISTRY_ROW = {"repo": "/Users/operator/dev/hscc", "home": "/home/operator"}
    CANDIDATES = ["/Users/operator/.hermes/x", "/home/operator/.hermes/y"]

    def test_path_parsing():
        assert parse("/Users/operator/dev/hscc/cli.py") == "cli.py"

    def test_a_fake_probe_records_a_home_path():
        probe = FakeProbe(ok_paths={"/home/operator/venv/bin/python"})
        assert subprocess.CompletedProcess(["/Users/operator/x"], 0, "", "")
    '''


def test_home_prefixed_fixture_data_is_not_a_violation():
    """The reason this is an AST sweep and not a substring ban.

    ``hscc-project/tests/test_registry.py`` and ``test_kanban.py`` embed
    home-prefixed paths as path-parsing inputs, and docstrings say them out loud
    while explaining why they must not be used. A substring ban reddens all of
    that, and the outcome of a false-positive guard is that someone deletes the
    guard.
    """
    assert _kinds(FIXTURE_DATA_SRC) == []


def test_a_home_path_in_a_log_or_read_call_is_not_a_violation():
    """Only a SPAWN argument is a machine read; a string in a log line is not."""
    src = '''
        import subprocess
        subprocess.check_call(["/bin/ls"], cwd="/tmp/ok")
        log.info("/Users/operator/dev/hscc")
        assert open("/Users/operator/x").read() == ""
        '''
    assert _kinds(src) == []


# --------------------------------------------------------------------------- #
# fail CLOSED — a guard that skips silences its own watcher
# --------------------------------------------------------------------------- #

def test_an_unparsable_source_is_a_violation_not_a_skip(tmp_path):
    bad = tmp_path / "broken.py"
    bad.write_text("def broken(:\n")
    viols = guard.scan_file(bad)
    assert [v.kind for v in viols] == ["unparsable"]


def test_an_unreadable_source_is_a_violation(tmp_path):
    d = tmp_path / "not-a-file.py"
    d.mkdir()
    assert [v.kind for v in guard.scan_file(d)] == ["unreadable"]


def test_the_report_names_the_file_line_and_the_remedy():
    text = guard.report(_scan('''
        import subprocess
        subprocess.run(["python", "x.py"], cwd="/Users/operator/hscc")
        ''', rel="tests/test_x.py"), "hscc-cluster", 42)
    # line 1 of the dedented source is blank, so the spawn is line 3.
    assert "tests/test_x.py:3" in text
    assert "hscc-cluster" in text
    assert "42 test source file(s)" in text
    assert "sys.executable" in text
    assert "__file__" in text


def test_scan_leg_reports_relative_paths(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "import subprocess\nsubprocess.run(['python3', 'x'])\n")
    viols, scanned = guard.scan_leg(tmp_path / "tests")
    assert scanned == 1
    assert viols[0].rel == "test_a.py"


def test_leg_files_skips_pycache(tmp_path):
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "stale.py").write_text("x = 1\n")
    (tmp_path / "test_a.py").write_text("x = 1\n")
    assert [p.name for p in guard.leg_files(tmp_path)] == ["test_a.py"]


# --------------------------------------------------------------------------- #
# the per-leg wiring — the guard must be REGISTERED, and must never skip
# --------------------------------------------------------------------------- #

GUARD_FILE = "tests/test_hermetic_subprocess_spawn.py"

#: Names that would let a guard report green by not looking. A watcher that can
#: skip is a watcher a mutant can silence: pytest counts a skip as "not failed",
#: so the leg stays green while the ban is dead (measured on t_b884497b, where a
#: capability probe mutated to always skip kept the whole guard yellow).
SKIP_NAMES = frozenset({"skip", "skipif", "xfail", "importorskip"})


def _registered_legs():
    """The leg roster, read from the one place that defines it."""
    text = (REPO / "scripts" / "run_tests.sh").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("DIRS=("):
            return stripped[len("DIRS=("):].rstrip(")").split()
    raise AssertionError("no DIRS=( ... ) roster found in scripts/run_tests.sh")


def test_every_registered_leg_ships_a_spawn_guard():
    """A leg that joins the roster without a guard is a leg nobody sweeps.

    Pinned against ``scripts/run_tests.sh``'s own ``DIRS`` list rather than a
    second list here, so the roster cannot grow a blind spot by omission — the
    exact failure that left four legs answering "can this interpreter run
    hermes?" privately (t_b884497b).
    """
    missing = [leg for leg in _registered_legs()
               if not (REPO / leg / GUARD_FILE).is_file()]
    assert not missing, (
        "These legs have no %s: %s" % (GUARD_FILE, ", ".join(missing)))


def test_no_spawn_guard_can_skip_or_xfail_its_own_sweep():
    """Fail-closed wiring, checked structurally, on every leg's guard file."""
    offenders = []
    for leg in _registered_legs():
        path = REPO / leg / GUARD_FILE
        if not path.is_file():
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr in SKIP_NAMES:
                offenders.append("%s:%d pytest.%s" % (leg, node.lineno, node.attr))
            elif isinstance(node, ast.Name) and node.id == "skipTest":
                offenders.append("%s:%d skipTest" % (leg, node.lineno))
    assert not offenders, (
        "A spawn guard that skips silences its own watcher: "
        + ", ".join(offenders))


def test_every_spawn_guard_sweeps_its_own_dir_only():
    """The per-leg contract: each guard names its own file as the sweep root.

    A guard that swept the repo root would apply one leg's fixture-data posture
    to another leg's tests, which is how a ban gets deleted rather than fixed.
    """
    offenders = []
    for leg in _registered_legs():
        path = REPO / leg / GUARD_FILE
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        src = path.read_text(encoding="utf-8")
        if "Path(__file__).resolve().parent" not in src:
            offenders.append("%s: no __file__-derived sweep root" % leg)
        if "rglob" in src and "leg_files" not in src and "scan_leg" not in src:
            offenders.append("%s: sweeps outside the leg helper" % leg)
        assert tree.body, leg
    assert not offenders, ", ".join(offenders)
