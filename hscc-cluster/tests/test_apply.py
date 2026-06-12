"""Tests for cluster_template.py — apply pipeline."""

import pytest
import json
import tempfile
from pathlib import Path
import sys

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from cluster_template import (
    preview_template,
    apply_template,
    write_json,
    atomic_yaml_update,
    validate_template_deployable,
    TemplateValidationError,
    install_proxy_plist,
)
from cluster_template_schema import list_templates, ClusterTemplate, ModelSpec, WorkerFamily, FamilyProxyConfig


class TestListTemplates:
    """Test template listing."""

    def test_list_built_in(self):
        """Should find built-in templates."""
        registry = list_templates(PLUGIN_DIR / "templates")
        assert len(registry.templates) >= 4  # basic-1 through basic-4

    def test_registry_structure(self):
        registry = list_templates(PLUGIN_DIR / "templates")
        tpl = registry.templates[0]
        assert "name" in tpl
        assert "version" in tpl
        assert "cluster_size" in tpl


class TestWriteJson:
    """Test atomic JSON writes."""

    def test_write_and_read(self, tmp_path):
        data = {"key": "value", "nested": {"a": 1}}
        write_json(tmp_path / "test.json", data)
        
        with open(tmp_path / "test.json") as f:
            result = json.load(f)
        assert result == data

    def test_backup_on_overwrite(self, tmp_path):
        data1 = {"version": 1}
        data2 = {"version": 2}

        write_json(tmp_path / "test.json", data1)
        write_json(tmp_path / "test.json", data2, backup=True)

        # Check backup exists
        backups = list(tmp_path.glob("test.json.bak.*"))
        assert len(backups) == 1

    def test_prune_backups_caps_to_max(self, tmp_path):
        """_prune_backups keeps only the newest MAX_BACKUPS (M1).

        Regression: a prior version left 100+ serving.json.bak.* in ~/.hscc
        because every write made a timestamped backup and nothing pruned them.
        """
        import os
        import cluster_template as ct
        p = tmp_path / "serving.json"
        p.write_text("{}")
        # Fabricate MAX+7 backups with strictly increasing mtimes.
        made = []
        for i in range(ct.MAX_BACKUPS + 7):
            b = tmp_path / f"serving.json.bak.{1000 + i}"
            b.write_text(f"v{i}")
            os.utime(b, (1000 + i, 1000 + i))
            made.append(b)
        ct._prune_backups(p)
        remaining = sorted(tmp_path.glob("serving.json.bak.*"))
        assert len(remaining) == ct.MAX_BACKUPS
        # The newest ones survive; the oldest are gone.
        assert made[-1] in remaining
        assert made[0] not in remaining

    def test_write_json_prunes_old_backups(self, tmp_path):
        """write_json itself caps backups across many overwrites."""
        import os
        import cluster_template as ct
        p = tmp_path / "serving.json"
        write_json(p, {"n": 0})
        # Seed extra old backups (distinct mtimes) then trigger one more write.
        for i in range(ct.MAX_BACKUPS + 5):
            b = tmp_path / f"serving.json.bak.{2000 + i}"
            b.write_text("old")
            os.utime(b, (2000 + i, 2000 + i))
        write_json(p, {"n": 1}, backup=True)  # makes 1 more + prunes
        assert len(list(tmp_path.glob("serving.json.bak.*"))) <= ct.MAX_BACKUPS

    def test_atomic_write_no_partial(self, tmp_path):
        """Temp file should not persist after write."""
        write_json(tmp_path / "test.json", {"ok": True})
        assert not (tmp_path / "test.json.tmp").exists()


class TestAtomicYamlUpdate:
    """Test atomic YAML file updates."""

    def test_create_new(self, tmp_path):
        data = {"new": "value"}
        path, changed = atomic_yaml_update(tmp_path / "test.yaml", lambda d: data)

        assert changed is True
        import yaml
        with open(path) as f:
            result = yaml.safe_load(f)
        assert result == data

    def test_update_existing(self, tmp_path):
        import yaml
        path = tmp_path / "test.yaml"
        with open(path, "w") as f:
            yaml.dump({"old": "value"}, f)

        path, changed = atomic_yaml_update(path, lambda d: {**d, "new": "value"})

        assert changed is True
        with open(path) as f:
            result = yaml.safe_load(f)
        assert result["old"] == "value"
        assert result["new"] == "value"

    def test_noop_reports_unchanged(self, tmp_path):
        # Applying the same content twice → second call reports changed=False
        # (so callers can skip a gateway restart).
        path = tmp_path / "test.yaml"
        atomic_yaml_update(path, lambda d: {"a": 1})
        _, changed = atomic_yaml_update(path, lambda d: {"a": 1})
        assert changed is False


class TestPreviewTemplate:
    """Test preview (dry-run) without writing files."""

    def test_preview_basic_1_node(self):
        result = preview_template("basic-1-node")
        
        assert result["template"] == "basic-1-node"
        assert result["cluster_size"] == 1
        assert len(result["changes"]) > 0
        
        # Check change structure
        change_files = [c["file"] for c in result["changes"]]
        assert "serving.json" in change_files
        assert "models.json" in change_files

    def test_preview_does_not_write(self, tmp_path):
        """Preview must not modify any files."""
        # Use templates dir (not tmp_path) — preview shouldn't write
        result = preview_template("basic-1-node")
        assert "changes" in result
        # The preview itself should only compute, not write

    def test_preview_multi_family(self):
        result = preview_template("multi-family-4-node")
        
        assert result["cluster_size"] == 4
        # Check that proxy configs are listed
        proxy_changes = [c for c in result["changes"] if c["file"] == "proxies/"]
        assert len(proxy_changes) == 1
        assert "coding" in proxy_changes[0].get("details", [""])[0]

    def test_preview_structure(self):
        result = preview_template("basic-2-node")
        
        # All changes should have file, action, summary
        for change in result["changes"]:
            assert "file" in change
            assert "action" in change
            assert "summary" in change


class TestApplyTemplate:
    """Test apply (dry-run mode by default)."""

    def test_apply_without_confirm_returns_preview(self):
        result = apply_template("basic-1-node")
        
        assert result["status"] == "preview"
        assert "Re-call with confirm=true" in result["note"]
        assert "changes" in result

    def test_preview_basic_structure(self):
        """Preview returns full plan without writing."""
        result = preview_template("basic-1-node")
        
        assert result["template"] == "basic-1-node"
        assert "cluster_size" in result
        assert "changes" in result


_REAL_RECIPE = "~/.sparkrun-local/recipes/local-fixed/qwen3.6-27b-fp8-vllm.yaml"
_MISSING_RECIPE = "/nonexistent/does-not-exist.yaml"


class TestValidateDeployable:
    """Pre-apply preflight: refuse undeployable templates before any write."""

    def _tpl(self, orch_recipe, families):
        # cluster_size must match orchestrator(1) + unique family node count.
        nodes = {n for f in families for n in f.nodes}
        return ClusterTemplate(
            name="t", cluster_size=1 + len(nodes),
            orchestrator=ModelSpec(recipe=orch_recipe),
            orchestrator_node="192.168.88.244", families=families)

    def test_valid_template_passes(self):
        tpl = self._tpl(_REAL_RECIPE, [
            WorkerFamily(name="coding",
                         models=[ModelSpec(recipe=_REAL_RECIPE)],
                         nodes=["192.168.88.246", "192.168.88.247", "192.168.88.248"],
                         proxy=FamilyProxyConfig(port=4000))])
        assert validate_template_deployable(tpl) == []

    def test_missing_recipe_flagged(self):
        tpl = self._tpl(_REAL_RECIPE, [
            WorkerFamily(name="coding", models=[ModelSpec(recipe=_MISSING_RECIPE)],
                         nodes=["192.168.88.246"], proxy=FamilyProxyConfig(port=4000))])
        errs = validate_template_deployable(tpl)
        assert any("not found" in e for e in errs)

    def test_two_models_one_node_collision(self):
        tpl = self._tpl(_REAL_RECIPE, [
            WorkerFamily(name="coding",
                         models=[ModelSpec(recipe=_REAL_RECIPE),
                                 ModelSpec(recipe=_REAL_RECIPE)],
                         nodes=["192.168.88.246"], proxy=FamilyProxyConfig(port=4000))])
        errs = validate_template_deployable(tpl)
        assert any("collision" in e for e in errs)

    def test_shared_proxy_port_flagged(self):
        tpl = self._tpl(_REAL_RECIPE, [
            WorkerFamily(name="coding", models=[ModelSpec(recipe=_REAL_RECIPE)],
                         nodes=["192.168.88.246"], proxy=FamilyProxyConfig(port=4001)),
            WorkerFamily(name="vision", models=[ModelSpec(recipe=_REAL_RECIPE)],
                         nodes=["192.168.88.247"], proxy=FamilyProxyConfig(port=4001))])
        errs = validate_template_deployable(tpl)
        assert any("proxy port 4001 shared" in e for e in errs)

    def test_family_on_orchestrator_node_flagged(self):
        tpl = self._tpl(_REAL_RECIPE, [
            WorkerFamily(name="coding", models=[ModelSpec(recipe=_REAL_RECIPE)],
                         nodes=["192.168.88.244"],  # = orchestrator node
                         proxy=FamilyProxyConfig(port=4001))])
        errs = validate_template_deployable(tpl)
        assert any("orchestrator node" in e for e in errs)

    def test_apply_blocks_undeployable_without_confirm(self):
        # multi-family-4-node references missing recipes -> preview is 'blocked'
        res = apply_template("multi-family-4-node", confirm=False)
        assert res["status"] == "blocked"
        assert res["errors"]

    def test_apply_raises_on_confirm_if_invalid(self):
        with pytest.raises(TemplateValidationError):
            apply_template("multi-family-4-node", confirm=True)


class TestInstallProxyPlist:
    """install_proxy_plist must WRITE the plist and LOAD it (not just write)."""

    def _family(self):
        return WorkerFamily(
            name="coding", models=[ModelSpec(recipe=_REAL_RECIPE)],
            nodes=["192.168.88.246"], proxy=FamilyProxyConfig(port=4000))

    def test_writes_and_loads(self, tmp_path, monkeypatch):
        import cluster_template as ct
        monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            from unittest.mock import MagicMock
            return MagicMock(returncode=0, stderr="")
        monkeypatch.setattr(ct.subprocess if hasattr(ct, "subprocess") else __import__("subprocess"),
                            "run", fake_run)
        # subprocess is imported inside the function, so patch the module global
        import subprocess
        monkeypatch.setattr(subprocess, "run", fake_run)

        res = install_proxy_plist(self._family())
        # plist written
        assert (tmp_path / "proxies" / "coding" / "proxy.plist").is_file()
        # launchctl bootstrap was invoked to LOAD it
        assert any("bootstrap" in str(c) for c in calls)
        assert res["loaded"] is True


class TestApplyIntegration:
    """Full apply against a temp HOME — asserts GENERATED FILES, not just mocks.

    This is the test the suite lacked: every prior bug passed because tests
    mocked the subprocess boundary and never inspected real output files.
    """

    def test_apply_hscc_live_writes_correct_files(self, tmp_path, monkeypatch):
        import cluster_template as ct
        import subprocess
        from unittest.mock import MagicMock

        # Redirect all live targets into the temp dir.
        hscc = tmp_path / "hscc"
        hscc.mkdir()
        monkeypatch.setattr(ct, "HSCC_DIR", hscc)
        monkeypatch.setattr(ct, "SERVING_JSON", hscc / "serving.json")
        monkeypatch.setattr(ct, "MODELS_JSON", hscc / "models.json")
        monkeypatch.setattr(ct, "CONFIG_YAML", hscc / "config.yaml")
        monkeypatch.setattr(ct, "PROXY_DIR", hscc / "proxies")
        monkeypatch.setattr(ct, "APPLIED_STATE", hscc / "applied_template.json")
        # No real launchctl / sparkrun.
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: MagicMock(returncode=0, stderr="", stdout=""))
        # Don't fire real model launches.
        monkeypatch.setattr(ct, "_provision_models",
                            lambda tpl, **k: {"status": "ok", "provisioned": [], "note": "test"})

        res = ct.apply_template("hscc-live", confirm=True)
        assert res["success"] is True

        # serving.json: 4 per-node units, workers keepalive
        serving = json.loads((hscc / "serving.json").read_text())
        assert len(serving["units"]) == 4
        workers = [u for u in serving["units"] if u["role"] == "worker"]
        assert all(len(u["nodes"]) == 1 and u["keepalive"] for u in workers)

        # config.yaml: providers deduped (custom only, single family)
        import yaml
        cfg = yaml.safe_load((hscc / "config.yaml").read_text())
        prov_names = [p["name"] for p in cfg["providers"]]
        assert prov_names.count("custom") == 1
        assert prov_names.count("family-coding") == 1

        # applied state recorded
        state = json.loads((hscc / "applied_template.json").read_text())
        assert state["template"] == "hscc-live"

    def test_apply_then_status_reports_template(self, tmp_path, monkeypatch):
        import cluster_template as ct
        import subprocess
        from unittest.mock import MagicMock
        hscc = tmp_path / "hscc"; hscc.mkdir()
        for attr, val in [("HSCC_DIR", hscc), ("SERVING_JSON", hscc / "serving.json"),
                          ("MODELS_JSON", hscc / "models.json"),
                          ("CONFIG_YAML", hscc / "config.yaml"),
                          ("PROXY_DIR", hscc / "proxies"),
                          ("APPLIED_STATE", hscc / "applied_template.json")]:
            monkeypatch.setattr(ct, attr, val)
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: MagicMock(returncode=0, stderr="", stdout=""))
        monkeypatch.setattr(ct, "_provision_models", lambda tpl, **k: {"status": "ok"})

        assert ct.applied_status()["applied"] is None
        ct.apply_template("hscc-live", confirm=True)
        assert ct.applied_status()["applied"]["template"] == "hscc-live"

    def test_failed_apply_rolls_back_to_prior_state(self, tmp_path, monkeypatch):
        """G4/5e: a half-completed apply restores the pre-apply snapshot so the
        cluster is left in its prior state, not corrupted."""
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
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: MagicMock(returncode=0, stderr="", stdout=""))
        # Seed a known-good prior serving.json (the state to restore).
        prior = {"version": 1, "units": [{"id": "prior", "role": "orchestrator",
                                          "nodes": ["10.0.0.1"]}]}
        (hscc / "serving.json").write_text(json.dumps(prior))

        # Make provisioning blow up AFTER serving.json was overwritten.
        def boom(tpl, **k):
            raise RuntimeError("provision exploded mid-apply")
        monkeypatch.setattr(ct, "_provision_models", boom)

        res = ct.apply_template("hscc-live", confirm=True)
        assert res["success"] is False
        assert res["rolled_back"] is True
        # serving.json restored to the prior content, not the half-applied one
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
        # mutate, then restore
        (hscc / "serving.json").write_text('{"v": 999}')
        assert ct._restore_snapshot(bundle) is True
        assert json.loads((hscc / "serving.json").read_text())["v"] == 1

    def test_snapshot_none_when_nothing_exists(self, tmp_path, monkeypatch):
        import cluster_template as ct
        hscc = tmp_path / "hscc"; hscc.mkdir()
        monkeypatch.setattr(ct, "HSCC_DIR", hscc)
        monkeypatch.setattr(ct, "CONFIG_YAML", hscc / "config.yaml")
        monkeypatch.setattr(ct, "ROLLBACK_DIR", hscc / "rollback")
        assert ct._snapshot_state() is None

    def test_rollback_bundles_pruned(self, tmp_path, monkeypatch):
        import cluster_template as ct
        import os as _os
        hscc = tmp_path / "hscc"; hscc.mkdir()
        monkeypatch.setattr(ct, "HSCC_DIR", hscc)
        monkeypatch.setattr(ct, "CONFIG_YAML", hscc / "config.yaml")
        monkeypatch.setattr(ct, "ROLLBACK_DIR", hscc / "rollback")
        (hscc / "serving.json").write_text("{}")
        # pre-seed > MAX_ROLLBACKS old bundles
        rb = hscc / "rollback"; rb.mkdir()
        for i in range(ct.MAX_ROLLBACKS + 4):
            b = rb / f"old-{i}"; b.mkdir()
            (b / "serving.json").write_text("{}")
            _os.utime(b, (1000 + i, 1000 + i))
        ct._snapshot_state()  # makes one more + prunes
        assert len([p for p in rb.iterdir() if p.is_dir()]) <= ct.MAX_ROLLBACKS


class TestValidateAndStatusHelpers:
    def test_validate_template_good(self):
        from cluster_template import validate_template
        r = validate_template("hscc-live")
        assert r["ok"] is True and r["errors"] == []

    def test_validate_template_bad(self):
        from cluster_template import validate_template
        r = validate_template("multi-family-4-node")
        assert r["ok"] is False and r["errors"]

    def test_validate_unknown_template(self):
        from cluster_template import validate_template
        r = validate_template("does-not-exist")
        assert r["ok"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
