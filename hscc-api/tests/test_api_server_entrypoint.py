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
import json
import os
import plistlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
API_DIR = REPO / "hscc-api"

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


def _spawn(sandbox, extra_args, port, timeout=25.0):
    """Start `python -m api_server serve <flags>` exactly as launchd would.

    cwd is the API dir (the unit's WorkingDirectory) because `-m` resolves the
    module from the working directory — the same reason the plist sets it.
    """
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "api_server", "serve", *extra_args],
        cwd=str(API_DIR), env=sandbox["env"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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


def _wait_for_child(proc, port, timeout=25.0):
    """Block until the child is LISTENING, or fail with its own output.

    Polls the socket rather than scraping stdout: the log line is a nicety, the
    listening socket is the contract (and it returns the moment the bind
    happens instead of always burning the timeout). If the child exits first —
    the inert-entrypoint defect exits 0 immediately — its captured output goes
    into the message, because a clean exit with nothing listening is exactly
    what launchd would keep replaying under KeepAlive.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc = proc.poll()
        if rc is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise AssertionError(
                f"`-m api_server serve` exited {rc} instead of serving "
                f"(a boot-launched unit would do this forever under "
                f"KeepAlive).\nchild output:\n{out}")
        if _listening(port):
            return
        time.sleep(0.05)
    raise AssertionError(
        f"child never listened on 127.0.0.1:{port} within {timeout}s")


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
    """
    port = _free_port()
    proc = _spawn(sandbox, ["--port", str(port)], port)
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        _kill(proc)
        pytest.fail("SIGTERM did not stop the server — it ignored the signal "
                    "launchd uses to stop the unit")
    assert proc.returncode == 0, (
        f"SIGTERM killed the process (rc={proc.returncode}) instead of "
        f"shutting it down; the gateway sidecar would be orphaned.\n{out}")
    assert "stopped" in out, out


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
