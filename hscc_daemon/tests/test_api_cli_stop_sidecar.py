"""`hscc api stop` must tear the gateway sidecar down, not just exit.

WHY THIS FILE EXISTS (card t_56552ecc, found while fixing t_6eff6258).
`api_cli._serve` owns the live ``hermes serve`` sidecar and tears it down in
its ``finally:`` (``_load_gateway_bridge()`` -> ``_bridge2.shutdown()``, then
``api.clear_api_state()``). But ``_sigterm_handler`` answered SIGTERM/SIGINT
with ``daemon_ops.write_stopped(...)`` followed by ``os._exit(0)``, and
``os._exit`` does not run ``finally:`` blocks. So the stop path — the ordinary
``hscc api stop`` verb, which SIGTERMs the pid-file instance at
``api_cli.py:874`` whenever the unit is not launchd-managed — never ran the
teardown: the supervised serve survived holding the API process's token, which
is the orphaned-sidecar / stale-token 403 outcome the handler was written to
prevent, reached from the other entry point.

The fix makes the stop path COOPERATIVE (the shape t_6eff6258 gave
``api_server.serve_main``): the handler sets a stop flag and asks the live
server to shut down, ``_serve`` honours the flag before serving and inside the
serving loop, and the teardown stays in the one ``finally:``.

WHAT IS RUN FOR REAL here: the actual ``hscc_daemon.api_cli`` verbs, in a
child process with a redirected HOME, a fixture token and an ephemeral loopback
port. Two things are made observable, and both are the point:

  * the sidecar, which is a REAL subprocess the fake bridge spawns and kills,
    so "torn down" is a dead pid rather than a flag a test set itself; and
  * whether the exit went THROUGH ``_serve``'s ``finally:``, which is exactly
    what ``os._exit`` skips.

Hermetic by construction: HOME points at a throwaway dir (so ``API_PID_FILE``
/ ``API_LOG_FILE`` / the token all resolve inside the sandbox), the API binds
an ephemeral loopback port, and ``gateway_bridge`` is a fake the test writes —
the live ``hermes serve`` is never spawned and the operator's live API is never
touched. The stop child also FENCES ``launchctl``: it stubs ``launchd_status``
to "not available" (the sandbox has a pid file, which would otherwise make
``_launchd_matters()`` ask the operator's live gui domain, and a loaded unit
would make the stop bootout the LIVE job) and makes ``_launchctl_run`` raise,
so a future edit that routes around the stub reddens here instead of touching
the machine.
"""
import ast
import collections
import inspect
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hscc_daemon.api_cli as api_cli

REPO = Path(__file__).resolve().parents[2]

#: Fixture DATA, never a real credential, and never printed in a message.
TOKEN = "sandbox-dummy-token-for-stop-sidecar"

# Lines the fake sidecar / harness write, kept as the single constants the
# waits and the assertions share: a rename in the harness reddens the pin that
# couples them rather than turning a case into a 30 s timeout.
SIDECAR_STARTED = "sidecar started"
SIDECAR_SHUTDOWN = "sidecar shutdown called"
SIDECAR_STOPPED = "sidecar stopped"
SERVE_RETURNED = "harness: _serve returned"

# What the fake bridge / harness read out of the environment. Named once, here.
ENV_SIDECAR_DIR = "HSCC_FAKE_SIDECAR_DIR"
ENV_GATEWAY_DIR = "HSCC_FAKE_GATEWAY_DIR"
ENV_REPO_ROOT = "HSCC_REPO_ROOT"


# --------------------------------------------------------------------------- #
# the fake sidecar: a real subprocess, so "gone" means a dead pid
# --------------------------------------------------------------------------- #

FAKE_BRIDGE = '''\
"""Stand-in for hscc-api/gateway_bridge.py, written by the test.

Only the two functions `api_cli._serve` calls are real work: `start_serve`
spawns an actual subprocess (the "sidecar") and records its pid, `shutdown`
terminates it. Everything else is the inert surface the module contract
requires. The lifecycle it records is the assertion: a hard exit skips
`shutdown`, and the pid it started stays alive.
"""
import os
import subprocess
import sys
import threading

STATE = os.environ["@@SIDECAR_DIR_ENV@@"]
_LOCK = threading.Lock()
_proc = None


def _mark(text):
    with open(os.path.join(STATE, "marker.log"), "a") as fh:
        fh.write(text + "\\n")
        fh.flush()


def start_serve(port=None, bin_path=None, **flags):
    global _proc
    with _LOCK:
        if _proc is not None and _proc.poll() is None:
            return True
        _proc = subprocess.Popen(
            [sys.executable, os.path.join(STATE, "dummy_sidecar.py"), STATE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(os.path.join(STATE, "sidecar.pid"), "w") as fh:
            fh.write(str(_proc.pid))
        _mark("@@STARTED@@ pid=%s port=%s" % (_proc.pid, port))
        return True


def shutdown():
    global _proc
    with _LOCK:
        proc, _proc = _proc, None
    _mark("@@SHUTDOWN@@")
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _mark("@@STOPPED@@")


def is_serve_up():
    return _proc is not None and _proc.poll() is None


def serve_endpoint():
    return None


def ensure_mounted(project, registry_path=None):
    return False


def mounted(project):
    return False


def _reset_for_tests():
    pass
'''
# Tokens rather than %-formatting: this template is itself Python source that
# uses %, and `%%`-escaping inside a nested code literal is how such a harness
# silently stops matching the constants it is supposed to embed.
FAKE_BRIDGE = (FAKE_BRIDGE
               .replace("@@SIDECAR_DIR_ENV@@", ENV_SIDECAR_DIR)
               .replace("@@STARTED@@", SIDECAR_STARTED)
               .replace("@@SHUTDOWN@@", SIDECAR_SHUTDOWN)
               .replace("@@STOPPED@@", SIDECAR_STOPPED))

DUMMY_SIDECAR = '''\
"""The stand-in `hermes serve`: exist, hold a pid, wait to be killed.

Deliberately does nothing else. Its only job is to be a process that outlives
the API when the API hard-exits without calling `shutdown()`.
"""
import time

time.sleep(600)
'''

HARNESS = '''\
"""Sandbox harness: run the REAL `hscc api` verbs (or the serve loop alone).

argv: <verb> [verb args...]   and the paths out of the environment, so no
machine-derived path has to be a spawn argument of the caller's Popen.

  start / stop  -> straight into `api_cli.cmd_api`, the dispatch `hscc` itself
                   reaches. `start` double-forks, so this process exits once the
                   server grandchild is on its own — like the real CLI.
  serve         -> `api_cli._serve` in THIS process, for the arm that needs an
                   observable exit code and a witness that the run came back
                   through _serve's finally: instead of being cut short.

The fake gateway_bridge is imported BEFORE anything else:
`api_cli._load_gateway_bridge()` puts the hscc-api dir on sys.path and then
`import gateway_bridge`, so the only reliable seam is a module that is already
in sys.modules — that is what makes this process start a FAKE sidecar.
"""
import os
import sys

sys.path.insert(0, os.environ["@@REPO_ROOT_ENV@@"])
sys.path.insert(0, os.environ["@@GATEWAY_DIR_ENV@@"])
import gateway_bridge  # noqa: F401  (the fake; claimed before hscc-api's dir)

from hscc_daemon import api_cli

verb, rest = sys.argv[1], sys.argv[2:]

if verb == "serve":
    # Install the stop handlers EXACTLY the way `hscc api start` does in its
    # forked child (api_cli._handle_start). This arm skips the double fork only
    # so the exit code stays observable, and skipping the registration would
    # leave the SIGTERM answered by the kernel's default action — the case
    # would then measure the harness, not the handler. Pin
    # TestStopPathIsCooperative.test_the_serving_process_is_given_the_handler
    # keeps `_handle_start` registering both signals, so this line cannot drift
    # away from production on its own.
    import signal as _signal
    _signal.signal(_signal.SIGTERM, api_cli._sigterm_handler)
    _signal.signal(_signal.SIGINT, api_cli._sigterm_handler)

    _api = api_cli._load_api_server()
    api_cli._serve(_api, ["127.0.0.1"], False, int(rest[0]))
    print("@@SERVE_RETURNED@@", flush=True)
    raise SystemExit(0)

if verb == "stop":
    # Never ask the operator's live gui domain. A sandbox pid file makes
    # _launchd_matters() True, so the probe would otherwise really run here;
    # the stub says "no supervisor", which is the truth inside this sandbox,
    # and the fence turns any route around it into a red test.
    api_cli.launchd_status = lambda **kw: {
        "available": False, "loaded": False, "pid": None, "state": None}

    def _fence(argv, timeout=10):
        raise AssertionError(
            "a sandboxed stop asked the live supervisor: %s" % (argv,))

    api_cli._launchctl_run = _fence

raise SystemExit(api_cli.cmd_api([verb] + list(rest)))
'''
HARNESS = (HARNESS
           .replace("@@REPO_ROOT_ENV@@", ENV_REPO_ROOT)
           .replace("@@GATEWAY_DIR_ENV@@", ENV_GATEWAY_DIR)
           .replace("@@SERVE_RETURNED@@", SERVE_RETURNED))


# --------------------------------------------------------------------------- #
# sandbox
# --------------------------------------------------------------------------- #

def _free_port():
    """An ephemeral loopback port, released before the child binds it."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _child_env(home):
    """Env for a sandboxed child: the operator's state must not be reachable.

    HOME is the redirect that matters (it is what `os.path.expanduser` on
    ``API_PID_FILE`` / ``API_LOG_FILE`` and the server's ``DEFAULT_HSCC_DIR``
    resolve through), and the drops keep a real profile home, a real hermes
    home, a real profiles-root intent, or a forced launchd probe from leaking
    into a child that is supposed to believe it lives in tmp.
    """
    drop = {"PYTHONPATH", "VIRTUAL_ENV", "HERMES_HOME", "HERMES_REAL_HOME",
            "HSCC_HOME", "HSCC_DIR", "HSCC_LAUNCHD_PROBE",
            "HSCC_API_PROFILES_ROOT", "HSCC_SPARKRUN_BIN",
            "HSCC_TEST_PY"}
    env = {k: v for k, v in os.environ.items()
           if k not in drop and not k.startswith("CONDA_")
           and not k.startswith("HERMES_")}
    env.update({"HOME": str(home), "NO_COLOR": "1", "TERM": "dumb"})
    return env


@pytest.fixture()
def sandbox(tmp_path):
    """A throwaway HOME plus the fake bridge, harness and sidecar script."""
    home = tmp_path / "home"
    hscc = home / ".hscc"
    hscc.mkdir(parents=True)
    (hscc / "api-token").write_text(TOKEN + "\n")

    gateway = tmp_path / "fake_gateway"
    gateway.mkdir()
    (gateway / "gateway_bridge.py").write_text(FAKE_BRIDGE)

    sidecar_dir = tmp_path / "sidecar"
    sidecar_dir.mkdir()
    (sidecar_dir / "dummy_sidecar.py").write_text(DUMMY_SIDECAR)

    harness_dir = tmp_path / "harness"
    harness_dir.mkdir()
    harness = harness_dir / "api_harness.py"
    harness.write_text(HARNESS)

    env = _child_env(home)
    env.update({ENV_SIDECAR_DIR: str(sidecar_dir),
                ENV_GATEWAY_DIR: str(gateway),
                ENV_REPO_ROOT: str(REPO)})
    box = {"home": home, "hscc": hscc, "gateway": gateway,
           "sidecar": sidecar_dir, "harness": harness, "env": env,
           "marker": sidecar_dir / "marker.log",
           "sidecar_pid": sidecar_dir / "sidecar.pid",
           "pid_file": hscc / "api.pid", "log": hscc / "api.log"}
    yield box
    # No test may leave a bound API or an orphaned sidecar behind for the next
    # one: both are read from the sandbox files the children wrote.
    for path, sig in ((box["pid_file"], signal.SIGKILL),
                      (box["sidecar_pid"], signal.SIGKILL)):
        pid = _read_int(path)
        if pid is not None and _alive(pid):
            try:
                os.kill(pid, sig)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# small observations
# --------------------------------------------------------------------------- #

def _read_int(path):
    try:
        return int(Path(path).read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _marker_lines(sandbox):
    try:
        return (sandbox["marker"]).read_text().splitlines()
    except FileNotFoundError:
        return []


def _have(sandbox, needle, timeout=15.0):
    """True once the fake bridge has recorded `needle`."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(needle in line for line in _marker_lines(sandbox)):
            return True
        time.sleep(0.05)
    return False


def _describe(sandbox, extra=""):
    """Everything a reviewer needs when a case goes red, read from disk."""
    bits = [extra] if extra else []
    bits.append(f"marker: {_marker_lines(sandbox)}")
    for key in ("pid_file", "log", "sidecar_pid"):
        path = sandbox[key]
        try:
            text = Path(path).read_text()
        except FileNotFoundError:
            text = "<absent>"
        bits.append(f"{key} ({path.name}): {text[:2000]}")
    return "\n".join(bits)


#: A verb that ran to completion. `pid` is the harness's own pid — the value
#: `hscc api start` transiently writes into the pid file before the server
#: grandchild overwrites it, which is what the readiness wait filters on.
Done = collections.namedtuple("Done", ("returncode", "stdout", "stderr", "pid"))

#: Where the `start` verb's output goes. A FILE, and that is load-bearing —
#: see :func:`_start_api`.
START_OUTPUT = "start.out"


def _harness_argv(sandbox, verb, *args):
    return [sys.executable, "-u", str(sandbox["harness"]), verb, *args]


def _start_api(sandbox, port):
    """`hscc api start`, with the child's output redirected to a FILE.

    Why not `capture_output=True`, measured rather than guessed: `start`
    double-forks and the serving grandchild INHERITS the CLI parent's
    descriptors. With a pipe, the grandchild holds the write end open, so
    `communicate()` waits for an EOF that cannot arrive until the server
    exits — the first draft of this case sat there through the whole 90 s
    timeout and then hung again on the cleanup read. A redirected file has no
    EOF to wait for, and `wait()` returns when the CLI parent exits, which is
    exactly the promise `start` makes.

    stderr is folded into the same file: the CLI's own report and the
    `daemon_ops.log` echo land together, and nothing here asserts on one
    without the other.
    """
    argv = _harness_argv(sandbox, "start", "--no-qr", "--port", str(port))
    out_path = sandbox["home"].parent / START_OUTPUT
    with open(out_path, "w") as fh:
        proc = subprocess.Popen(argv, env=sandbox["env"], stdout=fh,
                                stderr=subprocess.STDOUT, text=True)
        try:
            rc = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
            raise AssertionError(
                f"`hscc api start` did not return within 60s; its own output:\n"
                f"{out_path.read_text()}\n{_describe(sandbox)}") from None
    return Done(rc, out_path.read_text(), "", proc.pid)


def _run(sandbox, verb, *args):
    """Run a NON-FORKING verb to completion and collect its output.

    `stop` fits: it signals someone else's pid and returns when that pid is
    gone, so the pipes belong to this child alone and `communicate()` ends
    when it ends. A verb that forks must go through :func:`_start_api`
    instead, for the reason written there.
    """
    proc = subprocess.Popen(_harness_argv(sandbox, verb, *args),
                            env=sandbox["env"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return Done(proc.returncode, out, err, proc.pid)


def _popen(sandbox, verb, *args):
    """Run one verb in the background (the arm that signals it afterwards)."""
    return subprocess.Popen(_harness_argv(sandbox, verb, *args),
                            env=sandbox["env"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)


def _wait_listening(sandbox, port, timeout=40.0):
    """Block until the child's OWN log says it is listening on `port`.

    The log line — not a port probe — is the readiness here: `_serve` writes it
    after every bind and it is the line the operator reads, and unlike a probe
    it also proves the child got far enough into startup to have a log.
    """
    needle = f"HSCC API listening on 127.0.0.1:{port}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = sandbox["log"].read_text()
        except FileNotFoundError:
            text = ""
        if needle in text:
            return
        time.sleep(0.05)
    raise AssertionError(
        f"the child never reached {needle!r} within {timeout}s.\n"
        f"{_describe(sandbox)}")


def _wait_serving_child(sandbox, port, not_pid, timeout=40.0):
    """:func:`_wait_listening` plus the promise `start` makes on top of it.

    `hscc api start` is a double fork, so the pid the caller must signal is not
    the pid of the CLI process it spawned: the CLI parent writes its OWN pid
    into the pid file and the server grandchild overwrites it a moment later.
    The pid to return therefore has to be alive AND not the harness's, or the
    caller would SIGTERM a process that has already exited and test nothing.
    """
    _wait_listening(sandbox, port, timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = _read_int(sandbox["pid_file"])
        if pid and pid != not_pid and _alive(pid):
            return pid
        time.sleep(0.05)
    raise AssertionError(
        f"`hscc api start` served but never left a live pid of its own in "
        f"{sandbox['pid_file'].name} within {timeout}s (the caller would have "
        f"had nothing to signal).\n{_describe(sandbox)}")


# --------------------------------------------------------------------------- #
# the user-reachable path: `hscc api start`, then `hscc api stop`
# --------------------------------------------------------------------------- #

def test_api_stop_tears_the_sidecar_down(sandbox):
    """The defect, from the verb an operator actually types.

    `hscc api stop` SIGTERMs the pid-file instance. Before the fix the handler
    answered with `os._exit(0)`, which skips `_serve`'s `finally:` — so the
    sidecar this process started stayed alive holding the token, and the marker
    showed a sidecar that was started and never shut down.

    Discriminators, all three measured on the child rather than asserted about
    it: the sidecar pid is GONE, the fake bridge recorded the shutdown, and the
    stop reported a clean stop rather than having to escalate to SIGKILL.
    """
    port = _free_port()
    started = _start_api(sandbox, port)
    assert started.returncode == 0, (
        f"`hscc api start` failed: {started.stdout}\n{started.stderr}")
    pid = _wait_serving_child(sandbox, port, not_pid=started.pid)

    stopped = _run(sandbox, "stop")
    try:
        assert stopped.returncode == 0, (
            f"`hscc api stop` failed: {stopped.stdout}\n{stopped.stderr}\n"
            f"{_describe(sandbox)}")
        out = stopped.stdout + stopped.stderr
        assert "force-killed" not in out, (
            f"the stop had to escalate to SIGKILL, which cannot be caught and "
            f"would orphan the sidecar again: {out}\n{_describe(sandbox)}")
        assert "live supervisor" not in out, (
            f"the sandboxed stop reached for the operator's launchd: {out}")

        assert _have(sandbox, SIDECAR_STARTED), (
            f"the sidecar never reported starting, so the case proved nothing "
            f"about tearing it down:\n{_describe(sandbox)}")
        assert _have(sandbox, SIDECAR_SHUTDOWN), (
            f"`hscc api stop` did NOT tear the gateway sidecar down — the stop "
            f"path exited past _serve's finally:, which is the orphaned-sidecar "
            f"defect this file exists for.\n{_describe(sandbox)}")
        assert _have(sandbox, SIDECAR_STOPPED), (
            f"the shutdown started but the sidecar was not killed: "
            f"{_describe(sandbox)}")

        sidecar_pid = _read_int(sandbox["sidecar_pid"])
        assert sidecar_pid is not None, _describe(sandbox)
        deadline = time.time() + 10
        while _alive(sidecar_pid) and time.time() < deadline:
            time.sleep(0.05)
        assert not _alive(sidecar_pid), (
            f"the sidecar (pid {sidecar_pid}) outlived `hscc api stop` — it is "
            f"orphaned, still holding this API's token.\n{_describe(sandbox)}")
        assert not sandbox["pid_file"].exists(), (
            f"the pid file survived the stop: {_describe(sandbox)}")
        # The line `_serve`'s finally: writes on the way out — the same closing
        # line `serve_main` prints (t_6eff6258), and the one thing in the log
        # that says the SERVER finished rather than that a caller signalled it.
        assert "HSCC API stopped" in sandbox["log"].read_text(), (
            f"`_serve` never reported its teardown, so the stop left no trace "
            f"of the server having finished: {_describe(sandbox)}")
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)


# --------------------------------------------------------------------------- #
# the same exit, with an observable exit code
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_the_serving_loop_returns_instead_of_being_cut_short(sandbox, sig):
    """A signalled `_serve` must RETURN, so its `finally:` gets to run.

    `os._exit(0)` exits 0 too, so the return code alone is not the point: this
    asserts the run came back through `_serve` (the harness's own line after
    the call) AND that the sidecar died. Before the fix the child exited 0 with
    the sidecar alive and no `_serve`-returned line — three witnesses for one
    mechanism.

    Both signals the handler installs are covered, and they are not equally
    sharp: measured at the base of this card, the SIGINT arm ALREADY ended
    cleanly without a handler, because an unhandled SIGINT raises
    KeyboardInterrupt and `_MultiServer.serve_forever` swallows it — so the SIGTERM
    arm is the one that separates the two behaviours (measured: rc -15 by the
    default action at base with no handler, rc 0 with the cooperative one). The
    SIGINT arm stays because it guards the other failure this fix could have
    introduced: a handler that blocks instead of setting the flag hangs the same
    signal an operator in the foreground sends.
    """
    port = _free_port()
    proc = _popen(sandbox, "serve", str(port))
    try:
        # No pid file here: this arm runs `_serve` in the foreground exactly as
        # `python -m api_server serve` does, and that entry path deliberately
        # writes none. The signal goes to the pid WE spawned, so there is no
        # double-fork to resolve and no pid file to wait for.
        _wait_listening(sandbox, port)
        assert _have(sandbox, SIDECAR_STARTED), (
            f"the sidecar never started, so nothing below proves a teardown: "
            f"{_describe(sandbox)}")
        sidecar_pid = _read_int(sandbox["sidecar_pid"])
        os.kill(proc.pid, sig)
        try:
            out = proc.communicate(timeout=25)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.communicate()[0]
            pytest.fail(f"{sig.name} did not stop the serving loop: {out}\n"
                        f"{_describe(sandbox)}")
        assert proc.returncode == 0, (
            f"{sig.name} ended the process with rc={proc.returncode} instead "
            f"of a clean cooperative stop.\n{out}\n{_describe(sandbox)}")
        assert SERVE_RETURNED in out, (
            f"the process exited WITHOUT returning through `_serve` — that is "
            f"what `os._exit` does, and it skips the finally: that owns the "
            f"sidecar teardown.\nchild output: {out}\n{_describe(sandbox)}")
        assert _have(sandbox, SIDECAR_SHUTDOWN, timeout=5), (
            f"{sig.name} never reached the sidecar teardown: "
            f"{_describe(sandbox)}")
        assert sidecar_pid is not None and not _alive(sidecar_pid), (
            f"the sidecar (pid {sidecar_pid}) survived {sig.name}: "
            f"{_describe(sandbox)}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# --------------------------------------------------------------------------- #
# the shape, pinned at source level (the behavioural cases cannot enforce it)
# --------------------------------------------------------------------------- #

class TestStopPathIsCooperative:
    """Source pin: the stop path exits THROUGH `_serve`, not past it.

    The cases above prove today's answer and would catch a hard exit that
    happens to be reached — but the mechanism they test is a signal handler,
    and a handler is a callback the interpreter runs whenever it chooses. The
    one thing that must never come back is `os._exit` in the handler, and the
    one thing that must never move out of the `finally:` is the sidecar
    teardown. Both are structural facts about two functions, so both are
    pinned structurally (same doctrine t_6eff6258 used for the ordering there).
    """

    @staticmethod
    def _body(func):
        """AST of `func`, with linenos re-based onto the real file.

        `inspect.getsource` returns only the function's own lines, so without
        the shift every number in a failure message would be a line of the
        excerpt rather than a line of api_cli.py a reviewer can open.
        """
        source, start = inspect.getsourcelines(func)
        body = ast.parse("".join(source))
        ast.increment_lineno(body, start - 1)
        return body

    @staticmethod
    def _walk(nodes):
        """`ast.walk` over one node OR a list of nodes.

        `ast.walk` accepted an iterable only from 3.13, and the CI legs run on
        3.13 exactly — so passing `Try.finalbody` straight through is one
        interpreter bump away from a TypeError inside a pin.
        """
        if isinstance(nodes, ast.AST):
            yield from ast.walk(nodes)
            return
        for node in nodes:
            yield from ast.walk(node)

    @classmethod
    def _calls(cls, body, attr):
        """Every Call whose callee ends in `attr` (bare name or attribute)."""
        found = []
        for node in cls._walk(body):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == attr:
                found.append(node)
            elif isinstance(func, ast.Attribute) and func.attr == attr:
                found.append(node)
        return found

    @staticmethod
    def _is_stop_request_check(test):
        """True for exactly `_stop_requested.is_set()`, not for something wrapping it.

        `if False and _stop_requested.is_set()` mentions the same call and means
        the opposite — a pin that matched the BoolOp would bless the mutant the
        behavioural cases cannot see (t_6eff6258 learned this the same way).
        """
        return (isinstance(test, ast.Call)
                and isinstance(test.func, ast.Attribute)
                and test.func.attr == "is_set"
                and isinstance(test.func.value, ast.Name)
                and test.func.value.id == "_stop_requested")

    def test_the_handler_does_not_hard_exit(self):
        """`os._exit(0)` in `_sigterm_handler` is the defect, so it is banned.

        `os._exit` bypasses `finally:` (and atexit, and buffered output). The
        handler may set flags and ask the server to shut down; it may not leave
        the process by any door that skips `_serve`'s teardown.
        """
        body = self._body(api_cli._sigterm_handler)
        hard = [n for n in self._walk(body)
                if isinstance(n, ast.Call)
                and ((isinstance(n.func, ast.Attribute)
                      and n.func.attr in ("_exit", "abort", "kill"))
                     or (isinstance(n.func, ast.Name)
                         and n.func.id in ("_exit", "abort")))]
        assert not hard, (
            f"_sigterm_handler hard-exits at line(s) "
            f"{[n.lineno for n in hard]}: os._exit skips _serve's finally:, so "
            f"the supervised `hermes serve` is orphaned holding the API token")

    def test_the_handler_sets_the_stop_flag_the_serving_loop_honours(self):
        """Handler and loop must share ONE flag, and the loop must pass it down.

        A handler that sets a flag nobody reads is as silent as an `os._exit`.
        Pinned as a pair on purpose: the handler setting `_stop_requested`, and
        `_serve` handing that same name to `serve_forever(stop_event=...)`, so
        a stop landing while the accept loops exist ends the loops instead of
        being filed and forgotten.
        """
        handler = self._body(api_cli._sigterm_handler)
        sets = [n for n in ast.walk(handler)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "set"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "_stop_requested"]
        assert sets, (
            "_sigterm_handler no longer sets `_stop_requested`, so the stop "
            "request has nowhere to go and the loops keep accepting")

        serve = self._body(api_cli._serve)
        served = self._calls(serve, "serve_forever")
        assert served, "_serve no longer calls serve_forever at all"
        carried = [n for n in served
                   if any(kw.arg == "stop_event" for kw in n.keywords)]
        assert carried, (
            "_serve calls serve_forever without `stop_event=`, so a stop "
            "requested after the loops exist is never acted on (the flag would "
            "be set and ignored — the orphaned-sidecar bug by another route)")
        names = {kw.value.id for n in carried for kw in n.keywords
                 if kw.arg == "stop_event" and isinstance(kw.value, ast.Name)}
        assert "_stop_requested" in names, (
            f"_serve passes {sorted(names) or 'something unnamed'} to "
            f"stop_event, not the flag the handler sets — the two halves of the "
            f"handshake would not be the same object")

    def test_the_handler_keeps_the_pid_file_bookkeeping(self):
        """The bookkeeping the handler already did stays (the card's condition).

        `hscc api stop` reads the pid file and `hscc api status` answers from
        it; making the stop cooperative must not quietly turn "stopped" into
        "still claims a server" while the teardown runs.
        """
        body = self._body(api_cli._sigterm_handler)
        assert self._calls(body, "write_stopped"), (
            "_sigterm_handler no longer marks the pid file stopped: a stop "
            "that takes a moment to tear the sidecar down would leave the pid "
            "file claiming a live server")

    def test_the_serving_process_is_given_the_handler(self):
        """The process that serves must HAVE the handler in the first place.

        `_handle_start` installs the handlers in the child, before the second
        fork, so the grandchild that goes on to serve already answers the stop
        signal — the card is explicit that the ordering here is not the defect
        (unlike `serve_main`, where the registration used to sit after the
        bind). What is worth pinning is the existence and the wiring: a child
        that never registers them falls back to the kernel's default action,
        which is the same orphaned sidecar by a third route.

        This is also what makes the harness's `serve` arm honest: it registers
        the same two signals for the same callable, and this pin is what stops
        that copy drifting from production.
        """
        body = self._body(api_cli._handle_start)
        registered = self._calls(body, "signal")
        assert registered, (
            "`hscc api start`'s child registers no signal handler, so the "
            "process that ends up serving has no answer to the stop signal and "
            "the kernel's default action skips _serve's teardown")
        # The argument of `signal.signal(signal.SIGTERM, ...)` is itself an
        # Attribute: `signal.SIGTERM`, i.e. Name('signal') + attr 'SIGTERM'.
        handled = {arg.attr for node in registered for arg in node.args
                   if isinstance(arg, ast.Attribute)
                   and isinstance(arg.value, ast.Name)
                   and arg.value.id == "signal"}
        assert {"SIGTERM", "SIGINT"} <= handled, (
            f"the child handles {sorted(handled) or 'nothing'}; `hscc api stop` "
            f"sends SIGTERM and an operator in the foreground sends SIGINT, and "
            f"either unhandled signal skips the sidecar teardown")
        served = self._calls(body, "_serve")
        assert served, "_handle_start no longer calls _serve at all"
        assert (min(n.lineno for n in registered)
                < min(n.lineno for n in served)), (
            "the registration now sits after the serve call, so the process "
            "reaches the accept loop before it can answer a stop")

    def test_the_sidecar_teardown_lives_in_the_finally(self):
        """The teardown must be in the `finally:` that wraps the serving call.

        This is the invariant the whole card turns on: `os._exit` skips
        `finally:`, so the teardown is only as reliable as the block it sits
        in. Move it after the try (or into the `else:`) and it stops being
        reached by the paths that most need it — which is the defect, restated
        as a code movement.
        """
        body = self._body(api_cli._serve)
        served = self._calls(body, "serve_forever")
        assert served, "_serve no longer calls serve_forever"
        serving_line = min(n.lineno for n in served)
        holders = [n for n in self._walk(body)
                   if isinstance(n, ast.Try) and n.finalbody
                   and n.lineno <= serving_line
                   and any(isinstance(c, ast.Call)
                           and c.lineno == serving_line
                           for c in self._walk(n.body))]
        assert holders, (
            "no try/finally wraps the serving loop in _serve, so nothing is "
            "guaranteed to tear the sidecar down when the loop ends")
        # Innermost wrapper: the block the serving call actually sits in. An
        # outer finally would still be a guarantee; teardown moved to after the
        # try is not, and that move is what this pin reddens on.
        holder = max(holders, key=lambda n: n.lineno)
        final_calls = {c.func.attr for c in self._walk(holder.finalbody)
                       if isinstance(c, ast.Call)
                       and isinstance(c.func, ast.Attribute)}
        assert "shutdown" in final_calls, (
            f"the finally: at line {holder.lineno} no longer shuts the gateway "
            f"sidecar down ({sorted(final_calls)}): the supervised `hermes "
            f"serve` would outlive the API holding its token")
        assert "clear_api_state" in final_calls, (
            f"the finally: at line {holder.lineno} no longer clears the API "
            f"state, so a restart inherits this instance's view")

    def test_a_stop_requested_before_serving_never_starts_the_loops(self):
        """The early exit is a promise, pinned because it cannot be run.

        Between the handler being installed and the accept loops existing is a
        window in which a stop arrives with nothing serving yet. The answer has
        to be "then we never serve": fall straight through to the same
        teardown. No case reaches that branch on an idle machine — signalling
        that early needs the scheduler to cooperate, which is exactly why
        t_6eff6258 pinned its equivalent rather than testing it.
        """
        body = self._body(api_cli._serve)
        early = [n for n in ast.walk(body)
                 if isinstance(n, ast.If) and self._is_stop_request_check(n.test)]
        assert early, (
            "_serve no longer asks the stop flag before serving, so a stop "
            "requested during startup serves from the backlog instead of "
            "going straight to the teardown")
        served = self._calls(body, "serve_forever")
        serving_line = min(n.lineno for n in served)
        for node in early:
            assert node.lineno < serving_line, (
                f"the stop guard at :{node.lineno} sits at or after the "
                f"serving call at :{serving_line}, so it can no longer keep "
                f"the loops from starting")
