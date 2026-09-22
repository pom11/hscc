"""No-ANSI regression for `hscc cluster` / `hscc template` / `hscc profiles`.

The cluster/template group rendering was migrated from the raw ``_emit()``
``json.dumps`` blob to themed Rich Panel(s)/Table(s) (hscc_daemon/cluster_render.py).
This pins the two invariants the migration must not break:

  1. On a NON-tty stdout (pipe/redirect), Rich must degrade to PLAIN text with
     NO ANSI escape codes — so scripts that capture output still get clean text.
  2. The machine ``--json`` path (``template validate ... --json``) stays a
     byte-exact raw ``json.dumps`` dump.

The engine (hscc-cluster) is isolated here: the CLI is the only rendering
layer, so we stub the engine's ``cmd_*`` functions / ``cluster_template_cli``
to return representative result dicts and assert on the rendered output.
"""

import io
import sys
import types
from contextlib import redirect_stdout

import pytest

from hscc_daemon import hscc as hscc_mod

NO_ANSI = "\x1b["


# ── fake engine / template module ──────────────────────────────────────────

class _FakeEngine:
    """Record calls and return a representative result per cluster command."""

    def cmd_cluster_status(self):
        return {
            "workloads": [
                {"name": "@official/qwen3", "tp": 2, "pp": 1, "container_id": "abc123"},
                {"name": "@official/llama", "tp": 1, "pp": 1, "container_id": "def456"},
            ],
            "idle_hosts": ["10.0.0.3", "10.0.0.4"],
            "total_hosts": 4,
        }

    def cmd_hosts(self):
        return {
            "saved_clusters": {"success": True, "output": "hscc (default)\nother"},
            "live_status": {"success": True, "output": "1 container across 4 hosts"},
            "hosts": [
                {"name": "gw", "ip": "10.0.0.1", "role": "gateway"},
                {"name": "w1", "ip": "10.0.0.2", "role": "worker"},
            ],
        }

    def cmd_monitor(self):
        return {"success": True, "output": '{"cpu": 10, "ram": 50, "gpu": 20}',
                "json": {"cpu": 10, "ram": 50, "gpu": 20}}

    def cmd_jobs(self):
        return {"success": True, "returncode": 0,
                "output": "Job: @official/qwen3  [abc123]  (1 container(s))"}

    def cmd_info(self):
        return {
            "cluster_config": {"gateway": {"ip": "10.0.0.1"}, "workers": []},
            "saved_clusters": {},
            "default_cluster": {"output": "cluster hscc"},
            "cluster_files": {"hscc.yaml": "---\nname: hscc"},
        }

    def cmd_stop(self, cid):
        return {"success": True, "returncode": 0, "output": f"stopped {cid}"}

    def cmd_cluster_down(self, dry_run=False):
        return {"success": True, "dry_run": dry_run,
                "command": ["sparkrun", "stop", "--all"]}

    def cmd_cluster_up(self, dry_run=False):
        if dry_run:
            return {"success": True, "dry_run": True, "units": 3}
        return {"success": True, "dry_run": False, "units": 3,
                "issued": [{"success": True}, {"success": True}, {"success": True}]}

    def cmd_profile_status(self):
        return {"counts": {"backend-engineer": 3, "researcher-a": 2},
                "total_running": 5,
                "profiles": ["backend-engineer", "researcher-a"]}


_CLUSTER_CMDS = ["status", "hosts", "monitor", "jobs", "info", "down", "up"]


def _run_cluster(sub, monkeypatch, extra=None):
    eng = _FakeEngine()
    monkeypatch.setattr(hscc_mod, "_load_cluster_engine", lambda: eng)
    args = ["hscc", "cluster", sub] + (extra or [])
    monkeypatch.setattr(sys, "argv", args)
    out = io.StringIO()
    code = None
    try:
        with redirect_stdout(out):
            hscc_mod.main()
    except SystemExit as e:
        code = e.code
    return out.getvalue(), code


def _install_fake_template(monkeypatch, result):
    fake = types.ModuleType("cluster_template_cli")
    fake.cmd_cluster_template = lambda a: result
    monkeypatch.setitem(sys.modules, "cluster_template_cli", fake)
    if str(hscc_mod._resolve_cluster_dir()) not in sys.path:
        sys.path.insert(0, str(hscc_mod._resolve_cluster_dir()))


def _run_template(sub, monkeypatch, result, extra=None):
    _install_fake_template(monkeypatch, result)
    args = ["hscc", "template", sub] + (extra or [])
    monkeypatch.setattr(sys, "argv", args)
    out = io.StringIO()
    code = None
    try:
        with redirect_stdout(out):
            hscc_mod.main()
    except SystemExit as e:
        code = e.code
    return out.getvalue(), code


# ── cluster: no ANSI on non-tty stdout + still renders content ────────────

@pytest.mark.parametrize("sub", _CLUSTER_CMDS)
def test_cluster_no_ansi(sub, monkeypatch):
    text, code = _run_cluster(sub, monkeypatch)
    assert NO_ANSI not in text, f"cluster {sub} leaked ANSI: {text!r}"
    assert code == 0, f"cluster {sub} returned rc={code}: {text!r}"


def test_cluster_status_renders_workloads(monkeypatch):
    text, _ = _run_cluster("status", monkeypatch)
    assert "cluster status" in text
    assert "@official/qwen3" in text
    assert "abc123" in text
    # human view must NOT be a bare JSON blob of the raw result
    assert '"idle_hosts"' not in text


def test_cluster_dry_run_down_no_ansi(monkeypatch):
    text, code = _run_cluster("down", monkeypatch, extra=["--dry-run"])
    assert NO_ANSI not in text
    assert code == 0
    assert "dry-run" in text.lower()


def test_cluster_stop_with_id_no_ansi(monkeypatch):
    text, code = _run_cluster("stop", monkeypatch, extra=["abc123"])
    assert NO_ANSI not in text
    assert code == 0
    assert "abc123" in text


# ── template: no ANSI on non-tty stdout ───────────────────────────────────

def _template_results():
    return {
        "list": {"count": 2, "templates": [
            {"name": "3node-coding", "version": 3, "group": "", "description": "coding"},
            {"name": "lite", "version": 2, "group": "dev", "description": ""}]},
        "status": {"applied": "3node-coding",
                   "note": "applied at 12:00"},
        "preview": {"template": "3node-coding", "description": "coding",
                    "changes": [
                        {"file": "serving.json", "action": "write",
                         "summary": "3 units (1 orchestrator + 1 families)"},
                        {"file": "models.json", "action": "write",
                         "summary": "2 models registered"}],
                    "serve_delta": {"start": ["a", "b"], "stop": []}},
        "validate_ok": {"template": "3node-coding", "ok": True,
                        "structural": {"ok": True, "errors": [], "warnings": []},
                        "placement": {"ok": True, "errors": [], "warnings": []}},
        "validate_fail": {"template": "3node-coding", "ok": False,
                          "structural": {"ok": False, "errors": ["bad version"],
                                         "warnings": ["contention"]},
                          "placement": {"ok": True, "errors": [], "warnings": []}},
        "apply_ok": {"template": "3node-coding", "success": True,
                     "steps": [{"step": "serving.json", "status": "ok"},
                               {"step": "models.json", "status": "ok"}]},
        "apply_blocked": {"status": "blocked", "success": False,
                          "note": "Template is NOT deployable.",
                          "errors": ["bad layout"]},
    }


def test_template_list_no_ansi(monkeypatch):
    text, code = _run_template("list", monkeypatch,
                               _template_results()["list"])
    assert NO_ANSI not in text
    assert code == 0
    assert "3node-coding" in text
    assert "cluster templates" in text


@pytest.mark.parametrize("sub,key", [
    ("status", "status"),
    ("preview", "preview"),
    ("apply", "apply_ok"),
])
def test_template_no_ansi(sub, key, monkeypatch):
    text, code = _run_template(sub, monkeypatch, _template_results()[key])
    assert NO_ANSI not in text, f"template {sub} leaked ANSI: {text!r}"


def test_template_validate_human_no_ansi(monkeypatch):
    text, code = _run_template("validate", monkeypatch,
                               _template_results()["validate_ok"], extra=["X"])
    assert NO_ANSI not in text
    assert code == 0


def test_template_validate_fail_exits_nonzero_no_ansi(monkeypatch):
    text, code = _run_template("validate", monkeypatch,
                               _template_results()["validate_fail"], extra=["X"])
    assert NO_ANSI not in text
    assert code == 1
    assert "FAIL" in text


def test_template_apply_blocked_exits_nonzero_no_ansi(monkeypatch):
    text, code = _run_template("apply", monkeypatch,
                               _template_results()["apply_blocked"],
                               extra=["X", "--confirm"])
    assert NO_ANSI not in text
    assert code == 1


# ── --json byte-exact invariant (template validate) ───────────────────────

def test_template_validate_json_is_byte_exact(monkeypatch):
    import json
    result = _template_results()["validate_ok"]
    expected = json.dumps(result, indent=2, default=str) + "\n"
    text, code = _run_template("validate", monkeypatch, result,
                               extra=["X", "--json"])
    # The --json path is the machine contract: it must be a raw json.dumps
    # dump of the engine result — no Rich table/panel markup, no ANSI, no
    # theme decorations — byte-for-byte identical to _emit's output.
    assert text == expected
    assert code == 0


# ── profiles: no ANSI ─────────────────────────────────────────────────────

def test_profiles_no_ansi(monkeypatch):
    eng = _FakeEngine()
    monkeypatch.setattr(hscc_mod, "_load_cluster_engine", lambda: eng)
    monkeypatch.setattr(sys, "argv", ["hscc", "profiles"])
    out = io.StringIO()
    code = None
    with redirect_stdout(out):
        try:
            hscc_mod.main()
        except SystemExit as e:
            code = e.code
    text = out.getvalue()
    assert NO_ANSI not in text
    assert code == 0
    assert "backend-engineer" in text
    assert "researcher-a" in text
    # real shape carries counts + total_running (profile status engine)
    assert "3" in text
    assert "5 running task(s)" in text
