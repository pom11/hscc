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

Hermetic: a throwaway HOME (fixture token, never the operator's), an ephemeral
loopback port, and a FAKE `gateway_bridge` so the sidecar markers are this
file's own files rather than a supervised `hermes serve`.
"""
import ast
import inspect
import os
import re
import signal
import socket
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

import api_server

REPO = Path(__file__).resolve().parents[2]
API_DIR = REPO / "hscc-api"

# How long a cooperating serving process gets before this file calls it hung.
# Generous on purpose: the failure mode is a permanent deadlock, so a bound
# that is comfortably above the real teardown cost is what makes the verdict
# "hung" instead of "slow runner".
STOP_BOUND_S = 25.0

#: The line `serve_main` prints as the LAST thing before serve_forever(): the
#: handlers are installed and every listener is bound. Duplicated here (not
#: imported from test_api_server_entrypoint.py) because that file owns the
#: readiness contract and this one only waits on it.
READY_LINE = "hscc-api: ready"

#: What the operator is owed when a stop arrives: which signal, and that the
#: teardown finished. Both must come from the path that always runs.
SIGNAL_LINE = re.compile(r"^hscc-api: signal (\d+), shutting down$", re.MULTILINE)
STOPPED_LINE = "hscc-api: stopped"

# Marker file values written by the fake gateway_bridge in the child.
SIDECAR_STARTED = "started"
SIDECAR_SHUTDOWN = "shutdown"

# The token is DATA — a fixture value, never a real credential.
TOKEN = "stop-signal-dummy-token"

ENV_API_DIR = "HSCC_API_TEST_API_DIR"
ENV_MARKERS = "HSCC_API_TEST_MARKERS"


def _free_port():
    """An ephemeral loopback port, released before the child binds it."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _parse_method(func):
    """AST of a method, which `inspect.getsourcelines` hands back INDENTED.

    Parsing the excerpt as-is is an IndentationError, and the pin would then be
    red for a reason that has nothing to do with the shape it is reading.
    """
    source, _start = inspect.getsourcelines(func)
    return ast.parse(textwrap.dedent("".join(source)))


@pytest.fixture()
def sandbox(tmp_path):
    """A throwaway HOME plus the two marker files the fake sidecar writes."""
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
    return {"home": home, "hscc": hscc, "env": env, "markers": markers}


# --------------------------------------------------------------------------- #
# the child program
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

    def _signal_later():
        for _ in range(600):
            if _ready.wait(0.05):
                break
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


def _popen(sandbox, *args, program="HARNESS"):
    """Run the child program in a subprocess with the sandbox env."""
    script = Path(sandbox["markers"]).parent / (program.lower() + ".py")
    body = globals()[program]
    if program == "HARNESS":
        body = (body.replace("@@API_DIR_ENV@@", ENV_API_DIR)
                    .replace("@@MARKERS_ENV@@", ENV_MARKERS)
                    .replace("@@SIDECAR_STARTED@@", SIDECAR_STARTED)
                    .replace("@@SIDECAR_SHUTDOWN@@", SIDECAR_SHUTDOWN))
    script.write_text(body)
    return subprocess.Popen(
        [sys.executable, "-u", str(script), *args],
        cwd=str(API_DIR), env=sandbox["env"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _markers(sandbox):
    return [ln.strip() for ln in sandbox["markers"].read_text().splitlines()
            if ln.strip()]


def _communicate(proc, bound=STOP_BOUND_S):
    """Child output, or a pytest.fail that names the deadlock this file is about."""
    try:
        return proc.communicate(timeout=bound)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        out = proc.communicate()[0]
        pytest.fail(
            f"the stop was never answered: no exit within {bound}s.\n"
            f"A signal handler that waits on a lock its own thread holds never "
            f"returns, so the flag is never set, serve_forever never ends and "
            f"serve_main's finally: (the sidecar teardown) never runs.\n"
            f"child output:\n{out}")


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

    What stays production-shaped afterwards: `serve_main` really binds, the fake
    sidecar really starts, the real `serve_forever` really runs and returns, the
    `finally:` really tears the sidecar down, and the process exits 0.
    """
    port = _free_port()
    proc = _popen(sandbox, "stop-inside-flag-poll", str(port), sig.name)
    out = _communicate(proc)
    try:
        assert "@@INTERRUPTED-INSIDE-FLAG-POLL@@" in out, (
            "the stop never landed inside the serving loop's flag poll, so this "
            f"case says nothing about that window.\nchild output:\n{out}")
        assert SIDECAR_STARTED in _markers(sandbox), (
            "the sidecar never started, so the teardown below would prove "
            f"nothing: {out}")
        assert "@@SERVE_FOREVER_RETURNED@@" in out, (
            "the serving loops never ended: the stop was lost, which is what a "
            f"handler blocked on its own thread's lock does.\n{out}")
        assert f"@@SERVE_MAIN_RETURNED rc=0@@" in out, (
            f"serve_main left by a door other than its finally:.\n{out}")
        assert proc.returncode == 0, (
            f"{sig.name} ended the process with rc={proc.returncode} instead of "
            f"a cooperative stop; the sidecar would be orphaned.\n{out}")
        assert STOPPED_LINE in out, (
            f"no {STOPPED_LINE!r}: the teardown path was not reached.\n{out}")
        assert SIDECAR_SHUTDOWN in _markers(sandbox), (
            "the gateway sidecar outlived the API: the teardown lives in the "
            f"finally: a lost stop never reaches.\n{out}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_the_event_shape_is_the_hazard(sandbox, sig):
    """The premise, measured on this interpreter with no hscc code involved.

    If a future CPython makes `Event`'s lock reentrant, this case goes red
    first and says the deadlock the fix is justified by no longer exists —
    which is worth knowing before the fix is ever questioned on cost.
    """
    proc = _popen(sandbox, sig.name, program="PREMISE")
    try:
        out = proc.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return                      # hung, exactly as the hazard says
    pytest.fail(
        f"{sig.name} arriving while the thread held Event._cond came back: "
        f"the Event-shape hazard this card is built on no longer reproduces "
        f"on {sys.version.split()[0]}.\nchild output:\n{out}")


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_cooperative_stop_names_its_signal_and_tears_the_sidecar_down(
        sandbox, sig):
    """The operator-visible lines must come from the path that always runs.

    A plain `hscc api stop`-shaped stop: the child serves, says it is ready, a
    second thread signals it. The lines the operator reads in ~/.hscc/logs/api.log
    — which signal arrived, and that the server stopped — are asserted here, and
    `TestStopPathHoldsNothing` pins where they are allowed to live. The marker
    file is the witness that the teardown was real, not merely printed.
    """
    port = _free_port()
    proc = _popen(sandbox, "stop-after-ready", str(port), sig.name)
    out = _communicate(proc)
    try:
        assert READY_LINE in out, f"the child never reached readiness.\n{out}"
        assert f"@@SERVE_MAIN_RETURNED rc=0@@" in out, (
            f"serve_main left by a door other than its finally:.\n{out}")
        assert proc.returncode == 0, (
            f"{sig.name} ended the process with rc={proc.returncode}; the "
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
        assert _markers(sandbox) == [SIDECAR_STARTED, SIDECAR_SHUTDOWN], (
            "the sidecar was not started-then-torn-down by this run: "
            f"{_markers(sandbox)}\n{out}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


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
