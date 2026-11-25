"""t_e1c7c925 — the STOP verb's WRITE must be fenced the way its READ is fenced.

THE HOLE. ``launchd_status`` refuses to ask the operator's live gui domain inside a
pytest process without an injected seam (``_refused_launchd_status``, t_8bf34130).
The STOP verb's write never went through that seam: ``_stop_launchd`` calls
``_launchctl_run(["launchctl", "bootout", f"gui/{uid}/{label}"])`` directly. So a
case that injected ONLY ``launchd_status(_run_probe=...)`` answering ``loaded: True``
and drove ``_handle_stop`` exec'd a real ``launchctl bootout gui/<uid>/com.hscc.api``
— against the operator's RUNNING unit, which on the workstation and on the gate box
IS the HSCC API the merge queue talks through. t_db4d8a74 had to DROP a mutation
probe for exactly this reason. The only defence before this file was a per-file
convention in three test files (``test_api_cli_launchd.py``,
``test_api_cli_stop_sidecar.py``, ``test_api_cli_unverifiable_supervisor_row_...``),
which nothing enforces and the next file can forget.

THE FENCE, AND WHY IT SITS IN PRODUCTION (a decision on the card, not a preference).
``_launchctl_run`` itself asks ``hscc_daemon.suite_fence.under_pytest()`` — the same
predicate the read fence asks — rather than an autouse fixture in this package's
``conftest.py``. Two reasons, both measured:

* the caller that reaches the live seam is often a FRESH interpreter the suite item
  exec'd (``PYTEST_CURRENT_TEST`` crossed the fork, pytest was never imported there —
  the whole finding of t_251cd84f), and a conftest fixture cannot fence that child;
* a conftest fixture fences ``hscc_daemon/tests`` only, while a production guard
  fences every dir in the repo.

And per ``hscc-daemon-ambient-seam-stub``, a fence that REFUSES must not be
default-stubbed away: every case that forgot the seam would then pass for the wrong
reason. So there is NO conftest default here. The escape hatch is the case installing
its own runner — ``monkeypatch.setattr(api_cli, "_launchctl_run", my_runner)``
replaces the function, so the fence is simply not on that path.

WHAT THIS FILE PINS, AND HOW IT STAYS HERMETIC. Nothing here can reach the real
service manager. The in-suite cases spy on ``api_cli.subprocess.run`` itself, so "no
fork" is asserted at the fork, not inferred from a message; the two child cases plant
a FAKE ``launchctl`` first on the child's ``PATH`` (the t_251cd84f shape) whose exec
log is the evidence, and the canned bootout answer is synthesised — no operator path
appears in it and no real pid is ever signalled (``os.kill`` is stubbed in the child).

THE CONTROL IS THE POINT. The marked child must exec the fake ZERO times; the unmarked
child — the operator's shape, the same code with no marker — must exec it once with
the bootout argv and still report "booted out". Without the second half, the first
half would only prove that a branch nothing can enter is not taken.

THE FAIL-OPEN TRAP, pinned separately. ``_stop_launchd`` reads a non-zero rc whose
text contains "could not find" or "not found" as "unit already unloaded" and returns
0. The refusal text must therefore never contain either phrase, or the fence would
turn a refused bootout into a reported success.
"""

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

import hscc_daemon.api_cli as api_cli

#: The repo's refusal convention (``doctor_rows``, ``_refused_supervision_check``,
#: ``_refused_launchd_status``, ``_refused_transport``) — asserted as the WHOLE
#: phrase, and now sourced from the module's own constant so a rewording cannot
#: drift out from under a fragment.
REFUSAL = api_cli.REFUSAL

#: The two phrases ``_stop_launchd`` treats as "already unloaded" (rc 0). A refusal
#: carrying either would fail OPEN.
ALREADY_UNLOADED_PHRASES = ("could not find", "not found")

#: ``<repo>/hscc_daemon/tests/<this file>`` -> repo root, what the child puts on
#: ``sys.path`` so ``import hscc_daemon`` resolves the way the CLI does.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Canned bootout answer: rc 0 means launchd accepted it. Synthesised — no operator
#: path in it, and the pid it later reports gone is a stub, never a real signal.
FAKE_BOOTOUT_OK = "Boot-out successful: 0.\n"

_CHILD = r'''
import json, os, sys
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, sys.argv[1])
out = {"home": os.path.expanduser("~"), "pytest_imported": "pytest" in sys.modules,
       "rc": None, "stdout": "", "stderr": "", "error": None, "kills": []}

try:
    import hscc_daemon.api_cli as api_cli
    from hscc_daemon import api_launchd
except Exception as exc:
    out["error"] = "import: %s: %s" % (type(exc).__name__, exc)
    print(json.dumps(out))
    raise SystemExit(0)

tmp = os.environ["CHILD_TMP"]
# A unit file on disk + a pid file: the two things that make `_launchd_matters()`
# worth a fork, so the verb really does reach the supervisor. Planted under the
# child's own HOME — never the operator's LaunchAgents directory.
plist = os.path.join(tmp, "com.hscc.api.plist")
open(plist, "w").write("declared by the case\n")
api_launchd.API_PLIST_PATH = plist
api_cli.API_PID_FILE = os.path.join(tmp, "api.pid")
open(api_cli.API_PID_FILE, "w").write("42424\n")
api_cli.daemon_ops.log = lambda *a, **k: None
api_cli.os.kill = lambda pid, sig: out["kills"].append([pid, int(sig)])
api_cli._wait_for_pid_gone = lambda pid, **kw: True

# THE HAZARD, EXACTLY AS THE CARD DESCRIBES IT: only the READ is sheamed, and the
# canned answer says the job is loaded. `_launchctl_run` stays the real one.
_real_status = api_cli.launchd_status
_probe_calls = []


def _probe(argv, timeout=10):
    _probe_calls.append([str(a) for a in argv])
    return 0, "GuiJob: com.hscc.api\n\tstate = running\n\n\tpid = 42424\n"


api_cli.launchd_status = lambda **kw: _real_status(_run_probe=_probe)

import io
_o, _e = io.StringIO(), io.StringIO()
with redirect_stdout(_o), redirect_stderr(_e):
    out["rc"] = api_cli.cmd_api(["stop"])
out["stdout"], out["stderr"] = _o.getvalue(), _e.getvalue()
out["probe_calls"] = _probe_calls
try:
    out["pid_file"] = open(api_cli.API_PID_FILE).read().strip()
except OSError:
    out["pid_file"] = None
print(json.dumps(out))
'''

#: A fake launchctl that accepts the bootout and logs its own exec.
_FAKE_LAUNCHCTL = r'''#!/usr/bin/env python3
import os, sys
log = os.environ["FAKE_LAUNCHCTL_LOG"]
with open(log, "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
sys.stdout.write("Boot-out successful: 0.\n")
sys.exit(0)
'''


def _plant(tmp_path):
    """HOME + a PATH whose only ``launchctl`` is this file's fake.

    The fake is FIRST on PATH, ahead of ``/usr/bin``, so no argv this file builds can
    resolve to the real binary — the card forbids verifying this by driving a real
    bootout, because the live unit is the API the merge queue talks through.
    """
    home = tmp_path / "home"
    (home / ".hscc").mkdir(parents=True, exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "launchctl"
    fake.write_text(_FAKE_LAUNCHCTL, encoding="utf-8")
    fake.chmod(0o755)
    return home, bindir, bindir / "launchctl.calls"


def _spawn(tmp_path, *, under_test: bool) -> dict:
    """Drive `hscc api stop` from a child and report which branch it took.

    ``PYTEST_CURRENT_TEST`` is the only variable separating the two halves, which is
    the same channel the operator's own ``hscc api stop`` never carries.
    """
    home, bindir, exec_log = _plant(tmp_path)
    child_dir = tmp_path / "child"
    child_dir.mkdir(exist_ok=True)
    probe = child_dir / "drive_stop.py"
    probe.write_text(_CHILD, encoding="utf-8")

    env = dict(os.environ)
    env.update(HOME=str(home), XDG_CACHE_HOME=str(tmp_path / "cache"),
               PYTHONDONTWRITEBYTECODE="1", CHILD_TMP=str(child_dir),
               PATH=os.pathsep.join([str(bindir), "/usr/bin", "/bin"]),
               FAKE_LAUNCHCTL_LOG=str(exec_log))
    for var in ("PYTEST_CURRENT_TEST", "PYTEST_VERSION", "PYTEST_XDIST_WORKER",
                "HSCC_DIR", "HSCC_HOME", "HSCC_LAUNCHD_PROBE", "HERMES_HOME",
                "HERMES_AGENT_PATH", "HERMES_REAL_HOME", "HERMES_PROFILE",
                "HSCC_TESTS_ENFORCE"):
        env.pop(var, None)
    if under_test:
        env["PYTEST_CURRENT_TEST"] = __name__ + "::drive_stop (call)"

    proc = subprocess.run([sys.executable, str(probe), _REPO],
                          cwd=str(child_dir), capture_output=True, text=True,
                          timeout=600, env=env)
    assert proc.returncode == 0, f"driver died rc={proc.returncode}: {proc.stderr[-900:]}"
    report = json.loads(proc.stdout)
    assert report.get("error") is None, f"the driver failed: {report['error']}"
    assert report["home"] == str(home), (
        f"driver ran under {report['home']!r}, not the planted home — its bootout "
        "target could have been the operator's real gui domain")
    assert report["pytest_imported"] is False, (
        "the driver imported pytest itself: it is not the spawned-child shape and "
        "every assertion below would pass for the wrong reason")
    report["exec_lines"] = (exec_log.read_text().splitlines()
                            if exec_log.exists() else [])
    return report


# --------------------------------------------------------------------------- #
# the in-suite fence: asserted AT THE FORK, not inferred from a message
# --------------------------------------------------------------------------- #

@pytest.fixture()
def _no_fork(monkeypatch):
    """Make a real fork impossible AND observable, for the in-suite cases only.

    Requested EXPLICITLY, never autouse: ``api_cli.subprocess`` IS the shared
    ``subprocess`` module object, so an autouse patch here would also blind the
    child-driving cases below, which must really spawn.

    Every case using it asserts on this record, so "the fence refused" means
    ``subprocess.run`` was never entered — not "the output looked like a refusal".
    """
    calls = []

    def _never(*args, **kwargs):
        calls.append([str(a) for a in (args[0] if args else [])])
        raise AssertionError(f"a real fork reached subprocess.run: {args!r}")

    monkeypatch.setattr(api_cli.subprocess, "run", _never)
    return calls


def test_the_live_seam_refuses_under_pytest_with_no_runner_installed(_no_fork):
    """The fence itself: no fork, a VISIBLE refusal, and not the fail-open wording."""
    rc, out = api_cli._launchctl_run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/com.hscc.api"])
    assert rc is None, f"a refused bootout must not carry a returncode: {rc!r}"
    assert REFUSAL in out, f"the refusal must stay visible: {out!r}"
    assert _no_fork == [], f"the fence forked anyway: {_no_fork}"
    # (None, ...) is this function's "the supervisor could not be asked" channel —
    # the caller must never read a refusal as a statement about the unit.
    for phrase in ALREADY_UNLOADED_PHRASES:
        assert phrase not in out.lower(), (
            f"a refusal carrying {phrase!r} would be read by _stop_launchd as "
            f"'unit already unloaded' and return 0 — the fence failing OPEN: {out!r}")


def test_a_loaded_read_answer_alone_cannot_reach_a_real_bootout(_no_fork, monkeypatch,
                                                                tmp_path):
    """THE CARD'S EXACT HAZARD, in-suite: only the read is sheamed, loaded: True.

    Before t_e1c7c925 this exec'd a real bootout against the operator's unit. Now
    the verb must fail closed — non-zero, visible, the fork never entered, and the
    pid file left ALONE (``write_stopped`` here would be the same false claim
    through the back door, the t_8bf34130 ruling).
    """
    from hscc_daemon import api_launchd

    monkeypatch.setattr(api_cli.sys, "platform", "darwin")
    plist = tmp_path / "com.hscc.api.plist"
    plist.write_text("declared by the case; never read by the verb\n")
    monkeypatch.setattr(api_launchd, "API_PLIST_PATH", str(plist))
    pid_file = tmp_path / "api.pid"
    pid_file.write_text("42424\n")
    monkeypatch.setattr(api_cli, "API_PID_FILE", str(pid_file))
    monkeypatch.setattr(api_cli.daemon_ops, "log", lambda *a, **k: None)
    kill_calls = []
    monkeypatch.setattr(api_cli.os, "kill",
                        lambda pid, sig: kill_calls.append((pid, sig)))

    class _ReadProbe:
        def __init__(self):
            self.calls = []

        def __call__(self, argv, timeout=10):
            self.calls.append([str(a) for a in argv])
            return 0, ("GuiJob: com.hscc.api\n\tstate = running\n\n"
                       "\tpid = 42424\n")

    probe = _ReadProbe()
    real = api_cli.launchd_status
    monkeypatch.setattr(api_cli, "launchd_status",
                        lambda **kw: real(_run_probe=probe))

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = api_cli._handle_stop([])

    assert rc == 1, f"a refused bootout must not report success: rc={rc} {out.getvalue()!r}"
    assert REFUSAL in err.getvalue(), (
        f"the refusal must be visible on stderr: {err.getvalue()!r}")
    assert _no_fork == [], f"the bootout reached the fork anyway: {_no_fork}"
    assert "already unloaded" not in out.getvalue()
    assert "booted out" not in out.getvalue()
    # The read seam WAS consulted and DID answer loaded — this is the case where the
    # verb knew the unit was loaded and still had to refuse the write.
    assert [c[1] for c in probe.calls] == ["print"], (
        f"the read seam should have been asked exactly once: {probe.calls}")
    assert pid_file.read_text().strip() == "42424", (
        "a refused stop must leave the pid file alone")
    # `daemon_ops.get_pid` does a `kill(pid, 0)` liveness probe on the way in, so
    # "must not touch the process" means no SIGTERM/SIGKILL — the same filter
    # test_api_cli_launchd.py:_signals uses, and for the same reason.
    import signal as _signal
    assert [sig for _p, sig in kill_calls
            if sig in (_signal.SIGTERM, _signal.SIGKILL)] == [], (
        f"a refused stop must not signal the pid: {kill_calls}")


def test_installing_a_runner_is_the_escape_hatch(monkeypatch, tmp_path):
    """A case that installs its own runner passes, unchanged, and pins its argv.

    This is why the fence needs no new parameter and why the three pre-existing
    files needed no edits: ``monkeypatch.setattr`` REPLACES ``_launchctl_run``, so
    the fence is not on the path at all.
    """
    from hscc_daemon import api_launchd

    monkeypatch.setattr(api_cli.sys, "platform", "darwin")
    plist = tmp_path / "com.hscc.api.plist"
    plist.write_text("declared by the case\n")
    monkeypatch.setattr(api_launchd, "API_PLIST_PATH", str(plist))
    pid_file = tmp_path / "api.pid"
    pid_file.write_text("42424\n")
    monkeypatch.setattr(api_cli, "API_PID_FILE", str(pid_file))
    monkeypatch.setattr(api_cli.daemon_ops, "log", lambda *a, **k: None)
    monkeypatch.setattr(api_cli.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(api_cli, "_wait_for_pid_gone", lambda pid, **kw: True)

    class _ReadProbe:
        def __call__(self, argv, timeout=10):
            return 0, ("GuiJob: com.hscc.api\n\tstate = running\n\n"
                       "\tpid = 42424\n")

    real = api_cli.launchd_status
    monkeypatch.setattr(api_cli, "launchd_status",
                        lambda **kw: real(_run_probe=_ReadProbe()))

    boots = []

    def _runner(argv, timeout=10):
        boots.append([str(a) for a in argv])
        return 0, FAKE_BOOTOUT_OK

    monkeypatch.setattr(api_cli, "_launchctl_run", _runner)

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = api_cli._handle_stop([])

    assert rc == 0, f"an installed runner must drive the real verb: rc={rc} {err.getvalue()!r}"
    assert boots == [["launchctl", "bootout", f"gui/{os.getuid()}/com.hscc.api"]], boots
    assert "booted out" in out.getvalue()


# --------------------------------------------------------------------------- #
# the two children: the fence, and the control that proves it is a fence
# --------------------------------------------------------------------------- #

def test_the_bootout_is_refused_in_a_child_the_suite_spawned(tmp_path):
    """The arm a conftest fixture could never cover.

    ``PYTEST_CURRENT_TEST`` crossed the fork and pytest was never imported here, so
    the only thing protecting the operator's unit in a spawned ``hscc api stop`` is
    the production fence asking ``suite_fence``. The fake binary's exec log is the
    evidence: it must be EMPTY.
    """
    report = _spawn(tmp_path, under_test=True)
    assert report["exec_lines"] == [], (
        "a suite-spawned stop exec'd launchctl — the operator's live unit was "
        f"reachable: {report['exec_lines']}")
    assert REFUSAL in report["stderr"], (
        f"the refusal must be visible in the child's own output: {report['stderr']!r}")
    assert report["rc"] == 1, f"a refused bootout must exit non-zero: {report['rc']}"
    assert "booted out" not in report["stdout"]
    # The read WAS asked and DID answer loaded (the seam took the argv), so the zero
    # above is the write fence refusing, not a verb that never got as far as asking.
    assert report["probe_calls"] == [["launchctl", "print",
                                      f"gui/{os.getuid()}/com.hscc.api"]], (
        f"the read seam should have been consulted once: {report['probe_calls']}")


def test_the_operator_path_still_boots_the_unit_out(tmp_path):
    """THE CONTROL: without the marker the same code really boots out — once.

    This is what makes the assertion above a fence rather than a dead branch, and it
    is the card's "the operator path must keep working unchanged". It execs only
    this file's fake (PATH is planted), and it is the ONLY case here that may exec.
    """
    report = _spawn(tmp_path, under_test=False)
    assert report["exec_lines"] == [
        f"bootout gui/{os.getuid()}/com.hscc.api"], (
        "the operator path did not boot the unit out through the fake — the "
        f"refusal above proves nothing: {report['exec_lines']}")
    assert REFUSAL not in report["stderr"], (
        f"the operator path was refused: {report['stderr']!r}")
    assert report["rc"] == 0, (
        f"an accepted bootout must report success: rc={report['rc']} "
        f"{report['stdout']!r} {report['stderr']!r}")
    assert "booted out" in report["stdout"]
    # KeepAlive rule: bootout, never a resurrectable SIGTERM — even here, where the
    # verb really execs. Only signal 0 liveness probes may appear.
    assert report["kills"] == [] or all(k[1] == 0 for k in report["kills"]), (
        f"the operator path signalled a supervised pid: {report['kills']}")


# --------------------------------------------------------------------------- #
# the predicate: refusing because a suite is driving is a property of the
# process AND of any process it spawned, so the site must ASK the shared copy
# --------------------------------------------------------------------------- #

def test_the_write_site_asks_the_shared_predicate_and_not_a_private_copy(
        monkeypatch):
    """Both signals, and a call count on the shared predicate (t_fdbc24f9's ruling).

    A private ``"pytest" in sys.modules`` copy answers correctly in the suite process
    and misses every child; a copy that answers correctly while never ASKING
    ``suite_fence`` can drift from it silently. The delegation spy is what
    ``test_api_cli_launchd_fence_reaches_children_t_251cd84f.py`` uses for the read
    site; this pins the write site the same way.
    """
    from hscc_daemon import suite_fence

    calls = []
    _asked = suite_fence.under_pytest
    monkeypatch.setattr(suite_fence, "under_pytest",
                        lambda: (calls.append(1), _asked())[1])
    # Patch the FORK, never `_launchctl_run` itself: replacing that function replaces
    # the fence this row is pinning and every answer below would read "not refused".
    class _Cp:
        returncode, stdout, stderr = 0, "\tpid = 1\n", ""

    forks = []

    def _fake_run(argv, **kw):
        forks.append([str(a) for a in argv])
        return _Cp()

    monkeypatch.setattr(api_cli.subprocess, "run", _fake_run)

    for in_modules, marker, expected_refusal in (
            (True, False, True),     # the suite process, outside an item
            (False, True, True),     # a child a suite item spawned
            (True, True, True),      # a child of a suite that also imported pytest
            (False, False, False),   # a plain operator run: must never be refused
    ):
        calls.clear()
        forks.clear()
        if in_modules:
            monkeypatch.setitem(sys.modules, "pytest",
                                sys.modules.get("pytest", object()))
        else:
            monkeypatch.delitem(sys.modules, "pytest", raising=False)
        if marker:
            monkeypatch.setenv("PYTEST_CURRENT_TEST", "t_e1c7c925::pin (call)")
        else:
            monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

        rc, out = api_cli._launchctl_run(["launchctl", "bootout", "gui/1/x"])
        refused = rc is None and REFUSAL in (out or "")
        assert refused is expected_refusal, (
            f"pytest={in_modules} marker={marker}: expected refusal="
            f"{expected_refusal}, got {refused}: {out!r}")
        assert (forks == []) is expected_refusal, (
            f"pytest={in_modules} marker={marker}: the fork itself must follow the "
            f"refusal, got {forks}")
        assert calls, "the write site must ASK hscc_daemon.suite_fence, not keep a copy"


def test_the_refusal_phrase_is_the_repo_convention():
    """The read fence and the write fence must say the same sentence.

    Four other refusals in the repo carry this exact phrase (``doctor_rows``,
    ``_refused_supervision_check``, ``_refused_transport``), and a test in
    hscc-bootstrap asserts on it verbatim. A write fence that reworded it would
    silently split the convention.
    """
    assert REFUSAL == "refused inside a pytest process"
    read_note = api_cli._refused_launchd_status()["note"]
    assert REFUSAL in read_note, (
        f"the read refusal no longer carries the shared phrase: {read_note!r}")
    assert api_cli._refused_launchd_status()["refused"] is True
