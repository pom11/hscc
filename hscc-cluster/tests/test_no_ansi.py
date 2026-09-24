"""No-ANSI regression for the themed hscc-cluster standalone CLI entry points.

The standalone/back-compat entry points (hscc.py:main, cluster_template_cli.py
__main__, cluster_template.py:main, gateway_restart.py __main__) were migrated
from raw ``print`` / ``json.dumps`` to the themed Rich surface (hscc-cluster/
_theme.py, the ``_theme`` pattern from hscc-project). This pins the two
invariants the migration must not break:

  1. On a NON-tty stdout (pipe/redirect), the themed Console must degrade to
     PLAIN text with NO ANSI escape codes — so scripts that capture output still
     get clean text.
  2. The machine ``--json`` path stays a byte-exact raw ``json.dumps`` dump
     (daemon / scripts / the iOS console parse it).

The engine cmd_* functions are library functions (they return dicts); we feed
representative result dicts straight to the standalone render helpers
(``hscc._emit_result`` / ``cluster_template_cli`` panel / ``_emit_template`` /
``gateway_restart._emit_result``) and assert on the rendered output, exactly as
the sibling hscc_daemon no-ANSI suite does.

The rule "assert the constant, never a literal value" is honoured: the ANSI
escape sequence is asserted via the module constant ``NO_ANSI`` and the
anti-collapse non-tty width is asserted via the exported
``DEFAULT_NONTTY_WIDTH`` constant, not a magic literal.
"""

import io
import json
import sys
from contextlib import redirect_stdout

import pytest

import _theme
import hscc
import gateway_restart
import cluster_template_cli
import cluster_template

# Assert the constant, never a literal value (Rich's ESC[).
NO_ANSI = "\x1b["
NO_ANSI_BYTE = "\x1b"


def _no_ansi(fn):
    """Run ``fn`` with stdout redirected to a NON-tty StringIO; return output.

    Asserts no ANSI escapes — the mandatory no-ANSI regression for a converted
    command.
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    out = buf.getvalue()
    assert NO_ANSI not in out, f"ANSI escape found in piped output: {out!r}"
    assert NO_ANSI_BYTE not in out, f"ANSI escape found in piped output: {out!r}"
    return out


# ── hscc.py main() command-result render (_emit_result) ────────────────────

class TestNoAnsiHscc:
    """`hscc-cluster <cmd>` result rendering: human plain on non-tty, --json raw."""

    def test_cluster_status_human(self):
        out = _no_ansi(lambda: hscc._emit_result("cluster-status", {
            "workloads": [{"name": "@official/qwen3", "tp": 2, "pp": 1,
                           "container_id": "abc123"}],
            "idle_hosts": ["10.0.0.3"],
            "total_hosts": 4,
        }))
        assert "@official/qwen3" in out
        assert "abc123" in out
        assert "4" in out
        # human view is NOT a bare json blob
        assert '"workloads"' not in out

    @pytest.mark.parametrize("cmd,result", [
        ("down", {"success": True, "dry_run": True,
                  "command": ["sparkrun", "stop", "--all"]}),
        ("up", {"success": True, "dry_run": True, "units": 3}),
        ("stop", {"success": False, "returncode": 1,
                  "error": "no such container"}),
    ])
    def test_fleet_ops_human(self, cmd, result):
        out = _no_ansi(lambda: hscc._emit_result(cmd, result))
        assert out.strip()

    def test_profile_status_human(self):
        out = _no_ansi(lambda: hscc._emit_result("profile-status", {
            "counts": {"backend-engineer": 3, "researcher-a": 2},
            "total_running": 5,
            "profiles": ["backend-engineer", "researcher-a"],
        }))
        assert "backend-engineer" in out
        assert "3" in out

    def test_escape_literal_markup(self):
        # literal Rich markup in engine-derived content renders literally (no
        # MissingStyle, no style injection)
        out = _no_ansi(lambda: hscc._emit_result("hosts", {
            "hosts": [{"name": "[PRUNE]keep", "ip": "10.0.0.1", "role": "gateway"}],
        }))
        assert "[PRUNE]keep" in out

    def test_json_byte_identical(self):
        result = {"success": True, "returncode": 0,
                  "output": "Job: @official/qwen3  [abc123]"}
        expected = json.dumps(result, indent=2, default=str) + "\n"
        out = _no_ansi(lambda: hscc._emit_result("jobs", result, as_json=True))
        assert out == expected, "--json output must be byte-identical"


# ── cluster_template_cli.py __main__ result render ─────────────────────────

class TestNoAnsiTemplateCli:
    """`cluster-template <sub>` human plain on non-tty, --json raw."""

    def _human(self, args, result):
        # mirror the __main__ theme path (panel over _template_lines)
        out = []
        console = _theme.make_console()
        buf = io.StringIO()
        with redirect_stdout(buf):
            console.print(_theme.panel(args[0], "\n".join(cluster_template_cli._template_lines(result))))
        out = buf.getvalue()
        assert NO_ANSI not in out and NO_ANSI_BYTE not in out, f"ANSI: {out!r}"
        return out

    def test_list_human(self):
        out = self._human(["list"], {"count": 2, "templates": [
            {"name": "3node-coding", "version": 3, "description": "coding"},
            {"name": "lite", "version": 2, "description": ""}]})
        assert "3node-coding" in out
        assert "2" in out

    def test_preview_human(self):
        out = self._human(["preview"], {"template": "3node-coding",
                                        "description": "coding",
                                        "changes": [
                                            {"file": "serving.json", "action": "write"},
                                            {"file": "models.json", "action": "write"}]})
        assert "3node-coding" in out
        assert "serving.json" in out

    def test_escape_literal_markup(self):
        out = self._human(["preview"], {"changes": [
            {"file": "serving.json", "action": "write", "summary": "[PRUNE]x"}]})
        assert "[PRUNE]x" in out

    def test_json_byte_identical(self):
        # __main__ --json path is exercised as a real subprocess against THIS
        # worktree's plugin dir (not the primary checkout) and must byte-equal
        # the canonical raw dump.
        import os
        import subprocess as _sp
        plugin_dir = os.path.dirname(os.path.dirname(
            os.path.abspath(cluster_template_cli.__file__)))
        result = _sp.run(
            ["python", os.path.join(plugin_dir, "cluster_template_cli.py"),
             "list", "--json"],
            capture_output=True, text=True, cwd=os.path.dirname(plugin_dir),
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        canonical = json.dumps(payload, indent=2, default=str) + "\n"
        assert result.stdout == canonical, "--json output must be byte-identical"
        assert "\x1b" not in result.stdout


# ── cluster_template.py main() result render (_emit_template) ──────────────

class _Args:
    def __init__(self, command, json=False):
        self.command = command
        self.json = json


class TestNoAnsiClusterTemplate:
    """`python cluster_template.py <cmd>` human plain on non-tty, --json raw."""

    @pytest.mark.parametrize("command,result", [
        ("list", {"count": 2, "templates": [
            {"name": "3node-coding", "version": 3, "description": "coding"}]}),
        ("preview", {"template": "3node-coding", "changes": [
            {"file": "serving.json", "action": "write"}]}),
        ("apply", {"template": "3node-coding", "success": True,
                   "steps": [{"step": "serving.json", "status": "ok"}]}),
    ])
    def test_human_no_ansi(self, command, result):
        out = _no_ansi(lambda: cluster_template._emit_template(
            _Args(command), result))
        assert out.strip()

    def test_escape_literal_markup(self):
        out = _no_ansi(lambda: cluster_template._emit_template(_Args("preview"), {
            "changes": [{"file": "serving.json", "action": "write",
                         "summary": "[KEEP]really"}]}))
        assert "[KEEP]really" in out

    def test_json_byte_identical(self):
        result = {"count": 2, "templates": [{"name": "3node-coding", "version": 3}]}
        expected = json.dumps(result, indent=2) + "\n"
        out = _no_ansi(lambda: cluster_template._emit_template(
            _Args("list", json=True), result))
        assert out == expected, "--json output must be byte-identical"


# ── gateway_restart.py __main__ result render (_emit_result) ───────────────

class TestNoAnsiGatewayRestart:
    """`python gateway_restart.py` human plain on non-tty, --json raw."""

    def test_success_human(self):
        out = _no_ansi(lambda: gateway_restart._emit_result(
            {"success": True, "note": "Gateway (ai.hermes.gateway) kicked successfully"}))
        assert "ai.hermes.gateway" in out
        assert "kicked successfully" in out

    def test_failure_human(self):
        out = _no_ansi(lambda: gateway_restart._emit_result(
            {"success": False, "note": "Gateway kick failed: boom"}))
        assert "boom" in out

    def test_json_byte_identical(self):
        result = {"success": True, "note": "Gateway kicked"}
        expected = json.dumps(result, indent=2, default=str) + "\n"
        out = _no_ansi(lambda: gateway_restart._emit_result(result, as_json=True))
        assert out == expected, "--json output must be byte-identical"


# ── anti-collapse width constant ───────────────────────────────────────────

def test_nontty_width_constant_used():
    """make_console defaults its width to the exported constant (assert the
    constant, never a literal value)."""
    assert _theme.DEFAULT_NONTTY_WIDTH == 200
    assert _theme.make_console().width == _theme.DEFAULT_NONTTY_WIDTH
