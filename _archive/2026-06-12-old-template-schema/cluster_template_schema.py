"""
Pydantic schema for HSCC cluster templates.

Defines the structure for cluster templates that specify:
- Orchestrator node + recipe
- Worker families (grouped models behind separate LiteLLM proxies)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Dict, Any, Union

from pydantic import BaseModel, Field, field_validator


class NodeSpec(BaseModel):
    """A single cluster node (gateway, worker, NAS)."""
    role: str = Field(description="node role: gateway, worker, nas")
    ip: str = Field(description="IP address")
    ssh_user: str = "spark"


class FamilyProxyConfig(BaseModel):
    """Configuration for a single LiteLLM proxy instance."""
    port: int = Field(default=8000, ge=1, le=65535, description="proxy port")
    host: str = "0.0.0.0"
    model_type: str = "openai"
    api_base: Optional[str] = None  # set if connecting to a different API
    api_key: Optional[str] = None
    extra_args: Dict[str, Any] = Field(default_factory=lambda: {}, description="extra proxy args (e.g. --max-model-len)")


class ModelSpec(BaseModel):
    """A model to serve on a node pool."""
    recipe: str = Field(description="sparkrun recipe (relative or absolute path)")
    tp: int = Field(default=1, ge=1, description="tensor parallelism")
    pp: int = Field(default=1, ge=1, description="pipeline parallelism")
    gpu_memory_util: float = Field(default=0.8, ge=0.1, le=1.0)


class WorkerFamily(BaseModel):
    """A group of models served behind a single LiteLLM proxy."""
    name: str = Field(description="family name, e.g. 'coding', 'vision'")
    models: List[ModelSpec] = Field(description="models in this family")
    nodes: List[str] = Field(description="node IPs for this family")
    proxy: FamilyProxyConfig = Field(default_factory=FamilyProxyConfig)


class ClusterTemplate(BaseModel):
    """Top-level cluster template."""
    name: str = Field(description="template name (filename without .yaml)")
    version: int = 1
    cluster_size: int = Field(ge=1, le=10, description="number of nodes")
    description: str = ""

    orchestrator: ModelSpec = Field(description="orchestrator model config")
    orchestrator_node: str = Field(
        description="IP of the gateway/orchestrator node"
    )

    families: List[WorkerFamily] = Field(
        default_factory=list,
        description="worker model families, each with its own proxy"
    )

    # ── Helpers ──────────────────────────────────────────────────────────

    # ── Validators ─────────────────────────────────────────────────────────

    from pydantic import model_validator

    @model_validator(mode="after")
    def validate_cluster_size(self):
        node_count = 1 + sum(len(f.nodes) for f in self.families)
        if self.cluster_size != node_count:
            raise ValueError(
                f"cluster_size={self.cluster_size} but node count from nodes=[] is {node_count}"
            )
        return self

    @property
    def total_nodes(self) -> int:
        return 1 + sum(len(f.nodes) for f in self.families)

    @property
    def all_worker_ips(self) -> List[str]:
        ips: List[str] = []
        for f in self.families:
            ips.extend(f.nodes)
        return ips

    def to_serving_json(self) -> dict:
        """Convert template to serving.json format."""
        units = []
        # Orchestrator unit
        units.append({
            "id": "orch",
            "role": "orchestrator",
            "model": self._model_name(self.orchestrator.recipe),
            "recipe": self.orchestrator.recipe,
            "nodes": [self.orchestrator_node],
        })
        # Family units
        for family in self.families:
            for model in family.models:
                short = self._model_name(model.recipe).split('/')[-1]
                # ONE unit per (model, node). The daemon keep-alive + idle-reaper
                # operate per node (health-check each endpoint, relaunch a crashed
                # one), so a single unit listing many nodes is not keep-alive-able.
                for node in family.nodes:
                    suffix = node.rsplit('.', 1)[-1]
                    units.append({
                        "id": f"family-{family.name}-{short}-{suffix}",
                        "role": "worker",
                        "keepalive": True,
                        "model": self._model_name(model.recipe),
                        "recipe": model.recipe,
                        "nodes": [node],
                        "tp": model.tp,
                        "pp": model.pp,
                        "family": family.name,
                    })
        return {"version": 1, "port": 8000, "units": units}

    @staticmethod
    def _model_name(recipe_path: str) -> str:
        """Resolve the served model name for a recipe.

        Reads the recipe's ``model:`` field (what vLLM serves + the proxy
        registers). Falls back to the recipe filename stem when the file can't
        be read (e.g. in tests with placeholder paths).
        """
        expanded = str(recipe_path).replace("~", str(Path.home()))
        if expanded.endswith((".yaml", ".yml")):
            try:
                import yaml
                with open(expanded) as f:
                    cfg = yaml.safe_load(f)
                if isinstance(cfg, dict) and cfg.get("model"):
                    return cfg["model"]
            except Exception:
                pass
        # Fallback: filename stem (drop dir + .yaml/.yml extension).
        return Path(expanded).stem


class TemplateRegistry(BaseModel):
    """Registry of available templates."""
    templates: List[dict] = Field(
        default_factory=list,
        description="list of {name, version, cluster_size, description} dicts"
    )


def load_template(yaml_path: Union[str, Path]) -> ClusterTemplate:
    """Load and validate a template from a YAML file."""
    import yaml
    path = Path(yaml_path)
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {yaml_path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    # Merge: load the yaml file's template, then validate
    return ClusterTemplate(**data)


def list_templates(template_dir: Union[str, Path, None] = None) -> TemplateRegistry:
    """List all available templates from the templates directory."""
    if template_dir is None:
        plugin_dir = Path(__file__).parent
        template_dir = plugin_dir / "templates"
    else:
        template_dir = Path(template_dir)

    if not template_dir.exists():
        return TemplateRegistry(templates=[])

    templates = []
    for f in sorted(template_dir.glob("*.yaml")):
        try:
            with open(f) as fh:
                import yaml
                data = yaml.safe_load(fh)
            if data and "name" in data:
                templates.append({
                    "name": data["name"],
                    "version": data.get("version", 1),
                    "cluster_size": data.get("cluster_size", "?"),
                    "description": data.get("description", ""),
                })
        except Exception:
            continue

    return TemplateRegistry(templates=templates)
