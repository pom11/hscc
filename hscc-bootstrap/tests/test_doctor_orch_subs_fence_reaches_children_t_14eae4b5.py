"""t_14eae4b5 — the orch_wake_subs fence must reach a process the suite SPAWNED.

The door the agent tool relays is a separate interpreter:
``doctor_tools.doctor_findings`` execs ``python3 hscc-bootstrap/doctor.py --json``
(a flat sibling import only resolves when the script runs as a script), so every
row doctor.py renders — this one included — evaluates in a child process in
which pytest was never imported.

That is what made the leg red. ``"pytest" in sys.modules`` is process-local, so
the fence read as though it had fired while doing nothing at all: the child took
the ambient path, enumerated the operator's boards, opened them, and appended
``hscc_daemon.autodown._note_kanban_unavailable``'s once-per-process WARN to
``$HOME/.hscc/daemon.log``. With HOME sealed by the caller — which is exactly
what ``doctor_tools``' own read-only suite does — that WARN is a new file inside
the sandbox, and the guard that caught it says so:

    hscc-cluster/tests/test_doctor_tool.py::TestReadOnly::
        test_the_read_writes_nothing_anywhere
    AssertionError: a read wrote into the sandbox: ['.hscc/daemon.log']

Both halves are executed here, not asserted: the same probe script is run once
with the marker pytest exports (it must refuse and write nothing) and once
without it (it must take the ambient path and produce that very file). The
second run is what proves the marker is the difference, rather than a tmp HOME
happening to have nothing to read.

Hermetic: the child gets a planted HOME with no agent tree, so its ambient path
resolves ``~/.hermes/hermes-agent`` inside the sandbox and never reaches a live
board, a gateway or the operator's ``~/.hscc`` — and behaves identically under a
thin interpreter and under hermes' own venv. The only host binary it execs is
this interpreter itself.
"""

import json
import os
import subprocess
import sys

import doctor_orch_subs as dos

#: A fresh interpreter that imports this module the way the deployed tree does
#: (the plugin dir on ``sys.path``) and calls the row with NOTHING injected —
#: the exact call ``doctor.py`` makes when no seam was handed to it.
_CHILD = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])   # the plugin dir (deployed layout)\n"
    "sys.path.insert(0, sys.argv[2])   # the repo root, so `hscc_daemon` resolves\n"
    "import doctor_orch_subs as dos\n"
    "print(json.dumps(dos.doctor_check()))\n"
)

#: ``<root>/hscc-bootstrap/doctor_orch_subs.py`` → ``<root>``, the same root
#: ``doctor.py`` puts on the path before a row may import the daemon package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(dos.__file__)))

#: What the door's read-only guard found, verbatim: the one file the ambient
#: path leaves behind in a sealed HOME.
_LOG_REL = os.path.join(".hscc", "daemon.log")


def _spawn_row(tmp_path, *, under_test: bool):
    """Run the uninjected row in a FRESH interpreter under a planted HOME.

    Returns ``(verdict, files_the_child_created_under_that_home)``. Nothing here
    can reach the operator's trees: HOME/HERMES_HOME are set for the child only,
    and the planted home has no agent tree, so the child's board enumeration
    fails closed inside the sandbox.
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
    files = sorted(str(p.relative_to(home))
                   for p in home.rglob("*") if p.is_file())
    assert proc.returncode == 0, f"probe died: {proc.stderr[-800:]}"
    return json.loads(proc.stdout), files


def test_a_spawn_of_the_suite_is_inside_the_suite(tmp_path):
    """The fence's whole purpose is "no suite may read the operator's boards".

    A test that spawns the door IS a suite reading them, through a child it
    cannot patch — so the child must refuse, and must leave the sealed HOME
    byte-unchanged (the WARN into ``.hscc/daemon.log`` is what the door's own
    read-only guard failed on).
    """
    verdict, files = _spawn_row(tmp_path, under_test=True)
    assert "refused" in verdict["detail"], (
        "the fence did not reach the child: it took the ambient read path, "
        "which enumerates and opens the operator's boards — "
        f"{verdict['detail'][:200]}")
    assert verdict["name"] == dos.CHECK_NAME
    assert files == [], (
        f"a refused read wrote into the sealed HOME: {files}")


def test_without_the_marker_the_same_child_reads_for_real(tmp_path):
    """Control: the assertion above bites, and the marker is what stops it.

    Same probe, same planted HOME, only ``PYTEST_CURRENT_TEST`` removed — the
    child must then take the ambient path (it cannot refuse) and must leave the
    WARN file the door's read-only guard caught. If this ever goes green by
    NOT writing, the fence test above is guarding nothing and this file has to
    say what replaced the write.
    """
    verdict, files = _spawn_row(tmp_path, under_test=False)
    assert "refused" not in verdict["detail"], (
        "the child refused without any pytest in sight — the fence now denies "
        "the operator's own doctor run, which is the row's only real use")
    assert _LOG_REL in files, (
        f"expected the ambient path to leave {_LOG_REL!r} behind (the regression "
        f"this card fixes); got {files} — the write moved, so re-read t_14eae4b5 "
        "before trusting the test above")
    line = (tmp_path / "home" / _LOG_REL).read_text(encoding="utf-8")
    assert "Autodown kanban interlock unevaluable" in line, line[:300]
