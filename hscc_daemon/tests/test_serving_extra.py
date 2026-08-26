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


class TestKeepaliveUnits:
    """keepalive_units() contract: ONE entry PER NODE keyed ``node`` (singular),
    i.e. ``{node, port, recipe, id}`` for every member of every keep-alive
    worker unit (serving.py:172-196).

    A multi-node / multi-tp keep-alive unit yields one entry PER MEMBER, not
    one per unit — consumers (health.check_workers, through, autodown
    ``_default_keepalive_ok``) expand and judge each node themselves (skipping
    tp-peer span members, which serve through the unit's head and expose no
    endpoint of their own).
    """

    def _serving(self, units):
        return {"version": 2, "units": units}

    def test_one_entry_per_node_singular_key(self, monkeypatch):
        """A multi-node keep-alive unit yields one entry PER NODE, keyed
        ``node`` (singular) — NOT one per unit with a ``nodes`` list."""
        from hscc_daemon import serving
        monkeypatch.delenv("HSCC_KEEPALIVE_NODES", raising=False)
        sd = self._serving([
            {"id": "ka-1", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.247", "10.0.0.248"], "port": 8000, "recipe": "r"},
        ])
        units = serving.keepalive_units(sd)
        assert len(units) == 2                      # one per node, not one per unit
        assert {u["node"] for u in units} == {"10.0.0.247", "10.0.0.248"}
        assert all(u["port"] == 8000 for u in units)
        assert all(u["id"] == "ka-1" for u in units)
        assert all(u["recipe"] == "r" for u in units)
        assert all(set(u.keys()) == {"node", "port", "recipe", "id"} for u in units)

    def test_dedupes_dup_nodes_same_unit(self, monkeypatch):
        """The same unit listed twice is not double-emitted per node."""
        from hscc_daemon import serving
        monkeypatch.delenv("HSCC_KEEPALIVE_NODES", raising=False)
        sd = self._serving([
            {"id": "ka-1", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.247"], "port": 8000, "recipe": "r"},
            {"id": "ka-1", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.247"], "port": 8000, "recipe": "r"},
        ])
        units = serving.keepalive_units(sd)
        assert len(units) == 1
        assert units[0]["node"] == "10.0.0.247"

    def test_co_located_units_distinct_ports(self, monkeypatch):
        """Two units sharing a node on different ports yield two entries."""
        from hscc_daemon import serving
        monkeypatch.delenv("HSCC_KEEPALIVE_NODES", raising=False)
        sd = self._serving([
            {"id": "ka-a", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.247"], "port": 8000, "recipe": "ra"},
            {"id": "ka-b", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.247"], "port": 8001, "recipe": "rb"},
        ])
        units = serving.keepalive_units(sd)
        assert {(u["node"], u["port"]) for u in units} == {
            ("10.0.0.247", 8000), ("10.0.0.247", 8001)}

    def test_non_keepalive_and_orchestrator_excluded(self, monkeypatch):
        from hscc_daemon import serving
        monkeypatch.delenv("HSCC_KEEPALIVE_NODES", raising=False)
        sd = self._serving([
            {"id": "wk", "role": "worker", "keepalive": False,
             "nodes": ["10.0.0.1"], "port": 8000, "recipe": "r"},
            {"id": "orch", "role": "orchestrator", "keepalive": True,
             "nodes": ["10.0.0.2"], "port": 8000, "recipe": "r"},
        ])
        assert serving.keepalive_units(sd) == []

    def test_env_nodes_emitted(self, monkeypatch):
        from hscc_daemon import serving
        monkeypatch.setenv("HSCC_KEEPALIVE_NODES", "10.0.0.9")
        units = serving.keepalive_units(self._serving([]))
        assert len(units) == 1
        assert units[0]["node"] == "10.0.0.9"
        assert units[0]["id"] == "10.0.0.9:8000"

    def test_none_serving(self, monkeypatch):
        from hscc_daemon import serving
        monkeypatch.delenv("HSCC_KEEPALIVE_NODES", raising=False)
        assert serving.keepalive_units(None) == []


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

    def test_cluster_json_non_dict_falls_back(self, tmp_hfcc_dir, monkeypatch):
        """A valid-JSON non-dict (e.g. [] or null) triggers the sparkrun fallback
        path instead of crashing with AttributeError."""
        from hscc_daemon import serving

        cluster_file = tmp_hfcc_dir / "cluster.json"

        # Test with array
        cluster_file.write_text("[]")
        monkeypatch.setattr(serving, "CLUSTER_JSON", str(cluster_file))
        monkeypatch.setattr(serving, "SERVING_JSON", str(tmp_hfcc_dir / "serving.json"))

        # Must not raise
        serving.resolve_cluster_config()

        # Test with null
        cluster_file.write_text("null")
        serving.resolve_cluster_config()

        # Test with string
        cluster_file.write_text('"just a string"')
        serving.resolve_cluster_config()

        # Test with number
        cluster_file.write_text("42")
        serving.resolve_cluster_config()

    def test_cluster_json_malformed_falls_back(self, tmp_hfcc_dir, monkeypatch):
        """Corrupt JSON triggers the sparkrun fallback path."""
        from hscc_daemon import serving

        cluster_file = tmp_hfcc_dir / "cluster.json"
        cluster_file.write_text("{bad json")
        monkeypatch.setattr(serving, "CLUSTER_JSON", str(cluster_file))
        monkeypatch.setattr(serving, "SERVING_JSON", str(tmp_hfcc_dir / "serving.json"))

        serving.resolve_cluster_config()  # must not raise


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


class TestFleetPlanPaths:
    """fleet_up_plan() and fleet_down_cmd() argv must be fully path-expanded.

    Regression for the wake-plan bug: serving.json stores ``recipe`` values
    with a literal ``~``, and the plan is passed as an argv LIST to subprocess
    (no shell), so an unexpanded tilde reaches sparkrun as a nonexistent path.
    Every recipe put into the plan must be os.path.expanduser'd. Also asserts
    fleet_down_cmd is scoped to the cluster (``--cluster <name>``), never a bare
    unscoped ``sparkrun stop --all``.
    """

    def _serving(self, monkeypatch):
        from hscc_daemon import serving
        monkeypatch.delenv("HSCC_KEEPALIVE_NODES", raising=False)
        return {
            "port": 8000,
            "units": [
                {"id": "orch", "role": "orchestrator",
                 "nodes": ["10.0.0.244", "10.0.0.246"], "port": 8000,
                 "recipe": "~/.sparkrun-local/recipes/orch.yaml"},
                {"id": "wk1", "role": "worker", "keepalive": False,
                 "nodes": ["10.0.0.247"], "port": 8000,
                 "recipe": "~/.sparkrun-local/recipes/wk.yaml"},
                {"id": "wk-keep", "role": "worker", "keepalive": True,
                 "nodes": ["10.0.0.248"], "port": 8000,
                 "recipe": "~/.sparkrun-local/recipes/wk.yaml"},
            ],
        }

    def test_fleet_up_plan_no_tilde_anywhere(self, monkeypatch):
        """Every argv element of every up command is fully expanded (~ gone)."""
        from hscc_daemon import serving
        sd = self._serving(monkeypatch)
        plan = serving.fleet_up_plan(sd)
        assert len(plan) == 3  # orchestrator + w1 + keepalive worker
        for entry in plan:
            for arg in entry["cmd"]:
                assert not arg.startswith("~"), \
                    f"unexpanded tilde in up argv: {entry['cmd']!r}"
            # the recipe position itself must be expanded
            recipe = entry["cmd"][2]
            assert not recipe.startswith("~"), recipe
            assert recipe.startswith("/"), recipe

    def test_fleet_down_cmd_scoped_to_cluster(self, monkeypatch):
        """"sparkrun stop --all" is scoped with --cluster (never bare)."""
        from hscc_daemon import serving
        cmd = serving.fleet_down_cmd()
        assert cmd[0] == "sparkrun" and cmd[1] == "stop"
        assert "--all" in cmd
        assert "--cluster" in cmd
        # scoped to the cluster read from the resolved config (module global),
        # not hardcoded in this test.
        i = cmd.index("--cluster")
        assert cmd[i + 1] == serving.HSCC_CLUSTER
        # no path argv (nothing to expand) and nothing starts with ~
        for arg in cmd:
            assert not arg.startswith("~")


class TestFleetUpServedModelName:
    """fleet_up_plan() must restore the role alias via --served-model-name.

    Regression for the autodown-wake bug (t_cbce664b): vLLM only serves a
    logical alias if it is STARTED with ``--served-model-name``. autoup() (and
    ``hscc cluster up``) build from ``fleet_up_plan()``, so the wake command
    for every unit MUST carry ``--served-model-name <concrete> <alias>`` with
    the alias derived from the unit's ROLE identity (orchestrator-model /
    worker-model) and the concrete id from the unit's model field.
    """

    def _serving(self):
        return {
            "port": 8000,
            "units": [
                {"id": "orch", "role": "orchestrator",
                 "nodes": ["10.0.0.244", "10.0.0.246"], "port": 8000,
                 "recipe": "/abs/recipes/orch.yaml",
                 "model": "deepseek-ai/DeepSeek-V4-Flash-0731", "tp": 2},
                {"id": "wk1", "role": "worker", "keepalive": False,
                 "nodes": ["10.0.0.247"], "port": 8000,
                 "recipe": "/abs/recipes/wk.yaml",
                 "model": "deepseek-ai/DeepSeek-V4-Flash-0731"},
            ],
        }

    def test_each_cmd_contains_served_model_name_with_alias(self, monkeypatch):
        """Every wake command CONTAINS --served-model-name with the role alias.

        Built from a fixture serving.json (no serve_cmd field at all) — proves
        the alias is CARRIED THROUGH from the unit, not copied from serve_cmd.
        """
        from hscc_daemon import serving
        monkeypatch.delenv("HSCC_KEEPALIVE_NODES", raising=False)
        plan = serving.fleet_up_plan(self._serving())
        assert len(plan) == 2
        by_id = {e["unit_id"]: e for e in plan}
        # Orchestrator advertises orchestrator-model.
        orch = by_id["orch"]["cmd"]
        i = orch.index("--served-model-name")
        assert orch[i + 1] == "deepseek-ai/DeepSeek-V4-Flash-0731 orchestrator-model"
        # Worker advertises worker-model (with its --tp).
        wk = by_id["wk1"]["cmd"]
        j = wk.index("--served-model-name")
        assert wk[j + 1] == "deepseek-ai/DeepSeek-V4-Flash-0731 worker-model"

    def test_role_identity_not_kind_string(self, monkeypatch):
        """The alias follows ROLE (serving.json role), not the planner's kind."""
        from hscc_daemon import serving
        monkeypatch.delenv("HSCC_KEEPALIVE_NODES", raising=False)
        # A unit whose role is literally 'worker' but is the orchestrator's
        # tp-peer must STILL advertise worker-model (role identity wins).
        sd = {
            "port": 8000,
            "units": [
                {"id": "orch", "role": "orchestrator",
                 "nodes": ["10.0.0.244"], "port": 8000,
                 "recipe": "/abs/recipes/orch.yaml",
                 "model": "deepseek-ai/DeepSeek-V4-Flash-0731"},
                {"id": "wk1", "role": "worker",
                 "nodes": ["10.0.0.247"], "port": 8000,
                 "recipe": "/abs/recipes/wk.yaml",
                 "model": "deepseek-ai/DeepSeek-V4-Flash-0731"},
            ],
        }
        plan = serving.fleet_up_plan(sd)
        by_id = {e["unit_id"]: e for e in plan}
        wk = by_id["wk1"]["cmd"]
        j = wk.index("--served-model-name")
        assert wk[j + 1] == "deepseek-ai/DeepSeek-V4-Flash-0731 worker-model"

    def test_concrete_falls_back_to_recipe_stem(self, monkeypatch):
        """A unit without a model field uses the recipe filename stem."""
        from hscc_daemon import serving
        monkeypatch.delenv("HSCC_KEEPALIVE_NODES", raising=False)
        sd = {
            "port": 8000,
            "units": [
                {"id": "wk1", "role": "worker",
                 "nodes": ["10.0.0.247"], "port": 8000,
                 "recipe": "~/recipes/my-model-a3b.yaml"},
            ],
        }
        plan = serving.fleet_up_plan(sd)
        cmd = plan[0]["cmd"]
        j = cmd.index("--served-model-name")
        assert cmd[j + 1] == "my-model-a3b worker-model"

    def test_no_model_no_recipe_yields_no_concrete(self):
        """A unit with neither model nor recipe has no concrete id ⇒ the flag
        is omitted (no --served-model-name), so a wake still starts the unit
        rather than refusing it."""
        from hscc_daemon import serving
        assert serving._concrete_model(
            {"id": "wk1", "role": "worker", "nodes": ["10.0.0.247"],
             "port": 8000}) is None
        assert serving._served_model_name(
            {"id": "wk1", "role": "worker", "nodes": ["10.0.0.247"]}) is None


class TestServeCmdMismatchRefused:
    """A serve_cmd disagreeing with the unit's recipe/nodes/port is REFUSED.

    The wake command is always built from the unit's authoritative fields, so a
    corrupt ``serve_cmd`` (e.g. rewritten to wrong hosts by an errant apply) can
    never be RUN. The refusal is proven two ways: (1) ``_serve_cmd_mismatch``
    reports the disagreement; (2) ``fleet_up_plan`` still issues the CORRECT
    derived command (right hosts/recipe/port + the alias) and loudly warns —
    never the serve_cmd's wrong targets.
    """

    def _unit(self, **over):
        u = {"id": "wk1", "role": "worker", "nodes": ["10.0.0.247"],
             "port": 8000, "recipe": "/abs/recipes/wk.yaml",
             "model": "deepseek-ai/DeepSeek-V4-Flash-0731"}
        u.update(over)
        return u

    def _good_serve_cmd(self):
        return ["sparkrun", "run", "/abs/recipes/wk.yaml", "--cluster", "hscc",
                "--hosts", "10.0.0.247", "--port", "8000", "--no-follow",
                "--ensure", "--served-model-name",
                "deepseek-ai/DeepSeek-V4-Flash-0731 worker-model"]

    def test_agreeing_serve_cmd_no_mismatch(self):
        from hscc_daemon import serving
        u = self._unit(serve_cmd=self._good_serve_cmd())
        assert serving._serve_cmd_mismatch(u, self._good_serve_cmd()) == ""

    def test_wrong_recipe_is_refused(self):
        from hscc_daemon import serving
        bad = self._good_serve_cmd()
        bad[2] = "/Users/desac/recipes/orch.yaml"   # wrong recipe
        u = self._unit(serve_cmd=bad)
        assert serving._serve_cmd_mismatch(u, bad) != ""

    def test_wrong_hosts_is_refused(self, monkeypatch):
        from hscc_daemon import serving
        # A serve_cmd targeting 10.0.0.1,10.0.0.2 while the unit hosts .247.
        sd = {
            "port": 8000,
            "units": [dict(self._unit(), serve_cmd=[
                "sparkrun", "run", "/abs/recipes/wk.yaml", "--cluster", "hscc",
                "--hosts", "10.0.0.1,10.0.0.2", "--port", "8000", "--no-follow",
                "--ensure", "--served-model-name",
                "deepseek-ai/DeepSeek-V4-Flash-0731 worker-model"])],
        }
        monkeypatch.delenv("HSCC_KEEPALIVE_NODES", raising=False)
        plan = serving.fleet_up_plan(sd)
        assert len(plan) == 1
        cmd = plan[0]["cmd"]
        # The ISSUED command uses the unit's real host (.247), NOT the serve_cmd's
        # imagined 10.0.0.1/10.0.0.2 — the bogus serve_cmd was REFUSED.
        assert "--hosts" in cmd
        assert cmd[cmd.index("--hosts") + 1] == "10.0.0.247"
        # ...and still carries the correct alias.
        j = cmd.index("--served-model-name")
        assert cmd[j + 1] == "deepseek-ai/DeepSeek-V4-Flash-0731 worker-model"

    def test_wrong_port_is_refused(self, monkeypatch, capsys):
        from hscc_daemon import serving
        sd = {
            "port": 8000,
            "units": [dict(self._unit(), serve_cmd=[
                "sparkrun", "run", "/abs/recipes/wk.yaml", "--cluster", "hscc",
                "--hosts", "10.0.0.247", "--port", "9000", "--no-follow",
                "--ensure", "--served-model-name",
                "deepseek-ai/DeepSeek-V4-Flash-0731 worker-model"])],
        }
        monkeypatch.delenv("HSCC_KEEPALIVE_NODES", raising=False)
        plan = serving.fleet_up_plan(sd)
        cmd = plan[0]["cmd"]
        # Real unit port (8000), not the serve_cmd's 9000.
        assert cmd[cmd.index("--port") + 1] == "8000"
        # And the refusal is surfaced loudly via _serving_warn.
        assert "serve_cmd" in capsys.readouterr().err


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
