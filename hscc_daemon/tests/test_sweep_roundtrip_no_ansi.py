"""No-ANSI regression for the secondary entry points (card 5/5).

hscc_daemon/api_route_sweep.py and hscc_daemon/verify_chat_roundtrip.py were
migrated to themed Rich views (cli_theme.make_panel / make_status_panel /
make_table). This pins the invariant the migration must not break: on a NON-tty
stdout (pipe/redirect) Rich must degrade to PLAIN text with NO ANSI escape
codes — so scripts that capture output still get clean text. It also re-checks
that the machine ``--json`` path stays a byte-exact raw ``json.dumps`` dump for
both scripts, since ``verify.py`` shell out to them with ``--json`` and parses
their stdout as JSON.

The computation (api_route_sweep.sweep and verify_chat_roundtrip.run) is
stubbed — it touches LIVE infrastructure (real API, real curl, real chat POST)
and must not be reached by the test. Only the rendering layer is under test.

event_driven.py and the hscc-cluster engine entry are skipped (see the card's
per-file decisions) — ed-* commands surface via inert cli.py placeholders, and
hscc-cluster's deprecated back-compat entry dumps raw JSON by design.
"""

import io
import json
import sys
from contextlib import redirect_stdout

import pytest

import hscc_daemon.api_route_sweep as sweep_mod
import hscc_daemon.verify_chat_roundtrip as roundtrip_mod

NO_ANSI = "\x1b["


def _run(argv, monkeypatch, main_fn):
    """Call ``main_fn()`` with sys.argv pinned; return (stdout, exit_code)."""
    monkeypatch.setattr(sys, "argv", argv)
    out = io.StringIO()
    code = None
    with redirect_stdout(out):
        try:
            code = main_fn()
        except SystemExit as e:
            code = e.code
    return out.getvalue(), code


# ── api_route_sweep ─────────────────────────────────────────────────────────

_ROWS = [
    {"route": "/v1/health", "status": "200", "seconds": 0.05,
     "parses": True, "ok": True, "note": ""},
    {"route": "/v1/sessions", "status": "200", "seconds": 0.12,
     "parses": True, "ok": True, "note": "(needs ?profile= — supplied)"},
    {"route": "/v1/orchestrator/chat", "status": "405", "seconds": 0.02,
     "parses": True, "ok": True, "note": "route exists; POST-only, not exercised"},
    {"route": "/v1/broken", "status": "500", "seconds": 0.9,
     "parses": False, "ok": False, "note": ""},
]
_FAILURES = ["/v1/broken"]
_DYNAMIC = ["/v1/sessions/\\(profile)"]


def _stub_sweep(monkeypatch, rows=_ROWS, failures=_FAILURES, dynamic=_DYNAMIC):
    monkeypatch.setattr(sweep_mod, "sweep",
                        lambda: (rows, failures, dynamic))


def test_sweep_human_no_ansi_on_non_tty(monkeypatch):
    _stub_sweep(monkeypatch)
    text, code = _run(["api_route_sweep.py"], monkeypatch, sweep_mod.main)
    assert code == 1  # a failing route still exits non-zero
    assert NO_ANSI not in text, f"sweep leaked ANSI: {text!r}"
    # human view is a themed table + status panels, not a raw JSON blob
    assert '"routes"' not in text
    assert "/v1/health" in text
    assert "FAIL" in text and "/v1/broken" in text


def test_sweep_ok_human_no_ansi(monkeypatch):
    _stub_sweep(monkeypatch, rows=_ROWS[:-1], failures=[], dynamic=_DYNAMIC)
    text, code = _run(["api_route_sweep.py"], monkeypatch, sweep_mod.main)
    assert code == 0
    assert NO_ANSI not in text
    assert "All swept routes answered with parseable JSON." in text


def test_sweep_json_byte_exact(monkeypatch):
    _stub_sweep(monkeypatch)
    text, code = _run(["api_route_sweep.py", "--json"], monkeypatch,
                      sweep_mod.main)
    assert code == 1
    assert NO_ANSI not in text
    expected = json.dumps({"routes": _ROWS, "failures": _FAILURES,
                           "not_swept_dynamic": _DYNAMIC}, indent=2)
    assert text == expected + "\n"


def test_sweep_json_ignores_theme_flag(monkeypatch):
    """--theme with --json must not alter the byte-exact machine path."""
    _stub_sweep(monkeypatch)
    text, code = _run(["api_route_sweep.py", "--json", "--theme", "light"],
                      monkeypatch, sweep_mod.main)
    assert code == 1
    expected = json.dumps({"routes": _ROWS, "failures": _FAILURES,
                           "not_swept_dynamic": _DYNAMIC}, indent=2)
    assert text == expected + "\n"


def test_sweep_human_accepts_theme(monkeypatch):
    _stub_sweep(monkeypatch)
    text, code = _run(["api_route_sweep.py", "--theme", "light"],
                      monkeypatch, sweep_mod.main)
    assert NO_ANSI not in text
    assert code == 1
    assert "/v1/health" in text


# ── verify_chat_roundtrip ───────────────────────────────────────────────────

_OK = {
    "base": "http://100.64.0.1:8788",
    "metrics_url": "http://100.64.0.1:8000/metrics",
    "tokens_before": 100,
    "job_id": "job_abc",
    "status": "done",
    "elapsed": 3.25,
    "reply": "pong",
    "tokens_after": 115,
    "delta": 15,
    "metric_verified": True,
    "ok": True,
}

_FAIL = {
    "base": "http://100.64.0.1:8788",
    "metrics_url": "http://100.64.0.1:8000/metrics",
    "tokens_before": 100,
    "http": 500,
    "job_id": "job_xyz",
    "reply": None,
    "error": "POST /v1/orchestrator/chat did not yield a job.",
}

_PRECOND = {"error": "no API host or token (is `hscc api status` running?)"}


def _stub_run(monkeypatch, code, result):
    monkeypatch.setattr(roundtrip_mod, "run", lambda *a, **k: (code, result))


def test_roundtrip_ok_human_no_ansi(monkeypatch):
    _stub_run(monkeypatch, 0, _OK)
    text, code = _run(["verify_chat_roundtrip.py"], monkeypatch,
                      roundtrip_mod.main)
    assert code == 0
    assert NO_ANSI not in text, f"roundtrip leaked ANSI: {text!r}"
    assert '"job_id"' not in text   # not a raw JSON blob of the result
    assert "job_abc" in text
    assert "delta +15" in text


def test_roundtrip_fail_human_no_ansi(monkeypatch):
    _stub_run(monkeypatch, 1, _FAIL)
    text, code = _run(["verify_chat_roundtrip.py"], monkeypatch,
                      roundtrip_mod.main)
    assert code == 1
    assert NO_ANSI not in text, f"roundtrip FAILED leaked ANSI: {text!r}"
    assert "CHAT ROUND TRIP FAILED" in text
    assert "job_xyz" in text


def test_roundtrip_precond_human_no_ansi(monkeypatch):
    _stub_run(monkeypatch, 2, _PRECOND)
    text, code = _run(["verify_chat_roundtrip.py"], monkeypatch,
                      roundtrip_mod.main)
    assert code == 2
    assert NO_ANSI not in text, f"roundtrip precond leaked ANSI: {text!r}"
    assert "cannot run chat round trip" in text


def test_roundtrip_json_byte_exact(monkeypatch):
    _stub_run(monkeypatch, 0, _OK)
    text, code = _run(["verify_chat_roundtrip.py", "--json"], monkeypatch,
                      roundtrip_mod.main)
    assert code == 0
    assert NO_ANSI not in text
    assert text == json.dumps(_OK, indent=2) + "\n"


def test_roundtrip_fail_json_byte_exact(monkeypatch):
    _stub_run(monkeypatch, 1, _FAIL)
    text, code = _run(["verify_chat_roundtrip.py", "--json"], monkeypatch,
                      roundtrip_mod.main)
    assert code == 1
    assert NO_ANSI not in text
    assert text == json.dumps(_FAIL, indent=2) + "\n"


def test_roundtrip_json_ignores_theme_flag(monkeypatch):
    _stub_run(monkeypatch, 0, _OK)
    text, code = _run(
        ["verify_chat_roundtrip.py", "--json", "--theme", "dark"],
        monkeypatch, roundtrip_mod.main)
    assert code == 0
    assert text == json.dumps(_OK, indent=2) + "\n"


def test_roundtrip_human_accepts_theme(monkeypatch):
    _stub_run(monkeypatch, 0, _OK)
    text, code = _run(["verify_chat_roundtrip.py", "--theme", "light"],
                      monkeypatch, roundtrip_mod.main)
    assert code == 0
    assert NO_ANSI not in text
    assert "job_abc" in text


# ── markup integrity: piped output must never leak literal Rich markup ──────

def test_sweep_no_raw_markup_leak(monkeypatch):
    _stub_sweep(monkeypatch)
    text, _ = _run(["api_route_sweep.py"], monkeypatch, sweep_mod.main)
    for tag in ("[ok]", "[/ok]", "[warn]", "[/warn]", "[error]", "[/error]"):
        assert tag not in text, f"markup leak {tag!r} in {text!r}"


def test_roundtrip_no_raw_markup_leak(monkeypatch):
    for code, res in ((0, _OK), (1, _FAIL), (2, _PRECOND)):
        _stub_run(monkeypatch, code, res)
        text, _ = _run(["verify_chat_roundtrip.py"], monkeypatch,
                       roundtrip_mod.main)
        for tag in ("[ok]", "[/ok]", "[warn]", "[/warn]",
                    "[error]", "[/error]"):
            assert tag not in text, f"markup leak {tag!r} in {text!r}"
