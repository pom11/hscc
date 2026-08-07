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


# ── existing-span guard (exclude_nodes) ──────────────────────────────────────

class TestExistingSpanGuard:
    """A node that is ALREADY a tp peer of a live serving span must never be
    handed a new unit by resolve() (the plan/provision side of the
    double-provision bug)."""

    def test_span_member_excluded_from_pool_not_assigned_solo(self):
        """resolve() given exclude_nodes=[peer] drops it from the pool, so it is
        never assigned a solo (tp=1) unit."""
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": "o.yaml",
            "families": [{"name": "coding", "models": ["m.yaml"],
                          "workers": "all"}]})
        plan = ti.resolve(t, _topo(3), _coster=_coster(),
                          exclude_nodes={"10.0.0.4"})   # .4 is a live span peer
        fam_nodes = {u.node for u in plan.families[0].units}
        assert "10.0.0.4" not in fam_nodes               # never assigned
        assert fam_nodes == {"10.0.0.2", "10.0.0.3"}      # only truly-free workers

    def test_no_exclude_nodes_behavior_unchanged(self):
        """Without exclude_nodes, the pool is untouched (regression guard)."""
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": "o.yaml",
            "families": [{"name": "coding", "models": ["m.yaml"],
                          "workers": "all"}]})
        plan = ti.resolve(t, _topo(3), _coster=_coster())
        assert {u.node for u in plan.families[0].units} == \
            {"10.0.0.2", "10.0.0.3", "10.0.0.4"}

    def test_span_member_never_picked_as_tp_peer_of_new_span(self):
        """exclude_nodes also keeps a span member out of the peer slot of a NEW
        spanning unit (it can't be borrowed for orchestrator tp-spanning)."""
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": {"recipe": "o.yaml", "tp": 2},
            "families": [{"name": "coding", "models": ["m.yaml"],
                          "workers": "remaining"}]})
        plan = ti.resolve(t, _topo(3), _coster=_coster(),
                          exclude_nodes={"10.0.0.2"})
        # orchestrator spans its own node + a FREE worker (not the excluded peer)
        assert plan.orchestrator.nodes == ["10.0.0.1", "10.0.0.3"]
        assert "10.0.0.2" not in plan.orchestrator.nodes


# ── unit-aware reserved guard (re-apply idempotency) ─────────────────────────

class TestUnitAwareReserved:
    """resolve() must let a node back into the SAME unit it already serves
    (re-apply idempotency), while still refusing to hand it to a DIFFERENT
    unit. This replaces the blunt exclude-every-tp-peer approach, which emptied
    the pool on a re-apply of the same template (issue t_16dcceb4)."""

    def test_reserved_own_family_node_kept_for_reapply(self):
        """A node already serving family 'reasoning' as a tp peer stays in its
        own unit's pool — re-applying the same template does NOT drop it."""
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": {"recipe": "o.yaml", "tp": 2},
            "families": [{"name": "reasoning",
                          "models": [{"recipe": "big.yaml", "tp": 2}],
                          "workers": "remaining"}]})
        # 4-node cluster; serving.json records orch=[.1,.2], reasoning=[.3,.4].
        topo = FakeTopo(orchestrator=FakeNode("10.0.0.1"),
                        workers=[FakeNode(f"10.0.0.{2+i}") for i in range(3)])
        reserved = {
            "10.0.0.1": {"kind": "orchestrator", "family": None, "model": "o"},
            "10.0.0.2": {"kind": "orchestrator", "family": None, "model": "o"},
            "10.0.0.3": {"kind": "worker", "family": "reasoning", "model": "big"},
            "10.0.0.4": {"kind": "worker", "family": "reasoning", "model": "big"},
        }
        plan = ti.resolve(t, topo, _coster=_coster(), reserved=reserved)
        # orchestrator keeps its own tp peer .2 (same unit), NOT dropped.
        assert plan.orchestrator.nodes == ["10.0.0.1", "10.0.0.2"]
        assert len(plan.families[0].units) == 1
        # reasoning re-gets BOTH its tp=2 nodes.
        assert plan.families[0].units[0].nodes == ["10.0.0.3", "10.0.0.4"]
        assert plan.families[0].units[0].tp == 2

    def test_reserved_node_never_handed_to_different_family(self):
        """A node serving family 'vision' is never handed to family 'coding'
        (double-provision guard preserved)."""
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": "o.yaml",
            "families": [{"name": "coding", "models": ["m.yaml"],
                          "workers": "remaining"}]})
        topo = FakeTopo(orchestrator=FakeNode("10.0.0.1"),
                        workers=[FakeNode("10.0.0.2"), FakeNode("10.0.0.3"),
                                 FakeNode("10.0.0.4")])
        # .3 and .4 are currently serving family 'vision' — they must not be
        # reassigned to 'coding'. Only the free .2 is available.
        reserved = {
            "10.0.0.3": {"kind": "worker", "family": "vision", "model": "v"},
            "10.0.0.4": {"kind": "worker", "family": "vision", "model": "v"},
        }
        plan = ti.resolve(t, topo, _coster=_coster(), reserved=reserved)
        fam_nodes = {u.node for u in plan.families[0].units}
        assert fam_nodes == {"10.0.0.2"}          # reserved stays out of the pool
        assert "10.0.0.3" not in fam_nodes and "10.0.0.4" not in fam_nodes

    def test_reserved_worker_not_borrowed_as_orchestrator_peer(self):
        """A node serving a worker family can't be silently consumed as the
        orchestrator's tp peer over its live span."""
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "orchestrator": {"recipe": "o.yaml", "tp": 2},
            "families": [{"name": "vision", "models": ["v.yaml"], "tp": 1,
                          "workers": "remaining"}]})
        topo = FakeTopo(orchestrator=FakeNode("10.0.0.1"),
                        workers=[FakeNode("10.0.0.2"), FakeNode("10.0.0.3"),
                                 FakeNode("10.0.0.4")])
        # .3 is currently serving family 'vision'; orchestrator needs 1 peer.
        reserved = {"10.0.0.3": {"kind": "worker", "family": "vision", "model": "v"}}
        plan = ti.resolve(t, topo, _coster=_coster(), reserved=reserved)
        # orchestrator borrows a FREE worker (.2), not the live span member .3
        assert plan.orchestrator.nodes == ["10.0.0.1", "10.0.0.2"]
        assert "10.0.0.3" not in plan.orchestrator.nodes
        # vision family still gets its own node
        assert {u.node for u in plan.families[0].units} == {"10.0.0.3", "10.0.0.4"}


# ── schema v3: nodes / allow_colocation / routing (PARSING ONLY) ──────────────
# T1 (t_30b1e1ee) parses and carries the three optional v3 keys through the
# template model. It does NOT act on them — placement/routing/validation are
# later cards. Two invariants are enforced here:
#   * a v3 template exposes the keys when present
#   * omission is DISTINGUISHABLE from empty (None != []) because omission
#     means "do not touch" at apply time
#   * every shipped v2 template round-trips to byte-identical output (golden)

V3_FULL = {
    "name": "4node-dual-dsv4",
    "version": 3,
    "orchestrator": {
        "recipe": "~/.sparkrun-local/recipes/local-fixed/deepseek-v4-fp8-scitrera-hscc.yaml",
        "tp": 2,
        "nodes": ["10.0.0.244", "10.0.0.246"],
    },
    "families": [
        {
            "name": "reasoning",
            "nodes": ["10.0.0.247", "10.0.0.248"],
            "allow_colocation": False,
            "proxy": True,
            "models": [
                {"recipe": "~/.sparkrun-local/recipes/local-fixed/"
                           "deepseek-v4-fp8-scitrera-hscc.yaml",
                 "tp": 2}
            ],
        }
    ],
    "routing": {"delegation": "family-reasoning",
                "compaction": "orchestrator",
                "auxiliaries": "orchestrator"},
}


class TestSchemaV3:
    """v3 parsing — carries the keys through the model, does not act on them."""

    def test_full_v3_exposes_all_keys(self):
        t = ti.ClusterTemplate.from_dict(V3_FULL)
        assert t.version == 3
        # orchestrator nodes
        assert t.orchestrator.nodes == ["10.0.0.244", "10.0.0.246"]
        assert t.orchestrator.tp == 2
        # family nodes + allow_colocation
        fam = t.families[0]
        assert fam.name == "reasoning"
        assert fam.nodes == ["10.0.0.247", "10.0.0.248"]
        assert fam.allow_colocation is False
        assert fam.proxy is True
        assert fam.models[0].tp == 2
        # routing block
        assert t.routing == {"delegation": "family-reasoning",
                             "compaction": "orchestrator",
                             "auxiliaries": "orchestrator"}

    def test_v3_explicitly_allows_colocation(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "version": 3,
            "orchestrator": {"recipe": "o.yaml"},
            "families": [{"name": "c", "models": ["m.yaml"],
                          "allow_colocation": True}]})
        assert t.families[0].allow_colocation is True

    def test_v3_omissions_are_absent_not_empty(self):
        """A v3 template omitting the keys parses with them ABSENT (None), NOT
        defaulted to empty lists — absence must be distinguishable from empty,
        because omission means do-not-touch later."""
        t = ti.ClusterTemplate.from_dict({"name": "x", "version": 3,
                                          "orchestrator": "o.yaml"})
        assert t.orchestrator.nodes is None          # absent, not []
        assert t.routing is None                     # absent, not {}
        # and with a family present, both family keys are absent (None/False)
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "version": 3, "orchestrator": "o.yaml",
            "families": [{"name": "c", "models": ["m.yaml"]}]})
        assert t.families[0].nodes is None           # absent, not []
        assert t.families[0].allow_colocation is False  # default false

    def test_absent_nodes_distinguishable_from_empty_list(self):
        """None (omitted) and [] (explicitly empty) must round-trip differently."""
        omitted = ti.ClusterTemplate.from_dict(
            {"name": "x", "version": 3, "orchestrator": {"recipe": "o.yaml"}})
        empty = ti.ClusterTemplate.from_dict(
            {"name": "x", "version": 3,
             "orchestrator": {"recipe": "o.yaml", "nodes": []}})
        assert omitted.orchestrator.nodes is None
        assert empty.orchestrator.nodes == []
        assert omitted.orchestrator.to_dict() != empty.orchestrator.to_dict()
        # omitted serializes WITHOUT the key; empty serializes WITH "nodes": []
        assert "nodes" not in omitted.orchestrator.to_dict()
        assert empty.orchestrator.to_dict()["nodes"] == []

    def test_roundtrip_stable(self):
        """from_dict(to_dict(x)) == x — the model is self-consistent."""
        a = ti.ClusterTemplate.from_dict(V3_FULL)
        b = ti.ClusterTemplate.from_dict(a.to_dict())
        assert a.to_dict() == b.to_dict()
        assert b.orchestrator.nodes == a.orchestrator.nodes
        assert b.families[0].nodes == a.families[0].nodes
        assert b.routing == a.routing

    def test_v2_still_rejects_new_keys(self):
        """v3 keys on a v1/v2 template are still topology pins → refused loudly
        (an operator must bump version: 3 to use them, not silently drop them)."""
        with pytest.raises(ti.TemplateIntentError) as e:
            ti.ClusterTemplate.from_dict({
                "name": "x", "version": 2, "orchestrator": "o.yaml",
                "families": [{"name": "c", "models": ["m.yaml"],
                              "nodes": ["10.0.0.246"]}]})
        assert "nodes" in str(e.value)

        with pytest.raises(ti.TemplateIntentError) as e:
            ti.ClusterTemplate.from_dict({
                "name": "x", "version": 2, "orchestrator": "o.yaml",
                "routing": {"delegation": "orchestrator"}})
        assert "routing" in str(e.value)

    def test_v3_does_not_reject_nodes_keys(self):
        """version: 3 must NOT be refused for having nodes/routing (they're valid)."""
        t = ti.ClusterTemplate.from_dict(V3_FULL)
        assert t.version == 3 and t.routing

    def test_v3_legacy_topology_keys_still_rejected(self):
        """v3 does not resurrect pre-v2 topology pins."""
        with pytest.raises(ti.TemplateIntentError) as e:
            ti.ClusterTemplate.from_dict({
                "name": "x", "version": 3, "orchestrator": "o.yaml",
                "orchestrator_node": "10.0.0.244"})
        assert "orchestrator_node" in str(e.value)
        with pytest.raises(ti.TemplateIntentError) as e:
            ti.ClusterTemplate.from_dict({
                "name": "x", "version": 3, "orchestrator": "o.yaml",
                "families": [{"name": "c", "models": ["m.yaml"],
                              "proxy": {"port": 4001}}]})
        assert "proxy.port" in str(e.value)


# ── schema v3 byte-identical regression (every shipped v2 template) ─────────

def test_v2_templates_parse_byte_identical_to_golden():
    """Every existing v2 template in templates/ must parse to BYTE-IDENTICAL
    output as before the v3 additions. Guarded against a stored golden snapshot
    captured from the pre-v3 code: if v3 parsing changed ANYTHING about how a
    v2 template parses (dropped a field, added a spurious one, reordered), this
    fails. This is the regression guard that makes any honest breakage loud."""
    import glob, json, os, yaml
    here = os.path.dirname(os.path.abspath(__file__))
    golden_path = os.path.join(here, "_v2_parsed_golden.json")
    assert os.path.exists(golden_path), "missing golden snapshot"
    with open(golden_path) as fh:
        golden = json.load(fh)
    assert golden, "empty golden snapshot"

    templates = sorted(glob.glob(
        os.path.join(os.path.dirname(here), "templates", "**", "*.yaml"),
        recursive=True))
    parsed = {}
    for f in templates:
        with open(f) as fh:
            data = yaml.safe_load(fh) or {}
        name = data.get("name")
        if not name:
            continue
        parsed[name] = ti.ClusterTemplate.from_dict(data).to_dict()

    assert set(parsed) == set(golden), \
        f"template set changed vs golden: missing={set(golden)-set(parsed)} " \
        f"extra={set(parsed)-set(golden)}"
    for name in golden:
        assert parsed[name] == golden[name], \
            f"template '{name}' parsed output differs from pre-v3 golden"
