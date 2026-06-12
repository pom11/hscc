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
    list_templates,
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


@pytest.fixture
def stub_cluster(monkeypatch):
    """Stub discovery + recipe_cost so resolve() works without a live cluster or
    real recipe files."""
    monkeypatch.setattr(cluster_template, "_discover", lambda: _topo(3))
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


class TestInstallProxyPlist:
    def test_writes_and_loads(self, tmp_path, monkeypatch):
        import cluster_template as ct
        import subprocess
        from unittest.mock import MagicMock
        monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")
        calls = []
        monkeypatch.setattr(subprocess, "run",
                            lambda argv, **k: calls.append(argv) or MagicMock(returncode=0, stderr=""))
        fam = ti.ResolvedFamily(name="coding", proxy_port=4000, units=[
            ti.ResolvedUnit("worker", "coding", "m.yaml", "M", "10.0.0.2", 8000, 1, 1)])
        res = install_proxy_plist(fam)
        assert (tmp_path / "proxies" / "coding" / "proxy.plist").is_file()
        assert any("bootstrap" in str(c) for c in calls)
        assert res["loaded"] is True and res["port"] == 4000


class TestPruneOrphanProxies:
    def test_removes_orphan_family_dirs_and_backups(self, tmp_path, monkeypatch):
        import cluster_template as ct
        import subprocess
        from unittest.mock import MagicMock
        monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: MagicMock(returncode=0))
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
                          ("PROXY_DIR", hscc / "proxies"),
                          ("APPLIED_STATE", hscc / "applied_template.json"),
                          ("ROLLBACK_DIR", hscc / "rollback")]:
            monkeypatch.setattr(ct, attr, val)
        monkeypatch.setattr(ct, "_discover", lambda: _topo(n_workers))
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
        assert r["ok"] is True and r["errors"] == []

    def test_validate_unknown(self):
        r = cluster_template.validate_template("does-not-exist")
        assert r["ok"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
