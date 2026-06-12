"""Tests for cluster_template_cli.py — CLI subcommand routing."""

import pytest
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from cluster_template_cli import cmd_cluster_template


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
        assert "apply <name> [--confirm]" in subcommands

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
        assert result["count"] >= 4  # basic-1 through basic-4 node templates

    def test_list_returns_templates(self):
        result = cmd_cluster_template(["list"])
        assert "templates" in result
        assert isinstance(result["templates"], list)
        assert len(result["templates"]) == result["count"]

    def test_list_template_structure(self):
        result = cmd_cluster_template(["list"])
        # Each template entry has name, version, cluster_size, description
        first = result["templates"][0]
        assert "name" in first
        assert "version" in first
        assert "cluster_size" in first
        assert "description" in first

    def test_list_includes_basic_1_node(self):
        result = cmd_cluster_template(["list"])
        names = [t["name"] for t in result["templates"]]
        assert "basic-1-node" in names


class TestCmdClusterTemplatePreview:
    """Test the 'preview' subcommand."""

    def test_preview_valid_template(self):
        result = cmd_cluster_template(["preview", "basic-1-node"])
        assert "template" in result
        assert result["template"] == "basic-1-node"
        assert "cluster_size" in result
        assert "description" in result
        assert "changes" in result
        assert isinstance(result["changes"], list)
        assert len(result["changes"]) >= 1

    def test_preview_includes_change_entries(self):
        result = cmd_cluster_template(["preview", "basic-1-node"])
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

    def test_preview_no_provisioning_in_changes(self):
        """Preview should include a provisioning change entry."""
        result = cmd_cluster_template(["preview", "basic-1-node"])
        actions = [c["action"] for c in result["changes"]]
        assert "provision" in actions


class TestCmdClusterTemplateApply:
    """Test the 'apply' subcommand."""

    def test_apply_without_confirm_returns_preview(self):
        """Without --confirm, apply returns a preview status."""
        result = cmd_cluster_template(["apply", "basic-1-node"])
        assert result["status"] == "preview"
        assert "Re-call with confirm=true" in result["note"]
        assert "changes" in result

    def test_apply_without_confirm_includes_plan(self):
        result = cmd_cluster_template(["apply", "basic-1-node"])
        changes = result["changes"]
        assert "template" in changes
        assert "changes" in changes

    def test_apply_missing_name_returns_error(self):
        result = cmd_cluster_template(["apply"])
        assert "error" in result
        assert "Missing template name" in result["error"]

    def test_apply_nonexistent_returns_error(self):
        result = cmd_cluster_template(["apply", "nonexistent"])
        assert "error" in result
        assert "not found" in result["error"].lower()



class TestStatusAndValidateCommands:
    def test_status_subcommand(self):
        result = cmd_cluster_template(["status"])
        assert "applied" in result

    def test_validate_subcommand_good(self):
        result = cmd_cluster_template(["validate", "hscc-live"])
        assert result["ok"] is True

    def test_validate_subcommand_bad(self):
        result = cmd_cluster_template(["validate", "multi-family-4-node"])
        assert result["ok"] is False
        assert result["errors"]

    def test_validate_missing_name(self):
        result = cmd_cluster_template(["validate"])
        assert "error" in result

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
