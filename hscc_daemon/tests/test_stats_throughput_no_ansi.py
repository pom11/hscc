"""No-ANSI regression for `hscc` stats/throughput/autoscale/escalate/verify + help.

The info/analytics commands (and the help text) were migrated to themed Rich
views (hscc_daemon/hscc.py). This pins the invariant the migration must not
break: on a NON-tty stdout (pipe/redirect) Rich must degrade to PLAIN text
with NO ANSI escape codes — so scripts that capture output still get clean
text. It also re-checks that the machine ``--json`` path stays a byte-exact
raw ``json.dumps`` dump for every command that carries ``--json``.

The computation modules (verify.run_all, stats.compute_stats/format_stats,
throughput.compute_throughput/format_throughput, autoscale.decide_scale,
escalate_watcher.scan_and_escalate) are stubbed to return representative
result dicts; the CLI is the only rendering layer under test.
"""

import io
import json
import sys
from contextlib import redirect_stdout

import pytest

from hscc_daemon import hscc as hscc_mod
from hscc_daemon import verify as verify_mod
from hscc_daemon import stats as stats_mod
from hscc_daemon import throughput as throughput_mod
from hscc_daemon import autoscale as autoscale_mod
from hscc_daemon import escalate_watcher as ew_mod

NO_ANSI = "\x1b["


# ── fixture helpers ─────────────────────────────────────────────────────────

def _run(argv, monkeypatch):
    """Run hscc_mod.main() with sys.argv pinned; return (stdout, exit_code)."""
    monkeypatch.setattr(sys, "argv", argv)
    out = io.StringIO()
    code = None
    with redirect_stdout(out):
        try:
            hscc_mod.main()
        except SystemExit as e:
            code = e.code
    return out.getvalue(), code


# ── verify ─────────────────────────────────────────────────────────────────

_VERIFY_OK = {
    "ok": True,
    "checks": [
        {"name": "plugins", "ok": True, "detail": "all core commands found"},
        {"name": "proxy", "ok": True, "detail": "3 models available"},
    ],
}

_VERIFY_FAIL = {
    "ok": False,
    "checks": [
        {"name": "api_routes", "ok": False,
         "detail": "one or more routes did not answer: POST /chat 404",
         "next_step": "open the failing route; that screen would be dead"},
        {"name": "plugins", "ok": True, "detail": "all core commands found"},
    ],
}

_VERIFY_UNVER = {
    "ok": True,
    "checks": [{"name": "nas", "ok": None, "detail": "could not reach"}],
    "unverified": ["nas"],
}


@pytest.mark.parametrize("result", [_VERIFY_OK, _VERIFY_FAIL, _VERIFY_UNVER])
def test_verify_no_ansi_on_non_tty(result, monkeypatch):
    monkeypatch.setattr(verify_mod, "run_all", lambda **kw: result)
    text, code = _run(["hscc", "verify"], monkeypatch)
    assert NO_ANSI not in text, f"verify leaked ANSI: {text!r}"
    # human view is a Rich Table (not a raw JSON blob of the result)
    assert '"checks"' not in text
    assert "\N{CHECK MARK}" in text or "\N{BALLOT X}" in text


def test_verify_next_step_preserved(monkeypatch):
    """The plain-language 'next step: <hint>' stays greppable in the table."""
    monkeypatch.setattr(verify_mod, "run_all", lambda **kw: _VERIFY_FAIL)
    text, code = _run(["hscc", "verify"], monkeypatch)
    assert NO_ANSI not in text
    assert "next step: open the failing route; that screen would be dead" in text
    assert code == 1  # failing verify still exits non-zero


def test_verify_json_byte_exact(monkeypatch):
    monkeypatch.setattr(verify_mod, "run_all", lambda **kw: _VERIFY_OK)
    text, code = _run(["hscc", "verify", "--json"], monkeypatch)
    assert text == json.dumps(_VERIFY_OK) + "\n"
    assert code == 0


# ── stats ──────────────────────────────────────────────────────────────────

_STATS = {"since_days": 3, "completions": {"total": 12}, "tools": {"total": 4}}


def _stub_stats(monkeypatch):
    monkeypatch.setattr(stats_mod, "compute_stats",
                        lambda since_days: {"since_days": since_days})
    monkeypatch.setattr(stats_mod, "format_stats",
                        lambda s: f"completions={s['since_days']}d,total=12")


def test_stats_no_ansi_on_non_tty(monkeypatch):
    _stub_stats(monkeypatch)
    text, code = _run(["hscc", "stats", "3"], monkeypatch)
    assert NO_ANSI not in text, f"stats leaked ANSI: {text!r}"
    assert code == 0
    assert "fleet stats" in text          # themed panel title
    assert '"since_days"' not in text     # not a raw JSON blob


def test_stats_json_byte_exact(monkeypatch):
    monkeypatch.setattr(stats_mod, "compute_stats",
                        lambda since_days: {"since_days": since_days})
    text, code = _run(["hscc", "stats", "3", "--json"], monkeypatch)
    assert text == json.dumps({"since_days": 3}) + "\n"
    assert code == 0


# ── throughput ─────────────────────────────────────────────────────────────

_TP = {
    "fleet": {"nodes_ok": 2, "total_tokens_per_sec": 1500.0},
    "by_node": {"w1": {"tokens_per_sec": 1000.0}, "w2": {"tokens_per_sec": 500.0}},
}


def _stub_throughput(monkeypatch):
    monkeypatch.setattr(throughput_mod, "compute_throughput", lambda: _TP)
    monkeypatch.setattr(throughput_mod, "format_throughput",
                        lambda d: f"fleet throughput: {d['fleet']['total_tokens_per_sec']:.0f} tok/s")


def test_throughput_no_ansi_on_non_tty(monkeypatch):
    _stub_throughput(monkeypatch)
    text, code = _run(["hscc", "throughput"], monkeypatch)
    assert NO_ANSI not in text, f"throughput leaked ANSI: {text!r}"
    assert code == 0
    assert "fleet throughput" in text
    assert "1500" in text


def test_throughput_json_byte_exact(monkeypatch):
    monkeypatch.setattr(throughput_mod, "compute_throughput", lambda: _TP)
    text, code = _run(["hscc", "throughput", "--json"], monkeypatch)
    assert text == json.dumps(_TP) + "\n"
    assert code == 0


# ── autoscale ──────────────────────────────────────────────────────────────

def _stub_throughput_and_scale(monkeypatch):
    monkeypatch.setattr(throughput_mod, "compute_throughput",
                        lambda: {"fleet": {"nodes_ok": 2}, "by_node": {}})
    monkeypatch.setattr(autoscale_mod, "decide_scale",
                        lambda tp, current_workers: {
                            "action": "scale_up", "target": 4, "reason": "queue deep"})


def test_autoscale_no_ansi_on_non_tty(monkeypatch):
    _stub_throughput_and_scale(monkeypatch)
    text, code = _run(["hscc", "autoscale"], monkeypatch)
    assert NO_ANSI not in text, f"autoscale leaked ANSI: {text!r}"
    assert code == 0
    assert "autoscale: scale_up (target 4) — queue deep" in text


def test_autoscale_json_byte_exact(monkeypatch):
    monkeypatch.setattr(throughput_mod, "compute_throughput",
                        lambda: {"fleet": {"nodes_ok": 2}, "by_node": {}})
    monkeypatch.setattr(autoscale_mod, "decide_scale",
                        lambda tp, current_workers: {"action": "none", "reason": "calm"})
    text, code = _run(["hscc", "autoscale", "--json"], monkeypatch)
    assert text == json.dumps({"action": "none", "reason": "calm"}) + "\n"
    assert code == 0


# ── escalate ───────────────────────────────────────────────────────────────

_ESC = [
    {"task": "t_abc123", "action": "escalate", "to": "human", "category": "health"},
    {"task": "t_def456", "action": "escalate_failed", "to": "none", "category": "health"},
]


def test_escalate_no_ansi_on_non_tty(monkeypatch):
    monkeypatch.setattr(ew_mod, "scan_and_escalate", lambda **_kw: _ESC)
    text, code = _run(["hscc", "escalate"], monkeypatch)
    assert NO_ANSI not in text, f"escalate leaked ANSI: {text!r}"
    assert code == 0
    assert "pending escalations" in text
    assert "t_abc123" in text and "human" in text


def test_escalate_empty_no_ansi(monkeypatch):
    monkeypatch.setattr(ew_mod, "scan_and_escalate", lambda **_kw: [])
    text, code = _run(["hscc", "escalate"], monkeypatch)
    assert NO_ANSI not in text
    assert code == 0
    assert "no escalations pending" in text


def test_escalate_json_byte_exact(monkeypatch):
    monkeypatch.setattr(ew_mod, "scan_and_escalate", lambda **_kw: _ESC)
    text, code = _run(["hscc", "escalate", "--json"], monkeypatch)
    assert text == json.dumps(_ESC) + "\n"
    assert code == 0


# ── help text: no ANSI on non-tty ─────────────────────────────────────────

def test_help_no_ansi_on_non_tty(monkeypatch):
    text, code = _run(["hscc", "help"], monkeypatch)
    assert NO_ANSI not in text, f"help leaked ANSI: {text!r}"
    assert code == 0
    for kw in ("verify", "stats", "throughput", "autoscale", "escalate"):
        assert kw in text


def test_help_advanced_no_ansi_on_non_tty(monkeypatch):
    text, code = _run(["hscc", "help", "advanced"], monkeypatch)
    assert NO_ANSI not in text, f"help advanced leaked ANSI: {text!r}"
    assert code == 0
    assert "start-daemon" in text


# a piped invocation must not carry the literal markup either — Rich parses it
def test_no_raw_markup_leak(monkeypatch):
    monkeypatch.setattr(verify_mod, "run_all", lambda **kw: _VERIFY_OK)
    text, _ = _run(["hscc", "verify"], monkeypatch)
    assert "[ok]" not in text and "[/ok]" not in text
