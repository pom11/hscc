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
        # Full span + tp carried on every per-node entry (so a consumer can
        # rebuild the whole-span --hosts <span> --tp N command, not a solo).
        assert all(u["nodes"] == ["10.0.0.247", "10.0.0.248"] for u in units)
        assert all(u["tp"] is None for u in units)
        # model + role are carried too (for rebuilding the --served-model-name
        # the unit was declared with, so a relaunch reuses the SAME cluster_id
        # instead of deriving a duplicate — t_e1ff0e8e). This unit has no
        # `model`, so it is carried as None (no --served-model-name on relaunch).
        assert all(u["model"] is None for u in units)
        assert all(u["role"] == "worker" for u in units)
        # uniform_pool is carried too: health.check_workers relaunches from
        # these entries via _unit_run_cmd, which needs the fleet-level flag to
        # derive BOTH aliases for a uniform pool (else a gentle relaunch
        # collapses the pool one node at a time). False when absent/not a pool.
        assert all(u["uniform_pool"] is False for u in units)
        # max_num_seqs is carried too (None when the unit declares no cap) —
        # health's gentle relaunch and auto-heal both hand these entries to
        # serving._unit_run_cmd, which needs the cap to re-issue the unit's
        # declared concurrency limit instead of the recipe default.
        assert all(u["max_num_seqs"] is None for u in units)
        assert all(set(u.keys()) == {"node", "port", "recipe", "id", "nodes",
                                     "tp", "model", "role", "uniform_pool",
                                     "max_num_seqs"}
                   for u in units)

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

    def test_unit_port_wins_over_top_level(self):
        """A per-unit port overrides the top-level serving port — a unit can
        serve on a non-default port even when the global port differs."""
        from hscc_daemon.serving import orchestrator_endpoint
        serving_data = {
            "port": 9000,
            "units": [
                {"role": "orchestrator", "nodes": ["10.0.0.1"], "recipe": "r",
                 "model": "m", "port": 8123},
            ]
        }
        assert orchestrator_endpoint(serving_data) == "http://10.0.0.1:8123/v1"

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


class TestUniformPoolWakePath:
    """A ``uniform_pool`` fleet wakes with BOTH aliases on EVERY unit.

    The apply path (``cluster_template._render_serve_cmd`` /
    ``_unit_serve_names``) advertises ``worker-model orchestrator-model`` on
    every unit of a uniform pool, because each node runs the same recipe and
    must be able to take either role. The wake path
    (``serving._unit_run_cmd``, used by ``hscc cluster up``,
    ``autodown.autoup()`` and ``health.check_workers``) derives the command
    independently, and before this it derived the ROLE alias only — so a fleet
    up collapsed the pool back to orchestrator-on-one-node and never recovered.

    sparkrun folds ``--served-model-name`` and ``--tp`` into the intent hash it
    turns into a ``cluster_id``, so divergence between the two builders does
    not error: ``--ensure`` recreates the unit with the wrong aliases.
    """

    def _serving(self, uniform_pool):
        s = {
            "version": 3,
            "units": [
                {"id": "orch", "role": "orchestrator", "nodes": ["10.0.0.244"],
                 "port": 8000, "recipe": "/abs/recipes/flash.yaml",
                 "model": "lab/Flash-NVFP4", "tp": 1},
                {"id": "wk1", "role": "worker", "nodes": ["10.0.0.246"],
                 "port": 8001, "recipe": "/abs/recipes/flash.yaml",
                 "model": "lab/Flash-NVFP4", "tp": 1, "keepalive": True},
            ],
        }
        if uniform_pool:
            s["uniform_pool"] = True
        return s

    def _smn(self, cmd):
        from hscc_daemon import serving
        return serving._flag_after(cmd, "--served-model-name")

    def test_pool_units_advertise_both_aliases(self):
        from hscc_daemon import serving
        plan = serving.fleet_up_plan(self._serving(True))
        assert len(plan) == 2
        for e in plan:
            assert self._smn(e["cmd"]) == \
                "lab/Flash-NVFP4 worker-model orchestrator-model"

    def test_non_pool_keeps_single_role_alias(self):
        """Without the flag, role-derived aliases are unchanged (no regression
        for every non-uniform template)."""
        from hscc_daemon import serving
        plan = serving.fleet_up_plan(self._serving(False))
        by_kind = {e["kind"]: e for e in plan}
        assert self._smn(by_kind["orchestrator"]["cmd"]) == \
            "lab/Flash-NVFP4 orchestrator-model"
        assert self._smn(by_kind["worker"]["cmd"]) == \
            "lab/Flash-NVFP4 worker-model"

    def test_keepalive_units_carry_the_flag(self):
        """``health.check_workers`` relaunches from ``keepalive_units`` entries,
        so the flag must ride on those too or a gentle relaunch collapses the
        pool one node at a time."""
        from hscc_daemon import serving
        units = serving.keepalive_units(self._serving(True))
        assert units and all(u.get("uniform_pool") for u in units)
        for u in units:
            u = dict(u, nodes=u["nodes"], recipe="/abs/recipes/flash.yaml")
            assert self._smn(serving._unit_run_cmd(u)) == \
                "lab/Flash-NVFP4 worker-model orchestrator-model"

    def test_tp_always_emitted_even_for_tp1(self):
        """``cluster_template._render_serve_cmd`` ALWAYS emits --tp; omitting it
        for tp=1 gave every single-node unit a different intent hash on the two
        paths."""
        from hscc_daemon import serving
        for e in serving.fleet_up_plan(self._serving(True)):
            assert serving._flag_after(e["cmd"], "--tp") == "1"

    def test_is_uniform_pool_reads_top_level(self):
        from hscc_daemon import serving
        assert serving.is_uniform_pool(self._serving(True)) is True
        assert serving.is_uniform_pool(self._serving(False)) is False
        assert serving.is_uniform_pool(None) is False
        assert serving.is_uniform_pool({}) is False

    def test_alias_drift_between_paths_is_surfaced(self):
        """The canary: a recorded serve_cmd whose alias/tp disagrees with the
        derived command is reported, so this class of drift can never again be
        silent."""
        from hscc_daemon import serving
        u = {"id": "wk1", "role": "worker", "nodes": ["10.0.0.246"],
             "port": 8001, "recipe": "/abs/recipes/flash.yaml",
             "model": "lab/Flash-NVFP4", "tp": 1, "uniform_pool": True}
        derived = serving._unit_run_cmd(u)
        stale = list(derived)
        stale[stale.index("--served-model-name") + 1] = \
            "lab/Flash-NVFP4 worker-model"
        assert "--served-model-name" in serving._serve_cmd_mismatch(
            dict(u, serve_cmd=stale), derived)
        assert serving._serve_cmd_mismatch(
            dict(u, serve_cmd=derived), derived) == ""


class TestFleetUpMaxNumSeqs:
    """A unit applied with a ``max_num_seqs`` cap must WAKE with that cap.

    The template-side leg (t_fd56b3ee / PR #118) renders ``-o max_num_seqs=N``
    on the apply path. This is the wake leg: ``_unit_run_cmd`` is the SINGLE
    start-command builder for ``hscc cluster up``, autodown's wake, health's
    gentle relaunch and auto-heal, and it builds purely from serving.json
    entries — the template file is never re-read. So the cap has to (a) be
    recorded in serving.json (pinned from the other side in
    hscc-cluster/tests/test_max_num_seqs.py::TestServingJsonCarriesCap) and
    (b) be emitted here. Without (b) a capped unit comes back at its RECIPE
    default after any autodown/reboot/heal cycle: flash-next ships 8 while the
    operator's rule for this fleet is <=5 running requests per node, so the
    rule silently breaks exactly at the moment nobody is watching.

    INTENT-HASH PARITY (why this is a value-fidelity bug, not a duplicate
    hazard): sparkrun deliberately does NOT hash serve args into the intent id
    — ``generate_intent_id`` hashes runtime/model/container/port/served-model-
    name/parallelism only. So an added ``-o`` pair cannot change the
    ``cluster_id`` and ``--ensure`` cannot launch a parallel duplicate the way
    the historical ``--served-model-name`` / ``--tp`` divergences did
    (t_e1ff0e8e). The wake command and the apply command therefore need NOT be
    token-identical here; what must not diverge is the VALUE.
    """

    ORCH = {"id": "orch", "role": "orchestrator", "nodes": ["10.0.0.244"],
            "port": 8000, "recipe": "/abs/recipes/orch.yaml",
            "model": "lab/Orch-Flash"}
    WK = {"id": "wk1", "role": "worker", "keepalive": True,
          "nodes": ["10.0.0.247"], "port": 8000,
          "recipe": "/abs/recipes/wk.yaml", "model": "lab/Flash-NVFP4"}

    def _serving(self, orch_cap=None, wk_cap=None):
        orch = dict(self.ORCH)
        wk = dict(self.WK)
        if orch_cap is not None:
            orch["max_num_seqs"] = orch_cap
        if wk_cap is not None:
            wk["max_num_seqs"] = wk_cap
        return {"version": 2, "units": [orch, wk]}

    def _opt(self, cmd, key="max_num_seqs"):
        """The value of `-o key=…` in cmd, or None when the pair is absent."""
        from hscc_daemon import serving
        return serving._option_overrides(cmd).get(key)

    # ── the builder itself ───────────────────────────────────────────────────

    def test_cap_emitted_as_option_pair(self):
        from hscc_daemon import serving
        cmd = serving._unit_run_cmd(dict(self.WK, max_num_seqs=5))
        assert self._opt(cmd) == "5"
        # exactly the two tokens `-o`, `max_num_seqs=5` — sparkrun has no
        # dedicated concurrency flag (verified against `sparkrun run --help`:
        # "-o, --option TEXT  Override any recipe default … (repeatable)").
        i = cmd.index("-o")
        assert cmd[i + 1] == "max_num_seqs=5"

    def test_absent_cap_emits_no_option(self):
        """No cap declared ⇒ no `-o` at all: the recipe default must win, which
        is the pre-change behaviour for every existing serving.json."""
        from hscc_daemon import serving
        cmd = serving._unit_run_cmd(dict(self.WK))
        assert "-o" not in cmd and "--option" not in cmd

    def test_cap_rides_after_tp(self):
        """Position mirrors cluster_template._render_serve_cmd (…--tp N, then
        the override pairs), so a recorded serve_cmd and a derived wake command
        for the same unit read alike."""
        from hscc_daemon import serving
        cmd = serving._unit_run_cmd(dict(self.WK, tp=2, max_num_seqs=5))
        assert cmd.index("-o") > cmd.index("--tp")

    def test_bad_values_degrade_to_no_cap(self):
        """serving.json is operator-editable: a bool / non-numeric / <=0 value
        must not reach sparkrun as a malformed override. These degrade to
        "no cap" (the recipe default — the old behaviour) rather than launching
        garbage or refusing the wake outright."""
        from hscc_daemon import serving
        for bad in (True, False, 0, -1, "five", None, [5], {}):
            cmd = serving._unit_run_cmd(dict(self.WK, max_num_seqs=bad))
            assert "-o" not in cmd, f"{bad!r} emitted an -o pair"
            assert serving._max_num_seqs_from(dict(self.WK,
                                                   max_num_seqs=bad)) is None

    def test_int_like_values_still_cap(self):
        """A hand-edited `"max_num_seqs": "5"` or `5.0` is harmless and the
        operator plainly meant a cap of 5 — honor it rather than dropping the
        cap (dropping it fails toward the recipe default, which is the
        over-the-limit direction this card exists to close). The STRICT
        validation that rejects such values lives at template parse time
        (template_intent._max_num_seqs_from), where it can be reported."""
        from hscc_daemon import serving
        for good, want in (("5", 5), (5.0, 5), (5, 5)):
            assert serving._max_num_seqs_from(
                dict(self.WK, max_num_seqs=good)) == want
            assert self._opt(serving._unit_run_cmd(
                dict(self.WK, max_num_seqs=good))) == str(want)

    # ── the two entry builders (removed call sites hide from unit tests) ─────

    def test_fleet_up_plan_carries_the_cap_on_both_roles(self):
        """`hscc cluster up` + autodown's wake build from these entries."""
        from hscc_daemon import serving
        plan = serving.fleet_up_plan(self._serving(orch_cap=7, wk_cap=5))
        by_id = {e["unit_id"]: e for e in plan}
        assert self._opt(by_id["orch"]["cmd"]) == "7"
        assert self._opt(by_id["wk1"]["cmd"]) == "5"

    def test_fleet_up_plan_omits_it_when_not_stamped(self, monkeypatch):
        from hscc_daemon import serving
        monkeypatch.delenv("HSCC_KEEPALIVE_NODES", raising=False)
        plan = serving.fleet_up_plan(self._serving())
        assert plan, "fixture produced no plan"
        for e in plan:
            assert "-o" not in e["cmd"]

    def test_keepalive_units_carry_the_cap(self):
        """health.check_workers' gentle relaunch and auto-heal feed
        ``keepalive_units`` entries straight into ``_unit_run_cmd``; if the cap
        is dropped in THAT builder, only a per-node relaunch loses it."""
        from hscc_daemon import serving
        units = serving.keepalive_units(self._serving(wk_cap=5))
        assert len(units) == 1
        assert units[0]["max_num_seqs"] == 5
        assert self._opt(serving._unit_run_cmd(units[0])) == "5"

    def test_keepalive_units_none_when_unit_has_no_cap(self, monkeypatch):
        from hscc_daemon import serving
        monkeypatch.delenv("HSCC_KEEPALIVE_NODES", raising=False)
        units = serving.keepalive_units(self._serving())
        assert len(units) == 1
        assert units[0]["max_num_seqs"] is None
        assert "-o" not in serving._unit_run_cmd(units[0])

    def test_env_derived_keepalive_entries_have_no_cap(self, monkeypatch):
        """The env-fallback branch has no serving.json unit behind it, so it
        must not invent a cap key with a bogus value."""
        from hscc_daemon import serving
        monkeypatch.setenv("HSCC_KEEPALIVE_NODES", "10.0.0.9")
        units = serving.keepalive_units({"version": 2, "units": []})
        assert len(units) == 1
        assert "-o" not in serving._unit_run_cmd(units[0])

    # ── the mismatch canary ──────────────────────────────────────────────────

    def test_recorded_option_the_derived_cmd_lacks_is_loud(self):
        """THE canary this card asked for: a recorded serve_cmd that carries a
        cap the derived command lacks means the unit's own fields lost the
        knob, and the wake would restart the unit at the recipe default. That
        must be reported, not swallowed — same contract as the --served-model-
        name / --tp comparisons above. The WORDING is pinned too: a generic
        'x != y' that does not say WHICH side carries the cap leaves the
        operator guessing whether the fleet is over-cap or the record is stale,
        so the message must name the direction (mutation-probe P6: dropping the
        directional branch otherwise survives every behavioural assertion)."""
        from hscc_daemon import serving
        u = dict(self.WK)                       # NO max_num_seqs on the unit
        derived = serving._unit_run_cmd(u)
        assert self._opt(derived) is None
        recorded = derived + ["-o", "max_num_seqs=5"]
        mism = serving._serve_cmd_mismatch(dict(u, serve_cmd=recorded), derived)
        assert mism and "max_num_seqs" in mism
        assert "dropped a knob" in mism and "recipe default" in mism

    def test_agreeing_option_is_not_a_mismatch(self):
        """An unchanged fleet stays SILENT: same cap on both sides ⇒ '' (this
        is the half of the contract that keeps a healthy wake from warning)."""
        from hscc_daemon import serving
        u = dict(self.WK, max_num_seqs=5)
        derived = serving._unit_run_cmd(u)
        assert serving._serve_cmd_mismatch(dict(u, serve_cmd=derived),
                                           derived) == ""

    def test_differing_value_is_a_mismatch(self):
        from hscc_daemon import serving
        u = dict(self.WK, max_num_seqs=5)
        derived = serving._unit_run_cmd(u)
        stale = list(derived)
        stale[stale.index("max_num_seqs=5")] = "max_num_seqs=8"
        mism = serving._serve_cmd_mismatch(dict(u, serve_cmd=stale), derived)
        assert "max_num_seqs" in mism and "'8'" in mism and "'5'" in mism

    def test_record_without_the_option_while_derived_has_it_is_loud(self):
        """The opposite direction: the record predates the cap (or was written
        by the pre-fix apply) while the unit now declares one. The running
        fleet is NOT at the declared cap — the same 'drift NOT applied' fact
        the apply path reports — so it must not read as agreement."""
        from hscc_daemon import serving
        u = dict(self.WK, max_num_seqs=5)
        derived = serving._unit_run_cmd(u)
        recorded = [t for t in derived if t not in ("-o", "max_num_seqs=5")]
        mism = serving._serve_cmd_mismatch(dict(u, serve_cmd=recorded), derived)
        assert "max_num_seqs" in mism
        assert "the fleet is NOT running it" in mism

    def test_long_form_option_spelling_is_not_drift(self):
        """`--option key=value` is the same sparkrun override spelled long-form;
        a record using it must not read as drift (mirrors the normalization
        cluster_template._serve_cmd_flags already does for the apply path)."""
        from hscc_daemon import serving
        u = dict(self.WK, max_num_seqs=5)
        derived = serving._unit_run_cmd(u)
        long_form = [("--option" if t == "-o" else t) for t in derived]
        assert serving._serve_cmd_mismatch(dict(u, serve_cmd=long_form),
                                           derived) == ""

    def test_unrelated_option_pair_does_not_trip_the_unit_fields(self):
        """The canary stays scoped: an `-o` in the record with a key the derived
        command never claims (a recipe tweak recorded from elsewhere) does not
        make a healthy unit's recipe/hosts/port comparison fail — it is reported
        for that key only, and the wake still runs the derived command."""
        from hscc_daemon import serving
        u = dict(self.WK, max_num_seqs=5)
        derived = serving._unit_run_cmd(u)
        extra = derived + ["-o", "seed=1"]
        mism = serving._serve_cmd_mismatch(dict(u, serve_cmd=extra), derived)
        assert mism and "seed" in mism and "max_num_seqs" not in mism

    def test_option_drift_does_not_suppress_the_intent_hash_flags(self):
        """Order-independence: the intent-hash flags are checked BEFORE the
        option pairs, so an option disagreement can never mask an alias/tp
        divergence (that class causes duplicate containers; this one does not)."""
        from hscc_daemon import serving
        u = dict(self.WK, max_num_seqs=5)
        derived = serving._unit_run_cmd(u)
        stale = [t.replace("lab/Flash-NVFP4 worker-model",
                           "lab/Flash-NVFP4 orchestrator-model")
                 for t in derived]
        mism = serving._serve_cmd_mismatch(dict(u, serve_cmd=stale), derived)
        assert "--served-model-name" in mism

    def test_plan_still_runs_the_derived_command_on_option_drift(
            self, monkeypatch, capsys):
        """The refuse-but-proceed rule holds for the new flag class too: a
        corrupt record is warned about, and the ISSUED command is the derived
        one (correct recipe/hosts/port), never the record's argv."""
        from hscc_daemon import serving
        sd = {"version": 2, "units": [dict(
            self.WK, serve_cmd=["sparkrun", "run", "/abs/recipes/wk.yaml",
                                "--cluster", "hscc", "--hosts", "10.0.0.247",
                                "--port", "8000", "--no-follow", "--ensure",
                                "--served-model-name",
                                "lab/Flash-NVFP4 worker-model", "--tp", "1",
                                "-o", "max_num_seqs=8"])]}
        monkeypatch.delenv("HSCC_KEEPALIVE_NODES", raising=False)
        plan = serving.fleet_up_plan(sd)
        assert len(plan) == 1
        cmd = plan[0]["cmd"]
        # No cap on the unit ⇒ nothing issued…
        assert self._opt(cmd) is None
        # …and the record's cap did NOT leak into the command that gets run.
        assert "max_num_seqs=8" not in cmd
        err = capsys.readouterr().err
        assert "serve_cmd" in err and "max_num_seqs" in err
