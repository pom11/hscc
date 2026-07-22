"""Unit tests for lifecycle.py - agent lifecycle management.

Tests reconcile_lifecycle, watchdog block, restart_vllm, and helpers.
"""
import datetime
import json
import os
import pytest
from pathlib import Path


class TestReconcileLifecycle:
    """reconcile_lifecycle() syncs lifecycle.json to agents.json status."""

    def test_no_lifecycle_file(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import lifecycle
        monkeypatch.setattr(lifecycle, "log", lambda *a, **kw: None)

        def fake_expanduser(p):
            if p == "~/.hscc/lifecycle.json":
                return str(tmp_hfcc_dir / "nonexistent.json")
            return p
        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)

        agents = [{"id": "a1", "status": "idle"}]
        lifecycle.reconcile_lifecycle(agents)  # should not raise

    def test_reconciles_running_to_idle(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import lifecycle
        monkeypatch.setattr(lifecycle, "log", lambda *a, **kw: None)
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        lc_file = tmp_hfcc_dir / "lifecycle.json"
        lc_file.write_text(json.dumps({
            "agents": {
                "a1": {"state": "running", "updated_at": "2026-01-01T00:00:00"},
                "a2": {"state": "idle", "updated_at": "2026-01-01T00:00:00"},
            }
        }))

        agents = [
            {"id": "a1", "status": "idle"},   # was running, now idle -> reconcile
            {"id": "a2", "status": "idle"},   # already idle -> no change
        ]

        def fake_expanduser(p):
            if p == "~/.hscc/lifecycle.json":
                return str(lc_file)
            return p
        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)

        lifecycle.reconcile_lifecycle(agents)

        updated = json.loads(lc_file.read_text())
        assert updated["agents"]["a1"]["state"] == "idle"
        assert updated["agents"]["a1"]["reconciled"] is True
        assert updated["agents"]["a2"]["state"] == "idle"  # unchanged

    def test_does_not_touch_spawning(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import lifecycle
        monkeypatch.setattr(lifecycle, "log", lambda *a, **kw: None)
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        lc_file = tmp_hfcc_dir / "lifecycle.json"
        lc_file.write_text(json.dumps({
            "agents": {
                "a1": {"state": "spawning", "updated_at": "2026-01-01T00:00:00"},
            }
        }))

        agents = [{"id": "a1", "status": "idle"}]  # spawning stays

        def fake_expanduser(p):
            if p == "~/.hscc/lifecycle.json":
                return str(lc_file)
            return p
        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)

        lifecycle.reconcile_lifecycle(agents)
        updated = json.loads(lc_file.read_text())
        assert updated["agents"]["a1"]["state"] == "spawning"  # unchanged

    def test_malformed_lifecycle_file(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import lifecycle
        monkeypatch.setattr(lifecycle, "log", lambda *a, **kw: None)

        lc_file = tmp_hfcc_dir / "lifecycle.json"
        lc_file.write_text("{bad json")

        def fake_expanduser(p):
            if p == "~/.hscc/lifecycle.json":
                return str(lc_file)
            return p
        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)

        lifecycle.reconcile_lifecycle([{"id": "a1", "status": "idle"}])  # should not raise


class TestFindHermesBin:
    """find_hermes_bin() locates the hermes executable."""

    def test_venv_path(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import lifecycle
        hermes = tmp_hfcc_dir / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes"
        hermes.parent.mkdir(parents=True)
        hermes.touch()

        def fake_expanduser(p):
            if p == "~":
                return str(tmp_hfcc_dir)
            return p
        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)
        monkeypatch.setattr("shutil.which", lambda x: None)  # not in PATH

        result = lifecycle.find_hermes_bin()
        assert result == str(hermes)

    def test_path_fallback(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import lifecycle
        hermes = tmp_hfcc_dir / "bin" / "hermes"
        hermes.parent.mkdir(parents=True)
        hermes.touch()

        monkeypatch.setattr("shutil.which", lambda x: str(hermes) if x == "hermes" else None)
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_hfcc_dir))

        result = lifecycle.find_hermes_bin()
        assert result == str(hermes)

    def test_not_found(self, monkeypatch):
        from hscc_daemon import lifecycle
        monkeypatch.setattr("shutil.which", lambda x: None)
        monkeypatch.setattr(os.path, "expanduser", lambda p: "/nonexistent")
        assert lifecycle.find_hermes_bin() is None


class TestKanbanTaskStatus:
    """_kanban_task_status() reads task status from kanban file."""

    def test_finds_task(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import lifecycle

        kb_file = tmp_hfcc_dir / "kanban.json"
        kb_file.write_text(json.dumps({
            "tasks": [
                {"id": "t1", "status": "running"},
                {"id": "t2", "status": "done"},
            ]
        }))

        def fake_expanduser(p):
            if p == "~/.hermes/kanban.json":
                return str(kb_file)
            return p
        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)

        assert lifecycle._kanban_task_status("t1") == "running"
        assert lifecycle._kanban_task_status("t2") == "done"

    def test_missing_task(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import lifecycle

        kb_file = tmp_hfcc_dir / "kanban.json"
        kb_file.write_text(json.dumps({"tasks": []}))

        def fake_expanduser(p):
            if p == "~/.hermes/kanban.json":
                return str(kb_file)
            return p
        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)

        assert lifecycle._kanban_task_status("missing") == "unknown"

    def test_missing_file(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import lifecycle

        def fake_expanduser(p):
            if p == "~/.hermes/kanban.json":
                return str(tmp_hfcc_dir / "nonexistent.json")
            return p
        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)

        assert lifecycle._kanban_task_status("t1") == "unknown"


class TestLoadSaveWatchdogBlock:
    """load_watchdog_block() and save_watchdog_block() persist watchdog state."""

    def test_load_default(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import lifecycle
        monkeypatch.setattr(lifecycle, "WATCHDOG_BLOCK_FILE", str(tmp_hfcc_dir / "block.json"))
        block = lifecycle.load_watchdog_block()
        assert block["blocked"] is False
        assert block["reason"] == ""
        assert block["auto_restart_count"] == 0
        assert block["blocked_at"] is None
        assert block["failures"] == []

    def test_load_existing(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import lifecycle
        file_path = tmp_hfcc_dir / "block.json"
        file_path.write_text(json.dumps({
            "blocked": True,
            "reason": "3 failures",
            "blocked_at": "2026-01-01T00:00:00",
            "auto_restart_count": 2,
            "failures": [],
        }))
        monkeypatch.setattr(lifecycle, "WATCHDOG_BLOCK_FILE", str(file_path))
        block = lifecycle.load_watchdog_block()
        assert block["blocked"] is True
        assert block["reason"] == "3 failures"
        assert block["auto_restart_count"] == 2

    def test_save_and_load(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import lifecycle
        monkeypatch.setattr(lifecycle, "WATCHDOG_BLOCK_FILE", str(tmp_hfcc_dir / "block.json"))
        lifecycle.save_watchdog_block({"blocked": True, "reason": "test", "auto_restart_count": 1})
        block = lifecycle.load_watchdog_block()
        assert block["blocked"] is True
        assert block["reason"] == "test"

    def test_load_malformed(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import lifecycle
        file_path = tmp_hfcc_dir / "block.json"
        file_path.write_text("{bad")
        monkeypatch.setattr(lifecycle, "WATCHDOG_BLOCK_FILE", str(file_path))
        block = lifecycle.load_watchdog_block()
        assert block["blocked"] is False  # returns default


class TestCleanupOldFailures:
    """cleanup_old_failures() keeps only recent failures."""

    def test_keeps_recent(self):
        from hscc_daemon.lifecycle import cleanup_old_failures
        now = datetime.datetime.now(datetime.timezone.utc)
        failures = [
            {"timestamp": (now - datetime.timedelta(minutes=5)).isoformat()},
            {"timestamp": (now - datetime.timedelta(minutes=1)).isoformat()},
        ]
        result = cleanup_old_failures(failures, window_minutes=10)
        assert len(result) == 2

    def test_removes_old(self):
        from hscc_daemon.lifecycle import cleanup_old_failures
        now = datetime.datetime.now(datetime.timezone.utc)
        failures = [
            {"timestamp": (now - datetime.timedelta(minutes=15)).isoformat()},
            {"timestamp": (now - datetime.timedelta(minutes=1)).isoformat()},
        ]
        result = cleanup_old_failures(failures, window_minutes=10)
        assert len(result) == 1

    def test_keeps_no_timestamp(self):
        from hscc_daemon.lifecycle import cleanup_old_failures
        failures = [
            {"timestamp": ""},
            {"timestamp": "not-a-date"},
        ]
        result = cleanup_old_failures(failures, window_minutes=10)
        assert len(result) == 2  # entries without valid timestamps are kept

    def test_empty_list(self):
        from hscc_daemon.lifecycle import cleanup_old_failures
        assert cleanup_old_failures([], window_minutes=10) == []


class TestRefreshLiveWorkers:
    """refresh_live_workers() updates workers.json with live status."""

    def test_no_nodes_env(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import lifecycle
        monkeypatch.setattr(lifecycle, "log", lambda *a, **kw: None)
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        workers_file = tmp_hfcc_dir / "workers.json"

        def fake_expanduser(p):
            if p == "~/.hscc/workers.json":
                return str(workers_file)
            return p
        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)
        monkeypatch.delenv("HSCC_NODES", raising=False)

        lifecycle.refresh_live_workers()
        data = json.loads(workers_file.read_text())
        assert data["workers"] == []

    def test_nodes_updated(self, tmp_hfcc_dir, monkeypatch, fake_subprocess):
        from hscc_daemon import lifecycle
        monkeypatch.setattr(lifecycle, "log", lambda *a, **kw: None)
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        workers_file = tmp_hfcc_dir / "workers.json"

        def fake_expanduser(p):
            if p == "~/.hscc/workers.json":
                return str(workers_file)
            return p
        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)
        monkeypatch.setenv("HSCC_NODES", "10.0.0.1,10.0.0.2")

        fake_subprocess.set_result(stdout='{"ok":true}', returncode=0)  # node 1
        fake_subprocess.set_result(stdout="", returncode=1)  # node 2

        lifecycle.refresh_live_workers()
        data = json.loads(workers_file.read_text())
        assert len(data["workers"]) == 2
        assert data["workers"][0]["status"] == "online"
        assert data["workers"][1]["status"] == "offline"


class TestPipelineWatchdog:
    """pipeline_watchdog() return value — healthy only when BOTH dgx and gateway pass."""

    def test_both_ok_returns_true(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import lifecycle
        monkeypatch.setattr(lifecycle, "log", lambda *a, **kw: None)
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(lifecycle, "WATCHDOG_BLOCK_FILE", str(tmp_hfcc_dir / "block.json"))

        result = lifecycle.pipeline_watchdog(
            check_dgx_fn=lambda: True,
            check_gateway_fn=lambda: True,
            restart_vllm_fn=lambda: {"ok": True},
            send_macos_notification_fn=lambda *a, **kw: None,
        )
        assert result is True

    def test_dgx_fail_gw_ok_returns_false(self, tmp_hfcc_dir, monkeypatch):
        """Partial failure (DGX down, gateway OK) returns False."""
        from hscc_daemon import lifecycle
        monkeypatch.setattr(lifecycle, "log", lambda *a, **kw: None)
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(lifecycle, "WATCHDOG_BLOCK_FILE", str(tmp_hfcc_dir / "block.json"))

        restart_calls = []
        result = lifecycle.pipeline_watchdog(
            check_dgx_fn=lambda: False,
            check_gateway_fn=lambda: True,
            restart_vllm_fn=lambda: restart_calls.append(1) or {"ok": True},
            send_macos_notification_fn=lambda *a, **kw: None,
        )
        assert result is False  # NOT True — the old bug returned True here
        assert len(restart_calls) == 1  # vLLM restart was attempted

    def test_dgx_ok_gw_fail_returns_false(self, tmp_hfcc_dir, monkeypatch):
        """Partial failure (gateway down, DGX OK) returns False."""
        from hscc_daemon import lifecycle
        monkeypatch.setattr(lifecycle, "log", lambda *a, **kw: None)
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(lifecycle, "WATCHDOG_BLOCK_FILE", str(tmp_hfcc_dir / "block.json"))

        result = lifecycle.pipeline_watchdog(
            check_dgx_fn=lambda: True,
            check_gateway_fn=lambda: False,
            restart_vllm_fn=lambda: {"ok": True},
            send_macos_notification_fn=lambda *a, **kw: None,
        )
        assert result is False  # NOT True — the old bug returned True here

    def test_both_fail_returns_false(self, tmp_hfcc_dir, monkeypatch):
        """Both failing returns False."""
        from hscc_daemon import lifecycle
        monkeypatch.setattr(lifecycle, "log", lambda *a, **kw: None)
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(lifecycle, "WATCHDOG_BLOCK_FILE", str(tmp_hfcc_dir / "block.json"))

        result = lifecycle.pipeline_watchdog(
            check_dgx_fn=lambda: False,
            check_gateway_fn=lambda: False,
            restart_vllm_fn=lambda: {"ok": True},
            send_macos_notification_fn=lambda *a, **kw: None,
        )
        assert result is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
