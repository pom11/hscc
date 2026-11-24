"""t_14eae4b5 — the orch_wake_subs fence must reach a process the suite SPAWNED.

The door the agent tool relays is a separate interpreter:
``doctor_tools.doctor_findings`` execs ``python3 hscc-bootstrap/doctor.py --json``
(a flat sibling import only resolves when the script runs as a script), so every
row doctor.py renders — this one included — evaluates in a child process in
which pytest was never imported. ``"pytest" in sys.modules`` is process-local, so
the fence read as though it had fired while refusing nothing at all, and the
child took the ambient path: Hermes' board enumeration, which opens the
operator's boards.

That is how the defect was caught. The door's own read-only guard went red the
moment this row was registered:

    hscc-cluster/tests/test_doctor_tool.py::TestReadOnly::
        test_the_read_writes_nothing_anywhere
    AssertionError: a read wrote into the sandbox: ['.hscc/daemon.log']

The WARN that file came from was a *symptom* of the read. t_2c218de8 made the
row's borrowed call quiet (``doctor_orch_subs._silent_read``), which is right on
its own terms — a doctor read writes nothing anywhere, for the operator too — but
it also removed the only evidence a file-diffing guard could see. The ambient
read itself survived. So this file pins the read, not the log line: the probe
child counts how many times the row reached ``autodown.board_db_paths`` (the
repo's ONE board enumeration) and the two halves differ on that count alone.

Both halves run here, so neither is a tautology: the same probe script under the
marker pytest exports (must refuse, must never reach the enumeration, must leave
the sealed HOME byte-unchanged) and without it (must take the ambient path, must
reach the enumeration). The second run is what proves the marker is the
difference, rather than a tmp HOME that happens to hold nothing worth reading.

Hermetic: the child gets a planted HOME and HERMES_HOME with no agent tree, so
its ambient path resolves inside the sandbox and cannot reach a live board, a
gateway or the operator's ``~/.hscc`` — and it behaves identically under a thin
interpreter and under hermes' own venv, because the assertion is about which
branch the row took, not about whether Hermes' library was importable. The only
host binary this execs is this interpreter itself, and the child's own HOME is
echoed back and checked, so a broken seal fails here rather than silently
reaching a real board.
"""

import json
import os
import subprocess
import sys

import doctor_orch_subs as dos

#: A fresh interpreter that imports this module the way the deployed tree does
#: (the plugin dir on ``sys.path``) and calls the row with NOTHING injected —
#: the exact call ``doctor.py`` makes when no seam was handed to it.
#:
#: ``board_db_paths`` is wrapped before the row runs so the child can report
#: whether the row reached the live enumeration. ``enumerate_boards`` imports the
#: daemon module lazily (``from hscc_daemon import autodown``), which returns
#: this same module object, so the wrapper sees the row's own call and not a
#: second import of it.
_CHILD = (
    "import json, os, sys\n"
    "sys.path.insert(0, sys.argv[1])   # the plugin dir (deployed layout)\n"
    "sys.path.insert(0, sys.argv[2])   # the repo root, so `hscc_daemon` resolves\n"
    "from hscc_daemon import autodown\n"
    "seen = []\n"
    "real = autodown.board_db_paths\n"
    "def spy(*a, **k):\n"
    "    seen.append(1)\n"
    "    return real(*a, **k)\n"
    "autodown.board_db_paths = spy\n"
    "import doctor_orch_subs as dos\n"
    "print(json.dumps({'verdict': dos.doctor_check(),\n"
    "                  'enum_calls': len(seen),\n"
    "                  'home': os.environ.get('HOME')}))\n"
)

#: ``<root>/hscc-bootstrap/doctor_orch_subs.py`` → ``<root>``, the same root
#: ``doctor.py`` puts on the path before a row may import the daemon package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(dos.__file__)))


def _spawn_row(tmp_path, *, under_test: bool) -> dict:
    """Run the uninjected row in a FRESH interpreter under a planted HOME.

    Returns the child's report: ``verdict`` (the row's own dict),
    ``enum_calls`` (how often it reached the live board enumeration) and
    ``files`` (everything the child created under that HOME). Nothing here can
    reach the operator's trees: HOME/HERMES_HOME are set for the child only, the
    agent-tree pointer is removed rather than redirected, and the planted home
    has no boards in it — so the child's ambient path fails closed inside the
    sandbox.
    """
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".hscc").mkdir()
    probe = tmp_path / "probe_uninjected_row.py"
    probe.write_text(_CHILD, encoding="utf-8")
    env = dict(os.environ)
    env.update(HOME=str(home), HERMES_HOME=str(home / ".hermes"),
               XDG_CACHE_HOME=str(tmp_path / "cache"),
               PATH="/usr/bin:/bin:/usr/sbin:/sbin")
    # An agent tree the caller resolved would let the child's enumeration find
    # real boards, so it is dropped — the child must fail the same way CI's thin
    # leg interpreter does (no importable hermes_cli at all).
    for var in ("HERMES_AGENT_PATH", "HSCC_DIR", "HSCC_HOME",
                "HERMES_REAL_HOME", "PYTEST_CURRENT_TEST"):
        env.pop(var, None)
    if under_test:
        # What pytest itself exports for the duration of every item, and what
        # every subprocess of that item therefore inherits.
        env["PYTEST_CURRENT_TEST"] = (__name__ + "::probe (call)")
    proc = subprocess.run(
        [sys.executable, str(probe), os.path.dirname(os.path.abspath(dos.__file__)),
         _REPO_ROOT],
        cwd=str(tmp_path), capture_output=True, text=True,
        timeout=180, env=env)
    assert proc.returncode == 0, f"probe died: {proc.stderr[-800:]}"
    report = json.loads(proc.stdout)
    # The seal is the whole safety argument, so the child testifies to it.
    assert report["home"] == str(home), (
        f"the probe ran under {report['home']!r}, not the planted home — the "
        "child could have reached the operator's boards")
    report["files"] = sorted(str(p.relative_to(home))
                             for p in home.rglob("*") if p.is_file())
    return report


def test_a_spawn_of_the_suite_is_inside_the_suite(tmp_path):
    """The fence's whole purpose is "no suite may read the operator's boards".

    A test that spawns the door IS a suite reading them, through a child it
    cannot patch — so the child must refuse, and must never reach the board
    enumeration at all (refusing before the call is what makes "no write" a
    consequence rather than a second thing to arrange).
    """
    r = _spawn_row(tmp_path, under_test=True)
    assert "refused" in r["verdict"]["detail"], (
        "the fence did not reach the child: it took the ambient read path, "
        "which enumerates and opens the operator's boards — "
        f"{r['verdict']['detail'][:200]}")
    assert r["verdict"]["name"] == dos.CHECK_NAME
    assert r["enum_calls"] == 0, (
        f"the refused row still reached the live board enumeration "
        f"{r['enum_calls']} time(s)")
    assert r["files"] == [], (
        f"a refused read wrote into the sealed HOME: {r['files']}")


def test_without_the_marker_the_same_child_reaches_the_enumeration(tmp_path):
    """Control: the assertion above bites, and the marker is what stops it.

    Same probe, same planted HOME, only ``PYTEST_CURRENT_TEST`` removed — the
    child must then take the ambient path (it cannot refuse) and must reach the
    board enumeration, which is the read the fence exists to prevent.

    Deliberately NOT asserted here: whether that read leaves a log line behind.
    t_2c218de8 made the borrowed call quiet, so the file is no longer the
    marker of the read, and pinning it would make this file depend on a
    different lane's ruling about logging. "A doctor read writes nothing
    anywhere" is asserted where it belongs — by the door's own read-only guard
    in ``hscc-cluster/tests/test_doctor_tool.py``.
    """
    r = _spawn_row(tmp_path, under_test=False)
    assert "refused" not in r["verdict"]["detail"], (
        "the child refused without any pytest in sight — the fence now denies "
        "the operator's own doctor run, which is the row's only real use")
    assert r["enum_calls"] >= 1, (
        "the control took neither branch: the row reported "
        f"{r['verdict']['detail'][:200]!r} without reaching the enumeration, so "
        "the fence test above is guarding a branch nothing can take — re-read "
        "t_14eae4b5 before trusting it")
