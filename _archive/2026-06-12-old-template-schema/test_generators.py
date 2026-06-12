"""Tests for cluster_template.py — config generation functions."""

import pytest
import json
import tempfile
from pathlib import Path
import sys

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

# Import from module files directly
import cluster_template
_extract_model_name = cluster_template._extract_model_name
_build_models_json = cluster_template._build_models_json
_build_proxy_config = cluster_template._build_proxy_config
_update_hermes_config = cluster_template._update_hermes_config

# Import schema classes
import cluster_template_schema
ClusterTemplate = cluster_template_schema.ClusterTemplate
ModelSpec = cluster_template_schema.ModelSpec
WorkerFamily = cluster_template_schema.WorkerFamily
FamilyProxyConfig = cluster_template_schema.FamilyProxyConfig


class TestExtractModelName:
    """Test model name extraction from recipe paths."""

    def test_simple_path_fallback_to_stem(self):
        # Nonexistent recipe -> fall back to filename stem.
        assert _extract_model_name("/path/to/recipe.yaml") == "recipe"

    def test_yaml_suffix_fallback_to_stem(self):
        assert _extract_model_name("/path/to/model.yml") == "model"

    def test_reads_model_field_from_real_recipe(self):
        # A real recipe resolves to its actual served model name (the model:
        # field), NOT the filename stem — so serving.json/models.json/config.yaml
        # all agree on the same name.
        import os
        recipe = "~/.sparkrun-local/recipes/local-fixed/qwen3.6-27b-fp8-vllm.yaml"
        if os.path.isfile(os.path.expanduser(recipe)):
            assert _extract_model_name(recipe) == "Qwen/Qwen3.6-27B-FP8"

    def test_extractor_matches_schema(self):
        # _extract_model_name must equal the schema's _model_name (single source
        # of truth) for the same input.
        from cluster_template_schema import ClusterTemplate
        for p in ("/path/to/recipe.yaml", "/x/qwen3.6-27b-fp8-vllm.yaml"):
            assert _extract_model_name(p) == ClusterTemplate._model_name(p)


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
        
        assert result["primary_model"] == "orch"
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

    def test_reapply_is_idempotent(self):
        """Re-running apply must NOT duplicate providers.

        Regression: family providers were appended on every call, so repeated
        apply grew the list unbounded (49x in prod) and corrupted config.yaml.
        """
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
        config = {}
        # Apply five times — the bug would yield 1 + 2*5 = 11 providers.
        for _ in range(5):
            config = _update_hermes_config(config, tpl)
        assert len(config["providers"]) == 3  # orchestrator + 2 families, no dups
        names = [p["name"] for p in config["providers"]]
        assert sorted(names) == ["custom", "family-coding", "family-vision"]

    def test_collapses_preexisting_duplicates(self):
        """A config already corrupted with dup providers is cleaned, not grown."""
        tpl = ClusterTemplate(
            name="test", cluster_size=1,
            orchestrator=ModelSpec(recipe="/path/to/orch.yaml"),
            orchestrator_node="192.168.88.244", families=[],
        )
        # Simulate the corrupted live config: many dup family entries.
        config = {"providers": [{"name": "family-coding", "model": {}}] * 20}
        result = _update_hermes_config(config, tpl)
        names = [p["name"] for p in result["providers"]]
        assert names.count("family-coding") <= 1  # collapsed
        assert "custom" in names


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
