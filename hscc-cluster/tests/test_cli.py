"""Tests for cluster_template_cli.py — CLI subcommand routing."""

import pytest
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from cluster_template_cli import cmd_cluster_template
import cluster_template
import template_intent as ti
import recipe_cost as rc
from dataclasses import dataclass


@dataclass
class _N:
    ip: str
    vram_free_gb: float = 120.0


@dataclass
class _T:
    orchestrator: _N
    workers: list


@pytest.fixture
def stub_cluster(monkeypatch):
    """Stub discovery + recipe cost + recipe-exists so CLI preview/apply/validate
    resolve against a fake cluster without live sparkrun or real recipe files."""
    monkeypatch.setattr(cluster_template, "_discover",
                        lambda probe=False: _T(_N("10.0.0.1"), [_N("10.0.0.2"), _N("10.0.0.3")]))
    monkeypatch.setattr(ti._rc, "recipe_cost",
                        lambda r: rc.RecipeCost(r, per_gpu_total_gb=30, fits=True))
    monkeypatch.setattr(cluster_template.Path, "is_file", lambda self: True)


class TestCmdClusterTemplateMissingSubcommand:
    """Test that calling cmd_cluster_template with no args returns usage help."""

    def test_empty_args_returns_error(self):
        result = cmd_cluster_template([])
        assert "error" in result
        assert result["error"] == "Missing subcommand"

    def test_empty_args_returns_usage(self):
        result = cmd_cluster_template([])
        assert "usage" in result
        assert "cluster-template" in result["usage"]

    def test_empty_args_returns_subcommands(self):
        result = cmd_cluster_template([])
        assert "subcommands" in result
        subcommands = result["subcommands"]
        assert "list" in subcommands
        assert "preview <name>" in subcommands
        assert any("apply <name>" in k and "--confirm" in k for k in subcommands)

    def test_unknown_subcommand_returns_error(self):
        result = cmd_cluster_template(["bogus"])
        assert "error" in result
        assert "Unknown subcommand" in result["error"]


class TestCmdClusterTemplateList:
    """Test the 'list' subcommand."""

    def test_list_returns_count(self):
        result = cmd_cluster_template(["list"])
        assert "count" in result
        assert isinstance(result["count"], int)
        assert result["count"] >= 2  # single-family + colocated-two-models

    def test_list_returns_templates(self):
        result = cmd_cluster_template(["list"])
        assert isinstance(result["templates"], list)
        assert len(result["templates"]) == result["count"]

    def test_list_template_structure(self):
        first = cmd_cluster_template(["list"])["templates"][0]
        assert "name" in first and "version" in first and "description" in first

    def test_list_includes_single_family(self):
        names = [t["name"] for t in cmd_cluster_template(["list"])["templates"]]
        assert "single-family" in names


class TestCmdClusterTemplatePreview:
    """Test the 'preview' subcommand."""

    def test_preview_valid_template(self, stub_cluster):
        result = cmd_cluster_template(["preview", "single-family"])
        assert result["template"] == "single-family"
        assert "description" in result
        assert isinstance(result["changes"], list) and result["changes"]

    def test_preview_includes_change_entries(self, stub_cluster):
        result = cmd_cluster_template(["preview", "single-family"])
        file_actions = [c["file"] for c in result["changes"]]
        assert "serving.json" in file_actions
        assert "models.json" in file_actions
        assert "config.yaml" in file_actions

    def test_preview_nonexistent_returns_error(self):
        result = cmd_cluster_template(["preview", "nonexistent"])
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_preview_missing_name_returns_error(self):
        result = cmd_cluster_template(["preview"])
        assert "error" in result
        assert "Missing template name" in result["error"]
        assert "usage" in result

    def test_preview_has_provision_entry(self, stub_cluster):
        result = cmd_cluster_template(["preview", "single-family"])
        actions = [c["action"] for c in result["changes"]]
        assert "provision" in actions


class TestCmdClusterTemplateApply:
    """Test the 'apply' subcommand."""

    def test_apply_without_confirm_returns_preview(self, stub_cluster):
        result = cmd_cluster_template(["apply", "single-family"])
        assert result["status"] == "preview"
        assert "Re-call with confirm=true" in result["note"]
        assert "changes" in result

    def test_apply_without_confirm_includes_plan(self, stub_cluster):
        result = cmd_cluster_template(["apply", "single-family"])
        changes = result["changes"]
        assert "template" in changes and "changes" in changes

    def test_apply_missing_name_returns_error(self):
        result = cmd_cluster_template(["apply"])
        assert "error" in result
        assert "Missing template name" in result["error"]

    def test_apply_nonexistent_returns_error(self):
        # A nonexistent template flows through the SAME gate as `template
        # validate` — it is a structural failure, so apply blocks rather than
        # raising, and reports the not-found error in the unified shape. This
        # is deliberate (T5): apply and validate share one implementation, so
        # they agree on a missing template exactly as on any other.
        result = cmd_cluster_template(["apply", "nonexistent"])
        assert result["status"] == "blocked"
        assert result["success"] is False
        combined = result["errors"] + result["validation"]["structural"]["errors"]
        assert any("not found" in e.lower() for e in combined)


class TestStatusAndValidateCommands:
    def test_status_subcommand(self):
        result = cmd_cluster_template(["status"])
        assert "applied" in result

    def test_validate_subcommand_good(self, stub_cluster):
        result = cmd_cluster_template(["validate", "single-family"])
        assert result["ok"] is True

    def test_validate_missing_name(self):
        result = cmd_cluster_template(["validate"])
        assert "error" in result


# ── hscc orch inject (t_75f17c83: thin CLI verb over hscc-api/repl_inject) ──
#
# `import hscc` resolves through this suite's conftest (plugin dir on
# sys.path) — the same engine module object test_no_ansi.py uses; the alias
# just keeps these tests readable.

class _FakeInjectError(Exception):
    """Stand-in for repl_inject.InjectError (kind + message)."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind, self.message = kind, message


class FakeReplModule:
    """Stub of the repl_inject MODULE for the CLI verb — no tmux, ever.

    Records the inject() calls so the tests can pin WHAT the CLI passes
    through (mode/dry_run/pane_override) and prove `--dry-run` sends nothing;
    busy/verb follow the parity contract (VERB_PREFIX table in repl_inject)
    so the assertions read like the real receipt.
    """

    def __init__(self, busy=False, fail=None):
        self.calls = []
        self.busy = busy
        self.fail = fail                       # exception inject() raises

    def inject(self, profile, text, mode="auto", pane_override=None,
               dry_run=False, sleep_fn=None):
        # Mirrors real deliver(): refuse empty, rstrip newlines, then decide.
        if not text or not text.strip():
            raise _FakeInjectError("empty", "empty message")
        text = text.rstrip("\n")
        self.calls.append({"profile": profile, "text": text, "mode": mode,
                           "pane_override": pane_override, "dry_run": dry_run})
        if self.fail:
            raise self.fail
        eff = "queue" if (mode == "auto" and self.busy) else \
            ("plain" if mode in ("auto", "interrupt") else mode)
        verb = {"plain": "", "queue": "/queue ", "steer": "/steer "}[eff]
        base = {"pane": "%7", "target": "s1:0.1", "profile": profile,
                "busy": self.busy, "mode": eff, "verb": verb,
                "interrupts": eff == "plain" and self.busy}
        if dry_run:
            return {**base, "dry_run": True, "text": text, "chars": len(text)}
        return {**base, "sent_text": (verb + text) if verb else text}

    @staticmethod
    def format_plan(p):
        line = (f"target: {p['profile']} @ {p['target']} "
                f"({'busy' if p['busy'] else 'idle'}) -> {p['mode']}")
        if p.get("interrupts"):
            line += " (interrupts the running turn)"
        return line


import hscc as cluster_hscc_engine  # noqa: E402


class TestOrchInjectVerb:
    def test_registered_in_commands(self):
        assert cluster_hscc_engine.COMMANDS["orch-inject"] is cluster_hscc_engine.cmd_orch_inject
        assert cluster_hscc_engine.COMMANDS["inject"] is cluster_hscc_engine.cmd_orch_inject
        assert cluster_hscc_engine.COMMANDS["orch"] is cluster_hscc_engine.cmd_orch

    def test_orch_group_routes_inject(self, monkeypatch):
        seen = {}
        def spy(argv=None, repl=None):
            seen["argv"] = argv
            return {"ok": True}
        monkeypatch.setattr(cluster_hscc_engine, "cmd_orch_inject", spy)
        cluster_hscc_engine.cmd_orch(["inject", "demo-orch", "hello"])
        assert seen["argv"] == ["demo-orch", "hello"]
        with pytest.raises(ValueError, match="unknown orch subcommand"):
            cluster_hscc_engine.cmd_orch(["nope"])
        with pytest.raises(ValueError, match="unknown orch subcommand"):
            cluster_hscc_engine.cmd_orch([])

    def test_dry_run_reports_and_sends_nothing(self):
        fake = FakeReplModule(busy=False)
        r = cluster_hscc_engine.cmd_orch_inject(
            ["demo-orch", "hi there", "--dry-run"], repl=fake)
        assert r["dry_run"] is True and r["mode"] == "plain" and r["chars"] == 8
        assert r["line"] == "target: demo-orch @ s1:0.1 (idle) -> plain"
        assert fake.calls == [{"profile": "demo-orch", "text": "hi there",
                               "mode": "auto", "pane_override": None,
                               "dry_run": True}]

    def test_dry_run_busy_auto_queues(self):
        fake = FakeReplModule(busy=True)
        r = cluster_hscc_engine.cmd_orch_inject(
            ["demo-orch", "ping", "--dry-run"], repl=fake)
        assert r["mode"] == "queue" and r["verb"] == "/queue " and r["interrupts"] is False
        assert "(busy) -> queue" in r["line"]

    def test_explicit_mode_passed_straight_through(self):
        fake = FakeReplModule(busy=True)
        r = cluster_hscc_engine.cmd_orch_inject(
            ["demo-orch", "msg", "--mode", "steer"], repl=fake)
        assert fake.calls[0]["mode"] == "steer" and fake.calls[0]["dry_run"] is False
        assert r["mode"] == "steer" and r["sent_text"] == "/steer msg"

    def test_real_send_receipt_carries_the_json_fields(self):
        # Card contract: --json prints the receipt (pane, idle/busy, verb, chars).
        fake = FakeReplModule(busy=False)
        r = cluster_hscc_engine.cmd_orch_inject(["demo-orch", "hello"], repl=fake)
        assert r["pane"] == "%7" and r["busy"] is False
        assert r["verb"] == "" and r["chars"] == 5
        # The dry-run path keeps inject()'s own (rstripped) chars, not ours.
        fake2 = FakeReplModule(busy=False)
        r2 = cluster_hscc_engine.cmd_orch_inject(
            ["demo-orch", "hi\n\n", "--dry-run"], repl=fake2)
        assert r2["chars"] == 2

    def test_busy_explicit_plain_reports_interrupts(self):
        fake = FakeReplModule(busy=True)
        r = cluster_hscc_engine.cmd_orch_inject(
            ["demo-orch", "stop", "--mode", "plain"], repl=fake)
        assert r["mode"] == "plain" and r["verb"] == ""
        # The one combination that cuts off a running turn must be reported.
        assert r["interrupts"] is True

    def test_legacy_alias_interrupt_accepted(self):
        fake = FakeReplModule()
        r = cluster_hscc_engine.cmd_orch_inject(
            ["demo-orch", "x", "--mode", "interrupt"], repl=fake)
        assert fake.calls[0]["mode"] == "interrupt"    # NOT rewritten by the CLI
        assert r["mode"] == "plain"                    # repl_inject canonicalises

    def test_pane_override_and_json_flag(self):
        fake = FakeReplModule()
        r = cluster_hscc_engine.cmd_orch_inject(
            ["demo-orch", "x", "--pane", "%7", "--json"], repl=fake)
        assert fake.calls[0]["pane_override"] == "%7"
        assert r["pane"] == "%7" and r["target"] == "s1:0.1"

    def test_missing_args_refuse_without_sending(self):
        fake = FakeReplModule()
        with pytest.raises(ValueError, match="required"):
            cluster_hscc_engine.cmd_orch_inject(["only-profile"], repl=fake)
        with pytest.raises(ValueError, match="required"):
            cluster_hscc_engine.cmd_orch_inject([], repl=fake)
        assert fake.calls == []

    def test_unknown_mode_refusal_surfaces_from_wrapper(self):
        fake = FakeReplModule(fail=_FakeInjectError("empty", "unknown mode 'teleport'"))
        with pytest.raises(_FakeInjectError):
            cluster_hscc_engine.cmd_orch_inject(
                ["demo-orch", "x", "--mode", "teleport"], repl=fake)

    def test_module_unavailable_is_a_clear_error(self, monkeypatch):
        monkeypatch.setattr(cluster_hscc_engine, "_load_repl_inject", lambda: None)
        with pytest.raises(RuntimeError, match="repl_inject not importable"):
            cluster_hscc_engine.cmd_orch_inject(["p", "m"])

    def test_loader_finds_the_sibling_hscc_api_module(self):
        repl = cluster_hscc_engine._load_repl_inject()
        assert repl is not None and repl.__file__.endswith("hscc-api/repl_inject.py")
        assert repl.MODES == ("auto", "plain", "queue", "steer")

# ── Fix 4: CLI argv passes sys.argv[1:] ──────────────────────────────────────

class TestCLIArgvNoScriptName:
    """Fix 4: __main__ passes sys.argv[1:] (not sys.argv) to cmd_cluster_template,
    so the script name is NOT passed as args[0] (which caused 'Unknown subcommand:
    cluster_template_cli.py')."""

    def test_argv_without_script_name(self):
        """When called with ['list', '--json'], the subcommand is 'list' not 'cluster_template_cli.py'."""
        # Simulate the __main__ path: subprocess calls this with [script, 'list'].
        # `--json` selects the machine path (the default human path is themed and
        # no longer JSON), so pass it to keep the JSON contract this test pins.
        import subprocess
        result = subprocess.run(
            ["python", "hscc-cluster/cluster_template_cli.py", "list", "--json"],
            capture_output=True, text=True, cwd="/Users/desac/dev/hscc",
            timeout=30,
        )
        data = __import__("json").loads(result.stdout)
        assert "error" not in data, f"Got error: {data.get('error')}"
        assert "count" in data and data["count"] >= 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
