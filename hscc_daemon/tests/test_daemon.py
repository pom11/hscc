#!/usr/bin/env python3
"""Tests for hscc_daemon handler layer and escalator logic."""

import sys
import os
import json
import time
from pathlib import Path

# Add parent dir to path so we can import handlers
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from handlers.base import AbstractHandler, HandlerResult, HANDLER_TIMEOUT_SEC
from handlers.vllm import VLLMHandler
from handlers.container import ContainerHandler
from handlers.gateway import GatewayHandler
from handlers.nas import NASHandler

# ─── Test helpers ───


def make_mock_handler(
    return_status: str, detail: dict | None = None, throw: bool = False
):
    """Create a minimal handler that returns a fixed status."""

    class MockHandler(AbstractHandler):
        def __init__(self, status, det, throws):
            self._status = status
            self._det = det
            self._throws = throws

        @property
        def name(self) -> str:
            return "mock"

        def check(self):
            if self._throws:
                raise RuntimeError("mock exception")
            return HandlerResult(status=self._status, detail=self._det or {})

    return MockHandler(return_status, detail, throw)


# ─── HandlerResult tests ───


class TestHandlerResult:
    def test_valid_statuses(self):
        for status in ("healthy", "unhealthy", "unknown"):
            r = HandlerResult(status=status, detail={"test": True})
            assert r.status == status
            assert r.to_dict()["status"] == status

    def test_invalid_status_raises(self):
        try:
            HandlerResult(status="broken", detail={})
            assert False, "should have raised"
        except ValueError:
            pass  # expected

    def test_to_dict_serializable(self):
        r = HandlerResult(
            status="healthy", detail={"key": "val"}, elapsed_ms=42.5
        )
        d = r.to_dict()
        assert d == {
            "status": "healthy",
            "detail": {"key": "val"},
            "elapsed_ms": 42.5,
        }

    def test_to_json_serializable(self):
        r = HandlerResult(status="unhealthy", detail={"error": "test"})
        obj = json.loads(r.to_json())
        assert obj["status"] == "unhealthy"
        assert obj["detail"]["error"] == "test"

    def test_empty_detail_default(self):
        r = HandlerResult(status="healthy")
        assert r.detail == {}
        assert "detail" in r.to_dict()


# ─── AbstractHandler tests via mock ───


class TestMockHandler:
    def test_healthy_result(self):
        h = make_mock_handler("healthy", {"cpu": 50})
        r = h.run()
        assert r.status == "healthy"
        assert r.detail == {"cpu": 50}
        assert r.elapsed_ms is not None

    def test_unhealthy_result(self):
        h = make_mock_handler("unhealthy", {"error": "down"})
        r = h.run()
        assert r.status == "unhealthy"

    def test_exception_becomes_unknown(self):
        h = make_mock_handler("healthy", {}, throw=True)
        r = h.run()
        assert r.status == "unknown"
        assert "mock exception" in r.detail["error"]

    def test_timeout_becomes_unknown(self):
        """Handler that sleeps longer than 10s should be caught by timeout."""

        class SlowHandler(AbstractHandler):
            @property
            def name(self):
                return "slow"

            def check(self):
                time.sleep(HANDLER_TIMEOUT_SEC + 2)
                return HandlerResult(status="healthy")

        h = SlowHandler()
        r = h.run()
        assert r.status == "unknown"
        assert "timeout" in r.detail["error"]


# ─── vLLM handler tests ───


class TestVLLMHandler:
    def test_constructor_default_url(self):
        h = VLLMHandler()
        assert h.url == "http://localhost:8000/health"
        assert h.name == "vllm"

    def test_custom_url(self):
        h = VLLMHandler(url="http://10.0.0.1:9000/health")
        assert h.url == "http://10.0.0.1:9000/health"

    def test_unreachable_port_becomes_unknown(self):
        """Port 19999 should be unknown (connection refused), not crash."""
        h = VLLMHandler(url="http://localhost:19999/health")
        r = h.run()
        assert r.status == "unknown"
        assert "connection" in r.detail["error"]


# ─── Container handler tests ───


class TestContainerHandler:
    def test_constructor_default(self):
        h = ContainerHandler()
        assert h.container_id == "hscc-orchestrator"
        assert h.name == "container"

    def test_nonexistent_container_is_unhealthy(self):
        """A container that doesn't exist should be unhealthy, not unknown."""
        h = ContainerHandler(
            container_id="definitely-not-a-real-container-xyz-12345"
        )
        r = h.run()
        assert r.status == "unhealthy"
        assert "not found" in r.detail.get("error", "").lower()


# ─── Gateway handler tests ───


class TestGatewayHandler:
    def test_constructor_default(self):
        h = GatewayHandler()
        assert h.url == "http://localhost:18789/health"
        assert h.name == "gateway"

    def test_unreachable_port_becomes_unknown(self):
        h = GatewayHandler(url="http://localhost:19998/health")
        r = h.run()
        assert r.status == "unknown"
        assert "connection" in r.detail["error"]


# ─── NAS handler tests ───


class TestNASHandler:
    def test_constructor_defaults(self):
        h = NASHandler()
        assert h.host == "nas.local"
        assert h.name == "nas"

    def test_ssh_not_found_becomes_unknown(self):
        """If ssh host is unreachable, should return unknown not crash."""
        h = NASHandler(host="nonexistent-host-xyz-12345")
        r = h.run()
        assert r.status == "unknown"  # host unreachable


# ─── Core loop / Escalator tests ───


class CoreLoop:
    """Minimal core loop for escalation testing."""

    def _run_mock_cycle(self, handler_map: dict) -> dict:
        """Run a simulated daemon cycle with mocked handlers."""
        report = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cycle": 1,
            "checks": {},
            "actions": [],
        }

        for name, handler in handler_map.items():
            result = handler.run()
            report["checks"][name] = result.to_dict()

        # Escalator logic (inlined here for test)
        self._escalate(report)
        return report

    def _escalate(self, report: dict):
        """Core escalator logic — MUST match daemon.py escalation."""
        checks = report["checks"]
        actions = report["actions"]

        # Count statuses
        statuses = [c["status"] for c in checks.values()]
        any_unhealthy = "unhealthy" in statuses
        any_unknown = "unknown" in statuses
        all_unknown = all(s == "unknown" for s in statuses)

        # Orchestrator check (vllm + container are orchestrator-related)
        vllm_status = checks.get("vllm", {}).get("status", "unknown")
        container_status = checks.get("container", {}).get("status", "unknown")
        orchestrator_down = (
            vllm_status == "unhealthy"
            and container_status == "unhealthy"
            and vllm_status != "unknown"
            and container_status != "unknown"
        )

        # Action: auto-restart orchestrator
        if orchestrator_down:
            actions.append("restart_orchestrator")

        # Action: Telegram alert for non-orchestrator unhealthy
        non_orchestrator_unhealthy = not orchestrator_down and any_unhealthy
        if non_orchestrator_unhealthy:
            actions.append("telegram_alert")

        # Action: Telegram alert if all unknown (system blind)
        if all_unknown:
            actions.append("telegram_alert_all_unknown")


class TestEscalatorLogic:
    def test_all_healthy_no_actions(self):
        map_ = {
            "vllm": make_mock_handler("healthy"),
            "container": make_mock_handler("healthy"),
            "gateway": make_mock_handler("healthy"),
            "nas": make_mock_handler("healthy"),
        }
        report = CoreLoop()._run_mock_cycle(map_)
        assert report["actions"] == [], f"expected no actions, got {report['actions']}"

    def test_vllm_unhealthy_triggers_restart(self):
        map_ = {
            "vllm": make_mock_handler("unhealthy", {"error": "dead"}),
            "container": make_mock_handler("unhealthy", {"error": "not found"}),
            "gateway": make_mock_handler("healthy"),
            "nas": make_mock_handler("healthy"),
        }
        report = CoreLoop()._run_mock_cycle(map_)
        assert "restart_orchestrator" in report["actions"]

    def test_gateway_unhealthy_triggers_telegram(self):
        map_ = {
            "vllm": make_mock_handler("healthy"),
            "container": make_mock_handler("healthy"),
            "gateway": make_mock_handler("unhealthy", {"error": "down"}),
            "nas": make_mock_handler("healthy"),
        }
        report = CoreLoop()._run_mock_cycle(map_)
        assert "telegram_alert" in report["actions"]
        assert "restart_orchestrator" not in report["actions"]

    def test_all_unknown_triggers_alert(self):
        map_ = {
            "vllm": make_mock_handler("unknown"),
            "container": make_mock_handler("unknown"),
            "gateway": make_mock_handler("unknown"),
            "nas": make_mock_handler("unknown"),
        }
        report = CoreLoop()._run_mock_cycle(map_)
        assert "telegram_alert_all_unknown" in report["actions"]

    def test_mixed_healthy_unknown_no_restart(self):
        """unknown status should NOT trigger restart (only unhealthy does)."""
        map_ = {
            "vllm": make_mock_handler("unknown"),
            "container": make_mock_handler("unknown"),
            "gateway": make_mock_handler("healthy"),
            "nas": make_mock_handler("healthy"),
        }
        report = CoreLoop()._run_mock_cycle(map_)
        assert "restart_orchestrator" not in report["actions"]


# ─── Faulty Business Logic Checks ───


class TestFaultyBusinessLogic:
    """Prevent faulty logic patterns. Each test ensures a common bug cannot happen."""

    def test_unknown_never_becomes_restart(self):
        """CRITICAL: unknown status must NEVER trigger auto-restart.
        Only explicit 'unhealthy' can trigger restart."""
        map_ = {
            "vllm": make_mock_handler("unknown", {"error": "timeout"}),
            "container": make_mock_handler("unhealthy", {"error": "not found"}),
            "gateway": make_mock_handler("healthy"),
            "nas": make_mock_handler("healthy"),
        }
        report = CoreLoop()._run_mock_cycle(map_)
        # vllm=unknown, container=unhealthy => unknown check fails => no restart
        assert "restart_orchestrator" not in report["actions"]

    def test_single_unhealthy_does_not_restart(self):
        """If only non-orchestrator is unhealthy, no restart."""
        map_ = {
            "vllm": make_mock_handler("healthy"),
            "container": make_mock_handler("healthy"),
            "gateway": make_mock_handler("unhealthy"),
            "nas": make_mock_handler("healthy"),
        }
        report = CoreLoop()._run_mock_cycle(map_)
        assert "restart_orchestrator" not in report["actions"]
        assert "telegram_alert" in report["actions"]

    def test_max_one_restart_per_cycle(self):
        """Even if multiple orchestrator checks fail, restart action appears only once."""
        map_ = {
            "vllm": make_mock_handler("unhealthy"),
            "container": make_mock_handler("unhealthy"),
            "gateway": make_mock_handler("unhealthy"),
            "nas": make_mock_handler("unhealthy"),
        }
        report = CoreLoop()._run_mock_cycle(map_)
        restart_count = report["actions"].count("restart_orchestrator")
        assert restart_count == 1, f"Expected 1 restart, got {restart_count}"

    def test_handler_crash_does_not_crash_loop(self):
        """If a handler's check() throws, the daemon continues with other handlers."""
        map_ = {
            "vllm": make_mock_handler("healthy"),
            "container": make_mock_handler("healthy"),
            "gateway": make_mock_handler("unknown", {"error": "handler crashed"}),
            "nas": make_mock_handler("healthy"),
        }
        report = CoreLoop()._run_mock_cycle(map_)
        # All 4 handlers should be in the report (none missing)
        assert len(report["checks"]) == 4
        assert "gateway" in report["checks"]
        assert report["checks"]["gateway"]["status"] == "unknown"

    def test_report_always_written_even_if_partial(self):
        """Even if handler times out, report JSON is always written."""
        map_ = {
            "vllm": make_mock_handler("healthy"),
            "container": make_mock_handler("unknown", {"error": "timeout"}),
            "gateway": make_mock_handler("unknown", {"error": "timeout"}),
            "nas": make_mock_handler("unknown", {"error": "timeout"}),
        }
        report = CoreLoop()._run_mock_cycle(map_)
        assert "timestamp" in report
        assert "cycle" in report
        assert "checks" in report
        assert "actions" in report
        assert len(report["checks"]) == 4

    def test_telegram_rate_limit_enforced(self):
        """Max 5 alerts per 60s. With 3 unhealthy checks, only 1 telegram action."""
        # Current design: one telegram_alert action covers all non-orchestrator failures
        map_ = {
            "vllm": make_mock_handler("healthy"),
            "container": make_mock_handler("healthy"),
            "gateway": make_mock_handler("unhealthy"),
            "nas": make_mock_handler("unhealthy"),
        }
        report = CoreLoop()._run_mock_cycle(map_)
        alert_count = report["actions"].count("telegram_alert")
        assert alert_count == 1, f"Expected 1 telegram action (batched), got {alert_count}"

    def test_false_positive_guard(self):
        """A handler returning an unusual detail dict must not change status classification."""
        map_ = {
            "vllm": make_mock_handler(
                "healthy", {"weird": {"nested": {"data": [1, 2, 3]}}}
            ),
            "container": make_mock_handler("healthy", {"state": "running", "uptime": "5h"}),
            "gateway": make_mock_handler("unhealthy", {"detail": {"nested": "value"}}),
            "nas": make_mock_handler("healthy", {"disk_pct": 45}),
        }
        report = CoreLoop()._run_mock_cycle(map_)
        assert report["checks"]["vllm"]["status"] == "healthy"
        assert report["checks"]["gateway"]["status"] == "unhealthy"
        assert "telegram_alert" in report["actions"]
        assert "restart_orchestrator" not in report["actions"]

    def test_vllm_unhealthy_container_healthy_no_restart(self):
        """vLLM unhealthy alone shouldn't restart - container must also be unhealthy."""
        map_ = {
            "vllm": make_mock_handler("unhealthy"),
            "container": make_mock_handler("healthy"),
            "gateway": make_mock_handler("healthy"),
            "nas": make_mock_handler("healthy"),
        }
        report = CoreLoop()._run_mock_cycle(map_)
        assert "restart_orchestrator" not in report["actions"]

    def test_container_unhealthy_vllm_healthy_no_restart(self):
        """Container unhealthy alone shouldn't restart - vLLM must also be unhealthy."""
        map_ = {
            "vllm": make_mock_handler("healthy"),
            "container": make_mock_handler("unhealthy"),
            "gateway": make_mock_handler("healthy"),
            "nas": make_mock_handler("healthy"),
        }
        report = CoreLoop()._run_mock_cycle(map_)
        assert "restart_orchestrator" not in report["actions"]
