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

# ── Fix 4: CLI argv passes sys.argv[1:] ──────────────────────────────────────

class TestCLIArgvNoScriptName:
    """Fix 4: __main__ passes sys.argv[1:] (not sys.argv) to cmd_cluster_template,
    so the script name is NOT passed as args[0] (which caused 'Unknown subcommand:
    cluster_template_cli.py')."""

    def test_argv_without_script_name(self):
        """When called with ['list'], the subcommand is 'list' not 'cluster_template_cli.py'."""
        # Simulate the __main__ path: subprocess calls this with [script, 'list']
        import subprocess
        result = subprocess.run(
            ["python", "hscc-cluster/cluster_template_cli.py", "list"],
            capture_output=True, text=True, cwd="/Users/desac/dev/hscc",
            timeout=30,
        )
        data = __import__("json").loads(result.stdout)
        assert "error" not in data, f"Got error: {data.get('error')}"
        assert "count" in data and data["count"] >= 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
