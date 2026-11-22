"""End-to-end proof for the ONE argv the launchd unit actually execs.

The unit writes `ProgramArguments = [python, -m, api_server, serve, <flags>]`
with WorkingDirectory = the hscc-api plugin dir (api_launchd.build_api_plist).
Nothing else in the suite runs THAT command: the route tests import the module
and drive `create_server(addr=...)` in-process, so they stay green even if the
`-m api_server` entrypoint itself is inert — which, until t_37948685, it was:
`api_server` had no `__main__` block, so the command imported, registered its
routes and exited 0 without binding a socket, and launchd would have respun a
silent no-op forever under KeepAlive.

So this file runs the real thing as a subprocess, from the real WorkingDirectory,
with the real token file, and asserts the bound address is what --bind/--port
said and that GET /v1/ping answers. It is the only test in the repo that
connects the plist text to a listening socket.

Hermetic by construction: HOME points at a throwaway dir, so the server reads a
fixture token (never the operator's ~/.hscc/api-token), the autodown activity
stamp lands in the sandbox, and the live-gateway sidecar cannot reach the real
`hermes serve` binary. Servers bind an ephemeral loopback port like the rest of
the suite, so these cases are parallel-safe.
"""
import ast
import inspect
import json
import os
import plistlib
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import api_server

REPO = Path(__file__).resolve().parents[2]
API_DIR = REPO / "hscc-api"

# The line `serve_main` prints as the LAST thing before serve_forever(): the
# handlers are installed and every listener is bound, so waiting for it is the
# only readiness that means "this process can already be stopped cleanly".
# Kept here as the single constant both the wait and the source pin compare
# against, so a rename in production fails loudly instead of timing out here.
READY_LINE = "hscc-api: ready"

# The other half of the readiness contract: what `serve_main` prints when a stop
# was already requested by the time it reached the point of serving. Coupled to
# production by the same pin that couples READY_LINE, because no case can reach
# that branch on an idle machine (see the pin's docstring for why it is pinned
# rather than tested).
NOT_SERVING_LINE = "hscc-api: stop requested during startup, not serving"

# The token is DATA — a fixture value, never a real credential, and never
# printed in an assertion message.
TOKEN = "e2e-dummy-token-for-launchd-argv"


def _free_port():
    """An ephemeral loopback port, released before the child binds it."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def sandbox(tmp_path):
    """A throwaway HOME holding exactly the files the server reads.

    HOME is redirected, so `DEFAULT_HSCC_DIR` (~/.hscc) resolves inside the tmp
    dir: the child can neither read nor mint the operator's real api-token, and
    the autodown activity stamp lands here too.
    """
    home = tmp_path / "home"
    hscc = home / ".hscc"
    hscc.mkdir(parents=True)
    (hscc / "api-token").write_text(TOKEN + "\n")
    # Mirror what launchd actually hands the process: the unit's
    # EnvironmentVariables are HOME + PATH and NOTHING ELSE (see
    # api_launchd.build_api_plist). So PYTHONPATH/CONDA_* are dropped here — if
    # the entrypoint only worked because the suite's PYTHONPATH happened to be
    # set, that is a launchd-path defect this file should catch.
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "VIRTUAL_ENV")
           and not k.startswith("CONDA_")
           and k not in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE",
                         "HERMES_DELEGATED_CHILD_CONTEXT", "HSCC_SPARKRUN_BIN")}
    env.update({"HOME": str(home), "NO_COLOR": "1", "TERM": "dumb"})
    return {"home": home, "hscc": hscc, "env": env}


def _kill(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _attach_reader(proc):
    """Drain the child's stdout on a daemon thread, keeping every line.

    Readiness has to be the child's own line, and something must therefore read
    the pipe continuously — not the one assertion that waits for it, because a
    pipe nobody reads fills (64 KB here) and blocks the CHILD, and because the
    lines read while waiting are exactly the ones a failure message needs. So:
    one thread appends each line to `proc.lines` and pushes it to `proc.lines_q`;
    EOF pushes a None sentinel so a waiter can tell "the child exited" from
    "the child is slow" without polling.

    Every line stays in `proc.lines` because the pipe can only be read once:
    an assertion that later called `communicate()` would get "" and conclude the
    child printed nothing.
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
                                   name="entrypoint-stdout-drain")
    proc.reader.start()
    return proc


def _output(proc, join_timeout=10.0):
    """Everything the child wrote, once the drain thread has seen EOF."""
    reader = getattr(proc, "reader", None)
    if reader is not None:
        reader.join(join_timeout)
    return "".join(getattr(proc, "lines", []))


def _spawn_raw(sandbox, extra_args):
    """Start `python -m api_server serve <flags>` and hand back the child.

    No readiness wait: a caller that is testing what happens BEFORE the server
    is ready needs the process, not a promise about it. `timeout`/readiness
    lives in :func:`_spawn`.
    """
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "api_server", "serve", *extra_args],
        cwd=str(API_DIR), env=sandbox["env"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return _attach_reader(proc)


def _spawn(sandbox, extra_args, port, timeout=25.0):
    """:func:`_spawn_raw` plus :func:`_wait_for_child` (the served-child setup).

    cwd is the API dir (the unit's WorkingDirectory) because `-m` resolves the
    module from the working directory — the same reason the plist sets it.
    """
    proc = _spawn_raw(sandbox, extra_args)
    try:
        _wait_for_child(proc, port, timeout)
    except Exception:
        _kill(proc)
        raise
    return proc


def _listening(port, timeout=0.25):
    """True when something accepts on 127.0.0.1:port right now."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_port(proc, port, timeout=25.0):
    """Block until the PORT answers. The OLD readiness, kept for one job only.

    Used by the case that reproduces the startup race, precisely because it is
    the earlier of the two readinesses: it returns as soon as the kernel will
    complete a handshake off the listen backlog, which is while the child may
    still be inside startup. That is the moment to signal if you want to know
    whether the process can already answer a stop.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc = proc.poll()
        if rc is not None:
            raise AssertionError(
                f"`-m api_server serve` exited {rc} before its port came up."
                f"\nchild output:\n{_output(proc)}")
        if _listening(port):
            return
        time.sleep(0.05)
    raise AssertionError(
        f"child never listened on 127.0.0.1:{port} within {timeout}s")


def _wait_for_child(proc, port, timeout=25.0):
    """Block until the child SAYS it is ready, then check the port too.

    The contract is the child's `hscc-api: ready` line, not the port
    (t_6eff6258). A port probe cannot express "this process can be stopped
    cleanly": the kernel answers a connect off the listen backlog the instant
    `listen()` runs, long before Python has necessarily reached the signal
    handler, so a caller that SIGTERMs on "port reachable" can hit a child that
    is still starting — on a loaded runner it usually does, and the default
    action answers with rc -15. `serve_main` prints the ready line only after
    the handlers are installed and every listener is bound, so waiting for it
    makes the readiness mean what the SIGTERM tests assume it means.

    The wait is event-driven: it blocks on the drain queue, and the queue's EOF
    sentinel distinguishes "the child died" from "the child is slow" without
    polling or sleeping. The port probe stays as a SECONDARY check — the line
    without a reachable socket would be a lie about the thing launchd needs.
    """
    if not hasattr(proc, "lines_q"):
        _attach_reader(proc)
    deadline = time.time() + timeout
    while True:
        budget = deadline - time.time()
        if budget <= 0:
            raise AssertionError(
                f"child never printed {READY_LINE!r} within {timeout}s; "
                f"child output so far:\n{_output(proc, join_timeout=0.1)}")
        try:
            line = proc.lines_q.get(timeout=budget)
        except queue.Empty:
            raise AssertionError(
                f"child never printed {READY_LINE!r} within {timeout}s; "
                f"child output so far:\n{_output(proc, join_timeout=0.1)}") from None
        if line is None:                      # EOF: the child is on its way out
            rc = proc.wait(timeout=10)
            raise AssertionError(
                f"`-m api_server serve` exited {rc} before saying "
                f"{READY_LINE!r} (a boot-launched unit would do this forever "
                f"under KeepAlive).\nchild output:\n{_output(proc)}")
        if READY_LINE in line:
            break
    if not _listening(port):
        raise AssertionError(
            f"child said {READY_LINE!r} but nothing accepts on "
            f"127.0.0.1:{port}.\nchild output:\n{_output(proc)}")


def _get(port, token=TOKEN, path="/v1/ping", timeout=20.0):
    """GET path with an optional bearer token; retries until the port answers."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:      # answered with 4xx/5xx
            return exc.code, exc.read()
        except Exception as exc:                    # not up yet
            last = exc
            time.sleep(0.1)
    raise AssertionError(f"no answer from 127.0.0.1:{port}: {last!r}")


# ---------------------------------------------------------------------------
# the defect this file exists for
# ---------------------------------------------------------------------------

def test_m_api_server_serve_actually_listens(sandbox):
    """`python -m api_server serve` must BIND and ANSWER.

    Pre-fix this fails with "exited 0 instead of serving" — a clean exit and no
    output, which is exactly why the defect survived: launchd treats exit 0 as
    success, so an inert entrypoint looks healthy in `launchctl print` while
    nothing serves.
    """
    port = _free_port()
    proc = _spawn(sandbox, ["--bind", "127.0.0.1", "--port", str(port)], port)
    try:
        status, body = _get(port)
        assert status == 200, body
        assert body["service"] == "hscc-api"
    finally:
        _kill(proc)


def test_launchd_program_arguments_reach_a_listening_socket(sandbox):
    """The full chain: install-service flags -> plist bytes -> argv -> socket.

    Writes the unit with the library (into a private HOME), reads the
    ProgramArguments back out of the plist, and execs THAT argv. A passthrough
    that stops at the plist text — the shape of the original gap — fails here.
    """
    sys.path.insert(0, str(REPO))
    from hscc_daemon import api_launchd

    port = _free_port()
    agents = sandbox["home"] / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    plist_path = str(agents / "com.hscc.api.plist")
    monkeypatch_target = api_launchd.API_PLIST_PATH      # restored below
    try:
        api_launchd.API_PLIST_PATH = plist_path
        api_launchd.install_api_plist(
            str(API_DIR), python_path=sys.executable,
            serve_extra_args=api_launchd.serve_args(bind="127.0.0.1", port=port))
        with open(plist_path, "rb") as fh:
            args = plistlib.load(fh)["ProgramArguments"]
        # The unit is what gets exec'd — pin the argv shape before running it.
        assert args[:4] == [sys.executable, "-m", "api_server", "serve"]
        assert args[-4:] == ["--bind", "127.0.0.1", "--port", str(port)]

        proc = subprocess.Popen(args, cwd=str(API_DIR), env=sandbox["env"],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        try:
            _wait_for_child(proc, port, 25.0)
            status, body = _get(port)
            assert status == 200, body
        finally:
            _kill(proc)
    finally:
        api_launchd.API_PLIST_PATH = monkeypatch_target


def test_serve_honours_the_token_and_refuses_without_it(sandbox):
    """Passing the bind flags must not weaken auth (design §Hard constraints):
    a launchd-run instance answers exactly like `hscc api start` does."""
    port = _free_port()
    proc = _spawn(sandbox, ["--port", str(port)], port)
    try:
        status, _body = _get(port, token=None)
        assert status == 401, f"unauthenticated request got {status}"
        status, body = _get(port)
        assert status == 200 and body["ok"] is True
    finally:
        _kill(proc)


def test_serve_runs_the_gateway_sidecar_fail_safe(sandbox):
    """A launchd-run instance keeps the live-chat sidecar contract.

    HOME points at the sandbox, so the default `hermes serve` binary does not
    exist: `start_serve` must report False and the API must serve anyway (the
    WS relay's REST fallback stays authoritative). This is the parity check for
    `api_cli._serve` — if a future edit makes a missing sidecar fatal, or drops
    the sidecar from this entrypoint entirely, the iOS live path regresses
    silently after the operator migrates onto launchd.
    """
    port = _free_port()
    proc = _spawn(sandbox, ["--port", str(port)], port)
    try:
        status, body = _get(port)
        assert status == 200 and body["ok"] is True
    finally:
        _kill(proc)


# ---------------------------------------------------------------------------
# fail-closed and refusal paths, as the unit would hit them at boot
# ---------------------------------------------------------------------------

def test_serve_registers_routes_into_the_instance_that_serves(sandbox):
    """The double-import trap that the `sys.modules` alias prevents.

    `python -m api_server` runs this file under the name `__main__`. Every
    routes_*.py does `from api_server import ROUTES`, so unless `api_server` is
    aliased to the `__main__` module, Python imports the SAME file a second
    time and the routes register into THAT instance — while the process that is
    actually listening serves from the `__main__` instance, whose ROUTES hold
    only the inline /v1/ping. The symptom is an API that answers /v1/ping and
    404s everything else, which would look like a working API to `launchctl
    print` and to a smoke test that only checks liveness.

    Discriminator with no side effects: `/v1/profile/install` is registered
    POST-only by routes_profile.py. A GET on it is 405 method_not_allowed if the
    table reached this instance, 404 unknown_route if it did not. Nothing runs.
    """
    port = _free_port()
    proc = _spawn(sandbox, ["--port", str(port)], port)
    try:
        status, _body = _get(port, path="/v1/profile/install")
        assert status == 405, (
            f"expected 405 (route registered in the serving module) but got "
            f"{status} — the routes table is on the other module instance, so "
            f"the sys.modules['api_server'] alias in api_server.py is broken")
    finally:
        _kill(proc)


def test_serve_refuses_a_wide_bind_before_binding(sandbox):
    """0.0.0.0 in a hand-edited plist is refused by the SERVER, not only by
    install-service — the unit file is editable, so the refusal cannot live in
    one place. Non-zero exit, so launchd marks the unit as failed instead of
    leaving it looking healthy."""
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "api_server", "serve", "--bind", "0.0.0.0"],
        cwd=str(API_DIR), env=sandbox["env"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode != 0, "a wide bind must not start the server"
    assert "untrusted network" in out


def test_serve_stops_cleanly_on_sigterm(sandbox):
    """launchd stops the unit with SIGTERM, so that path decides whether the
    supervised `hermes serve` sidecar is torn down or orphaned.

    Without the handler, SIGTERM kills the process immediately: `bridge
    .shutdown()` never runs, the supervised serve survives reparented on 9119
    holding this process's token, and the NEXT boot pays gateway_bridge's
    stale-token heal (the defect that made the iOS read-path 403 for a whole
    session). With it, serve_forever returns, the finally: tears the sidecar
    down, and the process exits 0 having said so.

    Discriminator: a default-action kill leaves rc=-15 and no 'stopped' line.

    The signal now fires the instant the child prints its ready line
    (`_wait_for_child` waits for exactly that), so this is the earliest stop a
    caller can justify — the child has declared its handlers installed, and an
    accept loop may or may not have been entered yet depending on how the run
    was scheduled. Both of those arms must end rc 0; the arm below covers the
    one before the loop existed at all.
    """
    port = _free_port()
    proc = _spawn(sandbox, ["--port", str(port)], port)
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        _kill(proc)
        pytest.fail("SIGTERM did not stop the server — it ignored the signal "
                    "launchd uses to stop the unit")
    out = _output(proc)
    assert proc.returncode == 0, (
        f"SIGTERM killed the process (rc={proc.returncode}) instead of "
        f"shutting it down; the gateway sidecar would be orphaned.\n{out}")
    assert "stopped" in out, out


def test_serve_stops_cleanly_on_a_stop_during_startup(sandbox):
    """A stop that lands BEFORE the ready line must still be a clean stop.

    This is the case the roster caught (rc -15 on a loaded runner, green
    everywhere else). Readiness by port probe returns the moment the kernel
    will complete a handshake off the listen backlog, which is strictly earlier
    than `hscc-api: ready` — so signalling there aims at the window where the
    child has bound its sockets and may still be inside startup. That used to be
    the default action's window: the handlers were installed after the bind, so
    SIGTERM here killed the process outright, `bridge.shutdown()` never ran, and
    the supervised `hermes serve` was left holding the token.

    Discriminator, same as the case above: rc -15 and no 'stopped' line. It is
    not a race this test can win by being lucky — on the fixed code no point
    after the bind is missing a handler, so both scheduling outcomes are
    covered by the same assertions.
    """
    port = _free_port()
    proc = _spawn_raw(sandbox, ["--port", str(port)])
    try:
        _wait_for_port(proc, port)
    except Exception:
        _kill(proc)
        raise
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        _kill(proc)
        pytest.fail("a SIGTERM during startup did not stop the server — the "
                    "handlers were not installed yet when the port came up")
    out = _output(proc)
    assert proc.returncode == 0, (
        f"SIGTERM during startup killed the process (rc={proc.returncode}) "
        f"instead of shutting it down; the gateway sidecar would be "
        f"orphaned.\n{out}")
    assert "stopped" in out, out


# ── the ordering the two cases above cannot see (card t_6eff6258) ──────────
class TestStopHandlersInstallBeforeTheBind:
    """Source pin: the stop handlers exist BEFORE anything is listening.

    The behavioural cases prove the current answer is right; they cannot prove
    it stays right, because the defect they caught is a RACE. Move the
    registration back below the bind and every case in this file passes on an
    idle machine — the child always wins the race there — and only a loaded
    roster runner loses it. A reviewer's "why not just test it" question has no
    honest answer for an ordering that is invisible unless you are unlucky, so
    the ordering itself is pinned at source level.

    Pinned, in AST terms on the function the entrypoint actually runs:
    the `signal.signal` registration precedes the `create_servers` call (bind +
    listen) and precedes `serve_forever` (the card's "do not move serve_forever
    earlier" half), and the ready line is printed after the registration —
    because the readiness the tests wait for is only worth waiting for if it is
    downstream of the handlers being installed.
    """

    def _serve_main_body(self):
        """AST of `serve_main`, with linenos re-based to the real file.

        `inspect.getsource` hands back just the function's own lines, so its
        linenos would start at 1 and every number in a failure message would be
        a line of the excerpt rather than a line of api_server.py a reviewer can
        open.
        """
        source, start = inspect.getsourcelines(api_server.serve_main)
        body = ast.parse("".join(source))
        ast.increment_lineno(body, start - 1)
        return body

    @staticmethod
    def _calls(body, name):
        """Every Call in `body` whose callee is `name` or `*.name`."""
        found = []
        for node in ast.walk(body):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == name:
                found.append(node)
            elif isinstance(func, ast.Attribute) and func.attr == name:
                found.append(node)
        return found

    def test_registration_precedes_the_bind_and_the_serving_call(self):
        body = self._serve_main_body()
        registered = self._calls(body, "signal")
        bound = self._calls(body, "create_servers")
        served = self._calls(body, "serve_forever")
        # Each anchor must EXIST: a pin that only compares is satisfied by
        # deleting the thing it was supposed to order.
        assert registered, (
            "serve_main no longer registers a signal handler — SIGTERM/SIGINT "
            "fall back to the default action, bridge.shutdown() never runs and "
            "the supervised `hermes serve` is orphaned holding the token")
        assert bound, (
            "serve_main no longer calls create_servers by name, so this pin "
            "cannot locate the bind — update it deliberately, do not let it "
            "go quietly inert")
        assert served, "serve_main no longer calls serve_forever at all"
        first_handler = min(n.lineno for n in registered)
        first_bind = min(n.lineno for n in bound)
        first_serving = min(n.lineno for n in served)
        assert first_handler < first_bind, (
            f"the stop handler is registered at :{first_handler} but the "
            f"listeners are bound at :{first_bind} — between those two lines a "
            f"SIGTERM is answered by the default action, which is the orphaned-"
            f"sidecar bug this file exists for")
        assert first_handler < first_serving, (
            f"serve_forever at :{first_serving} now precedes the registration "
            f"at :{first_handler}: a caller could reach the accept loop before "
            f"the handlers exist")

    def test_readiness_line_is_printed_after_the_handlers_are_installed(self):
        """`hscc-api: ready` must sit between the registration and serve_forever.

        The literal is compared against READY_LINE in this file on purpose: the
        readiness contract is the reason the SIGTERM cases can fire when they
        fire, and a production rename would otherwise show up only as a
        25-second timeout in a waiter.
        """
        body = self._serve_main_body()
        printed = [
            n for n in ast.walk(body)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "print" and n.args
            and isinstance(n.args[0], ast.Constant)
            and n.args[0].value == READY_LINE
        ]
        assert printed, (
            f"serve_main no longer prints {READY_LINE!r}; _wait_for_child "
            f"waits for that line, so the readiness contract is broken")
        registered = self._calls(body, "signal")
        served = self._calls(body, "serve_forever")
        first_handler = min(n.lineno for n in registered)
        first_serving = min(n.lineno for n in served)
        for node in printed:
            assert first_handler < node.lineno < first_serving, (
                f"the readiness line at :{node.lineno} is not between the "
                f"handler registration (:{first_handler}) and serve_forever "
                f"(:{first_serving}); a waiter would then proceed before the "
                f"process can be stopped cleanly")

    def test_a_stop_requested_before_serving_never_starts_the_loops(self):
        """The early exit is a promise, and it is pinned because it cannot be run.

        Between the handler being installed and the accept loops existing there
        is a window in which a stop arrives and NOTHING is serving yet. The
        answer has to be "then we never serve": the process must not drain the
        backlog it just opened, it tears the sidecar down and exits 0.

        No case reaches that branch on an idle machine — a SIGTERM that early
        needs the scheduler to cooperate, which is exactly why the roster caught
        it and a workstation never does. Proven by probe: neutralising the guard
        (`if False and stopping.is_set()`, which keeps every other line intact)
        leaves every behavioural case green. So the shape is pinned: a guard
        whose test IS the stop flag — not something that merely mentions it,
        which is what the mutant turns it into — ahead of the readiness line,
        printing its own line instead of `hscc-api: ready`.
        """
        body = self._serve_main_body()
        early = [n for n in ast.walk(body)
                 if isinstance(n, ast.If) and self._is_stop_request_check(n.test)]
        assert early, (
            "serve_main no longer asks the stop flag before serving, so a stop "
            "requested during startup starts the accept loops and serves from "
            "the backlog instead of going straight to the teardown")
        printed = [
            n for n in ast.walk(body)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "print" and n.args
            and isinstance(n.args[0], ast.Constant)
            and n.args[0].value == NOT_SERVING_LINE
        ]
        assert printed, (
            f"nothing prints {NOT_SERVING_LINE!r}: the never-serve path now "
            f"looks exactly like the serving path in the logs, which is how a "
            f"unit that answered nothing while looking healthy stayed hidden")
        registered = self._calls(body, "signal")
        ready = [
            n for n in ast.walk(body)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "print" and n.args
            and isinstance(n.args[0], ast.Constant)
            and n.args[0].value == READY_LINE
        ]
        assert ready, f"no {READY_LINE!r} print to order against"
        first_handler = min(n.lineno for n in registered)
        first_ready = min(n.lineno for n in ready)
        for node in early:
            assert first_handler < node.lineno < first_ready, (
                f"the stop guard at :{node.lineno} is not between the handler "
                f"registration (:{first_handler}) and the readiness line "
                f"(:{first_ready}); either the flag can be set by nobody or the "
                f"waiter is released before the branch can take effect")

    @staticmethod
    def _is_stop_request_check(test):
        """True for exactly `stopping.is_set()`, not for something wrapping it.

        `if False and stopping.is_set()` mentions the same call and changes the
        meaning entirely — a structural pin that matched the BoolOp would bless
        the mutant the behavioural cases already let through.
        """
        return (isinstance(test, ast.Call)
                and isinstance(test.func, ast.Attribute)
                and test.func.attr == "is_set"
                and isinstance(test.func.value, ast.Name)
                and test.func.value.id == "stopping")

    def test_the_pinned_function_is_the_one_the_entrypoint_runs(self):
        """Otherwise the pin could bless a copy that nothing ever executes."""
        body = ast.parse(inspect.getsource(api_server.main))
        assert self._calls(body, "serve_main"), (
            "`python -m api_server serve` no longer reaches serve_main, so "
            "everything pinned above is dead code")


def test_serve_reports_a_port_already_held_and_exits_nonzero(sandbox):
    """KeepAlive turns any startup failure into a retry loop, so the failure
    has to NAME itself. The realistic boot case is a stale `hscc api start`
    instance still holding the port — a launchd-run process writes no
    api.pid, so neither side can see the other.

    The port is bound by this test before the child starts, so the child hits
    EADDRINUSE in create_server(); it must exit non-zero with an actionable
    message, not traceback and not exit 0.
    """
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "api_server", "serve",
             "--port", str(port)],
            cwd=str(API_DIR), env=sandbox["env"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out, _ = proc.communicate(timeout=30)
        assert proc.returncode != 0, f"bound a port already in use:\n{out}"
        assert "Traceback" not in out, out
        assert "cannot bind" in out, out
    finally:
        holder.close()


def test_serve_fails_closed_on_an_empty_token(sandbox):
    """An EMPTY token file -> refuse to start.

    This is the fail-closed case in `load_token`'s contract (a missing file
    legitimately mints a token on first run, so that one is not a refusal). On
    the `start` path it is already covered; this pins it on the launchd path
    too, because a boot-time unit that comes up unauthenticated is the worst
    possible failure mode for a tailnet-bound API and would look healthy in
    `launchctl print`.
    """
    (sandbox["hscc"] / "api-token").write_text("")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "api_server", "serve", "--port",
         str(_free_port())],
        cwd=str(API_DIR), env=sandbox["env"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode != 0, f"started with an empty token:\n{out}"
    assert "empty" in out.lower(), out


def test_serve_without_the_serve_subcommand_fails_loudly(sandbox):
    """A hand-edited unit that drops `serve` must fail, not silently serve
    something the operator did not ask for."""
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "api_server"],
        cwd=str(API_DIR), env=sandbox["env"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode != 0, f"`-m api_server` with no verb served:\n{out}"
    assert "api_server serve" in out, out
