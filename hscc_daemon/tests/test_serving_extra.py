"""Additional unit tests for serving.py - keepalive_nodes, resolve_cluster_config,
_resolve_serving_overlay, orchestrator_endpoint, and helpers.

The existing test_serving.py covers load_serving, orchestrator_nodes,
compute_base_url_change, serving_port, update_orchestrator_followers,
live_dispatch_hosts, and _worker_recipe_for.  This file adds the remaining
surface area.
"""
import json
import os
import pytest
from pathlib import Path


class TestKeepaliveNodes:
    """keepalive_nodes() resolves keep-alive worker nodes."""

    def test_empty_no_env(self, monkeypatch):
        from hscc_daemon import serving
        monkeypatch.delenv("HSCC_KEEPALIVE_NODES", raising=False)
        nodes = serving.keepalive_nodes({})
        assert nodes == set()

    def test_from_env(self, monkeypatch):
        from hscc_daemon import serving
        monkeypatch.setenv("HSCC_KEEPALIVE_NODES", "10.0.0.1,10.0.0.2,10.0.0.3")
        nodes = serving.keepalive_nodes({})
        assert nodes == {"10.0.0.1", "10.0.0.2", "10.0.0.3"}

    def test_env_space_separated(self, monkeypatch):
        from hscc_daemon import serving
        monkeypatch.setenv("HSCC_KEEPALIVE_NODES", "10.0.0.1 10.0.0.2")
        nodes = serving.keepalive_nodes({})
        assert nodes == {"10.0.0.1", "10.0.0.2"}

    def test_from_serving_json(self, monkeypatch):
        from hscc_daemon import serving
        monkeypatch.delenv("HSCC_KEEPALIVE_NODES", raising=False)
        serving_data = {
            "units": [
                {"role": "worker", "nodes": ["10.0.0.1"], "keepalive": True},
                {"role": "worker", "nodes": ["10.0.0.2"], "keepalive": False},
            ]
        }
        nodes = serving.keepalive_nodes(serving_data)
        assert nodes == {"10.0.0.1"}

    def test_env_and_serving_union(self, monkeypatch):
        from hscc_daemon import serving
        monkeypatch.setenv("HSCC_KEEPALIVE_NODES", "10.0.0.1")
        serving_data = {
            "units": [
                {"role": "worker", "nodes": ["10.0.0.2"], "keepalive": True},
            ]
        }
        nodes = serving.keepalive_nodes(serving_data)
        assert nodes == {"10.0.0.1", "10.0.0.2"}

    def test_orchestrator_not_keepalive(self, monkeypatch):
        from hscc_daemon import serving
        monkeypatch.delenv("HSCC_KEEPALIVE_NODES", raising=False)
        serving_data = {
            "units": [
                {"role": "orchestrator", "nodes": ["10.0.0.1"], "keepalive": True},
            ]
        }
        # orchestrator units are not worker units -> not keepalive
        nodes = serving.keepalive_nodes(serving_data)
        assert "10.0.0.1" not in nodes

    def test_none_serving(self, monkeypatch):
        from hscc_daemon import serving
        monkeypatch.delenv("HSCC_KEEPALIVE_NODES", raising=False)
        nodes = serving.keepalive_nodes(None)
        assert nodes == set()


class TestOrchestratorEndpoint:
    """orchestrator_endpoint() constructs the serving URL."""

    def test_standard_endpoint(self):
        from hscc_daemon.serving import orchestrator_endpoint, serving_port
        serving_data = {
            "port": 8000,
            "units": [
                {"role": "orchestrator", "nodes": ["10.0.0.1"], "recipe": "r", "model": "m"},
            ]
        }
        assert orchestrator_endpoint(serving_data) == "http://10.0.0.1:8000/v1"

    def test_custom_port(self):
        from hscc_daemon.serving import orchestrator_endpoint
        serving_data = {
            "port": 9000,
            "units": [
                {"role": "orchestrator", "nodes": ["10.0.0.1"], "recipe": "r", "model": "m"},
            ]
        }
        assert orchestrator_endpoint(serving_data) == "http://10.0.0.1:9000/v1"

    def test_no_orchestrator(self):
        from hscc_daemon.serving import orchestrator_endpoint
        assert orchestrator_endpoint(None) is None
        assert orchestrator_endpoint({"units": []}) is None


class TestResolveClusterConfig:
    """resolve_cluster_config() reads cluster.json or falls back to sparkrun."""

    def test_cluster_json_applied(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import serving

        cluster_file = tmp_hfcc_dir / "cluster.json"
        cluster_file.write_text(json.dumps({
            "gateway": {"ip": "10.0.0.1"},
            "workers": [{"ip": "10.0.0.2"}],
            "nasDevices": [{"ip": "10.0.0.10"}],
        }))

        serving_file = tmp_hfcc_dir / "serving.json"
        # No serving.json -> use defaults
        monkeypatch.setattr(serving, "CLUSTER_JSON", str(cluster_file))
        monkeypatch.setattr(serving, "SERVING_JSON", str(serving_file))

        serving.resolve_cluster_config()
        assert serving.PRIMARY_NODE == "10.0.0.1"
        assert serving.NAS_HOST == "10.0.0.10"

    def test_cluster_json_missing(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import serving

        # No cluster.json -> fallback
        monkeypatch.setattr(serving, "CLUSTER_JSON", str(tmp_hfcc_dir / "cluster.json"))
        monkeypatch.setattr(serving, "SERVING_JSON", str(tmp_hfcc_dir / "serving.json"))

        serving.resolve_cluster_config()
        # Should complete without crashing


class TestRebuildVllmCmds:
    """_rebuild_vllm_cmds() sets vLLM control commands."""

    def test_builds_commands(self, monkeypatch):
        from hscc_daemon import serving
        # Capture the current state
        orig_primary = serving.PRIMARY_NODE
        orig_recipe = serving.VLLM_RECIPE
        orig_port = serving.VLLM_PORT

        serving.PRIMARY_NODE = "10.0.0.5"
        serving.VLLM_RECIPE = "~/recipe.yaml"
        serving.VLLM_PORT = 8000

        serving._rebuild_vllm_cmds()

        assert serving.VLLM_HEALTH_URL == "http://10.0.0.5:8000/health"
        assert "sparkrun" in serving.VLLM_STOP_CMD
        assert "10.0.0.5" in serving.VLLM_STOP_CMD
        assert "sparkrun" in serving.VLLM_START_CMD

        # Restore
        serving.PRIMARY_NODE = orig_primary
        serving.VLLM_RECIPE = orig_recipe
        serving.VLLM_PORT = orig_port


class TestEndpointHealthy:
    """_endpoint_healthy() checks orchestrator endpoint."""

    def test_healthy(self, fake_subprocess):
        from hscc_daemon.serving import _endpoint_healthy
        fake_subprocess.set_result(stdout="200", returncode=0)
        assert _endpoint_healthy("http://10.0.0.1:8000/v1") is True

    def test_unhealthy(self, fake_subprocess):
        from hscc_daemon.serving import _endpoint_healthy
        fake_subprocess.set_result(stdout="000", returncode=0)
        assert _endpoint_healthy("http://10.0.0.1:8000/v1") is False

    def test_curl_failure(self, fake_subprocess):
        from hscc_daemon.serving import _endpoint_healthy
        fake_subprocess.set_result(stdout="", stderr="curl error", returncode=7)
        assert _endpoint_healthy("http://10.0.0.1:8000/v1") is False

    def test_timeout(self, fake_subprocess):
        from hscc_daemon.serving import _endpoint_healthy
        fake_subprocess.set_result(timeout_exc=True)
        assert _endpoint_healthy("http://10.0.0.1:8000/v1") is False


class TestServingWarn:
    """_serving_warn() prints warnings safely at import time."""

    def test_prints_to_stderr(self, monkeypatch):
        from hscc_daemon import serving
        import io

        buf = io.StringIO()
        monkeypatch.setattr(serving.sys, "stderr", buf)
        # Ensure log is not available (import-time scenario)
        serving._serving_warn("test warning")
        output = buf.getvalue()
        assert "test warning" in output


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
