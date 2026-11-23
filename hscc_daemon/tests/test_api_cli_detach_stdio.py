"""`hscc api start` must not keep the caller's stdio — the F6 parity gap (t_22f4a99f).

`api_cli._handle_start` double-forks and ``os.setsid()``s, and the comment at its
fork site claimed "the same double-fork pattern as the daemon (cli.cmd_start)".
Only half of that was true: `cli.cmd_start` calls ``cli._detach_stdio()`` in its
grandchild (F6) and ``api_cli`` never redirected anything —
``grep -n 'os.fork|setsid|dup2|DEVNULL' hscc_daemon/api_cli.py`` on the base tree
returned the forks, the setsid, and no dup2 at all. ``setsid()`` detaches the
SESSION, not the descriptors, so the API grandchild kept the caller's fd 0/1/2.

Two symptoms, one of which is why this was found: a caller that waits for its own
stdout to close — ``subprocess.run(capture_output=True)``, ``hscc api start |
cat``, an editor's shell integration — blocks until the API exits, because the
detached grandchild still holds the write end. t_e5a9469e measured exactly that
shape from the other side (a stray listener that did NOT redirect made the CLI-help
audit's `capture_output` probe block for its whole 60 s window; the same stray with
stdio on DEVNULL returned instantly).

Shape of the guard, and why each piece is here:

* ``TestReusesTheDaemonHelper`` (in-process): the fix must be the daemon's own
  helper, not a second redirect, and it must sit AFTER the re-fork and BEFORE
  ``_serve`` — placement is the whole property (too early and the parent's panel
  is silenced, too late and the loop has already written to the terminal).
* ``TestCallersPipeReachesEof`` runs the real `cmd_api` → `_handle_start` →
  real ``os.fork``/``setsid``/redirect path in a SUBPROCESS under a throwaway
  HOME, with the caller's stdout as a pipe. Only two things are stubbed — the
  config/token resolution and the serve loop — because the real ones bind a
  network port and boot the ``hermes serve`` sidecar, and a serve loop that
  DIES (EADDRINUSE) would let the pipe reach EOF for the wrong reason. The
  stray grandchild therefore stays alive on purpose, so EOF can only mean
  "the descriptor was released".
* ``test_a_stray_holding_the_pipe_is_detected`` is the premise: the same
  harness with the detach neutralised (the pre-fix tree, reproduced by test-side
  patching, never by a product branch) MUST keep blocking. Without it every EOF
  assertion below could pass while being blind.
"""

import json
import os
import select
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hscc_daemon.api_cli as api_cli_mod
from hscc_daemon import cli as daemon_cli

REPO = Path(__file__).resolve().parents[2]

# Ports a real `hscc api start` would own. Held by the test so the strays here
# can never take them, and so a regression that starts binding shows up as a
# connect on our own listening socket instead of as a working API.
TRIPWIRE_PORTS = (8787, 8788)

# How long the (intentionally leaked-until-reaped) stray serve loop lives. Must
# dwarf every window below so "the stray finally died" can never be the reason a
# read returned.
STRAY_SECONDS = 60
# The fixed posture must reach EOF within this. Generous: it covers a full
# interpreter start + import of hscc_daemon + two forks on a loaded CI box.
EOF_DEADLINE = 25.0
# The premise posture must NOT reach EOF within this, with the stray alive.
NO_EOF_WINDOW = 4.0
# Bounded wait for the stray's own report file to appear.
REPORT_DEADLINE = 25.0


# ---------------------------------------------------------------------------
# driver — the code the SUBPROCESS runs (a string, so the poison/patch posture
# is fixed before the product module is imported and cannot be normalised away
# by an in-process fixture; cf. the t_c51c87d7 out-of-process pattern)
# ---------------------------------------------------------------------------

DRIVER = r'''
import json
import os
import stat
import sys
import time

REPO = "@@REPO@@"
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hscc-project"))

import hscc_daemon.api_cli as api_cli

MODE = os.environ["DETACH_MODE"]                  # "start" | "help"
REPORT = os.environ["DETACH_REPORT"]              # where the stray reports itself
LEAK = os.environ.get("DETACH_LEAK") == "1"       # neutralise the detach


def _is_devnull(fd):
    """True when fd currently points at the devnull CHARACTER DEVICE.

    st_rdev identity, not "is it a char device": a tty is a char device too,
    and that is precisely the case this must not accept.
    """
    try:
        st = os.fstat(fd)
    except OSError:
        return False
    return (stat.S_ISCHR(st.st_mode)
            and st.st_rdev == os.stat(os.devnull).st_rdev)


class _FakeApi:
    """Config/token answers only — no port is consulted, no token is minted.

    The real `resolve_config` resolves binds and `load_token` can WRITE
    ~/.hscc/api-token; neither is part of the property under test and both are
    sandbox-side effects this driver must not cause.
    """

    def resolve_config(self, bind_overrides=None, tailscale=False,
                       port_override=None):
        return {"host": "127.0.0.1", "hosts": ["127.0.0.1"], "port": 8787,
                "unavailable": []}

    def load_token(self):
        return "sanitized-dummy-token"


def _stub_serve(api, binds=None, tailscale=False, port_override=None):
    """Stand in for the serve loop IN THE GRANDCHILD, and stay alive.

    Reports the descriptor state at exactly the point the real loop would
    start, then outlives every window the test measures — so the caller's pipe
    can only close because the daemon released it, never because the daemon
    finished.
    """
    payload = {"pid": os.getpid(), "fd0": _is_devnull(0),
               "fd1": _is_devnull(1), "fd2": _is_devnull(2)}
    tmp = REPORT + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, REPORT)
    deadline = time.time() + float(os.environ.get("DETACH_STRAY_SECONDS", "60"))
    while time.time() < deadline:
        time.sleep(0.05)


if MODE == "start":
    api_cli._load_api_server = lambda: _FakeApi()
    api_cli._warn_payload_drift = lambda *a, **k: None
    api_cli._serve = _stub_serve
    if LEAK:
        # Reproduce the PRE-FIX tree: the child keeps the caller's descriptors.
        # Patched here, in the harness, never as a branch inside the product.
        api_cli._detach_stdio = lambda: None
    sys.exit(api_cli.cmd_api(["start", "--no-qr"]))

# MODE == "help": the audit's probe shape (`api start --help` under a captured
# pipe) with a fork landmine under it — the help path must print its usage and
# execute NOTHING, so a fork is a hard failure rather than a silent hang.
def _no_fork(*a, **k):
    raise OSError("FORK_TRIPPED: `api start --help` forked")


os.fork = _no_fork
_rc = api_cli.cmd_api(["start", "--help"])
if os.path.exists(REPORT):
    print("REPORT_WRITTEN: help path started a daemon", file=sys.stderr)
    _rc = 91
sys.exit(_rc)
'''


def _child_env(home: Path, canary: Path) -> dict:
    """A clean-room env: throwaway HOME, canary PATH, no Hermes/conda leak-in."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CONDA_", "HERMES_"))
           and k not in ("COLUMNS", "HSCC_SPARKRUN_BIN", "HSCC_DIR",
                         "HSCC_HOME", "PYTHONHOME")}
    env.update({
        "HOME": str(home),
        "PYTHONPATH": os.pathsep.join([str(REPO), str(REPO / "hscc-project")]),
        "COLUMNS": "120", "NO_COLOR": "1", "TERM": "dumb",
        "HSCC_THEME": "dark",
        "PATH": os.pathsep.join([str(canary), env.get("PATH", "")]),
        "AUDIT_CANARY_LOG": str(canary / "trips.log"),
    })
    return env


@pytest.fixture()
def sandbox(tmp_path):
    """Throwaway HOME + recording PATH canaries (audit-engine safety model)."""
    home = tmp_path / "home"
    (home / ".hscc").mkdir(parents=True)
    # Seeded so module import never shells out to resolve a topology.
    # (192.0.2.1 is the documented TEST-NET placeholder, never a real host.)
    (home / ".hscc" / "cluster.json").write_text(json.dumps(
        {"gateway": {"host": "192.0.2.1"}, "workers": [], "nasDevices": []}))
    canary = tmp_path / "canary-bin"
    canary.mkdir()
    for name in ("sparkrun", "hermes", "gh", "git"):
        p = canary / name
        p.write_text("#!/bin/sh\n"
                     'echo "' + name + ' $*" >> "$AUDIT_CANARY_LOG"\n'
                     "exit 97\n")
        p.chmod(0o755)
    (canary / "trips.log").write_text("")
    return {"home": home, "canary": canary, "tmp": tmp_path,
            "env": _child_env(home, canary)}


@pytest.fixture()
def tripwires():
    """Own the api ports for the duration of a test."""
    socks = {}
    for port in TRIPWIRE_PORTS:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            s.listen(1)
        except OSError:
            s.close()          # already owned elsewhere; the rest still apply
            continue
        socks[port] = s
    yield socks
    for s in socks.values():
        s.close()


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reap(pid, grace=5.0):
    """Terminate a stray grandchild. It installs _sigterm_handler, so SIGTERM
    is the polite verb; SIGKILL is the backstop if it never got that far."""
    if pid is None or not _alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _poll_eof(stream, window):
    """Read *stream* for at most *window* seconds. Returns (eof, bytes_so_far).

    EOF means every write end of the caller's stdout pipe is closed — which for
    a daemonising CLI is the whole property: the detached grandchild either
    still holds it or it does not.
    """
    buf = b""
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        readable, _, _ = select.select([stream], [], [], 0.1)
        if not readable:
            continue
        chunk = os.read(stream.fileno(), 65536)
        if chunk == b"":
            return True, buf
        buf += chunk
    return False, buf


def _wait_for_report(path, proc, deadline=REPORT_DEADLINE):
    """The stray's own descriptor report, or a failure carrying its output."""
    deadline_at = time.monotonic() + deadline
    while time.monotonic() < deadline_at:
        if path.exists():
            return json.loads(path.read_text())
        if proc.poll() is not None and path.exists():
            return json.loads(path.read_text())
        time.sleep(0.05)
    out = b""
    try:
        out = proc.stdout.read() if proc.poll() is not None else b""
    except Exception:
        pass
    pytest.fail(
        "the started daemon never reported (rc="
        f"{proc.returncode})\n--- child output ---\n{out.decode(errors='replace')}")


# ---------------------------------------------------------------------------
# 1. the fix is the daemon's helper, in the right place
# ---------------------------------------------------------------------------

class TestReusesTheDaemonHelper:
    """The card's rule: import the existing helper, do not write a second redirect."""

    def test_api_cli_imports_the_daemons_helper(self):
        """Identity, not just "a function with that name exists".

        A second, locally-written redirect would satisfy the symptom test while
        leaving two detach implementations to drift — the failure mode F6 was
        written about. One helper, both entry points.
        """
        assert api_cli_mod._detach_stdio is daemon_cli._detach_stdio

    def test_grandchild_detaches_after_refork_and_before_serve(self, tmp_path,
                                                              monkeypatch):
        """Placement: after setsid + the re-fork, before save_pid/_serve.

        Fork emulation (the t_52bd498e/test_cli_arg_safety shape): `os.fork`
        answers the way the kernel would, so each branch runs in-process with
        every side effect recorded instead of performed.

        The ordering here is deliberately stricter than the behaviour requires:
        detaching earlier in the child is behaviourally equivalent (the
        grandchild inherits the descriptors), and a mutation that moved the call
        ahead of `setsid()` was caught by THIS case and by nothing else in the
        file (1 failed / 5 passed — the out-of-process EOF cases stayed green).
        That is the point: it pins the parity contract the card names — the same
        point of the same pattern as `cli.cmd_start` — not only the symptom.
        """
        import signal as _sig
        import hscc_daemon.daemon_ops as daemon_ops

        class _Api:
            """Config/token only — `_serve` is stubbed, nothing binds."""

            def resolve_config(self, bind_overrides=None, tailscale=False,
                               port_override=None):
                return {"host": "127.0.0.1", "hosts": ["127.0.0.1"],
                        "port": 8787, "unavailable": []}

            def load_token(self):
                return "sanitized-dummy-token"

        def wire(order, forks):
            monkeypatch.setattr(api_cli_mod, "API_PID_FILE",
                                str(tmp_path / "api.pid"))
            monkeypatch.setattr(api_cli_mod, "_load_api_server", lambda: _Api())
            monkeypatch.setattr(api_cli_mod, "_warn_payload_drift",
                                lambda *a, **k: None)
            monkeypatch.setattr(api_cli_mod, "_serve",
                                lambda *a, **k: order.append("serve"))
            monkeypatch.setattr(api_cli_mod, "_detach_stdio",
                                lambda: order.append("detach"))
            monkeypatch.setattr(daemon_ops, "save_pid",
                                lambda *a, **k: order.append("save_pid"))
            monkeypatch.setattr(daemon_ops, "log", lambda *a, **k: None)

            def fake_fork():
                label, value = next(forks)
                order.append(label)
                return value

            monkeypatch.setattr(os, "fork", fake_fork)
            monkeypatch.setattr(os, "setsid", lambda: order.append("setsid"))
            monkeypatch.setattr(os, "chdir", lambda p: None)
            monkeypatch.setattr(os, "_exit",
                                lambda code=0: order.append("exit"))
            monkeypatch.setattr(_sig, "signal", lambda *a, **k: None)

        # Parent branch (first fork returns the child's pid): the caller keeps
        # its own terminal — the panel it prints after the fork must stay visible.
        parent_order = []
        wire(parent_order, iter([("fork1", 4242)]))
        assert api_cli_mod._handle_start(["--no-qr"]) == 0
        assert "detach" not in parent_order, parent_order
        assert "setsid" not in parent_order, parent_order

        # Child branch: fork -> 0 (child), re-fork -> 0 (grandchild).
        order = []
        wire(order, iter([("fork1", 0), ("fork2", 0)]))
        api_cli_mod._handle_start(["--no-qr"])          # grandchild returns None

        assert order.index("setsid") < order.index("detach"), order
        assert order.index("fork2") < order.index("detach"), order
        assert order.index("detach") < order.index("save_pid"), order
        assert order.index("detach") < order.index("serve"), order


# ---------------------------------------------------------------------------
# 2. the observable property, out of process
# ---------------------------------------------------------------------------

class TestCallersPipeReachesEof:
    """A real double-forked `hscc api start` under a caller whose stdout is a pipe."""

    def _spawn(self, sandbox, *, leak):
        driver = sandbox["tmp"] / "detach_driver.py"
        driver.write_text(DRIVER.replace("@@REPO@@", str(REPO)))
        report = sandbox["tmp"] / "report.json"
        env = dict(sandbox["env"])
        env.update({
            "DETACH_MODE": "start",
            "DETACH_REPORT": str(report),
            "DETACH_LEAK": "1" if leak else "0",
            "DETACH_STRAY_SECONDS": str(STRAY_SECONDS),
        })
        proc = subprocess.Popen(
            [sys.executable, str(driver)], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=env, cwd=str(sandbox["tmp"]))
        return proc, report

    def test_start_releases_the_callers_stdout(self, sandbox, tripwires):
        """EOF reaches the caller while the detached api is STILL RUNNING.

        That pairing is the assertion: EOF alone could also mean "the daemon
        exited", which is a different (and here a false) explanation.
        """
        proc, report = self._spawn(sandbox, leak=False)
        stray = None
        try:
            info = _wait_for_report(report, proc)
            stray = info["pid"]
            assert proc.poll() is not None, (
                "the `hscc api start` caller is still alive — the test would "
                "then be measuring its own fd, not the daemon's")
            assert _alive(stray), "the daemon died; EOF would prove nothing"

            eof, out = _poll_eof(proc.stdout, EOF_DEADLINE)
            assert eof, (
                "the api daemon still holds the caller's stdout write end — a "
                "capturing caller (subprocess.run(capture_output=True), "
                "`hscc api start | cat`) blocks until the API exits")
            assert b"HSCC API started" in out, out
            assert _alive(stray), (
                "EOF arrived only because the daemon exited — the harness "
                "proved nothing about the descriptor")
            # The mechanism, read from inside the daemon itself.
            assert info["fd0"] and info["fd1"] and info["fd2"], (
                "fd 0/1/2 are not the devnull device in the serve loop: "
                f"{info}")
            assert proc.wait(timeout=10) == 0
        finally:
            _reap(stray)
            try:
                proc.stdout.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        # Nothing may have shelled out from the start path either.
        trips = (sandbox["canary"] / "trips.log").read_text().strip()
        assert trips == "", f"`api start` shelled out during the probe: {trips}"

    def test_a_stray_holding_the_pipe_is_detected(self, sandbox, tripwires):
        """PREMISE (positive control): the posture above can fail.

        Same driver, same harness, with the detach neutralised — i.e. the
        pre-fix tree. If this test ever reaches EOF quickly, the EOF assertion
        in the fixed posture is blind and the whole file is decoration.
        """
        proc, report = self._spawn(sandbox, leak=True)
        stray = None
        try:
            info = _wait_for_report(report, proc)
            stray = info["pid"]
            assert proc.poll() is not None
            assert _alive(stray)
            assert not (info["fd0"] and info["fd1"] and info["fd2"]), (
                "the control did not reproduce the leak: fd 0/1/2 are already "
                f"on the null device ({info}) — the harness cannot see the bug")

            eof, out = _poll_eof(proc.stdout, NO_EOF_WINDOW)
            assert not eof, (
                "the harness reached EOF with the stray still attached to the "
                f"pipe (out={out!r}) — the fixed-posture test would pass while "
                "blind")
            assert _alive(stray), (
                "the pipe closed because the stray died, not because it held "
                "the descriptor")
        finally:
            _reap(stray)
            try:
                proc.stdout.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

    def test_no_stray_survives_the_probe(self, sandbox):
        """Hygiene: the reaper used above is the one that keeps CI clean."""
        proc, report = self._spawn(sandbox, leak=False)
        info = _wait_for_report(report, proc)
        stray = info["pid"]
        _poll_eof(proc.stdout, 2.0)
        proc.stdout.close()
        proc.wait(timeout=10)
        assert _reap(stray), f"stray daemon {stray} survived"
        assert not _alive(stray)


# ---------------------------------------------------------------------------
# 3. the audit's own probe shape: `api start --help` under a captured pipe
# ---------------------------------------------------------------------------

class TestHelpCaptureReturnsPromptly:
    """`hscc api start --help` must print and return — never fork, never hold.

    The (D) api-port probe calls this through ``subprocess.run(capture_output=
    True)``; a help path that forks a daemon holding the pipe turns the probe
    into a timeout (t_e5a9469e). The fork landmine in the driver is what gives
    the timing assertion teeth here: a fork is reported as a hard failure even
    on a machine fast enough to dodge the deadline.
    """

    def test_help_capture_is_prompt_and_inert(self, sandbox, tripwires):
        driver = sandbox["tmp"] / "help_driver.py"
        driver.write_text(DRIVER.replace("@@REPO@@", str(REPO)))
        report = sandbox["tmp"] / "help-report.json"
        env = dict(sandbox["env"])
        env.update({"DETACH_MODE": "help", "DETACH_REPORT": str(report)})
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, str(driver)], env=env,
                cwd=str(sandbox["tmp"]), capture_output=True, text=True,
                timeout=30)
        except subprocess.TimeoutExpired:
            pytest.fail("`api start --help` never released the captured pipe")
        elapsed = time.monotonic() - started
        assert proc.returncode == 0, (
            f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
        assert "FORK_TRIPPED" not in proc.stderr + proc.stdout
        assert "Usage: hscc api start" in proc.stdout
        assert not report.exists(), "the help path started a daemon"
        # Prompt, not merely finite: the deadline is far outside the ~1 s a
        # fork-free start takes on a loaded box.
        assert elapsed < 20, f"`api start --help` took {elapsed:.1f}s"
        for port, sock in tripwires.items():
            sock.settimeout(0.1)
            try:
                conn, _ = sock.accept()
                conn.close()
                pytest.fail(f"`api start --help` connected to api port {port}")
            except (socket.timeout, OSError):
                pass
