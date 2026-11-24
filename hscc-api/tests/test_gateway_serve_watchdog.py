"""A SIGKILLed API process must not leave the gateway sidecar running.

WHY THIS FILE EXISTS (card t_ed2f1abe, split out of t_56552ecc).
``gateway_bridge.shutdown()`` — called from the one ``finally:`` in
``hscc_daemon/api_cli._serve`` — used to be the ONLY thing that ever stopped the
supervised ``hermes serve``. That covers the cooperative stop (``hscc api stop``
SIGTERMs, the handler sets its flag, the ``finally:`` runs). It covers nothing
else: ``hscc api stop`` itself escalates to SIGKILL after its grace window, and a
crash or a teardown that blocks has the same shape. A parent that dies without
running that ``finally:`` leaves the serve reparented to launchd, still holding
the dead API's session token — the orphaned-sidecar / stale-token 403 that
``start_serve``'s port-occupancy heal then has to clean up on the next start.

The fix moves the durability into the sidecar's own supervision: the serve is
spawned under ``gateway_serve_watchdog.py``, which holds the read end of a pipe
whose write end only the API process keeps. The kernel returns EOF to it the
instant the API's fd table is destroyed, whatever destroyed it.

WHAT IS RUN FOR REAL, AND THE BOUND. The parent is a real process running the
real ``gateway_bridge.start_serve``; the sidecar is a real subprocess bound to a
real ephemeral loopback port. The assertion is that the sidecar's PID is GONE
(plus the port stops answering), not that some flag was set.

Measured on this host (5 trials per arm, real SIGKILL of the parent, chain
parent -> watchdog -> serve; ``changelog.d`` carries the same table):

    detection, liveness pipe (shipped primary)    0.0008 - 0.0010 s
    detection, reparenting poll (fallback, 0.25)  0.2366 - 0.3387 s
    serve truly gone, pipe arm                    0.065  - 0.174  s
    serve truly gone, fallback arm                0.290  - 0.504  s
    serve truly gone, fallback at a 1.0 s tick    0.953  - 1.157  s

SO THE BLIND SPOT ACCEPTED, both numbers: detection costs ~1 ms on the shipped
pipe path and ``_DEFAULT_POLL_S`` = 0.25 s in the reparenting-poll fallback (a
coarser tick costs exactly that much more — the 1.0 s arm above). Teardown after
detection costs up to 2.0 s of SIGTERM grace + 1.0 s of SIGKILL grace = 3.0 s,
which is what a serve that ignores SIGTERM costs. Worst case end to end is
0.25 + 3.0 = 3.25 s, and ``_SIDECAR_GONE_BOUND_S`` asserts that with slack; it is
derived from those constants (see the invariant test at the end), not tuned.

Hermetic by construction, and never the live gateway: the parent runs with HOME
and HERMES_HOME redirected into the sandbox, the sidecar is a fake ``serve`` the
test writes (the real ``hermes serve`` binary is never named), the port is
ephemeral loopback, the session token is a fixture string, and the parent FENCES
``launchctl`` — any argv containing it raises, so a future edit that reaches for
the operator's gui domain reddens here instead of touching the machine
(``gateway_bridge`` has no launchd surface today; the fence keeps it that way).
"""
import errno
import importlib.util
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import gateway_bridge as bridge

REPO = Path(__file__).resolve().parents[2]
API_DIR = REPO / "hscc-api"
WATCHDOG = API_DIR / "gateway_serve_watchdog.py"

#: Linux exposes per-process truth here; macOS does not have this directory, and
#: the branch in _state_and_command is the whole difference between the two.
_PROC_DIR = Path("/proc")

#: The env name the watchdog reads. Read through ``getattr`` with the literal
#: default on purpose: the harness must still run (and still fail for the
#: behavioural reason) on a tree where the seam does not exist yet, so that the
#: red-at-base arm demonstrates an orphan, not an ImportError.
LIVENESS_ENV = getattr(bridge, "ENV_LIVENESS_FD", "HSCC_GATEWAY_LIVENESS_FD")

#: Fixture DATA, never a real credential, and never printed in a message.
FIXTURE_TOKEN = "sandbox-dummy-token-for-watchdog"

#: The bound the acceptance cases assert. Derived, not tuned: the fallback's
#: detection blind spot (0.25 s) + the watchdog's TERM grace (2.0 s) + its KILL
#: grace (1.0 s) = 3.25 s worst case, plus slack for a loaded CI host. Measured
#: here: 0.065 - 0.504 s; the shipped arm never got near 1 s.
_SIDECAR_GONE_BOUND_S = 4.0

#: How long to wait for the fake sidecar to come up before calling a case broken
#: (that is the API's own readiness probe, not the bound above).
_SIDECAR_READY_TIMEOUT_S = 25.0

# Marker lines the sandbox parent writes. Constants, so that a rename reddens the
# coupling between harness and assertion instead of turning a case into a
# 25 s timeout.
READY = "sandbox: serve up"
SHUTDOWN_DONE = "sandbox: shutdown done"


def _load_watchdog():
    """Import the shipped watchdog module by path (it is a script, not a pkg)."""
    spec = importlib.util.spec_from_file_location(
        "gateway_serve_watchdog", str(WATCHDOG))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def reset_bridge_state():
    """Same isolation the bridge's own tests use, plus the new fd state."""
    bridge._reset_for_tests()
    yield
    bridge._reset_for_tests()


# --------------------------------------------------------------------------- #
# the fake sidecar: a real subprocess, so "gone" means a dead pid
# --------------------------------------------------------------------------- #

FAKE_SERVE = '''\
"""Stand-in for ``hermes serve``, written by the test. Binds the port it is
told (``--port N``, the argument the real serve takes) and reports its pid, so
the sidecar's death is observable as a dead pid and a closed port. Records the
signal that killed it. The report path comes from the env, because ``argv``
belongs to the serve contract, not to the harness.
"""
import json
import os
import select
import signal
import socket
import sys
import time

report_path = os.environ["FAKE_SERVE_REPORT"]
sig_log = os.path.join(os.path.dirname(report_path), "signals.log")

argv = sys.argv[1:]
port = 0
if "--port" in argv:
    try:
        port = int(argv[argv.index("--port") + 1])
    except (ValueError, IndexError):
        port = 0


def _seen(signum, _frame):
    with open(sig_log, "a") as fh:
        fh.write("%s %s\\n" % (time.time(), signum))
        fh.flush()
        os.fsync(fh.fileno())
    raise SystemExit(0)


signal.signal(signal.SIGTERM, _seen)

sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", port))
sock.listen(8)

# Hygiene probe: whatever the watchdog hands down it must NOT hand further down.
# The env NAME being absent from this process is the exact statement — an fd
# number could collide with an unrelated fd here, a leaked env name cannot.
report = {
    "pid": os.getpid(),
    "pgid": os.getpgid(0),
    "port": sock.getsockname()[1],
    "requested_port": port,
    "liveness_env_in_serve": os.environ.get("@@ENV_LIVENESS@@"),
}
with open(report_path, "w") as fh:
    json.dump(report, fh)
    fh.flush()
    os.fsync(fh.fileno())

# Accept and drop, the way a real serve does. A listener that never accepts
# fills its backlog and then answers nothing, and "the port stopped answering"
# stops meaning anything if it never answered in the first place.
while True:
    readable, _, _ = select.select([sock], [], [], 0.05)
    if readable:
        try:
            conn, _addr = sock.accept()
            conn.close()
        except OSError:
            pass
'''


# --------------------------------------------------------------------------- #
# the fake API process: imports the real bridge, spawns the real chain
# --------------------------------------------------------------------------- #

FAKE_API_PARENT = '''\
"""Stand-in for the API process, written by the test. Runs the REAL
``gateway_bridge.start_serve`` (so the real watchdog + liveness-pipe wiring is
what gets measured), then either sits until the harness SIGKILLs it (mode
``serve``) or calls the real ``shutdown()`` (mode ``stop``).
"""
import os
import subprocess
import sys
import time

SANDBOX = os.environ["SANDBOX_DIR"]
MARKS = os.path.join(SANDBOX, "marks.log")
REPORT = os.path.join(SANDBOX, "serve_report.json")
SUPERVISED = os.path.join(SANDBOX, "supervised.pid")


# FENCE: nothing in this sandbox may reach the operator's launchd gui domain.
# subprocess.run resolves Popen through this module global, so a subclass covers
# both call shapes.
_RealPopen = subprocess.Popen


class _FencedPopen(_RealPopen):
    def __init__(self, args, *a, **k):
        argv = args if isinstance(args, (list, tuple)) else [args]
        if any("launchctl" in str(x) for x in argv):
            raise AssertionError("sandbox reached for launchctl: %r" % (argv,))
        _RealPopen.__init__(self, args, *a, **k)


subprocess.Popen = _FencedPopen

sys.path.insert(0, os.environ["HSCC_API_DIR"])
import gateway_bridge  # noqa: E402

# The base shape, on demand: no watchdog, serve spawned directly. That is what
# ``origin/main`` does always, and the base arm below asserts against it.
if os.environ.get("HSCC_TEST_NO_WATCHDOG") == "1":
    gateway_bridge._watchdog_script = lambda: None

# A fixture token, so nothing real is in the sidecar env even in principle.
gateway_bridge._serve_token = os.environ["FIXTURE_TOKEN"]
ok = gateway_bridge.start_serve(port=int(os.environ["SANDBOX_PORT"]),
                                bin_path=os.environ["FAKE_SERVE_PATH"])

proc = gateway_bridge._serve_proc
if proc is not None:
    with open(SUPERVISED, "w") as fh:
        fh.write(str(proc.pid))

with open(MARKS, "a") as fh:
    fh.write(("@@READY@@" if ok else "sandbox: serve DOWN") + "\\n")
    fh.flush()
    os.fsync(fh.fileno())

MODE = os.environ.get("HSCC_TEST_MODE")
if MODE in ("stop", "stop_on_file"):
    if MODE == "stop_on_file":
        # t_b952885e: the harness gates the stop, so a case can SIGKILL the
        # watchdog FIRST and only then run the ordinary cooperative stop path.
        sentinel = os.path.join(SANDBOX, "stop.requested")
        while not os.path.exists(sentinel):
            time.sleep(0.02)
    gateway_bridge.shutdown()
    with open(MARKS, "a") as fh:
        fh.write("@@SHUTDOWN_DONE@@\\n")
        fh.flush()
        os.fsync(fh.fileno())
    raise SystemExit(0)

while True:
    time.sleep(0.05)
'''


# --------------------------------------------------------------------------- #
# observation helpers
# --------------------------------------------------------------------------- #

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


#: Sentinels for :func:`_state_and_command`. ``UNKNOWN`` is deliberately NOT
#: the same answer as ``DEAD``: "gone" is the answer that can hide a surviving
#: orphan, so a platform that could not answer must never be read as one.
DEAD = "dead"
UNKNOWN = "unknown"


def _state_and_command(pid):
    """``(state, command)`` for ``pid`` — ``DEAD``/``UNKNOWN`` + ``None`` else.

    Linux first, and for a reason that cost a CI run to find: ``/proc/<pid>/stat``
    and ``/proc/<pid>/cmdline`` answer without a subprocess and the command line
    there cannot be truncated, whereas procps ``ps`` clips ``command=`` to the
    display width (80 columns when nothing wider is configured) and these sandbox
    paths are longer than that. Every identity answer in this file quietly came
    back "not ours" on the first Linux runner that tried them, which turned
    "SIGKILL the parent" into a 30-second wait on a process that was never
    signalled.

    macOS has no ``/proc``, so it gets the one ``ps`` call the zombie answer
    already needed (BSD ``ps`` does not clip, and ``-ww`` keeps it honest if a
    narrow terminal ever is).
    """
    target = _PROC_DIR / str(int(pid))
    if _PROC_DIR.is_dir():
        try:
            stat = (target / "stat").read_text()
        except FileNotFoundError:
            return DEAD, None
        except OSError:
            return UNKNOWN, None
        # comm is parenthesised and may contain spaces or ')': the state field is
        # the first token after the LAST ')'.
        tail = stat.rpartition(")")[2].split()
        if not tail:
            return UNKNOWN, None
        try:
            raw = (target / "cmdline").read_bytes()
        except FileNotFoundError:
            return DEAD, None
        except OSError:
            return tail[0], None
        return tail[0], raw.decode("utf-8", "replace")
    try:
        out = subprocess.run(
            ["ps", "-ww", "-o", "state=,command=", "-p", str(int(pid))],
            capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001 — a ps that will not answer is not a death
        return UNKNOWN, None
    if not out:
        return UNKNOWN, None
    state, _, command = out.partition(" ")
    return state, command


def _alive(pid, marker=None) -> bool:
    """Is this pid still a RUNNING process — and, given ``marker``, still OURS?

    ``kill(pid, 0)`` alone is not enough and this is the trap that bites every
    supervisor test: a process that has exited but has not been reaped yet is a
    zombie, still answers signal 0, and is a child of SOMEONE ELSE (the sandbox
    parent, which is dead or busy), so nothing here can reap it. Telling "exited"
    from "running" from the outside needs the process state, which
    :func:`_state_and_command` reads per platform.

    ``marker`` closes the second half of the same trap. macOS and Linux both
    recycle pids, and under a full leg (1300 items, heavy process churn) an
    unrelated process can be handed a dead serve's number within microseconds of
    it dying — it happened on the third of three leg runs, arm=poison, where the
    wait had already seen the pid go and the very next ``_alive`` answered
    "running". Existence is not identity, and asserting on existence alone makes
    a passing teardown look like a failure. The marker is a path that appears in
    the argv of the process this test started and in no one else's.

    When the platform cannot answer at all, the answer is "alive": the safe
    direction, because a wrong "gone" is what lets an orphan through the door.
    """
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, TypeError, ValueError):
        return True
    state, command = _state_and_command(pid)
    if state == DEAD:
        return False
    if state == UNKNOWN or command is None:
        return True
    if state.startswith("Z"):
        return False
    if marker is not None:
        return marker in command
    return True


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
        except OSError:
            return True
    return False


def _wait_until_gone(pid, timeout: float, marker=None) -> float:
    """Seconds until ``pid`` is gone; ~``timeout`` if it never is.

    With ``marker`` "gone" means "no longer the process we started", which is the
    only statement that survives pid recycling.
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if not _alive(pid, marker):
            return time.monotonic() - start
        time.sleep(0.05)
    return time.monotonic() - start


def _signal(pid, sig, marker=None) -> bool:
    """Send ``sig`` to ``pid`` only if it is still the process we started.

    Returns whether it was sent. Every kill in this file goes through here, for
    the same reason the assertions do: a cleanup that signals a recycled number
    signals somebody else's test.
    """
    if not _alive(pid, marker):
        return False
    try:
        os.kill(int(pid), sig)
    except OSError:
        return False
    return True


def _kill_soft(pid, marker=None) -> None:
    """SIGKILL ``pid``, but never a process this test did not start."""
    _signal(pid, signal.SIGKILL, marker)


def test_state_and_command_parses_a_proc_style_layout(tmp_path, monkeypatch):
    """The Linux branch, driven through a fake ``/proc`` on any platform.

    The CI leg is the only Linux this file gets, so the parsing rules liveness
    now rests on are tested against a directory laid out like ``/proc`` rather
    than discovered on a runner: the state token is the first field after the
    LAST ``)`` (``comm`` may hold spaces and parens), a missing ``stat`` is
    ``DEAD``, and an unreadable one is ``UNKNOWN`` — never ``DEAD``, because
    "gone" is the answer that lets an orphan through.
    """
    fake = tmp_path / "proc"
    entry = fake / "4242"
    entry.mkdir(parents=True)
    (entry / "stat").write_text(
        "4242 (weird (proc) name) S 4241 4242 4242 0 -1 4194304 "
        "1 0 0 0 0 0 0 0 20 0 1 0 1234 1 1 0 0\n")
    marker = str(tmp_path / "fake_api.py")
    (entry / "cmdline").write_bytes(b"/usr/bin/python\x00"
                                    + marker.encode() + b"\x00")
    monkeypatch.setattr(sys.modules[__name__], "_PROC_DIR", fake)

    state, command = _state_and_command(4242)
    assert state == "S"
    assert command is not None and marker in command
    assert _state_and_command(4243) == (DEAD, None)
    # (No _alive() call here on purpose: _alive starts with a real
    # kill(4242, 0), and whether that pid exists on this machine has nothing to
    # do with the parsing being tested.)


def test_state_and_command_of_this_process_agrees_with_kill(tmp_path, monkeypatch):
    """Whatever branch this platform takes, it must agree with ``kill(pid, 0)``.

    One real assertion per platform, so a broken branch (a clipped ``ps`` column,
    a ``/proc`` read that needs privileges) reddens here instead of showing up
    forty assertions later as "the parent was not ours to signal".
    """
    here = os.getpid()
    monkeypatch.setattr(sys.modules[__name__], "_PROC_DIR", _PROC_DIR)
    state, command = _state_and_command(here)
    assert state not in (DEAD, UNKNOWN), f"cannot read our own pid: {state!r}"
    assert not state.startswith("Z")
    assert command is not None and sys.executable in command
    assert _alive(here, str(tmp_path / "not_our_path")) is False
    assert _alive(here, sys.executable) is True


# --------------------------------------------------------------------------- #
# harness: one sandbox API parent + its real supervised sidecar
# --------------------------------------------------------------------------- #

class Sidecar:
    """Handle on one sandbox API parent and the real sidecar it supervises."""

    def __init__(self, sandbox: Path, parent, mode: str = "serve"):
        self.sandbox = sandbox
        self.parent = parent
        self.mode = mode
        self._report = None
        self._supervised = None

    # -- observed facts: all read out of the sandbox, none set by the test --
    def report(self) -> dict:
        if self._report is None:
            self._report = json.loads(
                (self.sandbox / "serve_report.json").read_text())
        return self._report

    @property
    def serve_pid(self):
        return self.report()["pid"]

    @property
    def port(self):
        return self.report()["port"]

    @property
    def supervised_pid(self):
        if self._supervised is None:
            self._supervised = int(
                (self.sandbox / "supervised.pid").read_text())
        return self._supervised

    # -- identity markers: each process this harness starts has a path in its
    # argv that no other process in the leg has, which is what turns a pid into
    # an answer that means something (see _alive's second paragraph). --
    @property
    def serve_marker(self) -> str:
        return str(self.sandbox / "fake_serve.py")

    @property
    def parent_marker(self) -> str:
        return str(self.sandbox / "fake_api.py")

    @property
    def watchdog_marker(self) -> str:
        return str(WATCHDOG)

    def signals(self) -> list:
        path = self.sandbox / "signals.log"
        if not path.exists():
            return []
        return [line.split()[1] for line in
                path.read_text().strip().splitlines() if line.strip()]

    def marks(self) -> str:
        path = self.sandbox / "marks.log"
        return path.read_text() if path.exists() else ""

    # -- waits -------------------------------------------------------------
    def wait_for(self, text: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if text in self.marks():
                return True
            if self.parent.poll() is not None:
                break
            time.sleep(0.02)
        return text in self.marks()

    def wait_sidecar_gone(self, timeout: float) -> float:
        return _wait_until_gone(self.serve_pid, timeout, self.serve_marker)

    def kill_parent(self):
        _signal(self.parent.pid, signal.SIGKILL, self.parent_marker)
        self.parent.wait(timeout=30)


@pytest.fixture
def sidecar(request, tmp_path):
    """One sandbox API parent with a real supervised sidecar, cleaned up.

    Indirect-parametrised over ``mode``:

      ``serve`` (default)  the parent serves until the harness kills it;
      ``no_watchdog``      the parent disables the seam — the ``origin/main``
                           shape, used to show the orphan reproduces;
      ``stop``             the parent calls the real ``shutdown()`` itself, so
                           the test asserts the cooperative door from outside;
      ``stop_on_file``     like ``stop``, but the stop is gated on a
                           ``stop.requested`` sentinel, so the harness can break
                           the watchdog first and then run the ordinary stop
                           path (t_b952885e).
    """
    mode = getattr(request, "param", "serve")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "home").mkdir()

    serve_py = sandbox / "fake_serve.py"
    # An EXECUTABLE script with an interpreter shebang, because that is what the
    # real ``bin_path`` is (an executable launcher on the hermes venv bin path)
    # and because ``start_serve`` execs it directly: the watchdog runs it, not an
    # interpreter pointed at it.
    serve_py.write_text("#!" + sys.executable + "\n"
                        + FAKE_SERVE.replace("@@ENV_LIVENESS@@", LIVENESS_ENV))
    serve_py.chmod(0o755)
    parent_py = sandbox / "fake_api.py"
    parent_py.write_text(FAKE_API_PARENT
                         .replace("@@READY@@", READY)
                         .replace("@@SHUTDOWN_DONE@@", SHUTDOWN_DONE))

    env = dict(os.environ)
    env.update({
        "HOME": str(sandbox / "home"),
        "HERMES_HOME": str(sandbox / "home" / ".hermes"),
        "SANDBOX_DIR": str(sandbox),
        "SANDBOX_PORT": str(_free_port()),
        "HSCC_API_DIR": str(API_DIR),
        "FAKE_SERVE_PATH": str(serve_py),
        "FAKE_SERVE_REPORT": str(sandbox / "serve_report.json"),
        "FIXTURE_TOKEN": FIXTURE_TOKEN,
        "HSCC_TEST_MODE": mode if mode in ("stop", "stop_on_file") else "serve",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    if mode == "no_watchdog":
        env["HSCC_TEST_NO_WATCHDOG"] = "1"
    env.pop(LIVENESS_ENV, None)
    env.pop("HERMES_DASHBOARD_SESSION_TOKEN", None)

    # The parent's stderr goes to a file, because "start_serve returned False"
    # with nothing on stderr is an unanswerable failure.
    err_log = sandbox / "parent.err"
    err_fh = open(err_log, "wb")
    proc = subprocess.Popen([sys.executable, str(parent_py)], env=env,
                            stdout=subprocess.DEVNULL, stderr=err_fh,
                            start_new_session=True)
    handle = Sidecar(sandbox, proc, mode)

    def diag() -> str:
        try:
            tail = err_log.read_text(errors="replace")[-900:]
        except OSError:
            tail = "<no parent stderr>"
        return f"mode={mode} marks={handle.marks()!r} parent_stderr={tail!r}"

    try:
        # Readiness = the API's own TCP probe passed, which is also proof the
        # fake sidecar really bound a real port.
        assert handle.wait_for(READY, _SIDECAR_READY_TIMEOUT_S), (
            f"sandbox API never reported the sidecar up; {diag()}")
        # The ``--port`` the bridge put in argv reached the serve VERBATIM
        # through the extra layer — the layer wraps, it does not rewrite.
        assert handle.report()["requested_port"] == int(env["SANDBOX_PORT"])
        assert handle.report()["port"] == int(env["SANDBOX_PORT"])
        assert handle.report()["liveness_env_in_serve"] is None
        if mode != "stop":
            assert _port_free(handle.port) is False, (
                f"sidecar must really be listening; {diag()}")
        # Harness self-check, and it earned its place the hard way: if the
        # identity answer does not work on THIS platform, every pid assertion
        # below silently answers "gone", "not ours to signal" turns into a 30 s
        # wait, and the failure lands far from its cause. That is exactly what
        # happened on the first Linux runner (procps clips ps's command= column).
        assert _alive(proc.pid, handle.parent_marker), (
            f"harness identity check broken on {sys.platform}: our own live "
            f"parent pid {proc.pid} did not match its own argv marker; "
            f"_state_and_command answered {tuple(_state_and_command(proc.pid))!r}")
        yield handle
    finally:
        # Cleanup: never leak a process into the rest of the leg, whatever
        # failed above.
        _kill_soft(proc.pid, handle.parent_marker)
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        _kill_soft(handle._supervised, handle.watchdog_marker)
        _kill_soft(handle._report and handle._report.get("pid"),
                   handle.serve_marker)
        err_fh.close()


# --------------------------------------------------------------------------- #
# THE case: SIGKILL (what ``hscc api stop`` escalates to) must not orphan it
# --------------------------------------------------------------------------- #

def test_sigkilled_api_parent_does_not_orphan_the_sidecar(sidecar):
    """SIGKILL the API parent -> the real sidecar subprocess must die too.

    Bound: ``_SIDECAR_GONE_BOUND_S`` (4.0 s, derived from 0.25 s fallback tick +
    2.0 s TERM grace + 1.0 s KILL grace); measured on the shipped pipe arm
    0.065 - 0.174 s. RED AT BASE: there the API's direct child IS the serve and
    nothing watches the parent's death, so this assertion fails outright — the
    companion case below reproduces exactly that shape at this same sha.
    """
    serve_pid = sidecar.serve_pid
    supervised_pid = sidecar.supervised_pid

    sidecar.kill_parent()
    elapsed = sidecar.wait_sidecar_gone(_SIDECAR_GONE_BOUND_S)

    assert elapsed <= _SIDECAR_GONE_BOUND_S, (
        f"sidecar pid {serve_pid} was still alive {_SIDECAR_GONE_BOUND_S}s "
        f"after its API parent was SIGKILLed — the orphaned-serve / "
        f"stale-token 403 shape")
    assert not _alive(serve_pid, sidecar.serve_marker)
    # The observable consequence, not a flag the test set itself: the port the
    # dead API's sidecar held is released. An orphan keeps it, dead token and all.
    assert _port_free(sidecar.port), f"port {sidecar.port} still answering"
    # And the watcher must not outlive the process it was watching either. Same
    # bounded wait, because between "exited" and "reaped by launchd" (its new
    # parent now that the API is gone) it is still a pid that answers signal 0.
    wd_elapsed = _wait_until_gone(supervised_pid, _SIDECAR_GONE_BOUND_S,
                                  sidecar.watchdog_marker)
    assert wd_elapsed <= _SIDECAR_GONE_BOUND_S, (
        f"watchdog pid {supervised_pid} outlived its API parent")


@pytest.mark.parametrize("sidecar", ["no_watchdog"], indirect=True)
def test_the_same_kill_orphans_the_sidecar_without_the_watchdog(sidecar):
    """The red-at-base arm, reproducible at this sha: seam off == base shape.

    ``_watchdog_script -> None`` is what ``origin/main`` does always (spawn the
    serve directly; nothing watches the parent), so this case proves the one
    above is red there rather than merely un-run: with the seam off, the sidecar
    is STILL ALIVE after the same SIGKILL and the same bound. If this ever
    passes, the base arm has stopped testing what the fix replaces.
    """
    serve_pid = sidecar.serve_pid
    sidecar.kill_parent()
    elapsed = sidecar.wait_sidecar_gone(_SIDECAR_GONE_BOUND_S)

    assert _alive(serve_pid, sidecar.serve_marker), (
        f"the orphan did not reproduce with the watchdog disabled (gone after "
        f"{elapsed:.2f}s): something other than the watchdog is now reaping it")
    assert not _port_free(sidecar.port), "the orphan should still hold the port"


# --------------------------------------------------------------------------- #
# the doors that already worked must keep working through the extra layer
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sidecar", ["stop"], indirect=True)
def test_cooperative_shutdown_still_takes_the_sidecar_down(sidecar):
    """``shutdown()`` — the cooperative door — still stops the real sidecar.

    The extra layer must not slow or break the ordinary ``hscc api stop`` path:
    the API terminates the watchdog, the watchdog forwards and reaps. Green at
    base by construction (there the terminate lands on the serve itself), and it
    has to STAY green — that is why it is pinned rather than assumed.
    """
    assert sidecar.wait_for(SHUTDOWN_DONE,
                            bridge._SHUTDOWN_WAIT_S + 10.0), (
        f"shutdown() never returned; marks={sidecar.marks()!r}")
    assert not _alive(sidecar.serve_pid, sidecar.serve_marker), (
        "sidecar survived a clean shutdown()")
    assert _port_free(sidecar.port)


def test_sigterm_of_the_supervised_process_takes_the_sidecar_down(sidecar):
    """SIGTERM the WATCHDOG and the sidecar still dies.

    This is the regression the extra layer could have shipped: a process's
    default answer to SIGTERM is to drop dead where it stands, and a watchdog
    that dies on the API's stop signal without forwarding it re-creates the
    orphan from inside the fix. ``watch()`` traps TERM/INT/HUP for this reason.
    """
    assert _signal(sidecar.supervised_pid, signal.SIGTERM,
                   sidecar.watchdog_marker), (
        f"watchdog {sidecar.supervised_pid} was not ours to signal — it is gone "
        f"or someone else holds the number")
    elapsed = sidecar.wait_sidecar_gone(_SIDECAR_GONE_BOUND_S)
    assert elapsed <= _SIDECAR_GONE_BOUND_S, (
        f"the sidecar outlived a SIGTERM to its watchdog by {elapsed:.2f}s — "
        f"the watchdog must forward, not just exit")
    assert "15" in sidecar.signals(), (
        f"the serve was never SIGTERMed by the watchdog: {sidecar.signals()}")


def test_sidecar_dying_on_its_own_leaves_no_watchdog_behind(sidecar):
    """When the serve goes away by itself, the watcher notices and exits.

    Otherwise every crash of ``hermes serve`` leaks one process parked on a pipe
    until the API itself restarts.
    """
    supervised_pid = sidecar.supervised_pid
    assert _signal(sidecar.serve_pid, signal.SIGKILL, sidecar.serve_marker), (
        f"serve {sidecar.serve_pid} was not ours to signal — it is already gone "
        f"or someone else holds the number")
    elapsed = _wait_until_gone(supervised_pid, _SIDECAR_GONE_BOUND_S,
                               sidecar.watchdog_marker)
    assert elapsed <= _SIDECAR_GONE_BOUND_S, (
        f"watchdog {supervised_pid} is still watching a serve that died "
        f"{elapsed:.2f}s ago")


# --------------------------------------------------------------------------- #
# the supervisor nobody supervises (t_b952885e) — measured, pinned, residual
# --------------------------------------------------------------------------- #

def _kill_watchdog(sidecar) -> None:
    """SIGKILL the watchdog (identity-checked), then prove it is actually gone.

    Measured here and in the t_b952885e measurement runs: a real SIGKILLed
    watchdog is gone in 0.0024 - 0.0032 s. The bound is the file's, not a new
    number — nothing in the residual argument depends on how fast the watcher
    dies, only that it does.
    """
    wd_pid = sidecar.supervised_pid
    assert _signal(wd_pid, signal.SIGKILL, sidecar.watchdog_marker), (
        f"watchdog {wd_pid} was not ours to signal — it is gone or someone "
        f"else holds the number")
    elapsed = _wait_until_gone(wd_pid, _SIDECAR_GONE_BOUND_S,
                               sidecar.watchdog_marker)
    assert elapsed <= _SIDECAR_GONE_BOUND_S, (
        f"watchdog {wd_pid} did not die within {_SIDECAR_GONE_BOUND_S}s of "
        f"its SIGKILL")


RESIDUAL = (
    "documented residual, decided on measurement (t_b952885e, see "
    "changelog.d/t_b952885e.md): nothing supervises the watchdog, and on "
    "macOS the LAST watcher is uncloseable — no PR_SET_PDEATHSIG, so every "
    "sidecar-side chain just moves the hole one level down, and an API-side "
    "re-attach must signal the serve's pid without a child handle, i.e. "
    "identity-blind pid signalling, the trap that already reddened this "
    "harness twice. The orphan behaves exactly like the sidecar the API "
    "wants while the API lives (same token, same port, keeps serving), and "
    "start_serve's port-occupancy heal kills it before the next supervised "
    "serve starts.")


@pytest.mark.xfail(strict=True, reason=RESIDUAL)
def test_sigkill_of_the_watchdog_does_not_orphan_the_sidecar(sidecar):
    """THE acceptance case of t_b952885e: SIGKILL the WATCHDOG -> serve gone
    within the bound. It does not hold today, so the case lands as a strict
    xfail: red at base un-marked, green-as-failure in the leg, and XPASS-red —
    not silently green — the day a fix ships and this doc must move with it.

    Measured 10/10 trials (5 x 2 runs, real chain, real SIGKILL of the real
    watchdog): serve alive at the 4.0 s bound, alive at 6.0 s, port still
    held. The hole is real; the decision is that closing it is more moving
    parts than the risk, with the risk spelled out below and in the changelog.
    """
    _kill_watchdog(sidecar)
    elapsed = sidecar.wait_sidecar_gone(_SIDECAR_GONE_BOUND_S)
    assert elapsed <= _SIDECAR_GONE_BOUND_S, (
        f"serve pid {sidecar.serve_pid} was still alive {elapsed:.2f}s after "
        f"its watchdog was SIGKILLed — the orphan/403 shape, one level up")
    assert _port_free(sidecar.port), f"port {sidecar.port} still answering"


def test_watchdog_sigkill_orphan_is_the_documented_residual(sidecar):
    """The mirror of the acceptance case above: the shape as it stands TODAY.

    A change detector, and it should be read as one: these three cases (this
    pin, the stop pin below, the strict xfail above) assert the same sentence
    from three sides, and a real fix reddens all of them at once. That is the
    mechanism that forces the risk statement to be re-decided with the code,
    rather than quietly inherited by it.

    Nothing here depends on the orphan being fast; it depends on it being
    INDEFINITE — measured: alive at the bound in 10/10 trials and holding the
    port (``hscc api stop`` with a dead watchdog: 3/3, below).
    """
    _kill_watchdog(sidecar)
    elapsed = sidecar.wait_sidecar_gone(_SIDECAR_GONE_BOUND_S)
    assert elapsed >= _SIDECAR_GONE_BOUND_S, (
        f"the orphan died after {elapsed:.2f}s — the residual stated on the "
        f"xfail above and in changelog.d/t_b952885e.md is no longer true")
    assert _alive(sidecar.serve_pid, sidecar.serve_marker)
    assert not _port_free(sidecar.port), (
        "the orphan should still hold the port and keep serving — it is "
        "exactly the sidecar the live API wants, which is why this residual "
        "is invisible while the API is up")


@pytest.mark.parametrize("sidecar", ["stop_on_file"], indirect=True)
def test_clean_stop_with_a_dead_watchdog_still_leaves_the_orphan(sidecar):
    """The residual's sharpest edge: ``hscc api stop`` after the watchdog has
    already died does NOT sweep the orphan.

    ``shutdown()`` talks to the watchdog (``_stop_supervised``); a watchdog
    that is already dead is polled and reaped without anything being signalled
    (CPython's pid-reuse guard in ``Popen.terminate``, and it is wanted here),
    so the cooperative stop returns in 0.07 - 0.16 s (measured 3/3) and the
    serve is alive right after it and 2 s later, holding the port.

    The boundary that DOES sweep it is the next start: ``start_serve``'s
    port-occupancy heal (the iOS-chat-e2e 2026-10-13 machinery) reads the
    stale token, vets the listener's cmdline and kills it before spawning a
    supervised fresh one. The residual is therefore "an orphan that survives a
    stop but never survives a start cycle" — and this case pins that sentence
    end to end. If the stop path ever grows a dead-watchdog sweep on purpose,
    this pin reddens with the other two and the changelog moves with it.
    """
    _kill_watchdog(sidecar)
    (sidecar.sandbox / "stop.requested").write_text("go")
    assert sidecar.wait_for(SHUTDOWN_DONE, bridge._SHUTDOWN_WAIT_S + 10.0), (
        f"shutdown() never returned; marks={sidecar.marks()!r}")
    assert _alive(sidecar.serve_pid, sidecar.serve_marker), (
        "the clean stop swept the orphan — if that is deliberate now, update "
        "the xfail above and changelog.d/t_b952885e.md")
    assert not _port_free(sidecar.port), (
        f"port {sidecar.port} freed by a stop that never signalled the serve")


# --------------------------------------------------------------------------- #
# the watchdog on its own: both signals, and the fd hygiene
# --------------------------------------------------------------------------- #

#: ``arm`` -> what lands in ``HSCC_GATEWAY_LIVENESS_FD`` for the watchdog:
#: ``pipe``   a real pipe the sandbox parent created (the shipped primary)
#: ``poison`` a number that is not an fd at all (the poll fallback's trigger)
#: ``none``   unset (also the poll fallback, via the "no fd" branch)
WD_PARENT = '''\
import os
import subprocess
import sys
import time

env = dict(os.environ)
arm = "@@ARM@@"
pass_fds = ()
if arm == "pipe":
    read_fd, _write_fd = os.pipe()
    env["@@ENV_LIVENESS@@"] = str(read_fd)
    pass_fds = (read_fd,)
elif arm == "poison":
    env["@@ENV_LIVENESS@@"] = "987654"
else:
    env.pop("@@ENV_LIVENESS@@", None)

proc = subprocess.Popen(
    [sys.executable, "@@WATCHDOG@@", sys.executable, "@@SERVE@@",
     "--port", "0"],
    env=env, pass_fds=pass_fds, stdout=subprocess.DEVNULL,
    stderr=open("@@LOG@@", "a"), start_new_session=True)
with open("@@WD_PID@@", "w") as fh:
    fh.write(str(proc.pid))

while True:
    time.sleep(0.05)
'''


def _run_watchdog_alone(tmp_path, arm: str):
    """Spawn the shipped watchdog under a throwaway parent process.

    Returns (parent, serve report, watchdog pid, watchdog log path, serve path).
    The last one is the identity marker for the serve's pid.
    """
    sandbox = tmp_path / f"wd_{arm}"
    sandbox.mkdir()
    serve_py = sandbox / "fake_serve.py"
    serve_py.write_text(FAKE_SERVE.replace("@@ENV_LIVENESS@@", LIVENESS_ENV))
    report = sandbox / "serve_report.json"
    log = sandbox / "watchdog.log"
    parent_py = sandbox / "wd_parent.py"
    parent_py.write_text(WD_PARENT
                         .replace("@@ARM@@", arm)
                         .replace("@@ENV_LIVENESS@@", LIVENESS_ENV)
                         .replace("@@WATCHDOG@@", str(WATCHDOG))
                         .replace("@@SERVE@@", str(serve_py))
                         .replace("@@LOG@@", str(log))
                         .replace("@@WD_PID@@", str(sandbox / "watchdog.pid")))
    env = dict(os.environ)
    env.update({"WD_DIR": str(sandbox),
                "FAKE_SERVE_REPORT": str(report),
                "PYTHONDONTWRITEBYTECODE": "1"})
    env.pop(LIVENESS_ENV, None)
    parent = subprocess.Popen([sys.executable, str(parent_py)], env=env,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              start_new_session=True)
    deadline = time.monotonic() + _SIDECAR_READY_TIMEOUT_S
    while not report.exists() and time.monotonic() < deadline:
        if parent.poll() is not None:
            break
        time.sleep(0.02)
    if not report.exists():
        _kill_soft(parent.pid, str(parent_py))
        pytest.fail(f"the watchdog never brought its child up (arm={arm})")
    info = json.loads(report.read_text())
    watchdog_pid = int((sandbox / "watchdog.pid").read_text())
    return parent, info, watchdog_pid, log, serve_py


@pytest.mark.parametrize("arm", ["pipe", "poison", "none"])
def test_watchdog_buries_its_child_when_the_parent_is_sigkilled(tmp_path, arm):
    """All three arms: a SIGKILLed parent must not leave a serve running.

    ``pipe`` is the shipped primary (EOF, no tick); ``poison`` and ``none`` take
    the reparenting-poll fallback, whose blind spot is ``_DEFAULT_POLL_S``. One
    parametrisation, so a claim about the fallback cannot quietly stop being
    tested while the primary still passes.
    """
    parent, info, watchdog_pid, log, serve_py = _run_watchdog_alone(tmp_path,
                                                                    arm)
    serve_marker = str(serve_py)
    watchdog_marker = str(WATCHDOG)
    parent_marker = str(tmp_path / f"wd_{arm}" / "wd_parent.py")
    try:
        assert info["liveness_env_in_serve"] is None, (
            "the watchdog handed the liveness env down to the serve — it must "
            "stop at its own child")
        t0 = time.monotonic()
        assert _signal(parent.pid, signal.SIGKILL, parent_marker), (
            f"arm={arm}: the sandbox parent was not ours to signal")
        parent.wait(timeout=30)
        elapsed = _wait_until_gone(info["pid"], _SIDECAR_GONE_BOUND_S,
                                   serve_marker)
        assert elapsed <= _SIDECAR_GONE_BOUND_S, (
            f"arm={arm}: serve pid {info['pid']} still alive {elapsed:.2f}s "
            f"after the parent was SIGKILLed")
        assert not _alive(info["pid"], serve_marker)
        assert _wait_until_gone(watchdog_pid, _SIDECAR_GONE_BOUND_S,
                                watchdog_marker) <= _SIDECAR_GONE_BOUND_S
        # The reason the watchdog itself recorded, which is the only way to tell
        # the pipe path from the fallback without timing both.
        reason = log.read_text() if log.exists() else ""
        if arm == "pipe":
            assert "pipe-eof" in reason, reason
        else:
            assert "reparented-to" in reason, reason
    finally:
        _kill_soft(info["pid"], serve_marker)
        _kill_soft(watchdog_pid, watchdog_marker)
        _kill_soft(parent.pid, parent_marker)


def test_watchdog_rejects_a_liveness_fd_that_is_not_a_pipe(tmp_path):
    """Unit: a regular file / a missing fd / garbage is "no pipe", not "wait".

    The fallback trigger, pinned. A stale or spoofed env number that is not a
    FIFO must send the watchdog to the reparenting probe — never to a blocking
    read on something that will never EOF.
    """
    wd = _load_watchdog()
    os.environ.pop(wd.ENV_LIVENESS_FD, None)
    assert wd.liveness_fd() == -1                      # env unset
    os.environ[wd.ENV_LIVENESS_FD] = "not-a-number"
    try:
        assert wd.liveness_fd() == -1
    finally:
        os.environ.pop(wd.ENV_LIVENESS_FD, None)
    regular = tmp_path / "regular.txt"
    regular.write_text("x")
    fd = os.open(str(regular), os.O_RDONLY)
    os.environ[wd.ENV_LIVENESS_FD] = str(fd)
    try:
        assert wd.liveness_fd() == -1                  # S_ISFIFO gate
    finally:
        os.environ.pop(wd.ENV_LIVENESS_FD, None)
        os.close(fd)
    r, w = os.pipe()
    os.environ[wd.ENV_LIVENESS_FD] = str(r)
    try:
        assert wd.liveness_fd() == r                   # a real one is accepted
        assert stat.S_ISFIFO(os.fstat(r).st_mode)
    finally:
        os.environ.pop(wd.ENV_LIVENESS_FD, None)
        os.close(r)
        os.close(w)


# --------------------------------------------------------------------------- #
# the bridge side of the seam: what start_serve actually hands the child
# --------------------------------------------------------------------------- #

class _RecordingPopen:
    """Stands in for subprocess.Popen; records the spawn it was asked for."""

    last = None

    def __init__(self, args, **kwargs):
        _RecordingPopen.last = self
        self.args = [str(a) for a in (
            args if isinstance(args, (list, tuple)) else [args])]
        self.kwargs = kwargs
        self.pid = 424242
        self.returncode = None
        self.terminated = False
        self.waited = None

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waited = timeout
        return 0


@pytest.fixture
def recording_spawn(monkeypatch):
    monkeypatch.setattr(bridge.subprocess, "Popen", _RecordingPopen)
    _RecordingPopen.last = None
    return _RecordingPopen


def test_spawn_hands_down_the_read_end_and_keeps_the_write_end(monkeypatch,
                                                               recording_spawn):
    """The pipe contract, pinned at the call site.

    ``pass_fds`` carries ONLY the read end, the child is told its number, the
    parent keeps the write end in module state, and the read end is closed here
    — a parent that kept its own read end open could hold the pipe shut for a
    watchdog that should already be gone.

    ``pass_fds`` is also the ONLY thing that makes the fd inheritable (``os.pipe``
    fds are non-inheritable since PEP 446 and ``Popen`` closes every other fd in
    the child), which is what keeps the API's *other* children from holding the
    write end open and delaying detection forever — ``api_cli._spawn_dashboard``
    is exactly that shape. Measured with a sibling in its own session left alive
    past the parent's SIGKILL: the serve still died 0.054 - 0.172 s later, which
    is pipe speed, not the 0.25 s poll speed the fallback would cost.
    """
    monkeypatch.setattr(bridge, "_watchdog_script", lambda: str(WATCHDOG))
    monkeypatch.setattr(bridge, "_serve_env", lambda env: dict(env))
    proc = bridge._spawn_serve(["/bin/true", "serve"])
    spawned = recording_spawn.last

    assert spawned.args[0] == (sys.executable or "python3")
    assert spawned.args[1] == str(WATCHDOG)
    assert spawned.args[2:] == ["/bin/true", "serve"]
    read_fd = spawned.kwargs["pass_fds"][0]
    assert len(spawned.kwargs["pass_fds"]) == 1
    assert spawned.kwargs["env"][bridge.ENV_LIVENESS_FD] == str(read_fd)
    assert spawned.kwargs["start_new_session"] is True
    # The read end is closed in the parent...
    with pytest.raises(OSError) as exc:
        os.fstat(read_fd)
    assert exc.value.errno == errno.EBADF
    # ...and what the module holds is a different, live pipe end.
    write_fd = bridge._serve_liveness_write_fd
    assert isinstance(write_fd, int) and write_fd != read_fd
    assert stat.S_ISFIFO(os.fstat(write_fd).st_mode)
    assert proc is spawned

    bridge._release_liveness_write_fd()
    with pytest.raises(OSError) as exc:
        os.fstat(write_fd)
    assert exc.value.errno == errno.EBADF
    assert bridge._serve_liveness_write_fd is None


def test_two_spawns_never_hold_two_write_ends(monkeypatch, recording_spawn):
    """A stale write end would keep a retired watchdog waiting for nothing.

    Spied on ``os.close`` rather than compared by fd number: the OS hands the
    same number straight back once the old end is closed (measured — the second
    spawn got the same 12), so "the old one is gone" is only observable as the
    close that removed it.
    """
    monkeypatch.setattr(bridge, "_watchdog_script", lambda: str(WATCHDOG))
    monkeypatch.setattr(bridge, "_serve_env", lambda env: dict(env))
    closed = []
    real_close = os.close

    def spy_close(fd):
        closed.append(fd)
        try:
            return real_close(fd)
        except OSError:
            return None            # the synthetic numbers below are not real

    def fake_pipe():
        # Real fds cannot be used to prove this: the OS hands the SAME number
        # straight back once the old end is closed (measured — both spawns got
        # 12), so "the old end was released" is only observable as the close and
        # as a number that cannot be confused with a reused one.
        base = 100 + 2 * len(pipes_given)
        pipes_given.append(base)
        return base, base + 1

    pipes_given = []
    monkeypatch.setattr(bridge.os, "close", spy_close)
    monkeypatch.setattr(bridge.os, "pipe", fake_pipe)
    bridge._spawn_serve(["/bin/true", "serve"])
    first = bridge._serve_liveness_write_fd
    bridge._spawn_serve(["/bin/true", "serve"])
    second = bridge._serve_liveness_write_fd
    assert second is not None and second != first
    assert first in closed, "the previous write end was left open"
    assert closed.count(first) == 1, closed      # closed once, not twice
    assert second not in closed
    bridge._release_liveness_write_fd()
    assert second in closed


def test_spawn_falls_back_to_the_direct_spawn_without_a_watchdog(monkeypatch,
                                                                recording_spawn):
    """No watchdog available = the pre-t_ed2f1abe shape, and it is LOUD.

    This arm is also the suite-wide conftest default, which is what keeps every
    other unit test in the leg from ever spawning a real subprocess. A missing
    watchdog must degrade the guarantee visibly: a silent degradation is how the
    orphan comes back and nobody notices for a release.
    """
    monkeypatch.setattr(bridge, "_watchdog_script", lambda: None)
    monkeypatch.setattr(bridge, "_serve_env", lambda env: dict(env))
    warned = []
    monkeypatch.setattr(bridge.log, "warning",
                        lambda *a, **k: warned.append(a))
    proc = bridge._spawn_serve(["/bin/echo", "serve"])
    spawned = recording_spawn.last
    assert spawned.args == ["/bin/echo", "serve"]
    assert "pass_fds" not in spawned.kwargs
    assert bridge._serve_liveness_write_fd is None
    assert proc is spawned
    assert warned, "the degraded spawn was not logged"
    assert any("NOT survive" in str(part) for part in warned[0]), warned


def test_spawn_still_gets_a_sidecar_when_the_watchdog_cannot_start(monkeypatch,
                                                                   recording_spawn):
    """A broken watchdog must not cost the operator the sidecar (fail-open)."""
    calls = []

    def flaky(args, **kwargs):
        argv = [str(a) for a in (args if isinstance(args, (list, tuple))
                                 else [args])]
        calls.append(argv)
        if str(WATCHDOG) in argv:
            raise OSError(2, "no such watchdog")
        return _RecordingPopen(args, **kwargs)

    monkeypatch.setattr(bridge, "_watchdog_script", lambda: str(WATCHDOG))
    monkeypatch.setattr(bridge, "_serve_env", lambda env: dict(env))
    monkeypatch.setattr(bridge.subprocess, "Popen", flaky)
    proc = bridge._spawn_serve(["/bin/echo", "serve"])
    assert len(calls) == 2, calls              # watchdog attempt, then direct
    assert calls[0][1] == str(WATCHDOG)
    assert calls[1] == ["/bin/echo", "serve"]
    assert proc.pid == 424242
    assert bridge._serve_liveness_write_fd is None


def test_api_waits_longer_than_the_watchdog_takes_to_teardown():
    """Cross-module invariant, pinned in both directions.

    The API stops waiting at ``_SHUTDOWN_WAIT_S`` and then SIGKILLs the
    supervised process. If the watchdog's own TERM+KILL budget ever grew past
    that, the API would kill the watchdog while it was still waiting on the
    serve — orphaning the sidecar with the code written to prevent it, and
    reddening the SIGKILL case above on a loaded host rather than on a bad day.
    """
    wd = _load_watchdog()
    assert wd._DEFAULT_TERM_GRACE_S + wd._DEFAULT_KILL_GRACE_S \
        < bridge._SHUTDOWN_WAIT_S
    # ...and the bound this file asserts is at least the watchdog's whole budget
    # plus the fallback's tick, so the case cannot pass by being tuned tight.
    assert _SIDECAR_GONE_BOUND_S >= (
        wd._DEFAULT_POLL_S + wd._DEFAULT_TERM_GRACE_S
        + wd._DEFAULT_KILL_GRACE_S)


def test_watchdog_argv_stays_the_serve_command_for_the_cmdline_guard():
    """The orphan heal's blast-radius guard reads ``ps``, so argv must not move.

    ``start_serve`` vetts an occupant by matching ``serve`` and ``hermes`` in
    the LISTENER's cmdline. The watchdog sits above the serve and must not
    rename or wrap what the listener reports: the supervised command has to
    reach the serve verbatim, and the watchdog's own knobs must not leak into
    argv.
    """
    wd_src = WATCHDOG.read_text()
    assert "argv[1:]" in wd_src or "sys.argv[1:]" in wd_src
    assert "HSCC_GATEWAY_LIVENESS_FD" in wd_src
    # The env var is popped, never passed on, and no option parsing eats argv.
    assert "env.pop(ENV_LIVENESS_FD" in wd_src
    assert 'argv[:1] == ["--"]' in wd_src
