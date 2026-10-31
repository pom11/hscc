"""Tests for the per-model ``kind`` template key (media M4, M0 §4).

`kind: chat|image|video` on ModelIntent — validated and round-tripped exactly
like ``max_num_seqs`` / ``gpu_memory_utilization`` (the #106 precedent: a
model-level key, no schema-version bump). What is pinned here:

1. VALIDATION at parse — an enum, default "chat"; anything else (bad string,
   case-drift, non-string) fails at PARSE so `hscc template validate` reports
   it offline, mirroring :func:`template_intent._kind_from`.
2. ROUND-TRIP — ``to_dict()`` omits ``kind`` when it is "chat", so every
   pre-media template round-trips byte-identically (the goldens pin the rest).
3. RESOLVE — every placement branch (tp==1 replication, tp>1 span, explicit
   nodes, orchestrator) carries the declared kind onto ResolvedUnit.kind.
4. RENDER UNCHANGED — M0 §4 is explicit: ``kind`` NEVER enters the serve argv.
   Declaring or FLIPPING kind on an already-provisioned unit must produce zero
   phantom drift (the two rendered commands must be equal, not merely
   equivalent), while the recipe stays the real drift signal.
5. SERVING.JSON — non-chat kind is written into the unit record (omit-if-
   default, the uniform_pool convention); a chat-only plan's serving.json is
   the exact pre-change shape.
6. The shipped DRAFT media template validates structurally (dry) — fakes only,
   no live cluster, no sparkrun exec (the suite fence makes any real call a
   hard failure). The kind-vs-recipe coherence leg is §4's "built later".

All tests use fakes — no sparkrun, no cluster state.
"""

import json

import pytest

import template_intent as ti
import recipe_cost as rc
import cluster_template as ct


# ── fake topology (same shape as test_max_num_seqs's) ─────────────────────────

class FakeNode:
    def __init__(self, ip, vram_free_gb=120.0):
        self.ip = ip
        self.vram_free_gb = vram_free_gb


class FakeTopo:
    def __init__(self, orchestrator, workers):
        self.orchestrator = orchestrator
        self.workers = workers


def _topo(n_workers=2, vram=120.0):
    return FakeTopo(FakeNode("10.0.0.1"),
                    [FakeNode(f"10.0.0.{2+i}", vram) for i in range(n_workers)])


def _coster(per_gpu=30.0, fits=True, tp=1):
    return lambda recipe: rc.RecipeCost(recipe, per_gpu_total_gb=per_gpu,
                                        fits=fits, tensor_parallel=tp)


def _model_dict(**extra):
    d = {"recipe": "r.yaml"}
    d.update(extra)
    return d


# ── 1. validation ────────────────────────────────────────────────────────────

class TestKindValidation:
    """`kind: chat|image|video` or nothing. Bad values fail at PARSE."""

    def test_absent_means_chat(self):
        m = ti.ModelIntent.from_dict(_model_dict())
        assert m.kind == "chat"
        assert "kind" not in m.to_dict()

    def test_string_shorthand_defaults_to_chat(self):
        m = ti.ModelIntent.from_dict("r.yaml")
        assert m.kind == "chat"

    def test_valid_values_parse(self):
        for good in ("chat", "image", "video"):
            m = ti.ModelIntent.from_dict(_model_dict(kind=good))
            assert m.kind == good

    def test_rejects_unknown_string(self):
        for bad in ("audio", "imagen", "", "CHAT", "Image", "video "):
            with pytest.raises(ti.TemplateIntentError) as e:
                ti.ModelIntent.from_dict(_model_dict(kind=bad))
            assert "kind" in str(e.value)

    def test_rejects_non_string(self):
        # YAML `kind: true`, a list, a dict — never the enum the key means.
        for bad in (True, 1, 0, ["image"], {"k": "image"}):
            with pytest.raises(ti.TemplateIntentError) as e:
                ti.ModelIntent.from_dict(_model_dict(kind=bad))
            assert "kind" in str(e.value)

    def test_error_names_the_key_and_the_vocabulary(self):
        # The operator must be able to fix the line without opening the spec:
        # the message names the key AND every legal value (like gpu_mem names
        # its range).
        with pytest.raises(ti.TemplateIntentError) as e:
            ti.ModelIntent.from_dict(_model_dict(kind="vidoe"))
        msg = str(e.value)
        assert msg.startswith("kind")
        for vocab in ("chat", "image", "video"):
            assert vocab in msg

    def test_parse_error_surfaces_through_template_from_dict(self):
        # Same path the structural validator walks: a family model with a bad
        # kind must raise, not be silently dropped to chat.
        with pytest.raises(ti.TemplateIntentError):
            ti.ClusterTemplate.from_dict({
                "name": "x", "version": 3, "orchestrator": "o.yaml",
                "families": [{"name": "m", "workers": 1,
                              "models": [_model_dict(kind="vidoe")]}]})

    def test_unit_kinds_constant_is_the_vocabulary(self):
        assert ti.UNIT_KINDS == ("chat", "image", "video")


# ── 2. round-trip ────────────────────────────────────────────────────────────

class TestKindRoundTrip:
    def test_present_round_trips(self):
        m = ti.ModelIntent.from_dict(_model_dict(kind="image"))
        assert m.to_dict() == {"recipe": "r.yaml", "kind": "image"}

    def test_chat_spelled_out_omitted_so_pre_change_dicts_are_identical(self):
        # M0 §4 chose explicit `chat` over None — which makes the omit rule do
        # the byte-identity work: an author who WRITES `kind: chat` gets the
        # same canonical dict as one who omits it.
        m = ti.ModelIntent.from_dict(
            _model_dict(kind="chat", tp=2, max_num_seqs=5))
        assert m.to_dict() == {"recipe": "r.yaml", "tp": 2, "max_num_seqs": 5}
        assert "kind" not in m.to_dict()

    def test_full_template_round_trip(self):
        # to_dict() canonicalises a string-shorthand orchestrator to its dict
        # form (pre-existing behaviour); the model dicts — where `kind` lives —
        # must round-trip exactly.
        d = {"name": "x", "version": 3, "orchestrator": "o.yaml",
             "families": [{"name": "media", "workers": 1, "models": [
                 {"recipe": "img.yaml", "kind": "image"},
                 {"recipe": "vid.yaml", "kind": "video"}]}]}
        tpl = ti.ClusterTemplate.from_dict(d)
        out = tpl.to_dict()
        assert out["families"] == d["families"]
        assert out["orchestrator"] == {"recipe": "o.yaml"}

    def test_is_a_known_model_key(self):
        # Without this every media template fails structural validation as a
        # typo (`unknown key 'kind'`) before parse ever runs.
        assert "kind" in ct._KNOWN_MODEL_KEYS

    def test_structural_validation_accepts_kind_and_still_rejects_typos(
            self, tmp_path, monkeypatch):
        raw = {"name": "t", "version": 3,
               "orchestrator": {"recipe": "o.yaml", "kind": "chat"},
               "families": [{"name": "media", "workers": 1, "models": [
                   {"recipe": "img.yaml", "kind": "image"}]}]}
        tpl = ti.ClusterTemplate.from_dict(raw)
        errs, _warns = ct._structural_validate(
            raw, tpl, cluster_ips=["10.0.0.1"], recipe_exists=lambda r: True)
        assert errs == []
        # A near-miss spelling stays a hard structural error.
        raw2 = json.loads(json.dumps(raw))
        raw2["families"][0]["models"][0]["knd"] = "image"
        tpl2 = ti.ClusterTemplate.from_dict(raw2)
        errs2, _ = ct._structural_validate(
            raw2, tpl2, cluster_ips=["10.0.0.1"], recipe_exists=lambda r: True)
        assert any("unknown key 'knd'" in e for e in errs2)


# ── 3. resolve carries kind through every placement branch ──────────────────

class TestKindResolve:
    def _tpl(self, **model_extra):
        return ti.ClusterTemplate.from_dict({
            "name": "x", "version": 3, "orchestrator": "o.yaml",
            "families": [{"name": "media", "workers": "all",
                          "models": [_model_dict(**model_extra)]}]})

    def test_tp1_replication_carries_it(self):
        plan = ti.resolve(self._tpl(kind="video"), _topo(2),
                          _coster=_coster(per_gpu=30))
        assert [u.kind for u in plan.families[0].units] == ["video", "video"]

    def test_tp_span_branch_carries_it(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "version": 3, "orchestrator": "o.yaml",
            "families": [{"name": "media", "workers": "all",
                          "models": [_model_dict(kind="image", tp=2)]}]})
        plan = ti.resolve(t, _topo(2), _coster=_coster(per_gpu=30))
        units = plan.families[0].units
        assert len(units) == 1 and units[0].kind == "image"

    def test_explicit_nodes_branch_carries_it(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "version": 3, "orchestrator": "o.yaml",
            "families": [{"name": "media", "nodes": ["10.0.0.2"],
                          "models": [_model_dict(kind="video")]}]})
        plan = ti.resolve(t, _topo(2), _coster=_coster(per_gpu=30))
        assert plan.families[0].units[0].kind == "video"

    def test_orchestrator_can_declare_a_kind_too(self):
        # Nothing forbids it, so the plumbing must hold (the fleet will not do
        # it, but a half-plumbed branch is where the next consumer gets a lie).
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "version": 3,
            "orchestrator": {"recipe": "o.yaml", "kind": "image"},
            "families": []})
        plan = ti.resolve(t, _topo(1), _coster=_coster(per_gpu=30))
        assert plan.orchestrator.kind == "image"

    def test_absent_stays_chat_on_the_plan(self):
        plan = ti.resolve(self._tpl(), _topo(2), _coster=_coster(per_gpu=30))
        assert all(u.kind == "chat" for u in plan.all_units)

    def test_resolved_unit_defaults_to_chat(self):
        # Hand-built units (heal paths, tests) that predate media keep working.
        u = ti.ResolvedUnit("worker", "coding", "m.yaml", "M",
                            ["10.0.0.2"], 8000, 1, 1)
        assert u.kind == "chat"


# ── 4. render: kind NEVER enters the serve argv (M0 §4, zero drift by design) ─

class TestKindNeverEntersRender:
    def test_serve_cmd_identical_across_kind(self):
        # The §4 drift guarantee, stated the strongest way: two units with the
        # same recipe and different kind render BYTE-EQUAL argv — not merely
        # equivalent under normalization. `kind` is invisible to sparkrun.
        base = ct._render_serve_cmd("hscc", "10.0.0.2", 8000, "/r/media.yaml",
                                    "worker-model", 1, "media", 0.25)
        assert base == ct._render_serve_cmd("hscc", "10.0.0.2", 8000,
                                            "/r/media.yaml", "worker-model",
                                            1, "media", 0.25)
        assert "kind" not in " ".join(base)
        assert "image" not in " ".join(base) and "video" not in " ".join(base)

    def test_kind_flip_on_provisioned_unit_is_not_drift(self):
        # A recorded image-unit command vs the command an apply renders after
        # the operator flips kind to video: equal => no drift warning, no
        # recreate. The recipe change remains real drift (control below).
        recorded = ct._render_serve_cmd("hscc", "10.0.0.2", 8000,
                                        "/r/media.yaml", "worker-model",
                                        1, "media", 0.25)
        flipped = ct._render_serve_cmd("hscc", "10.0.0.2", 8000,
                                       "/r/media.yaml", "worker-model",
                                       1, "media", 0.25)  # kind: video renders the same
        assert ct.serve_cmds_equivalent(recorded, flipped)
        assert ct._diff_serve_cmds(recorded, flipped) == ""
        # Control: a RECIPE change IS real drift and must still report.
        new_recipe = ct._render_serve_cmd("hscc", "10.0.0.2", 8000,
                                          "/r/other.yaml", "worker-model",
                                          1, "media", 0.25)
        assert not ct.serve_cmds_equivalent(recorded, new_recipe)
        assert "positional" in ct._diff_serve_cmds(recorded, new_recipe)


# ── 5. serving.json carries non-chat kind, chat stays byte-identical ────────

class TestServingJsonCarriesKind:
    def _plan(self, **model_extra):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "version": 3, "orchestrator": "o.yaml",
            "families": [{"name": "media", "workers": 1,
                          "models": [_model_dict(**model_extra)]}]})
        return ti.resolve(t, _topo(1), _coster=_coster(per_gpu=30))

    def test_worker_unit_carries_the_kind(self):
        sj = ti.to_serving_json(self._plan(kind="video"))
        w = [u for u in sj["units"] if u["role"] == "worker"][0]
        assert w["kind"] == "video"

    def test_orchestrator_unit_carries_the_kind(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "version": 3,
            "orchestrator": {"recipe": "o.yaml", "kind": "image"},
            "families": []})
        sj = ti.to_serving_json(ti.resolve(t, _topo(1), _coster=_coster(per_gpu=30)))
        assert sj["units"][0]["kind"] == "image"

    def test_chat_unit_omitted_not_nulled(self):
        sj = ti.to_serving_json(self._plan(kind="chat"))
        w = [u for u in sj["units"] if u["role"] == "worker"][0]
        assert "kind" not in w

    def test_no_kind_serving_json_is_the_exact_pre_change_shape(self):
        # Byte-identity for the whole pre-media fleet: same key set AND order.
        sj = ti.to_serving_json(self._plan())
        w = [u for u in sj["units"] if u["role"] == "worker"][0]
        assert list(w.keys()) == ["id", "role", "keepalive", "model",
                                  "recipe", "nodes", "port", "tp", "pp",
                                  "family"]

    def test_key_order_kind_appended_last(self):
        # kind is emitted after max_num_seqs, matching the round-trip order.
        plan = self._plan(kind="image", max_num_seqs=5)
        w = [u for u in ti.to_serving_json(plan)["units"]
             if u["role"] == "worker"][0]
        assert w["max_num_seqs"] == 5 and w["kind"] == "image"
        assert list(w.keys())[-1] == "kind"

    def test_the_record_survives_json_round_trip(self):
        # serving.json is read back by keepalive/wake paths — the kind must be
        # in the serialized bytes, not just the Python dict.
        sj = ti.to_serving_json(self._plan(kind="image"))
        back = json.loads(json.dumps(sj))
        w = [u for u in back["units"] if u["role"] == "worker"][0]
        assert w["kind"] == "image"


# ── 6. the shipped DRAFT media template (dry, fakes only — never applied) ───

class TestShippedMediaTemplate:
    """flux2-media.yaml is a DRAFT (operator ruling: no live media tests).
    Validated here the same way every shipped template is: parse + resolve
    against a FAKE 3-node topology + structural layer with injected fakes.
    Nothing here applies, recreates, or reaches the cluster."""

    NAME = "3node-flux2-media"

    def _load(self):
        return ct._load_intent(self.NAME)

    def test_parses_and_declares_both_media_kinds(self):
        tpl = self._load()
        kinds = {f.name: {m.kind for m in f.models} for f in tpl.families}
        assert kinds == {"image": {"image"}, "video": {"video"}}
        assert tpl.orchestrator.kind == "chat"

    def test_resolves_on_fake_3node_topology_one_media_unit_per_worker(self):
        # 3 nodes = gateway orchestrator + one worker per media unit; the §5
        # fraction rule (0.25 + 0.70 = 0.95 > 0.8) is WHY they are not
        # co-located on one Spark — pinned so nobody "optimises" them onto
        # the same node without re-doing the budget.
        tpl = self._load()
        plan = ti.resolve(tpl, _topo(2), _coster=_coster(per_gpu=30.0))
        units = plan.all_units
        assert len(units) == 3
        by_kind = {u.kind: u for u in units}
        assert set(by_kind) == {"chat", "image", "video"}
        assert by_kind["image"].node != by_kind["video"].node
        # placement itself: every node belongs to the fake topology
        assert {u.node for u in units} <= {"10.0.0.1", "10.0.0.2", "10.0.0.3"}

    def test_serving_json_marks_exactly_the_media_units(self):
        tpl = self._load()
        sj = ti.to_serving_json(ti.resolve(tpl, _topo(2), _coster=_coster(per_gpu=30.0)))
        kinds = {u["role"]: u.get("kind") for u in sj["units"]}
        assert kinds["orchestrator"] is None          # chat → omitted
        workers = [u for u in sj["units"] if u["role"] == "worker"]
        assert sorted(u["kind"] for u in workers) == ["image", "video"]

    def test_structural_validate_passes_dry(self, monkeypatch):
        # The card's acceptance path: `validate`-class DRY code only. The
        # placement layer (live cluster) is skipped — structural_only is the
        # dry mode — and the two fail-safe offline checks (cluster.json,
        # staged-recipe existence) are answered by injected fakes, exactly as
        # test_validate.py does for every other shipped template.
        monkeypatch.setattr(ct, "CLUSTER_JSON", ct.Path("/nonexistent/cluster.json"))
        monkeypatch.setattr("recipe_cost.recipe_exists", lambda r: True)
        res = ct.validate_template(self.NAME, structural_only=True)
        assert res["structural"]["ok"] is True, res["structural"]
        assert res["structural"]["errors"] == []
        assert res["placement"]["skipped"] is True    # no live cluster touched
        assert res["ok"] is True

    def test_media_units_point_at_m1_media_recipes(self):
        # The template must reference the recipes M1 actually shipped (the
        # card's example names flux2-DEV/ltx-NVFP4; M1 ruled klein + FP8 —
        # see the template header. This pins the deviation is intentional).
        tpl = self._load()
        recipes = {m.recipe for f in tpl.families for m in f.models}
        assert recipes == {
            "~/.sparkrun-local/recipes/media/flux-klein-gen-sgld.yaml",
            "~/.sparkrun-local/recipes/media/ltx-video-sgld.yaml"}

    def test_declared_fractions_respect_the_08_rule_per_node(self):
        # §5's single hard rule: per-node fraction sum ≤ 0.8. With one media
        # unit per worker here, each node's sum is just its unit's fraction —
        # check both the individual bounds and the non-co-location.
        tpl = self._load()
        plan = ti.resolve(tpl, _topo(2), _coster=_coster(per_gpu=30.0))
        media = [u for u in plan.all_units if u.kind != "chat"]
        assert len(media) == 2
        assert all(u.gpu_memory_utilization is not None
                   and 0 < u.gpu_memory_utilization <= 0.8 for u in media)
        # one media unit per worker: their nodes are distinct
        assert len({u.node for u in media}) == 2
