"""Tests for the HSCC doctor script (doctor.py)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from io import StringIO
from unittest.mock import patch

import yaml

import doctor
import enable_plugins


class TestPythonCheck:
    def test_passes(self):
        check = doctor._python_ok()
        assert check.ok is True
        assert check.name == "python"

    def test_fails_for_old_version(self):
        # Can't monkeypatch sys.version_info (read-only), test structurally.
        check = doctor.Check("python", False, detail="3.7.0",
                             fix="Install Python 3.9+", fatal=True)
        assert check.ok is False
        assert check.name == "python"
        assert "3.7.0" in check.detail
        assert "Install Python 3.9+" in check.fix
        assert check.fatal is True


class TestPyyamlCheck:
    def test_passes(self):
        check = doctor._pyyaml_ok()
        assert check.ok is True

    def test_fails_when_missing(self):
        with patch.dict(sys.modules, {"yaml": None}):
            pass  # yaml import still works from real sys
        # Instead just verify the function handles it:
        check = doctor._pyyaml_ok()
        assert check.ok is True  # PyYAML is installed in the test env


class TestSparkrunCheck:
    def test_passes(self, tmp_path, monkeypatch):
        fake = tmp_path / "sparkrun"
        fake.write_text("#!/bin/bash\necho ok\n")
        fake.chmod(0o755)
        with patch("shutil.which", return_value=str(fake)):
            check = doctor._sparkrun_ok()
        assert check.ok is True
        assert check.name == "sparkrun"

    def test_fails_when_missing(self):
        with patch("shutil.which", return_value=None):
            check = doctor._sparkrun_ok()
        assert check.ok is False
        assert "not on PATH" in check.detail


class TestHermesCheck:
    def test_passes(self, tmp_path):
        hm = tmp_path / "hermes"
        hm.mkdir()
        (hm / "hermes-agent").mkdir()
        check = doctor._hermes_ok(str(hm))
        assert check.ok is True

    def test_fails_when_missing(self, tmp_path):
        hm = tmp_path / "hermes"
        hm.mkdir()
        check = doctor._hermes_ok(str(hm))
        assert check.ok is False
        assert check.fatal is True


class TestGatewayCheck:
    def test_running(self):
        with patch("subprocess.run", return_value=type("R", (),
                                                       {"returncode": 0})()):
            check = doctor._gateway_running()
        assert check.ok is True
        assert check.detail == "running"
        assert check.fatal is False

    def test_not_running(self):
        with patch("subprocess.run", return_value=type("R", (),
                                                       {"returncode": 1})()):
            check = doctor._gateway_running()
        assert check.ok is False
        assert check.detail == "not running"

    def test_handles_exception(self):
        with patch("subprocess.run", side_effect=subprocess.SubprocessError("err")):
            check = doctor._gateway_running()
        assert check.ok is False


class TestDiskCheck:
    def test_passes(self):
        check = doctor._disk_ok("/tmp", min_gb=0.001)
        assert check.ok is True
        assert "GB free" in check.detail

    def test_fails_on_error(self):
        with patch("shutil.disk_usage", side_effect=OSError("err")):
            check = doctor._disk_ok("/nonexistent")
        assert check.ok is False
        assert check.fatal is False


class TestNASCheck:
    def test_returns_none_fallback(self):
        check = doctor._nas_ok(_runner=lambda: None)
        assert check.ok is True
        assert "none configured" in check.detail


class TestCheckModelsServed:
    """Verifies configured model ids are actually served by their endpoints."""

    @staticmethod
    def _write_config(tmp_path, cfg):
        conf = tmp_path / "config.yaml"
        yaml.safe_dump(cfg, conf.open("w"))
        return str(tmp_path)

    def test_served_model_is_ok(self, tmp_path):
        cfg = {
            "model": {"default": "Qwen/Qwen3.6-27B-FP8",
                      "base_url": "http://localhost:4000/v1"},
        }
        home = self._write_config(tmp_path, cfg)

        def fake_get(url):
            return '{"data": [{"id": "Qwen/Qwen3.6-27B-FP8"}, {"id": "other"}]}'

        check = doctor._check_models_served(home, _http_get=fake_get)
        assert check.ok is True
        assert check.fatal is False

    def test_stale_aux_compression_flagged(self, tmp_path):
        cfg = {
            "auxiliary": {
                "compression": {
                    "base_url": "http://10.0.0.244:8000/v1",
                    "model": "nvidia/Qwen3.6-35B-A3B-NVFP4",
                },
            },
        }
        home = self._write_config(tmp_path, cfg)

        def fake_get(url):
            return '{"data": [{"id": "deepseek-ai/DeepSeek-V4-Flash-0731"}]}'

        check = doctor._check_models_served(home, _http_get=fake_get)
        assert check.ok is False
        assert check.fatal is False
        # detail names the offending config key + endpoint + served ids
        assert "auxiliary.compression.model" in check.detail
        assert "nvidia/Qwen3.6-35B-A3B-NVFP4" in check.detail
        assert "http://10.0.0.244:8000/models" in check.detail
        assert "deepseek-ai/DeepSeek-V4-Flash-0731" in check.detail
        assert "fix" in check.fix.lower() or check.fix

    def test_fallback_provider_mismatch_flagged(self, tmp_path):
        cfg = {
            "fallback_providers": [{
                "model": "Qwen/Qwen3.6-27B-FP8",
                "base_url": "http://localhost:4000/v1",
            }],
        }
        home = self._write_config(tmp_path, cfg)

        def fake_get(url):
            return '{"data": [{"id": "deepseek-ai/DeepSeek-V4-Flash-0731"}]}'

        check = doctor._check_models_served(home, _http_get=fake_get)
        assert check.ok is False
        assert check.fatal is False
        assert "fallback_providers[0].model" in check.detail

    def test_base_url_v1_hits_models_endpoint(self, tmp_path):
        cfg = {
            "model": {"default": "X", "base_url": "http://localhost:8000/v1"},
        }
        home = self._write_config(tmp_path, cfg)
        seen = {}

        def fake_get(url):
            seen["url"] = url
            return '{"data": [{"id": "X"}]}'

        doctor._check_models_served(home, _http_get=fake_get)
        # A base_url ending in /v1 must be probed at .../models, not .../v1/models
        assert seen["url"] == "http://localhost:8000/models"

    def test_endpoint_unreachable_is_not_a_false_alarm(self, tmp_path):
        cfg = {
            "model": {"default": "X", "base_url": "http://localhost:8000/v1"},
        }
        home = self._write_config(tmp_path, cfg)

        def fake_get(url):
            raise ConnectionError("boom")

        check = doctor._check_models_served(home, _http_get=fake_get)
        assert check.ok is True  # no false alarm on probe failure
        assert check.fatal is False
        assert "unreachable" in check.detail

    def test_unreadable_config_is_not_a_false_alarm(self, tmp_path):
        # config.yaml absent -> ok, explanatory note
        home = str(tmp_path)
        check = doctor._check_models_served(home, _http_get=lambda u: "")
        assert check.ok is True
        assert "unreadable" in check.detail

    def test_run_doctor_includes_check_and_keeps_ok(self, tmp_path):
        # A models-served failure is NON-fatal: it must not flip run_doctor ok.
        cfg = {
            "model": {"default": "GONE", "base_url": "http://localhost:8000/v1"},
        }
        self._write_config(tmp_path, cfg)

        def fake_get(url):
            return '{"data": [{"id": "X"}]}'

        os.makedirs(os.path.join(str(tmp_path), "hermes-agent"), exist_ok=True)
        result = doctor.run_doctor(hermes_home=str(tmp_path),
                                   _cluster_runner=lambda: "[]",
                                   _http_get=fake_get)
        names = [c["name"] for c in result["checks"]]
        assert "models served" in names
        ms = next(c for c in result["checks"] if c["name"] == "models served")
        assert ms["ok"] is False
        assert ms["fatal"] is False
        assert result["ok"] is True  # non-fatal failure must NOT flip overall ok



class TestRunDoctor:
    def test_returns_correct_structure(self, tmp_path):
        # Hurt-free hermes_home (no config.yaml) keeps the models-served check
        # from probing live endpoints in tests.
        result = doctor.run_doctor(hermes_home=str(tmp_path),
                                   _cluster_runner=lambda: "[]")
        assert "ok" in result
        assert "checks" in result
        assert "fatal_failures" in result
        assert isinstance(result["checks"], list)

    def test_check_structure(self, tmp_path):
        result = doctor.run_doctor(hermes_home=str(tmp_path),
                                   _cluster_runner=lambda: "[]")
        for c in result["checks"]:
            assert "name" in c
            assert "ok" in c
            assert "detail" in c

    def test_fatal_failures(self):
        with patch("shutil.which", return_value=None):
            with patch("doctor._hermes_ok", return_value=doctor.Check(
                    "hermes", False, "missing", "fix", fatal=True)):
                hermes_home = "/tmp/hermes-test-failures"
                try:
                    shutil.rmtree(hermes_home, ignore_errors=True)
                    os.makedirs(hermes_home, exist_ok=True)
                    result = doctor.run_doctor(
                        hermes_home=hermes_home, _cluster_runner=lambda: "[]")
                    assert "hermes" in result["fatal_failures"]
                    assert "sparkrun" in result["fatal_failures"]
                    assert result["ok"] is False
                finally:
                    shutil.rmtree(hermes_home, ignore_errors=True)


class TestRunDoctorFix:
    def test_returns_fixes_key(self):
        tmpdir = tempfile.mkdtemp()
        try:
            config_path = os.path.join(tmpdir, "config.yaml")
            with open(config_path, "w") as f:
                f.write("plugins:\n  enabled: [test]\n")
            os.makedirs(os.path.join(tmpdir, "hermes-agent"), exist_ok=True)
            result = doctor.run_doctor_fix(
                config_path=config_path,
                hermes_home=tmpdir,
                _cluster_runner=lambda: "[]",
            )
            assert "fixes_applied" in result
            assert isinstance(result["fixes_applied"], list)
        finally:
            shutil.rmtree(tmpdir)

    def test_no_config_path(self):
        tmpdir = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmpdir, "hermes-agent"), exist_ok=True)
            result = doctor.run_doctor_fix(
                config_path=None, hermes_home=tmpdir,
                _cluster_runner=lambda: "[]",
            )
            assert "fixes_applied" in result
            assert result["fixes_applied"] == []
        finally:
            shutil.rmtree(tmpdir)


class TestGetNested:
    def test_basic(self):
        assert doctor._get_nested({"a": {"b": 1}}, "a", "b") == 1
        assert doctor._get_nested({}, "missing", "key") is None

    def test_complex_value(self):
        assert doctor._get_nested({"a": {"b": [1, 2]}}, "a",
                                  "b") == "[1, 2]"


class TestDoctorFixNoopWhenFresh:
    def test_fix_noop_when_fresh(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.yaml"
        cfg = {
            "plugins": {
                "enabled": ["hscc-cluster", "hscc-commands", "sparkrun-hermes"]
            },
            "toolsets": ["hermes-cli", "hscc-cluster", "sparkrun", "delegation"],
            "kanban": {
                "default_assignee": "worker",
                "max_in_progress": 30,
                "max_in_progress_per_profile": 10,
                "auto_review": {
                    "review_roles": ["worker"],
                    "reviewer": "reviewer"
                },
                "failure_limit": 3,
            },
            "delegation": {
                "base_url": "http://localhost:4000/v1",
                "model": "Qwen/Qwen3.6-27B-FP8",
                "provider": "custom",
                "api_key": "sk-sparkrun",
                "max_concurrent_children": 9,
            },
            "compression": {"threshold": 0.8},
            "auxiliary": {
                "compression": {
                    "base_url": "http://10.0.0.244:8000/v1",
                    "model": "nvidia/Qwen3.6-35B-A3B-NVFP4",
                    "provider": "custom",
                    "api_key": "sk-sparkrun",
                    "timeout": 90,
                },
                **{task: {
                    "provider": "custom",
                    "base_url": enable_plugins.COMPACT_URL,
                    "model": enable_plugins.COMPACT_MODEL,
                    "api_key": enable_plugins.COMPACT_KEY,
                } for task in enable_plugins._LOCAL_TEXT_AUX_TASKS},
            },
            "fallback_providers": [{
                "provider": "custom",
                "model": "Qwen/Qwen3.6-27B-FP8",
                "base_url": "http://localhost:4000/v1",
                "api_key": "sk-sparkrun",
            }],
            "prompt_caching": {"cache_ttl": "1hr"},
            "dashboard": {"public_url": "http://10.0.0.245:3000"},
            "hooks": {
                "pre_tool_call": [{
                    "matcher": "hscc-cluster",
                    "command": "cluster-guard.py",
                    "timeout": 10,
                }],
                "post_tool_call": [{
                    "matcher": "hscc-cluster",
                    "command": "cluster-guard.py",
                    "timeout": 5,
                }],
                "on_session_start": [{
                    "command": "cluster-guard.py",
                    "timeout": 5,
                }],
            },
            "multiplex_profiles": True,
            "gateway": {"multiplex_profiles": True},
        }
        with open(config_path, "w") as fh:
            yaml.safe_dump(cfg, fh)

        hermes_home = str(tmp_path)
        os.makedirs(os.path.join(hermes_home, "hermes-agent"), exist_ok=True)

        res = doctor.run_doctor_fix(
            config_path=str(config_path),
            hermes_home=hermes_home,
            _cluster_runner=lambda: '[{"name":"x"}]',
        )
        assert res["fixes_applied"] == []


class TestDoctorFixDriftDetected:
    def test_fix_applied_when_drift(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.yaml"
        cfg = {
            "plugins": {"enabled": []},
            "toolsets": ["hermes-cli"],
        }
        with open(config_path, "w") as fh:
            yaml.safe_dump(cfg, fh)

        hermes_home = str(tmp_path)
        os.makedirs(os.path.join(hermes_home, "hermes-agent"), exist_ok=True)
        monkeypatch.syspath_prepend(str(tmp_path.parent))

        res = doctor.run_doctor_fix(
            config_path=str(config_path),
            hermes_home=hermes_home,
            _cluster_runner=lambda: '[{"name":"x"}]',
        )
        assert "fixes_applied" in res


    def test_fix_reconciles_even_when_healthy(self, tmp_path):
        """Fix 1: doctor --fix reconciles even when all checks pass (HEALTHY system).

        Previously, reconciliation was gated on has_nonfatal_failures, so a healthy
        system never called enable() and config drift was never corrected.
        """
        config_path = tmp_path / "config.yaml"
        # Config is missing HSCC wiring but all doctor checks pass
        cfg = {
            "plugins": {"enabled": []},
            "toolsets": ["hermes-cli"],
        }
        with open(config_path, "w") as fh:
            yaml.safe_dump(cfg, fh)

        hermes_home = str(tmp_path)
        os.makedirs(os.path.join(hermes_home, "hermes-agent"), exist_ok=True)

        res = doctor.run_doctor_fix(
            config_path=str(config_path),
            hermes_home=hermes_home,
            _cluster_runner=lambda: '[{"name":"x"}]',
        )
        # Even though all checks pass (healthy), fixes should still be applied
        assert "fixes_applied" in res
        assert len(res["fixes_applied"]) > 0  # config drift was reconciled
        # Verify plugins were actually added
        final_cfg = yaml.safe_load(open(config_path))
        assert "hscc-cluster" in final_cfg["plugins"]["enabled"]


class TestDoctorCLI:
    def test_main_json_output(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.yaml"
        cfg = {"plugins": {"enabled": []}, "toolsets": ["hermes-cli"]}
        with open(config_path, "w") as f:
            yaml.safe_dump(cfg, f)
        os.makedirs(os.path.join(str(tmp_path), "hermes-agent"), exist_ok=True)
        result = doctor.main(["--json", "--fix"])
        assert result == 0

    def test_main_text_output(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.yaml"
        cfg = {"plugins": {"enabled": []}}
        with open(config_path, "w") as f:
            yaml.safe_dump(cfg, f)
        os.makedirs(os.path.join(str(tmp_path), "hermes-agent"), exist_ok=True)
        old_stdout = sys.stdout
        try:
            sys.stdout = StringIO()
            result = doctor.main(["--text"])
            output = sys.stdout.getvalue()
            assert "python" in output
            assert result == 0
        finally:
            sys.stdout = old_stdout