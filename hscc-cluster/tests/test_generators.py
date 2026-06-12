"""Tests for cluster_template.py — config generation functions."""

import pytest
import tempfile
import json
import os
from pathlib import Path
import sys

PLUGIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from cluster_template import (
    _build_models_json,
    _build_proxy_config,
    _extract_model_name,
    _update_hermes_config,
)
from cluster_template_schema import (
    ClusterTemplate,
    ModelSpec,
    WorkerFamily,
    FamilyProxyConfig,
)


class TestExtractModelName:
    """Test model name extraction from recipe paths."""

    def test_simple_path(self):
        assert _extract_model_name("/path/to/recipe.yaml") == "recipe"

    def test_complex_path(self):
        result = _extract_model_name("~/.sparkrun-local/recipes/local-fixed/qwen3.6-27b-fp8-vllm.yaml")
        assert "fp8" in result  # should extract something useful

    def test_yaml_suffix(self):
        assert _extract_model_name("/path/to/model.yml") == "model"


class TestBuildModelsJson:
    """Test models.json generation."""

    def test_orchestrator_only(self):
        tpl = ClusterTemplate(
            name="test",
            cluster_size=1,
            orchestrator=ModelSpec(recipe="/path/to/orch.yaml"),
            orchestrator_node="192.168.88.244",
        )
        result = _build_models_json(tpl)

        assert result["provider"] == "custom"
        assert result["base_url"] == "http://192.168.88.244:8000/v1"
        assert len(result["models"]) == 1
        assert result["models"][0]["family"] == "orchestrator"

    def test_with_families(self):
        tpl = ClusterTemplate(
            name="test",
            cluster_size=2,
            orchestrator=ModelSpec(recipe="/path/to/orch.yaml"),
            orchestrator_node="192.168.88.244",
            families=[
                WorkerFamily(
                    name="coding",
                    models=[
                        ModelSpec(recipe="/path/to/m1.yaml", tp=2),
                        ModelSpec(recipe="/path/to/m2.yaml", tp=1),
                    ],
                    nodes=["192.168.88.246"],
                    proxy=FamilyProxyConfig(port=4001),
                ),
            ],
        )
        result = _build_models_json(tpl)

        assert len(result["models"]) == 3  # 1 orch + 2 models
        assert result["models"][1]["family"] == "coding"
        assert result["models"][1]["tp"] == 2
        assert result["models"][2]["tp"] == 1


class TestBuildProxyConfig:
    """Test LiteLLM proxy config generation."""

    def test_basic(self):
        family = WorkerFamily(
            name="coding",
            models=[ModelSpec(recipe="/path/to/m.yaml")],
            nodes=["192.168.88.246"],
            proxy=FamilyProxyConfig(port=4001),
        )
        result = _build_proxy_config(family)

        assert "model" in result
        assert "litellm_settings" in result
        assert "general_settings" in result
        assert len(result["serving_model_configs"]) == 1
        assert result["proxy_params"]["port"] == 4001

    def test_extra_args(self):
        family = WorkerFamily(
            name="coding",
            models=[ModelSpec(recipe="/path/to/m.yaml")],
            nodes=["192.168.88.246"],
            proxy=FamilyProxyConfig(port=4001, extra_args={"max-model-len": "32768"}),
        )
        result = _build_proxy_config(family)
        assert result["proxy_params"]["extra_args"] == {"max-model-len": "32768"}


class TestUpdateHermesConfig:
    """Test Hermes config.yaml updates."""

    def test_empty_config(self):
        config = {}
        tpl = ClusterTemplate(
            name="test",
            cluster_size=1,
            orchestrator=ModelSpec(recipe="/path/to/orch.yaml"),
            orchestrator_node="192.168.88.244",
        )
        result = _update_hermes_config(config, tpl)

        assert "providers" in result
        assert len(result["providers"]) == 1
        assert result["providers"][0]["name"] == "custom"

    def test_with_families(self):
        config = {}
        tpl = ClusterTemplate(
            name="test",
            cluster_size=3,
            orchestrator=ModelSpec(recipe="/path/to/orch.yaml"),
            orchestrator_node="192.168.88.244",
            families=[
                WorkerFamily(
                    name="coding",
                    models=[ModelSpec(recipe="/path/to/m.yaml")],
                    nodes=["192.168.88.246"],
                    proxy=FamilyProxyConfig(port=4001),
                ),
                WorkerFamily(
                    name="vision",
                    models=[ModelSpec(recipe="/path/to/m2.yaml")],
                    nodes=["192.168.88.247"],
                    proxy=FamilyProxyConfig(port=4002),
                ),
            ],
        )
        result = _update_hermes_config(config, tpl)

        assert len(result["providers"]) == 3  # orchestrator + 2 families
        family_names = {p["name"] for p in result["providers"]}
        assert "custom" in family_names
        assert "family-coding" in family_names
        assert "family-vision" in family_names


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
