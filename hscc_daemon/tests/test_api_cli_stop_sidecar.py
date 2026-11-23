"""`hscc api stop` must tear the gateway sidecar down, not just exit.

WHY THIS FILE EXISTS (card t_56552ecc, found while fixing t_6eff6258).
`api_cli._serve` owns the live ``hermes serve`` sidecar and tears it down in
its ``finally:`` (``_load_gateway_bridge()`` -> ``_bridge2.shutdown()``, then
``api.clear_api_state()``). But ``_sigterm_handler`` answered SIGTERM/SIGINT
with ``daemon_ops.write_stopped(...)`` followed by ``os._exit(0)``, and
``os._exit`` does not run ``finally:`` blocks. So the stop path — the ordinary
``hscc api stop`` verb, which SIGTERMs the pid-file instance in
``api_cli._handle_stop`` whenever the unit is not launchd-managed — never ran
the teardown: the supervised serve survived holding the API process's token,
which is the orphaned-sidecar / stale-token 403 outcome the handler was written
to prevent, reached from the other entry point.

The fix makes the stop path COOPERATIVE (the shape t_6eff6258 gave
``api_server.serve_main``): the handler sets a stop flag, ``_serve`` honours the
flag before serving and inside the serving loop, and the teardown stays in the
one ``finally:``. What the leg run then caught is the other half of the shape,
and it is why the handler here is allowed to do nothing but set that flag: a
signal handler runs on the thread it interrupts, so any work it does that takes
a lock can be a lock that thread already held — ``daemon_ops.log`` holds
``daemon_ops._handlers_lock`` (a plain non-reentrant ``threading.Lock``) while
it resolves the rotating handler, and a stop landing there deadlocked the
process, lost the stop, and left the sidecar for the escalation SIGKILL. The
blocking part (log line, PID-file bookkeeping, the shutdown request) therefore
lives on a helper thread, and the flag is a lock-free ``_StopFlag`` rather than
a ``threading.Event``, whose ``set()``/``is_set()`` take a lock the serving loop
holds on every pass.

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
import threading
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

#: What the `serve-lograce` child prints while it works, and what the parent
#: waits on. Constants for both sides, so a rename on one side reddens the
#: assertion that reads the other instead of turning a case into a timeout.
#: `stage:` lines carry the child's phase; the last one seen is in every failure
#: message, which is what makes a red here diagnosable without a re-run.
STAGE_PREFIX = "stage:"
#: Inside the log hook, holding `daemon_ops._handlers_lock`, BEFORE the signal is
#: raised: the window was entered.
RACE_ENTERED = "@@INSIDE-LOG-WRITE@@"
#: ...and the handler came back out of it. This is the line the case asserts on.
INTERRUPTED_INSIDE_LOG = "@@INTERRUPTED-INSIDE-LOG@@"
#: The child's watchdog thread observed that the handler had still not returned
#: `HANDLER_GRACE` seconds after the raise. An OBSERVATION, not a verdict: a
#: throttled task can also simply not have been running (measured: under ~100x
#: oversubscription this line appears while the handler is merely unscheduled),
#: so nothing reddens on it. It is what lets a child that then goes silent be
#: named — see :func:`_await_child`.
HANDLER_LATE = "@@HANDLER-LATE@@"

# ━━━ load budgets, not behaviour budgets ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# None of these can turn a wrong answer into a right one: they only decide how
# long a LIVE child may take. Every one of them is measured, and the measurement
# is why they are this size.
#
#   idle 14-core host: the child's first stdout line lands ~0.15 s after spawn
#   and the whole lograce arm (both signals) runs in 1.76 s.
#   contended GitHub runner, run 45081900690: the same child had not written ONE
#   byte of stdout — and no `api.log` at all — in the 25 s the old
#   `communicate(timeout=25)` allowed, i.e. it was still inside its imports.
#   The child had not reached the window the case is about, yet the case blamed
#   the signal handler.
#
# So the budgets are sized for a runner that is two orders of magnitude slower
# than an idle one, and a stall is judged against the child's own progress rather
# than against its whole life.
#: Silence from a live child between two announced stages (or, once the parent
#: has asked it to stop, between the signal and its exit) before it is called
#: stuck. ~1000x the idle gap, ~7x the worst CI observation.
CHILD_STAGE_STALL = 180.0
#: One child's entire life, whatever it is doing — the backstop behind the stall.
CHILD_LIFE_CAP = 420.0
#: Spawn -> the child's own `HSCC API listening on ...` line. Same import phase
#: as above, so the same order of budget.
CHILD_READY_BUDGET = 180.0
#: How long the child's watchdog waits, AFTER the signal has been raised, for the
#: handler to come back out of the log-write window before it says the handler is
#: still in there. The handler's body is an attribute write and a return, so this
#: is generous about the work; it is only ever an observation, because a
#: throttled task can also be unscheduled (see :data:`HANDLER_LATE`).
HANDLER_GRACE = 8.0
#: How long a marker the child writes BEFORE it exits may take to land on disk.
CHILD_MARKER_BUDGET = 60.0

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

# The child's first words, BEFORE anything that a loaded runner can stretch.
# The parent waits on this file's progress (:func:`_await_child`), so a stage
# line has to exist before the imports that ran 25 s long on CI — otherwise the
# parent has nothing to be patient about and guesses from a stopwatch.
print("@@MARK_STAGE@@ harness-up", flush=True)

sys.path.insert(0, os.environ["@@REPO_ROOT_ENV@@"])
sys.path.insert(0, os.environ["@@GATEWAY_DIR_ENV@@"])
import gateway_bridge  # noqa: F401  (the fake; claimed before hscc-api's dir)
print("@@MARK_STAGE@@ fake-bridge-imported", flush=True)

from hscc_daemon import api_cli
print("@@MARK_STAGE@@ api_cli-imported", flush=True)

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

    print("@@MARK_STAGE@@ serve-enter", flush=True)
    _api = api_cli._load_api_server()
    api_cli._serve(_api, ["127.0.0.1"], False, int(rest[0]))
    print("@@SERVE_RETURNED@@", flush=True)
    raise SystemExit(0)

if verb == "serve-lograce":
    # The state a stop has to survive: the signal lands while THIS thread is
    # inside a log write. `daemon_ops.log` -> `append_rotating` -> `_get_logger`
    # holds `daemon_ops._handlers_lock`, a plain non-reentrant `threading.Lock`,
    # and a signal handler runs on the thread it interrupts — so a handler that
    # logs (as `api_cli._sigterm_handler` did until t_56552ecc measured the
    # trap) waits on a lock only its own thread can release. Nothing else in the
    # stop path then happens: no flag, no return from `_serve`, no sidecar
    # teardown, and `hscc api stop` escalates to SIGKILL, which skips the
    # teardown too.
    #
    # Not a pretend log write: it takes the same lock the real write takes. It
    # fires on the FIRST log line of startup, which is where a stop from the
    # ordinary `stop` verb actually lands — the API logs several times before an
    # accept loop exists, and the leg run that first caught this hit exactly
    # that window. `_serve` then goes on to bind and start the sidecar, so the
    # teardown still has a real child to kill, and the stop flag the handler set
    # is what keeps the accept loops from ever starting.
    import signal as _signal
    import threading as _threading
    import time as _time
    from hscc_daemon import daemon_ops

    _signum = getattr(_signal, rest[1])
    _real_log = daemon_ops.log
    _fired = []
    _raised = _threading.Event()
    _came_back = _threading.Event()

    def _watchdog():
        # The trap this arm exists for is a handler that never comes back, and the
        # only witness that can see it is a thread the blocked thread is not
        # holding: `threading.Lock.acquire()` gives up the GIL while it waits, so
        # this thread still runs while the main thread is stuck inside the
        # handler. It measures from the RAISE, not from its own birth — measured
        # the hard way under ~100x oversubscription, a grace clocked from here
        # accused the handler of a deadlock while the child was still crossing
        # `_load_api_server()` and had not raised anything yet.
        #
        # And it only OBSERVES. A task the kernel has simply not scheduled looks
        # identical to a task deadlocked on a lock from the outside, so this line
        # can be a slow runner as easily as a bug; what turns it into a verdict is
        # the child then going silent for the whole stall budget, which is what
        # :func:`_await_child` decides.
        _raised.wait()
        if not _came_back.wait(@@HANDLER_GRACE@@):
            print("@@MARK_LATE@@ %s had still not returned %.0fs after the raise"
                  % (rest[1], @@HANDLER_GRACE@@), flush=True)

    _threading.Thread(target=_watchdog, daemon=True).start()

    def _log(msg, *a, **kw):
        if not _fired:
            _fired.append(str(msg))
            with daemon_ops._handlers_lock:   # exactly where the real write stands
                print("@@MARK_RACE@@", flush=True)   # the window is entered
                _raised.set()                # the grace clock starts HERE
                _signal.raise_signal(_signum)  # the stop lands here, on this thread
                _came_back.set()              # the handler came back; stand down
                _time.sleep(0.4)              # a handler that waits, waits here
            print("@@MARK_INTERRUPTED@@", flush=True)
        return _real_log(msg, *a, **kw)

    daemon_ops.log = _log
    _signal.signal(_signal.SIGTERM, api_cli._sigterm_handler)
    _signal.signal(_signal.SIGINT, api_cli._sigterm_handler)

    print("@@MARK_STAGE@@ serve-enter", flush=True)
    _api = api_cli._load_api_server()
    api_cli._serve(_api, ["127.0.0.1"], False, int(rest[0]))
    daemon_ops.log = _real_log
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
           .replace("@@SERVE_RETURNED@@", SERVE_RETURNED)
           .replace("@@MARK_STAGE@@", STAGE_PREFIX)
           .replace("@@MARK_RACE@@", RACE_ENTERED)
           .replace("@@MARK_INTERRUPTED@@", INTERRUPTED_INSIDE_LOG)
           .replace("@@MARK_LATE@@", HANDLER_LATE)
           .replace("@@HANDLER_GRACE@@", repr(HANDLER_GRACE)))


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


def _have(sandbox, needle, timeout=CHILD_MARKER_BUDGET):
    """True once the fake bridge has recorded `needle`.

    The bridge writes these from the API process, so the wait crosses a process
    boundary and is a load wait like every other one in this file.
    """
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


# --------------------------------------------------------------------------- #
# waiting on a child: progress, not elapsed time
# --------------------------------------------------------------------------- #

class _ChildTrail:
    """A live child's stdout, kept as it arrives, with a silence clock on it.

    Why not `communicate(timeout=N)`: that judges the child's WHOLE LIFE against
    one stopwatch, so a runner that is merely slow at `import` reddens a case
    whose behaviour is fine. Measured, run 45081900690: the `serve-lograce` child
    had written no stdout and no `api.log` in 25 s — it never reached the
    log-write window the case is about — and the case reported the signal handler
    wedged. Nothing was wedged; the child had not been asked yet.

    Here the wait is keyed on progress. The harness announces bounded stages
    (every gap between two of them is one import step), each line resets this
    clock, and the budget therefore only runs out on a child that has stopped
    doing anything — not on one that is doing a lot of it slowly.
    """

    def __init__(self, proc):
        self._proc = proc
        self._lines = []
        self._lock = threading.Lock()
        self._since = time.monotonic()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        try:
            for line in self._proc.stdout:
                with self._lock:
                    self._lines.append(line.rstrip("\n"))
                    self._since = time.monotonic()
        except (ValueError, OSError):
            # the pipe went away under us (the caller closed it after the child
            # died); whatever was collected is still the truth up to that point.
            pass

    def mark(self):
        """Restart the silence clock: the parent is about to ask for something.

        A serving child is SILENT by design, so a stall measured across the whole
        run would execute a healthy server. The parent marks at the moment it
        signals, and from there the clock measures exactly the property under
        test — how long the stop takes to produce a word.
        """
        with self._lock:
            self._since = time.monotonic()

    def idle(self):
        with self._lock:
            return time.monotonic() - self._since

    def text(self):
        with self._lock:
            return "\n".join(self._lines)

    def saw(self, needle):
        return needle in self.text()

    def last(self):
        with self._lock:
            return self._lines[-1] if self._lines else "<nothing at all>"

    def finish(self, timeout=10.0):
        """Once the child is gone: drain what it buffered, then close the pipe."""
        self._thread.join(timeout)
        try:
            self._proc.stdout.close()
        except (ValueError, OSError):
            pass


#: What :func:`_await_child` settled for. `clean` is the only verdict the child
#: earned by itself; `stalled` and `wedged` both mean the parent had to kill it,
#: and `detail` says which clock ran out.
ChildRun = collections.namedtuple(
    "ChildRun", ("returncode", "output", "verdict", "detail"))


def _kill_child(proc):
    if proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def _stuck_verdict(trail):
    """`wedged` when the child itself said the handler was still in the window.

    The `@@HANDLER-LATE@@` line ALONE is not a verdict — a task the scheduler
    simply passed over looks identical from here, and under ~100x oversubscription
    this file's own rig measured it firing on a child that then finished fine.
    But a child that said it and then produced NOTHING for the whole stall budget
    is describing a lock it cannot leave, which is the defect this file exists for.
    The line supplies the diagnosis; the silence supplies the verdict.
    """
    return "wedged" if trail.saw(HANDLER_LATE) else "stalled"


def _await_child(proc, trail, *, stall=CHILD_STAGE_STALL, cap=CHILD_LIFE_CAP):
    """Wait for a backgrounded child to exit, judging SILENCE not elapsed time.

    Returns a :data:`ChildRun`:

      ``clean``    the child exited on its own; `returncode` is its and the
                   caller asserts on what it did.
      ``stalled``  the parent had to kill it: a live child announced nothing for
                   `stall` seconds, or outlived `cap` while still talking, with no
                   such observation on record. `detail` says which clock ran out.
      ``wedged``   the child went silent, and its own watchdog thread (see the
                   `serve-lograce` harness) had said the handler was still inside
                   the log-write window when the raise happened. The diagnosis is
                   the child's; the silence is what makes it a verdict.

    There is deliberately no separate ``capped`` verdict: a child that will not
    finish and a child that will not speak are the same finding for a caller, and
    which clock ran out belongs in `detail` rather than in a branch nobody reads.

    In either of the other two the child has already been killed when this
    returns. The price of the design is that a genuine wedge now costs the stall
    budget to
    report instead of a few seconds; a case that accuses a healthy child on a
    busy runner costs the fleet a re-run triage every time it fires, and this one
    has already done exactly that on CI.
    """
    deadline = time.monotonic() + cap
    while True:
        returncode = proc.poll()
        if returncode is not None:
            trail.finish()
            return ChildRun(returncode, trail.text(), "clean", "")
        idle = trail.idle()
        if idle > stall:
            _kill_child(proc)
            trail.finish()
            return ChildRun(None, trail.text(), _stuck_verdict(trail),
                            f"went silent for {idle:.0f}s after {trail.last()!r}")
        if time.monotonic() > deadline:
            _kill_child(proc)
            trail.finish()
            return ChildRun(None, trail.text(), _stuck_verdict(trail),
                            f"still alive after {cap:.0f}s, last line "
                            f"{trail.last()!r}")
        time.sleep(0.05)


def _wait_listening(sandbox, port, timeout=CHILD_READY_BUDGET):
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


def _wait_serving_child(sandbox, port, not_pid, timeout=CHILD_READY_BUDGET):
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
    trail = _ChildTrail(proc)
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
        # From here the silence IS the measurement. A serving child has nothing
        # to say, so the stall clock is restarted at the moment we ask it to stop
        # (see :meth:`_ChildTrail.mark`) instead of running from its spawn — the
        # one window that must not be patient is the one we just opened.
        trail.mark()
        os.kill(proc.pid, sig)
        run = _await_child(proc, trail)
        out = run.output
        if run.verdict != "clean":
            pytest.fail(f"{sig.name} did not stop the serving loop: the child "
                        f"{run.verdict} ({run.detail}).\n"
                        f"child output: {out}\n{_describe(sandbox)}")
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


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_landing_inside_a_log_write_still_stops(sandbox, sig):
    """A stop arriving mid-log-write must still tear the sidecar down.

    The stop path is a signal handler, and a handler runs on the thread it
    interrupted. `daemon_ops.log` holds `daemon_ops._handlers_lock` — a plain,
    NON-reentrant `threading.Lock` — while `_get_logger` resolves the rotating
    handler, so a handler that logs (as this file's did) waits on a lock only its
    own thread can release. Measured at the base of this card, both signals hang
    the child there forever: the flag is never set, `_serve` never returns, and
    the SIGKILL that `hscc api stop` escalates to then skips the teardown the
    card exists to guarantee.

    This is the case the leg run wrote itself: `..._being_cut_short`'s SIGINT arm
    failed once in ~4800 items with exactly that signature — the child's log
    ending at its startup lines, no `Received signal` line, the sidecar alive. The
    window is milliseconds wide (a few log writes during startup) and the
    scheduler decides, so the harness takes the lock on purpose instead of waiting
    for the lottery.

    What stays production-shaped afterwards: the binds happen, the fake sidecar
    really starts, the stop flag the handler set is what keeps the accept loops
    from starting, and the `finally:` still has a real child to kill.

    How it waits, and why not on a clock: the signal is raised by the CHILD, so
    there is no parent-side moment to aim at — all the parent can observe is what
    the child says. The child therefore announces the window it is standing in
    (`@@INSIDE-LOG-WRITE@@`, lock held and all) and the parent waits on SILENCE
    rather than on elapsed life (:func:`_await_child`). A runner slow enough to
    spend this case's whole old 25 s budget inside `import` reddened it on CI
    (run 45081900690, both arms, empty stdout and no `api.log`) with the child not
    yet entered into the handler at all — and the child had no way to say so.
    """
    port = _free_port()
    proc = _popen(sandbox, "serve-lograce", str(port), sig.name)
    trail = _ChildTrail(proc)
    try:
        run = _await_child(proc, trail)
        out = run.output
        if run.verdict == "wedged":
            # The child's own watchdog thread — the only witness that can still
            # run while the main thread is blocked in `Lock.acquire()` — had said
            # the handler was still inside the window, and the child then said
            # nothing at all for the whole stall budget. Defect named by the
            # process that has it, and confirmed by the silence after it.
            pytest.fail(
                f"{sig.name} arriving inside a log write never stopped the "
                f"process — the handler is blocked on a lock its own thread holds "
                f"(`daemon_ops._handlers_lock`), so the stop flag was never set "
                f"and the sidecar teardown was never reached ({run.detail})."
                f"\nchild output: {out}\n{_describe(sandbox)}")
        if run.verdict != "clean":
            # No verdict from the child, so no claim about its mechanism. The old
            # case asserted the deadlock here on nothing but a stopwatch and it
            # was wrong: on CI the child had not reached the window at all.
            pytest.fail(
                f"{sig.name} was raised inside a log write and the child never "
                f"came back — it {run.verdict} ({run.detail}). Nothing above it "
                f"was reached, so this says nothing about the handler."
                f"\nchild output: {out}\n{_describe(sandbox)}")
        assert INTERRUPTED_INSIDE_LOG in out, (
            "the signal never landed inside a log write, so this case says "
            f"nothing about that window.\nchild output: {out}\n"
            f"{_describe(sandbox)}")
        assert proc.returncode == 0, (
            f"{sig.name} landed inside a log write and the process left by a "
            f"door other than `_serve`: rc={proc.returncode}.\n{out}\n"
            f"{_describe(sandbox)}")
        assert SERVE_RETURNED in out, (
            f"the run ended without returning through `_serve`, so the "
            f"finally: that owns the sidecar was skipped.\n{out}\n"
            f"{_describe(sandbox)}")
        assert _have(sandbox, SIDECAR_STARTED, timeout=5), (
            "the sidecar never started, so a teardown here would prove nothing "
            f"about the stop path: {_describe(sandbox)}")
        sidecar_pid = _read_int(sandbox["sidecar_pid"])
        assert _have(sandbox, SIDECAR_SHUTDOWN, timeout=5), (
            f"{sig.name} arrived inside a log write and the sidecar teardown was "
            f"never reached: {_describe(sandbox)}")
        assert sidecar_pid is not None and not _alive(sidecar_pid), (
            f"the sidecar (pid {sidecar_pid}) survived a {sig.name} that arrived "
            f"inside a log write: {_describe(sandbox)}")
        # The closing line is written by `_serve`'s finally:, on the thread that
        # was interrupted. The handler's own line is written by the helper thread
        # and is best-effort by construction — this is the one an operator has a
        # right to find, so it is the one pinned here.
        assert "HSCC API stopped" in sandbox["log"].read_text(), (
            "no closing line in the API log: the teardown did not finish, or it "
            f"moved off the guaranteed path: {_describe(sandbox)}")
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
        """The bookkeeping the handler always did stays (the card's condition).

        `hscc api stop` reads the pid file and `hscc api status` answers from it;
        making the stop cooperative must not quietly turn "stopped" into "still
        claims a server" while the teardown runs.

        It lives in `_serve`'s `finally:` — the block a cooperative stop always
        reaches — and that is where this pin looks. It does NOT live in the
        handler, and must not: the handler may not call anything at all (see
        `test_the_handler_holds_nothing_the_serving_thread_holds`), and the
        `finally:` already wrote this file before the card existed. The invariant
        the card set — a stop must still mark the pid file stopped — is unchanged;
        only the function that owns it is, and it owns it for every exit path
        rather than only the signalled one.
        """
        body = self._body(api_cli._serve)
        assert self._calls(body, "write_stopped"), (
            "nothing in the stop path marks the pid file stopped: a stop that "
            "takes a moment to tear the sidecar down would leave the pid file "
            "claiming a live server (`hscc api status` answers from that file)")

    def test_the_handler_holds_nothing_the_serving_thread_holds(self):
        """The handler body is two operations, and neither one may block.

        A signal handler runs on the thread it interrupts, so anything it does
        that takes a lock can be a lock that thread was already holding. That is
        not hypothetical in this file: the handler opened with `daemon_ops.log`,
        which holds `daemon_ops._handlers_lock` (a plain non-reentrant
        `threading.Lock`) while `_get_logger` resolves the rotating handler, and a
        stop landing there deadlocked the process — the flag was never set,
        `_serve` never returned, and the sidecar was orphaned by the escalation
        SIGKILL. The behavioural case
        `test_a_signal_landing_inside_a_log_write_still_stops` reaches that state;
        this pin is what keeps a third operation from being added back.

        So the body is allowed one call — `_stop_requested.set()` — plus the
        plain module write that records which signal arrived. Nothing else: no
        logging, no pid-file write, no `server.shutdown()`, and no
        `threading.Thread(...).start()` either (this fix used that for one commit,
        to move the log line off the handler; starting a thread takes threading's
        bookkeeping locks and then waits for the new thread to report itself
        started, so the handler is no longer bounded by the work it delegated).
        The flag is deliberately not a `threading.Event` either, whose `set()` /
        `is_set()` take a lock the serving loop holds on every pass.
        """
        body = self._body(api_cli._sigterm_handler)
        # `_body` parses the function's own source, so the module it lands in has
        # the def in it — the statements to judge are the FunctionDef's body.
        fn = body.body[0]
        assert isinstance(fn, ast.FunctionDef), (
            "the excerpt of `_sigterm_handler` no longer parses to a function, so "
            "this pin is reading something else")
        stmts = list(fn.body)
        if (stmts and isinstance(stmts[0], ast.Expr)
                and isinstance(stmts[0].value, ast.Constant)
                and isinstance(stmts[0].value.value, str)):
            stmts = stmts[1:]                     # the docstring, not a statement

        def _is_flag_set(node):
            return (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "set"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "_stop_requested")

        def _callee(calls):
            """Readable names for a failure message, without assuming the shape."""
            names = []
            for c in calls:
                func = c.func if isinstance(c, ast.Call) else None
                if isinstance(func, ast.Attribute):
                    names.append(func.attr)
                elif isinstance(func, ast.Name):
                    names.append(func.id)
                else:
                    names.append(type(func).__name__)
            return names

        calls = [n.value for n in stmts if isinstance(n, ast.Expr)]
        flag = [c for c in calls if _is_flag_set(c)]
        assert len(calls) == len(flag) == 1, (
            f"_sigterm_handler makes {len(calls)} call(s) ({_callee(calls)}); the "
            "body is allowed exactly one — `_stop_requested.set()`. A handler runs "
            "on the thread it interrupts, so any call can wait on a lock that "
            "thread already holds, and a handler that never returns loses the stop "
            "outright: no flag, no return from `_serve`, no sidecar teardown, and "
            "`hscc api stop` escalating to the SIGKILL that skips the teardown as "
            "well. Measured three ways at t_56552ecc — `daemon_ops.log` "
            "(`daemon_ops._handlers_lock`), `threading.Thread(...).start()` "
            "(threading's bookkeeping, plus waiting for the thread it just made), "
            "and `threading.Event` (below)")
        assert all(isinstance(n, (ast.Global, ast.Assign, ast.Expr))
                   for n in stmts), (
            f"_sigterm_handler contains "
            f"{[type(n).__name__ for n in stmts]}; only the record of which signal "
            "arrived and the flag write are allowed, and both are plain writes")
        stray = [t.id for n in stmts if isinstance(n, ast.Assign)
                 for t in n.targets
                 if isinstance(t, ast.Name) and t.id != "_last_signal"]
        assert not stray, (
            f"_sigterm_handler writes {stray}; the only module state a handler may "
            "touch is `_last_signal` (a plain write) and the flag — anything else "
            "is state some other thread is holding a lock over")
        assert not isinstance(api_cli._stop_requested, threading.Event), (
            "`_stop_requested` is a threading.Event again: Event.set()/is_set() "
            "take the event's internal lock, and the serving loop calls is_set() "
            "on every pass — a stop landing there would have the handler wait on "
            "a lock its own thread holds, which is the deadlock this pin exists "
            "for. `_StopFlag` is the lock-free answer")

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
