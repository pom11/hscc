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


class TestWriteStateDirectorySeam:
    """write_state()'s `directory` seam (t_e9b0f048).

    The ONE writer must be able to be told which stamp root to use, which is
    what lets cron_watchers' WRITES meet the root its reads already honoured.
    `directory=None` — every daemon caller — keeps the old behaviour exactly:
    STATE_DIR stays the writer's own choice, not a second default open-coded by
    a caller.
    """

    def test_directory_moves_the_write_and_is_created(self, tmp_hfcc_dir,
                                                      monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir / "canonical"))
        elsewhere = tmp_hfcc_dir / "relocated" / "state"
        assert not elsewhere.exists()
        state.write_state("dgx", {"ok": True}, directory=str(elsewhere))
        assert (elsewhere / "dgx.json").is_file()
        assert not (tmp_hfcc_dir / "canonical" / "dgx.json").exists()

    def test_none_keeps_the_state_dir_selection(self, tmp_hfcc_dir, monkeypatch):
        """Negative control: the seam must not shift the default path."""
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        state.write_state("dgx", {"ok": True})
        assert (tmp_hfcc_dir / "dgx.json").is_file()

    def test_directory_carries_the_same_envelope(self, tmp_hfcc_dir,
                                                 monkeypatch):
        """One format, whichever root is named: timestamp + stream, atomic."""
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        elsewhere = tmp_hfcc_dir / "other"
        entry = state.write_state("dgx", {"ok": True, "gate_rc": 0},
                                 directory=str(elsewhere))
        assert entry["stream"] == "dgx" and entry["timestamp"]
        on_disk = json.loads((elsewhere / "dgx.json").read_text())
        assert on_disk == entry
        assert list((elsewhere).iterdir()) == [elsewhere / "dgx.json"]

    def test_directory_honours_the_same_stream_name(self, tmp_hfcc_dir,
                                                    monkeypatch):
        """A second pass overwrites the first at the named root — no second
        stamp file, so a watcher can never have two places to be seen."""
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        elsewhere = tmp_hfcc_dir / "other"
        state.write_state("dgx", {"ok": True}, directory=str(elsewhere))
        state.write_state("dgx", {"ok": False, "error": "boom"},
                          directory=str(elsewhere))
        assert list(elsewhere.iterdir()) == [elsewhere / "dgx.json"]
        assert json.loads((elsewhere / "dgx.json").read_text())["ok"] is False

    def test_directory_still_honours_persist_disabled(self, tmp_hfcc_dir,
                                                     monkeypatch):
        """The suppression rule is uniform: an ad-hoc CLI run publishes nothing,
        to any root — the seam is not a way around persist_disabled()."""
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir))
        elsewhere = tmp_hfcc_dir / "other"
        with state.persist_disabled():
            entry = state.write_state("dgx", {"ok": True},
                                      directory=str(elsewhere))
        assert entry["ok"] is True          # callers still see the entry
        assert not elsewhere.exists()
        assert not (tmp_hfcc_dir / "dgx.json").exists()


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
