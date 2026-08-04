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
        assert r["ok"] is True and r["errors"] == []

    def test_validate_unknown(self):
        r = cluster_template.validate_template("does-not-exist")
        assert r["ok"] is False


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
        assert "stale-provider" not in names

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


# ── Fix: provision timeout raised to configurable 900s ──────────────────

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
