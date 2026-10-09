"""Rich-markup-injection regression for the HSCC human views (t_716cf37c).

Rich parses every ``str`` renderable as MARKUP: a path, log line, rule id,
card title or exception message containing ``[/bold]`` (any unmatched closing
tag) crashed the whole command with an unhandled ``rich.errors.MarkupError``.
Every probe in the card's review reproduced it live.

The contract these tests pin:

  * a value containing ``[/bold]``/``[bold]`` reaching ANY touched render
    surface (Console.print, Table.add_row, Panel body) renders LITERALLY and
    never raises MarkupError;
  * intentional inline styles the renderer itself builds (``[ok]``/``[error]``
    /``[warn]``/``[dim]``/``[label]``) still work — we escape the DATA, not
    the markup (see ``cli_theme.esc``).

``--json`` is unaffected by design (plain ``print``); the no-ANSI suite
(test_cli_no_ansi.py) keeps pinning that half.
"""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from rich.errors import MarkupError

from hscc_daemon import cli, cli_theme, cluster_render
from hscc_daemon import daemon_ops

MARK_CLOSE = "[/bold]"
MARK_BOTH = "a[bold]b[/bold]c"


def _run(fn):
    """Run ``fn`` capturing stdout; SystemExit is part of the CLI contract.

    ANY other exception (MarkupError in particular) fails the test — that IS
    the bug being pinned.
    """
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            fn()
    except SystemExit:
        pass
    return buf.getvalue()


def _assert_literal(out, value=MARK_CLOSE):
    assert value in out, f"markup not rendered literally: {out!r}"


def _render(fn, *args):
    """Call a render function bound to a console that writes into a buffer.

    Returns the emitted text. MarkupError from any surface fails here.
    """
    buf = io.StringIO()
    console = cli_theme.make_console(file=buf)
    fn(console, *args)
    return buf.getvalue()


# ── hscc log (make_panel body from daemon log lines) ──────────────────────

def test_log_survives_markup_line(monkeypatch):
    monkeypatch.setattr(daemon_ops, "get_daemon_log_tail",
                        lambda n: [f"leak seen at /tmp/p{MARK_CLOSE} x\n"])
    out = _run(cli.cmd_log)
    _assert_literal(out)


def test_log_renders_both_tag_forms(monkeypatch):
    monkeypatch.setattr(daemon_ops, "get_daemon_log_tail",
                        lambda n: [f"tool said {MARK_BOTH}\n"])
    out = _run(cli.cmd_log)
    _assert_literal(out, MARK_BOTH)


# ── hscc triggers (panel body + add_row of rule ids) ──────────────────────

def test_triggers_markup_rule_id(monkeypatch, tmp_path):
    from hscc_daemon import trigger
    monkeypatch.setattr(trigger, "TRIGGERS_FILE", str(tmp_path / "t.json"))
    monkeypatch.setattr(trigger, "COOLDOWN_FILE", str(tmp_path / "c.json"))
    (tmp_path / "t.json").write_text(json.dumps({"rules": [
        {"id": f"r{MARK_CLOSE}", "enabled": True, "cooldown_seconds": 5},
        {"id": f"s{MARK_BOTH}", "enabled": False, "cooldown_seconds": 9},
    ]}))
    (tmp_path / "c.json").write_text(json.dumps({f"r{MARK_CLOSE}": 1791500000}))
    out = _run(cli.cmd_triggers)
    _assert_literal(out)
    _assert_literal(out, MARK_BOTH)
    # Intentional style from the renderer (the enabled marker) still renders:
    # the glyph appears and its [ok] tags are CONSUMED, not leaked.
    assert "✓" in out and "[ok]" not in out


# ── hscc check --repo (table cells + title + hint from a repo path) ───────

def test_check_repo_markup_path(monkeypatch, tmp_path):
    from hscc_daemon import guard_posture
    # The path itself is never stat'ed (posture() is patched below), so no
    # directory needs to exist — the point is the STRING reaching the table.
    repo = f"{tmp_path}/p{MARK_CLOSE}"
    monkeypatch.setattr(
        guard_posture, "posture",
        lambda r: {"state": "armed", "toplevel": f"p{MARK_CLOSE}",
                   "core_hooks_path": f".githooks{MARK_BOTH}",
                   "hook_present": True, "hook_executable": True,
                   "guard_present": True,
                   "detail": f"hooksPath={MARK_CLOSE} verified"})
    monkeypatch.setattr(guard_posture, "exit_code_for", lambda p: 0)
    monkeypatch.setattr(guard_posture, "next_step_for",
                        lambda p: f"run bootstrap in p{MARK_CLOSE}")
    out = _run(lambda: cli.cmd_check("--repo", str(repo)))
    _assert_literal(out)
    _assert_literal(out, MARK_BOTH)


def test_check_repo_failclosed_error_markup(monkeypatch):
    from hscc_daemon import guard_posture

    def boom(repo):
        raise guard_posture.GuardSourceError(f"candidate {MARK_CLOSE} unreadable")

    monkeypatch.setattr(guard_posture, "posture", boom)
    out = _run(lambda: cli.cmd_check("--repo", "/tmp"))
    _assert_literal(out)


# ── hscc status (stream rows + watchdog panel from state files) ───────────

def test_status_markup_watchdog_reason_and_streams(monkeypatch):
    from hscc_daemon import state as state_mod
    monkeypatch.setattr(
        daemon_ops, "daemon_liveness",
        lambda: {"state": "running-stale-heartbeat", "pid": 7,
                 "pid_file_present": True,
                 "last_heartbeat": f"2026-10-09T00:00:00{MARK_CLOSE}"})
    monkeypatch.setattr(
        state_mod, "read_all_states",
        lambda: {
            "dgx": {"ok": True, "timestamp": f"2026-10-09T00:00:00{MARK_CLOSE}"},
            "watchdog": {"blocked": True,
                         "reason": f"wedged on /srv{MARK_BOTH}",
                         "auto_restart_count": 2},
        })
    out = _run(cli.cmd_status)
    _assert_literal(out)
    _assert_literal(out, MARK_BOTH)


# ── hscc check <stream> (state message + exception text) ──────────────────

def test_check_stream_markup_message_and_error(monkeypatch):
    from hscc_daemon import health, state as state_mod
    monkeypatch.setattr(health, "check_dgx", lambda: True)
    monkeypatch.setattr(state_mod, "read_state",
                        lambda s: {"message": f"gpu temp {MARK_BOTH}", "ok": True})
    out = _run(lambda: cli._cmd_check_impl("dgx"))
    _assert_literal(out, MARK_BOTH)

    def boom():
        raise RuntimeError(f"sparkrun died at /srv{MARK_CLOSE}")

    monkeypatch.setattr(health, "check_dgx", boom)
    out = _run(lambda: cli._cmd_check_impl("dgx"))
    _assert_literal(out)
    out = _run(lambda: cli._cmd_check_impl("all"))
    _assert_literal(out)


# ── cluster_render (raw sparkrun/ssh output classes) ──────────────────────

def test_cluster_render_markup_survives():
    cases = [
        ("render_cluster_status", {
            "workloads": [{"name": f"vllm{MARK_CLOSE}", "tp": 1, "pp": 2,
                           "container_id": f"c{MARK_BOTH}"}],
            "idle_hosts": [f"dgx-a{MARK_CLOSE}"], "total_hosts": 3}),
        ("render_hosts", {
            "hosts": [{"name": f"node{MARK_CLOSE}", "ip": "10.0.0.1",
                       "role": f"worker{MARK_BOTH}"}],
            "saved_clusters": {"output": f"cluster show -> /x{MARK_CLOSE}"}}),
        ("render_monitor", {"output": f"raw monitor line {MARK_BOTH}"}),
        ("render_jobs", {"output": f"sparkrun jobs: unit{MARK_CLOSE}.service"}),
        ("render_info", {"cluster_files": {f"saved{MARK_CLOSE}.json": {}}}),
        ("render_stop", {"success": False,
                         "error": f"stop failed at {MARK_BOTH}"}),
        ("render_down", {"success": True,
                         "command": f"systemctl stop x{MARK_CLOSE}",
                         "output": f"stopped {MARK_BOTH}"}),
        ("render_profiles", {"counts": {f"profi{MARK_CLOSE}le": 2}}),
        ("render_template", "list", {
            "templates": [{"name": f"t{MARK_CLOSE}", "version": "1",
                           "group": "g", "description": f"d{MARK_BOTH}"}],
            "count": 1}),
        ("render_template", "validate", {
            "ok": False, "template": f"tpl{MARK_CLOSE}",
            "structural": {"ok": False, "errors": [f"bad key {MARK_BOTH}"],
                           "warnings": []}}),
        ("render_template", "apply", {
            "success": False, "note": f"apply refused: {MARK_BOTH}",
            "errors": [f"line {MARK_CLOSE}"]}),
    ]
    for entry in cases:
        name, *args = entry
        fn = getattr(cluster_render, name)
        out = _render(fn, *args)
        _assert_literal(out)


def test_cluster_render_error_panel_markup():
    out = _render(cluster_render._emit_error_panel, "cluster status",
                  {"error": f"ssh failed at /p{MARK_CLOSE}",
                   "usage": f"hscc x {MARK_BOTH}"})
    _assert_literal(out)


# ── hscc.py human handlers (verify/stats/throughput/autoscale/escalate) ──

def test_verify_markup_detail_and_hint(monkeypatch):
    import sys
    from hscc_daemon import hscc as hscc_mod
    from hscc_daemon import verify as verify_mod
    monkeypatch.setattr(sys, "argv", ["hscc", "verify"])
    monkeypatch.setattr(verify_mod, "run_all", lambda **kw: {
        "ok": False,
        "checks": [{"name": f"guard{MARK_CLOSE}", "ok": False,
                    "detail": f"unreadable /x{MARK_BOTH}",
                    "next_step": f"run bootstrap in /x{MARK_CLOSE}"}],
    })
    out = _run(hscc_mod._handle_verify)
    _assert_literal(out)
    _assert_literal(out, MARK_BOTH)


def test_stats_throughput_autoscale_escalate_markup(monkeypatch):
    import sys
    from hscc_daemon import hscc as hscc_mod
    from hscc_daemon import stats as stats_mod
    from hscc_daemon import throughput as tp_mod
    from hscc_daemon import autoscale, escalate_watcher

    monkeypatch.setattr(sys, "argv", ["hscc", "stats"])
    monkeypatch.setattr(stats_mod, "compute_stats", lambda since_days=7: {
        "since_days": 7,
        "completions": {"total": 1, "by_profile": {f"profi{MARK_CLOSE}le": 1}},
        "activity": {},
    })
    _assert_literal(_run(hscc_mod._handle_stats))

    fake_tp = {"fleet": {"prompt_tokens": 1, "generation_tokens": 2,
                         "running": 0, "waiting": 0, "nodes_ok": 1,
                         "nodes_total": 2},
               "by_node": {f"node{MARK_BOTH}": {"prompt_tokens": 1,
                                                "generation_tokens": 2}}}
    monkeypatch.setattr(tp_mod, "compute_throughput", lambda: fake_tp)
    monkeypatch.setattr(sys, "argv", ["hscc", "throughput"])
    _assert_literal(_run(hscc_mod._handle_throughput), MARK_BOTH)

    monkeypatch.setattr(sys, "argv", ["hscc", "autoscale"])
    monkeypatch.setattr(autoscale, "decide_scale", lambda tp, **kw: {
        "action": "scale_up", "target": 3,
        "reason": f"queue depth at /srv{MARK_CLOSE}"})
    _assert_literal(_run(hscc_mod._handle_autoscale))

    monkeypatch.setattr(sys, "argv", ["hscc", "escalate"])
    monkeypatch.setattr(escalate_watcher, "scan_and_escalate",
                        lambda **kw: [{"task": f"t_abc{MARK_CLOSE}",
                                       "action": "escalate",
                                       "to": f"triage{MARK_BOTH}"}])
    out = _run(hscc_mod._handle_escalate)
    _assert_literal(out)
    _assert_literal(out, MARK_BOTH)


# ── api_route_sweep / autodown panel surfaces ─────────────────────────────

def test_api_route_sweep_markup_rows():
    from hscc_daemon import api_route_sweep
    out = _run(lambda: api_route_sweep.render_human(
        [{"status": "200", "route": f"/api/p{MARK_CLOSE}", "note": "ok",
          "seconds": 0.1},
         {"status": "500", "route": "/api/x", "note": f"boom {MARK_BOTH}",
          "seconds": 0.2}],
        failures=[f"/api/p{MARK_CLOSE}"],
        dynamic=[f"/api/items/{MARK_BOTH}"]))
    _assert_literal(out)
    _assert_literal(out, MARK_BOTH)


def test_autodown_status_panel_markup():
    from hscc_daemon import autodown_cli
    out = _run(lambda: autodown_cli._print_status_panel(
        f"  reason: {cli_theme.esc(MARK_BOTH)}", status="ok", title="t"))
    _assert_literal(out, MARK_BOTH)


# ── the esc() contract itself ─────────────────────────────────────────────

def test_esc_pins_data_but_keeps_renderer_markup():
    # Data comes out literally, ready for embedding in a markup string...
    assert cli_theme.esc(f"p{MARK_CLOSE}") == r"p\[/bold]"
    # ...while the surrounding markup the renderer builds stays live:
    # this must NOT raise and the tag characters must survive.
    def emit():
        cli_theme.make_console().print(f"[ok]{cli_theme.esc(MARK_BOTH)}[/ok]")
    assert MARK_BOTH in _run(emit)


def test_intentional_styles_still_apply():
    """Escaping DATA must not disable the ok/error/warn styling the CLI
    builds for itself: a forced-terminal console must emit ANSI for [ok]."""
    out = _run(lambda: cli_theme.make_console(
        "dark", force_terminal=True, width=80,
    ).print(cli_theme.make_status_panel("all good", status="ok", title="t")))
    assert "\x1b[" in out, "styling lost — theme no longer applies"
    # The markup tags themselves were consumed, not leaked:
    assert "[/ok]" not in out and "OK" in out
