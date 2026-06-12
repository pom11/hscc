"""Tests for hscc.py cluster-template CLI integration."""

import pytest
import json
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_DIR))


class TestClusterTemplateCli:
    """Test the cluster-template subcommand routing."""

    def test_missing_subcommand(self):
        from cluster_template_cli import cmd_cluster_template
        result = cmd_cluster_template([])
        
        assert "error" in result
        assert "Missing subcommand" in result["error"]
        assert "list" in result["subcommands"]

    def test_list_command(self):
        from cluster_template_cli import cmd_cluster_template
        result = cmd_cluster_template(["list"])
        
        assert "count" in result
        assert isinstance(result["count"], int)
        assert result["count"] >= 4  # built-in templates

    def test_preview_invalid_name(self):
        from cluster_template_cli import cmd_cluster_template
        result = cmd_cluster_template(["preview", "nonexistent-template-xyz"])
        
        assert "error" in result
        assert "not found" in result["error"].lower() or "FileNotFoundError" in str(result.get("error", ""))

    def test_apply_no_confirm(self):
        from cluster_template_cli import cmd_cluster_template
        result = cmd_cluster_template(["apply", "basic-1-node"])
        
        assert result["status"] == "preview"
        assert "Re-call with confirm" in result["note"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
