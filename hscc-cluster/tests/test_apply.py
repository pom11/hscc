"""Tests for cluster_template.py — apply pipeline."""

import pytest
import json
from pathlib import Path
import sys

PLUGIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from cluster_template import (
    preview_template,
    apply_template,
    write_json,
    atomic_yaml_update,
)
from cluster_template_schema import list_templates


class TestListTemplates:
    """Test template listing."""

    def test_list_built_in(self):
        """Should find built-in templates."""
        registry = list_templates(PLUGIN_DIR / "templates")
        assert len(registry.templates) >= 4  # basic-1 through basic-4

    def test_registry_structure(self):
        registry = list_templates(PLUGIN_DIR / "templates")
        tpl = registry.templates[0]
        assert "name" in tpl
        assert "version" in tpl
        assert "cluster_size" in tpl


class TestWriteJson:
    """Test atomic JSON writes."""

    def test_write_and_read(self, tmp_path):
        data = {"key": "value", "nested": {"a": 1}}
        write_json(tmp_path / "test.json", data)
        
        with open(tmp_path / "test.json") as f:
            result = json.load(f)
        assert result == data

    def test_backup_on_overwrite(self, tmp_path):
        data1 = {"version": 1}
        data2 = {"version": 2}
        
        write_json(tmp_path / "test.json", data1)
        write_json(tmp_path / "test.json", data2, backup=True)
        
        # Check backup exists
        backups = list(tmp_path.glob("test.json.bak.*"))
        assert len(backups) == 1

    def test_atomic_write_no_partial(self, tmp_path):
        """Temp file should not persist after write."""
        write_json(tmp_path / "test.json", {"ok": True})
        assert not (tmp_path / "test.json.tmp").exists()


class TestAtomicYamlUpdate:
    """Test atomic YAML file updates."""

    def test_create_new(self, tmp_path):
        data = {"new": "value"}
        path = atomic_yaml_update(tmp_path / "test.yaml", lambda d: data)
        
        import yaml
        with open(path) as f:
            result = yaml.safe_load(f)
        assert result == data

    def test_update_existing(self, tmp_path):
        import yaml
        path = tmp_path / "test.yaml"
        with open(path, "w") as f:
            yaml.dump({"old": "value"}, f)
        
        path = atomic_yaml_update(path, lambda d: {**d, "new": "value"})
        
        with open(path) as f:
            result = yaml.safe_load(f)
        assert result["old"] == "value"
        assert result["new"] == "value"


class TestPreviewTemplate:
    """Test preview (dry-run) without writing files."""

    def test_preview_basic_1_node(self):
        result = preview_template("basic-1-node")
        
        assert result["template"] == "basic-1-node"
        assert result["cluster_size"] == 1
        assert len(result["changes"]) > 0
        
        # Check change structure
        change_files = [c["file"] for c in result["changes"]]
        assert "serving.json" in change_files
        assert "models.json" in change_files

    def test_preview_does_not_write(self):
        """Preview must not modify any files."""
        result = preview_template("basic-1-node")
        assert "changes" in result

    def test_preview_multi_family(self):
        result = preview_template("multi-family-4-node")
        
        assert result["cluster_size"] == 4
        assert len(result["changes"]) > 0

    def test_preview_structure(self):
        result = preview_template("basic-2-node")
        
        # All changes should have file, action, summary
        for change in result["changes"]:
            assert "file" in change
            assert "action" in change
            assert "summary" in change


class TestApplyTemplate:
    """Test apply (dry-run mode by default)."""

    def test_apply_without_confirm_returns_preview(self):
        result = apply_template("basic-1-node")
        
        assert result["status"] == "preview"
        assert "Re-call with confirm=true" in result["note"]
        assert "changes" in result

    def test_apply_basic_structure(self):
        """Apply with confirm=True returns full step list."""
        result = apply_template("multi-family-4-node", confirm=True)
        
        assert result["template"] == "multi-family-4-node"
        assert "steps" in result
        assert isinstance(result["steps"], list)
        assert len(result["steps"]) >= 5  # serving.json, models.json, config.yaml, proxies, provision


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
