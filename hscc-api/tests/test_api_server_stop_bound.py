"""The cooperative stop must not be hostage to a listener loop that cannot return.

``_MultiServer.serve_forever(poll_interval, stop_event)`` is the ONLY thing that
stops ``serve_main`` (t_49f4c69b: the signal handler makes no call but the flag's
``set()``), and its flag branch used to end with a bare ``self.shutdown()`` on the
serving thread. ``BaseServer.shutdown()`` is ``__shutdown_request = True`` followed
by ``__is_shut_down.wait()`` — it waits for that listener's own loop to return, and
the loop calls ``service_actions()`` on every pass (stdlib ``socketserver.py``). So
one listener that cannot return means ``serve_forever`` never returns, ``serve_main``'s
``finally:`` never runs, and the supervised gateway sidecar keeps running with this
process's token while the supervisor escalates to SIGKILL — which skips the teardown
too. Measured before the fix (probe on the leg interpreter, one ``_ApiServer``,
``poll_interval=0.1``): a healthy listener's ``shutdown()`` came back in 0.09 s; a
listener whose ``service_actions`` blocks was STILL BLOCKED at 5.15 s. Nothing in this
plugin overrides ``service_actions`` today (``grep -rn service_actions hscc-api`` is
empty), so this is a robustness gap on a path that has become load-bearing.

The shape this file pins (the card's decision, recorded here so a reviewer can
reverse it knowingly):

  * the stop is BOUNDED, and the bound is loud. Giving up beats waiting forever:
    the teardown is the point of a stop, and a bounded stop reaches it.
  * the bound lives in the flag branch of ``serve_forever``, NOT in
    ``_MultiServer.shutdown()``. ``BaseServer.shutdown()``'s contract is "return
    only once the listener loop has returned", and ``server_close()`` relies on it —
    a ``shutdown()`` that gave up early would let a caller yank the listening socket
    out from under a loop that is still accepting.

Hermetic: loopback / ``::1`` only, a throwaway HOME, and a fake ``gateway_bridge``
that writes a marker instead of supervising a real ``hermes serve``.
"""
import ast
import inspect
import os
import queue
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

import api_server

REPO = Path(__file__).resolve().parents[2]
API_DIR = REPO / "hscc-api"

# What `hscc api stop` waits before it escalates to SIGKILL
# (hscc_daemon/api_cli.py::_wait_for_pid_gone, timeout_s=10.0). The stop bound has
# to sit inside that window, or the process is SIGKILLed before its teardown.
SUPERVISOR_SIGKILL_SECONDS = 10.0

READY_LINE = "hscc-api: ready"
STOPPED_LINE = "hscc-api: stopped"
WARN_MARK = "did not stop"


def _free_port():
    """An ephemeral loopback port, released before the child binds it."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _shut_down_within():
    """The shipped bound, read from production (never a copy that can drift)."""
    value = getattr(api_server, "_SHUTDOWN_WAIT_SECONDS", None)
    assert value is not None, (
        "_MultiServer.serve_forever's stop path has no bound at all: a listener "
        "loop that cannot return keeps serve_main's finally: — and therefore the "
        "gateway sidecar's teardown — from ever running")
    return float(value)


def _wedged(server, entered, gate):
    """Make one listener's loop unable to return, and say when it happened.

    ``service_actions`` is the seam ``BaseServer.serve_forever`` calls on every pass
    *before* it re-reads ``__shutdown_request``, so blocking here is exactly the
    wedged-loop state — no sleep, no race, no timing luck.

    Two events, and both matter:

      * ``entered`` — set from inside the wedged call. Without it the caller cannot
        know the loop reached the seam: a stop that arrives while the listener
        thread has not entered its ``while not __shutdown_request`` check yet makes
        the loop exit before ``service_actions`` is ever called, and the case then
        "passes" on a build that still hangs. This was measured: aiming at the
        freshly-started loop, ``shutdown()`` came back in 0.15 s for exactly that
        reason.
      * ``gate`` — belongs to the CALLER and is fresh per test. A gate shared across
        arms leaks a release from the healthy arm into the wedged one, and the
        wedged case then passes for free.
    """
    def _wedged_action(_entered=entered, _gate=gate):
        # No `self`: this is an INSTANCE attribute, and Python does not bind a
        # function stored on an instance — `self.service_actions()` calls it with
        # no arguments at all.
        _entered.set()
        _gate.wait()
    server.service_actions = _wedged_action


def _await_wedge(entered, server):
    """Block until the listener is demonstrably stuck inside ``service_actions``."""
    assert entered.wait(5.0), (
        f"the listener never reached service_actions, so this case proves nothing "
        f"about a wedged loop (hosts={server.hosts!r})")


def _serve_on_a_thread(server, stopping, poll_interval=0.1):
    """Call ``serve_forever`` off the main thread and collect its outcome.

    The failure mode here is a HANG, and a hang on the main thread takes the whole
    leg down with it instead of reporting itself. So: a joined daemon thread, plus
    the exception box — ``is_alive()`` alone reads a crash as the clean stop.
    """
    box = {}

    def _run():
        try:
            box["result"] = server.serve_forever(
                poll_interval=poll_interval, stop_event=stopping)
        except BaseException as exc:            # noqa: BLE001 - report, never swallow
            box["crashed"] = exc

    thread = threading.Thread(target=_run, daemon=True, name="stop-bound-serve")
    thread.start()
    return thread, box


# ---------------------------------------------------------------------------
# in-process: the flag branch itself
# ---------------------------------------------------------------------------

def test_a_wedged_listener_cannot_hold_the_stop_hostage(hscc_dir):
    """The flag wakes the join loop; that loop's wait must then be bounded.

    Deliberately names no production constant: at base this case fails the way the
    defect behaves — the stop is requested, the join loop is woken, and 10 s later
    ``serve_forever`` has still not returned (the probe measured ``shutdown()``
    blocked at 5.15 s with ``service_actions`` wedged and never coming back). A
    method that cannot return cannot reach ``serve_main``'s ``finally:``.
    """
    server = api_server.create_server(hscc_dir=hscc_dir, addr=("127.0.0.1", 0))
    entered, gate = threading.Event(), threading.Event()
    _wedged(server.servers[0], entered, gate)
    stopping = threading.Event()
    thread, box = _serve_on_a_thread(server, stopping)
    try:
        _await_wedge(entered, server)
        started = time.monotonic()
        stopping.set()
        thread.join(SUPERVISOR_SIGKILL_SECONDS)
        elapsed = time.monotonic() - started
        assert not thread.is_alive(), (
            f"serve_forever is still blocked {elapsed:.1f} s after the stop was "
            f"requested: the flag woke the join loop, the loop then waited on a "
            f"listener that cannot return, and serve_main's finally: — the "
            f"sidecar teardown — would never run")
        assert "crashed" not in box, f"serve_forever raised: {box['crashed']!r}"
        assert box["result"] is False, (
            f"a stop that abandoned a wedged listener reported success: "
            f"{box['result']!r}")
        assert server.wedged_listeners == ["127.0.0.1"], (
            f"the wedged listener was not named to the caller: "
            f"{server.wedged_listeners!r}")
    finally:
        gate.set()                              # let the wedged loop unwind at all
        server.server_close()


def test_a_healthy_stop_is_still_clean_and_says_so(hscc_dir):
    """Control: the bound must not make a normal stop look like a failure.

    ``serve_main`` exits non-zero exactly when a listener was abandoned, so this
    arm is what keeps "loud" from becoming "always". It also pins that the bound is
    a ceiling and not a wait: a healthy stop comes back in poll-interval time, not
    in bound time.
    """
    server = api_server.create_server(hscc_dir=hscc_dir, addr=("127.0.0.1", 0))
    stopping = threading.Event()
    thread, box = _serve_on_a_thread(server, stopping)
    started = time.monotonic()
    try:
        stopping.set()
        thread.join(10.0)
        elapsed = time.monotonic() - started
        assert not thread.is_alive(), "a healthy listener did not stop within 10 s"
        assert "crashed" not in box, f"serve_forever raised: {box['crashed']!r}"
        assert box["result"] is True, (
            f"a clean stop did not report itself clean: {box['result']!r}")
        assert server.wedged_listeners == [], (
            f"a healthy stop blamed somebody: {server.wedged_listeners!r}")
        assert elapsed < 2.0, (
            f"a healthy stop waited {elapsed:.1f} s instead of returning as soon "
            f"as the loops came back — the bound is a ceiling, not a wait")
    finally:
        server.server_close()


def test_the_bound_is_named_and_is_the_wait_that_is_kept(hscc_dir, monkeypatch):
    """The bound is a named constant, and the stop keeps it.

    Two things a mutation would otherwise slip through:

      * the constant exists and sits INSIDE the ``10 s`` the supervisor gives the
        pid before SIGKILLing it (``hscc_daemon/api_cli.py::_wait_for_pid_gone``) —
        a bound bigger than that window buys nothing, because the SIGKILL skips the
        teardown exactly like the hang does;
      * lowering it actually lowers the real wait. Patching the constant and still
        coming back at the shipped 5 s would mean the flag branch does not read it.
    """
    bound = _shut_down_within()
    assert 0 < bound < SUPERVISOR_SIGKILL_SECONDS, (
        f"_SHUTDOWN_WAIT_SECONDS={bound} is not inside the "
        f"{SUPERVISOR_SIGKILL_SECONDS} s the supervisor waits before SIGKILL")

    monkeypatch.setattr(api_server, "_SHUTDOWN_WAIT_SECONDS", 1.0)
    server = api_server.create_server(hscc_dir=hscc_dir, addr=("127.0.0.1", 0))
    entered, gate = threading.Event(), threading.Event()
    _wedged(server.servers[0], entered, gate)
    stopping = threading.Event()
    thread, box = _serve_on_a_thread(server, stopping)
    try:
        _await_wedge(entered, server)
        started = time.monotonic()
        stopping.set()
        thread.join(SUPERVISOR_SIGKILL_SECONDS)
        elapsed = time.monotonic() - started
        assert not thread.is_alive(), (
            f"the stop never came back even with the bound patched down to 1 s "
            f"({elapsed:.1f} s so far): the flag branch is not waiting on the "
            f"bound this module advertises")
        # 0.5 s to notice the flag + the 0.5 s join granularity, then the bound.
        assert elapsed < 1.0 + 3.0, (
            f"the stop waited {elapsed:.1f} s against a bound of 1.0 s, so the "
            f"bound named here is not the wait that was kept")
    finally:
        gate.set()
        server.server_close()


def test_the_flag_branch_never_calls_shutdown_on_the_serving_thread():
    """Source pin: the flag branch may not wait on a listener directly.

    Behavioural cases cover the arms that can be constructed; this one stops the
    one-line regression ("just call ``self.shutdown()`` here") that no test on an
    idle runner would notice until a listener wedged.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(
        api_server._MultiServer.serve_forever)))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "shutdown"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"):
            pytest.fail(
                f"serve_forever waits on a listener loop directly at line "
                f"{node.lineno} (self.shutdown()): the stop bound must live in a "
                f"wait the flag already wakes up, not in an unbounded join on the "
                f"serving thread")


def test_the_bound_does_not_move_into_shutdown():
    """Source pin: ``_MultiServer.shutdown()`` keeps stdlib's contract.

    ``BaseServer.shutdown()`` returning means "that loop has returned", and
    ``server_close()`` is only safe because of it. Moving the bound here would let
    a caller close the listening socket under a loop that is still accepting, so
    the give-up has to stay where the flag branch put it.
    """
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(api_server._MultiServer.shutdown)))
    for node in ast.walk(tree):
        name = getattr(getattr(node, "func", None), "attr", None)
        if name in ("join", "wait") or (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Thread"):
            pytest.fail(
                f"_MultiServer.shutdown() gained a bounded wait at line "
                f"{node.lineno}: shutdown() must stay 'the loop has returned' so "
                f"server_close() cannot close a socket under a live loop — the "
                f"give-up belongs in serve_forever's flag branch")


def test_only_the_wedged_listener_is_named(hscc_dir, monkeypatch):
    """Attribution: a stop must say WHICH loop it abandoned, per listener.

    ``shutdown()`` used to loop the servers in order, so one wedged listener also
    prevented the healthy one from ever being asked to stop — the operator got no
    name and the healthy socket stayed open.
    """
    monkeypatch.setattr(api_server, "_SHUTDOWN_WAIT_SECONDS", 1.0)
    server = api_server.create_server(
        hscc_dir=hscc_dir, bind_overrides=["127.0.0.1", "::1"], port_override=0)
    assert server.hosts == ["127.0.0.1", "::1"]
    entered, gate = threading.Event(), threading.Event()
    _wedged(server.servers[1], entered, gate)   # the ::1 listener is the wedged one
    stopping = threading.Event()
    thread, box = _serve_on_a_thread(server, stopping)
    try:
        _await_wedge(entered, server)
        stopping.set()
        thread.join(10.0)
        assert not thread.is_alive(), "serve_forever never returned from the stop"
        assert "crashed" not in box, f"serve_forever raised: {box['crashed']!r}"
        assert server.wedged_listeners == ["::1"], (
            f"expected exactly the wedged listener to be abandoned, got "
            f"{server.wedged_listeners!r}")
    finally:
        gate.set()
        server.server_close()


# ---------------------------------------------------------------------------
# the process the launchd unit actually runs
# ---------------------------------------------------------------------------

_FAKE_BRIDGE = textwrap.dedent('''
    """Stand-in for the live-gateway sidecar: records that teardown ran."""
    import os
    import pathlib

    MARKER = pathlib.Path(os.environ["HSCC_TEST_BRIDGE_MARKER"])

    def start_serve(*args, **kwargs):
        MARKER.write_text("started")
        return True

    def shutdown(*args, **kwargs):
        MARKER.write_text("shutdown")
''')

_CHILD_DRIVER = textwrap.dedent('''
    import os
    import pathlib
    import sys
    import threading

    sys.path.insert(0, {fake_dir!r})       # fake gateway_bridge wins
    sys.path.insert(1, {api_dir!r})        # api_server and its routes

    import api_server

    if {wedge}:
        NEVER = threading.Event()          # set by nobody: the loop cannot return
        ENTERED = pathlib.Path(os.environ["HSCC_TEST_WEDGE_MARKER"])

        def _wedged(self):
            ENTERED.write_text("inside-service-actions")
            NEVER.wait()

        api_server._ApiServer.service_actions = _wedged

    sys.exit(api_server.serve_main(port_override={port!r}))
''')


def _attach_reader(proc):
    """Drain the child's stdout on a daemon thread and keep every line.

    Copied in shape from test_api_server_entrypoint.py for the same two reasons:
    a pipe nobody reads fills and blocks the CHILD, and the lines read while
    waiting for readiness are exactly the ones a failure message needs — the pipe
    can only be read once, so a later ``communicate()`` would report "".
    """
    proc.lines = []
    proc.lines_q = queue.Queue()

    def _drain():
        try:
            for line in proc.stdout:
                proc.lines.append(line)
                proc.lines_q.put(line)
        finally:
            proc.lines_q.put(None)

    proc.reader = threading.Thread(target=_drain, daemon=True,
                                   name="stop-bound-stdout-drain")
    proc.reader.start()
    return proc


def _output(proc, join_timeout=10.0):
    """Everything the child wrote, once the drain thread has seen EOF."""
    reader = getattr(proc, "reader", None)
    if reader is not None:
        reader.join(join_timeout)
    return "".join(getattr(proc, "lines", []))


def _wait_for_line(proc, needle, timeout):
    """Block until a drained line contains ``needle`` (EOF or slow => fail)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            line = proc.lines_q.get(timeout=0.5)
        except queue.Empty:
            continue
        if line is None:
            return False                    # the child exited before saying it
        if needle in line:
            return True
    return False


def _kill(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _spawn_child(tmp_path, sandbox, port, wedge):
    """Run ``serve_main`` in a child, with (or without) a wedged listener loop.

    Readiness is the child's own ready line — the last line before
    ``serve_forever()`` — so the stop sent afterwards is one the child promised it
    could answer. When wedging, the stop waits for a second signal: the child's
    own file saying it is INSIDE ``service_actions``. Signalling on readiness alone
    races the listener threads into their loops, and a stop that arrives first
    makes each loop exit before the seam is ever reached — the case would then pass
    on a build that still hangs.
    """
    fake = tmp_path / "fake_bridge"
    fake.mkdir()
    (fake / "gateway_bridge.py").write_text(_FAKE_BRIDGE)
    marker = tmp_path / "bridge.marker"
    wedge_marker = tmp_path / "wedge.marker"
    env = dict(sandbox["env"])
    env["HSCC_TEST_BRIDGE_MARKER"] = str(marker)
    env["HSCC_TEST_WEDGE_MARKER"] = str(wedge_marker)
    driver = tmp_path / "driver.py"
    driver.write_text(_CHILD_DRIVER.format(
        fake_dir=str(fake), api_dir=str(API_DIR), port=port, wedge=wedge))
    proc = subprocess.Popen(
        [sys.executable, "-u", str(driver)], cwd=str(tmp_path), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    _attach_reader(proc)
    proc.test_marker = marker
    if not _wait_for_line(proc, READY_LINE, 25.0):
        out = _output(proc)
        _kill(proc)
        raise AssertionError(f"child never became ready: {out!r}")
    if wedge:
        deadline = time.time() + 5.0
        while not wedge_marker.exists() and time.time() < deadline:
            time.sleep(0.05)
        if not wedge_marker.exists():
            out = _output(proc)
            _kill(proc)
            raise AssertionError(
                f"the child never entered service_actions, so the loop is not "
                f"wedged and the case would prove nothing: {out!r}")
    return proc


@pytest.fixture()
def sandbox(tmp_path):
    """Throwaway HOME with a fixture token — never the operator's ~/.hscc."""
    home = tmp_path / "home"
    hscc = home / ".hscc"
    hscc.mkdir(parents=True)
    (hscc / "api-token").write_text("stop-bound-dummy-token\n")
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "VIRTUAL_ENV")
           and not k.startswith("CONDA_")
           and k not in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE",
                         "HERMES_DELEGATED_CHILD_CONTEXT", "HSCC_SPARKRUN_BIN")}
    env.update({"HOME": str(home), "NO_COLOR": "1", "TERM": "dumb"})
    return {"home": home, "hscc": hscc, "env": env}


def test_serve_main_reaches_the_teardown_when_a_listener_is_wedged(tmp_path,
                                                                    sandbox):
    """The whole point of the bound, end to end in the real unit entrypoint.

    Red at base: the child took the stop (it printed the shutdown line) and then
    hung inside ``shutdown()`` — no teardown, no exit, and the supervisor's next
    move is SIGKILL, which also skips the sidecar teardown (same family as the
    ``os._exit`` orphan t_56552ecc closed). At head the child gives up on the
    wedged loop, tears the sidecar down, names the listener, and exits non-zero.
    """
    port = _free_port()
    proc = _spawn_child(tmp_path, sandbox, port, wedge=True)
    proc.terminate()
    try:
        proc.wait(timeout=SUPERVISOR_SIGKILL_SECONDS + 5)
    except subprocess.TimeoutExpired:
        out = _output(proc)
        _kill(proc)
        pytest.fail(
            "a wedged listener loop held serve_main's finally: — the gateway "
            "sidecar keeps this process's token and the supervisor escalates to "
            f"SIGKILL. Child output:\n{out}")
    out = _output(proc)
    assert proc.returncode == 1, (
        f"a stop that abandoned a listener exited {proc.returncode}; the "
        f"supervisor must be able to tell it from a clean stop.\n{out}")
    assert WARN_MARK in out, (
        f"the give-up was silent — an operator reading ~/.hscc/logs/api.log has "
        f"no idea a listener loop is wedged.\n{out}")
    assert STOPPED_LINE in out, (
        f"the teardown never completed.\n{out}")
    assert proc.test_marker.read_text() == "shutdown", (
        "the gateway sidecar was not torn down — it survives holding this "
        "process's token (the stale-token heal is the next boot's problem)")


def test_serve_main_exits_zero_without_a_warning_when_the_listener_is_healthy(
        tmp_path, sandbox):
    """Control for the arm above: bounded, loud, non-zero applies ONLY to a wedge.

    The healthy child stops with rc 0, tears the sidecar down and prints no
    warning. Without this arm the wedged case would pass just as well on a build
    that warns and exits 1 on every stop.
    """
    port = _free_port()
    proc = _spawn_child(tmp_path, sandbox, port, wedge=False)
    proc.terminate()
    try:
        proc.wait(timeout=SUPERVISOR_SIGKILL_SECONDS + 5)
    except subprocess.TimeoutExpired:
        out = _output(proc)
        _kill(proc)
        pytest.fail(f"a healthy SIGTERM did not stop the server.\n{out}")
    out = _output(proc)
    assert proc.returncode == 0, (
        f"a clean stop exited {proc.returncode}.\n{out}")
    assert WARN_MARK not in out, f"a healthy stop warned anyway.\n{out}"
    assert STOPPED_LINE in out, f"the teardown never completed.\n{out}"
    assert proc.test_marker.read_text() == "shutdown", (
        "the gateway sidecar was not torn down on the healthy path either")
