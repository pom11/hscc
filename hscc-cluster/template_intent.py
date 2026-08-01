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

class ResolvedUnit:
    """A resolved deployment unit — may span multiple nodes when tp > 1."""

    def __init__(self, role: str, family: Optional[str], recipe: str,
                 model: str, nodes: List[str], port: int, tp: int, pp: int):
        self.role = role
        self.family = family
        self.recipe = recipe
        self.model = model
        self.nodes = nodes        # ordered span; len == tp
        self.port = port
        self.tp = tp
        self.pp = pp

    @property
    def node(self) -> str:
        """Backward-compat: primary node in the span."""
        return self.nodes[0]

    def __repr__(self) -> str:
        return (f"ResolvedUnit(role={self.role!r}, family={self.family!r}, "
                f"recipe={self.recipe!r}, model={self.model!r}, "
                f"nodes={self.nodes!r}, port={self.port}, tp={self.tp}, pp={self.pp})")


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
    orch_ip = topology.orchestrator.ip
    orch_model = _model_name(tpl.orchestrator.recipe)
    orch_tp = tpl.orchestrator.tp

    worker_nodes = [{"ip": w.ip, "vram_free_gb": getattr(w, "vram_free_gb", None)}
                    for w in topology.workers]
    pool_ips = [w["ip"] for w in worker_nodes]

    # ── Orchestrator spanning ─────────────────────────────────────────────
    # tp>1: claim the orchestrator node + (tp-1) workers from the front of the pool.
    orch_span: List[str] = [orch_ip]
    if orch_tp > 1:
        needed = orch_tp - 1
        if needed > len(pool_ips):
            raise TemplateIntentError(
                f"orchestrator tp={orch_tp} but only {len(pool_ips)} worker nodes "
                f"available (need {needed} additional nodes)")
        orch_span.extend(pool_ips[:needed])
        pool_ips = pool_ips[needed:]  # remove claimed nodes from the pool

    orchestrator = ResolvedUnit(
        role="orchestrator", family=None, recipe=tpl.orchestrator.recipe,
        model=orch_model, nodes=orch_span, port=8000,
        tp=tpl.orchestrator.tp, pp=tpl.orchestrator.pp)

    claimed: set = set(orch_span[1:])  # workers claimed by orchestrator span
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

        # Find the max tp in this family
        max_tp = max(m.tp for m in fam.models)

        # A family with multi models cannot have tp>1
        multi = len(fam.models) > 1
        if multi and max_tp > 1:
            raise TemplateIntentError(
                f"family '{fam.name}': cannot co-locate when a model has tp>1 "
                f"(tp>1 needs the node exclusively)")

        # ── Family spanning ───────────────────────────────────────────────
        units: List[ResolvedUnit] = []
        if max_tp > 1:
            # tp>1: each instance consumes `tp` nodes. `workers: N` means N nodes
            # total, so number of instances = N // tp.
            num_instances = len(avail) // max_tp
            if num_instances == 0:
                raise TemplateIntentError(
                    f"family '{fam.name}': tp={max_tp} requires at least {max_tp} "
                    f"nodes but only {len(avail)} available")
            for inst in range(num_instances):
                span = avail[inst * max_tp:(inst + 1) * max_tp]
                for i, m in enumerate(fam.models):
                    cost = coster(m.recipe)
                    if cost.fits is False:
                        raise TemplateIntentError(
                            f"family '{fam.name}': {m.recipe} does not fit a DGX Spark")
                    per = cost.per_gpu_total_gb
                    # VRAM check: every node in the span
                    for node_ip in span:
                        free = nodes_for_fam[node_ip].get("vram_free_gb")
                        if free is not None and per is not None and per > free:
                            raise TemplateIntentError(
                                f"family '{fam.name}': {m.recipe} does not fit node "
                                f"{node_ip} VRAM (need {per} GB > {free} GB free)")
                    units.append(ResolvedUnit(
                        role="worker", family=fam.name, recipe=m.recipe,
                        model=_model_name(m.recipe), nodes=span, port=8000 + i,
                        tp=m.tp, pp=m.pp))
                for node_ip in span:
                    claimed.add(node_ip)
        else:
            # tp==1: existing replication behavior — one unit per node
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
                        model=_model_name(m.recipe), nodes=[ip], port=8000 + i,
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
        # Check EVERY node in the span, not just the primary: a tp>1 unit
        # occupies its whole span, so a second unit landing on any spanned node
        # at the same port is a real conflict. Keying on u.node alone would miss
        # a collision on the 2nd..Nth node.
        for node_ip in u.nodes:
            key = (node_ip, u.port)
            if key in seen:
                errors.append(f"(node,port) collision: {node_ip}:{u.port}")
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
        "nodes": plan.orchestrator.nodes,
        "port": plan.orchestrator.port,
    }]
    for fam in plan.families:
        for u in fam.units:
            short = u.model.split("/")[-1]
            suffix = u.nodes[0].rsplit(".", 1)[-1]
            units.append({
                "id": f"family-{fam.name}-{short}-{suffix}-{u.port}",
                "role": "worker",
                "keepalive": True,
                "model": u.model,
                "recipe": u.recipe,
                "nodes": u.nodes,
                "port": u.port,
                "tp": u.tp,
                "pp": u.pp,
                "family": fam.name,
            })
    return {"version": 2, "units": units}
