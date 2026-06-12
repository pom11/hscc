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
