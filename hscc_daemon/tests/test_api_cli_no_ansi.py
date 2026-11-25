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

No real ``launchctl`` is ever EXECED anywhere in this file: the fence in
``launchd_status`` refuses to ask inside a pytest process, so an unsheamed verb
gets a visible refusal row rather than a fork.

What is NOT hermetic, stated plainly because it is the sentence the next reader
acts on: only the THREE lifecycle cases below declare a supervisor posture
(``test_status_not_running_no_ansi``, ``test_stop_no_ansi``,
``test_stop_running_no_ansi`` — see ``_supervisor_has_no_unit``). The other four
that drive these verbs — ``test_no_raw_markup_leak``, ``test_status_no_ansi_with_qr``,
``test_status_running_no_ansi``, ``test_status_stale_pid_no_ansi`` — still call them
unsheamed, and ``_launchd_matters()`` stats the operator's real
``~/Library/LaunchAgents/com.hscc.api.plist`` for each. They stay green only
because the fence answers and their assertions never read a supervisor row.
Residual tracked in t_6c4c17fc; do not copy the unsheamed shape into a new case.
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

        # t_cc7b6023: `status` now reads the credential through `peek_token`
        # (it must never create one). A fake standing in for a CURRENT payload
        # answers both; a fake with only `load_token` models a payload so old it
        # cannot read without minting, and `status` correctly prints no QR.
        def peek_token(self):
            return self.load_token()

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


# ── the supervisor seam ─────────────────────────────────────────────────────
#
# `status` and `stop` ask the supervisor who owns the API before they answer
# (`_launchd_matters()` → `launchd_status()`), and that read is AMBIENT. The gate
# opens on the operator's real `~/Library/LaunchAgents/com.hscc.api.plist`, so on
# any box where the unit was ever installed — a workstation, and the box the
# merge-queue gate runs on — it opened inside every case in this file. The
# t_8bf34130 fence then refused to answer inside a pytest process, and the three
# lifecycle cases below rendered whatever the host happened to be doing instead
# of what they claim: `status` printed "state unknown (supervisor not
# consulted)" where it asserts "not running", and `stop` failed CLOSED with rc 1
# where it asserts its normal report. (Both directions are the same defect: an
# assertion that holds only because no API happens to be running reads the host
# just as hard as one that fails because one is.)
#
# The fence itself is the feature and is NOT loosened here — a suite that forgets
# the seam must see the refusal, pinned in
# test_api_cli_unverifiable_supervisor_row_t_8bf34130.py and
# test_api_cli_launchd.py::test_probe_refuses_to_run_inside_pytest_without_a_seam.
# What these cases lacked is the seam they were already being told to inject.
# Each now DECLARES the posture it draws — macOS, a unit file installed, no job
# loaded — which is exactly the unsupervised world those three verbs report and
# SIGTERM about, and pins the two ambient inputs that decide whether the seam is
# consulted at all. Same shape as test_api_cli_launchd.py::_run_status.
#
# So the verdict of THESE THREE cases no longer moves with the host: one canned
# `launchctl print` answer is the fact under test, and the cases that want the
# supervised (bootout) path are the ones in test_api_cli_launchd.py that declare
# it. The four status/markup cases that drive these verbs without a seam are the
# residual named in the module docstring (t_6c4c17fc) — they are green on the
# fence's refusal, not on a declared posture.

#: Measured on this box: `launchctl print gui/<uid>/com.hscc.api` for a job that
#: is not loaded answers rc 113 with `Bad request.` plus a could-not-find-service
#: line (and `_launchctl_run` folds stderr into the returned text). `launchd_status`
#: reads ANY rc!=0 as an ANSWER — available, not loaded — never as a fault. Kept as
#: data so the posture is readable rather than buried in a lambda. The same value is
#: pinned at the probe level in test_api_cli_launchd.py::
#: test_probe_not_loaded_is_an_answer_not_a_fault; rc 3 is bootout's already-unloaded
#: answer, not print's.
LAUNCHCTL_NO_JOB = (
    113,
    'Bad request.\nCould not find service "com.hscc.api" '
    "in domain for user gui: 501",
)


class _Probe:
    """A canned `launchctl` runner. Records every argv it was asked to run."""

    def __init__(self, rc, out):
        self.rc = rc
        self.out = out
        self.calls = []

    def __call__(self, argv, timeout=10):
        self.calls.append([str(a) for a in argv])
        return self.rc, self.out


def _supervisor_has_no_unit(monkeypatch, tmp_path):
    """Declare: unit file installed, no launchd job loaded. Returns the probe.

    Pins every input the supervisor question can turn on:
      * `sys.platform` — the gate's first test, so the seam is consulted on the
        same code path on every host instead of only on macOS;
      * `api_launchd.API_PLIST_PATH` — a unit file on disk is what makes asking
        launchd worth a fork, and pinning it keeps the case from inheriting the
        host's real LaunchAgents directory;
      * `_run_probe` — the canned answer, so `launchctl` is never execed.

    The original `launchd_status` is captured BEFORE the patch: the obvious
    `lambda **kw: api_cli_mod.launchd_status(_run_probe=probe)` self-recurses,
    because by the time the lambda runs the attribute IS the lambda.
    """
    from hscc_daemon import api_launchd

    monkeypatch.setattr(api_cli_mod.sys, "platform", "darwin")
    plist = tmp_path / "com.hscc.api.plist"
    plist.write_text("declared by the case; never read by the verbs\n")
    monkeypatch.setattr(api_launchd, "API_PLIST_PATH", str(plist))
    probe = _Probe(*LAUNCHCTL_NO_JOB)
    real = api_cli_mod.launchd_status
    monkeypatch.setattr(api_cli_mod, "launchd_status",
                        lambda **kw: real(_run_probe=probe))
    return probe


#: The verbs may ask launchd exactly once, and only ever the read-only `print`.
#: An empty list would mean the seam was never consulted (the ambient bug back);
#: a `bootout` entry would mean the case silently moved onto the supervised path.
PRINT_ONLY = ["print"]


# ── status ──────────────────────────────────────────────────────────────────

def test_status_not_running_no_ansi(api_env, monkeypatch, tmp_path):
    # Declared posture (see the supervisor seam block): no pid file AND no loaded
    # unit — the only box on which "not running" is a true statement. Asking the
    # host instead would make this case pass on a machine with no API and fail on
    # one with an API, which is the same ambient bug in two directions.
    probe = _supervisor_has_no_unit(monkeypatch, tmp_path)
    out, rc = _capture(cmd_api, ["status", "--no-qr"])
    assert rc == 0
    assert NO_ANSI not in out, f"status leaked ANSI: {out!r}"
    assert "not running" in out
    assert "launchd probe refused" not in out  # the seam answered, not the fence
    # No supervisor row at all: launchd was ASKED (see the call count below) and
    # answered "no such job". Without this line the case would still pass on a
    # box where the unit IS loaded — launchd's own "state = not running" contains
    # the phrase above, which is how a weak assertion re-opens the ambient hole.
    assert "Supervisor:" not in out
    assert "╭─ api" in out  # themed status panel present (box degrades to text)
    assert "127.0.0.1:8787" in out  # host:port still reported
    assert [c[1] for c in probe.calls] == PRINT_ONLY


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

def test_stop_no_ansi(api_env, monkeypatch, tmp_path):
    # Declared posture: nothing supervises the API and nothing is running, so the
    # verb's report — not a fail-closed refusal — is what this case renders.
    probe = _supervisor_has_no_unit(monkeypatch, tmp_path)
    out, rc = _capture(cmd_api, ["stop"])
    assert rc == 0
    assert NO_ANSI not in out, f"stop leaked ANSI: {out!r}"
    assert "HSCC API is not running" in out
    assert [c[1] for c in probe.calls] == PRINT_ONLY


def test_stop_running_no_ansi(api_env, monkeypatch, tmp_path):
    import hscc_daemon.daemon_ops as daemon_ops_mod

    # Declared posture: a unit file is installed but launchd has no job loaded,
    # so nobody supervises this pid and SIGTERM is the right verb (the bootout
    # path is declared and pinned in test_api_cli_launchd.py). Without the seam
    # the verb fails CLOSED on a refusal — rc 1 and an error on stderr — and this
    # case would be asserting the host's supervisor state, not its rendering.
    probe = _supervisor_has_no_unit(monkeypatch, tmp_path)
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
    # Signalled, not booted out: one read-only supervisor question.
    assert [c[1] for c in probe.calls] == PRINT_ONLY


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
