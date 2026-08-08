"""Tests for the two-layer template validation (T4, t_939abe5f).

Layer 1 (structural) is OFFLINE: version recognised, unknown keys rejected,
every node in nodes: exists in cluster.json, len(nodes)==tp, no unflagged
colocation, routing targets resolve, recipes exist. It must never touch
discovery / the resolver / live cluster state.

Layer 2 (placement) is the existing resolver-dependent check.

The reason for the split (spec, "A related failure of trust"): the template
that currently RUNS the live fleet — 4node-dual-dsv4 via the inferred
resolver — fails its own validation with pool=[]. validate cannot tell
"template is malformed" from "resolver is broken". The structural layer fixes
that: it answers only about the template file, deterministically.
"""

import json
import os

import cluster_template as ct
import template_intent as ti

# Cluster.json compute nodes used across these tests (gateway .1 + workers.
# The exact same addresses the placement tests use for the 4-node layout).
COMPUTE = ["10.0.0.244", "10.0.0.245", "10.0.0.246",
           "10.0.0.247", "10.0.0.248"]


def _raw_tpl(d: dict):
    """Parse a template dict → (raw, parsed_ClusterTemplate)."""
    return d, ti.ClusterTemplate.from_dict(d)


# Stub recipe-existence predicate for the primitive structural-error tests so
# they isolate node/tp/key/routing/version checks from recipe-file availability
# (recipe existence is tested separately in TestStructuralRecipes).
EXISTS = lambda r: True


class TestStructuralErrors:
    def _check(self, d, cluster_ips=None):
        raw, tpl = _raw_tpl(d)
        return ct._structural_validate(raw, tpl, cluster_ips=cluster_ips,
                                       recipe_exists=EXISTS)

    def _base(self, **extra):
        d = {"name": "x", "version": 3, "orchestrator": "o.yaml",
             "families": [{"name": "reasoning",
                           "models": [{"recipe": "big.yaml", "tp": 2}],
                           "nodes": ["10.0.0.247", "10.0.0.248"]}]}
        d.update(extra)
        return d

    def test_ok_template(self):
        errs, warns = self._check(self._base(), COMPUTE)
        assert errs == []
        assert warns == []

    def test_unknown_node_fires(self):
        # node .250 is not in cluster.json
        d = self._base()
        d["families"][0]["nodes"] = ["10.0.0.250", "10.0.0.248"]
        errs, _ = self._check(d, COMPUTE)
        assert any("node '10.0.0.250' in family 'reasoning' is not defined "
                   "in cluster.json" in e for e in errs)

    def test_tp_mismatch_fires(self):
        # 2 nodes listed but tp=3
        d = self._base()
        d["families"][0]["models"][0]["tp"] = 3
        errs, _ = self._check(d, COMPUTE)
        assert any("family 'reasoning': 2 nodes listed but model tp=3" in e
                   for e in errs)

    def test_orchestrator_tp_mismatch_fires(self):
        d = self._base()
        d["orchestrator"] = {"recipe": "o.yaml", "tp": 2,
                             "nodes": ["10.0.0.244"]}
        errs, _ = self._check(d, COMPUTE)
        assert any("orchestrator: 1 nodes listed but tp=2" in e for e in errs)

    def test_unflagged_colision_fires(self):
        # reasoning names .248, and a second family ALSO names .248, neither
        # sets allow_colocation → hard error naming both units (bare names per
        # spec phrasing).
        d = self._base()
        d["families"].append({
            "name": "coding", "models": [{"recipe": "c.yaml", "tp": 1}],
            "nodes": ["10.0.0.248"]})
        errs, _ = self._check(d, COMPUTE)
        assert any(
            "node '10.0.0.248' claimed by both 'reasoning' and 'coding'"
            in e for e in errs)
        assert any("set allow_colocation: true on both to permit" in e
                   for e in errs)

    def test_dangling_routing_target_fires(self):
        # routing.delegation -> family-coding, but no coding family defined.
        d = self._base()
        d["routing"] = {"delegation": "family-coding"}
        errs, _ = self._check(d, COMPUTE)
        assert any(
            "routing.delegation -> 'family-coding': no such family in this "
            "template" in e for e in errs)

    def test_unknown_key_fires(self):
        d = self._base()
        d["familyz"] = [{"name": "c", "models": ["c.yaml"]}]  # stray/typo key
        errs, _ = self._check(d, COMPUTE)
        assert any("unknown key 'familyz'" in e for e in errs)

    def test_unknown_model_key_fires(self):
        d = self._base()
        d["families"][0]["models"][0]["tp_"] = 2
        errs, _ = self._check(d, COMPUTE)
        assert any("family 'reasoning' model 0: unknown key 'tp_'" in e
                   for e in errs)

    def test_version_unrecognised_fires(self):
        d = self._base(version=4)
        errs, _ = self._check(d, COMPUTE)
        assert any("version 4: unrecognised" in e for e in errs)


class TestColocationPermitted:
    """allow_colocation: true on BOTH units permits sharing and warns."""

    def _validate(self, d):
        return ct._structural_validate(*_raw_tpl(d), cluster_ips=COMPUTE,
                                       recipe_exists=EXISTS)

    def _both_allow(self):
        d = {"name": "x", "version": 3, "orchestrator": "o.yaml",
             "families": [
                 {"name": "reasoning",
                  "models": [{"recipe": "big.yaml", "tp": 1}],
                  "nodes": ["10.0.0.248"], "allow_colocation": True},
                 {"name": "vision",
                  "models": [{"recipe": "v.yaml", "tp": 1}],
                  "nodes": ["10.0.0.248"], "allow_colocation": True},
             ]}
        return d

    def test_both_allow_is_warning_not_error(self):
        errs, warns = self._validate(self._both_allow())
        assert errs == []
        assert any("allow_colocation: true on both" in w for w in warns)
        assert any("VRAM contention" in w for w in warns)

    def test_one_allow_still_errors(self):
        d = self._both_allow()
        d["families"][1]["allow_colocation"] = False  # only one says allow
        errs, warns = self._validate(d)
        # both units must allow → still a hard error
        assert any("claimed by both" in e for e in errs)
        assert warns == []


class TestStructuralRecipes:
    """Recipe existence is part of the structural layer (offline disk check)."""

    def test_missing_recipe_fires(self):
        d, tpl = _raw_tpl({"name": "x", "version": 3, "orchestrator": "o.yaml",
                           "families": [{"name": "f",
                                         "models": ["nope.yaml"]}]})
        errs, _ = ct._structural_validate(d, tpl, cluster_ips=COMPUTE)
        assert any("orchestrator recipe not found: o.yaml" in e for e in errs)
        assert any("family 'f' recipe not found: nope.yaml" in e for e in errs)

    def test_existing_recipe_ok(self, tmp_path):
        # Write a real recipe so Path(...).is_file() is true on disk.
        recipe = tmp_path / "r.yaml"
        recipe.write_text("model: m\n")
        recipe_orch = tmp_path / "o.yaml"
        recipe_orch.write_text("model: o\n")
        d, tpl = _raw_tpl({"name": "x", "version": 3,
                           "orchestrator": str(recipe_orch),
                           "families": [{"name": "f",
                                         "models": [str(recipe)]}]})
        errs, _ = ct._structural_validate(d, tpl, cluster_ips=[])
        assert errs == []


# ── 4node-dual-dsv4 fixture: explicit nodes vs inferring pool=[] ─────────────
# The canonical motivating case. A v3 4node-dual-dsv4 that names its spans
# explicitly MUST pass structural validation deterministically — while the
# INFERRING (version-2) version of the same template fails resolution with the
# resolver's pool=[] error. That contrast is exactly the point of the split.

EXPLICIT_4NODE = {
    "name": "4node-dual-dsv4", "version": 3,
    "orchestrator": {"recipe": "~/.sparkrun-local/recipes/local-fixed/"
                               "deepseek-v4-fp8-scitrera-hscc.yaml",
                     "tp": 2, "nodes": ["10.0.0.244", "10.0.0.246"]},
    "families": [{"name": "reasoning",
                  "models": [{"recipe": "~/.sparkrun-local/recipes/local-fixed/"
                                        "deepseek-v4-fp8-scitrera-hscc.yaml",
                              "tp": 2}],
                  "nodes": ["10.0.0.247", "10.0.0.248"]}],
}


class TestFourNodeDual:
    def test_explicit_4node_passes_structural(self):
        """The v3 explicit-nodes 4node-dual-dsv4 passes structural validation
        deterministically — no resolver, no live state, no pool=[]."""
        raw, tpl = _raw_tpl(EXPLICIT_4NODE)
        errs, warns = ct._structural_validate(raw, tpl, cluster_ips=COMPUTE,
                                              recipe_exists=EXISTS)
        assert errs == []
        assert warns == []

    def test_structural_only_succeeds_with_cluster_unreachable(self, monkeypatch):
        """validate_template(..., structural_only=True) returns structural ok
        even when the resolver/placement layer would fail (pool=[]). The whole
        point: validate must not depend on resolver health."""
        # Emulate the broken/inaccessible resolver: _resolve raises the exact
        # pool=[] error 4node-dual-dsv4 produced in production. Since
        # structural_only never calls _resolve, this patch must never fire.
        def broken_resolve(*a, **k):
            raise AssertionError("_resolve must not be called under "
                                 "--structural-only")
        monkeypatch.setattr(ct, "_resolve", broken_resolve)
        # structural validates recipes via Path.is_file; stub them to exist so
        # the structural layer isn't flagged for missing recipe files.
        monkeypatch.setattr(ct.Path, "is_file", lambda self: True)

        res = ct.validate_template("4node-dual-dsv4", structural_only=True)
        assert res["structural"]["ok"] is True
        assert res["structural"]["errors"] == []
        assert res["placement"].get("skipped") is True
        assert res["ok"] is True

    def test_non_structural_reports_placement_pool_empty(self, monkeypatch):
        """validate_template WITHOUT --structural-only runs layer 2, which — on
        the inferring/broken resolver — reports the pool=[] placement error
        while structural stays green. This is the legibility the spec wanted."""
        def broken_resolve(*a, **k):
            raise ti.TemplateIntentError(
                "family 'reasoning': no available worker nodes "
                "(pool=[], claimed=['10.0.0.247'])")
        monkeypatch.setattr(ct, "_resolve", broken_resolve)
        monkeypatch.setattr(ct.Path, "is_file", lambda self: True)

        res = ct.validate_template("4node-dual-dsv4")
        assert res["structural"]["ok"] is True
        assert res["placement"]["ok"] is False
        assert any("pool=[]" in e for e in res["placement"]["errors"])
        assert res["ok"] is False

    def test_shipped_dual_dsv4_parses_v3_and_validates(self, monkeypatch):
        """The SHIPPED templates/4node/dual-dsv4.yaml parses as version 3 with
        the routing block (delegation → family-reasoning) and NO explicit nodes,
        and passes structural validation. This is the durable encoding of the
        live-config fix: delegation must route to the reasoning family, not the
        orchestrator. Structural (offline) — no resolver, no live state."""
        import yaml as _yaml
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(os.path.dirname(here), "templates", "4node",
                            "dual-dsv4.yaml")
        with open(path) as fh:
            raw = _yaml.safe_load(fh)

        # parses as v3 with the declared routing block
        tpl = ti.ClusterTemplate.from_dict(raw)
        assert tpl.version == 3
        assert tpl.routing == {"delegation": "family-reasoning",
                               "compaction": "orchestrator",
                               "auxiliaries": "orchestrator"}
        # symbolic + portable: no explicit node pins (else reusable 1-8 node
        # library would be broken by this cluster's hardcoded IPs)
        assert tpl.orchestrator.nodes is None
        assert tpl.families[0].nodes is None

        # structural validation of the shipped file (offline)
        monkeypatch.setattr(ct.Path, "is_file", lambda self: True)  # recipes exist
        errs, warns = ct._structural_validate(raw, tpl, cluster_ips=COMPUTE,
                                              recipe_exists=EXISTS)
        assert errs == []
        assert warns == []


# ── validate_template end-to-end (real template file, real cluster.json) ─────

class TestValidateTemplateE2E:
    """validate_template against a real temp template file + cluster.json,
    exercising the offline path (structural OK without any cluster state) and
    the placement sub-layer labels."""

    def _setup(self, tmp_path, monkeypatch, template_name, yaml_text,
               cluster_json=None):
        tdir = tmp_path / "templates"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / f"{template_name}.yaml").write_text(yaml_text)
        monkeypatch.setattr(ct, "TEMPLATE_DIR", tdir)
        if cluster_json is not None:
            cj = tmp_path / "cluster.json"
            cj.write_text(json.dumps(cluster_json))
            monkeypatch.setattr(ct, "CLUSTER_JSON", cj)

    def test_structural_only_ok_with_no_cluster_state(self, tmp_path, monkeypatch):
        """--structural-only must succeed even with the cluster unreachable: no
        cluster.json file, no discovery — structural validates purely from the
        template file + recipe existence. Here recipes are made to exist via
        Path.is_file stub so nothing depends on disk state either."""
        self._setup(tmp_path, monkeypatch, "t", (
            "name: t\nversion: 3\norchestrator:\n  recipe: o.yaml\n  tp: 2\n"
            "  nodes: [10.0.0.244, 10.0.0.246]\n"
            "families:\n  - name: reasoning\n    models:\n      - recipe: big.yaml\n"
            "        tp: 2\n    nodes: [10.0.0.247, 10.0.0.248]\n"))
        # No cluster.json written → structural sees no compute nodes; explicit
        # spans reference nodes not in that empty set → would fail. Instead
        # provide a cluster.json so the "exists in cluster.json" check passes,
        # and stub recipes to exist. The point being tested: no RESOLVER/no
        # discovery is touched and the run succeeds offline.
        cj = {"name": "hscc",
              "gateway": {"ip": "10.0.0.244"},
              "workers": [
                  {"ip": "10.0.0.246"}, {"ip": "10.0.0.247"},
                  {"ip": "10.0.0.248"}],
              "nasDevices": [{"ip": "10.0.0.99"}]}
        monkeypatch.setattr(ct, "CLUSTER_JSON",
                            _write_cluster(tmp_path, cj))
        monkeypatch.setattr(ct.Path, "is_file", lambda self: True)

        res = ct.validate_template("t", structural_only=True)
        assert res["structural"]["ok"] is True, res
        assert res["structural"]["errors"] == []
        assert res["placement"].get("skipped") is True
        assert res["ok"] is True

    def test_full_shape_two_layers(self, tmp_path, monkeypatch):
        """Non-structural validate returns both layers in the spec shape, with
        placement failing from the resolver (monkeypatched) and structural green.
        exit-failure is surfaced as top-level ok=False."""
        self._setup(tmp_path, monkeypatch, "t", (
            "name: t\nversion: 3\norchestrator:\n  recipe: o.yaml\n  tp: 2\n"
            "  nodes: [10.0.0.244, 10.0.0.246]\n"
            "families:\n  - name: reasoning\n    models:\n      - recipe: big.yaml\n"
            "        tp: 2\n    nodes: [10.0.0.247, 10.0.0.248]\n"))
        monkeypatch.setattr(ct, "CLUSTER_JSON",
                            _write_cluster(tmp_path, {
                                "gateway": {"ip": "10.0.0.244"},
                                "workers": [{"ip": "10.0.0.246"},
                                            {"ip": "10.0.0.247"},
                                            {"ip": "10.0.0.248"}]}))
        monkeypatch.setattr(ct.Path, "is_file", lambda self: True)

        def broken_resolve(*a, **k):
            raise ti.TemplateIntentError("family 'reasoning': pool=[]")
        monkeypatch.setattr(ct, "_resolve", broken_resolve)

        res = ct.validate_template("t")
        assert res["template"] == "t"
        assert res["structural"]["ok"] is True
        assert res["placement"]["ok"] is False
        assert res["placement"]["errors"] == ["family 'reasoning': pool=[]"]
        assert res["ok"] is False

    def test_nas_ip_not_a_compute_node_fires(self, tmp_path, monkeypatch):
        """A NAS storage IP named in nodes: is a structural error (models cannot
        be placed on storage)."""
        self._setup(tmp_path, monkeypatch, "t", (
            "name: t\nversion: 3\norchestrator:\n  recipe: o.yaml\n"
            "families:\n  - name: f\n    models:\n      - recipe: big.yaml\n"
            "    nodes: [10.0.0.99]\n"))
        monkeypatch.setattr(ct, "CLUSTER_JSON",
                            _write_cluster(tmp_path, {
                                "gateway": {"ip": "10.0.0.244"},
                                "workers": [{"ip": "10.0.0.247"}],
                                "nasDevices": [{"ip": "10.0.0.99"}]}))
        monkeypatch.setattr(ct.Path, "is_file", lambda self: True)
        res = ct.validate_template("t", structural_only=True)
        assert res["structural"]["ok"] is False
        assert any("node '10.0.0.99' in family 'f' is not defined in cluster.json"
                   in e for e in res["structural"]["errors"])


def _write_cluster(tmp_path, cj: dict):
    p = tmp_path / "cluster.json"
    p.write_text(json.dumps(cj))
    return p
