"""Unit tests for stats.py — fleet analytics aggregation.

Tests are fully isolated: file I/O uses tmp_path, no external dependencies.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hscc_daemon.stats import compute_stats, format_stats


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _ago_iso(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class TestComputeStatsMissingFiles:
    """Missing files produce zeroed stats, never raise."""

    def test_no_files(self, tmp_path):
        stats = compute_stats(hscc_dir=str(tmp_path))
        assert stats["since_days"] == 7
        assert stats["completions"]["total"] == 0
        assert stats["completions"]["by_profile"] == {}
        assert stats["completions"]["by_day"] == {}
        assert stats["activity"]["tool_calls_by_profile"] == {}
        assert stats["activity"]["top_tools"] == []

    def test_only_completions_file(self, tmp_path):
        path = tmp_path / "task_completions.jsonl"
        path.write_text(json.dumps({"task_id": "t1", "profile_name": "x",
                                    "summary": "ok", "board": "b",
                                    "completed_at": _now_iso()}) + "\n")
        stats = compute_stats(hscc_dir=str(tmp_path))
        assert stats["completions"]["total"] == 1
        assert stats["activity"]["top_tools"] == []

    def test_only_events_file(self, tmp_path):
        path = tmp_path / "tool_events.jsonl"
        path.write_text(json.dumps({"event": "call", "profile_name": "x",
                                    "tool_name": "read", "timestamp": _now_iso()}) + "\n")
        stats = compute_stats(hscc_dir=str(tmp_path))
        assert stats["completions"]["total"] == 0
        assert stats["activity"]["tool_calls_by_profile"]["x"] == 1


class TestComputeStatsCompletions:
    """task_completions.jsonl aggregation."""

    def _write(self, tmp_path, records):
        path = tmp_path / "task_completions.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        return path

    def test_total_count(self, tmp_path):
        records = [
            {"task_id": f"t{i}", "profile_name": "eng",
             "summary": "ok", "board": "main",
             "completed_at": _now_iso()}
            for i in range(5)
        ]
        self._write(tmp_path, records)
        stats = compute_stats(hscc_dir=str(tmp_path))
        assert stats["completions"]["total"] == 5

    def test_by_profile(self, tmp_path):
        records = [
            {"task_id": "t1", "profile_name": "eng",
             "summary": "ok", "board": "main", "completed_at": _now_iso()},
            {"task_id": "t2", "profile_name": "eng",
             "summary": "ok", "board": "main", "completed_at": _now_iso()},
            {"task_id": "t3", "profile_name": "writer",
             "summary": "ok", "board": "main", "completed_at": _now_iso()},
        ]
        self._write(tmp_path, records)
        stats = compute_stats(hscc_dir=str(tmp_path))
        assert stats["completions"]["by_profile"] == {"eng": 2, "writer": 1}

    def test_by_day(self, tmp_path):
        day0 = datetime.now(timezone.utc)
        day1 = day0 - timedelta(days=1)
        records = [
            {"task_id": "t1", "profile_name": "eng",
             "summary": "ok", "board": "main", "completed_at": day0.isoformat()},
            {"task_id": "t2", "profile_name": "eng",
             "summary": "ok", "board": "main", "completed_at": day1.isoformat()},
        ]
        self._write(tmp_path, records)
        stats = compute_stats(hscc_dir=str(tmp_path))
        assert day0.strftime("%Y-%m-%d") in stats["completions"]["by_day"]
        assert day1.strftime("%Y-%m-%d") in stats["completions"]["by_day"]

    def test_old_records_excluded(self, tmp_path):
        records = [
            {"task_id": "t_old", "profile_name": "eng",
             "summary": "ok", "board": "main", "completed_at": _ago_iso(30)},
            {"task_id": "t_new", "profile_name": "eng",
             "summary": "ok", "board": "main", "completed_at": _now_iso()},
        ]
        self._write(tmp_path, records)
        stats = compute_stats(hscc_dir=str(tmp_path))
        assert stats["completions"]["total"] == 1

    def test_malformed_line_skipped(self, tmp_path):
        path = tmp_path / "task_completions.jsonl"
        content = (
            json.dumps({"task_id": "t1", "profile_name": "eng",
                        "summary": "ok", "board": "main",
                        "completed_at": _now_iso()}) + "\n"
            + "this is not json\n"
            + json.dumps({"task_id": "t2", "profile_name": "eng",
                          "summary": "ok", "board": "main",
                          "completed_at": _now_iso()}) + "\n"
        )
        path.write_text(content)
        stats = compute_stats(hscc_dir=str(tmp_path))
        assert stats["completions"]["total"] == 2

    def test_null_profile_ignored_in_by_profile(self, tmp_path):
        records = [
            {"task_id": "t1", "profile_name": None,
             "summary": "ok", "board": "main", "completed_at": _now_iso()},
            {"task_id": "t2", "profile_name": "eng",
             "summary": "ok", "board": "main", "completed_at": _now_iso()},
        ]
        self._write(tmp_path, records)
        stats = compute_stats(hscc_dir=str(tmp_path))
        assert stats["completions"]["total"] == 2
        assert "eng" in stats["completions"]["by_profile"]
        assert stats["completions"]["by_profile"]["eng"] == 1

    def test_bad_timestamp_skipped(self, tmp_path):
        records = [
            {"task_id": "t1", "profile_name": "eng",
             "summary": "ok", "board": "main", "completed_at": "not-a-date"},
            {"task_id": "t2", "profile_name": "eng",
             "summary": "ok", "board": "main", "completed_at": _now_iso()},
        ]
        self._write(tmp_path, records)
        stats = compute_stats(hscc_dir=str(tmp_path))
        assert stats["completions"]["total"] == 1

    def test_trailing_z_accepted(self, tmp_path):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        records = [
            {"task_id": "t1", "profile_name": "eng",
             "summary": "ok", "board": "main", "completed_at": ts},
        ]
        self._write(tmp_path, records)
        stats = compute_stats(hscc_dir=str(tmp_path))
        assert stats["completions"]["total"] == 1


class TestComputeStatsToolEvents:
    """tool_events.jsonl aggregation."""

    def _write(self, tmp_path, records):
        path = tmp_path / "tool_events.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        return path

    def test_tool_calls_by_profile(self, tmp_path):
        records = [
            {"event": "call", "profile_name": "eng",
             "tool_name": "read_file", "timestamp": _now_iso()},
            {"event": "call", "profile_name": "eng",
             "tool_name": "terminal", "timestamp": _now_iso()},
            {"event": "call", "profile_name": "writer",
             "tool_name": "read_file", "timestamp": _now_iso()},
        ]
        self._write(tmp_path, records)
        stats = compute_stats(hscc_dir=str(tmp_path))
        assert stats["activity"]["tool_calls_by_profile"] == {"eng": 2, "writer": 1}

    def test_top_tools(self, tmp_path):
        records = []
        for i in range(5):
            records.append({"event": "call", "profile_name": "eng",
                            "tool_name": "read_file", "timestamp": _now_iso()})
        for i in range(3):
            records.append({"event": "call", "profile_name": "eng",
                            "tool_name": "terminal", "timestamp": _now_iso()})
        records.append({"event": "call", "profile_name": "eng",
                        "tool_name": "write_file", "timestamp": _now_iso()})
        self._write(tmp_path, records)
        stats = compute_stats(hscc_dir=str(tmp_path))
        assert stats["activity"]["top_tools"][0] == ["read_file", 5]
        assert stats["activity"]["top_tools"][1] == ["terminal", 3]

    def test_top_tools_max_10(self, tmp_path):
        records = []
        for i in range(20):
            records.append({"event": "call", "profile_name": "eng",
                            "tool_name": f"tool_{i}", "timestamp": _now_iso()})
        self._write(tmp_path, records)
        stats = compute_stats(hscc_dir=str(tmp_path))
        assert len(stats["activity"]["top_tools"]) == 10

    def test_old_events_excluded(self, tmp_path):
        records = [
            {"event": "call", "profile_name": "eng",
             "tool_name": "read", "timestamp": _ago_iso(30)},
            {"event": "call", "profile_name": "eng",
             "tool_name": "read", "timestamp": _now_iso()},
        ]
        self._write(tmp_path, records)
        stats = compute_stats(hscc_dir=str(tmp_path))
        assert stats["activity"]["tool_calls_by_profile"]["eng"] == 1

    def test_malformed_line_skipped(self, tmp_path):
        path = tmp_path / "tool_events.jsonl"
        content = (
            json.dumps({"event": "call", "profile_name": "eng",
                        "tool_name": "read", "timestamp": _now_iso()}) + "\n"
            + "bad line here\n"
            + json.dumps({"event": "call", "profile_name": "eng",
                          "tool_name": "read", "timestamp": _now_iso()}) + "\n"
        )
        path.write_text(content)
        stats = compute_stats(hscc_dir=str(tmp_path))
        assert stats["activity"]["tool_calls_by_profile"]["eng"] == 2


class TestFormatStats:
    """format_stats produces readable text."""

    def test_nonempty(self):
        stats = {
            "since_days": 7,
            "completions": {
                "total": 10,
                "by_profile": {"eng": 7, "writer": 3},
                "by_day": {"2026-07-20": 4, "2026-07-21": 6},
            },
            "activity": {
                "tool_calls_by_profile": {"eng": 50, "writer": 20},
                "top_tools": [["read_file", 30], ["terminal", 25]],
            },
        }
        text = format_stats(stats)
        assert "Fleet stats" in text
        assert "Completions: 10" in text
        assert "eng: 7" in text
        assert "read_file: 30" in text
        assert "2026-07-20" in text

    def test_empty_stats(self):
        stats = {
            "since_days": 7,
            "completions": {"total": 0, "by_profile": {}, "by_day": {}},
            "activity": {"tool_calls_by_profile": {}, "top_tools": []},
        }
        text = format_stats(stats)
        assert "Completions: 0" in text
        assert "Top tools:" not in text

    def test_returns_string(self):
        stats = {
            "since_days": 1,
            "completions": {"total": 1, "by_profile": {}, "by_day": {}},
            "activity": {"tool_calls_by_profile": {}, "top_tools": []},
        }
        assert isinstance(format_stats(stats), str)
