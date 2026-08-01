"""Tests for template_intent.py — topology-free intent schema + resolve (D16/D17)."""

from dataclasses import dataclass

import pytest

import template_intent as ti
import recipe_cost as rc


# ── fake topology (mimics discovery.ClusterTopology) ─────────────────────────

@dataclass
class FakeNode:
    ip: str
    vram_free_gb: float = 120.0


@dataclass
class FakeTopo:
    orchestrator: FakeNode
    workers: list


def _topo(n_workers=3, vram=120.0):
    return FakeTopo(orchestrator=FakeNode("10.0.0.1"),
                    workers=[FakeNode(f"10.0.0.{2+i}", vram) for i in range(n_workers)])


def _coster(per_gpu=30.0, fits=True, tp=1):
    return lambda recipe: rc.RecipeCost(recipe, per_gpu_total_gb=per_gpu,
                                        fits=fits, tensor_parallel=tp)


# ── schema parsing ───────────────────────────────────────────────────────────

class TestSchema:
    def test_minimal(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": {"recipe": "orch.yaml"}})
        assert t.name == "x" and t.orchestrator.recipe == "orch.yaml"
        assert t.families == []

    def test_model_shorthand_string(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": "orch.yaml",
            "families": [{"name": "coding", "models": ["m.yaml"], "workers": "all"}]})
        assert t.families[0].models[0].recipe == "m.yaml"

    def test_rejects_legacy_orchestrator_node(self):
        with pytest.raises(ti.TemplateIntentError) as e:
            ti.ClusterTemplate.from_dict({
                "name": "x", "orchestrator": {"recipe": "o.yaml"},
                "orchestrator_node": "10.0.0.244"})
        assert "topology-free" in str(e.value)

    def test_rejects_legacy_family_nodes_and_port(self):
        with pytest.raises(ti.TemplateIntentError) as e:
            ti.ClusterTemplate.from_dict({
                "name": "x", "orchestrator": {"recipe": "o.yaml"},
                "families": [{"name": "c", "models": ["m.yaml"],
                              "nodes": ["10.0.0.246"],
                              "proxy": {"port": 4001}}]})
        msg = str(e.value)
        assert "nodes" in msg and "port" in msg

    def test_workers_must_be_valid(self):
        with pytest.raises(ti.TemplateIntentError):
            ti.ClusterTemplate.from_dict({
                "name": "x", "orchestrator": "o.yaml",
                "families": [{"name": "c", "models": ["m.yaml"], "workers": "some"}]})

    def test_family_needs_models(self):
        with pytest.raises(ti.TemplateIntentError):
            ti.ClusterTemplate.from_dict({
                "name": "x", "orchestrator": "o.yaml",
                "families": [{"name": "c", "models": []}]})


# ── resolve ──────────────────────────────────────────────────────────────────

class TestResolve:
    def test_orchestrator_maps_to_gateway(self):
        t = ti.ClusterTemplate.from_dict({"name": "x", "orchestrator": "o.yaml"})
        plan = ti.resolve(t, _topo(), _coster=_coster())
        assert plan.orchestrator.node == "10.0.0.1"
        assert plan.orchestrator.port == 8000

    def test_family_all_workers_mapped(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": "o.yaml",
            "families": [{"name": "coding", "models": ["m.yaml"], "workers": "all"}]})
        plan = ti.resolve(t, _topo(3), _coster=_coster())
        fam = plan.families[0]
        assert fam.proxy_port == 4000
        assert {u.node for u in fam.units} == {"10.0.0.2", "10.0.0.3", "10.0.0.4"}
        assert all(u.port == 8000 for u in fam.units)   # one model/node → :8000

    def test_worker_count_limits(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": "o.yaml",
            "families": [{"name": "c", "models": ["m.yaml"], "workers": 2}]})
        plan = ti.resolve(t, _topo(3), _coster=_coster())
        assert len(plan.families[0].units) == 2

    def test_two_models_one_family_colocate_distinct_ports(self):
        # one worker, two small models → co-located on distinct ports
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": "o.yaml",
            "families": [{"name": "c", "models": ["a.yaml", "b.yaml"], "workers": 1}]})
        plan = ti.resolve(t, _topo(1, vram=120), _coster=_coster(per_gpu=30))
        ports = sorted(u.port for u in plan.families[0].units)
        assert ports == [8000, 8001]
        assert len({u.node for u in plan.families[0].units}) == 1   # same node

    def test_proxy_ports_increment_per_family(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": "o.yaml",
            "families": [
                {"name": "coding", "models": ["a.yaml"], "workers": 1},
                {"name": "vision", "models": ["b.yaml"], "workers": "remaining"}]})
        plan = ti.resolve(t, _topo(3), _coster=_coster())
        assert [f.proxy_port for f in plan.families] == [4000, 4001]

    def test_remaining_excludes_claimed(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": "o.yaml",
            "families": [
                {"name": "coding", "models": ["a.yaml"], "workers": 1},
                {"name": "vision", "models": ["b.yaml"], "workers": "remaining"}]})
        plan = ti.resolve(t, _topo(3), _coster=_coster())
        coding_nodes = {u.node for u in plan.families[0].units}
        vision_nodes = {u.node for u in plan.families[1].units}
        assert coding_nodes.isdisjoint(vision_nodes)
        assert len(vision_nodes) == 2     # the 2 not claimed by coding

    def test_overcommit_raises(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": "o.yaml",
            "families": [{"name": "c", "models": ["a.yaml", "b.yaml"], "workers": 1}]})
        with pytest.raises(ti.TemplateIntentError):
            ti.resolve(t, _topo(1, vram=50), _coster=_coster(per_gpu=40))  # 80>50

    def test_no_workers_raises(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": "o.yaml",
            "families": [{"name": "c", "models": ["a.yaml"], "workers": "all"}]})
        with pytest.raises(ti.TemplateIntentError):
            ti.resolve(t, _topo(0), _coster=_coster())


# ── serving.json ─────────────────────────────────────────────────────────────

class TestServingJson:
    def test_units_carry_ports(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": "o.yaml",
            "families": [{"name": "c", "models": ["a.yaml", "b.yaml"], "workers": 1}]})
        plan = ti.resolve(t, _topo(1), _coster=_coster(per_gpu=30))
        sj = ti.to_serving_json(plan)
        assert sj["version"] == 2
        workers = [u for u in sj["units"] if u["role"] == "worker"]
        assert len(workers) == 2
        assert all(w["keepalive"] and "port" in w for w in workers)
        assert sorted(w["port"] for w in workers) == [8000, 8001]
        # unique ids even when co-located (id includes port)
        assert len({w["id"] for w in workers}) == 2


# ── shipped node-count templates REALLY fit (real recipe_cost, real files) ───

def test_all_shipped_templates_resolve_and_fit():
    """Every templates/Nnode/*.yaml must resolve against an N-node cluster using
    the REAL sparkrun-show VRAM cost (recipe files exist) — proving they can
    actually be deployed, not just parse."""
    import glob, os, yaml
    import cluster_template as ct
    files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..",
                                          "templates", "*node", "*.yaml")))
    assert files, "no node-count templates found"
    for f in files:
        nnode = int(os.path.basename(os.path.dirname(f)).replace("node", ""))
        name = yaml.safe_load(open(f))["name"]
        tpl = ct._load_intent(name)
        topo = FakeTopo(FakeNode("10.0.0.1"),
                        [FakeNode(f"10.0.0.{2+i}") for i in range(nnode - 1)])
        plan = ti.resolve(tpl, topo)              # real recipe_cost (files on disk)
        assert ct.validate_resolved_plan(plan) == [], f"{name} did not validate"


# ── multi-node tp spanning ───────────────────────────────────────────────────

class TestMultiNodeTP:
    """tp>1 must claim a real span of nodes, not silently run on one GPU."""

    def test_orchestrator_tp2_claims_two_nodes_and_shrinks_pool(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": {"recipe": "o.yaml", "tp": 2},
            "families": [{"name": "coding", "models": ["m.yaml"],
                          "workers": "remaining"}]})
        plan = ti.resolve(t, _topo(n_workers=3), _coster=_coster())
        # orchestrator spans its own node + 1 worker taken from the pool front
        assert plan.orchestrator.nodes == ["10.0.0.1", "10.0.0.2"]
        assert plan.orchestrator.tp == 2
        # the borrowed worker must NOT also be handed to the family
        fam_nodes = {n for u in plan.families[0].units for n in u.nodes}
        assert "10.0.0.2" not in fam_nodes
        assert fam_nodes == {"10.0.0.3", "10.0.0.4"}

    def test_family_tp2_yields_one_spanning_unit_not_two(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": {"recipe": "o.yaml"},
            "families": [{"name": "reasoning",
                          "models": [{"recipe": "big.yaml", "tp": 2}],
                          "workers": 2}]})
        plan = ti.resolve(t, _topo(n_workers=3), _coster=_coster())
        units = plan.families[0].units
        assert len(units) == 1, "tp=2 over 2 workers is ONE instance, not two"
        assert units[0].nodes == ["10.0.0.2", "10.0.0.3"]
        assert units[0].tp == 2

    def test_orchestrator_tp_exceeding_cluster_raises(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": {"recipe": "o.yaml", "tp": 4}})
        with pytest.raises(ti.TemplateIntentError) as e:
            ti.resolve(t, _topo(n_workers=1), _coster=_coster())
        assert "tp=4" in str(e.value)

    def test_family_tp_exceeding_available_nodes_raises(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": {"recipe": "o.yaml"},
            "families": [{"name": "reasoning",
                          "models": [{"recipe": "big.yaml", "tp": 4}],
                          "workers": "all"}]})
        with pytest.raises(ti.TemplateIntentError) as e:
            ti.resolve(t, _topo(n_workers=2), _coster=_coster())
        assert "tp=4" in str(e.value)

    def test_span_vram_checked_on_every_node(self):
        """A model that fits the first node but not the second must be refused."""
        topo = FakeTopo(orchestrator=FakeNode("10.0.0.1"),
                        workers=[FakeNode("10.0.0.2", 120.0),
                                 FakeNode("10.0.0.3", 10.0)])
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": {"recipe": "o.yaml"},
            "families": [{"name": "reasoning",
                          "models": [{"recipe": "big.yaml", "tp": 2}],
                          "workers": 2}]})
        with pytest.raises(ti.TemplateIntentError) as e:
            ti.resolve(t, topo, _coster=_coster(per_gpu=76.16))
        assert "10.0.0.3" in str(e.value)

    def test_tp1_behavior_unchanged(self):
        """Regression: tp=1 still replicates one unit per node, nodes==[ip]."""
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": {"recipe": "o.yaml"},
            "families": [{"name": "coding", "models": ["m.yaml"],
                          "workers": "all"}]})
        plan = ti.resolve(t, _topo(n_workers=3), _coster=_coster())
        units = plan.families[0].units
        assert len(units) == 3
        assert all(len(u.nodes) == 1 and u.tp == 1 for u in units)
        assert plan.orchestrator.nodes == ["10.0.0.1"]

    def test_serving_json_emits_full_span(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": {"recipe": "o.yaml", "tp": 2},
            "families": []})
        plan = ti.resolve(t, _topo(n_workers=2), _coster=_coster())
        js = ti.to_serving_json(plan)
        assert js["units"][0]["nodes"] == ["10.0.0.1", "10.0.0.2"]

    def test_validate_catches_collision_on_non_primary_span_node(self):
        """A conflict on the 2nd node of a span must not slip through."""
        u1 = ti.ResolvedUnit(role="orchestrator", family=None, recipe="o.yaml",
                             model="o", nodes=["10.0.0.1", "10.0.0.2"],
                             port=8000, tp=2, pp=1)
        u2 = ti.ResolvedUnit(role="worker", family="f", recipe="o.yaml",
                             model="m", nodes=["10.0.0.2"], port=8000, tp=1, pp=1)
        plan = ti.ResolvedPlan(template="x", orchestrator=u1,
                               families=[ti.ResolvedFamily("f", 4000, [u2])])
        errs = ti.validate_resolved(plan)
        assert any("collision" in e and "10.0.0.2" in e for e in errs)
