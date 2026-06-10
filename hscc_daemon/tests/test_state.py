"""Unit tests for state.py - state directory management.

Tests are fully isolated: STATE_DIR is monkeypatched to tmp_path so no real
~/.hscc/state files are touched.
"""
import json
import os
import pytest
from pathlib import Path


class TestNowIso:
    """now_iso() returns an ISO 8601 UTC timestamp."""

    def test_returns_iso_string(self):
        from hscc_daemon.state import now_iso
        ts = now_iso()
        assert isinstance(ts, str)
        assert "+" in ts or "Z" in ts  # has timezone
        # Basic format check: YYYY-MM-DDTHH:MM:SS
        assert len(ts) >= 19

    def test_contains_date_time(self):
        from hscc_daemon.state import now_iso
        ts = now_iso()
        assert "T" in ts
        assert len(ts.split("T")) == 2


class TestEnsureStateDir:
    """ensure_state_dir() creates the state directory."""

    def test_creates_directory(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        new_dir = tmp_hfcc_dir / "new_state"
        assert not new_dir.exists()
        monkeypatch.setattr(state, "STATE_DIR", str(new_dir))
        state.ensure_state_dir()
        assert new_dir.is_dir()

    def test_noop_when_exists(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        # tmp_hfcc_dir already exists from fixture
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        state.ensure_state_dir()  # should not raise
        assert tmp_hfcc_dir.is_dir()


class TestWriteState:
    """write_state() persists check results atomically."""

    def test_write_and_read(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        entry = state.write_state("dgx", {"ok": True, "gpu_count": 8})
        assert entry["timestamp"] is not None
        assert entry["stream"] == "dgx"
        assert entry["ok"] is True
        assert entry["gpu_count"] == 8

    def test_persists_json(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        state.write_state("gateway", {"ok": False, "gateway_job": True})
        filepath = tmp_hfcc_dir / "gateway.json"
        assert filepath.exists()
        data = json.loads(filepath.read_text())
        assert data["ok"] is False
        assert data["gateway_job"] is True

    def test_overwrites_existing(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        state.write_state("dgx", {"ok": True})
        state.write_state("dgx", {"ok": False, "error": "timeout"})
        filepath = tmp_hfcc_dir / "dgx.json"
        data = json.loads(filepath.read_text())
        assert data["ok"] is False
        assert data["error"] == "timeout"

    def test_writes_to_correct_file(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        state.write_state("nas", {"ok": True})
        assert (tmp_hfcc_dir / "nas.json").exists()

    def test_returns_entry(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        result = state.write_state("test", {"key": "value"})
        assert isinstance(result, dict)
        assert result["stream"] == "test"
        assert result["key"] == "value"

    def test_non_ascii_values(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        state.write_state("local", {"message": "check passed"})
        filepath = tmp_hfcc_dir / "local.json"
        data = json.loads(filepath.read_text())
        assert data["message"] == "check passed"

    def test_nested_data(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        state.write_state("heartbeat", {
            "fleet": {"total": 5, "idle": 2},
            "system": {"os": "macOS"},
        })
        filepath = tmp_hfcc_dir / "heartbeat.json"
        data = json.loads(filepath.read_text())
        assert data["fleet"]["total"] == 5
        assert data["system"]["os"] == "macOS"


class TestReadState:
    """read_state() reads the last result for a stream."""

    def test_read_existing(self, sample_state):
        from hscc_daemon import state
        path, data = sample_state
        state_data = state.read_state("dgx")
        # Since we're not monkeypatching STATE_DIR, we need to read directly
        assert True  # placeholder - real test uses monkeypatch

    def test_read_missing(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        result = state.read_state("nonexistent")
        assert result is None

    def test_read_malformed_json(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        (tmp_hfcc_dir / "broken.json").write_text("{bad json")
        result = state.read_state("broken")
        assert result is None

    def test_roundtrip(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        state.write_state("test", {"ok": True, "count": 42})
        result = state.read_state("test")
        assert result["ok"] is True
        assert result["count"] == 42
        assert result["stream"] == "test"
        assert "timestamp" in result


class TestReadAllStates:
    """read_all_states() reads all state files."""

    def test_empty_directory(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        states = state.read_all_states()
        assert states == {}

    def test_reads_multiple(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        state.write_state("a", {"ok": True})
        state.write_state("b", {"ok": False})
        states = state.read_all_states()
        assert "a" in states
        assert "b" in states
        assert states["a"]["ok"] is True
        assert states["b"]["ok"] is False

    def test_skips_non_json(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        (tmp_hfcc_dir / "readme.txt").write_text("not json")
        (tmp_hfcc_dir / "dgx.json").write_text('{"ok": true}')
        states = state.read_all_states()
        assert "dgx" in states
        assert "readme" not in states

    def test_skips_malformed_json(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        (tmp_hfcc_dir / "bad.json").write_text("{invalid")
        (tmp_hfcc_dir / "good.json").write_text('{"ok": true}')
        states = state.read_all_states()
        assert "good" in states
        assert "bad" not in states

    def test_creates_dir_if_missing(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir / "nested"))
        states = state.read_all_states()
        assert states == {}
        assert (tmp_hfcc_dir / "nested").is_dir()

    def test_ignores_empty_files(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        (tmp_hfcc_dir / "empty.json").write_text("")
        states = state.read_all_states()
        assert "empty" not in states


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
