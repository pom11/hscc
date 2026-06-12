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
