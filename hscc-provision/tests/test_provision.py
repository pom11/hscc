"""Comprehensive tests for hscc-provision plugin (hscc.py).

All tests are fully isolated — no real SSH, subprocess, or network calls.
Every subprocess / SSH / file-system interaction is patched via conftest fixtures.

The autouse fixture ``patch_hfcc_dir`` in conftest.py:
  - re-imports the ``hscc`` module fresh for every test
  - patches HSCC_DIR / PROVISION_JSON / AGENTS_JSON to tmp_path
  - overrides os.path.exists to avoid reading real files

Tests use the ``fake_ssh`` fixture to control subprocess.run output.
"""

import json
import os
import subprocess
import sys

import pytest

# Import hscc at module level.  The autouse fixture ``patch_hfcc_dir`` will
# re-import and patch it *before* each test body runs.
import hscc  # noqa: E402  isort:skip

# ── State persistence: load_provision_state / save_provision_state ──────────

class TestStatePersistence:
    """load_provision_state() and save_provision_state() round-trip."""

    def test_save_then_load_roundtrip(self, provision_state_file):
        path, data = provision_state_file
        data["mappings"]["agent-1"] = {
            "recipe": "@official/qwen3.6-35b-a3b-fp8-vllm",
            "container_id": "abc123",
            "host": "192.0.2.10",
        }
        hscc.save_provision_state(data)
        loaded = hscc.load_provision_state()
        assert loaded["mappings"]["agent-1"]["recipe"] == "@official/qwen3.6-35b-a3b-fp8-vllm"
        assert loaded["mappings"]["agent-1"]["container_id"] == "abc123"

    def test_save_load_history(self, provision_state_file):
        _, data = provision_state_file
        data["history"] = [
            {"action": "test1", "ts": "2025-01-01T00:00:00Z"},
            {"action": "test2", "ts": "2025-01-02T00:00:00Z"},
        ]
        hscc.save_provision_state(data)
        loaded = hscc.load_provision_state()
        assert len(loaded["history"]) == 2
        assert loaded["history"][0]["action"] == "test1"

    def test_load_missing_state_file(self, tmp_hfcc_dir):
        prov = tmp_hfcc_dir / "provision.json"
        assert not prov.exists()
        state = hscc.load_provision_state()
        assert state == {"mappings": {}, "history": []}

    def test_load_malformed_json(self, tmp_hfcc_dir):
        prov = tmp_hfcc_dir / "provision.json"
        prov.write_text("not valid json {{{")
        state = hscc.load_provision_state()
        assert state == {"mappings": {}, "history": []}

    def test_load_empty_file(self, tmp_hfcc_dir):
        prov = tmp_hfcc_dir / "provision.json"
        prov.write_text("")
        state = hscc.load_provision_state()
        assert state == {"mappings": {}, "history": []}

    def test_load_invalid_type_json(self, tmp_hfcc_dir):
        prov = tmp_hfcc_dir / "provision.json"
        prov.write_text("[1, 2, 3]")
        state = hscc.load_provision_state()
        assert isinstance(state, list)


# ── run_cmd() — subprocess wrapper ────────────────────────────────────────

class TestRunCmd:
    """Subprocess wrapper with timeout handling."""

    def test_successful_command(self, fake_ssh):
        fake_ssh.set_result(stdout="hello world", returncode=0)
        result = hscc.run_cmd(["echo", "hello"])
        assert result["success"] is True
        assert result["returncode"] == 0
        assert result["output"] == "hello world"

    def test_failed_command(self, fake_ssh):
        fake_ssh.set_result(stdout="", stderr="something failed", returncode=1)
        result = hscc.run_cmd(["false"])
        assert result["success"] is False
        assert result["returncode"] == 1
        assert "something failed" in result.get("error", "")

    def test_timeout_handling(self, fake_ssh):
        fake_ssh.set_result(timeout_exc=True)
        result = hscc.run_cmd(["sleep", "999"], timeout=2)
        assert result["success"] is False
        assert "timed out" in result["error"].lower()
        assert result["command"] == ["sleep", "999"]

    def test_file_not_found(self, fake_ssh):
        fake_ssh.set_result(file_not_found=True)
        result = hscc.run_cmd(["nonexistent_binary"])
        assert result["success"] is False
        assert "command not found" in result["error"].lower()

    def test_as_json_parsing(self, fake_ssh):
        fake_ssh.set_result(stdout='{"key": "val"}', returncode=0)
        result = hscc.run_cmd(["cat", "data.json"], as_json=True)
        assert result["json"]["key"] == "val"

    def test_as_json_bad_json(self, fake_ssh):
        fake_ssh.set_result(stdout="not json at all", returncode=0)
        result = hscc.run_cmd(["cat", "data.json"], as_json=True)
        assert "json" not in result

    def test_empty_output(self, fake_ssh):
        fake_ssh.set_result(stdout="", returncode=0)
        result = hscc.run_cmd(["true"])
        assert result["success"] is True
        assert result["output"] == ""

    def test_custom_timeout(self, fake_ssh):
        fake_ssh.set_result(stdout="ok", returncode=0)
        result = hscc.run_cmd(["cmd"], timeout=99)
        assert result["success"] is True


# ── get_running_containers() — sparkrun status parsing ────────────────────

class TestGetRunningContainers:
    """Parsing of sparkrun status output."""

    _SPARKRUN_STATUS = (
        "Name                       Runtime  TP  Nodes  GpuMem       Model                        Registry\n"
        "@official/qwen3.6-35b-a3b-fp8-vllm  vllm  1  1  80gb  Qwen/Qwen3.6-35B-A3B-FP8  official\n"
        "Job: @official/qwen3.6-35b-a3b-fp8-vllm  (tp=1, pp=1)  [1b6e77192e59]  (1 container(s))\n"
        "  solo       192.0.2.10  Up 37 minutes  sparkrun-eugr-vllm\n"
        "Job: @official/llama3.1-70b-a3b-fp8-vllm  (tp=2, pp=1)  [aabbccdd1122]  (1 container(s))\n"
        "  solo       192.0.2.20  Up 5 hours  sparkrun-eugr-vllm\n"
        "Idle: 3 / 6 hosts\n"
        "Total: 6\n"
    )

    def test_parses_containers(self, fake_ssh):
        fake_ssh.set_result(stdout=self._SPARKRUN_STATUS, returncode=0)
        containers = hscc.get_running_containers()
        assert len(containers) == 2

        first = containers[0]
        assert first["name"] == "@official/qwen3.6-35b-a3b-fp8-vllm"
        assert first["container_id"] == "1b6e77192e59"
        assert first["tp"] == "1"
        assert first["pp"] == "1"
        assert first["host"] == "192.0.2.10"
        assert first["uptime"] == "Up 37 minutes"

    def test_parses_second_container(self, fake_ssh):
        fake_ssh.set_result(stdout=self._SPARKRUN_STATUS, returncode=0)
        containers = hscc.get_running_containers()
        second = containers[1]
        assert second["name"] == "@official/llama3.1-70b-a3b-fp8-vllm"
        assert second["container_id"] == "aabbccdd1122"
        assert second["tp"] == "2"
        assert second["pp"] == "1"
        assert second["host"] == "192.0.2.20"
        assert second["uptime"] == "Up 5 hours"

    def test_empty_output(self, fake_ssh):
        fake_ssh.set_result(stdout="", returncode=0)
        containers = hscc.get_running_containers()
        assert containers == []

    def test_no_idle_section(self, fake_ssh):
        output = (
            "Job: @official/test-model  (tp=1, pp=1)  [deadbeef]  (1 container(s))\n"
            "  solo       10.0.0.1  Up 10 minutes  sparkrun-vllm\n"
        )
        fake_ssh.set_result(stdout=output, returncode=0)
        containers = hscc.get_running_containers()
        assert len(containers) == 1
        assert containers[0]["host"] == "10.0.0.1"

    def test_run_cmd_failure(self, fake_ssh):
        fake_ssh.set_result(stderr="sparkrun not found", returncode=1)
        containers = hscc.get_running_containers()
        assert containers == []


# ── get_idle_hosts() — idle host detection ────────────────────────────────

class TestGetIdleHosts:
    """Idle host detection from sparkrun status output."""

    _SPARKRUN_STATUS = (
        "Idle hosts\n"
        "  192.0.2.30\n"
        "  192.0.2.31\n"
        "  192.0.2.32\n"
        "Running: 4 / 6 hosts\n"
    )

    def test_parses_idle_hosts(self, fake_ssh):
        fake_ssh.set_result(stdout=self._SPARKRUN_STATUS, returncode=0)
        idle = hscc.get_idle_hosts()
        assert len(idle) == 3
        assert "192.0.2.30" in idle
        assert "192.0.2.31" in idle
        assert "192.0.2.32" in idle

    def test_no_idle_section(self, fake_ssh):
        output = "No idle hosts — all busy\n"
        fake_ssh.set_result(stdout=output, returncode=0)
        idle = hscc.get_idle_hosts()
        assert idle == []

    def test_empty_output(self, fake_ssh):
        fake_ssh.set_result(stdout="", returncode=0)
        idle = hscc.get_idle_hosts()
        assert idle == []

    def test_single_idle_host(self, fake_ssh):
        output = "Idle hosts\n  10.0.0.99\n"
        fake_ssh.set_result(stdout=output, returncode=0)
        idle = hscc.get_idle_hosts()
        assert idle == ["10.0.0.99"]


# ── check_health() — vLLM health check ────────────────────────────────────

class TestCheckHealth:
    """vLLM health check on a specific host."""

    def test_healthy_on_port_8000(self, fake_ssh):
        fake_ssh.set_result(stdout='{"status":"ok"}', returncode=0)
        result = hscc.check_health("192.0.2.10")
        assert result["host"] == "192.0.2.10"
        assert result["healthy"] is True
        assert result["port"] == 8000

    def test_falls_through_unhealthy_ports(self, fake_ssh):
        fake_ssh.set_result(stdout="", returncode=1)       # port 8000
        fake_ssh.set_result(stdout='{"status":"ok"}', returncode=0)  # port 8001
        result = hscc.check_health("192.0.2.50")
        assert result["host"] == "192.0.2.50"
        assert result["healthy"] is True
        assert result["port"] == 8001

    def test_all_ports_unhealthy(self, fake_ssh):
        for _ in range(5):
            fake_ssh.set_result(stdout="", stderr="curl error", returncode=1)
        result = hscc.check_health("192.0.2.99")
        assert result["host"] == "192.0.2.99"
        assert result["healthy"] is False
        assert "No vLLM" in result["message"]

    def test_timeout_on_first_port(self, fake_ssh):
        fake_ssh.set_result(timeout_exc=True)  # port 8000
        fake_ssh.set_result(stdout='{"status":"ok"}', returncode=0)  # port 8001
        result = hscc.check_health("192.0.2.99")
        assert result["healthy"] is True


# ── cmd_recipes() — recipe listing ────────────────────────────────────────

class TestCmdRecipes:
    """Recipe listing from sparkrun list output."""

    _SPARKRUN_LIST = (
        "@official/qwen3.6-35b-a3b-fp8-vllm  vllm  1  1  80gb  Qwen/Qwen3.6-35B-A3B-FP8  official\n"
        "@official/llama3.1-70b-a3b-fp8-vllm  vllm  2  1  80gb  meta/llama3.1-70b  official\n"
        "@sparkrun-transitional/mistral-7b  sglang  1  1  40gb  mistralai/Mistral-7B  sparkrun-transitional\n"
    )

    def test_parses_recipes(self, fake_ssh, capsys):
        fake_ssh.set_result(stdout=self._SPARKRUN_LIST, returncode=0)  # sparkrun list
        fake_ssh.set_result(stdout="snapshot-hash-abc123\n", returncode=0)  # NAS check (ls)
        hscc.cmd_recipes()
        output = json.loads(capsys.readouterr().out)
        assert output["total"] == 3
        recipes = output["recipes"]
        assert recipes[0]["name"] == "@official/qwen3.6-35b-a3b-fp8-vllm"
        assert recipes[0]["runtime"] == "vllm"
        assert recipes[0]["tp"] == "1"
        assert recipes[0]["model"] == "Qwen/Qwen3.6-35B-A3B-FP8"

    def test_empty_recipes(self, fake_ssh, capsys):
        fake_ssh.set_result(stdout="", returncode=0)
        hscc.cmd_recipes()
        output = json.loads(capsys.readouterr().out)
        assert output["total"] == 0
        assert output["recipes"] == []

    def test_run_cmd_failure(self, fake_ssh, capsys):
        fake_ssh.set_result(stderr="error", returncode=1)
        hscc.cmd_recipes()
        output = json.loads(capsys.readouterr().out)
        assert output["total"] == 0


# ── cmd_stop() — container stop ───────────────────────────────────────────

class TestCmdStop:
    """Container stop command."""

    _STATUS_WITH_HERMES_HOST = (
        "Job: @official/test  (tp=1, pp=1)  [hermes-cont-id]  (1 container(s))\n"
        "  solo       10.0.0.1  Up 10 minutes  sparkrun\n"
    )

    def test_stop_success(self, fake_ssh, capsys):
        fake_ssh.set_result(stdout="stopped", returncode=0)
        hscc.cmd_stop("abc123")
        output = json.loads(capsys.readouterr().out)
        assert output["success"] is True
        assert output["output"] == "stopped"

    def test_stop_fails(self, fake_ssh, capsys):
        fake_ssh.set_result(stdout="", stderr="not found", returncode=1)
        hscc.cmd_stop("nonexistent")
        output = json.loads(capsys.readouterr().out)
        assert output["success"] is False

    def test_stop_hermes_container_refused(self, fake_ssh, capsys):
        fake_ssh.set_result(stdout=self._STATUS_WITH_HERMES_HOST, returncode=0)  # get_running_containers
        hscc.cmd_stop("hermes-cont-id")  # force=False (default)
        output = json.loads(capsys.readouterr().out)
        assert "REFUSED" in output["error"]

    def test_stop_hermes_container_forced(self, fake_ssh, capsys):
        fake_ssh.set_result(stdout=self._STATUS_WITH_HERMES_HOST, returncode=0)  # get_running_containers
        fake_ssh.set_result(stdout="force-stopped", returncode=0)  # stop
        hscc.cmd_stop("hermes-cont-id", force=True)
        output = json.loads(capsys.readouterr().out)
        assert output["success"] is True
        assert "REFUSED" not in output.get("error", "")


# ── cmd_assign() / cmd_unassign() — agent assignment ──────────────────────

class TestCmdAssign:
    """Agent assignment to a recipe."""

    def test_assign_existing_recipe(self, provision_state_file, sample_agents_json, fake_ssh, capsys):
        status_output = (
            "Job: @official/qwen3.6-35b-a3b-fp8-vllm  (tp=1, pp=1)  [run-abc]  (1 container(s))\n"
            "  solo       192.0.2.10  Up 10 minutes  sparkrun\n"
        )
        fake_ssh.set_result(stdout=status_output, returncode=0)  # get_running_containers (first call)
        fake_ssh.set_result(stdout=status_output, returncode=0)  # get_running_containers (second call, after cmd_run)
        fake_ssh.set_result(stdout="logged", returncode=0)  # log_event

        hscc.cmd_assign("agent-001", "@official/qwen3.6-35b-a3b-fp8-vllm")
        output = json.loads(capsys.readouterr().out)
        assert output["success"] is True
        assert output["agent_id"] == "agent-001"
        assert output["recipe"] == "@official/qwen3.6-35b-a3b-fp8-vllm"
        assert output["container"] == "run-abc"
        assert output["host"] == "192.0.2.10"

    def test_assign_agent_not_found(self, provision_state_file, sample_agents_json, fake_ssh, capsys):
        fake_ssh.set_result(stdout="", returncode=0)
        hscc.cmd_assign("nonexistent-agent", "@official/qwen3.6-35b-a3b-fp8-vllm")
        output = json.loads(capsys.readouterr().out)
        assert "not found" in output["error"].lower()

    def test_assign_no_idle_hosts(self, provision_state_file, sample_agents_json, fake_ssh, capsys):
        fake_ssh.set_result(stdout="Idle: 0 / 6\nTotal: 6\n", returncode=0)  # get_running_containers
        fake_ssh.set_result(stdout="No idle hosts\n", returncode=0)  # get_idle_hosts
        hscc.cmd_assign("agent-001", "@official/qwen3.6-35b-a3b-fp8-vllm")
        output = json.loads(capsys.readouterr().out)
        assert "idle" in output["error"].lower()

    def test_assign_creates_mapping(self, provision_state_file, sample_agents_json, fake_ssh, capsys):
        status_output = (
            "Job: @official/qwen3.6-35b-a3b-fp8-vllm  (tp=1, pp=1)  [run-xyz]  (1 container(s))\n"
            "  solo       192.0.2.10  Up 10 minutes  sparkrun\n"
        )
        fake_ssh.set_result(stdout=status_output, returncode=0)
        fake_ssh.set_result(stdout=status_output, returncode=0)
        fake_ssh.set_result(stdout="logged", returncode=0)

        hscc.cmd_assign("agent-001", "@official/qwen3.6-35b-a3b-fp8-vllm")
        state = hscc.load_provision_state()
        assert "agent-001" in state["mappings"]
        assert state["mappings"]["agent-001"]["recipe"] == "@official/qwen3.6-35b-a3b-fp8-vllm"


class TestCmdUnassign:
    """Agent unassignment."""

    def test_unassign_existing(self, provision_state_file, sample_agents_json, fake_ssh, capsys):
        hscc.save_provision_state({
            "mappings": {"agent-001": {"recipe": "test", "container_id": "c1", "host": "10.0.0.1", "wired_at": "2025-01-01T00:00:00Z"}},
            "history": [],
        })
        fake_ssh.set_result(stdout="ok", returncode=0)  # log_event

        hscc.cmd_unassign("agent-001")
        output = json.loads(capsys.readouterr().out)
        assert output["success"] is True
        assert output["agent_id"] == "agent-001"
        assert output["model"] == "auto"

        # Verify agents.json reset
        agents_path = hscc.AGENTS_JSON
        with open(agents_path) as f:
            data = json.load(f)
        assert data["agents"][0]["model"] == "auto"
        assert data["agents"][0]["endpoint"] == ""

    def test_unassign_missing_agent(self, provision_state_file, sample_agents_json, fake_ssh, capsys):
        fake_ssh.set_result(stdout="ok", returncode=0)  # log_event still runs
        hscc.cmd_unassign("nonexistent")
        output = json.loads(capsys.readouterr().out)
        assert "not found" in output["error"].lower()


# ── cmd_run() — container run ─────────────────────────────────────────────

class TestCmdRun:
    """Container run command."""

    def test_run_no_idle_hosts(self, fake_ssh, capsys):
        fake_ssh.set_result(stdout="No idle hosts\n", returncode=0)
        hscc.cmd_run("@official/qwen3.6-35b-a3b-fp8-vllm")
        output = json.loads(capsys.readouterr().out)
        assert "idle" in output["error"].lower()

    def test_run_recipe_not_found(self, fake_ssh, capsys):
        fake_ssh.set_result(stdout="192.0.2.10\n", returncode=0)  # get_idle_hosts
        fake_ssh.set_result(stdout="", returncode=1)  # recipe_to_model_path fails
        hscc.cmd_run("@official/qwen3.6-35b-a3b-fp8-vllm")
        output = json.loads(capsys.readouterr().out)
        assert "could not resolve model" in output["error"].lower()

    def test_run_nas_check_fails(self, fake_ssh, capsys):
        fake_ssh.set_result(stdout="192.0.2.10\n", returncode=0)  # get_idle_hosts
        fake_ssh.set_result(stdout="Model: Qwen/Qwen3.6-35B-A3B-FP8\n", returncode=0)  # recipe_to_model_path
        fake_ssh.set_result(stdout="", returncode=1)  # check_model_on_nas (ssh_cmd, ls)
        hscc.cmd_run("@official/qwen3.6-35b-a3b-fp8-vllm")
        output = json.loads(capsys.readouterr().out)
        assert "NOT FOUND" in output.get("error", "")

    def test_run_success(self, fake_ssh, capsys):
        fake_ssh.set_result(stdout="192.0.2.10\n", returncode=0)  # get_idle_hosts
        fake_ssh.set_result(stdout="Model: Qwen/Qwen3.6-35B-A3B-FP8\n", returncode=0)  # recipe_to_model_path
        fake_ssh.set_result(stdout="snapshot-hash\n", returncode=0)  # check_model_on_nas (ls)
        fake_ssh.set_result(stdout="container started abc123", returncode=0)  # subprocess.run in cmd_run
        hscc.cmd_run("@official/qwen3.6-35b-a3b-fp8-vllm")
        output = json.loads(capsys.readouterr().out)
        assert output["success"] is True
        assert output["output"] == "container started abc123"


# ── cmd_cleanup() — cleanup orphaned containers ───────────────────────────

class TestCmdCleanup:
    """Cleanup orphaned containers."""

    def test_cleanup_stops_orphans(self, provision_state_file, fake_ssh, capsys):
        state = {"mappings": {
            "agent-1": {"recipe": "@official/qwen3.6-35b-a3b-fp8-vllm", "container_id": "c1", "host": "192.0.2.10", "wired_at": "2025-01-01T00:00:00Z"},
        }, "history": []}
        hscc.save_provision_state(state)

        running_status = (
            "Job: @official/orphan-model  (tp=1, pp=1)  [orphan-cid]  (1 container(s))\n"
            "  solo       192.0.2.20  Up 5 minutes  sparkrun\n"
        )
        fake_ssh.set_result(stdout=running_status, returncode=0)  # get_running_containers (1st call)
        fake_ssh.set_result(stdout=running_status, returncode=0)  # get_running_containers (2nd call)
        fake_ssh.set_result(stdout="stopped", returncode=0)  # stop orphan
        fake_ssh.set_result(stdout="ok", returncode=0)  # log_event

        hscc.cmd_cleanup()
        output = json.loads(capsys.readouterr().out)
        assert output["stopped"] == 1
        assert output["skipped_self"] == 0

    def test_cleanup_skips_hermes_host(self, provision_state_file, fake_ssh, capsys):
        state = {"mappings": {}, "history": []}
        hscc.save_provision_state(state)

        running_status = (
            "Job: @official/hermes-container  (tp=1, pp=1)  [hermes-cid]  (1 container(s))\n"
            "  solo       10.0.0.1  Up 5 minutes  sparkrun\n"
        )
        fake_ssh.set_result(stdout=running_status, returncode=0)  # get_running_containers (1st)
        fake_ssh.set_result(stdout=running_status, returncode=0)  # get_running_containers (2nd)
        fake_ssh.set_result(stdout="ok", returncode=0)  # log_event

        hscc.cmd_cleanup()
        output = json.loads(capsys.readouterr().out)
        assert output["stopped"] == 0
        assert output["skipped_self"] == 1


# ── log_event() — event logging ───────────────────────────────────────────

class TestLogEvent:
    """Provisioning event logging."""

    def test_log_event_adds_to_history(self, provision_state_file):
        state = {"mappings": {}, "history": []}
        hscc.save_provision_state(state)
        hscc.log_event("test_action", {"key": "value"})
        loaded = hscc.load_provision_state()
        assert len(loaded["history"]) == 1
        assert loaded["history"][0]["action"] == "test_action"
        assert loaded["history"][0]["key"] == "value"
        assert "timestamp" in loaded["history"][0]

    def test_log_event_maintains_max_100(self, provision_state_file, fake_ssh):
        state = {"mappings": {}, "history": []}
        hscc.save_provision_state(state)
        for i in range(150):
            hscc.log_event(f"action-{i}", {"idx": i})
        loaded = hscc.load_provision_state()
        assert len(loaded["history"]) == 100


# ── cmd_health (top-level) — health command dispatcher ────────────────────

class TestCmdHealth:
    """Top-level health command."""

    def test_health_no_containers(self, fake_ssh, capsys):
        fake_ssh.set_result(stdout="No containers\n", returncode=0)
        hscc.cmd_health(None)
        output = json.loads(capsys.readouterr().out)
        assert "No running containers" in output["message"]

    def test_health_with_host(self, fake_ssh, capsys):
        fake_ssh.set_result(stdout='{"status":"ok"}', returncode=0)
        hscc.cmd_health("192.0.2.10")
        output = json.loads(capsys.readouterr().out)
        assert output["healthy"] is True
        assert output["host"] == "192.0.2.10"


# ── cmd_status — combined fleet status ────────────────────────────────────

class TestCmdStatus:
    """Combined fleet status display."""

    def test_status_with_data(self, provision_state_file, sample_agents_json, fake_ssh, capsys):
        fake_ssh.set_result(stdout="@official/qwen3.6-35b-a3b-fp8-vllm  vllm  1  1  80gb  Qwen/Qwen3.6-35B-A3B-FP8  official\n", returncode=0)
        fake_ssh.set_result(stdout="Job: @official/qwen3.6-35b-a3b-fp8-vllm  (tp=1, pp=1)  [c1]  (1 container(s))\n  solo       192.0.2.10  Up 10 minutes  sparkrun\n", returncode=0)
        fake_ssh.set_result(stdout="192.0.2.30\n", returncode=0)  # get_idle_hosts
        fake_ssh.set_result(stdout="idle: 2\n", returncode=0)  # log_event for status

        _, _ = sample_agents_json

        state = {"mappings": {
            "agent-1": {"recipe": "@official/qwen3.6-35b-a3b-fp8-vllm", "container_id": "c1", "host": "192.0.2.10", "wired_at": "2025-01-01T00:00:00Z"},
        }, "history": []}
        hscc.save_provision_state(state)

        hscc.cmd_status()
        captured = capsys.readouterr()
        assert "HSCC Provisioning Status" in captured.out
        assert "agent-1" in captured.out


# ── recipe_to_model_path() — model path resolution ────────────────────────

class TestRecipeToModelPath:
    """Model name resolution from recipe name."""

    def test_successful_resolution(self, fake_ssh):
        fake_ssh.set_result(stdout="Model: Qwen/Qwen3.6-35B-A3B-FP8\n", returncode=0)
        model = hscc.recipe_to_model_path("@official/qwen3.6-35b-a3b-fp8-vllm")
        assert model == "Qwen/Qwen3.6-35B-A3B-FP8"

    def test_failure_returns_none(self, fake_ssh):
        fake_ssh.set_result(stdout="", returncode=1)
        model = hscc.recipe_to_model_path("nonexistent-recipe")
        assert model is None

    def test_no_model_line(self, fake_ssh):
        fake_ssh.set_result(stdout="Runtime: vllm\nTP: 1\n", returncode=0)
        model = hscc.recipe_to_model_path("@official/qwen3.6-35b-a3b-fp8-vllm")
        assert model is None


# ── verify_recipe_on_nas() — NAS verification ─────────────────────────────

class TestVerifyRecipeOnNas:
    """NAS model verification."""

    def test_verified(self, fake_ssh):
        fake_ssh.set_result(stdout="snapshot-hash\n", returncode=0)  # ssh_cmd check_model_on_nas
        result = hscc.verify_recipe_on_nas("@official/qwen3.6-35b-a3b-fp8-vllm", "192.0.2.10")
        assert result["verified"] is True
        assert result["model"] == "Qwen/Qwen3.6-35B-A3B-FP8"

    def test_not_found_on_nas(self, fake_ssh):
        fake_ssh.set_result(stdout="", returncode=1)  # ssh_cmd check_model_on_nas
        fake_ssh.set_result(stdout="", returncode=1)  # find fallback
        result = hscc.verify_recipe_on_nas("@official/qwen3.6-35b-a3b-fp8-vllm", "192.0.2.10")
        assert result["verified"] is False
        assert "not found" in result["error"].lower()


# ── model_to_nas_dir() — NAS directory conversion ─────────────────────────

class TestModelToNasDir:
    """Model name → NAS directory conversion."""

    def test_conversion(self):
        result = hscc.model_to_nas_dir("Qwen/Qwen3.6-35B-A3B-FP8")
        assert result == "models--Qwen--Qwen3.6-35B-A3B-FP8"

    def test_single_slash(self):
        result = hscc.model_to_nas_dir("meta/llama3.1-70b")
        assert result == "models--meta--llama3.1-70b"


# ── resolve_local_recipe() — local recipe resolution ──────────────────────

class TestResolveLocalRecipe:
    """Local recipe file resolution."""

    def test_no_local_recipe(self):
        result = hscc.resolve_local_recipe("@official/qwen3.6-35b-a3b-fp8-vllm")
        assert result is None


# ── Edge cases — SSH errors, malformed JSON, etc. ─────────────────────────

class TestEdgeCases:
    """Edge cases and error paths."""

    def test_run_cmd_timeout(self, fake_ssh):
        fake_ssh.set_result(timeout_exc=True)
        result = hscc.run_cmd(["sleep", "999"], timeout=5)
        assert result["success"] is False
        assert "timed out" in result["error"].lower()
        assert result["command"] == ["sleep", "999"]

    def test_run_cmd_file_not_found(self, fake_ssh):
        fake_ssh.set_result(file_not_found=True)
        result = hscc.run_cmd(["totally_fake_binary"])
        assert result["success"] is False
        assert "command not found" in result["error"].lower()

    def test_ssh_cmd_timeout(self, fake_ssh):
        fake_ssh.set_result(timeout_exc=True)
        result = hscc.ssh_cmd("192.0.2.10", "ls /")
        assert result["success"] is False
        assert "timed out" in result["error"].lower()

    def test_ssh_cmd_file_not_found(self, fake_ssh):
        fake_ssh.set_result(file_not_found=True)
        result = hscc.ssh_cmd("192.0.2.10", "ls /")
        assert result["success"] is False
        assert "command not found" in result["error"].lower()

    def test_check_health_all_fail(self, fake_ssh):
        for _ in range(5):
            fake_ssh.set_result(stdout="", returncode=1)
        result = hscc.check_health("10.10.10.10")
        assert result["healthy"] is False

    def test_check_health_timeout_all(self, fake_ssh):
        for _ in range(5):
            fake_ssh.set_result(timeout_exc=True)
        result = hscc.check_health("10.10.10.10")
        assert result["healthy"] is False

    def test_cmd_assign_no_agents_json(self, tmp_path, provision_state_file, fake_ssh, capsys):
        """Assign when agents.json doesn't exist."""
        fake_ssh.set_result(stdout="", returncode=0)
        hscc.cmd_assign("agent-999", "@official/qwen3.6-35b-a3b-fp8-vllm")
        output = json.loads(capsys.readouterr().out)
        assert "not found" in output["error"].lower()

    def test_cmd_unassign_no_agents_json(self, tmp_path, provision_state_file, fake_ssh, capsys):
        """Unassign when agents.json doesn't exist."""
        fake_ssh.set_result(stdout="ok", returncode=0)  # log_event
        hscc.cmd_unassign("agent-999")
        output = json.loads(capsys.readouterr().out)
        assert "not found" in output["error"].lower()

    def test_get_running_containers_no_container_id(self, fake_ssh):
        """Malformed job line with no container ID."""
        output = "Job: @official/test  (tp=1, pp=1)\n"
        fake_ssh.set_result(stdout=output, returncode=0)
        containers = hscc.get_running_containers()
        assert len(containers) == 1
        assert containers[0]["container_id"] == "?"
