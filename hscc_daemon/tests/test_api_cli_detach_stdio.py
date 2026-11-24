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
  patching, never by a product branch) MUST keep the pipe open. Without it every
  EOF assertion below could pass while being blind.

Why every wait in ``TestCallersPipeReachesEof`` blocks on a state (t_0a723a58).
This class failed 6 of 6 sampled CI runs and never once locally, and each failure
was the same statement: ``assert proc.poll() is not None``. That is not a timeout
that was too short — it is a state being *peeked* instead of *waited for*. The
report file is written by the grandchild; the caller exits on its own unrelated
path (`save_pid()` file I/O + a rich-console panel + CPython teardown), and
nothing orders those two processes. An idle Mac always had the caller finished
first; a runner shared by a dozen legs did not. So the class now waits on states
— a ``waitpid`` for the caller, a file for the stray's report, *bytes received*
for "a stray holds my pipe", a zero-timeout ``select`` for "the pipe is closed" —
and the numbers below are hang guards, not budgets a test tries to beat.
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

# Written by the stray to fd 1 from the SAME statement in both postures (see
# DRIVER._stub_serve). With the detach in place it lands on the null device and
# the caller never sees it; without the detach it arrives on the test's own read
# end. That is the positive proof that a stray holds the caller's pipe.
STRAY_SENTINEL = b"__HSCC_DETACH_STRAY_HOLDING_STDOUT__\n"

# ---------------------------------------------------------------------------
# Ceilings. Every one of these is a HANG GUARD, not an assertion.
#
# Each bounds a wait for a specific STATE (the caller exited, the report file
# appeared, the stray's bytes arrived, the pipe reached EOF). No passing
# assertion compares elapsed time against them, so a loaded runner cannot turn a
# pass into a fail; it can only fail a state that genuinely never arrived, and
# the message then names that state.
#
# The values are UNCHANGED from the pre-fix file, and CALLER_EXIT_GUARD reuses
# EOF_DEADLINE rather than adding a bigger budget: no timeout here was raised,
# because no timeout was the defect.
# ---------------------------------------------------------------------------

# Backstop for a stray whose test died before reaping it. Not a window: the
# stray is released by `_reap` long before this, and no assertion measures
# against it — it exists only so a crashed test cannot leak a 60 s process.
STRAY_SECONDS = 60
# The fixed posture must reach EOF. Covers a full interpreter start + import of
# hscc_daemon + two forks on a loaded CI box.
EOF_DEADLINE = 25.0
# The stray's marker must arrive on the caller's pipe. Same value the pre-fix
# file spent on `NO_EOF_WINDOW`, with its meaning inverted: it is now the wait
# for a byte that MUST show up, not the tolerance for one that must not.
SENTINEL_GUARD = 4.0
# Bounded wait for the stray's own report file to appear.
REPORT_DEADLINE = 25.0
# The caller's own exit, waited for with a waitpid.
CALLER_EXIT_GUARD = EOF_DEADLINE


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
STRAY_SENTINEL = b"@@SENTINEL@@"


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


def _is_fifo(fd):
    """True when fd is a FIFO — i.e. still a pipe, and not the null device."""
    try:
        return stat.S_ISFIFO(os.fstat(fd).st_mode)
    except OSError:
        return False


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
    start, then outlives every wait the test performs — so the caller's pipe
    can only close because the daemon released it, never because the daemon
    finished.
    """
    payload = {"pid": os.getpid(), "fd0": _is_devnull(0),
               "fd1": _is_devnull(1), "fd2": _is_devnull(2),
               "fd1_fifo": _is_fifo(1)}
    tmp = REPORT + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, REPORT)
    # The premise's proof, and it costs one statement. Writing to fd 1 happens
    # here in BOTH postures, which is what makes it evidence rather than a
    # hint: if the detach ran, these bytes go to the null device and the test
    # never sees them; if it did not, they arrive on the test's own read end.
    # Bytes on that read end can only have come from a process holding the
    # write end — which is the only way "a stray is holding my pipe" can be
    # shown positively. (Identity would be the obvious alternative and does not
    # work: on macOS the two ends of a pipe report different st_ino, so the
    # stray cannot name the pipe the test is reading.)
    try:
        os.write(1, STRAY_SENTINEL)
    except OSError:
        pass          # the read end is already closed; a test finished with us
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


def _driver_source() -> str:
    """The DRIVER text with the repo path and the sentinel baked in.

    One place, both call sites, so the sentinel the test asserts on can never
    drift from the one the stray writes.
    """
    return (DRIVER.replace("@@REPO@@", str(REPO))
            .replace("@@SENTINEL@@", repr(STRAY_SENTINEL)))


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


# ---------------------------------------------------------------------------
# state primitives — each answers "what is true of the pipe RIGHT NOW", and the
# deadline in each only bounds how long the test is willing to wait for a state
# it expects to arrive. None of them turns a slow machine into a failed test.
# ---------------------------------------------------------------------------

def _read_nowait(stream):
    """One non-blocking read: (kind, bytes), kind in {data, eof, empty}.

    select(timeout=0) runs first so the os.read below can never block.

      * ``eof``   — read returned b"", i.e. EVERY write end of the caller's
        stdout pipe is closed right now. For a daemonising CLI this is the whole
        property: the detached grandchild either still holds it or it does not.
      * ``empty`` — nothing pending and a read would block, i.e. at least one
        writer is still attached.
      * ``data``  — bytes moved; read again.

    "Held" and "closed" are therefore read as states, not inferred from whether
    anything showed up inside a window.
    """
    readable, _, _ = select.select([stream], [], [], 0.0)
    if not readable:
        return "empty", b""
    chunk = os.read(stream.fileno(), 65536)
    return ("eof", b"") if chunk == b"" else ("data", chunk)


def _read_until(stream, settled, guard):
    """Drain *stream* until `settled(kind, buf)` holds; return (kind, buf).

    `guard` is a hang guard — it stops a state that never arrives from burning
    the leg forever. Nothing that passes here measured itself against it.
    """
    buf = b""
    kind = "empty"
    deadline = time.monotonic() + guard
    while True:
        kind, chunk = _read_nowait(stream)
        if kind == "data":
            buf += chunk
        if settled(kind, buf) or time.monotonic() >= deadline:
            return kind, buf
        time.sleep(0.02)


def _eof_state(kind, buf):
    """Settled when the caller's pipe reports EOF."""
    return kind == "eof"


def _stray_spoke(kind, buf):
    """Settled when the stray's marker is on our read end AND everything is drained.

    Bytes on the test's read end can only have come from a process holding the
    write end, so satisfying this is positive proof the harness can see a leak —
    which is the one thing the premise test exists to establish.
    """
    return STRAY_SENTINEL in buf and kind != "data"


def _wait_for_caller_exit(proc, posture):
    """Block until the `hscc api start` caller has ACTUALLY exited; return its rc.

    This replaces ``assert proc.poll() is not None``, and that swap is the fix.
    `poll()` asked "has the caller exited yet?" at one arbitrary instant and
    scored "not yet" as a defect. But nothing orders the caller's exit against
    the grandchild writing its report file: at that moment the caller still had
    `save_pid()` file I/O, a rich-console panel and a whole CPython teardown in
    front of it, while the grandchild had a couple of syscalls. On an idle Mac
    the caller always finished first and the class was green; on a runner shared
    by a dozen legs it lost, and every sampled CI failure in this class (6 of 6)
    was exactly that assert. `wait()` is a waitpid — it returns when the state
    is true, so a pass stops depending on which process the scheduler picked.

    The observable is unchanged: the test must still know the caller is gone
    before it attributes a closed pipe to the daemon rather than to itself.
    """
    try:
        return proc.wait(timeout=CALLER_EXIT_GUARD)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        pytest.fail(
            f"the `hscc api start` caller never exited ({posture}) — the test "
            "would then be measuring its own fd, not the daemon's")


def _wait_for_report(path, proc, deadline=REPORT_DEADLINE) -> dict:
    """The stray's own descriptor report, or a failure carrying its output.

    Raises rather than returning None, so a caller subscripting the report can
    never be reading a missing state: either the file is there or the leg fails
    naming the state that did not arrive.
    """
    deadline_at = time.monotonic() + deadline
    while time.monotonic() < deadline_at:
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(0.05)
    if path.exists():                       # one last look after the final sleep
        return json.loads(path.read_text())
    out = b""
    try:
        out = proc.stdout.read() if proc.poll() is not None else b""
    except Exception:
        pass
    raise AssertionError(
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
    """A real double-forked `hscc api start` under a caller whose stdout is a pipe.

    Every wait in this class blocks on a state — a waitpid for the caller's exit,
    a file for the stray's report, bytes received for "a stray holds my pipe", a
    zero-timeout select for "the pipe is closed" — and the constants above are
    hang guards around those waits, not budgets the test races. See the module
    docstring for the CI history that made this the shape.
    """

    def _spawn(self, sandbox, *, leak):
        driver = sandbox["tmp"] / "detach_driver.py"
        driver.write_text(_driver_source())
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

    def _cleanup(self, proc, stray):
        _reap(stray)
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

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
            rc = _wait_for_caller_exit(proc, "fixed posture")
            assert _alive(stray), "the daemon died; EOF would prove nothing"

            # The observable, waited for rather than caught. By the time the
            # caller's waitpid returns, the intermediate child is long gone (it
            # `os._exit`s before this grandchild can even reach the serve loop,
            # and the report proves the grandchild is in it) and the daemon's
            # fd 1 is the null device (asserted below) — so EOF is already true
            # here and this read returns at once instead of racing for it.
            kind, out = _read_until(proc.stdout, _eof_state, EOF_DEADLINE)
            assert kind == "eof", (
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
            # And the counterpart of the premise below: the stray's own write to
            # fd 1 went nowhere the caller can see.
            assert STRAY_SENTINEL not in out, (
                f"the daemon's marker arrived on the caller's pipe ({out!r}) "
                "while fd 1 was supposedly on the null device")
            assert rc == 0
        finally:
            self._cleanup(proc, stray)
        # Nothing may have shelled out from the start path either.
        trips = (sandbox["canary"] / "trips.log").read_text().strip()
        assert trips == "", f"`api start` shelled out during the probe: {trips}"

    def test_a_stray_holding_the_pipe_is_detected(self, sandbox, tripwires):
        """PREMISE (positive control): the posture above can fail.

        Same driver, same harness, with the detach neutralised — i.e. the
        pre-fix tree. If this test ever reaches EOF quickly, the EOF assertion
        in the fixed posture is blind and the whole file is decoration.

        "Blind" is now disproven by state, in three parts, none a duration:

        1. the stray reports its own fd 1 as a FIFO and not the null device;
        2. the stray's marker BYTES arrive on this test's read end — which only
           a holder of the write end can cause;
        3. with those bytes drained, a zero-timeout probe reports the pipe
           `empty` rather than `eof`, i.e. a writer is attached right now.

        (3) alone was the whole test before, phrased as "nothing arrived for
        4 s". That phrasing cannot be proven by waiting, because silence is only
        evidence while you know somebody is talking — (1) and (2) are the
        somebody, and they are the reason this control now fails loudly if the
        harness ever stops being able to see a leak.
        """
        proc, report = self._spawn(sandbox, leak=True)
        stray = None
        try:
            info = _wait_for_report(report, proc)
            stray = info["pid"]
            _wait_for_caller_exit(proc, "leak posture")
            assert _alive(stray)
            assert not (info["fd0"] and info["fd1"] and info["fd2"]), (
                "the control did not reproduce the leak: fd 0/1/2 are already "
                f"on the null device ({info}) — the harness cannot see the bug")
            assert info["fd1_fifo"], (
                f"the leaky stray's fd 1 is not a FIFO ({info}) — it is not "
                "holding the caller's pipe, so this control proves nothing")

            # (2) the positive signal, waited for: the stray's own bytes.
            kind, out = _read_until(proc.stdout, _stray_spoke, SENTINEL_GUARD)
            assert STRAY_SENTINEL in out, (
                "the stray never wrote to fd 1 — nothing arrived on the "
                f"caller's pipe ({out!r}), so this harness cannot see a leak "
                "and the fixed-posture EOF assertion is blind")
            # (3) with its bytes drained, the pipe is not at EOF.
            assert kind != "eof", (
                "the harness reached EOF with the stray still attached to the "
                f"pipe (out={out!r}) — the fixed-posture test would pass while "
                "blind")
            assert kind == "empty", (
                f"unexpected pipe state {kind!r} after draining the stray's "
                f"marker: {out!r}")
            assert _alive(stray), (
                "the pipe closed because the stray died, not because it held "
                "the descriptor")
        finally:
            self._cleanup(proc, stray)

    def test_no_stray_survives_the_probe(self, sandbox):
        """Hygiene: the reaper used above is the one that keeps CI clean."""
        proc, report = self._spawn(sandbox, leak=False)
        info = _wait_for_report(report, proc)
        stray = info["pid"]
        _wait_for_caller_exit(proc, "hygiene posture")
        _read_until(proc.stdout, _eof_state, EOF_DEADLINE)
        proc.stdout.close()
        assert proc.returncode == 0
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
        driver.write_text(_driver_source())
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
