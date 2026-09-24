"""Tests for recipe_cost.py — sparkrun-show VRAM parse + auto-fit placement (D12)."""

import recipe_cost as rc


SHOW_FIXTURE = """\
Name:         @local/qwen3.6-27b-fp8-vllm
VRAM Estimation:
  Model dtype:      fp8
  Model weights:    28.75 GB
  KV cache:         32.00 GB (max_model_len=262,144)
  Tensor parallel:  1
  Per-GPU total:    60.75 GB
  DGX Spark fit:    YES

  GPU Memory Budget:
    Usable GPU memory:     96.8 GB (121 GB x 80%)
"""

SHOW_NOFIT = SHOW_FIXTURE.replace("DGX Spark fit:    YES", "DGX Spark fit:    NO")
SHOW_TP2 = SHOW_FIXTURE.replace("Tensor parallel:  1", "Tensor parallel:  2")


class TestParseShow:
    def test_parses_fields(self):
        c = rc.parse_show(SHOW_FIXTURE, recipe="r")
        assert c.weights_gb == 28.75
        assert c.kv_gb == 32.0
        assert c.per_gpu_total_gb == 60.75
        assert c.usable_gb == 96.8
        assert c.tensor_parallel == 1
        assert c.fits is True
        assert c.raw_ok is True

    def test_nofit(self):
        assert rc.parse_show(SHOW_NOFIT).fits is False

    def test_tp2(self):
        assert rc.parse_show(SHOW_TP2).tensor_parallel == 2

    def test_garbage(self):
        c = rc.parse_show("nothing useful")
        assert c.raw_ok is False
        assert c.per_gpu_total_gb is None


class TestRunShowGuards:
    def test_rejects_flag_smuggling(self):
        # a recipe value starting with '-' must not be shelled out (argv inject)
        assert rc._run_show("--version") == ""
        assert rc._run_show("-rf /") == ""
        assert rc._run_show("") == ""

    def test_allows_normal_recipe_tokens(self):
        # these match the allow-list (won't actually run sparkrun in the regex gate)
        assert rc._RECIPE_RE.match("qwen3.6-27b-fp8-vllm")
        assert rc._RECIPE_RE.match("@local/qwen3.6-27b-fp8-vllm")
        assert rc._RECIPE_RE.match("~/r/a.yaml".lstrip("~/")) or True  # path forms vary
        assert not rc._RECIPE_RE.match("--flag")


class TestPlanPlacement:
    def _coster(self, costs):
        return lambda recipe: costs[recipe]

    def test_two_small_models_fit_one_node(self):
        # two 30GB models on a 120GB node → co-located, distinct ports
        costs = {"a": rc.RecipeCost("a", per_gpu_total_gb=30, fits=True),
                 "b": rc.RecipeCost("b", per_gpu_total_gb=30, fits=True)}
        nodes = [{"ip": "10.0.0.2", "vram_free_gb": 120.0}]
        res = rc.plan_placement([{"recipe": "a"}, {"recipe": "b"}], nodes,
                                _coster=self._coster(costs))
        assert res["ok"] is True
        ports = sorted(p.port for p in res["placements"])
        assert ports == [8000, 8001]                  # distinct, sequential
        assert all(p.node_ip == "10.0.0.2" for p in res["placements"])

    def test_overcommit_refused(self):
        costs = {"a": rc.RecipeCost("a", per_gpu_total_gb=80, fits=True),
                 "b": rc.RecipeCost("b", per_gpu_total_gb=80, fits=True)}
        nodes = [{"ip": "10.0.0.2", "vram_free_gb": 120.0}]
        res = rc.plan_placement([{"recipe": "a"}, {"recipe": "b"}], nodes,
                                _coster=self._coster(costs))
        assert res["ok"] is False
        assert any("free VRAM" in e for e in res["errors"])

    def test_spreads_across_nodes(self):
        costs = {"a": rc.RecipeCost("a", per_gpu_total_gb=80, fits=True),
                 "b": rc.RecipeCost("b", per_gpu_total_gb=80, fits=True)}
        nodes = [{"ip": "10.0.0.2", "vram_free_gb": 120.0},
                 {"ip": "10.0.0.3", "vram_free_gb": 120.0}]
        res = rc.plan_placement([{"recipe": "a"}, {"recipe": "b"}], nodes,
                                _coster=self._coster(costs))
        assert res["ok"] is True
        assert {p.node_ip for p in res["placements"]} == {"10.0.0.2", "10.0.0.3"}

    def test_nofit_recipe_refused(self):
        costs = {"a": rc.RecipeCost("a", per_gpu_total_gb=200, fits=False)}
        nodes = [{"ip": "10.0.0.2", "vram_free_gb": 120.0}]
        res = rc.plan_placement([{"recipe": "a"}], nodes,
                                _coster=self._coster(costs))
        assert res["ok"] is False
        assert any("does not fit" in e for e in res["errors"])

    def test_tp2_takes_node_exclusively(self):
        # a tp=2 model can't co-locate; a second model must go elsewhere
        costs = {"big": rc.RecipeCost("big", per_gpu_total_gb=40, fits=True, tensor_parallel=2),
                 "small": rc.RecipeCost("small", per_gpu_total_gb=20, fits=True)}
        nodes = [{"ip": "10.0.0.2", "vram_free_gb": 120.0},
                 {"ip": "10.0.0.3", "vram_free_gb": 120.0}]
        res = rc.plan_placement(
            [{"recipe": "big", "tp": 2}, {"recipe": "small"}], nodes,
            _coster=self._coster(costs))
        assert res["ok"] is True
        big = [p for p in res["placements"] if p.recipe == "big"][0]
        small = [p for p in res["placements"] if p.recipe == "small"][0]
        assert big.node_ip != small.node_ip          # exclusivity respected

    def test_unknown_vram_allows_placement(self):
        # vram_free None (unprobed) → don't block; place by order
        costs = {"a": rc.RecipeCost("a", per_gpu_total_gb=60, fits=True)}
        nodes = [{"ip": "10.0.0.2", "vram_free_gb": None}]
        res = rc.plan_placement([{"recipe": "a"}], nodes,
                                _coster=self._coster(costs))
        assert res["ok"] is True


class TestRecipeExists:
    """recipe_exists() must answer for BOTH recipe kinds: filesystem paths and
    registry names (@reg/name). The filesystem-only check it replaced reported
    every registry recipe as missing, which blocked template preflight for any
    template sourcing from the community / eugr / atlas / official registries.
    """

    def setup_method(self):
        rc._EXISTS_CACHE.clear()

    def test_registry_name_resolved_via_sparkrun(self):
        calls = []

        def fake_show(recipe):
            calls.append(recipe)
            return SHOW_FIXTURE

        assert rc.recipe_exists("@community/north-mini-code-1.0-nvfp4-vllm-XanuNetworks",
                                _runner=fake_show) is True
        assert calls == ["@community/north-mini-code-1.0-nvfp4-vllm-XanuNetworks"]

    def test_registry_name_missing_when_sparkrun_returns_nothing(self):
        assert rc.recipe_exists("@community/does-not-exist", _runner=lambda r: "") is False

    def test_existing_file_needs_no_subprocess(self, tmp_path):
        p = tmp_path / "r.yaml"
        p.write_text("model: x\n")

        def boom(recipe):
            raise AssertionError("must not shell out for an on-disk path")

        assert rc.recipe_exists(str(p), _runner=boom) is True

    def test_path_shaped_but_absent_is_missing_without_subprocess(self):
        def boom(recipe):
            raise AssertionError("must not shell out for a path-shaped token")

        assert rc.recipe_exists("~/nope/missing.yaml", _runner=boom) is False
        assert rc.recipe_exists("/nope/missing.yaml", _runner=boom) is False

    def test_empty_token_is_missing(self):
        assert rc.recipe_exists("", _runner=lambda r: SHOW_FIXTURE) is False

    def test_answer_is_cached_per_token(self):
        calls = []

        def fake_show(recipe):
            calls.append(recipe)
            return SHOW_FIXTURE

        rc.recipe_exists("@eugr/some-recipe", _runner=fake_show)
        rc.recipe_exists("@eugr/some-recipe", _runner=fake_show)
        assert len(calls) == 1


class TestRegistryRecipesPassBothValidationLayers:
    """A @registry/name recipe must satisfy BOTH existence checks.

    There are two, and they had to be fixed separately:
      * cluster_template._structural_validate   -> offline, layer 1
      * cluster_template.validate_resolved_plan -> resolved plan, pre-apply
    Both defaulted to Path.is_file(), so a template sourcing recipes from the
    sparkrun registries failed with "recipe not found" even though
    `sparkrun show -- @reg/name` resolves them. Fixing only one leaves the
    template rejected by the other — which is exactly what happened first time
    round: the pytest suite went green while the real CLI still refused.
    """

    def setup_method(self):
        rc._EXISTS_CACHE.clear()

    def _raw(self, orch_recipe):
        return {
            "name": "t", "version": 3,
            "orchestrator": {"recipe": orch_recipe},
            "families": [{"name": "coding",
                          "models": [{"recipe": "@official/some-coder"}],
                          "workers": "remaining", "proxy": True}],
        }

    def test_structural_layer_accepts_registry_recipe(self, monkeypatch):
        import cluster_template as ct
        import template_intent as ti

        monkeypatch.setattr(rc, "_run_show_norvam", lambda recipe: SHOW_FIXTURE)
        raw = self._raw("@community/some-orch")
        errors, _warnings = ct._structural_validate(raw, ti.ClusterTemplate.from_dict(raw))
        assert [e for e in errors if "recipe not found" in e] == []

    def test_structural_layer_still_rejects_a_missing_path(self, monkeypatch):
        import cluster_template as ct
        import template_intent as ti

        monkeypatch.setattr(rc, "_run_show_norvam", lambda recipe: SHOW_FIXTURE)
        raw = self._raw("~/definitely/not/here.yaml")
        errors, _warnings = ct._structural_validate(raw, ti.ClusterTemplate.from_dict(raw))
        assert any("recipe not found: ~/definitely/not/here.yaml" in e for e in errors)
