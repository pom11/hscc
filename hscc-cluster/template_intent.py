"""Topology-free cluster templates (D16/D17) — intent in, resolved plan out.

A template describes INTENT only: which recipes, what family structure, how many
workers. It carries NO IPs and NO ports. At apply time `resolve()` maps it onto
the live cluster (from discovery) and assigns ports + nodes via sparkrun-cost
auto-fit (recipe_cost.plan_placement). This dissolves the topology leak at the
root and makes re-IP / add-node need zero template edits.

Schema v2:
  ModelIntent   {recipe, tp, pp}
  FamilyIntent  {name, models[], workers: "all"|N|"remaining", proxy: bool}
  ClusterTemplate {name, version, description, orchestrator: ModelIntent, families[]}
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Union, Any, Dict

try:
    from . import recipe_cost as _rc
except ImportError:
    import recipe_cost as _rc


class TemplateIntentError(ValueError):
    """Invalid intent template (bad shape) or unresolvable against the cluster."""


# ── Intent schema (what the user authors) ───────────────────────────────────

@dataclass
class ModelIntent:
    recipe: str
    tp: int = 1
    pp: int = 1

    @staticmethod
    def from_dict(d: Union[str, dict]) -> "ModelIntent":
        if isinstance(d, str):
            return ModelIntent(recipe=d)
        if not isinstance(d, dict) or not d.get("recipe"):
            raise TemplateIntentError(f"model needs a recipe: {d!r}")
        return ModelIntent(recipe=d["recipe"], tp=int(d.get("tp", 1)),
                           pp=int(d.get("pp", 1)))


@dataclass
class FamilyIntent:
    name: str
    models: List[ModelIntent]
    workers: Union[str, int] = "all"   # "all" | N | "remaining"
    proxy: bool = True

    @staticmethod
    def from_dict(d: dict) -> "FamilyIntent":
        if not isinstance(d, dict) or not d.get("name"):
            raise TemplateIntentError(f"family needs a name: {d!r}")
        models = [ModelIntent.from_dict(m) for m in (d.get("models") or [])]
        if not models:
            raise TemplateIntentError(f"family '{d['name']}' has no models")
        workers = d.get("workers", "all")
        if isinstance(workers, str) and workers not in ("all", "remaining"):
            raise TemplateIntentError(
                f"family '{d['name']}' workers must be 'all', 'remaining', or an int")
        return FamilyIntent(name=d["name"], models=models, workers=workers,
                            proxy=bool(d.get("proxy", True)))


@dataclass
class ClusterTemplate:
    name: str
    orchestrator: ModelIntent
    families: List[FamilyIntent] = field(default_factory=list)
    version: int = 2
    description: str = ""

    @staticmethod
    def from_dict(d: dict) -> "ClusterTemplate":
        if not isinstance(d, dict) or not d.get("name"):
            raise TemplateIntentError("template needs a name")
        orch = d.get("orchestrator")
        if not orch:
            raise TemplateIntentError("template needs an orchestrator")
        # Reject legacy topology keys so an old template fails loudly, not silently.
        legacy = [k for k in ("orchestrator_node", "cluster_size") if k in d]
        for fam in (d.get("families") or []):
            if isinstance(fam, dict) and "nodes" in fam:
                legacy.append(f"families.{fam.get('name','?')}.nodes")
            if isinstance(fam, dict) and isinstance(fam.get("proxy"), dict):
                legacy.append(f"families.{fam.get('name','?')}.proxy.port")
        if legacy:
            raise TemplateIntentError(
                "v2 templates are topology-free — remove pinned keys: "
                + ", ".join(legacy))
        return ClusterTemplate(
            name=d["name"],
            orchestrator=ModelIntent.from_dict(orch),
            families=[FamilyIntent.from_dict(f) for f in (d.get("families") or [])],
            version=int(d.get("version", 2)),
            description=d.get("description", ""),
        )


# ── Resolved plan (concrete nodes + ports, produced at apply) ────────────────

@dataclass
class ResolvedUnit:
    role: str            # "orchestrator" | "worker"
    family: Optional[str]
    recipe: str
    model: str
    node: str
    port: int
    tp: int
    pp: int


@dataclass
class ResolvedFamily:
    name: str
    proxy_port: Optional[int]
    units: List[ResolvedUnit]


@dataclass
class ResolvedPlan:
    template: str
    orchestrator: ResolvedUnit
    families: List[ResolvedFamily]

    @property
    def all_units(self) -> List[ResolvedUnit]:
        u = [self.orchestrator]
        for f in self.families:
            u.extend(f.units)
        return u


def _select_workers(spec: Union[str, int], pool: List[str]) -> List[str]:
    """Map workers: all|N|remaining onto the available worker-ip pool."""
    if spec == "all":
        return list(pool)
    if spec == "remaining":
        return list(pool)
    n = int(spec)
    return pool[:n]


def resolve(tpl: ClusterTemplate, topology: Any, *, _coster=None,
            base_proxy_port: int = 4000) -> ResolvedPlan:
    """Map an intent template onto the live cluster topology.

    topology: a discovery.ClusterTopology (orchestrator + workers[, vram_free]).
    Assigns nodes from discovery and ports via recipe_cost.plan_placement.
    Raises TemplateIntentError when it can't fit / no workers / etc.
    """
    coster = _coster or _rc.recipe_cost
    orch_node = topology.orchestrator.ip
    orch_model = _model_name(tpl.orchestrator.recipe)
    orchestrator = ResolvedUnit(
        role="orchestrator", family=None, recipe=tpl.orchestrator.recipe,
        model=orch_model, node=orch_node, port=8000,
        tp=tpl.orchestrator.tp, pp=tpl.orchestrator.pp)

    worker_nodes = [{"ip": w.ip, "vram_free_gb": getattr(w, "vram_free_gb", None)}
                    for w in topology.workers]
    pool_ips = [w["ip"] for w in worker_nodes]
    claimed: set = set()
    resolved_families: List[ResolvedFamily] = []
    proxy_port = base_proxy_port

    for fam in tpl.families:
        avail = [ip for ip in _select_workers(fam.workers, pool_ips)
                 if ip not in claimed]
        if not avail:
            raise TemplateIntentError(
                f"family '{fam.name}': no available worker nodes "
                f"(pool={pool_ips}, claimed={sorted(claimed)})")
        nodes_for_fam = {w["ip"]: w for w in worker_nodes if w["ip"] in avail}
        # A family REPLICATES its models across the selected workers: every
        # selected node hosts the family's full model set. On each node the
        # co-located models get sequential ports (8000, 8001, …) and their
        # combined per-GPU cost must fit the node's free VRAM. A tp>1 model can't
        # co-locate (G3) — it must be the only model in its family.
        multi = len(fam.models) > 1
        if multi and any(m.tp > 1 for m in fam.models):
            raise TemplateIntentError(
                f"family '{fam.name}': cannot co-locate when a model has tp>1 "
                f"(tp>1 needs the node exclusively)")
        units: List[ResolvedUnit] = []
        for ip in avail:
            free = nodes_for_fam[ip].get("vram_free_gb")
            need_sum = 0.0
            for i, m in enumerate(fam.models):
                cost = coster(m.recipe)
                if cost.fits is False:
                    raise TemplateIntentError(
                        f"family '{fam.name}': {m.recipe} does not fit a DGX Spark")
                per = cost.per_gpu_total_gb
                if per is not None:
                    need_sum += per
                if free is not None and per is not None and need_sum > free:
                    raise TemplateIntentError(
                        f"family '{fam.name}': models overflow node {ip} VRAM "
                        f"(need {need_sum} GB > {free} GB free)")
                units.append(ResolvedUnit(
                    role="worker", family=fam.name, recipe=m.recipe,
                    model=_model_name(m.recipe), node=ip, port=8000 + i,
                    tp=m.tp, pp=m.pp))
            claimed.add(ip)
        resolved_families.append(ResolvedFamily(
            name=fam.name, proxy_port=proxy_port if fam.proxy else None,
            units=units))
        if fam.proxy:
            proxy_port += 1

    return ResolvedPlan(template=tpl.name, orchestrator=orchestrator,
                        families=resolved_families)


def validate_resolved(plan: ResolvedPlan) -> List[str]:
    """Validate a RESOLVED plan (intent has no ports/nodes to validate). With
    auto-assigned ports, collisions are structurally impossible — this catches
    any residual (node,port) dup + missing recipe."""
    errors: List[str] = []
    seen: set = set()
    for u in plan.all_units:
        key = (u.node, u.port)
        if key in seen:
            errors.append(f"(node,port) collision: {u.node}:{u.port}")
        seen.add(key)
        if not Path_isfile(u.recipe):
            errors.append(f"{u.role} recipe not found: {u.recipe}")
    return errors


# ── helpers ──────────────────────────────────────────────────────────────────

def Path_isfile(recipe: str) -> bool:
    return os.path.isfile(os.path.expanduser(recipe))


def _model_name(recipe_path: str) -> str:
    """Served model name from a recipe's model: field, else filename stem."""
    expanded = os.path.expanduser(recipe_path)
    if expanded.endswith((".yaml", ".yml")) and os.path.isfile(expanded):
        try:
            import yaml
            with open(expanded) as f:
                cfg = yaml.safe_load(f)
            if isinstance(cfg, dict) and cfg.get("model"):
                return cfg["model"]
        except Exception:
            pass
    return os.path.splitext(os.path.basename(expanded))[0]


def to_serving_json(plan: ResolvedPlan) -> dict:
    """Resolved plan → serving.json (one keepalive unit per worker unit, on its
    assigned port; the orchestrator on 8000)."""
    units = [{
        "id": "orch",
        "role": "orchestrator",
        "model": plan.orchestrator.model,
        "recipe": plan.orchestrator.recipe,
        "nodes": [plan.orchestrator.node],
        "port": plan.orchestrator.port,
    }]
    for fam in plan.families:
        for u in fam.units:
            short = u.model.split("/")[-1]
            suffix = u.node.rsplit(".", 1)[-1]
            units.append({
                "id": f"family-{fam.name}-{short}-{suffix}-{u.port}",
                "role": "worker",
                "keepalive": True,
                "model": u.model,
                "recipe": u.recipe,
                "nodes": [u.node],
                "port": u.port,
                "tp": u.tp,
                "pp": u.pp,
                "family": fam.name,
            })
    return {"version": 2, "units": units}
