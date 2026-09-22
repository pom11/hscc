"""Rich rendering tests for the migrated HSCC CLI commands.

These pin the HUMAN view (Rich, themed) of the commands this card rewrote —
`verify`, `stats`, `throughput`, `autoscale`, `escalate`, and the two help
functions — while ensuring the machine `--json` path stays a byte-exact plain
dump and the `--theme` flag only selects the palette (never leaks into JSON).

The existing suite (test_unified_cli.py) already asserts the real-handler
substrings survive; this file focuses on the NEW behaviour this card adds:
theme selection and Rich output structure.
"""

import io
import sys
from contextlib import redirect_stdout

import pytest

from hscc_daemon import hscc as hscc_mod
from hscc_daemon import verify as verify_mod
from hscc_daemon import stats as stats_mod
from hscc_daemon import throughput as throughput_mod
from hscc_daemon import autoscale as autoscale_mod
from hscc_daemon import escalate_watcher as ew_mod


# ── --theme is stripping-only, never a JSON-affecting token ───────────────

def test_strip_theme_arg_both_forms():
    cleaned, name = hscc_mod._strip_theme_arg(
        ["verify", "--theme", "light", "--json"])
    assert cleaned == ["verify", "--json"]
    assert name == "light"

    cleaned, name = hscc_mod._strip_theme_arg(["stats", "--theme=dark", "5"])
    assert cleaned == ["stats", "5"]
    assert name == "dark"


def test_strip_theme_arg_no_flag():
    cleaned, name = hscc_mod._strip_theme_arg(["verify"])
    assert cleaned == ["verify"]
    assert name is None


def test_strip_theme_arg_dangling_flag_is_ignored():
    cleaned, name = hscc_mod._strip_theme_arg(["verify", "--theme"])
    assert cleaned == ["verify"]
    assert name is None


def test_verify_json_ignores_theme(monkeypatch):
    """--json + --theme must produce exactly the raw dump — theme never leaks."""
    monkeypatch.setattr(sys, "argv", ["hscc", "verify", "--json", "--theme", "light"])
    fake = {"ok": True, "checks": [{"name": "plugins", "ok": True, "detail": "all good"}]}
    monkeypatch.setattr(verify_mod, "run_all", lambda **kw: fake)
    out = io.StringIO()
    with redirect_stdout(out):
        with pytest.raises(SystemExit):
            hscc_mod._handle_verify()
    assert out.getvalue() == '{"ok": true, "checks": [{"name": "plugins", "ok": true, "detail": "all good"}]}\n'


# ── verify human view is themed Rich (glyphs + styles), not plain print ───

def test_verify_human_view_is_themed(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hscc", "verify", "--theme", "dark"])
    fake = {
        "ok": True,
        "checks": [
            {"name": "api_routes", "ok": True, "detail": "routes ok"},
        ],
    }
    monkeypatch.setattr(verify_mod, "run_all", lambda **kw: fake)
    out = io.StringIO()
    with redirect_stdout(out):
        with pytest.raises(SystemExit):
            hscc_mod._handle_verify()
    text = out.getvalue()
    assert "\N{CHECK MARK}" in text          # themed glyph
    # Rich parsed our markup and degraded to clean plain text (no raw ANSI nor
    # un-parsed "[ok]" literals leaked) — the documented pipe/redirect behaviour.
    assert "\x1b[" not in text
    assert "[ok]" not in text
    assert "All checks passed" in text


def test_verify_light_theme_accepts_name(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hscc", "verify", "--theme=light"])
    fake = {"ok": True, "checks": [{"name": "a", "ok": True, "detail": "d"}]}
    monkeypatch.setattr(verify_mod, "run_all", lambda **kw: fake)
    out = io.StringIO()
    with redirect_stdout(out):
        with pytest.raises(SystemExit):
            hscc_mod._handle_verify()
    assert "All checks passed" in out.getvalue()


# ── stats renders format_stats output inside a themed Panel ───────────────

def test_stats_human_view_wraps_patched_format_stats(monkeypatch):
    """format_stats is patched by existing tests (returns f'ok:{n}d'); its
    output must survive inside the themed Panel so `ok:0d`/`ok:5d` still match."""
    monkeypatch.setattr(sys, "argv", ["hscc", "stats", "--json"])
    monkeypatch.setattr(stats_mod, "compute_stats", lambda since_days: {"since_days": since_days})

    # --json path ignores the human rendering entirely
    out = io.StringIO()
    with redirect_stdout(out):
        with pytest.raises(SystemExit):
            hscc_mod._handle_stats()
    assert out.getvalue() == '{"since_days": 7}\n'

    # human path wraps format_stats in a Panel
    monkeypatch.setattr(sys, "argv", ["hscc", "stats", "0"])
    monkeypatch.setattr(stats_mod, "format_stats", lambda r: f"ok:{r['since_days']}d")
    out = io.StringIO()
    with redirect_stdout(out):
        with pytest.raises(SystemExit):
            hscc_mod._handle_stats()
    text = out.getvalue()
    assert "ok:0d" in text
    assert "fleet stats" in text  # themed panel title


# ── autoscale keeps the decision substring in a status Panel ──────────────

def test_autoscale_human_view_keeps_decision_line(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hscc", "autoscale"])
    monkeypatch.setattr(throughput_mod, "compute_throughput",
                        lambda: {"fleet": {"nodes_ok": 2}, "by_node": {}})
    monkeypatch.setattr(autoscale_mod, "decide_scale",
                        lambda tp, current_workers: {
                            "action": "scale_up", "target": 4, "reason": "queue deep"})
    out = io.StringIO()
    with redirect_stdout(out):
        with pytest.raises(SystemExit):
            hscc_mod._handle_autoscale()
    text = out.getvalue()
    assert "autoscale: scale_up (target 4) — queue deep" in text
    assert "SCALE_UP" in text  # status panel header glyph


def test_autoscale_json_exact(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hscc", "autoscale", "--json", "--theme", "light"])
    monkeypatch.setattr(throughput_mod, "compute_throughput",
                        lambda: {"fleet": {"nodes_ok": 2}, "by_node": {}})
    monkeypatch.setattr(autoscale_mod, "decide_scale",
                        lambda tp, current_workers: {"action": "none", "reason": "calm"})
    out = io.StringIO()
    with redirect_stdout(out):
        with pytest.raises(SystemExit):
            hscc_mod._handle_autoscale()
    assert out.getvalue().startswith('{"action": "none", "reason": "calm"}')


# ── escalate renders a table (or empty line) ──────────────────────────────

def test_escalate_human_view_table(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hscc", "escalate"])
    monkeypatch.setattr(ew_mod, "scan_and_escalate",
                        lambda **_kw: [{"task": "t1", "action": "escalate",
                                        "to": "human", "category": "health"}])
    out = io.StringIO()
    with redirect_stdout(out):
        with pytest.raises(SystemExit):
            hscc_mod._handle_escalate()
    text = out.getvalue()
    assert "pending escalations" in text
    assert "t1" in text and "human" in text


def test_escalate_empty_list(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hscc", "escalate"])
    monkeypatch.setattr(ew_mod, "scan_and_escalate", lambda **_kw: [])
    out = io.StringIO()
    with redirect_stdout(out):
        with pytest.raises(SystemExit):
            hscc_mod._handle_escalate()
    assert "no escalations pending" in out.getvalue()


# ── help functions return Rich renderables (not str) ──────────────────────

def test_get_help_text_is_renderable(monkeypatch):
    from rich.console import Group
    renderable = hscc_mod._get_help_text()
    assert isinstance(renderable, Group)
    # every required command keyword is still represented
    text = _render(renderable)
    for kw in ("throughput", "autoscale", "escalate", "project <cmd>",
               "template validate", "cluster status"):
        assert kw in text


def test_get_advanced_help_is_renderable():
    from rich.console import Group
    renderable = hscc_mod._get_advanced_help()
    assert isinstance(renderable, Group)
    text = _render(renderable)
    for kw in ("start-daemon", "ed-status", "ed-install", "ed-uninstall"):
        assert kw in text


def _render(renderable):
    """Render a Rich renderable to plain text via the theme console."""
    from hscc_daemon import cli_theme as t
    out = io.StringIO()
    with redirect_stdout(out):
        t.make_console().print(renderable)
    return out.getvalue()
