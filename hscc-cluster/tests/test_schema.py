"""Tests for cluster_template_schema.py — Pydantic schema validation."""

import pytest
import tempfile
import yaml
from pathlib import Path
import sys

PLUGIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from cluster_template_schema import (
    ClusterTemplate,
    ModelSpec,
    WorkerFamily,
    FamilyProxyConfig,
    TemplateRegistry,
    load_template,
    list_templates,
)


class TestModelSpec:
    """Test ModelSpec schema."""

    def test_minimal(self):
        m = ModelSpec(recipe="test.yaml")
        assert m.recipe == "test.yaml"
        assert m.tp == 1
        assert m.pp == 1

    def test_with_tp_pp(self):
        m = ModelSpec(recipe="test.yaml", tp=2, pp=1)
        assert m.tp == 2

    def test_invalid_tp(self):
        with pytest.raises(ValueError):
            ModelSpec(recipe="test.yaml", tp=0)

    def test_invalid_gpu_util(self):
        with pytest.raises(ValueError):
            ModelSpec(recipe="test.yaml", gpu_memory_util=1.5)


class TestFamilyProxyConfig:
    """Test FamilyProxyConfig schema."""

    def test_defaults(self):
        p = FamilyProxyConfig(port=8000)
        assert p.port == 8000
        assert p.host == "0.0.0.0"

    def test_custom_port(self):
        p = FamilyProxyConfig(port=4001)
        assert p.port == 4001

    def test_invalid_port_low(self):
        with pytest.raises(ValueError):
            FamilyProxyConfig(port=0)

    def test_extra_args(self):
        p = FamilyProxyConfig(extra_args={"max-model-len": "32768"})
        assert p.extra_args == {"max-model-len": "32768"}


class TestWorkerFamily:
    """Test WorkerFamily schema."""

    def test_valid(self):
        f = WorkerFamily(
            name="coding",
            models=[ModelSpec(recipe="test.yaml")],
            nodes=["192.168.88.246"],
        )
        assert f.name == "coding"
        assert len(f.models) == 1
        assert f.nodes == ["192.168.88.246"]


class TestClusterTemplate:
    """Test ClusterTemplate schema."""

    def test_valid_4_node(self):
        tpl = ClusterTemplate(
            name="test",
            cluster_size=4,
            orchestrator=ModelSpec(recipe="orch.yaml"),
            orchestrator_node="192.168.88.244",
            families=[
                WorkerFamily(
                    name="coding",
                    models=[ModelSpec(recipe="coding.yaml")],
                    nodes=["192.168.88.246", "192.168.88.247"],
                ),
                WorkerFamily(
                    name="vision",
                    models=[ModelSpec(recipe="vision.yaml")],
                    nodes=["192.168.88.248"],
                ),
            ],
        )
        assert tpl.total_nodes == 4
        assert len(tpl.families) == 2

    def test_cluster_size_mismatch(self):
        with pytest.raises(ValueError, match="cluster_size.*node count"):
            ClusterTemplate(
                name="test",
                cluster_size=5,
                orchestrator=ModelSpec(recipe="orch.yaml"),
                orchestrator_node="192.168.88.244",
                families=[
                    WorkerFamily(
                        name="coding",
                        models=[ModelSpec(recipe="coding.yaml")],
                        nodes=["192.168.88.246", "192.168.88.247"],
                    ),
                ],
            )

    def test_all_worker_ips(self):
        tpl = ClusterTemplate(
            name="test",
            cluster_size=3,
            orchestrator=ModelSpec(recipe="orch.yaml"),
            orchestrator_node="192.168.88.244",
            families=[
                WorkerFamily(
                    name="a",
                    models=[ModelSpec(recipe="a.yaml")],
                    nodes=["192.168.88.246", "192.168.88.247"],
                ),
            ],
        )
        assert tpl.all_worker_ips == ["192.168.88.246", "192.168.88.247"]


class TestToServingJson:
    """Test ClusterTemplate.to_serving_json()."""

    def test_single_family(self):
        tpl = ClusterTemplate(
            name="test",
            cluster_size=2,
            orchestrator=ModelSpec(recipe="/path/to/orch.yaml"),
            orchestrator_node="192.168.88.244",
            families=[
                WorkerFamily(
                    name="coding",
                    models=[ModelSpec(recipe="/path/to/coding.yaml")],
                    nodes=["192.168.88.246"],
                ),
            ],
        )
        result = tpl.to_serving_json()
        assert result["version"] == 1
        assert len(result["units"]) == 2
        assert result["units"][0]["role"] == "orchestrator"
        assert result["units"][1]["role"] == "worker"
        assert result["units"][1]["family"] == "coding"

    def test_multi_family(self):
        tpl = ClusterTemplate(
            name="test",
            cluster_size=4,
            orchestrator=ModelSpec(recipe="/path/to/orch.yaml"),
            orchestrator_node="192.168.88.244",
            families=[
                WorkerFamily(
                    name="coding",
                    models=[ModelSpec(recipe="/path/to/a.yaml")],
                    nodes=["192.168.88.246", "192.168.88.247"],
                ),
                WorkerFamily(
                    name="vision",
                    models=[
                        ModelSpec(recipe="/path/to/v.yaml"),
                        ModelSpec(recipe="/path/to/w.yaml"),
                    ],
                    nodes=["192.168.88.248"],
                ),
            ],
        )
        result = tpl.to_serving_json()
        assert len(result["units"]) == 4  # 1 orch + 1 + 2 models


class TestLoadTemplate:
    """Test loading templates from YAML files."""

    def test_load_valid(self, tmp_path):
        yaml_content = {
            "name": "test",
            "cluster_size": 2,
            "orchestrator": {"recipe": "orch.yaml", "tp": 1, "pp": 1},
            "orchestrator_node": "192.168.88.244",
            "families": [
                {
                    "name": "coding",
                    "models": [{"recipe": "coding.yaml", "tp": 1}],
                    "nodes": ["192.168.88.246"],
                }
            ],
        }
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml.dump(yaml_content))

        tpl = load_template(yaml_file)
        assert tpl.name == "test"
        assert tpl.cluster_size == 2
        assert len(tpl.families) == 1
        assert tpl.families[0].name == "coding"

    def test_load_missing_file(self):
        with pytest.raises(FileNotFoundError):
            load_template("/nonexistent/template.yaml")


class TestListTemplates:
    """Test scanning templates directory."""

    def test_list_empty(self, tmp_path):
        registry = list_templates(tmp_path)
        assert len(registry.templates) == 0

    def test_list_one(self, tmp_path):
        yaml_content = {
            "name": "my-template",
            "cluster_size": 2,
            "description": "A test template",
            "orchestrator": {"recipe": "orch.yaml"},
            "orchestrator_node": "192.168.88.244",
            "families": [
                {
                    "name": "coding",
                    "models": [{"recipe": "m.yaml"}],
                    "nodes": ["192.168.88.246"],
                }
            ],
        }
        (tmp_path / "my-template.yaml").write_text(yaml.dump(yaml_content))

        registry = list_templates(tmp_path)
        assert len(registry.templates) == 1
        assert registry.templates[0]["name"] == "my-template"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
