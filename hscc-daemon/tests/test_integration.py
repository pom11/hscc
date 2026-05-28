#!/usr/bin/env python3
"""Integration tests for dry-run mode and full daemon lifecycle."""

import sys
import os
import json
import io
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

# Add parent dir to path so we can import handlers and daemon
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from handlers.base import AbstractHandler, HandlerResult

# ─── Test helpers ───


def make_mock_handler(return_status, detail=None, throw=False):
    """Create a minimal handler that returns a fixed status."""

    class MockHandler(AbstractHandler):
        def __init__(self, status, det, throws):
            self._status = status
            self._det = det
            self._throws = throws

        @property
        def name(self):
            return "mock"

        def check(self):
            if self._throws:
                raise RuntimeError("mock exception")
            return HandlerResult(status=self._status, detail=detail or {})

    return MockHandler(return_status, detail, throw)


# ─── Dry-run integration tests ───


class TestDryRunIntegration:
    """Test that --dry-run skips all real actions."""

    def test_dry_run_skips_restart(self):
        """Dry-run should report restart action but NOT execute it."""
        # Create a minimal config dict
        config = {
            "poll_interval_sec": 60,
            "handlers": {
                "vllm": {"url": "http://localhost:8000/health"},
                "gateway": {"url": "http://localhost:18789/health"},
                "container": {"id": "hscc-orchestrator"},
                "nas": {"host": "nas.local", "path": "/", "key_path": None},
            },
            "telegram": {"chat_id": None, "bot_token": None, "max_restarts": 1},
        }

        from daemon import Daemon

        daemon = Daemon(config=config, dry_run=True)

        # Override handlers with mock handlers for testing
        daemon.handlers = {
            "vllm": make_mock_handler("unhealthy", {"error": "dead"}),
            "container": make_mock_handler("unhealthy", {"error": "not found"}),
            "gateway": make_mock_handler("healthy"),
            "nas": make_mock_handler("healthy"),
        }

        report = {
            "timestamp": "2026-05-28T07:00:00Z",
            "cycle": 1,
            "checks": {},
            "actions": [],
        }

        # Run checks
        for name, handler in daemon.handlers.items():
            result = handler.run()
            report["checks"][name] = result.to_dict()

        # Escalate
        daemon._escalate(report)

        # Execute actions (dry-run)
        f = io.StringIO()
        with redirect_stdout(f):
            daemon._execute_actions(report)

        output = f.getvalue()
        assert "DRY-RUN" in output
        assert "restart_orchestrator" in output

    def test_dry_run_skips_telegram(self):
        """Dry-run should NOT attempt to send Telegram."""
        from daemon import Daemon

        config = {
            "poll_interval_sec": 60,
            "handlers": {
                "vllm": {"url": "http://localhost:8000/health"},
                "gateway": {"url": "http://localhost:18789/health"},
                "container": {"id": "hscc-orchestrator"},
                "nas": {"host": "nas.local", "path": "/", "key_path": None},
            },
            "telegram": {"chat_id": "123", "bot_token": "abc", "max_restarts": 1},
        }

        daemon = Daemon(config=config, dry_run=True)
        daemon.handlers = {
            "vllm": make_mock_handler("healthy"),
            "container": make_mock_handler("healthy"),
            "gateway": make_mock_handler("unhealthy"),
            "nas": make_mock_handler("healthy"),
        }

        report = {
            "timestamp": "2026-05-28T07:00:00Z",
            "cycle": 1,
            "checks": {},
            "actions": [],
        }
        for name, handler in daemon.handlers.items():
            result = handler.run()
            report["checks"][name] = result.to_dict()

        daemon._escalate(report)

        f = io.StringIO()
        with redirect_stdout(f):
            daemon._execute_actions(report)

        output = f.getvalue()
        assert "DRY-RUN" in output
        assert "telegram_alert" in output


class TestDaemonPersistence:
    """Test state persistence (status.json, alerts.jsonl)."""

    def test_persist_status_creates_file(self):
        """_persist_status should write status.json to disk."""
        from daemon import Daemon

        config = {
            "poll_interval_sec": 60,
            "handlers": {
                "vllm": {"url": "http://localhost:8000/health"},
                "gateway": {"url": "http://localhost:18789/health"},
                "container": {"id": "hscc-orchestrator"},
                "nas": {"host": "nas.local", "path": "/", "key_path": None},
            },
            "telegram": {"chat_id": None, "bot_token": None, "max_restarts": 1},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            # Override global paths
            import daemon as daemon_module

            orig_status = daemon_module.STATUS_FILE
            orig_config = daemon_module.CONFIG_FILE
            orig_pid = daemon_module.PID_FILE

            daemon_module.STATUS_FILE = os.path.join(tmpdir, "status.json")
            daemon_module.CONFIG_FILE = os.path.join(tmpdir, "config.json")
            daemon_module.PID_FILE = os.path.join(tmpdir, "daemon.pid")
            daemon_module.DAEMON_DIR = tmpdir

            try:
                daemon = Daemon(config=config, dry_run=True)
                report = {
                    "timestamp": "2026-05-28T07:00:00Z",
                    "cycle": 42,
                    "checks": {
                        "vllm": {"status": "healthy", "detail": {"code": 200}},
                        "gateway": {"status": "unhealthy", "detail": {"error": "down"}},
                    },
                    "actions": ["telegram_alert"],
                }
                daemon._persist_status(report)

                # Verify file was written
                assert os.path.exists(daemon_module.STATUS_FILE)
                with open(daemon_module.STATUS_FILE) as f:
                    written = json.load(f)
                assert written["cycle"] == 42
                assert written["checks"]["vllm"]["status"] == "healthy"
                assert "telegram_alert" in written["actions"]
            finally:
                daemon_module.STATUS_FILE = orig_status
                daemon_module.CONFIG_FILE = orig_config
                daemon_module.PID_FILE = orig_pid

    def test_persist_alerts_appends(self):
        """_persist_alerts should append to alerts.jsonl."""
        from daemon import Daemon

        config = {
            "poll_interval_sec": 60,
            "handlers": {
                "vllm": {"url": "http://localhost:8000/health"},
                "gateway": {"url": "http://localhost:18789/health"},
                "container": {"id": "hscc-orchestrator"},
                "nas": {"host": "nas.local", "path": "/", "key_path": None},
            },
            "telegram": {"chat_id": None, "bot_token": None, "max_restarts": 1},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            import daemon as daemon_module

            orig_alerts = daemon_module.ALERTS_FILE
            orig_status = daemon_module.STATUS_FILE
            orig_config = daemon_module.CONFIG_FILE
            orig_pid = daemon_module.PID_FILE

            daemon_module.ALERTS_FILE = os.path.join(tmpdir, "alerts.jsonl")
            daemon_module.STATUS_FILE = os.path.join(tmpdir, "status.json")
            daemon_module.CONFIG_FILE = os.path.join(tmpdir, "config.json")
            daemon_module.PID_FILE = os.path.join(tmpdir, "daemon.pid")
            daemon_module.DAEMON_DIR = tmpdir

            try:
                daemon = Daemon(config=config, dry_run=True)

                # Add some pending alerts
                daemon.pending_alerts = [
                    {
                        "timestamp": "2026-05-28T07:00:00Z",
                        "severity": "warning",
                        "message": "Test alert 1",
                        "auto_resolved": False,
                    },
                    {
                        "timestamp": "2026-05-28T07:01:00Z",
                        "severity": "critical",
                        "message": "Test alert 2",
                        "auto_resolved": False,
                    },
                ]

                daemon._persist_alerts()

                # Verify file was written with 2 lines
                assert os.path.exists(daemon_module.ALERTS_FILE)
                with open(daemon_module.ALERTS_FILE) as f:
                    lines = [l.strip() for l in f.readlines() if l.strip()]
                assert len(lines) == 2

                # Verify alerts were cleared
                assert daemon.pending_alerts == []

                # Parse and verify content
                alerts = [json.loads(l) for l in lines]
                assert alerts[0]["message"] == "Test alert 1"
                assert alerts[1]["message"] == "Test alert 2"
            finally:
                daemon_module.ALERTS_FILE = orig_alerts
                daemon_module.STATUS_FILE = orig_status
                daemon_module.CONFIG_FILE = orig_config
                daemon_module.PID_FILE = orig_pid


class TestConfigLoading:
    """Test config loading and merging."""

    def test_defaults_when_no_config(self):
        """load_config should return defaults when config file doesn't exist."""
        import daemon as daemon_module

        with tempfile.TemporaryDirectory() as tmpdir:
            orig_config = daemon_module.CONFIG_FILE
            daemon_module.CONFIG_FILE = os.path.join(tmpdir, "no_config.json")

            try:
                config = daemon_module.load_config()
                assert config["poll_interval_sec"] == 60
                assert config["handlers"]["vllm"]["url"] == "http://localhost:8000/health"
                assert config["telegram"]["max_restarts"] == 1
            finally:
                daemon_module.CONFIG_FILE = orig_config

    def test_user_overrides_defaults(self):
        """User config should override defaults."""
        import daemon as daemon_module

        with tempfile.TemporaryDirectory() as tmpdir:
            orig_config = daemon_module.CONFIG_FILE
            config_path = os.path.join(tmpdir, "config.json")

            # Create a minimal user config
            with open(config_path, "w") as f:
                json.dump({"poll_interval_sec": 30, "telegram": {"max_restarts": 3}}, f)

            daemon_module.CONFIG_FILE = config_path

            try:
                config = daemon_module.load_config()
                assert config["poll_interval_sec"] == 30
                assert config["telegram"]["max_restarts"] == 3
                # Defaults should still be present for missing keys
                assert config["handlers"]["vllm"]["url"] == "http://localhost:8000/health"
            finally:
                daemon_module.CONFIG_FILE = orig_config


class TestEscalatorConsistency:
    """Verify daemon._escalate matches test CoreLoop._escalate."""

    def test_restart_requires_both_vllm_and_container(self):
        """Restart only when BOTH vllm AND container are unhealthy."""
        from daemon import Daemon

        config = {
            "poll_interval_sec": 60,
            "handlers": {
                "vllm": {"url": "http://localhost:8000/health"},
                "gateway": {"url": "http://localhost:18789/health"},
                "container": {"id": "hscc-orchestrator"},
                "nas": {"host": "nas.local", "path": "/", "key_path": None},
            },
            "telegram": {"chat_id": None, "bot_token": None, "max_restarts": 1},
        }

        daemon = Daemon(config=config, dry_run=True)
        daemon.handlers = {
            "vllm": make_mock_handler("unhealthy"),
            "container": make_mock_handler("unhealthy"),
            "gateway": make_mock_handler("healthy"),
            "nas": make_mock_handler("healthy"),
        }

        report = {"timestamp": "T", "cycle": 1, "checks": {}, "actions": []}
        for name, handler in daemon.handlers.items():
            result = handler.run()
            report["checks"][name] = result.to_dict()
        daemon._escalate(report)

        assert "restart_orchestrator" in report["actions"]

    def test_unknown_prevents_restart(self):
        """Unknown status should prevent restart."""
        from daemon import Daemon

        config = {
            "poll_interval_sec": 60,
            "handlers": {
                "vllm": {"url": "http://localhost:8000/health"},
                "gateway": {"url": "http://localhost:18789/health"},
                "container": {"id": "hscc-orchestrator"},
                "nas": {"host": "nas.local", "path": "/", "key_path": None},
            },
            "telegram": {"chat_id": None, "bot_token": None, "max_restarts": 1},
        }

        daemon = Daemon(config=config, dry_run=True)
        daemon.handlers = {
            "vllm": make_mock_handler("unknown"),
            "container": make_mock_handler("healthy"),
            "gateway": make_mock_handler("healthy"),
            "nas": make_mock_handler("healthy"),
        }

        report = {"timestamp": "T", "cycle": 1, "checks": {}, "actions": []}
        for name, handler in daemon.handlers.items():
            result = handler.run()
            report["checks"][name] = result.to_dict()
        daemon._escalate(report)

        assert "restart_orchestrator" not in report["actions"]
