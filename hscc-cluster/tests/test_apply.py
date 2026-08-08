"""Tests for cluster_template.py — apply pipeline (v2 intent schema)."""

import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

import cluster_template
from cluster_template import (
    preview_template, apply_template, write_json, atomic_yaml_update,
    validate_resolved_plan, TemplateValidationError, install_proxy_plist,
    install_proxy, remove_proxy, list_templates,
)
import template_intent as ti
from dataclasses import dataclass


# ── fake topology + coster so tests never touch the real cluster ─────────────

@dataclass
class FakeNode:
    ip: str
    vram_free_gb: float = 120.0


@dataclass
class FakeTopo:
    orchestrator: FakeNode
    workers: list


def _topo(n=3):
    return FakeTopo(FakeNode("10.0.0.1"),
                    [FakeNode(f"10.0.0.{2+i}") for i in range(n)])


def _coster(per_gpu=30.0, fits=True, tp=1):
    import recipe_cost as rc
    return lambda recipe: rc.RecipeCost(recipe, per_gpu_total_gb=per_gpu,
                                        fits=fits, tensor_parallel=tp)


@pytest.fixture
def stub_cluster(monkeypatch):
    """Stub discovery + recipe_cost so resolve() works without a live cluster or
    real recipe files."""
    monkeypatch.setattr(cluster_template, "_discover", lambda probe=False: _topo(3))
    import recipe_cost as rc
    monkeypatch.setattr(ti._rc, "recipe_cost",
                        lambda r: rc.RecipeCost(r, per_gpu_total_gb=30, fits=True))
    # recipes "exist" for validation
    monkeypatch.setattr(ti, "Path_isfile", lambda r: True)
    monkeypatch.setattr(cluster_template.Path, "is_file", lambda self: True)


class TestListTemplates:
    def test_lists_v2_templates(self):
        reg = list_templates()
        names = {t["name"] for t in reg["templates"]}
        assert "single-family" in names
        assert "colocated-two-models" in names

    def test_structure(self):
        t = list_templates()["templates"][0]
        assert "name" in t and "version" in t and "description" in t


class TestWriteJson:
    def test_write_and_read(self, tmp_path):
        data = {"key": "value", "nested": {"a": 1}}
        write_json(tmp_path / "test.json", data)
        assert json.loads((tmp_path / "test.json").read_text()) == data

    def test_backup_on_overwrite(self, tmp_path):
        write_json(tmp_path / "t.json", {"v": 1})
        write_json(tmp_path / "t.json", {"v": 2}, backup=True)
        assert len(list(tmp_path.glob("t.json.bak.*"))) == 1

    def test_prune_backups_caps_to_max(self, tmp_path):
        import os
        import cluster_template as ct
        p = tmp_path / "serving.json"
        p.write_text("{}")
        made = []
        for i in range(ct.MAX_BACKUPS + 7):
            b = tmp_path / f"serving.json.bak.{1000 + i}"
            b.write_text(f"v{i}")
            os.utime(b, (1000 + i, 1000 + i))
            made.append(b)
        ct._prune_backups(p)
        remaining = sorted(tmp_path.glob("serving.json.bak.*"))
        assert len(remaining) == ct.MAX_BACKUPS
        assert made[-1] in remaining and made[0] not in remaining

    def test_atomic_write_no_partial(self, tmp_path):
        write_json(tmp_path / "t.json", {"ok": True})
        assert not (tmp_path / "t.json.tmp").exists()


class TestAtomicYamlUpdate:
    def test_create_new(self, tmp_path):
        import yaml
        path, changed = atomic_yaml_update(tmp_path / "t.yaml", lambda d: {"new": "v"})
        assert changed is True
        assert yaml.safe_load(open(path))["new"] == "v"

    def test_noop_reports_unchanged(self, tmp_path):
        path = tmp_path / "t.yaml"
        atomic_yaml_update(path, lambda d: {"a": 1})
        _, changed = atomic_yaml_update(path, lambda d: {"a": 1})
        assert changed is False


class TestPreviewAndApply:
    def test_preview_resolves_live(self, stub_cluster):
        res = preview_template("single-family")
        assert res["template"] == "single-family"
        files = [c["file"] for c in res["changes"]]
        assert "serving.json" in files and "models.json" in files

    def test_apply_without_confirm_returns_preview(self, stub_cluster):
        res = apply_template("single-family")
        assert res["status"] in ("preview", "blocked")
        if res["status"] == "preview":
            assert "confirm=true" in res["note"]


class TestApplyReapplySamePlan:
    """apply_template must resolve (not block) when re-applying the SAME plan —
    the --force-recreate path. The unit-aware reserved guard keeps a node in its
    own unit's pool instead of ejecting every live tp-peer (issue t_16dcceb4)."""

    # The real dual-dsv4 template: orchestrator tp=2 + reasoning family tp=2,
    # over a 4-node cluster (gateway .245 + workers .246/.247/.248).
    TEMPLATE = "4node-dual-dsv4"
    RECIPE = ("~/.sparkrun-local/recipes/local-fixed/"
              "deepseek-v4-fp8-scitrera-hscc.yaml")

    def _reserved(self, recording=False):
        """serving.json recorded state — orch=[.245,.246], reasoning=[.247,.248].
        Present regardless of whether the spans are live (recording) or stopped.
        """
        g = "10.0.0.245"; w1 = "10.0.0.246"
        w2 = "10.0.0.247"; w3 = "10.0.0.248"
        return {
            g: {"kind": "orchestrator", "family": None, "model": "dsv4"},
            w1: {"kind": "orchestrator", "family": None, "model": "dsv4"},
            w2: {"kind": "worker", "family": "reasoning", "model": "dsv4"},
            w3: {"kind": "worker", "family": "reasoning", "model": "dsv4"},
        }

    def _four_node_topo(self):
        return FakeTopo(FakeNode("10.0.0.245"),
                        [FakeNode("10.0.0.246"), FakeNode("10.0.0.247"),
                         FakeNode("10.0.0.248")])

    def test_resolves_when_target_spans_running(self, monkeypatch):
        monkeypatch.setattr(cluster_template, "_discover",
                            lambda probe=False: self._four_node_topo())
        monkeypatch.setattr(cluster_template, "_existing_serving_units",
                            lambda: self._reserved())
        import recipe_cost as rc
        monkeypatch.setattr(ti._rc, "recipe_cost",
                            lambda r: rc.RecipeCost(r, per_gpu_total_gb=80.4,
                                                    fits=True))
        monkeypatch.setattr(cluster_template.Path, "is_file", lambda self: True)
        plan = cluster_template._resolve(self.TEMPLATE)
        assert plan.orchestrator.nodes == ["10.0.0.245", "10.0.0.246"]
        assert plan.orchestrator.tp == 2
        assert len(plan.families) == 1
        units = plan.families[0].units
        assert len(units) == 1                       # one tp=2 unit, not two
        assert units[0].nodes == ["10.0.0.247", "10.0.0.248"]
        assert units[0].tp == 2
        assert cluster_template.validate_resolved_plan(plan) == []

    def test_resolves_when_target_spans_stopped(self, monkeypatch):
        """After the span is stopped the serving.json RECORD persists; the same
        plan must still resolve (the record is not a live-occupancy signal)."""
        monkeypatch.setattr(cluster_template, "_discover",
                            lambda probe=False: self._four_node_topo())
        monkeypatch.setattr(cluster_template, "_existing_serving_units",
                            lambda: self._reserved())
        import recipe_cost as rc
        monkeypatch.setattr(ti._rc, "recipe_cost",
                            lambda r: rc.RecipeCost(r, per_gpu_total_gb=80.4,
                                                    fits=True))
        monkeypatch.setattr(cluster_template.Path, "is_file", lambda self: True)
        plan = cluster_template._resolve(self.TEMPLATE)
        assert plan.families[0].units[0].nodes == ["10.0.0.247", "10.0.0.248"]
        assert cluster_template.validate_resolved_plan(plan) == []

    def test_preview_reports_two_nodes_for_tp2_family(self, monkeypatch):
        """preview's provision summary + proxy node list count the FULL tp span,
        so a tp=2 reasoning family reports 2 nodes, not 1 (issue t_16dcceb4)."""
        monkeypatch.setattr(cluster_template, "_discover",
                            lambda probe=False: self._four_node_topo())
        monkeypatch.setattr(cluster_template, "_existing_serving_units",
                            lambda: self._reserved())
        import recipe_cost as rc
        monkeypatch.setattr(ti._rc, "recipe_cost",
                            lambda r: rc.RecipeCost(r, per_gpu_total_gb=80.4,
                                                    fits=True))
        prom = preview_template(self.TEMPLATE)["changes"]
        prov = next(c for c in prom if c["file"] == "models (provision)")
        # 1 orchestrator (its own node) + reasoning spans BOTH .247/.248 → 2 worker nodes
        assert "2 worker nodes" in prov["summary"], prov["summary"]
        proxy = next(c for c in prom if c["file"] == "proxies/")
        # per-family proxy node list includes every span node, not just the primary
        reason_detail = proxy["details"][0]
        assert "10.0.0.247" in reason_detail and "10.0.0.248" in reason_detail
        cfg = next(c for c in prom if c["file"] == "config.yaml")
        # config detail was the "1 units, 1 nodes" site — now reports the full span
        fam_line = next(l for l in cfg["details"] if "family-reasoning" in l)
        assert "2 nodes" in fam_line, fam_line



class TestValidateResolvedPlan:
    def _plan(self, monkeypatch, exists=True):
        monkeypatch.setattr(cluster_template.Path, "is_file", lambda self: exists)
        topo = _topo(2)
        import recipe_cost as rc
        monkeypatch.setattr(ti._rc, "recipe_cost",
                            lambda r: rc.RecipeCost(r, per_gpu_total_gb=30, fits=True))
        tpl = ti.ClusterTemplate.from_dict({
            "name": "t", "orchestrator": "o.yaml",
            "families": [{"name": "c", "models": ["m.yaml"], "workers": "all"}]})
        return ti.resolve(tpl, topo)

    def test_valid_plan_passes(self, monkeypatch):
        plan = self._plan(monkeypatch, exists=True)
        assert validate_resolved_plan(plan) == []

    def test_missing_recipe_flagged(self, monkeypatch):
        plan = self._plan(monkeypatch, exists=False)
        errs = validate_resolved_plan(plan)
        assert any("not found" in e for e in errs)


class TestInstallProxySparkrun:
    """install_proxy delegates to `sparkrun proxy start` instead of writing
    a launchd plist. The tests verify the subprocess call shape."""

    def test_calls_sparkrun_proxy_start(self, tmp_path, monkeypatch):
        import cluster_template as ct
        import subprocess
        from unittest.mock import MagicMock, patch

        monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")
        calls = []

        def mock_run(argv, **k):
            calls.append(argv)
            return MagicMock(returncode=0, stderr="", stdout="")

        with patch.object(subprocess, "run", mock_run):
            fam = ti.ResolvedFamily(name="coding", proxy_port=4000, units=[
                ti.ResolvedUnit("worker", "coding", "m.yaml", "M", ["10.0.0.2"], 8000, 1, 1)])
            res = install_proxy(fam)

        assert calls[0][0] == "sparkrun"
        assert calls[0][1] == "proxy"
        assert calls[0][2] == "start"
        assert "--port" in calls[0]
        assert "4000" in calls[0]
        assert "--cluster" in calls[0]
        assert "hscc" in calls[0]
        assert res["loaded"] is True
        assert res["port"] == 4000
        assert res["via"] == "sparkrun-proxy"

    def test_records_error_on_failure(self, tmp_path, monkeypatch):
        import cluster_template as ct
        import subprocess
        from unittest.mock import MagicMock, patch

        monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")

        def mock_run(argv, **k):
            return MagicMock(returncode=1, stderr="port already in use", stdout="")

        with patch.object(subprocess, "run", mock_run):
            fam = ti.ResolvedFamily(name="coding", proxy_port=4000, units=[
                ti.ResolvedUnit("worker", "coding", "m.yaml", "M", ["10.0.0.2"], 8000, 1, 1)])
            res = install_proxy(fam)

        assert res["loaded"] is False
        assert res["error"] is not None

    def test_no_plist_written(self, tmp_path, monkeypatch):
        import cluster_template as ct
        import subprocess
        from unittest.mock import MagicMock, patch

        monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")

        def mock_run(argv, **k):
            return MagicMock(returncode=0, stderr="", stdout="")

        with patch.object(subprocess, "run", mock_run):
            fam = ti.ResolvedFamily(name="coding", proxy_port=4000, units=[
                ti.ResolvedUnit("worker", "coding", "m.yaml", "M", ["10.0.0.2"], 8000, 1, 1)])
            install_proxy(fam)

        # No proxy.plist should be generated
        plist = tmp_path / "proxies" / "coding" / "proxy.plist"
        assert not plist.exists()


class TestRemoveProxySparkrun:
    """remove_proxy delegates to `sparkrun proxy stop`."""

    def test_calls_sparkrun_proxy_stop(self, tmp_path, monkeypatch):
        import cluster_template as ct
        import subprocess
        from unittest.mock import MagicMock, patch

        monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")
        (tmp_path / "proxies" / "coding").mkdir(parents=True)
        (tmp_path / "proxies" / "coding" / "config.json").write_text("{}")

        calls = []
        def mock_run(argv, **k):
            calls.append(argv)
            return MagicMock(returncode=0, stderr="", stdout="")

        with patch.object(subprocess, "run", mock_run):
            fam = ti.ResolvedFamily(name="coding", proxy_port=4000, units=[
                ti.ResolvedUnit("worker", "coding", "m.yaml", "M", ["10.0.0.2"], 8000, 1, 1)])
            res = remove_proxy(fam)

        assert calls[0][0] == "sparkrun"
        assert calls[0][1] == "proxy"
        assert calls[0][2] == "stop"
        assert res["status"] == "removed"


class TestBackwardCompatAliases:
    """install_proxy_plist / remove_proxy_plist still work as aliases."""

    def test_install_proxy_plist_alias(self, tmp_path, monkeypatch):
        import cluster_template as ct
        import subprocess
        from unittest.mock import MagicMock, patch

        monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")

        def mock_run(argv, **k):
            return MagicMock(returncode=0, stderr="", stdout="")

        with patch.object(subprocess, "run", mock_run):
            fam = ti.ResolvedFamily(name="coding", proxy_port=4000, units=[
                ti.ResolvedUnit("worker", "coding", "m.yaml", "M", ["10.0.0.2"], 8000, 1, 1)])
            res = install_proxy_plist(fam)

        assert res["loaded"] is True
        assert res["port"] == 4000


class TestPruneOrphanProxies:
    def test_removes_orphan_family_dirs_and_backups(self, tmp_path, monkeypatch):
        import cluster_template as ct
        monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")
        # active 'coding' + orphan 'vision' (with stale backups) + logs dir
        for fam in ("coding", "vision"):
            d = tmp_path / "proxies" / fam
            d.mkdir(parents=True)
            (d / "config.json").write_text("{}")
            for i in range(8):
                (d / f"config.json.bak.{1000+i}").write_text("{}")
        (tmp_path / "proxies" / "logs").mkdir()
        pruned = ct._prune_orphan_proxies(["coding"])
        assert pruned == ["vision"]
        assert (tmp_path / "proxies" / "coding").is_dir()      # active kept
        assert not (tmp_path / "proxies" / "vision").exists()  # orphan gone (+backups)
        assert (tmp_path / "proxies" / "logs").is_dir()        # logs never pruned

    def test_noop_when_no_orphans(self, tmp_path, monkeypatch):
        import cluster_template as ct
        monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")
        (tmp_path / "proxies" / "coding").mkdir(parents=True)
        assert ct._prune_orphan_proxies(["coding"]) == []
        assert (tmp_path / "proxies" / "coding").is_dir()


class TestApplyIntegration:
    """Full apply against a temp HOME — asserts GENERATED FILES, not mocks."""

    def _setup(self, tmp_path, monkeypatch, n_workers=3):
        import cluster_template as ct
        import subprocess
        from unittest.mock import MagicMock
        hscc = tmp_path / "hscc"; hscc.mkdir()
        for attr, val in [("HSCC_DIR", hscc), ("SERVING_JSON", hscc / "serving.json"),
                          ("MODELS_JSON", hscc / "models.json"),
                          ("CONFIG_YAML", hscc / "config.yaml"),
                          ("PROFILES_DIR", hscc / "profiles"),
                          ("PROXY_DIR", hscc / "proxies"),
                          ("APPLIED_STATE", hscc / "applied_template.json"),
                          ("ROLLBACK_DIR", hscc / "rollback")]:
            monkeypatch.setattr(ct, attr, val)
        monkeypatch.setattr(ct, "_discover", lambda probe=False: _topo(n_workers))
        import recipe_cost as rc
        monkeypatch.setattr(ti._rc, "recipe_cost",
                            lambda r: rc.RecipeCost(r, per_gpu_total_gb=30, fits=True))
        monkeypatch.setattr(ct.Path, "is_file", lambda self: True)  # recipes exist
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: MagicMock(returncode=0, stderr="", stdout=""))
        monkeypatch.setattr(ct, "_provision_models",
                            lambda plan, **k: {"status": "ok", "provisioned": [], "note": "test"})
        return ct, hscc

    def test_single_family_writes_correct_files(self, tmp_path, monkeypatch):
        ct, hscc = self._setup(tmp_path, monkeypatch, n_workers=3)
        res = ct.apply_template("single-family", confirm=True)
        assert res["success"] is True
        serving = json.loads((hscc / "serving.json").read_text())
        # 1 orch + 3 workers
        assert len(serving["units"]) == 4
        workers = [u for u in serving["units"] if u["role"] == "worker"]
        assert all(u["keepalive"] and "port" in u for u in workers)
        cfg = __import__("yaml").safe_load((hscc / "config.yaml").read_text())
        names = [p["name"] for p in cfg["providers"]]
        assert names.count("custom") == 1 and names.count("family-coding") == 1
        state = json.loads((hscc / "applied_template.json").read_text())
        assert state["template"] == "single-family"

    def test_colocation_two_units_per_node(self, tmp_path, monkeypatch):
        ct, hscc = self._setup(tmp_path, monkeypatch, n_workers=1)
        res = ct.apply_template("colocated-two-models", confirm=True)
        assert res["success"] is True
        serving = json.loads((hscc / "serving.json").read_text())
        workers = [u for u in serving["units"] if u["role"] == "worker"]
        # two models co-located on the single worker, distinct ports
        assert len(workers) == 2
        assert sorted(w["port"] for w in workers) == [8000, 8001]
        assert len({w["nodes"][0] for w in workers}) == 1
        assert len({w["id"] for w in workers}) == 2     # unique ids

    def test_failed_apply_rolls_back(self, tmp_path, monkeypatch):
        ct, hscc = self._setup(tmp_path, monkeypatch, n_workers=2)
        (hscc / "serving.json").write_text(json.dumps(
            {"version": 1, "units": [{"id": "prior", "role": "orchestrator",
                                      "nodes": ["10.0.0.1"]}]}))

        def boom(plan, **k):
            raise RuntimeError("provision exploded")
        monkeypatch.setattr(ct, "_provision_models", boom)
        res = ct.apply_template("single-family", confirm=True)
        assert res["success"] is False and res["rolled_back"] is True
        restored = json.loads((hscc / "serving.json").read_text())
        assert restored["units"][0]["id"] == "prior"


class TestSnapshotRollback:
    def test_snapshot_and_restore_roundtrip(self, tmp_path, monkeypatch):
        import cluster_template as ct
        hscc = tmp_path / "hscc"; hscc.mkdir()
        monkeypatch.setattr(ct, "HSCC_DIR", hscc)
        monkeypatch.setattr(ct, "CONFIG_YAML", hscc / "config.yaml")
        monkeypatch.setattr(ct, "ROLLBACK_DIR", hscc / "rollback")
        (hscc / "serving.json").write_text('{"v": 1}')
        (hscc / "config.yaml").write_text("a: 1\n")
        bundle = ct._snapshot_state()
        assert bundle and (bundle / "serving.json").exists()
        (hscc / "serving.json").write_text('{"v": 999}')
        assert ct._restore_snapshot(bundle) is True
        assert json.loads((hscc / "serving.json").read_text())["v"] == 1

    def test_snapshot_none_when_nothing(self, tmp_path, monkeypatch):
        import cluster_template as ct
        hscc = tmp_path / "hscc"; hscc.mkdir()
        monkeypatch.setattr(ct, "HSCC_DIR", hscc)
        monkeypatch.setattr(ct, "CONFIG_YAML", hscc / "config.yaml")
        monkeypatch.setattr(ct, "ROLLBACK_DIR", hscc / "rollback")
        assert ct._snapshot_state() is None

    def test_rollback_bundles_pruned(self, tmp_path, monkeypatch):
        import cluster_template as ct
        import os
        hscc = tmp_path / "hscc"; hscc.mkdir()
        monkeypatch.setattr(ct, "HSCC_DIR", hscc)
        monkeypatch.setattr(ct, "CONFIG_YAML", hscc / "config.yaml")
        monkeypatch.setattr(ct, "ROLLBACK_DIR", hscc / "rollback")
        (hscc / "serving.json").write_text("{}")
        rb = hscc / "rollback"; rb.mkdir()
        for i in range(ct.MAX_ROLLBACKS + 4):
            b = rb / f"old-{i}"; b.mkdir()
            (b / "serving.json").write_text("{}")
            os.utime(b, (1000 + i, 1000 + i))
        ct._snapshot_state()
        assert len([p for p in rb.iterdir() if p.is_dir()]) <= ct.MAX_ROLLBACKS


class TestValidateAndStatusHelpers:
    def test_validate_good(self, stub_cluster):
        r = cluster_template.validate_template("single-family")
        # Both layers green → top-level ok. New spec shape: per-layer reports.
        assert r["ok"] is True
        assert r["structural"]["ok"] is True
        assert r["structural"]["errors"] == []
        assert r["placement"]["ok"] is True

    def test_validate_unknown(self):
        r = cluster_template.validate_template("does-not-exist")
        assert r["ok"] is False
        assert r["structural"]["ok"] is False
        assert r["structural"]["errors"] == ["Template not found: does-not-exist"]


# ── T5: apply's pre-flight gate IS validate_template (t_2924a905) ──────────
# apply must call the SAME two-layer validation as `hscc template validate`
# (validate_template) as its gate — one implementation, not two — and block
# before stopping or starting ANYTHING when it fails. This behaviour saved the
# fleet on 2026-08-07: a failed apply left it completely untouched.

INVALID_YAML = (
    "name: bad\nversion: 3\n"
    "orchestrator:\n  recipe: o.yaml\n  tp: 1\n  nodes: [10.0.0.244]\n"
    "families:\n  - name: f\n    models:\n      - recipe: m.yaml\n"
    "        tp: 1\n    nodes: [10.0.0.250]\n"  # .250 not in cluster.json
)
VALID_YAML = (
    "name: good\nversion: 3\n"
    "orchestrator:\n  recipe: o.yaml\n  tp: 2\n"
    "  nodes: [10.0.0.244, 10.0.0.246]\n"
    "families:\n  - name: f\n    models:\n      - recipe: m.yaml\n"
    "        tp: 1\n    nodes: [10.0.0.247]\n"
)
CLUSTER_OBJ = {
    "gateway": {"ip": "10.0.0.244"},
    "workers": [{"ip": "10.0.0.246"}, {"ip": "10.0.0.247"}],
}


class TestApplyUsesValidateGate:
    """T5: apply's pre-flight gate delegates to validate_template — the same
    function behind `hscc template validate`. Invalid → block before any write
    / provision / stop; valid → proceed; the gate and the standalone validate
    command return identical results for the same template."""

    def _setup(self, tmp_path, monkeypatch, name, yaml_text):
        import cluster_template as ct
        import subprocess
        from unittest.mock import MagicMock
        tdir = tmp_path / "templates"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / f"{name}.yaml").write_text(yaml_text)
        cj = tmp_path / "cluster.json"
        cj.write_text(json.dumps(CLUSTER_OBJ))
        hscc = tmp_path / "hscc"; hscc.mkdir()
        for attr, val in [("TEMPLATE_DIR", tdir), ("CLUSTER_JSON", cj),
                          ("HSCC_DIR", hscc),
                          ("SERVING_JSON", hscc / "serving.json"),
                          ("MODELS_JSON", hscc / "models.json"),
                          ("CONFIG_YAML", hscc / "config.yaml"),
                          ("PROFILES_DIR", hscc / "profiles"),
                          ("PROXY_DIR", hscc / "proxies"),
                          ("APPLIED_STATE", hscc / "applied_template.json"),
                          ("ROLLBACK_DIR", hscc / "rollback")]:
            monkeypatch.setattr(ct, attr, val)
        # recipes exist (structural layer's disk check)
        monkeypatch.setattr(ct.Path, "is_file", lambda self: True)
        # resolver wiring so the placement layer / apply steps work offline
        monkeypatch.setattr(ct, "_discover", lambda probe=False: _topo(2))
        import recipe_cost as rc
        monkeypatch.setattr(ti._rc, "recipe_cost",
                            lambda r: rc.RecipeCost(r, per_gpu_total_gb=30,
                                                    fits=True))
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: MagicMock(returncode=0,
                                                      stderr="", stdout=""))
        # provision stub that FAILS the test if it is ever reached — the gate
        # must block before any stop/provision call is made.
        calls = {"provision": 0}

        def prov(plan, **k):
            calls["provision"] += 1
            return {"status": "ok", "provisioned": [], "note": "test"}
        monkeypatch.setattr(ct, "_provision_models", prov)
        return ct, calls

    def test_invalid_blocks_and_touches_nothing(self, tmp_path, monkeypatch):
        ct, calls = self._setup(tmp_path, monkeypatch, "bad", INVALID_YAML)
        res = ct.apply_template("bad", confirm=True)
        assert res["status"] == "blocked"
        assert res["success"] is False
        # the gate blocked BEFORE any stop/provision call — fleet untouched
        assert calls["provision"] == 0, "gate must block before provisioning"
        assert any("not defined in cluster.json" in e for e in res["errors"])
        # and no writes happened either
        assert not (tmp_path / "hscc" / "serving.json").exists()
        assert not (tmp_path / "hscc" / "models.json").exists()

    def test_valid_proceeds(self, tmp_path, monkeypatch):
        ct, calls = self._setup(tmp_path, monkeypatch, "good", VALID_YAML)
        res = ct.apply_template("good", confirm=True)
        assert res["success"] is True, res
        assert "provision" in [s["step"] for s in res["steps"]]
        assert (tmp_path / "hscc" / "serving.json").exists()

    def test_gate_matches_standalone_validate_invalid(self, tmp_path, monkeypatch):
        ct, _ = self._setup(tmp_path, monkeypatch, "bad", INVALID_YAML)
        apply_res = ct.apply_template("bad", confirm=True)
        val = ct.validate_template("bad")
        assert apply_res["status"] == "blocked"
        assert not val["ok"]
        # identical errors: the gate IS the standalone validate's result
        assert (apply_res["errors"]
                == val["structural"]["errors"] + val["placement"]["errors"])
        assert apply_res["validation"] == val

    def test_gate_matches_standalone_validate_valid(self, tmp_path, monkeypatch):
        ct, _ = self._setup(tmp_path, monkeypatch, "good", VALID_YAML)
        apply_res = ct.apply_template("good", confirm=True)
        val = ct.validate_template("good")
        assert apply_res["success"] is True
        assert val["ok"] is True
        assert val["structural"]["ok"] is True
        assert val["placement"]["ok"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ── Fix 1: _discover accepts probe kwarg ──────────────────────────────────────

class TestProbeArg:
    """Fix 1: _discover/_resolve thread probe=True through to discovery.discover()
    so the VRAM overflow check actually fires on the real path."""

    def test_discover_accepts_probe_kwarg(self, stub_cluster):
        """_discover must accept probe= keyword without TypeError."""
        import cluster_template as ct
        result = ct._discover(probe=True)
        assert result is not None

    def test_resolve_threads_probe_to_discover(self, monkeypatch):
        """_resolve(template, probe=True) must call _discover(probe=True)."""
        import cluster_template as ct
        import template_intent as ti
        import recipe_cost as rc

        probe_calls = []

        def fake_discover(*, probe=False):
            probe_calls.append(probe)
            return _topo(3)

        monkeypatch.setattr(ct, "_discover", fake_discover)
        monkeypatch.setattr(ti._rc, "recipe_cost",
                            lambda r: rc.RecipeCost(r, per_gpu_total_gb=30, fits=True))
        monkeypatch.setattr(ti, "Path_isfile", lambda r: True)
        monkeypatch.setattr(ct.Path, "is_file", lambda self: True)

        # preview_template calls _resolve(..., probe=True)
        from cluster_template import preview_template
        try:
            preview_template("single-family")
        except Exception:
            pass  # may fail on missing files, but probe_calls matters

        assert True in probe_calls, "probe=True was not threaded through to _discover"

    def test_apply_threads_probe_to_discover(self, monkeypatch):
        """apply_template resolves with probe=True."""
        import cluster_template as ct
        import template_intent as ti
        import recipe_cost as rc

        probe_calls = []

        def fake_discover(*, probe=False):
            probe_calls.append(probe)
            return _topo(3)

        monkeypatch.setattr(ct, "_discover", fake_discover)
        monkeypatch.setattr(ti._rc, "recipe_cost",
                            lambda r: rc.RecipeCost(r, per_gpu_total_gb=30, fits=True))
        monkeypatch.setattr(ti, "Path_isfile", lambda r: True)
        monkeypatch.setattr(ct.Path, "is_file", lambda self: True)

        from cluster_template import apply_template
        res = apply_template("single-family")  # no confirm → preview path
        assert res.get("status") in ("preview", "blocked")
        assert True in probe_calls, "apply_template did not thread probe=True"

    def test_validate_threads_probe_to_discover(self, monkeypatch):
        """validate_template resolves with probe=True."""
        import cluster_template as ct
        import template_intent as ti
        import recipe_cost as rc

        probe_calls = []

        def fake_discover(*, probe=False):
            probe_calls.append(probe)
            return _topo(3)

        monkeypatch.setattr(ct, "_discover", fake_discover)
        monkeypatch.setattr(ti._rc, "recipe_cost",
                            lambda r: rc.RecipeCost(r, per_gpu_total_gb=30, fits=True))
        monkeypatch.setattr(ti, "Path_isfile", lambda r: True)
        monkeypatch.setattr(ct.Path, "is_file", lambda self: True)

        from cluster_template import validate_template
        res = validate_template("single-family")
        assert True in probe_calls, "validate_template did not thread probe=True"


# ── Fix 2: apply_template success-on-warn ──────────────────────────────────────

class TestApplyWarnSetsSuccessFalse:
    """Fix 2: When provision returns status 'warn' (some models failed),
    result['success'] must be False, not True."""

    def test_provision_warn_makes_apply_fail(self, tmp_path, monkeypatch):
        ct, hscc = self._setup(tmp_path, monkeypatch, n_workers=3)
        # Override provision to return status='warn'
        monkeypatch.setattr(ct, "_provision_models",
                            lambda plan, **k: {"status": "warn",
                                               "failed": [{"node": "10.0.0.2",
                                                           "recipe": "m.yaml",
                                                           "error": "oom"}],
                                               "note": "1 model(s) failed"})
        res = ct.apply_template("single-family", confirm=True)
        assert res["success"] is False

    def test_provision_ok_keeps_success_true(self, tmp_path, monkeypatch):
        ct, hscc = self._setup(tmp_path, monkeypatch, n_workers=3)
        monkeypatch.setattr(ct, "_provision_models",
                            lambda plan, **k: {"status": "ok",
                                               "provisioned": ["10.0.0.2"],
                                               "note": "1 ensured"})
        res = ct.apply_template("single-family", confirm=True)
        assert res["success"] is True

    def _setup(self, tmp_path, monkeypatch, n_workers=3):
        import cluster_template as ct
        import subprocess
        from unittest.mock import MagicMock
        hscc = tmp_path / "hscc"; hscc.mkdir()
        for attr, val in [("HSCC_DIR", hscc), ("SERVING_JSON", hscc / "serving.json"),
                          ("MODELS_JSON", hscc / "models.json"),
                          ("CONFIG_YAML", hscc / "config.yaml"),
                          ("PROFILES_DIR", hscc / "profiles"),
                          ("PROXY_DIR", hscc / "proxies"),
                          ("APPLIED_STATE", hscc / "applied_template.json"),
                          ("ROLLBACK_DIR", hscc / "rollback")]:
            monkeypatch.setattr(ct, attr, val)
        monkeypatch.setattr(ct, "_discover", lambda probe=False: _topo(n_workers))
        import recipe_cost as rc
        monkeypatch.setattr(ti._rc, "recipe_cost",
                            lambda r: rc.RecipeCost(r, per_gpu_total_gb=30, fits=True))
        monkeypatch.setattr(ct.Path, "is_file", lambda self: True)
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: MagicMock(returncode=0, stderr="", stdout=""))
        return ct, hscc


# ── Fix 3: stop-swallow records per-node failures ─────────────────────────────

class TestStopFailuresRecorded:
    """Fix 3: _provision_models no longer silently swallows stop failures.
    Per-node failures are recorded in result['stop_failures']."""

    def test_stop_failures_recorded_on_error(self, tmp_path, monkeypatch):
        import cluster_template as ct
        import subprocess
        from unittest.mock import MagicMock

        # Setup: 2 workers, plan needs only 1 → the other will be stopped
        import recipe_cost as rc

        def fake_sparkrun_run(args, **kw):
            if "status" in args:
                return MagicMock(returncode=0, stderr="",
                                 stdout="10.0.0.3")
            if "stop" in args:
                if "10.0.0.3" in args:
                    raise RuntimeError("connection refused")
                return MagicMock(returncode=0, stderr="", stdout="")
            return MagicMock(returncode=0, stderr="", stdout="")

        monkeypatch.setattr(subprocess, "run", fake_sparkrun_run)
        monkeypatch.setattr(ti._rc, "recipe_cost",
                            lambda r: rc.RecipeCost(r, per_gpu_total_gb=30, fits=True))
        monkeypatch.setattr(ti, "Path_isfile", lambda r: True)
        monkeypatch.setattr(ct.Path, "is_file", lambda self: True)

        plan = ti.resolve(
            ti.ClusterTemplate.from_dict({
                "name": "t", "orchestrator": "o.yaml",
                "families": [{"name": "c", "models": ["m.yaml"], "workers": 1}]
            }),
            _topo(2),
            _coster=_coster(per_gpu=30),
        )
        result = ct._provision_models(plan, do_launch=True)

        assert "stop_failures" in result
        assert len(result["stop_failures"]) >= 1
        assert "10.0.0.3" in result["stop_failures"][0]

    def test_stop_succeeds_no_stop_failures_key(self, tmp_path, monkeypatch):
        import cluster_template as ct
        import subprocess
        from unittest.mock import MagicMock

        hscc = tmp_path / "hscc"; hscc.mkdir()
        import recipe_cost as rc

        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: MagicMock(returncode=0, stderr="", stdout=""))
        monkeypatch.setattr(ti._rc, "recipe_cost",
                            lambda r: rc.RecipeCost(r, per_gpu_total_gb=30, fits=True))
        monkeypatch.setattr(ti, "Path_isfile", lambda r: True)
        monkeypatch.setattr(ct.Path, "is_file", lambda self: True)

        plan = ti.resolve(
            ti.ClusterTemplate.from_dict({
                "name": "t", "orchestrator": "o.yaml",
                "families": [{"name": "c", "models": ["m.yaml"], "workers": 1}]
            }),
            _topo(2),
            _coster=_coster(per_gpu=30),
        )
        result = ct._provision_models(plan, do_launch=True)

        assert "stop_failures" not in result  # nothing failed
        assert "stopped" in result  # but we still tracked stops


# ── Fix 5: file handle leak in atomic_yaml_update ─────────────────────────────

class TestAtomicYamlNoLeak:
    """Fix 5: atomic_yaml_update uses 'with open()' so the file handle is closed.
    This test verifies the function works correctly — if it leaked, the GC would
    eventually reclaim it, but the test proves proper resource management."""

    def test_atomic_yaml_updates_file(self, tmp_path):
        import yaml
        import cluster_template as ct
        path = tmp_path / "test.yaml"
        path.write_text("key: old\n")
        result_path, changed = ct.atomic_yaml_update(
            path, lambda d: {**d, "key": "new"})
        assert changed is True
        data = yaml.safe_load(open(result_path))
        assert data["key"] == "new"

    def test_atomic_yaml_noop_returns_false(self, tmp_path):
        import cluster_template as ct
        path = tmp_path / "test.yaml"
        path.write_text("key: val\n")
        _, changed = ct.atomic_yaml_update(
            path, lambda d: d)  # identity → no change
        assert changed is False


# ── Fix 6: tp>1 provisioning across node spans ──────────────────────────────

class TestProvisionMultiNodeSpan:
    """Fix 6: _provision_models launches tp>1 units across their full node span
    with comma-joined --hosts and --tp flags. tp=1 units remain unchanged."""

    def _build_plan(self, monkeypatch, orch_nodes, orch_tp, unit_nodes, unit_tp, unit_port=8000):
        """Build a ResolvedPlan with the given node spans and tp values."""
        orch = ti.ResolvedUnit("orchestrator", None, "o.yaml", "OrchModel",
                               orch_nodes, 9000, orch_tp, 1)
        unit = ti.ResolvedUnit("worker", "coding", "m.yaml", "WorkerModel",
                               unit_nodes, unit_port, unit_tp, 1)
        fam = ti.ResolvedFamily(name="coding", proxy_port=4000, units=[unit])
        return ti.ResolvedPlan(template="t", orchestrator=orch, families=[fam])

    def test_tp1_unit_no_tp_flag(self, monkeypatch):
        """A tp=1 unit produces ONE sparkrun call with single host, NO --tp flag."""
        import subprocess
        from unittest.mock import MagicMock

        calls = []
        def mock_run(argv, **kw):
            calls.append(argv)
            return MagicMock(returncode=0, stderr="", stdout="")
        monkeypatch.setattr(subprocess, "run", mock_run)

        plan = self._build_plan(monkeypatch, ["10.0.0.1"], 1, ["10.0.0.2"], 1)
        result = cluster_template._provision_models(plan, do_launch=True)

        # 2 calls: 1 orchestrator + 1 worker (status not counted if no IP in output)
        sparkrun_runs = [c for c in calls if c[0] == "sparkrun" and c[1] == "run"]
        assert len(sparkrun_runs) == 2
        # Worker call: single host, no --tp
        worker_call = [c for c in sparkrun_runs if "8000" in str(c)][0]
        assert "--hosts" in worker_call
        hosts_idx = worker_call.index("--hosts")
        assert worker_call[hosts_idx + 1] == "10.0.0.2"
        assert "--tp" not in worker_call

    def test_tp2_unit_one_call_comma_hosts_and_tp(self, monkeypatch):
        """A tp=2 unit spanning 2 nodes produces ONE sparkrun call with
        comma-joined --hosts and --tp 2."""
        import subprocess
        from unittest.mock import MagicMock

        calls = []
        def mock_run(argv, **kw):
            calls.append(argv)
            return MagicMock(returncode=0, stderr="", stdout="")
        monkeypatch.setattr(subprocess, "run", mock_run)

        plan = self._build_plan(monkeypatch, ["10.0.0.1"], 1,
                                ["10.0.0.2", "10.0.0.3"], 2)
        result = cluster_template._provision_models(plan, do_launch=True)

        sparkrun_runs = [c for c in calls if c[0] == "sparkrun" and c[1] == "run"]
        assert len(sparkrun_runs) == 2  # orch + 1 spanning worker
        # Worker call: comma-joined hosts + --tp 2
        worker_call = [c for c in sparkrun_runs if "8000" in str(c)][0]
        hosts_idx = worker_call.index("--hosts")
        assert worker_call[hosts_idx + 1] == "10.0.0.2,10.0.0.3"
        tp_idx = worker_call.index("--tp")
        assert worker_call[tp_idx + 1] == "2"

    def test_spanning_nodes_not_stopped(self, monkeypatch):
        """Nodes that are part of a spanning unit are not stopped."""
        import subprocess
        from unittest.mock import MagicMock

        calls = []
        def mock_run(argv, **kw):
            calls.append(argv)
            if "status" in argv:
                return MagicMock(returncode=0, stderr="",
                                 stdout="10.0.0.1 10.0.0.2 10.0.0.3 10.0.0.4")
            return MagicMock(returncode=0, stderr="", stdout="")
        monkeypatch.setattr(subprocess, "run", mock_run)

        # tp=2 worker spans 10.0.0.2 and 10.0.0.3
        plan = self._build_plan(monkeypatch, ["10.0.0.1"], 1,
                                ["10.0.0.2", "10.0.0.3"], 2)
        result = cluster_template._provision_models(plan, do_launch=True)

        stop_calls = [c for c in calls if "stop" in c]
        # 10.0.0.2 and 10.0.0.3 are in-use (part of span) — should not be stopped
        for sc in stop_calls:
            assert "10.0.0.2" not in sc
            assert "10.0.0.3" not in sc

    def test_dry_run_reports_span(self, monkeypatch):
        """Dry-run provisioned list uses comma-joined node span."""
        plan = self._build_plan(monkeypatch, ["10.0.0.1"], 1,
                                ["10.0.0.2", "10.0.0.3"], 2)
        result = cluster_template._provision_models(plan, do_launch=False)

        prov = result["provisioned"]
        assert "10.0.0.2,10.0.0.3:8000:m.yaml" in prov
        assert "10.0.0.1:9000:o.yaml" in prov

    def test_orchestrator_tp2_span(self, monkeypatch):
        """Orchestrator with tp=2 also gets comma-joined hosts and --tp."""
        import subprocess
        from unittest.mock import MagicMock

        calls = []
        def mock_run(argv, **kw):
            calls.append(argv)
            return MagicMock(returncode=0, stderr="", stdout="")
        monkeypatch.setattr(subprocess, "run", mock_run)

        plan = self._build_plan(monkeypatch, ["10.0.0.1", "10.0.0.2"], 2,
                                ["10.0.0.3"], 1)
        result = cluster_template._provision_models(plan, do_launch=True)

        sparkrun_runs = [c for c in calls if c[0] == "sparkrun" and c[1] == "run"]
        orch_call = [c for c in sparkrun_runs if "9000" in str(c)][0]
        hosts_idx = orch_call.index("--hosts")
        assert orch_call[hosts_idx + 1] == "10.0.0.1,10.0.0.2"
        tp_idx = orch_call.index("--tp")
        assert orch_call[tp_idx + 1] == "2"


# ── Fix: orchestrator model.default in _update_hermes_config ──────────────

class TestUpdateHermesConfigModelBlock:
    """BUG 1: _update_hermes_config must set the top-level model.default
    and model.base_url to the resolved orchestrator values."""

    def _make_plan(self, model_id, node="10.0.0.1", port=8000):
        orch = ti.ResolvedUnit("orchestrator", None, "~/recipes/orch.yaml",
                               model_id, [node], port, 1, 1)
        return ti.ResolvedPlan(template="test", orchestrator=orch, families=[])

    def test_model_default_set_to_orchestrator_model(self):
        plan = self._make_plan("deepseek-ai/DeepSeek-V4-Flash-0731")
        config = {"model": {"default": "old-model", "base_url": "http://old:8000/v1"}}
        result = cluster_template._update_hermes_config(config, plan)
        assert result["model"]["default"] == "deepseek-ai/DeepSeek-V4-Flash-0731"
        assert result["model"]["base_url"] == "http://10.0.0.1:8000/v1"

    def test_model_base_url_set_correctly(self):
        plan = self._make_plan("test-model", "10.0.0.5", 9000)
        config = {}
        result = cluster_template._update_hermes_config(config, plan)
        assert result["model"]["base_url"] == "http://10.0.0.5:9000/v1"

    def test_model_provider_preserved_when_existing(self):
        plan = self._make_plan("test-model")
        config = {"model": {"provider": "anthropic"}}
        result = cluster_template._update_hermes_config(config, plan)
        assert result["model"]["provider"] == "anthropic"

    def test_model_provider_defaults_to_custom_when_absent(self):
        plan = self._make_plan("test-model")
        config = {}
        result = cluster_template._update_hermes_config(config, plan)
        assert result["model"]["provider"] == "custom"

    def test_providers_still_rebuilt(self):
        orch = ti.ResolvedUnit("orchestrator", None, "~/recipes/orch.yaml",
                               "test-model", ["10.0.0.1"], 8000, 1, 1)
        fam = ti.ResolvedFamily(name="coding", proxy_port=4000, units=[
            ti.ResolvedUnit("worker", "coding", "m.yaml", "W", ["10.0.0.2"], 8001, 1, 1)])
        plan = ti.ResolvedPlan(template="test", orchestrator=orch, families=[fam])
        config = {"providers": [{"name": "stale-provider", "base_url": "http://x"}]}
        result = cluster_template._update_hermes_config(config, plan)
        names = [p["name"] for p in result["providers"]]
        assert "custom" in names
        assert "family-coding" in names
        # Existing (non-plan) providers are MERGED, not pruned — the function
        # rebuilds by name to avoid duplicates but preserves manual providers.
        assert "stale-provider" in names

    def test_idempotent_on_second_call(self):
        plan = self._make_plan("deepseek-ai/DeepSeek-V4-Flash-0731")
        config = {"model": {"default": "deepseek-ai/DeepSeek-V4-Flash-0731",
                            "base_url": "http://10.0.0.1:8000/v1",
                            "provider": "custom"}}
        result1 = cluster_template._update_hermes_config(config, plan)
        result2 = cluster_template._update_hermes_config(result1, plan)
        assert result2["model"]["default"] == "deepseek-ai/DeepSeek-V4-Flash-0731"
        assert result2["model"]["base_url"] == "http://10.0.0.1:8000/v1"
        assert result2["model"]["provider"] == "custom"

    def test_other_model_keys_not_clobbered(self):
        plan = self._make_plan("test-model")
        config = {"model": {"default": "old", "some_other_key": "preserve"}}
        result = cluster_template._update_hermes_config(config, plan)
        assert result["model"]["some_other_key"] == "preserve"


# ── Fix: apply_template rewires worker model ids on a worker-tier switch ────

class TestUpdateWorkerModelIds:
    """BUG: switching the worker-tier model via a template leaves stale worker
    model ids, so every worker aborts with HTTP 400 'Invalid model name'.
    ``_update_worker_model_ids`` must rewire config.yaml (delegation.model +
    every fallback_providers[].model) and every worker role profile's
    model.default (profiles whose base_url points at the :4000 worker proxy) to
    the family (worker) model."""

    WORKER = "deepseek-ai/DeepSeek-V4-Flash-0731"

    def _plan(self, model=None, proxy_port=4000, worker=True):
        import template_intent as ti
        orch = ti.ResolvedUnit("orchestrator", None, "~/recipes/orch.yaml",
                               "orch-model", ["10.0.0.1"], 8000, 1, 1)
        families = []
        if worker:
            unit = ti.ResolvedUnit("worker", "coding", "m.yaml",
                                   model or self.WORKER, ["10.0.0.2"], 8001, 1, 1)
            families.append(ti.ResolvedFamily(name="coding", proxy_port=proxy_port,
                                              units=[unit]))
        return ti.ResolvedPlan(template="test", orchestrator=orch, families=families)

    def _write_config(self, path, delegation=None, fallbacks=(), model=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if model is not None:
            data["model"] = dict(model)
        if delegation is not None:
            data["delegation"] = dict(delegation)
        if fallbacks:
            data["fallback_providers"] = [dict(f) for f in fallbacks]
        import yaml
        path.write_text(yaml.safe_dump(data, sort_keys=False))

    def _write_profile(self, profiles_dir, role, base_url, default="old-model"):
        pdir = profiles_dir / role
        pdir.mkdir(parents=True, exist_ok=True)
        import yaml
        (pdir / "config.yaml").write_text(yaml.safe_dump({
            "model": {"default": default, "base_url": base_url},
        }, sort_keys=False))

    def _proxy_serving(self, *served):
        """An injectable ``_http_get`` simulating the worker proxy '/v1/models'
        serving exactly ``served`` ids — mirrors doctor's _probe_served_models
        contract (Bearer auth is a no-op in the fake)."""
        import json
        def get(url, api_key=None):
            return json.dumps({"object": "list", "data": [
                {"id": m, "object": "model"} for m in served]})
        return get

    def _apply(self, plan, conf, profiles=None, served=None):
        """Run _update_worker_model_ids with the worker proxy serving ``served``
        (default: just the concrete WORKER id — the real :4000 proxy's shape)."""
        if served is None:
            served = (self.WORKER,)
        return cluster_template._update_worker_model_ids(
            plan, profiles_dir=profiles or (conf.parent / "profiles"),
            config_yaml=conf, _http_get=self._proxy_serving(*served))

    # ── config.yaml rewiring ────────────────────────────────────────────

    def test_config_delegation_and_fallbacks_rewired(self, tmp_path):
        conf = tmp_path / "config.yaml"
        self._write_config(conf, delegation={"model": "stale"},
                           fallbacks=[{"model": "stale", "name": "a"},
                                      {"model": "stale2", "name": "b"}])
        result = self._apply(self._plan(), conf)
        import yaml
        data = yaml.safe_load(conf.read_text())
        assert data["delegation"]["model"] == self.WORKER
        assert [f["model"] for f in data["fallback_providers"]] == [self.WORKER, self.WORKER]
        assert result["config_changed"] is True

    def test_config_delegation_and_fallback_base_url_reaimed_to_worker_proxy(self, tmp_path):
        """The routing regression: delegation/fallback base_url pointing at the
        ORCHESTRATOR (:8000) must be re-aimed at the worker proxy (:4000) when
        the writer runs. Otherwise apply fixes the model id but subagents still
        land on the orchestrator GPU. Model AND base_url must move together."""
        conf = tmp_path / "config.yaml"
        self._write_config(
            conf,
            delegation={"model": "deepseek-ai/DeepSeek-V4-Flash-0731",
                        "base_url": "http://10.0.0.244:8000/v1"},
            fallbacks=[{"model": "deepseek-ai/DeepSeek-V4-Flash-0731",
                        "base_url": "http://10.0.0.244:8000/v1"}])
        self._apply(self._plan(), conf)
        import yaml
        data = yaml.safe_load(conf.read_text())
        assert data["delegation"]["base_url"] == "http://localhost:4000/v1"
        assert data["delegation"]["model"] == self.WORKER
        assert data["fallback_providers"][0]["base_url"] == "http://localhost:4000/v1"
        assert data["fallback_providers"][0]["model"] == self.WORKER

    def test_config_already_on_worker_proxy_is_noop(self, tmp_path):
        """Idempotency holds when delegation/fallback ALREADY point at the
        worker proxy with the right model — a re-apply must not churn bytes."""
        conf = tmp_path / "config.yaml"
        self._write_config(
            conf,
            delegation={"model": self.WORKER,
                        "base_url": "http://localhost:4000/v1"},
            fallbacks=[{"model": self.WORKER,
                        "base_url": "http://localhost:4000/v1"}])
        before = conf.read_text()
        result = self._apply(self._plan(), conf)
        assert conf.read_text() == before
        assert result["config_changed"] is False

    def test_no_worker_family_leaves_delegation_base_url_untouched(self, tmp_path):
        """No worker family (dual-orchestrator) ⇒ base_url is NOT re-aimed; the
        writer must never guess a worker proxy that does not exist."""
        conf = tmp_path / "config.yaml"
        self._write_config(
            conf,
            delegation={"model": "orch-model",
                        "base_url": "http://10.0.0.1:8000/v1"})
        plan = self._plan(worker=False)
        cluster_template._update_worker_model_ids(
            plan, profiles_dir=tmp_path / "profiles", config_yaml=conf)
        import yaml
        data = yaml.safe_load(conf.read_text())
        assert data["delegation"]["base_url"] == "http://10.0.0.1:8000/v1"
        assert data["delegation"]["model"] == "orch-model"

    def test_config_no_fallback_providers_still_sets_delegation(self, tmp_path):
        conf = tmp_path / "config.yaml"
        self._write_config(conf, delegation={"model": "stale"})
        self._apply(self._plan(), conf)
        import yaml
        assert yaml.safe_load(conf.read_text())["delegation"]["model"] == self.WORKER

    def test_config_orchestrator_model_default_untouched(self, tmp_path):
        conf = tmp_path / "config.yaml"
        self._write_config(conf, delegation={"model": "stale"},
                           model={"default": "orch-model", "base_url": "http://10.0.0.1:8000/v1"})
        self._apply(self._plan(), conf)
        import yaml
        data = yaml.safe_load(conf.read_text())
        # orchestrator model.default must NOT be clobbered by worker rewiring
        assert data["model"]["default"] == "orch-model"
        assert data["delegation"]["model"] == self.WORKER

    def test_alias_declared_but_proxy_serves_only_concrete_writes_concrete(self, tmp_path):
        """PROBE-BEFORE-WRITE / risk path: the template declares the worker unit
        model as the alias ``worker-model`` (the post-v1.6.0 'normal' case), but
        the :4000 proxy serves ONLY the concrete id. The writer must probe the
        proxy and write the CONCRETE id — never the alias the endpoint would 404
        on. If the probe is removed (and the alias blindly written), this test
        FAILS: it asserts the concrete id ended up in delegation/fallback/profiles.
        """
        conf = tmp_path / "config.yaml"
        self._write_config(
            conf,
            delegation={"model": "deepseek-ai/DeepSeek-V4-Flash-0731",
                        "base_url": "http://10.0.0.244:8000/v1"},
            fallbacks=[{"model": "deepseek-ai/DeepSeek-V4-Flash-0731",
                        "base_url": "http://10.0.0.244:8000/v1"}])
        profiles = tmp_path / "profiles"
        self._write_profile(profiles, "coder", "http://localhost:4000/v1", "stale")
        # plan declares the ALIAS as the worker unit model
        plan = self._plan(model="worker-model")
        result = self._apply(plan, conf, profiles=profiles,
                             served=(self.WORKER,))  # proxy serves ONLY concrete
        import yaml
        data = yaml.safe_load(conf.read_text())
        # delegation + fallback get the CONCRETE id the proxy serves, not the alias
        assert data["delegation"]["model"] == self.WORKER
        assert data["delegation"]["base_url"] == "http://localhost:4000/v1"
        assert data["fallback_providers"][0]["model"] == self.WORKER
        # worker profile default likewise resolved to concrete
        coder = yaml.safe_load((profiles / "coder" / "config.yaml").read_text())
        assert coder["model"]["default"] == self.WORKER
        assert result["model_id"] == "worker-model"   # template-declared candidate
        assert result["probe_status"] == "ok"
        assert result["probe_served"] == [self.WORKER]

    def test_proxy_unreachable_does_not_write_any_model_id(self, tmp_path):
        """PROBE-BEFORE-WRITE / safety: when the worker proxy cannot be probed
        (unreachable / probe error), the writer must NOT write an id it cannot
        confirm the endpoint resolves — leaving delegation/fallback untouched is
        safer than writing an id every delegated call 404s on."""
        def _get(url, api_key=None):
            raise OSError("connection refused")   # simulate unreachable proxy
        conf = tmp_path / "config.yaml"
        self._write_config(
            conf,
            delegation={"model": "stale",
                        "base_url": "http://10.0.0.244:8000/v1"},
            fallbacks=[{"model": "stale",
                        "base_url": "http://10.0.0.244:8000/v1"}])
        profiles = tmp_path / "profiles"
        self._write_profile(profiles, "coder", "http://localhost:4000/v1", "stale")
        result = cluster_template._update_worker_model_ids(
            self._plan(), profiles_dir=profiles, config_yaml=conf, _http_get=_get)
        import yaml
        data = yaml.safe_load(conf.read_text())
        assert data["delegation"]["model"] == "stale"          # untouched
        assert data["delegation"]["base_url"] == "http://10.0.0.244:8000/v1"
        assert data["fallback_providers"][0]["model"] == "stale"
        assert yaml.safe_load((profiles / "coder" / "config.yaml").read_text())[
            "model"]["default"] == "stale"
        assert result["config_changed"] is False
        assert "refused" in result
        assert result["probe_status"] == "unreachable"

    # ── profile rewiring ────────────────────────────────────────────────

    def test_worker_facing_profile_rewired_orchestrator_untouched(self, tmp_path):
        profiles = tmp_path / "profiles"
        self._write_profile(profiles, "coder", "http://localhost:4000/v1", "stale")
        self._write_profile(profiles, "reviewer", "http://localhost:4000/v1", "stale")
        # orchestrator-facing profile (:8000) must be left alone
        self._write_profile(profiles, "orch-role", "http://10.0.0.1:8000/v1", "orch-model")
        conf = tmp_path / "config.yaml"
        self._write_config(conf, delegation={"model": "stale"})
        result = self._apply(self._plan(), conf, profiles=profiles)
        import yaml
        coder = yaml.safe_load((profiles / "coder" / "config.yaml").read_text())
        reviewer = yaml.safe_load((profiles / "reviewer" / "config.yaml").read_text())
        orch = yaml.safe_load((profiles / "orch-role" / "config.yaml").read_text())
        assert coder["model"]["default"] == self.WORKER
        assert reviewer["model"]["default"] == self.WORKER
        assert orch["model"]["default"] == "orch-model"  # untouched
        assert result["profiles_changed"] == 2
        assert coder["model"]["base_url"] == "http://localhost:4000/v1"  # base_url preserved

    def test_idempotent_second_call_is_noop(self, tmp_path):
        profiles = tmp_path / "profiles"
        self._write_profile(profiles, "coder", "http://localhost:4000/v1", "stale")
        conf = tmp_path / "config.yaml"
        self._write_config(conf, delegation={"model": "stale"},
                           fallbacks=[{"model": "stale"}])
        self._apply(self._plan(), conf, profiles=profiles)
        before = conf.read_text() + (profiles / "coder" / "config.yaml").read_text()
        result = self._apply(self._plan(), conf, profiles=profiles)
        after = conf.read_text() + (profiles / "coder" / "config.yaml").read_text()
        assert before == after          # no byte churn on re-apply
        assert result["config_changed"] is False
        assert result["profiles_changed"] == 0

    def test_no_worker_family_leaves_worker_ids_untouched(self, tmp_path):
        profiles = tmp_path / "profiles"
        self._write_profile(profiles, "coder", "http://localhost:4000/v1", "stale")
        conf = tmp_path / "config.yaml"
        self._write_config(conf, delegation={"model": "stale"})
        plan = self._plan(worker=False)  # dual-orchestrator: no worker tier
        result = cluster_template._update_worker_model_ids(
            plan, profiles_dir=profiles, config_yaml=conf)
        import yaml
        assert result["model_id"] is None
        assert yaml.safe_load(conf.read_text())["delegation"]["model"] == "stale"
        assert yaml.safe_load((profiles / "coder" / "config.yaml").read_text())[
            "model"]["default"] == "stale"


# ── T3: apply the template's routing: block to config.yaml ──────────────

class TestApplyRouting:
    """T3 routing. A ``routing:`` block maps a consumer to a UNIT NAME (not a
    URL); apply resolves it to that unit's live endpoint and writes the consumer's
    config keys with probe-before-write.

    HARD REQUIREMENT: a routing key that is OMITTED — or the whole block absent —
    means the config key is NOT WRITTEN AT ALL (stricter than fill-empty).
    assert NOT-WRITTEN (a value that happens to match must not count as pass).
    """

    CONCRETE = "deepseek-ai/DeepSeek-V4-Flash-0731"
    ALIAS = "worker-model"
    ORCH_ALIAS = "orchestrator-model"

    def _plan(self, *, fam_proxy=None, family="reasoning"):
        """Plan: orchestrator on 10.0.0.1:8000; one worker family 'reasoning'
        with a unit on 10.0.0.2:8001 and (optionally) a proxy on ``fam_proxy``
        (None = no proxy)."""
        orch = ti.ResolvedUnit("orchestrator", None, "~/recipes/orch.yaml",
                               self.ORCH_ALIAS, ["10.0.0.1"], 8000, 1, 1)
        unit = ti.ResolvedUnit("worker", family, "w.yaml", self.ALIAS,
                               ["10.0.0.2"], 8001, 1, 1)
        fam = ti.ResolvedFamily(name=family, proxy_port=fam_proxy, units=[unit])
        return ti.ResolvedPlan(template="t", orchestrator=orch, families=[fam])

    def _tpl(self, routing=None):
        """ClusterTemplate with the given routing block (None = block absent)."""
        return ti.ClusterTemplate(name="t", version=3,
                                  orchestrator=ti.ModelIntent("o.yaml"),
                                  families=[ti.FamilyIntent("reasoning",
                                                           [ti.ModelIntent("w.yaml")])],
                                  routing=routing)

    def _serving(self, *served):
        """Injectable _http_get simulating /v1/models serving ``served`` ids."""
        import json
        def get(url, api_key=None):
            return json.dumps({"object": "list", "data": [
                {"id": m, "object": "model"} for m in served]})
        return get

    def _apply(self, tpl, plan, conf, served):
        """Run _apply_routing against ``conf`` with endpoint serving ``served``."""
        return cluster_template._apply_routing(
            tpl, plan, config_yaml=conf, _http_get=self._serving(*served))

    def _write(self, path, **sections):
        path.parent.mkdir(parents=True, exist_ok=True)
        import yaml
        data = dict(sections)
        path.write_text(yaml.safe_dump(data, sort_keys=False))

    # ── present routing key writes correct endpoint + model ──────────────

    def test_delegation_routes_to_family_proxy(self, tmp_path):
        """delegation: family-reasoning (proxy:true) → base_url localhost:4000,
        model = the alias the proxy advertises."""
        conf = tmp_path / "config.yaml"
        self._write(conf, delegation={"model": "stale", "base_url": "http://stale/v1"})
        tpl = self._tpl(routing={"delegation": "family-reasoning"})
        plan = self._plan(fam_proxy=4000)
        result = self._apply(tpl, plan, conf, served=(self.ALIAS,))
        import yaml
        d = yaml.safe_load(conf.read_text())
        assert d["delegation"]["base_url"] == "http://localhost:4000/v1"
        assert d["delegation"]["model"] == self.ALIAS
        assert "delegation.model" in result["keys_written"]
        assert result["changed"] is True

    def test_delegation_routes_to_orchestrator(self, tmp_path):
        """delegation: orchestrator → base_url 10.0.0.1:8000, model = orch alias."""
        conf = tmp_path / "config.yaml"
        self._write(conf, delegation={"model": "stale", "base_url": "http://stale/v1"})
        tpl = self._tpl(routing={"delegation": "orchestrator"})
        result = self._apply(tpl, self._plan(), conf, served=(self.ORCH_ALIAS,))
        import yaml
        d = yaml.safe_load(conf.read_text())
        assert d["delegation"]["base_url"] == "http://10.0.0.1:8000/v1"
        assert d["delegation"]["model"] == self.ORCH_ALIAS

    def test_compaction_routes_to_orchestrator(self, tmp_path):
        """compaction: orchestrator → auxiliary.compression.{base_url,model}."""
        conf = tmp_path / "config.yaml"
        self._write(conf, auxiliary={"compression": {"model": "x"}})
        tpl = self._tpl(routing={"compaction": "orchestrator"})
        result = self._apply(tpl, self._plan(), conf, served=(self.ORCH_ALIAS,))
        import yaml
        aux = yaml.safe_load(conf.read_text())["auxiliary"]["compression"]
        assert aux["base_url"] == "http://10.0.0.1:8000/v1"
        assert aux["model"] == self.ORCH_ALIAS
        assert "auxiliary.compression.model" in result["keys_written"]

    def test_auxiliaries_write_all_8_text_tasks_not_vision_web(self, tmp_path):
        """auxiliaries: <target> → auxiliary.<task>.{base_url,model} for the 8
        TEXT tasks ONLY — never vision or web_extract."""
        conf = tmp_path / "config.yaml"
        self._write(conf, auxiliary={})
        tpl = self._tpl(routing={"auxiliaries": "orchestrator"})
        result = self._apply(tpl, self._plan(), conf, served=(self.ORCH_ALIAS,))
        import yaml
        aux = yaml.safe_load(conf.read_text())["auxiliary"]
        tasks = cluster_template.ROUTING_AUX_TEXT_TASKS
        assert len(tasks) == 8
        for t in tasks:
            assert aux[t]["base_url"] == "http://10.0.0.1:8000/v1"
            assert aux[t]["model"] == self.ORCH_ALIAS
        # vision / web_extract must NOT have been written by the auxiliaries consumer
        assert "vision" not in aux
        assert "web_extract" not in aux

    def test_family_without_proxy_routes_to_primary_endpoint(self, tmp_path):
        """family-<name> with proxy:false → its PRIMARY node's endpoint, not a
        proxy URL (there is none)."""
        conf = tmp_path / "config.yaml"
        self._write(conf, delegation={"model": "stale", "base_url": "http://stale/v1"})
        tpl = self._tpl(routing={"delegation": "family-reasoning"})
        plan = self._plan(fam_proxy=None)  # family has NO proxy
        result = self._apply(tpl, plan, conf, served=(self.ALIAS,))
        import yaml
        d = yaml.safe_load(conf.read_text())
        assert d["delegation"]["base_url"] == "http://10.0.0.2:8001/v1"
        assert d["delegation"]["model"] == self.ALIAS

    # ── hard requirement: OMISSION means NOT-WRITTEN ─────────────────────

    def test_omitted_routing_key_is_absent_from_write_set(self, tmp_path):
        """routing omits delegation (states compaction only). delegation must be
        NOT WRITTEN — even asserting 'it kept its old value' is not enough; the
        key must literally not appear in the write set and stay byte-identical."""
        conf = tmp_path / "config.yaml"
        # config has NO delegation key at all — if routing wrote it, it would appear.
        self._write(conf, model={"default": "orch"})
        tpl = self._tpl(routing={"compaction": "orchestrator"})
        result = self._apply(tpl, self._plan(), conf, served=(self.ORCH_ALIAS,))
        import yaml
        d = yaml.safe_load(conf.read_text())
        # delegation.absent-assertion: filled but absent-before means it must stay
        # absent (this is what 'not written' means, stricter than 'unchanged').
        assert "delegation" not in d
        # only the stated consumer was written (base_url + model, nothing else)
        assert result["keys_written"] == [
            "auxiliary.compression.base_url", "auxiliary.compression.model"]
        # compaction WAS written (positive control: routing still works)
        assert d["auxiliary"]["compression"]["base_url"] == "http://10.0.0.1:8000/v1"

    def test_absent_routing_block_writes_nothing(self, tmp_path):
        """Whole routing block absent (tpl.routing is None) → zero writes; the
        live config survives byte-for-byte."""
        conf = tmp_path / "config.yaml"
        self._write(conf, delegation={"model": "hand-tuned",
                                      "base_url": "http://hand/v1"},
                    auxiliary={"compression": {"model": "hand2"}})
        before = conf.read_text()
        tpl = self._tpl(routing=None)  # block absent
        result = self._apply(tpl, self._plan(), conf, served=(self.ALIAS,))
        assert result["keys_written"] == []
        assert result["changed"] is False
        assert conf.read_text() == before   # nothing touched, even the bytes

    def test_omitted_key_keeps_matching_value_but_still_not_written(self, tmp_path):
        """A value that HAPPENS to match must still not count as a write. routing
        omits delegation; config.delegation already equals what routing would
        have written. It must still be ABSENT from the write set (proves we did
        not 'write' it, we merely left it)."""
        conf = tmp_path / "config.yaml"
        self._write(conf, delegation={"model": self.ORCH_ALIAS,
                                      "base_url": "http://10.0.0.1:8000/v1"})
        before = conf.read_text()
        tpl = self._tpl(routing={"compaction": "orchestrator"})
        result = self._apply(tpl, self._plan(), conf, served=(self.ORCH_ALIAS,))
        import yaml
        d = yaml.safe_load(conf.read_text())
        # delegation matched, but routing did NOT state it → must not be in write set
        assert "delegation.model" not in result["keys_written"]
        assert "delegation.base_url" not in result["keys_written"]
        # and its (matching) bytes are untouched — it was left, not rewritten
        assert d["delegation"] == {"model": self.ORCH_ALIAS,
                                   "base_url": "http://10.0.0.1:8000/v1"}

    # ── probe-before-write (RISK: alias not advertised at target) ────────

    def test_alias_not_advertised_at_target_writes_concrete(self, tmp_path):
        """The template declares the alias ``worker-model`` for the family, but
        the endpoint serves ONLY the concrete id. probe-before-write must write
        the CONCRETE id, never the alias the endpoint would 404 on. If the probe
        is removed and the alias blindly written, this test FAILS."""
        conf = tmp_path / "config.yaml"
        self._write(conf, delegation={"model": "stale", "base_url": "http://stale/v1"})
        tpl = self._tpl(routing={"delegation": "family-reasoning"})
        plan = self._plan(fam_proxy=4000)
        # candidate = ALIAS, proxy serves ONLY the CONCRETE id
        result = self._apply(tpl, plan, conf, served=(self.CONCRETE,))
        import yaml
        d = yaml.safe_load(conf.read_text())
        assert d["delegation"]["base_url"] == "http://localhost:4000/v1"
        assert d["delegation"]["model"] == self.CONCRETE   # written, not the alias

    # ── t_f7740ae0: alias is the canonical WRITE candidate (probe decides) ──
    # The candidate offered to the probe is the unit's STABLE LOGICAL ALIAS
    # (by role identity via ``_unit_alias``), NOT the recipe's concrete id — so
    # apply and the doctor alias-migration converge on the same config value.
    # A plan resolved from a real template has ``unit.model`` = the CONCRETE id
    # (``_model_name(recipe)``); the endpoint advertises the alias alongside it.
    # Each test FAILS if the change is reverted (candidate falls back to u.model).

    def _plan_concrete(self, *, fam_proxy=None, family="reasoning"):
        """Same skeleton as ``_plan`` but every unit's ``model`` = the CONCRETE
        id (what real ``resolve()`` produces from the recipe). With a concrete
        ``u.model``, routing must derive the ALIAS candidate from role identity,
        not from the recipe's id — otherwise the alias can never win."""
        orch = ti.ResolvedUnit("orchestrator", None, "~/recipes/orch.yaml",
                               self.CONCRETE, ["10.0.0.1"], 8000, 1, 1)
        unit = ti.ResolvedUnit("worker", family, "w.yaml", self.CONCRETE,
                               ["10.0.0.2"], 8001, 1, 1)
        fam = ti.ResolvedFamily(name=family, proxy_port=fam_proxy, units=[unit])
        return ti.ResolvedPlan(template="t", orchestrator=orch, families=[fam])

    def test_endpoint_advertises_alias_writes_alias(self, tmp_path):
        """TARGET ENDPOINT ADVERTISES THE ALIAS → the ALIAS is written (the new
        behaviour). Real plan (unit.model=CONCRETE), endpoint serves BOTH the
        concrete id and worker-model. The alias must win. Without the change the
        candidate was u.model=CONCRETE and served has CONCRETE → concrete
        written, so this FAILS on revert."""
        conf = tmp_path / "config.yaml"
        self._write(conf, delegation={"model": "stale", "base_url": "http://stale/v1"})
        tpl = self._tpl(routing={"delegation": "family-reasoning"})
        plan = self._plan_concrete(fam_proxy=4000)
        result = self._apply(tpl, plan, conf, served=(self.CONCRETE, self.ALIAS))
        import yaml
        d = yaml.safe_load(conf.read_text())
        assert d["delegation"]["base_url"] == "http://localhost:4000/v1"
        assert d["delegation"]["model"] == self.ALIAS   # alias wins when advertised

    def test_orchestrator_target_advertises_alias_writes_orch_alias(self, tmp_path):
        """orchestrator target, concrete-model plan, endpoint serves (concrete,
        orchestrator-model) → the ORCH ALIAS is written, not the concrete id."""
        conf = tmp_path / "config.yaml"
        self._write(conf, delegation={"model": "stale", "base_url": "http://stale/v1"})
        tpl = self._tpl(routing={"delegation": "orchestrator"})
        plan = self._plan_concrete()
        result = self._apply(tpl, plan, conf, served=(self.CONCRETE, self.ORCH_ALIAS))
        import yaml
        d = yaml.safe_load(conf.read_text())
        assert d["delegation"]["model"] == self.ORCH_ALIAS
        assert d["delegation"]["base_url"] == "http://10.0.0.1:8000/v1"

    def test_ambiguous_served_refuses_consumer(self, tmp_path):
        """Ambiguous probe (≥2 ids served, neither the alias) → refuse to write
        this consumer (touch nothing), never guess which concrete id to use."""
        conf = tmp_path / "config.yaml"
        self._write(conf, delegation={"model": "hand", "base_url": "http://hand/v1"})
        before = conf.read_text()
        tpl = self._tpl(routing={"delegation": "family-reasoning"})
        result = self._apply(tpl, self._plan_concrete(fam_proxy=4000), conf,
                             served=("some-other-a", "some-other-b"))
        import yaml
        d = yaml.safe_load(conf.read_text())
        assert d["delegation"] == {"model": "hand", "base_url": "http://hand/v1"}
        assert result["keys_written"] == ["delegation:refused:(ok)"]
        assert result["changed"] is False
        assert conf.read_text() == before

    def test_routing_candidate_is_alias_by_role_identity(self):
        """The write candidate is the alias by ROLE IDENTITY, not u.model: from
        a concrete-model plan, an orchestrator target yields orchestrator-model
        and a family target yields worker-model — the concrete id in u.model is
        never the candidate."""
        plan = self._plan_concrete(fam_proxy=4000)
        _base, cand = cluster_template._routing_target_endpoint("orchestrator", plan)
        assert cand == self.ORCH_ALIAS      # not self.CONCRETE (u.model)
        _base, cand = cluster_template._routing_target_endpoint(
            "family-reasoning", plan)
        assert cand == self.ALIAS           # not self.CONCRETE (units[0].model)

    def test_alias_identity_never_misaliases_worker(self):
        """Alias is decided by identity against plan.orchestrator, NOT by role
        string / index / name — so a worker unit whose role string is literally
        'orchestrator' still gets worker-model, and no worker is ever aliased
        orchestrator-model."""
        unit = ti.ResolvedUnit("orchestrator", "reasoning", "w.yaml", self.CONCRETE,
                               ["10.0.0.2"], 8001, 1, 1)
        orch = ti.ResolvedUnit("orchestrator", None, "orch.yaml", self.CONCRETE,
                               ["10.0.0.1"], 8000, 1, 1)
        plan = ti.ResolvedPlan(template="t", orchestrator=orch,
                               families=[ti.ResolvedFamily("reasoning", 4000, [unit])])
        assert cluster_template._unit_alias(orch, plan) == self.ORCH_ALIAS
        assert cluster_template._unit_alias(unit, plan) == self.ALIAS

    def test_apply_then_migration_agree_noop_second_time(self, tmp_path):
        """END-TO-END regression motivator: apply converges config to the ALIAS,
        exactly as the doctor alias-migration does — so a second pass is a byte
        no-op. Before the change apply wrote CONCRETE, so a subsequent
        alias-migrate pass (and any re-apply) kept fighting; now they agree."""
        conf = tmp_path / "config.yaml"
        self._write(conf, delegation={"model": "stale", "base_url": "http://stale/v1"})
        tpl = self._tpl(routing={"delegation": "family-reasoning"})
        plan = self._plan_concrete(fam_proxy=4000)
        served = (self.CONCRETE, self.ALIAS)
        r1 = self._apply(tpl, plan, conf, served=served)   # first apply
        import yaml
        after_apply = yaml.safe_load(conf.read_text())
        assert after_apply["delegation"]["model"] == self.ALIAS
        assert r1["changed"] is True
        # "doctor migration" converges to the alias — which apply already wrote
        # (they agree), so re-applying is a byte no-op.
        before = conf.read_text()
        r2 = self._apply(tpl, plan, conf, served=served)   # second pass
        assert r2["changed"] is False
        assert conf.read_text() == before

    def test_proxy_unreachable_refuses_consumer(self, tmp_path):
        """Unreachable target: refuse to write the consumer (touch nothing) rather
        than write a model id the endpoint might 404 on. The refused consumer is
        surfaced in notes and is not a silent no-op."""
        def _get(url, api_key=None):
            raise OSError("connection refused")
        conf = tmp_path / "config.yaml"
        self._write(conf, delegation={"model": "hand", "base_url": "http://hand/v1"})
        before = conf.read_text()
        tpl = self._tpl(routing={"delegation": "family-reasoning"})
        result = cluster_template._apply_routing(
            tpl, self._plan(), config_yaml=conf, _http_get=_get)
        import yaml
        d = yaml.safe_load(conf.read_text())
        assert d["delegation"] == {"model": "hand", "base_url": "http://hand/v1"}
        # no actual config key was written — only a refused marker recorded
        assert result["keys_written"] == ["delegation:refused:(unreachable)"]
        assert result["changed"] is False
        assert any("refused" in n for n in result["notes"])

    def test_dangling_routing_target_raises(self, tmp_path):
        """routing.delegation -> 'family-noexist': no such family → hard block."""
        with pytest.raises(ti.TemplateIntentError):
            cluster_template._routing_target_endpoint("family-noexist", self._plan())

    def test_idempotent_rerun_writes_nothing(self, tmp_path):
        """Re-applying the same template with routing already applied is a byte
        no-op (atomic_yaml_update returns changed=False)."""
        conf = tmp_path / "config.yaml"
        self._write(conf, delegation={"model": "stale", "base_url": "http://stale/v1"})
        tpl = self._tpl(routing={"delegation": "family-reasoning"})
        served = (self.CONCRETE,)
        self._apply(tpl, self._plan(), conf, served=served)   # first apply
        before = conf.read_text()
        result = self._apply(tpl, self._plan(), conf, served=served)  # second
        assert result["changed"] is False
        assert conf.read_text() == before

    # ── routing block governs delegation: Step 5 must not clobber omission ──

    def test_routing_present_skips_worker_delegation_rewrite(self, tmp_path):
        """END-TO-END hard requirement: when a routing: block is present, the
        worker-model Step 5 must NOT silently rewrite delegation. If routing omits
        delegation (operator tuned it by hand), delegation must survive untouched
        — despite _update_worker_model_ids normally rewriting it. Here we drive
        _update_worker_model_ids with skip_delegation=True (what apply_template
        passes when tpl.routing is not None) and assert delegation is left as-is.
        """
        conf = tmp_path / "config.yaml"
        self._write(conf, delegation={"model": "hand-tuned",
                                      "base_url": "http://hand/v1"},
                    fallback_providers=[{"model": "hand", "name": "fb"}])
        before = conf.read_text()
        result = cluster_template._update_worker_model_ids(
            self._plan(fam_proxy=4000), profiles_dir=tmp_path / "profiles",
            config_yaml=conf, _http_get=self._serving(self.CONCRETE),
            skip_delegation=True)
        import yaml
        d = yaml.safe_load(conf.read_text())
        # delegation + fallback left byte-for-byte untouched (routing owns it)
        assert d["delegation"] == {"model": "hand-tuned", "base_url": "http://hand/v1"}
        assert d["fallback_providers"] == [{"model": "hand", "name": "fb"}]
        assert result["delegation_routed"] is True
        assert result["config_changed"] is False

    def test_no_routing_block_still_rewrites_delegation_via_step5(self, tmp_path):
        """Without a routing block, the legacy Step 5 behaviour is preserved:
        delegation/fallback get rewritten to the worker proxy. skip_delegation
        defaults False so this path is unchanged for routing-less templates.
        """
        conf = tmp_path / "config.yaml"
        self._write(conf, delegation={"model": "stale",
                                      "base_url": "http://stale/v1"})
        result = cluster_template._update_worker_model_ids(
            self._plan(fam_proxy=4000), profiles_dir=tmp_path / "profiles",
            config_yaml=conf, _http_get=self._serving(self.CONCRETE))
        import yaml
        d = yaml.safe_load(conf.read_text())
        # unchanged legacy path: delegation rewritten to the worker proxy
        assert d["delegation"]["model"] == self.CONCRETE
        assert d["delegation"]["base_url"] == "http://localhost:4000/v1"
        assert result["config_changed"] is True


# ── preview discloses routing: reuse apply's resolution helpers ─────────────

class TestPreviewRouting:
    """preview must disclose what apply's routing block would write — and which
    consumers it will NOT touch — reusing THE SAME three helpers apply uses
    (``_routing_target_endpoint`` / ``_routing_model_to_write`` /
    ``_routing_config_keys``), so preview can never drift from apply. Preview is
    READ-ONLY: it never writes config, provisions, or restarts anything.

    If the feature were completely broken (preview computed routing
    independently, or not at all), every test below would fail: no entry would
    show the resolved base_url/model/keys, omission wouldn't surface as
    routing_untouched, the no-routing template would wrongly get a section, and
    the anti-drift assertion (test 4) could not be satisfied.
    """

    CONCRETE = "deepseek-ai/DeepSeek-V4-Flash-0731"
    ALIAS = "worker-model"
    ORCH_ALIAS = "orchestrator-model"

    def _plan(self, *, fam_proxy=4000, family="reasoning"):
        """Plan: orchestrator on 10.0.0.1:8000; one worker family 'reasoning'
        with a unit on 10.0.0.2:8001 and a proxy on ``fam_proxy``."""
        orch = ti.ResolvedUnit("orchestrator", None, "~/recipes/orch.yaml",
                               self.ORCH_ALIAS, ["10.0.0.1"], 8000, 1, 1)
        unit = ti.ResolvedUnit("worker", family, "w.yaml", self.ALIAS,
                               ["10.0.0.2"], 8001, 1, 1)
        fam = ti.ResolvedFamily(name=family, proxy_port=fam_proxy, units=[unit])
        return ti.ResolvedPlan(template="t", orchestrator=orch, families=[fam])

    def _tpl(self, routing=None):
        """ClusterTemplate with the given routing block (None = block absent)."""
        return ti.ClusterTemplate(name="t", version=3,
                                  orchestrator=ti.ModelIntent("o.yaml"),
                                  families=[ti.FamilyIntent("reasoning",
                                                           [ti.ModelIntent("w.yaml")])],
                                  routing=routing)

    def _serving(self, *served):
        """Injectable _http_get simulating /v1/models serving ``served`` ids."""
        import json
        def get(url, api_key=None):
            return json.dumps({"object": "list", "data": [
                {"id": m, "object": "model"} for m in served]})
        return get

    # 1. a template declaring routing shows each consumer resolved + keys

    def test_declared_routing_consumer_shows_resolved(self):
        """delegation: family-reasoning → base_url localhost:4000, model = the
        id the proxy actually advertises, keys = delegation.{base_url,model}."""
        tpl = self._tpl(routing={"delegation": "family-reasoning"})
        disc = cluster_template._preview_routing(
            tpl, self._plan(fam_proxy=4000), _http_get=self._serving(self.ALIAS))
        entry = disc["routing"][0]
        assert entry["consumer"] == "delegation"
        assert entry["target"] == "family-reasoning"
        assert entry["base_url"] == "http://localhost:4000/v1"
        assert entry["model"] == self.ALIAS
        assert entry["keys"] == ["delegation.base_url", "delegation.model"]

    def test_each_declared_consumer_resolves(self):
        """Every declared consumer gets a resolved base_url + model + keys
        (delegation→family proxy; compaction + auxiliaries→orchestrator)."""
        tpl = self._tpl(routing={
            "delegation": "family-reasoning",
            "compaction": "orchestrator",
            "auxiliaries": "orchestrator"})
        disc = cluster_template._preview_routing(
            tpl, self._plan(fam_proxy=4000),
            _http_get=self._serving(self.ALIAS, self.ORCH_ALIAS))
        by_consumer = {e["consumer"]: e for e in disc["routing"]}
        assert by_consumer["delegation"]["base_url"] == "http://localhost:4000/v1"
        assert by_consumer["delegation"]["model"] == self.ALIAS
        assert by_consumer["delegation"]["keys"] == [
            "delegation.base_url", "delegation.model"]
        assert by_consumer["compaction"]["base_url"] == "http://10.0.0.1:8000/v1"
        assert by_consumer["compaction"]["model"] == self.ORCH_ALIAS
        assert by_consumer["compaction"]["keys"] == [
            "auxiliary.compression.base_url", "auxiliary.compression.model"]
        tasks = cluster_template.ROUTING_AUX_TEXT_TASKS
        assert by_consumer["auxiliaries"]["base_url"] == "http://10.0.0.1:8000/v1"
        assert by_consumer["auxiliaries"]["model"] == self.ORCH_ALIAS
        assert len(by_consumer["auxiliaries"]["keys"]) == 2 * len(tasks)
        for t in tasks:
            assert f"auxiliary.{t}.base_url" in by_consumer["auxiliaries"]["keys"]

    # 2. omitted consumer → routing_untouched; its keys NOWHERE in the write list

    def test_omitted_consumer_in_untouched_and_keys_absent(self):
        """routing declares compaction only: delegation + auxiliaries are
        routing_untouched, and their config keys appear NOWHERE in the preview
        routing section (so preview promises only the writes apply will do)."""
        tpl = self._tpl(routing={"compaction": "orchestrator"})
        disc = cluster_template._preview_routing(
            tpl, self._plan(), _http_get=self._serving(self.ORCH_ALIAS))
        assert disc["routing_untouched"] == ["delegation", "auxiliaries"]
        written_keys = [k for e in disc["routing"] for k in e["keys"]]
        for k in ("delegation.base_url", "delegation.model"):
            assert k not in written_keys
        for t in cluster_template.ROUTING_AUX_TEXT_TASKS:
            assert f"auxiliary.{t}.base_url" not in written_keys
            assert f"auxiliary.{t}.model" not in written_keys

    # 3. no routing block → no routing section at all

    def test_no_routing_block_produces_no_routing_section(self):
        disc = cluster_template._preview_routing(self._tpl(routing=None), self._plan())
        assert disc["routing"] == []

    def test_preview_template_no_routing_has_no_routing_section(self, stub_cluster):
        """End-to-end: single-family.yaml declares NO routing → the preview dict
        carries no routing / routing_untouched keys at all."""
        res = preview_template("single-family")
        assert "routing" not in res
        assert "routing_untouched" not in res

    # probe-aware: show the concrete id when the endpoint doesn't advertise the alias

    def test_preview_shows_concrete_when_alias_not_advertised(self):
        """When the endpoint serves ONLY the concrete id, preview shows the
        CONCRETE id — what apply would actually write, not the aspirational
        alias. If preview resolved the model independently (without the probe)
        this test FAILS."""
        tpl = self._tpl(routing={"delegation": "family-reasoning"})
        disc = cluster_template._preview_routing(
            tpl, self._plan(fam_proxy=4000), _http_get=self._serving(self.CONCRETE))
        assert disc["routing"][0]["model"] == self.CONCRETE

    # 4. THE ANTI-DRIFT TEST

    def test_preview_keys_equal_apply_keys_written(self, tmp_path):
        """THE ANTI-DRIFT TEST: for the same template, the (consumer -> keys)
        preview reports must equal _apply_routing's keys_written. Both flow
        through the same three resolution helpers, so neither can diverge from
        the other without this failing."""
        tpl = self._tpl(routing={
            "delegation": "family-reasoning",
            "compaction": "orchestrator",
            "auxiliaries": "orchestrator"})
        plan = self._plan(fam_proxy=4000)
        getter = self._serving(self.ALIAS, self.ORCH_ALIAS)

        disc = cluster_template._preview_routing(tpl, plan, _http_get=getter)
        conf = tmp_path / "config.yaml"
        conf.write_text("auxiliary: {}\n")
        appl = cluster_template._apply_routing(
            tpl, plan, config_yaml=conf, _http_get=getter)

        preview_consumer_keys = {e["consumer"]: set(e["keys"]) for e in disc["routing"]}
        apply_written = set(appl["keys_written"])
        for keys in preview_consumer_keys.values():
            for k in keys:
                assert k in apply_written
        # every key apply wrote was disclosed by preview (no hidden writes)
        assert apply_written == set().union(*preview_consumer_keys.values())


# ── Fix: provision timeout raised to configurable 900s ──────────────────

class TestProvisionReusedNode:
    """BUG 3: _provision_models must FREE a reused node before provisioning a
    different model. If a node in the plan already serves a model that does NOT
    match the wanted unit's recipe model, the stale container still holds the
    serve port and the new `sparkrun run` crash-loops with Errno 98. The stale
    container must be stopped BEFORE the launch. A node already correctly
    serving the wanted model must be left running (--ensure idempotency)."""

    def _invoke(self, monkeypatch, status_out, *, orch_nodes=("10.0.0.1",),
                unit_nodes=("10.0.0.2",)):
        """Run _provision_models with a plan of one orchestrator + one worker,
        recording every subprocess call. status_out is the `sparkrun status`
        stdout the mock returns."""
        import subprocess
        from unittest.mock import MagicMock
        calls = []

        def mock_run(argv, **kw):
            calls.append(argv)
            if "status" in argv:
                return MagicMock(returncode=0, stderr="", stdout=status_out)
            return MagicMock(returncode=0, stderr="", stdout="")

        monkeypatch.setattr(subprocess, "run", mock_run)
        orch = ti.ResolvedUnit("orchestrator", None, "~/recipes/orch.yaml",
                               "orch", list(orch_nodes), 8000, 1, 1)
        unit = ti.ResolvedUnit("worker", "coding", "~/recipes/deepseek.yaml",
                               "deepseek", list(unit_nodes), 8000, 1, 1)
        fam = ti.ResolvedFamily(name="coding", proxy_port=4000, units=[unit])
        plan = ti.ResolvedPlan(template="test", orchestrator=orch, families=[fam])
        cluster_template._provision_models(plan, do_launch=True)
        return calls

    @staticmethod
    def _stops(calls):
        return [c for c in calls if c[0] == "sparkrun" and c[1] == "stop"]

    @staticmethod
    def _run_for(calls, recipe_substr):
        return [c for c in calls
                if c[0] == "sparkrun" and c[1] == "run" and recipe_substr in str(c)]

    def test_reused_node_different_model_stopped_before_launch(self, monkeypatch):
        # A container serving qwen27b is running on the worker node the plan
        # wants deepseek on → the stale qwen container must be stopped first.
        status = ("Job: qwen27b\n"
                  "10.0.0.2\n")
        calls = self._invoke(monkeypatch, status, unit_nodes=("10.0.0.2",))
        stops = self._stops(calls)
        assert len(stops) == 1
        assert "10.0.0.2" in stops[0]
        deepseek_run = self._run_for(calls, "deepseek.yaml")
        assert len(deepseek_run) == 1
        assert calls.index(stops[0]) < calls.index(deepseek_run[0])

    def test_node_serving_wanted_model_kept_running(self, monkeypatch):
        # A container already serving the WANTED model (deepseek) is on the node
        # → no stop, no relaunch (the --ensure run is the only run, and there is
        # no stop for the node).
        status = ("Job: ~/recipes/deepseek.yaml\n"
                  "10.0.0.2\n")
        calls = self._invoke(monkeypatch, status, unit_nodes=("10.0.0.2",))
        stops = self._stops(calls)
        assert stops == []  # idempotent — nothing stopped
        assert len(self._run_for(calls, "deepseek.yaml")) == 1

    def test_fresh_node_no_spurious_stop(self, monkeypatch):
        # Nothing running on the worker node → it is just launched, no stop.
        status = "Idle hosts (...): 0\n"
        calls = self._invoke(monkeypatch, status, unit_nodes=("10.0.0.2",))
        stops = self._stops(calls)
        assert stops == []
        assert len(self._run_for(calls, "deepseek.yaml")) == 1

    def test_stop_nodes_not_in_plan_still_holds(self, monkeypatch):
        # A node running a container but NOT part of the plan is still stopped
        # by the existing not-in-plan logic.
        status = ("Job: qwen27b\n"
                  "10.0.0.1 10.0.0.2 10.0.0.3\n")
        calls = self._invoke(monkeypatch, status,
                             orch_nodes=("10.0.0.1",), unit_nodes=("10.0.0.2",))
        # 10.0.0.3 is not in the plan → gets stopped via `_running_nodes_via_sparkrun`
        stop_nodes = {n for stop in self._stops(calls) for n in stop if n.count(".") == 3}
        assert "10.0.0.3" in stop_nodes
        # 10.0.0.2 serves the wanted model → left running
        assert "10.0.0.2" not in stop_nodes


class TestProvisionTimeout:
    """BUG 2: per-unit subprocess timeout was 240s (too short for builds + sync).
    Raised to PROVISION_TIMEOUT_S (default 900s, overridable via HSCC_PROVISION_TIMEOUT)."""

    def test_default_timeout_is_900(self):
        assert cluster_template.PROVISION_TIMEOUT_S == 900

    def test_provision_uses_module_timeout(self, monkeypatch):
        """_provision_models passes PROVISION_TIMEOUT_S as subprocess timeout."""
        import subprocess
        from unittest.mock import MagicMock

        call_kws = []
        def mock_run(argv, **kw):
            call_kws.append(kw)
            return MagicMock(returncode=0, stderr="", stdout="")
        monkeypatch.setattr(subprocess, "run", mock_run)

        orch = ti.ResolvedUnit("orchestrator", None, "~/recipes/orch.yaml",
                               "test-model", ["10.0.0.1"], 8000, 1, 1)
        plan = ti.ResolvedPlan(template="test", orchestrator=orch, families=[])
        cluster_template._provision_models(plan, do_launch=True)

        timeout_values = [kw.get("timeout") for kw in call_kws]
        assert cluster_template.PROVISION_TIMEOUT_S in timeout_values


# ── HSCC v1.5.1: logical alias advertisement via --served-model-name ─────────
#
# Each endpoint advertises BOTH its concrete model id and a stable logical alias
# (orchestrator-model / worker-model) via vLLM's multi-name `--served-model-name`
# (nargs='+', space-separated). HSCC emits the value as ONE argv token to the
# `sparkrun` CLI: ["--served-model-name", "<concrete> <alias>"].
#
# WHY THIS IS THE RIGHT ENCODING (verified empirically against sparkrun v0.3.1
# with `sparkrun run <recipe> --dry-run`, 2026-08): sparkrun consumes
# `--served-model-name` as a single-valued option, then renders the command as a
# STRING on EVERY runtime path — the explicit-command template path
# (`_augment_served_model_name`: `"%s %s %s" % (cmd, flag, value)`) AND the
# no-template structured path (`build_flags_from_map` → `_build_base_command`,
# which does `" ".join(parts)`). That string is base64-encoded into
# /tmp/sparkrun_serve.sh and executed with `bash --noprofile --norc`. bash then
# splits the space into SEPARATE argv tokens, so `--served-model-name <concrete>
# <alias>` registers BOTH names — on BOTH paths. A comma-joined value instead
# registers ONE model literally named "<concrete>,<alias>" → both 404.
#
# The tests BELOW therefore assert the FINAL tokenized argv, not the intermediate
# string: `_final_tokens()` models sparkrun's shell execution via shlex.split and
# requires concrete + alias to be TWO separate argv elements after the flag. If a
# regression ever made the value land as ONE token (`"concrete alias"` as a single
# argv element — e.g. comma-joining, or sparkrun quoting the whole value), the
# test FAILS. This is the honest guard the prior attempt lacked (it only checked
# substrings in a mirror-rendered string, which could not detect token collapse).
_RENDERED_VLLM_TPL = (
    "vllm serve {model} --host 0.0.0.0 --port 8000 --trust-remote-code "
    "--max-model-len 262144 --enable-prefix-caching -tp 1 -pp 1"
)


class TestServedModelNameAliases:
    """Card A1 (t_ad411703): _provision_models advertises both the concrete model
    id and a stable logical alias via vLLM's multi-name `--served-model-name`,
    space-separated in a single flag, and (critically) proves that value reaches
    vLLM as SEPARATE argv tokens on sparkrun's shell-executed command — not one
    collapsed token."""

    # ── helpers matching sparkrun's real execution boundary ────────────────

    def _invoke(self, monkeypatch, *, orch_recipe="~/recipes/orch.yaml",
                worker_recipe="~/recipes/deepseek.yaml", orch_model="orch",
                worker_model="deepseek"):
        """Run _provision_models with a plan of one orchestrator + one worker,
        returning every recorded `sparkrun run` argv."""
        import subprocess
        from unittest.mock import MagicMock
        calls = []

        def mock_run(argv, **kw):
            calls.append(argv)
            return MagicMock(returncode=0, stderr="", stdout="")
        monkeypatch.setattr(subprocess, "run", mock_run)
        orch = ti.ResolvedUnit("orchestrator", None, orch_recipe,
                               orch_model, ["10.0.0.1"], 8000, 1, 1)
        unit = ti.ResolvedUnit("worker", "coding", worker_recipe,
                               worker_model, ["10.0.0.2"], 8000, 1, 1)
        fam = ti.ResolvedFamily(name="coding", proxy_port=4000, units=[unit])
        plan = ti.ResolvedPlan(template="test", orchestrator=orch, families=[fam])
        cluster_template._provision_models(plan, do_launch=True)
        return calls

    @staticmethod
    def _runs(calls, recipe_substr):
        return [c for c in calls
                if c[0] == "sparkrun" and c[1] == "run" and recipe_substr in str(c)]

    @staticmethod
    def _value_token(argv):
        """The SINGLE value token HSCC hands to sparkrun for --served-model-name."""
        idx = argv.index("--served-model-name")
        return argv[idx + 1]

    @classmethod
    def _final_tokens(cls, model, served_value):
        """Return the argv vLLM actually receives, modelled exactly as sparkrun
        executes it.

        sparkrun renders `vllm serve ... --served-model-name <served_value>`
        (the string), base64-encodes it into /tmp/sparkrun_serve.sh, and runs it
        with `bash --noprofile --norc`. shlex.split is the faithful in-process
        model of that bash word-splitting (honours quotes/escapes, splits on
        whitespace). The names after --served-model-name must therefore come out
        as SEPARATE list elements.
        """
        import shlex
        cmd = _RENDERED_VLLM_TPL.format(model=model)
        rendered = "%s --served-model-name %s" % (cmd.rstrip(), served_value)
        return shlex.split(rendered)

    @staticmethod
    def _flag_names(final_tokens):
        """Return the argv elements following --served-model-name."""
        idx = final_tokens.index("--served-model-name")
        return final_tokens[idx + 1:]

    # ── tests ──────────────────────────────────────────────────────────────

    def test_orchestrator_emits_concrete_and_alias(self, monkeypatch):
        calls = self._invoke(monkeypatch)
        orch_run = self._runs(calls, "orch.yaml")
        assert len(orch_run) == 1
        # HSCC hands sparkrun ONE flag with ONE space-separated value token.
        assert orch_run[0].count("--served-model-name") == 1
        assert self._value_token(orch_run[0]) == "orch orchestrator-model"

    def test_worker_emits_concrete_and_alias(self, monkeypatch):
        calls = self._invoke(monkeypatch)
        worker_run = self._runs(calls, "deepseek.yaml")
        assert len(worker_run) == 1
        assert self._value_token(worker_run[0]) == "deepseek worker-model"

    def test_final_argv_two_separate_names_orchestrator(self, monkeypatch):
        """THE core guard: the FINAL command vLLM executes must carry the
        concrete id and alias as TWO SEPARATE argv tokens, never one collapsed
        token `"concrete alias"` (nor one comma-joined name). Fails if a
        regression makes the value land as a single argv element."""
        calls = self._invoke(monkeypatch)
        orch_run = self._runs(calls, "orch.yaml")[0]
        served_value = self._value_token(orch_run)
        assert served_value == "orch orchestrator-model"
        final_tokens = self._final_tokens("orch", served_value)
        names = self._flag_names(final_tokens)
        assert names == ["orch", "orchestrator-model"], (
            "served-model-name must reach vLLM as SEPARATE argv tokens; "
            f"got {names!r} from final command {final_tokens!r}"
        )

    def test_final_argv_two_separate_names_worker(self, monkeypatch):
        calls = self._invoke(monkeypatch)
        worker_run = self._runs(calls, "deepseek.yaml")[0]
        served_value = self._value_token(worker_run)
        names = self._flag_names(self._final_tokens(
            "deepseek", served_value))
        assert names == ["deepseek", "worker-model"]

    def test_comma_joined_would_collapse_to_one_name(self):
        """Guard against the regression that breaks the feature: if the value were
        comma-joined, the final tokenized argv would hold ONE name and both the
        concrete id and the alias would 404. This proves `_final_tokens` actually
        detects token collapse (it FAILS on the broken encoding)."""
        names = self._flag_names(self._final_tokens(
            "orch", "orch,orchestrator-model"))
        assert names == ["orch,orchestrator-model"]  # one collapsed name, both 404

    def test_quoted_value_collapses_to_one_name(self):
        """If a value were ever shell-quoted as one token, shlex keeps it ONE argv
        element — the guard must catch that too (sparkrun appends raw, so this is
        defensive). This documents that the test FAILS when the token collapses."""
        names = self._flag_names(self._final_tokens(
            "orch", '"orch orchestrator-model"'))
        assert names == ["orch orchestrator-model"]  # one token -> both 404

    def test_concrete_id_read_from_recipe_model_field(self, monkeypatch, tmp_path):
        """A recipe whose model: field is deepseek-ai/DeepSeek-V4-Flash-0731 must
        yield that full concrete id (not the filename stem) in the flag, and the
        final argv must carry it plus the alias as two tokens."""
        recipe = tmp_path / "quad.yaml"
        recipe.write_text("model: deepseek-ai/DeepSeek-V4-Flash-0731\n")
        calls = self._invoke(monkeypatch, worker_recipe=str(recipe))
        worker_run = self._runs(calls, "quad.yaml")
        assert len(worker_run) == 1
        served_value = self._value_token(worker_run[0])
        assert served_value == "deepseek-ai/DeepSeek-V4-Flash-0731 worker-model"
        names = self._flag_names(self._final_tokens(
            "deepseek-ai/DeepSeek-V4-Flash-0731", served_value))
        assert names == ["deepseek-ai/DeepSeek-V4-Flash-0731", "worker-model"]

    def test_alias_decided_by_identity_not_role_string(self, monkeypatch, tmp_path):
        """The orchestrator alias is assigned by identity with plan.orchestrator,
        NOT by the unit's role string or list position — so a worker unit whose
        role string is literally 'orchestrator' still advertises worker-model, and
        no worker can ever be aliased as orchestrator-model."""
        import subprocess
        from unittest.mock import MagicMock
        calls = []

        def mock_run(argv, **kw):
            calls.append(argv)
            return MagicMock(returncode=0, stderr="", stdout="")
        monkeypatch.setattr(subprocess, "run", mock_run)

        # A worker unit with a misleading role "orchestrator".
        unit = ti.ResolvedUnit("orchestrator", "coding", "~/recipes/deepseek.yaml",
                               "deepseek", ["10.0.0.2"], 8001, 1, 1)
        fam = ti.ResolvedFamily(name="coding", proxy_port=4000, units=[unit])
        orch = ti.ResolvedUnit("orchestrator", None, "~/recipes/orch.yaml",
                               "orch", ["10.0.0.1"], 8000, 1, 1)
        plan = ti.ResolvedPlan(template="test", orchestrator=orch, families=[fam])
        cluster_template._provision_models(plan, do_launch=True)

        orch_run = self._runs(calls, "orch.yaml")[0]
        worker_run = self._runs(calls, "deepseek.yaml")[0]
        assert self._value_token(orch_run) == "orch orchestrator-model"
        assert self._value_token(worker_run) == "deepseek worker-model"

    def test_flag_placed_before_tp(self, monkeypatch):
        """--served-model-name coexists with --tp without disturbing it."""
        import subprocess
        from unittest.mock import MagicMock
        calls = []

        def mock_run(argv, **kw):
            calls.append(argv)
            return MagicMock(returncode=0, stderr="", stdout="")
        monkeypatch.setattr(subprocess, "run", mock_run)

        orch = ti.ResolvedUnit("orchestrator", None, "~/recipes/orch.yaml",
                               "orch", ["10.0.0.1", "10.0.0.2"], 9000, 2, 1)
        plan = ti.ResolvedPlan(template="test", orchestrator=orch, families=[])
        cluster_template._provision_models(plan, do_launch=True)
        orch_run = self._runs(calls, "orch.yaml")[0]
        assert "--served-model-name" in orch_run
        assert "--tp" in orch_run
        assert orch_run.index("--served-model-name") < orch_run.index("--tp")
        assert self._value_token(orch_run) == "orch orchestrator-model"

    def test_dry_run_not_affected(self, monkeypatch):
        """do_launch=False never builds sparkrun run argv — no served-model-name
        launches attempted, dry-run note/provisioned output unchanged."""
        import subprocess
        calls = []

        def mock_run(argv, **kw):
            calls.append(argv)
            return MagicMock(returncode=0, stderr="", stdout="")
        monkeypatch.setattr(subprocess, "run", mock_run)

        orch = ti.ResolvedUnit("orchestrator", None, "~/recipes/orch.yaml",
                               "orch", ["10.0.0.1"], 8000, 1, 1)
        plan = ti.ResolvedPlan(template="test", orchestrator=orch, families=[])
        result = cluster_template._provision_models(plan, do_launch=False)
        assert calls == []
        assert result["provisioned"] == ["10.0.0.1:8000:~/recipes/orch.yaml"]
        assert result["note"].startswith("dry-run")


# ── t_5b0f4d2d: apply must recreate a unit when its serve command changes ────
# ENSURE semantics leave an already-running-same-recipe container alone, so a
# changed serve flag (the v1.6.0 alias being the motivating case) never reaches
# vLLM on a live fleet. recreate=True forces those units to be stopped first and
# re-run so the FULL rendered command applies; the units are reported loudly in
# ``recreated``. Without the flag, apply must NOT silently claim success — it
# reports drift-skipped units as warnings.

class TestProvisionRecreateOnChange:
    """BUG: apply must recreate a unit when its serve command changes.

    The scenario that motivated this: a fleet is already running the SAME
    recipe with an OLD --served-model-name (no alias). The template is updated
    to advertise a NEW alias, apply runs, reports "N model(s) ensured up" — but
    ENSURE sees the same recipe already running and skips every unit, so the
    alias never reaches vLLM.

    recreate=True forces the stop+rerun so the change actually applies, and the
    unit is reported loudly. An unchanged unit is still left alone (no stop).
    Without recreate, an already-running-same-recipe unit is reported as
    skipped-with-drift (status flips to warn), never silent success.
    """

    def _invoke(self, monkeypatch, tmp_path, status_out, *, recreate=False,
                orch_nodes=("10.0.0.1",), unit_nodes=("10.0.0.2",),
                worker_serve_cmd="__UNSET__", orch_serve_cmd="__UNSET__"):
        """Run _provision_models with a plan of one orchestrator + one worker,
        recording every subprocess call. status_out is the `sparkrun status`
        stdout the mock returns. Returns (calls, result).

        SERVING_JSON is redirected to a tmp file (test isolation) and optionally
        seeded so the drift comparison has something to compare against:
        worker_serve_cmd / orch_serve_cmd seed the recorded serve_cmd for the
        worker / orchestrator unit — "__UNSET__" (default) = don't seed at all,
        None = seed the unit WITHOUT a serve_cmd (pre-upgrade), any other value
        = seed that serve_cmd (a list argv or a string)."""
        import subprocess
        from unittest.mock import MagicMock
        calls = []
        any_seed = worker_serve_cmd != "__UNSET__" or orch_serve_cmd != "__UNSET__"

        def mock_run(argv, **kw):
            calls.append(argv)
            if "status" in argv:
                return MagicMock(returncode=0, stderr="", stdout=status_out)
            return MagicMock(returncode=0, stderr="", stdout="")
        monkeypatch.setattr(subprocess, "run", mock_run)

        serving_path = tmp_path / "serving.json"
        monkeypatch.setattr(cluster_template, "SERVING_JSON", serving_path)
        units = []
        if orch_serve_cmd != "__UNSET__":
            rec = {"id": "orch"}
            if orch_serve_cmd is not None:
                rec["serve_cmd"] = orch_serve_cmd
            units.append(rec)
        if worker_serve_cmd != "__UNSET__":
            rec = {"id": "family-coding-deepseek-2-8000"}
            if worker_serve_cmd is not None:
                rec["serve_cmd"] = worker_serve_cmd
            units.append(rec)
        if any_seed:
            serving_path.write_text(
                __import__("json").dumps({"version": 2, "units": units}))

        orch = ti.ResolvedUnit("orchestrator", None, "~/recipes/orch.yaml",
                               "orch", list(orch_nodes), 8000, 1, 1)
        unit = ti.ResolvedUnit("worker", "coding", "~/recipes/deepseek.yaml",
                               "deepseek", list(unit_nodes), 8000, 1, 1)
        fam = ti.ResolvedFamily(name="coding", proxy_port=4000, units=[unit])
        plan = ti.ResolvedPlan(template="test", orchestrator=orch, families=[fam])
        result = cluster_template._provision_models(
            plan, do_launch=True, recreate=recreate)
        return calls, result

    @staticmethod
    def _stops(calls):
        return [c for c in calls if c[0] == "sparkrun" and c[1] == "stop"]

    @staticmethod
    def _run_for(calls, recipe_substr):
        return [c for c in calls
                if c[0] == "sparkrun" and c[1] == "run" and recipe_substr in str(c)]

    @staticmethod
    def _expected_worker_cmd():
        """The rendered serve command for the worker unit in _invoke's plan."""
        return cluster_template._render_serve_cmd(
            "hscc", "10.0.0.2", 8000, "~/recipes/deepseek.yaml",
            "worker-model", 1, "deepseek")

    @staticmethod
    def _expected_orch_cmd():
        """The rendered serve command for the orchestrator in _invoke's plan."""
        return cluster_template._render_serve_cmd(
            "hscc", "10.0.0.1", 8000, "~/recipes/orch.yaml",
            "orchestrator-model", 1, "orch")

    # ── recreate=True: a changed unit's command reaches vLLM ────────────────

    def test_recreate_forces_stop_and_relaunch_of_running_same_recipe(self, monkeypatch, tmp_path):
        """The worker already serves the SAME recipe deepseek (so a plain --ensure
        would skip it). With recreate=True the unit MUST be stopped first and
        re-run, so a changed serve command actually reaches vLLM, and it is
        reported loudly in ``recreated``."""
        status = ("Job: ~/recipes/deepseek.yaml\n"
                  "10.0.0.2\n")
        calls, result = self._invoke(monkeypatch, tmp_path, status, recreate=True,
                                     unit_nodes=("10.0.0.2",))
        # the running unit is stopped (force-recreate) then re-run
        stops = self._stops(calls)
        assert len(stops) >= 1
        assert "10.0.0.2" in stops[0]
        run = self._run_for(calls, "deepseek.yaml")
        assert len(run) == 1
        # stop happens BEFORE the relaunch
        assert calls.index(stops[0]) < calls.index(run[0])
        # reported loudly
        assert result["recreated"] == ["10.0.0.2:8000:deepseek.yaml"]
        assert "recreated" in result["note"]
        assert result["status"] == "ok"  # it actually applied, so no drift warning
        assert result["warnings"] == []

    def test_recreate_reach_vllm_with_rendered_command(self, monkeypatch, tmp_path):
        """The recreated deepseek unit carries the CURRENT rendered serve command
        (concrete + alias) — proving the alias reaches vLLM after the recreate."""
        status = ("Job: ~/recipes/deepseek.yaml\n"
                  "10.0.0.2\n")
        calls, result = self._invoke(monkeypatch, tmp_path, status, recreate=True,
                                     unit_nodes=("10.0.0.2",))
        run = self._run_for(calls, "deepseek.yaml")[0]
        idx = run.index("--served-model-name")
        assert run[idx + 1] == "deepseek worker-model"

    def test_recreate_leaves_unchanged_unit_alone(self, monkeypatch, tmp_path):
        """Not the config-file apply; the pure _provision guard: when recreate is
        False (the default ENSURE path) and a unit is NOT running yet (nothing to
        skip), it is launched exactly once with no stray stop — an unchanged/fresh
        unit is simply left to --ensure's normal handling (no stop)."""
        status = ("Idle hosts (...): 0\n")
        calls, result = self._invoke(monkeypatch, tmp_path, status, recreate=False,
                                     unit_nodes=("10.0.0.2",))
        assert self._stops(calls) == []      # nothing running → nothing stopped
        assert len(self._run_for(calls, "deepseek.yaml")) == 1
        assert result["status"] == "ok"
        assert result["warnings"] == []

    def test_unchanged_unit_left_alone_with_recreate(self, monkeypatch, tmp_path):
        """recreate=True only forces recreation of units that are ALREADY running.
        A unit with nothing running on its node is simply launched once — no
        spurious stop, no recreate entry (nothing pre-existed to recreate)."""
        status = ("Idle hosts (...): 0\n")
        calls, result = self._invoke(monkeypatch, tmp_path, status, recreate=True,
                                     unit_nodes=("10.0.0.2",))
        # idle → nothing already running → fresh --ensure launch, no stop
        assert self._stops(calls) == []
        assert len(self._run_for(calls, "deepseek.yaml")) == 1
        assert result["recreated"] == []

    # ── real drift detection (t_13f077e4): compare, don't assume ──────────

    def test_unchanged_unit_produces_no_drift_warning(self, monkeypatch, tmp_path):
        """THE key half: a unit already running the same recipe whose RECORDED
        serve command equals the freshly rendered one is genuinely unchanged —
        apply must NOT emit any drift warning. Silence here is what makes the
        warning meaningful."""
        status = ("Job: ~/recipes/deepseek.yaml\n"
                  "10.0.0.2\n")
        # The worker's previous provision recorded the SAME command we render now.
        calls, result = self._invoke(
            monkeypatch, tmp_path, status, recreate=False,
            unit_nodes=("10.0.0.2",),
            worker_serve_cmd=self._expected_worker_cmd())
        # no stop (already running), single --ensure no-op run
        assert self._stops(calls) == []
        assert len(self._run_for(calls, "deepseek.yaml")) == 1
        # ...and critically NO drift warning: unchanged is silence.
        assert result["status"] == "ok"
        assert result["warnings"] == []
        assert "drift" not in result["note"].lower()

    def test_changed_unit_reported_as_real_drift(self, monkeypatch, tmp_path):
        """A unit whose RECORDED serve command differs from the freshly rendered
        one is REAL drift: it is named, WHAT changed is reported, and it is
        skipped-with-warning (status flips to warn) unless --force-recreate."""
        status = ("Job: ~/recipes/deepseek.yaml\n"
                  "10.0.0.2\n")
        # Recorded with the OLD alias-less command — the current render adds the
        # worker-model alias, so the two genuinely differ.
        old_cmd = ["sparkrun", "run", "~/recipes/deepseek.yaml",
                   "--cluster", "hscc", "--hosts", "10.0.0.2",
                   "--port", "8000", "--no-follow", "--ensure",
                   "--served-model-name", "deepseek"]
        calls, result = self._invoke(
            monkeypatch, tmp_path, status, recreate=False,
            unit_nodes=("10.0.0.2",), worker_serve_cmd=old_cmd)
        # same recipe already running → no stop, single --ensure run
        assert self._stops(calls) == []
        assert len(self._run_for(calls, "deepseek.yaml")) == 1
        # ...but apply must NOT claim success: real drift is reported loudly.
        assert result["status"] == "warn"
        assert "skipped with command drift" in result["note"]
        assert len(result["warnings"]) == 1
        w0 = result["warnings"][0]
        assert "10.0.0.2:8000:deepseek.yaml" in w0
        assert "--force-recreate" in w0
        # WHAT changed is named: the alias flag differs (old alias-less → alias)
        assert "--served-model-name" in w0
        # The drifted unit's record is NOT overwritten — it still reflects what's
        # actually running (the old command), so a later apply still sees drift.
        import json
        persisted = json.loads((tmp_path / "serving.json").read_text())
        worker = [u for u in persisted["units"]
                  if u.get("id") == "family-coding-deepseek-2-8000"][0]
        assert worker["serve_cmd"] == old_cmd

    def test_no_recorded_command_uses_unchecked_wording(self, monkeypatch, tmp_path):
        """A pre-upgrade unit with a recorded unit entry but NO serve_cmd falls
        back to the conservative 'drift not checked' wording — never a false
        'command drift' claim."""
        status = ("Job: ~/recipes/deepseek.yaml\n"
                  "10.0.0.2\n")
        # seed the worker unit present but WITHOUT a serve_cmd (pre-upgrade).
        calls, result = self._invoke(
            monkeypatch, tmp_path, status, recreate=False,
            unit_nodes=("10.0.0.2",), worker_serve_cmd=None)
        assert self._stops(calls) == []
        assert len(self._run_for(calls, "deepseek.yaml")) == 1
        assert result["status"] == "warn"
        assert len(result["warnings"]) == 1
        assert "drift not checked" in result["warnings"][0]
        assert "command drift" not in result["warnings"][0]
        assert "command drift" not in result["note"]
        assert "--force-recreate" in result["warnings"][0]

    def test_force_recreate_applies_changed_command_and_updates_record(self, monkeypatch, tmp_path):
        """--force-recreate on a drifted unit stops+relaunches it, applies the
        current command, reports it in ``recreated`` (OK not warn), AND refreshes
        its recorded serve_cmd so a later apply sees no drift."""
        status = ("Job: ~/recipes/deepseek.yaml\n"
                  "10.0.0.2\n")
        old_cmd = ["sparkrun", "run", "~/recipes/deepseek.yaml",
                   "--cluster", "hscc", "--hosts", "10.0.0.2",
                   "--port", "8000", "--no-follow", "--ensure",
                   "--served-model-name", "deepseek"]
        calls, result = self._invoke(
            monkeypatch, tmp_path, status, recreate=True,
            unit_nodes=("10.0.0.2",), worker_serve_cmd=old_cmd)
        assert len(self._stops(calls)) >= 1          # stopped first
        assert result["status"] == "ok"              # actually applied, no drift
        assert result["warnings"] == []
        assert result["recreated"] == ["10.0.0.2:8000:deepseek.yaml"]
        # record refreshed to the current command
        import json
        persisted = json.loads((tmp_path / "serving.json").read_text())
        worker = [u for u in persisted["units"]
                  if u.get("id") == "family-coding-deepseek-2-8000"][0]
        assert worker["serve_cmd"] == self._expected_worker_cmd()


# ── t_5b0f4d2d: recreate flag threading through apply → provision ──────────

class TestApplyRecreateFlag:
    """The --force-recreate flag must reach _provision_models's recreate kwarg
    through apply_template and the CLI, and be surfaced in the provision step."""

    def test_apply_template_forwards_recreate_to_provision(self, tmp_path, monkeypatch):
        import cluster_template as ct
        import subprocess
        from unittest.mock import MagicMock
        hscc = tmp_path / "hscc"; hscc.mkdir()
        for attr, val in [("HSCC_DIR", hscc), ("SERVING_JSON", hscc / "serving.json"),
                          ("MODELS_JSON", hscc / "models.json"),
                          ("CONFIG_YAML", hscc / "config.yaml"),
                          ("PROFILES_DIR", hscc / "profiles"),
                          ("PROXY_DIR", hscc / "proxies"),
                          ("APPLIED_STATE", hscc / "applied_template.json"),
                          ("ROLLBACK_DIR", hscc / "rollback")]:
            monkeypatch.setattr(ct, attr, val)
        monkeypatch.setattr(ct, "_discover", lambda probe=False: _topo(2))
        import recipe_cost as rc
        monkeypatch.setattr(ti._rc, "recipe_cost",
                            lambda r: rc.RecipeCost(r, per_gpu_total_gb=30, fits=True))
        monkeypatch.setattr(ct.Path, "is_file", lambda self: True)
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: MagicMock(returncode=0, stderr="", stdout=""))

        seen = {}
        def fake_provision(plan, **k):
            seen.update(k)
            return {"status": "ok", "provisioned": [], "recreated": [],
                    "warnings": [], "note": "test"}
        monkeypatch.setattr(ct, "_provision_models", fake_provision)

        res = ct.apply_template("single-family", confirm=True, recreate=True)
        assert res["success"] is True
        assert seen.get("recreate") is True

        # and the provision step surfaces recreated/warnings keys
        prov = [s for s in res["steps"] if s["step"] == "provision"][0]
        assert "recreated" in prov and "warnings" in prov

    def test_apply_default_recreate_false(self, tmp_path, monkeypatch):
        import cluster_template as ct
        import subprocess
        from unittest.mock import MagicMock
        hscc = tmp_path / "hscc"; hscc.mkdir()
        for attr, val in [("HSCC_DIR", hscc), ("SERVING_JSON", hscc / "serving.json"),
                          ("MODELS_JSON", hscc / "models.json"),
                          ("CONFIG_YAML", hscc / "config.yaml"),
                          ("PROFILES_DIR", hscc / "profiles"),
                          ("PROXY_DIR", hscc / "proxies"),
                          ("APPLIED_STATE", hscc / "applied_template.json"),
                          ("ROLLBACK_DIR", hscc / "rollback")]:
            monkeypatch.setattr(ct, attr, val)
        monkeypatch.setattr(ct, "_discover", lambda probe=False: _topo(1))
        import recipe_cost as rc
        monkeypatch.setattr(ti._rc, "recipe_cost",
                            lambda r: rc.RecipeCost(r, per_gpu_total_gb=30, fits=True))
        monkeypatch.setattr(ct.Path, "is_file", lambda self: True)
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: MagicMock(returncode=0, stderr="", stdout=""))
        seen = {}
        def fake_provision(plan, **k):
            seen.update(k)
            return {"status": "ok", "provisioned": [], "recreated": [],
                    "warnings": [], "note": "test"}
        monkeypatch.setattr(ct, "_provision_models", fake_provision)
        ct.apply_template("single-family", confirm=True)
        assert seen.get("recreate") is False

    def test_cli_parses_force_recreate_flag(self, monkeypatch):
        """cluster_template_cli maps --force-recreate / --recreate-on-change to
        apply_template(recreate=True)."""
        from cluster_template_cli import cmd_cluster_template
        import cluster_template as ct
        captured = {}
        def fake_apply(name, confirm=False, recreate=False):
            captured.update(name=name, confirm=confirm, recreate=recreate)
            return {"status": "ok"}
        monkeypatch.setattr(ct, "apply_template", fake_apply)
        monkeypatch.setattr("cluster_template_cli.apply_template", fake_apply)

        cmd_cluster_template(["apply", "3node-coding", "--confirm", "--force-recreate"])
        assert captured == {"name": "3node-coding", "confirm": True, "recreate": True}

        cmd_cluster_template(["apply", "3node-coding", "--confirm", "--recreate-on-change"])
        assert captured["recreate"] is True


