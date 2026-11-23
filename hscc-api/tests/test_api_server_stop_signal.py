"""The stop path of `python -m api_server serve` may not hold or take a lock.

`serve_main` installs its SIGTERM/SIGINT handler before it binds, so a stop can
land at any point of startup or serving — and a signal handler runs ON THE
THREAD it interrupts. Every call the handler makes is therefore a call that may
need a lock that very thread is already holding, and a handler that waits on
such a lock never returns: the flag is never set, `serve_forever` never ends,
`serve_main`'s `finally:` never runs, and the supervisor's escalation (SIGKILL)
skips the gateway-sidecar teardown along with it.

What this file measures (probe, this interpreter, both signals):

  * `threading.Event`'s internal lock IS that lock. `Event._cond._lock` is a
    plain non-reentrant `threading.Lock`, `Event.is_set()` takes it, and
    `_MultiServer.serve_forever` asks `is_set()` on every pass of its join
    loop. Landing the stop while the serving thread holds it hangs a handler
    that calls `Event.set()` forever — `test_the_event_shape_is_the_hazard`
    below reproduces it, and `serve_main` used to carry exactly that handler.
  * the stdout write path was probed the same way (stop landing while the same
    thread is inside a `TextIOWrapper` write) and did NOT hang on this
    interpreter. The `print` still comes out of the handler, because a handler
    may not make a call whose locking it has to borrow from someone else's
    design — but this file does not claim a measured hang for it.

The replacement shape is the one t_56552ecc measured in the sibling plugin's
stop path (`hscc_daemon/api_cli.py`): a handler body of plain attribute writes
plus one call — `set()` on a lock-free `_StopFlag` — and the operator-visible
lines on the `finally:` that a cooperative stop always reaches.
`server.shutdown()` is not in the stop path at all: `BaseServer.shutdown()`
waits for the serving loop to return, and on this path the serving loop is the
interrupted thread.

How this file WAITS (t_eed6b74d — the shape the sibling file arrived at first):

  * Never a whole-life stopwatch. This file used to hand every child
    `communicate(timeout=25)` and call an expiry a deadlock, which accused the
    signal handler of a child that had merely not finished starting yet. A
    child announces bounded stages instead, each line resets a silence clock,
    and the budgets below are LOAD budgets: they only run out on a child that
    stopped doing anything.
  * Never a reserved port. `port_override=0` and the port the child READS BACK
    on its own `hscc-api: listening on …` line.
  * Every child is its own process group and the file owns it: the case's
    `finally:` AND the fixture teardown kill the GROUP, so a descendant (the
    real `gateway_bridge` spawns `hermes serve`) cannot outlive the run. The
    guard arms at the bottom prove that on the failure path, not just the happy
    one.

Hermetic: a throwaway HOME (fixture token, never the operator's), a port the OS
assigned to the child, and a FAKE `gateway_bridge` so the sidecar markers are
this file's own files rather than a supervised `hermes serve`.
"""
import ast
import collections
import contextlib
import inspect
import os
import re
import signal
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path

import pytest

import api_server

REPO = Path(__file__).resolve().parents[2]
API_DIR = REPO / "hscc-api"

# ━━━ load budgets, not behaviour budgets ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# None of these can turn a wrong answer into a right one: they only decide how
# long a LIVE child may take. What they replace is the shape that bounced a
# merge-queue gate: `STOP_BOUND_S = 25.0` judged a child's WHOLE LIFE against one
# stopwatch and called the expiry a wedged signal handler, so a child that was
# merely slow reddened a case about a deadlock. The sibling plugin measured the
# failure mode directly (hscc_daemon/tests/test_api_cli_stop_sidecar.py, run
# 45081900690): on a contended runner that child had written not one byte of
# stdout in 25 s — it had never entered the window the case was about.
#
# Sized from this file's own baseline (t_eed6b74d, load ~3 on a 14-core host):
# the whole file ran 13 items in 23.65 s, i.e. ~1.8 s per child, and a cold
# `import api_server` in a sandboxed child costs 0.05-0.15 s. Every budget here
# is two orders of magnitude above that, which is the point: they must not be
# reachable by a slow runner, only by a child that has stopped making progress.
#: Silence from a live child between two announced lines before it is called
#: stuck. The verdict it earns depends on WHICH line was last — see
#: :func:`_stop_verdict`.
CHILD_STAGE_STALL = 120.0
#: One child's entire life, whatever it is doing — the backstop behind the stall,
#: for a child that keeps talking and still never finishes.
CHILD_LIFE_CAP = 360.0
#: Spawn -> the child's own `hscc-api: listening on …` line (the import phase,
#: which is exactly the phase a fixed 25 s bound used to execute unfairly).
CHILD_READY_BUDGET = 120.0
#: How long a marker the child fsyncs BEFORE it exits may take to be readable.
#: The child fsyncs first, so this is only ever the OS being behind.
CHILD_MARKER_BUDGET = 60.0
#: The CHILD's own backstop: how long the `stop-after-ready` arm waits for
#: readiness before it signals anyway. Injected into the harness from here
#: (:func:`_program_text`) so the two sides cannot drift — the bug this replaces
#: had the child burning up to 30 s waiting to signal inside the parent's 25 s
#: bound, two numbers in one file disagreeing about who waits for whom.
CHILD_SIGNAL_BACKSTOP = 20.0
#: The premise case's grace, and its polarity is INVERTED on purpose: the
#: deadlock is the pass. It is a grace measured AFTER the child announced it was
#: inside the Event-lock window, never from spawn.
PREMISE_GRACE = 10.0
#: How long a pid this file killed may take to stop answering `kill(pid, 0)`.
CHILD_GONE_BUDGET = 15.0

#: The line `serve_main` prints as the LAST thing before serve_forever(): the
#: handlers are installed and every listener is bound. Duplicated here (not
#: imported from test_api_server_entrypoint.py) because that file owns the
#: readiness contract and this one only waits on it.
READY_LINE = "hscc-api: ready"

#: What the operator is owed when a stop arrives: which signal, and that the
#: teardown finished. Both must come from the path that always runs.
SIGNAL_LINE = re.compile(r"^hscc-api: signal (\d+), shutting down$", re.MULTILINE)
STOPPED_LINE = "hscc-api: stopped"

#: The port the child actually bound, printed by `serve_main` from the RESOLVED
#: address (api_server.py:1522, adopted for every listener by `create_servers`
#: when the request is port 0, api_server.py:1166-1168). This is how this file
#: gets a port: the child says which one it owns.
LISTENING_LINE = re.compile(r"^hscc-api: listening on 127\.0\.0\.1:(\d+)$",
                            re.MULTILINE)

#: What the children announce. Constants on both sides of the pipe: the child
#: program is a string, so a drift between what it prints and what a case waits
#: for is a silent pass — `test_the_announced_stages_are_named_once` pins it.
INTERRUPTED_FLAG_POLL = "@@INTERRUPTED-INSIDE-FLAG-POLL@@"
READY_OBSERVED = "@@READY-OBSERVED@@"
READY_BACKSTOP_EXPIRED = "@@READY-BACKSTOP-EXPIRED@@"
SERVE_FOREVER_RETURNED = "@@SERVE_FOREVER_RETURNED@@"
#: A prefix: the child appends the return code.
SERVE_MAIN_RETURNED = "@@SERVE_MAIN_RETURNED rc="
INTERRUPTED_EVENT_LOCK = "@@INTERRUPTED-INSIDE-EVENT-LOCK@@"
MAIN_RESUMED = "@@MAIN-RESUMED@@"
GUARD_ALIVE = "@@GUARD-ALIVE"
GUARD_KID = "@@GUARD-KID"

#: What `pgrep -f` looks for to answer "did anything of this run survive".
GUARD_TAG_PREFIX = "hscc-stop-signal-guard"

# Marker file values written by the fake gateway_bridge in the child.
SIDECAR_STARTED = "started"
SIDECAR_SHUTDOWN = "shutdown"

# The token is DATA — a fixture value, never a real credential.
TOKEN = "stop-signal-dummy-token"

ENV_API_DIR = "HSCC_API_TEST_API_DIR"
ENV_MARKERS = "HSCC_API_TEST_MARKERS"
ENV_GUARD_TAG = "HSCC_API_TEST_GUARD_TAG"

#: The port this file asks each child for. `0` = "the OS picks, you keep it",
#: which is the only honest answer on a host that runs a queue gate and several
#: workers' own legs at once.
OS_ASSIGNED_PORT = "0"


def _parse_method(func):
    """AST of a method, which `inspect.getsourcelines` hands back INDENTED.

    Parsing the excerpt as-is is an IndentationError, and the pin would then be
    red for a reason that has nothing to do with the shape it is reading.
    """
    source, _start = inspect.getsourcelines(func)
    return ast.parse(textwrap.dedent("".join(source)))


def _sandbox_box(tmp_path):
    """A throwaway HOME plus the two marker files the fake sidecar writes.

    A generator so the same body serves the fixture AND the guard arm that
    finalizes it the way pytest does (`next()`), which is the only way to prove
    the teardown reaches a child a case never reaped.
    """
    home = tmp_path / "home"
    hscc = home / ".hscc"
    hscc.mkdir(parents=True)
    (hscc / "api-token").write_text(TOKEN + "\n")
    markers = tmp_path / "sidecar.markers"
    markers.write_text("")
    # What the launchd unit actually hands the process: HOME + PATH and little
    # else. PYTHONPATH/VIRTUAL_ENV/CONDA_* are dropped so the child resolves
    # api_server the way the unit does, not the way this pytest process does.
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "VIRTUAL_ENV")
           and not k.startswith("CONDA_")
           and k not in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE",
                         "HERMES_DELEGATED_CHILD_CONTEXT", "HSCC_SPARKRUN_BIN")}
    env.update({"HOME": str(home), "NO_COLOR": "1", "TERM": "dumb",
                ENV_API_DIR: str(API_DIR), ENV_MARKERS: str(markers)})
    box = {"home": home, "hscc": hscc, "env": env, "markers": markers,
           "procs": []}
    yield box
    # Every child this fixture spawned is reaped even when a case died before its
    # own `finally:` (`_popen` uses start_new_session, so `_reap` reaches the
    # child's whole group, descendants included). t_7c2eb604 found harness
    # children orphaned for hours on this host; a pytest run must not leave one
    # behind — `test_the_sandbox_fixture_reaps_a_child_a_case_never_reaped` is
    # the arm that keeps this loop honest.
    for proc in list(box["procs"]):
        _reap(proc)


@pytest.fixture()
def sandbox(tmp_path):
    """The fixture form of :func:`_sandbox_box`."""
    yield from _sandbox_box(tmp_path)


# --------------------------------------------------------------------------- #
# the child programs
# --------------------------------------------------------------------------- #

HARNESS = r'''
import os
import signal
import sys
import time

sys.path.insert(0, os.environ["@@API_DIR_ENV@@"])

#: A FAKE gateway_bridge, claimed before anything imports the real one:
#: serve_main does `import gateway_bridge` inside the function, so a module
#: already in sys.modules is the only reliable seam. It records start/stop in
#: the marker file, which is what lets the parent assert the sidecar teardown
#: really ran instead of trusting a log line.
_MARKERS = os.environ["@@MARKERS_ENV@@"]


class _FakeBridge:
    @staticmethod
    def start_serve(*a, **kw):
        _mark("@@SIDECAR_STARTED@@")
        return True

    @staticmethod
    def shutdown():
        _mark("@@SIDECAR_SHUTDOWN@@")


def _mark(word):
    with open(_MARKERS, "a") as fh:
        fh.write(word + "\n")
        fh.flush()
        os.fsync(fh.fileno())


sys.modules["gateway_bridge"] = _FakeBridge

import api_server  # noqa: E402

# The premise of the inherited-mask case: name every signal this child started
# with BLOCKED, so that case can prove it measured a blocked stop rather than a
# spawner that forgot to block one. Printed before serve_main installs anything.
for _blocked_sig in signal.pthread_sigmask(signal.SIG_BLOCK, set()):
    print(f"@@BLOCKED-{_blocked_sig.name}@@", flush=True)

arm, port, signame = sys.argv[1], int(sys.argv[2]), sys.argv[3]
SIGNUM = getattr(signal, signame)


def _hold_the_flag_lock_and_land_the_stop(flag):
    """Stop this thread while it is inside the serving loop's flag poll.

    `flag` is the very object serve_main's handler will set — the join loop of
    `_MultiServer.serve_forever` calls `flag.is_set()` on every pass, so "the
    serving thread is inside is_set()" is the state a real stop lands in.

    An `Event` keeps a plain non-reentrant `threading.Lock` in `_cond`, so
    taking it here puts the thread in exactly the state `Event.set()` from the
    handler deadlocks against. A lock-free flag has no such lock to take: the
    stop then lands at the same point in the loop with nothing held, which is
    the state the fix is allowed to be interrupted in.
    """
    cond = getattr(flag, "_cond", None)
    if cond is not None:                 # the shape that can be fatal
        with cond:
            print("@@INTERRUPTED-INSIDE-FLAG-POLL@@", flush=True)
            signal.raise_signal(SIGNUM)
            time.sleep(0.5)              # a handler that waits, waits here
    else:                                # lock-free flag: nothing to hold
        flag.is_set()
        print("@@INTERRUPTED-INSIDE-FLAG-POLL@@", flush=True)
        signal.raise_signal(SIGNUM)
        time.sleep(0.5)


if arm == "stop-inside-flag-poll":
    # Hand the stop to the serving thread at the moment it owns the flag the
    # handler is about to set: wrap the method that receives that object and is
    # about to poll it, land the stop, then let the real loop run. The loop's
    # first pass sees the flag, so the assertion below is about the loops
    # ENDING, not about the harness.
    _real_serve_forever = api_server._MultiServer.serve_forever

    def _serve_forever(self, poll_interval=0.5, stop_event=None):
        _hold_the_flag_lock_and_land_the_stop(stop_event)
        rc = _real_serve_forever(self, poll_interval=poll_interval,
                                 stop_event=stop_event)
        print("@@SERVE_FOREVER_RETURNED@@", flush=True)
        return rc

    api_server._MultiServer.serve_forever = _serve_forever
    rc = api_server.serve_main(port_override=port)
    print(f"@@SERVE_MAIN_RETURNED rc={rc}@@", flush=True)
    raise SystemExit(rc if rc == 0 else 1)

if arm == "stop-after-ready":
    # An ordinary cooperative stop: serve until the child has said it is ready,
    # then signal from a second thread, exactly as launchd / `hscc api stop`
    # does. Nothing here takes a lock first, so the case measures the operator-
    # visible lines and the sidecar teardown rather than the deadlock window.
    import threading

    _ready = threading.Event()

    # The parent's stall budget is a multiple of this one (:data:
    # CHILD_SIGNAL_BACKSTOP, injected from the parent's constant by
    # `_program_text`), so a child that is slow to become ready says so and
    # then stops itself — it can never sit inside the parent's patience waiting
    # for permission to be tested. That is the pair this file used to get
    # backwards: 30 s of waiting here, 25 s of patience out there.
    _READY_BACKSTOP = @@SIGNAL_BACKSTOP@@

    def _signal_later():
        if not _ready.wait(_READY_BACKSTOP):
            print("@@READY-BACKSTOP-EXPIRED@@", flush=True)
        os.kill(os.getpid(), SIGNUM)

    _real_serve_forever = api_server._MultiServer.serve_forever

    def _serve_forever(self, poll_interval=0.5, stop_event=None):
        print("@@READY-OBSERVED@@", flush=True)
        _ready.set()
        return _real_serve_forever(self, poll_interval=poll_interval,
                                   stop_event=stop_event)

    api_server._MultiServer.serve_forever = _serve_forever
    threading.Thread(target=_signal_later, daemon=True).start()
    rc = api_server.serve_main(port_override=port)
    print(f"@@SERVE_MAIN_RETURNED rc={rc}@@", flush=True)
    raise SystemExit(rc if rc == 0 else 1)

raise SystemExit("unknown arm " + arm)
'''

PREMISE = r'''
"""The hazard itself, with no hscc code in it.

A stop that arrives while this thread holds the lock `Event.set()` needs does
not come back. `Event._cond._lock` is a plain non-reentrant Lock and
`Event.is_set()` takes it, which is why a serving loop that polls an Event on
every pass is a loop a handler must not `set()`.
"""
import os
import signal
import sys
import time
import threading


def handler(signum, frame):
    os.write(2, b"@@IN-HANDLER@@\n")
    EVENT.set()                       # the lock this thread already holds
    os.write(2, b"@@HANDLER-RETURNED@@\n")


EVENT = threading.Event()
signal.signal(getattr(signal, sys.argv[1]), handler)
with EVENT._cond:
    os.write(2, b"@@INTERRUPTED-INSIDE-EVENT-LOCK@@\n")
    signal.raise_signal(getattr(signal, sys.argv[1]))
    time.sleep(0.5)
os.write(2, b"@@MAIN-RESUMED@@\n")
'''

GUARD = r'''
"""A child that stays alive, with a real descendant inside its process group.

The ownership claim this file makes — spawn as a session leader, kill by GROUP —
is worth nothing unless it reaches the descendants. The production shape it
stands in for is `gateway_bridge.start_serve()`, which spawns `hermes serve` as a
CHILD of the API process; killing the API alone would orphan the serve, and
t_7c2eb604 measured one of those alive for hours on this host. A marker file
cannot show that, so the guard arm's child spawns a real (sleeping) process whose
argv carries a per-run tag: `pgrep -f <tag>` is then an outside witness to
whether the group really died.

Nothing here burns CPU — a load test belongs to scripts/load_burners.sh — and the
sleep is bounded, so even a guard arm that fails to kill leaves nothing behind
after two minutes.
"""
import os
import subprocess
import sys
import time

tag = os.environ["@@GUARD_TAG_ENV@@"]
print("@@GUARD-ALIVE pid=%d pgid=%d@@" % (os.getpid(), os.getpgid(0)),
      flush=True)
_kid = subprocess.Popen([sys.executable, "-u", "-c",
                         "import time; time.sleep(120)", tag])
print("@@GUARD-KID pid=%d@@" % _kid.pid, flush=True)
time.sleep(120)
'''


# --------------------------------------------------------------------------- #
# spawning, and owning what was spawned
# --------------------------------------------------------------------------- #

def _program_text(program):
    """A child program with its placeholders filled in.

    The budget the CHILD waits on is injected from the parent's constant rather
    than written again in the harness string: a second literal is exactly how
    "the child waits up to 30 s" came to live inside "the parent allows 25 s".
    """
    return (globals()[program]
            .replace("@@API_DIR_ENV@@", ENV_API_DIR)
            .replace("@@MARKERS_ENV@@", ENV_MARKERS)
            .replace("@@SIDECAR_STARTED@@", SIDECAR_STARTED)
            .replace("@@SIDECAR_SHUTDOWN@@", SIDECAR_SHUTDOWN)
            .replace("@@SIGNAL_BACKSTOP@@", repr(CHILD_SIGNAL_BACKSTOP))
            .replace("@@GUARD_TAG_ENV@@", ENV_GUARD_TAG))


def _popen(sandbox, *args, program="HARNESS"):
    """Run a child program in its OWN process group, recorded for the teardown.

    `start_new_session` is what makes `proc.pid` the group id, so `_reap` can
    take the child and every descendant it spawned with one `killpg` — and it
    works from the fixture teardown too, which is the path that runs when a case
    dies mid-flight (see the guard arms below).
    """
    script = Path(sandbox["markers"]).parent / (program.lower() + ".py")
    script.write_text(_program_text(program))
    proc = subprocess.Popen(
        [sys.executable, "-u", str(script), *args],
        cwd=str(API_DIR), env=sandbox["env"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True)
    sandbox["procs"].append(proc)
    return proc


def _popen_with_blocked_signal(sandbox, blocked, *args, program="HARNESS"):
    """Spawn with `blocked` BLOCKED in the forking thread, so the child inherits it.

    `signal.signal()` in the child sets only the disposition; POSIX carries the
    thread's signal MASK across fork/exec, and `subprocess` never clears it. The
    block lives in a throwaway thread (masks are per-thread), leaving pytest's
    own Ctrl-C handling alone.
    """
    box = {}

    def spawn():
        old = signal.pthread_sigmask(signal.SIG_BLOCK, set(blocked))
        try:
            box["proc"] = _popen(sandbox, *args, program=program)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old)

    thread = threading.Thread(target=spawn)
    thread.start()
    thread.join()
    return box["proc"]


def _reap(proc):
    """SIGKILL the child's process group (it is a session leader) and reap it."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


# --------------------------------------------------------------------------- #
# waiting on a fact, not on a clock
# --------------------------------------------------------------------------- #

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


def _wait_for(predicate, timeout, interval=0.05):
    """Bounded state poll. Every wait in this file is one of these.

    The `timeout` is a load budget, never the verdict: a case that needs a fact
    asserts the fact and uses this only to stop waiting for one forever.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _wait_for_marker(sandbox, word, timeout=CHILD_MARKER_BUDGET):
    """Wait for the fake sidecar to record `word` — the witness, read from disk.

    The child fsyncs the marker before it exits, so this normally returns on the
    first pass; it is here so "torn down" is a fact that was waited for rather
    than a race that was won.
    """
    return _wait_for(lambda: word in _markers(sandbox), timeout)


def _markers(sandbox):
    return [ln.strip() for ln in sandbox["markers"].read_text().splitlines()
            if ln.strip()]


def _survivors(tag):
    """PIDs whose argv still carries `tag` — i.e. anything of this run alive."""
    proc = subprocess.run(["pgrep", "-f", tag], capture_output=True, text=True)
    if proc.returncode == 1:                     # no match: the green state
        return []
    assert proc.returncode == 0, (
        f"pgrep could not answer (rc={proc.returncode}): {proc.stderr!r}")
    return [int(pid) for pid in proc.stdout.split()]


class _ChildTrail:
    """A live child's output, kept as it arrives, with a silence clock on it.

    Why not `communicate(timeout=N)`: that judges the child's WHOLE LIFE against
    one stopwatch, so a runner that is merely slow at `import` reddens a case
    whose behaviour is fine — and, worse, reddens it with the deadlock message.
    Here the wait is keyed on progress: the harness announces bounded stages
    (`hscc-api: listening on …`, `hscc-api: ready`, the `@@…@@` markers) and each
    line resets this clock, so a budget only runs out on a child that has
    stopped doing anything.
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
            # the pipe went away under us (the reaper closed it after the child
            # died); whatever was collected is still the truth up to that point.
            pass

    def idle(self):
        with self._lock:
            return time.monotonic() - self._since

    def text(self):
        with self._lock:
            return "\n".join(self._lines)

    def saw(self, needle):
        return needle in self.text()

    def last(self):
        """The stage the child died in — in every failure message."""
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
#: and `detail` says which budget ran out.
ChildRun = collections.namedtuple(
    "ChildRun", ("returncode", "output", "verdict", "detail"))


def _stop_verdict(trail):
    """`wedged` ONLY when the child had actually reached the stop window.

    This is the whole point of the rewrite. A child that announced
    `@@INTERRUPTED-INSIDE-FLAG-POLL@@` and then fell silent is describing the
    defect this file exists for: the handler was dispatched into a thread that
    owns the flag's lock and never came back. A child that fell silent BEFORE
    that line never entered the window at all — it was slow, or it died in its
    imports — and calling that a wedged signal handler is the mis-attribution
    that bounced a queue gate for a PR that touched neither file.
    """
    return "wedged" if trail.saw(INTERRUPTED_FLAG_POLL) else "stalled"


def _await_child(proc, trail, *, stall=CHILD_STAGE_STALL, cap=CHILD_LIFE_CAP):
    """Wait for a backgrounded child to exit, judging SILENCE not elapsed time.

      ``clean``    the child exited on its own; `returncode` is its and the
                   caller asserts on what it did.
      ``stalled``  the parent had to kill it after `stall` seconds of silence (or
                   `cap` of a life) with no stop-window line on record.
      ``wedged``   the same, but the child HAD announced the stop window: the
                   deadlock, named by the child's own last line.

    In the two latter cases the child has already been killed when this returns.
    The price of the design is that a genuine wedge now costs the stall budget to
    report instead of a few seconds; a case that accuses a healthy child on a
    busy runner costs the fleet a re-run triage every time it fires, and this
    family has already paid for exactly that on CI.
    """
    deadline = time.monotonic() + cap
    while True:
        returncode = proc.poll()
        if returncode is not None:
            trail.finish()
            return ChildRun(returncode, trail.text(), "clean", "")
        idle = trail.idle()
        if idle > stall:
            _reap(proc)
            trail.finish()
            return ChildRun(None, trail.text(), _stop_verdict(trail),
                            f"went silent for {idle:.0f}s after {trail.last()!r}")
        if time.monotonic() > deadline:
            _reap(proc)
            trail.finish()
            return ChildRun(None, trail.text(), _stop_verdict(trail),
                            f"still alive after {cap:.0f}s, last line "
                            f"{trail.last()!r}")
        time.sleep(0.05)


def _expect_clean(proc, trail, *, arm):
    """:func:`_await_child` with the only verdict a stop case accepts: clean.

    The message names the stage the child died in and says which of the two
    findings it is, so a red here is diagnosable from the report alone.
    """
    run = _await_child(proc, trail)
    if run.verdict == "clean":
        return run
    _reap(proc)
    if run.verdict == "wedged":
        why = ("the child REACHED the flag-poll window, the stop landed there and "
               "nothing came back out of it: a handler waiting on a lock the "
               "interrupted thread holds never returns, so the flag is never set, "
               "serve_forever never ends and serve_main's finally: — the sidecar "
               "teardown — never runs.")
    else:
        why = ("the child never reached the stop window this case is about: it "
               "went silent before announcing it, which is a child that was still "
               "starting or died in its imports — NOT a wedged signal handler. "
               "Accusing the handler of this is the mis-attribution this file was "
               "rewritten to stop making.")
    pytest.fail(f"[{arm}] the child came to no verdict: {run.verdict} "
                f"({run.detail}).\n{why}\n"
                f"last stage: {trail.last()!r}\n"
                f"child output:\n{run.output}")


def _resolved_port(out, trail):
    """The port the child bound, taken from the child's own line.

    `_free_port()` used to bind port 0, CLOSE the socket and hand the number to
    the child to bind. Between the close and the child's bind that number
    belongs to the whole host, and this host runs a queue gate plus several
    workers' legs at once: a stolen port killed the child on EADDRINUSE and the
    case reported a deadlock. `port_override=0` makes the kernel the allocator
    and `serve_main` prints the RESOLVED port, so nothing is ever reserved.
    """
    matches = LISTENING_LINE.findall(out)
    if not matches:
        pytest.fail(f"the child never printed its resolved port "
                    f"({LISTENING_LINE.pattern!r}); without it this case cannot "
                    f"say which listener it served.\n"
                    f"last stage: {trail.last()!r}\nchild output:\n{out}")
    assert len(set(matches)) == 1, (
        f"the child bound {sorted(set(matches))}; `create_servers` adopts ONE "
        "ephemeral port for every listener, so more than one means the port-0 "
        "contract moved out from under this file")
    port = int(matches[0])
    assert 0 < port <= 65535, f"resolved port {port} is not a bound port"
    return port


# --------------------------------------------------------------------------- #
# behaviour: a stop from the state that holds a lock
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_stop_landing_inside_the_flag_poll_still_stops(sandbox, sig):
    """The acceptance case: hold the lock, land the stop, expect a clean exit.

    `_MultiServer.serve_forever` asks `stop_event.is_set()` on every pass of its
    join loop. With `stopping` a `threading.Event`, that poll holds the event's
    own non-reentrant lock, and the handler `set()`-ing the same event waits on
    it forever — measured at the base of this card, both signals, the child
    never returns (see `test_the_event_shape_is_the_hazard`, which reproduces
    the same state with no hscc code in it).

    The window is milliseconds wide and the scheduler decides, so the harness
    takes the lock on purpose rather than waiting for the lottery — the same
    move t_56552ecc made in the sibling plugin after its leg flaked once in
    ~4800 items.

    What stays production-shaped afterwards: `serve_main` really binds (on a
    port the OS gave it), the fake sidecar really starts, the real
    `serve_forever` really runs and returns, the `finally:` really tears the
    sidecar down, and the process exits 0.
    """
    proc = _popen(sandbox, "stop-inside-flag-poll", OS_ASSIGNED_PORT, sig.name)
    trail = _ChildTrail(proc)
    run = _expect_clean(proc, trail, arm="stop-inside-flag-poll")
    out = run.output
    try:
        _resolved_port(out, trail)
        assert INTERRUPTED_FLAG_POLL in out, (
            "the stop never landed inside the serving loop's flag poll, so this "
            f"case says nothing about that window.\nchild output:\n{out}")
        assert SIDECAR_STARTED in _markers(sandbox), (
            "the sidecar never started, so the teardown below would prove "
            f"nothing: {out}")
        assert SERVE_FOREVER_RETURNED in out, (
            "the serving loops never ended: the stop was lost, which is what a "
            f"handler blocked on its own thread's lock does.\n{out}")
        assert f"{SERVE_MAIN_RETURNED}0@@" in out, (
            f"serve_main left by a door other than its finally:.\n{out}")
        assert run.returncode == 0, (
            f"{sig.name} ended the process with rc={run.returncode} instead of "
            f"a cooperative stop; the sidecar would be orphaned.\n{out}")
        assert STOPPED_LINE in out, (
            f"no {STOPPED_LINE!r}: the teardown path was not reached.\n{out}")
        assert _wait_for_marker(sandbox, SIDECAR_SHUTDOWN), (
            "the gateway sidecar outlived the API: the teardown lives in the "
            f"finally: a lost stop never reaches.\n{out}")
    finally:
        _reap(proc)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_the_event_shape_is_the_hazard(sandbox, sig):
    """The premise, measured on this interpreter with no hscc code involved.

    The polarity here is INVERTED on purpose and must stay that way: the hazard
    is a real deadlock, so SILENCE is the pass and coming back is the red. If a
    future CPython makes `Event`'s lock reentrant, this case goes red first and
    says the deadlock the fix is justified by no longer exists — which is worth
    knowing before the fix is ever questioned on cost.

    What changed with t_eed6b74d is not the polarity but where the clock starts:
    the grace runs from the child's own `@@INTERRUPTED-INSIDE-EVENT-LOCK@@`
    line, after a bounded wait that PROVES the child entered the window. Judged
    from spawn, a child that had not started yet looked exactly like the hazard.
    """
    proc = _popen(sandbox, sig.name, program="PREMISE")
    trail = _ChildTrail(proc)
    try:
        assert _wait_for(lambda: trail.saw(INTERRUPTED_EVENT_LOCK),
                         CHILD_READY_BUDGET), (
            f"the premise child never entered the Event-lock window, so nothing "
            f"was measured and its silence would prove nothing.\n"
            f"child output:\n{trail.text()}")
        if not _wait_for(lambda: proc.poll() is not None
                                or trail.saw(MAIN_RESUMED), PREMISE_GRACE):
            return                      # silent inside the window: the hazard
        pytest.fail(
            f"{sig.name} arriving while the thread held Event._cond came back: "
            f"the Event-shape hazard this card is built on no longer reproduces "
            f"on {sys.version.split()[0]}.\nchild output:\n{trail.text()}")
    finally:
        _reap(proc)
        trail.finish()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_cooperative_stop_names_its_signal_and_tears_the_sidecar_down(
        sandbox, sig):
    """The operator-visible lines must come from the path that always runs.

    A plain `hscc api stop`-shaped stop: the child serves, says it is ready, a
    second thread signals it. The lines the operator reads in ~/.hscc/logs/api.log
    — which signal arrived, and that the server stopped — are asserted here, and
    `TestStopPathHoldsNothing` pins where they are allowed to live. The marker
    file is the witness that the teardown was real, not merely printed, and the
    pid check is the witness that it is really gone.
    """
    proc = _popen(sandbox, "stop-after-ready", OS_ASSIGNED_PORT, sig.name)
    trail = _ChildTrail(proc)
    run = _expect_clean(proc, trail, arm="stop-after-ready")
    out = run.output
    try:
        port = _resolved_port(out, trail)
        assert READY_LINE in out, (
            f"the child never reached readiness (last stage {trail.last()!r}); "
            f"the stop it answered was a startup stop, not a served one.\n{out}")
        assert f"{SERVE_MAIN_RETURNED}0@@" in out, (
            f"serve_main left by a door other than its finally:.\n{out}")
        assert run.returncode == 0, (
            f"{sig.name} ended the process with rc={run.returncode}; the "
            f"supervised sidecar would be orphaned.\n{out}")
        matched = SIGNAL_LINE.search(out)
        assert matched, (
            f"nothing named the arriving signal ({SIGNAL_LINE.pattern!r}): an "
            f"operator reading api.log cannot tell a stop from a crash.\n{out}")
        assert int(matched.group(1)) == sig.value, (
            f"the line names signal {matched.group(1)}, not {sig.value}:\n{out}")
        assert STOPPED_LINE in out, (
            f"no {STOPPED_LINE!r} after {sig.name}: the run did not come back "
            f"through the teardown.\n{out}")
        assert _wait_for_marker(sandbox, SIDECAR_SHUTDOWN), (
            f"the sidecar was never torn down: {_markers(sandbox)}\n{out}")
        assert _markers(sandbox) == [SIDECAR_STARTED, SIDECAR_SHUTDOWN], (
            "the sidecar was not started-then-torn-down by this run: "
            f"{_markers(sandbox)}\n{out}")
    finally:
        _reap(proc)
    # "Torn down" is two facts, and this is the second one: the marker landed
    # above, and the pid is now gone. A child that still answers `kill(pid, 0)`
    # after a cooperative stop is the orphan this file keeps being called for.
    assert not _alive(proc.pid), (
        f"{sig.name} stopped the API on port {port} but left pid {proc.pid} "
        f"alive behind it.\nchild output:\n{out}")


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_stop_signal_the_spawner_blocked_is_still_honoured(sandbox, sig):
    """`serve_main` must clear an inherited BLOCK, not just install a handler.

    t_7c2eb604: one full-leg run failed BOTH SIGINT arms of the stop cases and
    neither SIGTERM arm. `signal.signal()` sets a DISPOSITION; the thread's
    signal MASK survives fork/exec and `subprocess` never clears it. A unit
    started by a supervisor that runs with SIGINT blocked would therefore have
    a handler that is never dispatched: the stop stays pending, `serve_forever`
    never sees the flag, and this process's sidecar is orphaned by the SIGKILL
    escalation.

    The child here is spawned with `sig` blocked and says so on stdout before
    `serve_main` runs (the premise assertion), then stops itself after ready —
    the ordinary cooperative path. Without the mask clear in `serve_main` the
    child goes silent inside the window and `_stop_verdict` calls it wedged;
    with it the run goes through the `finally:` and tears the sidecar down.
    """
    proc = _popen_with_blocked_signal(
        sandbox, {sig}, "stop-after-ready", OS_ASSIGNED_PORT, sig.name)
    trail = _ChildTrail(proc)
    run = _expect_clean(proc, trail, arm="stop-after-ready/blocked")
    out = run.output
    try:
        _resolved_port(out, trail)
        assert f"@@BLOCKED-{sig.name}@@" in out, (
            f"the child did not start with {sig.name} blocked, so this case "
            f"would not measure an inherited mask.\nchild output:\n{out}")
        assert f"{SERVE_MAIN_RETURNED}0@@" in out, (
            f"serve_main left by a door other than its finally:.\n{out}")
        assert run.returncode == 0, (
            f"{sig.name} inherited as BLOCKED ended the process with "
            f"rc={run.returncode}; the supervised sidecar would be orphaned."
            f"\n{out}")
        assert STOPPED_LINE in out, (
            f"no {STOPPED_LINE!r} after a blocked {sig.name}: the run did not "
            f"come back through the teardown.\n{out}")
        assert _wait_for_marker(sandbox, SIDECAR_SHUTDOWN), (
            f"the sidecar was never torn down: {_markers(sandbox)}\n{out}")
        assert _markers(sandbox) == [SIDECAR_STARTED, SIDECAR_SHUTDOWN], (
            "the sidecar was not started-then-torn-down by this run: "
            f"{_markers(sandbox)}\n{out}")
    finally:
        _reap(proc)
    assert not _alive(proc.pid), (
        f"a blocked {sig.name} was honoured by the teardown but left pid "
        f"{proc.pid} alive.\nchild output:\n{out}")


# --------------------------------------------------------------------------- #
# the ownership contract, proven on the failure path (t_eed6b74d)
# --------------------------------------------------------------------------- #
#
# t_71ed3b1c's `scripts/tests/test_load_burners.py` is the reviewed shape for
# this: own the group, and then PROVE that a signalled harness leaves no
# survivors, with a non-vacuity check first (pgrep must find the processes
# before it is allowed to conclude they are gone). The equivalent here is the
# three arms below: the group is really ours, killing it reaches the
# descendants, and the FIXTURE — the path that runs when a case never reaches
# its own `finally:` — really does it.

def _guard_tag():
    return f"{GUARD_TAG_PREFIX}-{uuid.uuid4().hex}"


def _guard_kid_pid(trail):
    """The pid the guard child reported for its own descendant, or None."""
    for line in trail.text().splitlines():
        if line.startswith(GUARD_KID):
            digits = re.sub(r"\D", "", line)   # the pid is its only number
            return int(digits) if digits else None
    return None


@contextlib.contextmanager
def _guard_child(sandbox, tag):
    """Spawn the GUARD child, and make a RED assertion leave nothing behind.

    Same belt-and-braces as load_burners' `_running`: the group is killed on the
    way out and `pgrep` sweeps whatever the group kill missed, so a failing
    ownership arm cannot become the orphan it is testing for.
    """
    sandbox["env"][ENV_GUARD_TAG] = tag
    proc = _popen(sandbox, program="GUARD")
    trail = _ChildTrail(proc)
    try:
        yield proc, trail
    finally:
        _reap(proc)
        trail.finish()
        for pid in _survivors(tag):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def test_every_child_this_file_spawns_leads_its_own_process_group(sandbox):
    """Non-vacuity first: the descendant must exist before ownership means anything.

    `_reap` claims to take the group. That is only true of a child that LEADS a
    group — `killpg(proc.pid)` on a child that shares pytest's group would miss
    the descendants entirely (and killing pytest's own group would end the run).
    """
    tag = _guard_tag()
    with _guard_child(sandbox, tag) as (proc, trail):
        assert _wait_for(lambda: trail.saw(GUARD_KID), CHILD_READY_BUDGET), (
            "the guard child never reported a descendant, so this arm would "
            f"prove nothing.\nchild output:\n{trail.text()}")
        kid = _guard_kid_pid(trail)
        assert kid and _alive(kid), (
            f"the reported descendant {kid} is not alive; the arm vacuously "
            f"passes.\nchild output:\n{trail.text()}")
        assert os.getpgid(proc.pid) == proc.pid, (
            f"child {proc.pid} leads pgid {os.getpgid(proc.pid)} — without "
            "`start_new_session=True` the group is pytest's own and one killpg "
            "either takes the test run down or reaches nothing")
        assert os.getpgid(kid) == proc.pid, (
            f"descendant {kid} leads pgid {os.getpgid(kid)} instead of sharing "
            f"child {proc.pid}'s group, so killing the group misses it — the "
            "orphaned-serve shape t_7c2eb604 measured alive for hours")
        assert tag in " ".join(
            subprocess.run(["ps", "-o", "command=", "-p", str(kid)],
                           capture_output=True, text=True).stdout.split()), (
            f"descendant {kid} does not carry the run tag, so `pgrep -f` below "
            "would find nothing and every survivor check would pass vacuously")


def test_killing_the_group_takes_the_descendant_with_it(sandbox):
    """The arm the card asked for, stated as the property it protects.

    `gateway_bridge.start_serve()` spawns `hermes serve` as a child of the API
    process. A teardown that kills only the API leaves that grandchild running
    with the token, on the port, after the test that believed it stopped it.
    """
    tag = _guard_tag()
    with _guard_child(sandbox, tag) as (proc, trail):
        assert _wait_for(lambda: trail.saw(GUARD_KID), CHILD_READY_BUDGET), (
            f"no descendant to test with.\nchild output:\n{trail.text()}")
        kid = _guard_kid_pid(trail)
        assert kid and _alive(kid), f"descendant {kid} was never alive"
        _reap(proc)
        assert _wait_for(lambda: not _alive(kid), CHILD_GONE_BUDGET), (
            f"descendant {kid} outlived `killpg` of the child's group — the "
            f"exact orphan shape t_7c2eb604 exists to prevent")
        assert _wait_for(lambda: not _survivors(tag), CHILD_GONE_BUDGET), (
            f"tagged processes survived the group kill: {_survivors(tag)}")


def test_the_sandbox_fixture_reaps_a_child_a_case_never_reaped(tmp_path):
    """The `finally:` in a case is not the last line of defence. The fixture is.

    load_burners' guard signals the harness and counts survivors; the equivalent
    failure here is the one this file is about — a case that dies mid-flight and
    never reaches its own `finally:`. So this arm drives the fixture body the way
    pytest finalizes a yield fixture (`next()`, expecting `StopIteration`) and
    asserts the child AND its descendant are gone afterwards. Driving the real
    generator is the point: a copy of the teardown here could pass while the
    fixture's loop had been deleted.
    """
    tag = _guard_tag()
    box = _sandbox_box(tmp_path)
    sandbox = next(box)
    # `_guard_child` does this for the arms that use the fixture; this arm drives
    # the fixture body directly, so the tag goes in by hand.
    sandbox["env"][ENV_GUARD_TAG] = tag
    proc = _popen(sandbox, program="GUARD")
    trail = _ChildTrail(proc)
    kid = None
    try:
        assert _wait_for(lambda: trail.saw(GUARD_KID), CHILD_READY_BUDGET), (
            f"no descendant to test with.\nchild output:\n{trail.text()}")
        kid = _guard_kid_pid(trail)
        assert kid and _alive(kid), f"descendant {kid} was never alive"
        with pytest.raises(RuntimeError):
            # the case dies here — no `finally:` of its own ever runs, and
            # nothing between the raise and the teardown reaps the child.
            raise RuntimeError("simulated mid-case failure")
        assert proc.poll() is None, (
            "the child died on its own before the teardown was reached, so this "
            "arm would pass without the teardown doing anything")
    finally:
        with pytest.raises(StopIteration):
            next(box)                    # what pytest does to finalize the fixture
        # Sample the facts BEFORE this arm's own belt-and-braces sweep. A
        # mutation probe (t_eed6b74d) proved why: when `_reap` was weakened to
        # kill only the direct child, the sweep below had already cleared the
        # tagged descendant by the time the asserts ran, and the arm went green
        # on its own cleanup instead of the fixture's. The cleanup stays — a red
        # must not orphan — but it may not be the thing the assertion reads.
        survived = _survivors(tag)
        kid_alive = kid is not None and _alive(kid)
        trail.finish()
        for pid in survived:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    assert proc.poll() is not None, (
        f"the fixture teardown reaped neither pid {proc.pid} nor its group")
    assert not _alive(proc.pid), f"child {proc.pid} outlived the teardown"
    assert not kid_alive, (
        f"descendant {kid} outlived the teardown — the fixture's killpg is not "
        "reaching the group")
    assert not survived, (
        f"tagged survivors of the teardown: {survived} — the group kill missed them")


def test_the_two_budgets_agree_about_who_waits_for_whom():
    """The child's own waiting backstop must sit well under the parent's stall.

    The shape this file replaced had a child that could burn up to 30 s waiting
    for permission to be stopped inside a case that allowed the whole child 25 s
    — two numbers in one file, disagreeing at any load, on any machine. The
    child's number is now THIS constant, injected into the harness string, so it
    cannot be edited into disagreement again; these are the arithmetic facts the
    rest of the file depends on.
    """
    assert CHILD_SIGNAL_BACKSTOP * 3 <= CHILD_STAGE_STALL, (
        f"the child waits up to {CHILD_SIGNAL_BACKSTOP}s to be signalled while a "
        f"live child is called stuck after {CHILD_STAGE_STALL}s of silence — the "
        "parent must never run out of patience before the child has finished "
        "waiting")
    assert CHILD_STAGE_STALL < CHILD_LIFE_CAP, (
        "a stall budget above the life cap is dead code: the cap would always "
        "answer first, which is the whole-life stopwatch again")
    assert CHILD_SIGNAL_BACKSTOP < CHILD_READY_BUDGET, (
        "the child gives up on readiness before the parent finishes waiting for "
        "it to become ready, so the parent would read a self-inflicted startup "
        "stop as a hang")
    rendered = _program_text("HARNESS")
    assert f"_READY_BACKSTOP = {CHILD_SIGNAL_BACKSTOP}" in rendered, (
        "the harness no longer takes the parent's backstop constant — a literal "
        "in the child program is how the two sides drifted apart last time")


def test_the_announced_stages_are_named_once():
    """The child program is a string; a drifted marker is a silent pass.

    Every `@@…@@` a case waits for must be the same literal the child prints.
    Without this pin, renaming one side turns an assertion into `assert ""` and
    the case goes green having measured nothing.
    """
    harness, premise, guard = (_program_text("HARNESS"),
                               _program_text("PREMISE"),
                               _program_text("GUARD"))
    for marker in (INTERRUPTED_FLAG_POLL, READY_OBSERVED,
                   READY_BACKSTOP_EXPIRED, SERVE_FOREVER_RETURNED,
                   SERVE_MAIN_RETURNED):
        assert marker in harness, f"{marker} is waited on but no child prints it"
    for marker in (INTERRUPTED_EVENT_LOCK, MAIN_RESUMED):
        assert marker in premise, f"{marker} is waited on but no child prints it"
    for marker in (GUARD_ALIVE, GUARD_KID):
        assert marker in guard, f"{marker} is waited on but no child prints it"
    assert READY_LINE not in harness and STOPPED_LINE not in harness, (
        "the harness printing the product's own lines would let a case pass on "
        "the harness's word instead of serve_main's")


# --------------------------------------------------------------------------- #
# the shape, pinned at source level (the behavioural cases cannot enforce it)
# --------------------------------------------------------------------------- #

class TestStopPathHoldsNothing:
    """Source pins: the handler holds nothing, and the lines live on the door.

    A signal handler is a callback the interpreter runs at an arbitrary
    bytecode boundary on whatever thread it interrupts, so "it usually works"
    is not a property a stop path can be trusted to keep. These pins are the
    same doctrine t_56552ecc applied in the sibling plugin's stop path and
    t_6eff6258 used for the ordering in this very file.
    """

    @staticmethod
    def _serve_main_body():
        """AST of `serve_main`, linenos re-based onto the real file."""
        source, start = inspect.getsourcelines(api_server.serve_main)
        body = ast.parse("".join(source))
        ast.increment_lineno(body, start - 1)
        return body

    @staticmethod
    def _handler(body):
        """The `_on_term` FunctionDef nested in `serve_main` — the one installed.

        Read from `serve_main`'s own body rather than from a module-level name
        so the pin cannot silently drift onto a copy that nothing installs.
        """
        funcs = [n for n in ast.walk(body)
                 if isinstance(n, ast.FunctionDef) and n.name == "_on_term"]
        assert len(funcs) == 1, (
            f"expected exactly one `_on_term` in serve_main, found {len(funcs)}"
            ": the stop handler this file pins is no longer unambiguous")
        return funcs[0]

    @staticmethod
    def _statements(fn):
        stmts = list(fn.body)
        if (stmts and isinstance(stmts[0], ast.Expr)
                and isinstance(stmts[0].value, ast.Constant)
                and isinstance(stmts[0].value.value, str)):
            stmts = stmts[1:]                    # the docstring is not a statement
        return stmts

    @staticmethod
    def _callee(calls):
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

    def test_the_handler_is_the_one_serve_main_installs(self):
        """Both signals, and the handler is the one pinned below."""
        body = self._serve_main_body()
        installed = [
            n for n in ast.walk(body)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "signal" and len(n.args) == 2
            and isinstance(n.args[1], ast.Name) and n.args[1].id == "_on_term"
        ]
        named = [
            n.args[0].attr for n in installed
            if isinstance(n.args[0], ast.Attribute)
            and n.args[0].attr in ("SIGTERM", "SIGINT")
        ]
        assert sorted(named) == ["SIGINT", "SIGTERM"], (
            f"serve_main installs `_on_term` for {sorted(named) or 'nothing'}; "
            "launchd stops the unit with SIGTERM and an operator's Ctrl-C is "
            "SIGINT, so a stop path that covers one of them is the orphaned-"
            "sidecar bug this file exists for")

    def test_the_handler_makes_no_call_except_the_stop_flag(self):
        """One call, and it is `stopping.set()`. Nothing else may block.

        Measured three ways in this family, all of them a lost stop: a log/stdout
        write (the writer's lock), `threading.Thread(...).start()` (threading's
        bookkeeping, plus waiting for the thread it just made), and
        `threading.Event.set()` (the event's own lock, which the serving loop
        holds every pass it polls the flag — the door this file reproduces).
        """
        fn = self._handler(self._serve_main_body())
        stmts = self._statements(fn)

        def _is_flag_set(node):
            return (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "set"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "stopping")

        calls = [n.value for n in stmts if isinstance(n, ast.Expr)]
        flag = [c for c in calls if _is_flag_set(c)]
        assert len(calls) == len(flag) == 1, (
            f"_on_term makes {len(calls)} call(s) ({self._callee(calls)}); the "
            "body is allowed exactly one — `stopping.set()`. A handler runs on "
            "the thread it interrupts, so any call can wait on a lock that "
            "thread already holds, and a handler that never returns loses the "
            "stop outright: no flag, no return from serve_forever, no "
            "sidecar teardown, and the SIGKILL that follows skips the "
            "teardown as well.")
        assert all(isinstance(n, (ast.Global, ast.Assign, ast.Expr))
                   for n in stmts), (
            f"_on_term contains {[type(n).__name__ for n in stmts]}; only the "
            "record of which signal arrived and the flag write are allowed, and "
            "both are plain writes")
        stray = [t.id for n in stmts if isinstance(n, ast.Assign)
                 for t in n.targets
                 if isinstance(t, ast.Name) and t.id != "_last_signal"]
        assert not stray, (
            f"_on_term writes {stray}; the only module state a handler may touch "
            "is `_last_signal` (a plain write) and the flag — anything else is "
            "state some other thread is holding a lock over")

    def test_the_stop_flag_is_not_a_threading_event(self):
        """`Event.set()` / `is_set()` both take the event's lock; the flag may not.

        Pinned on both ends: what `serve_main` builds, and that the flag type's
        own `set`/`is_set` are plain attribute reads and writes with no call in
        them (a `__slots__` object whose methods grow a lock is the same bug).
        """
        body = self._serve_main_body()
        made = [
            n for n in ast.walk(body)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "stopping"
                    for t in n.targets)
        ]
        assert len(made) == 1, (
            f"serve_main assigns `stopping` {len(made)} times; the flag the "
            "handler and the serving loop share has to be one unambiguous "
            "object")
        value = made[0].value
        assert (isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "_StopFlag"), (
            f"`stopping` is built from {self._callee([value])[0]}, not "
            "`_StopFlag`: a threading.Event's is_set() holds the event's lock "
            "while the serving loop polls it, and that is the deadlock this "
            "file reproduces (see test_the_event_shape_is_the_hazard)")

        flag = api_server._StopFlag
        assert not isinstance(flag(), threading.Event), (
            "`_StopFlag` is an Event subclass again — same lock, same deadlock")
        assert getattr(flag, "__slots__", None) == ("_seen",), (
            f"_StopFlag.__slots__ is {getattr(flag, '__slots__', None)!r}: the "
            "flag is meant to be one attribute, not an object with a lock in it")
        for name in ("set", "is_set"):
            fn = getattr(flag, name)
            body = _parse_method(fn)
            calls = [n for n in ast.walk(body) if isinstance(n, ast.Call)]
            assert not calls, (
                f"_StopFlag.{name}() now calls {self._callee(calls)}: the flag "
                "is lock-free because a handler may set it while the interrupted "
                "thread is inside the other half of it")

    def test_the_operator_lines_live_in_the_finally_that_always_runs(self):
        """Both lines belong to the door, not to the handler.

        `finally:` is what a cooperative stop always reaches; the handler is
        what a lost stop never leaves. So the signal line moved to the former.
        """
        body = self._serve_main_body()
        trys = [n for n in ast.walk(body) if isinstance(n, ast.Try) and n.finalbody]
        assert trys, (
            "serve_main has no try/finally any more: the sidecar teardown that "
            "lives in it would be skippable again")
        for node in trys:
            prints = [n for n in ast.walk(node)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                      and n.func.id == "print"]
            joined = " ".join(filter(None,
                                     (self._first_argument_text(p) for p in prints)))
            assert "shutting down" in joined, (
                "the operator's `hscc-api: signal N, shutting down` line is no "
                "longer printed from serve_main's finally: — it has moved back "
                "into the handler or away entirely, and a stop that never "
                "leaves the handler then prints nothing at all")
            assert STOPPED_LINE in joined, (
                f"{STOPPED_LINE!r} is no longer printed from the finally:, so a "
                "run that ends early looks identical to one that stopped")
            assert any(isinstance(n, ast.Call)
                       and isinstance(n.func, ast.Attribute)
                       and n.func.attr == "shutdown"
                       for n in ast.walk(node)), (
                "the sidecar teardown is no longer in the finally: — that is "
                "t_56552ecc's orphaned-sidecar bug, in this plugin")

    @staticmethod
    def _first_argument_text(call):
        """The literal text of a print's first argument, f-string included."""
        if not call.args:
            return None
        arg = call.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if isinstance(arg, ast.JoinedStr):
            return "".join(v.value if isinstance(v, ast.Constant) else ""
                           for v in arg.values)
        return None

    def test_shutdown_is_not_in_the_stop_path(self):
        """`server.shutdown()` waits for the loop that the handler interrupts.

        The handler used to hand it to a helper thread; the thread could block
        the same way, and the call itself has no business in a stop path. The
        flag is what ends the loops, from inside the loop.
        """
        fn = self._handler(self._serve_main_body())
        banned = {"shutdown", "start", "print", "log", "warning", "info",
                  "flush", "open", "write"}
        found = set(self._callee(
            [n for n in ast.walk(fn) if isinstance(n, ast.Call)])) & banned
        assert not found, (
            f"_on_term calls {sorted(found)}; a handler may make one call, "
            "`stopping.set()`, and nothing that waits for anything")

    def test_the_serving_loop_still_only_asks_is_set(self):
        """t_6eff6258's contract, re-checked against a lock-free flag.

        `_MultiServer.serve_forever` takes a caller's `stop_event` and may only
        ever ask it `is_set()` — that is the whole surface a `_StopFlag` has to
        satisfy, and the moment the loop asks for anything else (a `wait()`, a
        `clear()`) the duck-type silently stops holding.
        """
        body = _parse_method(api_server._MultiServer.serve_forever)
        asked = [
            n.func.attr for n in ast.walk(body)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "stop_event"
        ]
        assert asked == ["is_set"], (
            f"serve_forever asks stop_event {asked}; the contract this file's "
            "flag satisfies is exactly one is_set() per pass")

    def test_the_pinned_function_is_the_one_the_entrypoint_runs(self):
        """Otherwise every pin above could bless a copy nothing executes."""
        body = ast.parse(inspect.getsource(api_server.main))
        assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "serve_main" for n in ast.walk(body)), (
            "`python -m api_server serve` no longer reaches serve_main, so the "
            "pins above describe dead code")
