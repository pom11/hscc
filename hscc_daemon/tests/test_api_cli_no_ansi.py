"""No-ANSI regression for the `api` command group (hscc_daemon/api_cli.py).

The api start/stop/status commands were migrated to themed Rich views via
``cli_theme`` (``_console`` → ``make_console`` / ``make_status_panel``). This
pins the invariant the migration must not break: on a NON-tty stdout
(pipe/redirect) Rich must degrade to PLAIN text with NO ANSI escape codes — so
scripts that capture output still get clean text. It also re-checks that the
byte-exact machine contract — the QR payload built by ``_build_qr_payload``
for the iOS scanner — is NEVER altered by theming, and that no literal
``[ok]`` / ``[warn]`` / ``[error]`` markup ever leaks to a piped consumer.

Only presentation (and the QR wire payload) is exercised here; the 
start/stop/status lifecycle logic and the never-print-the-token invariant are
covered by test_api_cli.py / test_unified_cli.py. No real API server is bound
and no real background process is spawned.
"""

import io
from contextlib import redirect_stdout

import pytest

import hscc_daemon.api_cli as api_cli_mod
from hscc_daemon.api_cli import cmd_api

NO_ANSI = "\x1b["


def _make_fake_api(host="127.0.0.1", port=8787, token="SECRETTOKEN",
                   token_raises=False):
    """A fake api_server module for rendering tests (no real bind/read)."""

    class _FakeApi:
        def resolve_config(self, **kw):
            return {"host": host, "port": port}

        def load_token(self):
            if token_raises:
                raise RuntimeError("token file is unreadable")
            return token

    return _FakeApi()


def _capture(fn, *args, **kwargs):
    """Run ``fn`` with stdout redirected to a StringIO; return (out, rc)."""
    out = io.StringIO()
    rc = None
    try:
        with redirect_stdout(out):
            rc = fn(*args, **kwargs)
    except SystemExit as exc:
        rc = exc.code
    return out.getvalue(), rc


@pytest.fixture
def api_env(monkeypatch, tmp_path):
    """Redirect api_cli's PID/log files into the tmp dir and stub the server.

    The real server module would pull in routes_cluster/routes_project; the
    fake keeps rendering tests hermetic. Nothing binds a port or spawns a
    background process. Returns a namespace for per-test control.
    """
    monkeypatch.setattr(api_cli_mod, "_load_api_server", _make_fake_api)
    pid_file = str(tmp_path / "api.pid")
    log_file = str(tmp_path / "api.log")
    monkeypatch.setattr(api_cli_mod, "API_PID_FILE", pid_file)
    monkeypatch.setattr(api_cli_mod, "API_LOG_FILE", log_file)
    return {"pid_file": pid_file, "log_file": log_file}


@pytest.fixture
def no_fork(monkeypatch):
    """Never create a real child process in `start`: fork returns a fake pid."""
    import hscc_daemon.daemon_ops as daemon_ops_mod

    monkeypatch.setattr(api_cli_mod.os, "fork", lambda: 12345)
    monkeypatch.setattr(daemon_ops_mod, "save_pid", lambda pid_file: None)


# ── status ──────────────────────────────────────────────────────────────────

def test_status_not_running_no_ansi(api_env):
    out, rc = _capture(cmd_api, ["status", "--no-qr"])
    assert rc == 0
    assert NO_ANSI not in out, f"status leaked ANSI: {out!r}"
    assert "not running" in out
    assert "╭─ api" in out  # themed status panel present (box degrades to text)
    assert "127.0.0.1:8787" in out  # host:port still reported


def test_status_running_no_ansi(api_env, monkeypatch, tmp_path):
    import os
    monkeypatch.setattr(api_cli_mod, "API_PID_FILE",
                        str(tmp_path / "api.pid"))
    (tmp_path / "api.pid").write_text(str(os.getpid()))
    out, rc = _capture(cmd_api, ["status", "--no-qr"])
    assert rc == 0
    assert NO_ANSI not in out, f"status leaked ANSI: {out!r}"
    assert "running" in out
    assert "╭─ api" in out
    assert "127.0.0.1:8787" in out


def test_status_stale_pid_no_ansi(api_env, monkeypatch, tmp_path):
    # A PID file whose process no longer exists -> stale (warn) state. We can't
    # reliably pick a dead real PID, so exercise the stale branch by pointing
    # API_PID_FILE at a path that exists but whose pid is not running.
    import os
    pid_file = str(tmp_path / "api.pid")
    # Write an impossibly-high PID that cannot belong to a live process.
    (tmp_path / "api.pid").write_text("99999999")
    monkeypatch.setattr(api_cli_mod, "API_PID_FILE", pid_file)
    out, rc = _capture(cmd_api, ["status", "--no-qr"])
    assert rc == 0
    assert NO_ANSI not in out, f"status leaked ANSI: {out!r}"
    assert "stale PID file" in out
    assert "127.0.0.1:8787" in out


def test_status_no_ansi_with_qr(api_env):
    """The QR (block art) + themed prose must be ANSI-free on a non-tty."""
    out, rc = _capture(cmd_api, ["status"])
    assert rc == 0
    assert NO_ANSI not in out, f"status leaked ANSI: {out!r}"
    assert "Scan to connect" in out
    assert "\u2588" in out or "\u2584" in out or "\u2580" in out  # QR rendered
    assert "SECRETTOKEN" not in out  # token stays encoded, never literal


# ── start ──────────────────────────────────────────────────────────────────

def test_start_no_ansi(api_env, no_fork):
    out, rc = _capture(cmd_api, ["start", "--no-qr"])
    assert rc == 0
    assert NO_ANSI not in out, f"start leaked ANSI: {out!r}"
    assert "Starting HSCC API on 127.0.0.1:8787..." in out
    assert "HSCC API started (PID 12345)" in out


def test_start_no_ansi_with_qr(api_env, no_fork):
    out, rc = _capture(cmd_api, ["start"])
    assert rc == 0
    assert NO_ANSI not in out, f"start leaked ANSI: {out!r}"
    assert "Scan to connect" in out
    assert "\u2588" in out or "\u2584" in out or "\u2580" in out
    assert "SECRETTOKEN" not in out


# ── stop ────────────────────────────────────────────────────────────────────

def test_stop_no_ansi(api_env):
    out, rc = _capture(cmd_api, ["stop"])
    assert rc == 0
    assert NO_ANSI not in out, f"stop leaked ANSI: {out!r}"
    assert "HSCC API is not running" in out


def test_stop_running_no_ansi(api_env, monkeypatch):
    import hscc_daemon.daemon_ops as daemon_ops_mod

    # Simulate a live API process without sending real signals: get_pid
    # reports a running pid, and the first liveness probe during the wait loop
    # reports the process is gone (so it hits the "stopped" path, not SIGKILL).
    monkeypatch.setattr(daemon_ops_mod, "get_pid", lambda pid_file=None: 4242)
    kill_calls = []

    def _fake_kill(pid, sig):
        kill_calls.append((pid, sig))
        if sig == 0:
            # liveness probe: first call reports alive, second raises -> gone
            if kill_calls.count((pid, 0)) >= 2:
                raise ProcessLookupError(pid)

    monkeypatch.setattr(api_cli_mod.os, "kill", _fake_kill)
    out, rc = _capture(cmd_api, ["stop"])
    assert rc == 0
    assert NO_ANSI not in out, f"stop leaked ANSI: {out!r}"
    assert "Stopping HSCC API" in out
    assert "HSCC API stopped (PID 4242)" in out


# ── help ────────────────────────────────────────────────────────────────────

def test_help_no_ansi_on_non_tty():
    out, rc = _capture(cmd_api, [])
    assert rc == 0
    assert NO_ANSI not in out, f"help leaked ANSI: {out!r}"
    for kw in ("start", "stop", "status"):
        assert kw in out


def test_unknown_subcommand_no_ansi(api_env):
    out, rc = _capture(cmd_api, ["bogus"])
    assert rc == 1
    assert NO_ANSI not in out, f"unknown-sub hit ANSI: {out!r}"
    assert "unknown api subcommand" in out
    assert "start" in out and "stop" in out and "status" in out


# ── markup integrity: piped output must never leak literal Rich markup ──────

def test_no_raw_markup_leak(api_env, no_fork):
    outs = []
    for argv in (["status", "--no-qr"], ["start", "--no-qr"], ["stop"]):
        out, _ = _capture(cmd_api, argv)
        outs.append(out)
    for text in outs:
        for tag in ("[ok]", "[/ok]", "[warn]", "[/warn]",
                    "[error]", "[/error]", "[title]", "[/title]"):
            assert tag not in text, f"markup leak {tag!r} in {text!r}"


# ── QR wire contract stays byte-identical (never themed) ────────────────────

def test_qr_payload_byte_identical():
    payload = api_cli_mod._build_qr_payload("100.64.0.3", 8787, "TOK123")
    assert payload == (
        '{"v":1,"host":"100.64.0.3","port":8787,"token":"TOK123"}'
    )
    assert not payload.endswith("\n")
    assert '\"port\":8787' in payload and '\"v\":1' in payload


def test_theme_flag_stripped_and_applied(api_env, monkeypatch):
    """`--theme light` selects the palette and never reaches handlers as a
    start flag — it is stripped before dispatch, like the sibling groups."""
    seen = []
    monkeypatch.setattr(
        api_cli_mod, "_handle_start",
        lambda argv=[], theme_name=None: seen.append((argv, theme_name)) or 0)
    out, rc = _capture(cmd_api, ["start", "--theme", "light", "--port", "9999"])
    assert rc == 0
    # theme_name is extracted and forwarded; the underlying argv loses --theme.
    assert seen == [(["--port", "9999"], "light")]
