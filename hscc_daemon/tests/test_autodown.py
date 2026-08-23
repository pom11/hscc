"""Tests for hscc_daemon/autodown.py — Phase 1 (config + core state module).

All tests use tmp paths (monkeypatched AUTODOWN_FILE) and an injected fake
kanban_db — NEVER the real ~/.hscc or ~/.hermes. Everything runs without the
daemon.
"""

import json
import sqlite3
from contextlib import contextmanager

import pytest

import hscc_daemon.autodown as ad


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def autodown_file(tmp_path, monkeypatch):
    """Point AUTODOWN_FILE at a tmp path and return the path."""
    path = tmp_path / "hscc" / "autodown.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ad, "AUTODOWN_FILE", str(path))
    return path


class _FakeKb:
    """Fake Hermes kanban library backed by a real in-memory sqlite board.

    Exposes the ``connect_closing()`` interface _has_active_work uses, so the
    SQL predicate is genuinely exercised. ``conn_closed`` records that the
    connection was released (no leaks).
    """

    def __init__(self, statuses=()):
        self.conn_closed = 0
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)"
        )
        for sid, status in enumerate(statuses):
            self._conn.execute(
                "INSERT INTO tasks (id, status) VALUES (?, ?)",
                (f"t-{sid}", status),
            )
        self._conn.commit()

    @contextmanager
    def connect_closing(self):
        try:
            yield self._conn
        finally:
            # Real Hermes connect_closing closes the connection; we just mark it
            # so tests can assert no leak, then reopen for re-use.
            self.conn_closed += 1


class _UnreachableKb:
    """kanban lib whose connect raises — exercises the fail-safe True path."""

    @contextmanager
    def connect_closing(self):
        raise RuntimeError("DB unreachable")


# ---------------------------------------------------------------------------
# load_config / save_config — round trip + fail-closed
# ---------------------------------------------------------------------------

class TestConfig:
    def test_default_config_shape(self):
        """DEFAULT_CONFIG carries exactly the §7 schema keys, off by default."""
        assert ad.DEFAULT_CONFIG["enabled"] is False
        assert ad.DEFAULT_CONFIG["state"] == "up"
        for key in ("enabled", "idle_minutes", "state", "last_activity_iso",
                    "down_since", "wake_source", "wake_at", "cancel_requested",
                    "reason"):
            assert key in ad.DEFAULT_CONFIG

    def test_round_trip(self, autodown_file):
        """save_config then load_config returns the same fields."""
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["enabled"] = True
        cfg["idle_minutes"] = 15
        cfg["state"] = "down"
        cfg["last_activity_iso"] = "2026-08-23T10:00:00+00:00"
        cfg["down_since"] = "2026-08-23T10:00:00+00:00"
        cfg["wake_source"] = "http"
        cfg["wake_at"] = "2026-08-23T11:00:00+00:00"
        cfg["cancel_requested"] = True
        cfg["reason"] = "operator asked"
        ad.save_config(cfg)
        loaded = ad.load_config()
        assert loaded == cfg

    def test_absent_file_disabled(self, autodown_file):
        """Absent file ⇒ disabled default, never enabled."""
        cfg = ad.load_config()
        assert cfg["enabled"] is False
        assert cfg["state"] == "up"
        assert cfg["idle_minutes"] == 10

    def test_corrupt_json_disabled(self, autodown_file):
        """Corrupt JSON ⇒ disabled default (no crash, not enabled)."""
        autodown_file.write_text("{ this is not json !!!")
        cfg = ad.load_config()
        assert cfg["enabled"] is False
        assert cfg["state"] == "up"

    def test_invalid_top_level_type_disabled(self, autodown_file):
        """A JSON value that is not a dict (e.g. a list) ⇒ disabled."""
        autodown_file.write_text("[1, 2, 3]")
        cfg = ad.load_config()
        assert cfg["enabled"] is False

    def test_partial_file_completes_defaults(self, autodown_file):
        """A file missing some fields still yields a complete config."""
        autodown_file.write_text(json.dumps({"enabled": True}))
        cfg = ad.load_config()
        assert cfg["enabled"] is True
        assert cfg["idle_minutes"] == 10      # default filled
        assert cfg["state"] == "up"           # default filled
        assert cfg["last_activity_iso"] is None

    def test_atomic_write_no_tmp_leftover(self, autodown_file):
        """save_config leaves no stray .tmp file behind."""
        ad.save_config(dict(ad.DEFAULT_CONFIG))
        assert autodown_file.exists()
        assert not (autodown_file.parent / "autodown.json.tmp").exists()


# ---------------------------------------------------------------------------
# record_activity
# ---------------------------------------------------------------------------

class TestRecordActivity:
    def test_advances_timestamp(self, autodown_file):
        """record_activity sets last_activity_iso to a newer value."""
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["last_activity_iso"] = "2000-01-01T00:00:00+00:00"
        ad.save_config(cfg)
        before = cfg["last_activity_iso"]

        ad.record_activity("kanban")
        loaded = ad.load_config()
        assert loaded["last_activity_iso"] != before
        assert loaded["last_activity_iso"] is not None
        assert "T" in loaded["last_activity_iso"]  # ISO 8601
        assert loaded["wake_source"] == "kanban"

    def test_works_when_file_absent(self, autodown_file):
        """record_activity creates the file in the disabled default state."""
        assert not autodown_file.exists()
        ad.record_activity("cli")
        assert autodown_file.exists()
        loaded = ad.load_config()
        # File created, still disabled, but the activity timestamp is set.
        assert loaded["enabled"] is False
        assert loaded["last_activity_iso"] is not None
        assert loaded["wake_source"] == "cli"

    def test_source_recorded(self, autodown_file):
        """The passed source is recorded in wake_source."""
        ad.record_activity("telegram")
        assert ad.load_config()["wake_source"] == "telegram"


# ---------------------------------------------------------------------------
# _has_active_work — kanban idle predicate (§1a)
# ---------------------------------------------------------------------------

LIVE_STATUSES = ["running", "ready", "review", "qa", "in_progress",
                 "todo", "scheduled", "triage", "claimed"]
TERMINAL_STATUSES = ["done", "archived", "blocked"]


class TestHasActiveWork:
    @pytest.mark.parametrize("status", LIVE_STATUSES)
    def test_true_for_each_live_status(self, status):
        """A single card in any live/imminent state ⇒ True (not idle)."""
        kb = _FakeKb([status])
        assert ad._has_active_work(kb) is True

    def test_true_when_mixed_with_terminal(self):
        """A live card alongside terminal ones ⇒ True."""
        kb = _FakeKb(["done", "running", "archived"])
        assert ad._has_active_work(kb) is True

    def test_false_when_board_quiet(self):
        """Only terminal/parked statuses ⇒ False (genuinely idle)."""
        kb = _FakeKb(TERMINAL_STATUSES)
        assert ad._has_active_work(kb) is False

    def test_false_when_empty_board(self):
        """An empty board ⇒ False (idle)."""
        kb = _FakeKb([])
        assert ad._has_active_work(kb) is False

    def test_true_when_db_unreachable(self):
        """Unreachable DB ⇒ True (fail-safe, never consider idle)."""
        assert ad._has_active_work(_UnreachableKb()) is True

    def test_true_when_null_status(self):
        """A task with NULL status counts as active (can't positively clear)."""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO tasks (id, status) VALUES ('t-1', NULL)")
        conn.commit()

        class _Kb:
            @contextmanager
            def connect_closing(self):
                yield conn

        assert ad._has_active_work(_Kb()) is True

    def test_connection_is_closed(self):
        """The injected connection is released after the read."""
        kb = _FakeKb([])
        ad._has_active_work(kb)
        assert kb.conn_closed == 1


# ---------------------------------------------------------------------------
# classify — unit classification table (§5)
# ---------------------------------------------------------------------------

class TestClassify:
    def test_expected_down(self):
        """blocked + intentional autodown + state down ⇒ expected_down."""
        block = {"blocked": True, "intentional": "autodown",
                 "reason": "autodown: intentional idle teardown"}
        state = {"state": "down"}
        assert ad.classify(block, state) == "expected_down"

    def test_should_be_up_when_waking(self):
        """blocked + intentional autodown + state waking ⇒ should_be_up."""
        block = {"blocked": True, "intentional": "autodown"}
        state = {"state": "waking"}
        assert ad.classify(block, state) == "should_be_up"

    def test_should_be_up_when_block_latched_state_up(self):
        """block latched but state not confirmed down ⇒ should_be_up."""
        block = {"blocked": True, "intentional": "autodown"}
        state = {"state": "up"}
        assert ad.classify(block, state) == "should_be_up"

    def test_healthy_no_block(self):
        """No intentional autodown block ⇒ healthy."""
        block = {"blocked": False}
        state = {"state": "up"}
        assert ad.classify(block, state) == "healthy"

    def test_healthy_when_blocked_but_not_intentional(self):
        """A plain watchdog block (not autodown) ⇒ healthy (normal supervision)."""
        block = {"blocked": True, "intentional": None,
                 "reason": "breaker tripped"}
        state = {"state": "up"}
        assert ad.classify(block, state) == "healthy"

    def test_healthy_when_no_autodown_state(self):
        """None/missing inputs ⇒ healthy (never invented down)."""
        assert ad.classify(None, None) == "healthy"
        assert ad.classify({}, {}) == "healthy"


# ---------------------------------------------------------------------------
# Phase 3 — cycle() idle evaluation + safety interlocks (§1, §6)
# ---------------------------------------------------------------------------

import datetime as _dt

NOW = _dt.datetime(2026, 8, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)


class TestCycle:
    """cycle() decision + interlock conjunction tests.

    cycle() is tiny and thin: it guards config/state, then defers the predicate
    to _is_idle and the teardown to _invoke_teardown. So the tests exercise the
    real conjunction (every interlock), the real window math, the real agents
    loader, and the lazy teardown seam — with everything injected off the real
    ~/.hscc / ~/.hermes.
    """

    def _ready(self, autodown_file, idle_minutes=10, age_minutes=15):
        """Write an ENABLED, up config whose window has elapsed (idle-able).

        ``last_activity_iso`` is ``age_minutes`` before NOW so the elapsed
        window is satisfied; each individual test then breaks ONE interlock to
        assert it independently blocks teardown.
        """
        back = NOW - _dt.timedelta(minutes=age_minutes)
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["enabled"] = True
        cfg["state"] = "up"
        cfg["idle_minutes"] = idle_minutes
        cfg["last_activity_iso"] = back.isoformat()
        ad.save_config(cfg)
        return cfg

    def _write_agents(self, tmp_path, agents):
        """Write an agents.json to a tmp path and return it."""
        p = tmp_path / "agents.json"
        p.write_text(json.dumps({"agents": agents}))
        return str(p)

    # --- disabled ⇒ cycle does nothing -----------------------------------
    def test_disabled_does_nothing(self, autodown_file, tmp_path, monkeypatch):
        """enabled:false ⇒ cycle returns immediately, never touches anything."""
        calls = []
        monkeypatch.setattr(ad, "teardown", lambda: calls.append("teardown"),
                 raising=False)
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["enabled"] = False
        cfg["state"] = "up"
        cfg["last_activity_iso"] = "2000-01-01T00:00:00+00:00"  # ancient
        ad.save_config(cfg)

        # Even with every interlock clear, disabled ⇒ no teardown.
        agents = self._write_agents(tmp_path, [{"name": "a", "status": "idle"}])
        ad.cycle(
            kanban_db=_FakeKb([]),
            agents_file=agents,
            now=NOW,
            keepalive_ok=lambda: True,
        )
        assert calls == []

    # --- each interlock INDEPENDENTLY blocks teardown ---------------------
    def test_active_kanban_work_blocks(self, autodown_file, tmp_path, monkeypatch):
        """Active kanban work (running) ⇒ not idle ⇒ no teardown."""
        calls = []
        monkeypatch.setattr(ad, "teardown", lambda: calls.append("teardown"),
                 raising=False)
        self._ready(autodown_file)
        agents = self._write_agents(tmp_path, [{"name": "a", "status": "idle"}])
        ad.cycle(
            kanban_db=_FakeKb(["running"]),  # active work — the broken interlock
            agents_file=agents,
            now=NOW,
            keepalive_ok=lambda: True,
        )
        assert calls == []

    def test_busy_agent_blocks(self, autodown_file, tmp_path, monkeypatch):
        """An enabled agent that is not idle ⇒ no teardown."""
        calls = []
        monkeypatch.setattr(ad, "teardown", lambda: calls.append("teardown"),
                 raising=False)
        self._ready(autodown_file)
        agents = self._write_agents(
            tmp_path, [{"name": "a", "status": "idle"},
                       {"name": "b", "status": "working"}])  # broken interlock
        ad.cycle(
            kanban_db=_FakeKb([]),
            agents_file=agents,
            now=NOW,
            keepalive_ok=lambda: True,
        )
        assert calls == []

    def test_window_not_elapsed_blocks(self, autodown_file, tmp_path, monkeypatch):
        """now - last_activity_iso < idle_minutes ⇒ no teardown."""
        calls = []
        monkeypatch.setattr(ad, "teardown", lambda: calls.append("teardown"),
                 raising=False)
        self._ready(autodown_file, idle_minutes=10, age_minutes=5)  # only 5m < 10m
        agents = self._write_agents(tmp_path, [{"name": "a", "status": "idle"}])
        ad.cycle(
            kanban_db=_FakeKb([]),
            agents_file=agents,
            now=NOW,
            keepalive_ok=lambda: True,
        )
        assert calls == []

    def test_unhealthy_keepalive_blocks(self, autodown_file, tmp_path, monkeypatch):
        """Unhealthy keepalive unit ⇒ abort, no teardown."""
        calls = []
        monkeypatch.setattr(ad, "teardown", lambda: calls.append("teardown"),
                 raising=False)
        self._ready(autodown_file)
        agents = self._write_agents(tmp_path, [{"name": "a", "status": "idle"}])
        ad.cycle(
            kanban_db=_FakeKb([]),
            agents_file=agents,
            now=NOW,
            keepalive_ok=lambda: False,  # broken interlock
        )
        assert calls == []

    # --- conjunction: all-clear ⇒ teardown exactly once -------------------
    def test_all_clear_tears_down_exactly_once(
            self, autodown_file, tmp_path, monkeypatch):
        """Every interlock clear ⇒ teardown invoked exactly once."""
        calls = []
        monkeypatch.setattr(ad, "teardown", lambda: calls.append("teardown"),
                 raising=False)
        self._ready(autodown_file)
        agents = self._write_agents(tmp_path, [{"name": "a", "status": "idle"}])
        ad.cycle(
            kanban_db=_FakeKb([]),
            agents_file=agents,
            now=NOW,
            keepalive_ok=lambda: True,
        )
        assert calls == ["teardown"]

    # --- warm-up / first-boot guard ---------------------------------------
    def test_null_last_activity_does_not_teardown(
            self, autodown_file, tmp_path, monkeypatch):
        """NULL last_activity_iso ⇒ treated as activity just now, so no teardown.

        Also asserts the warm-up guard STAMPS the timestamp so the next window
        is measured from "now", per §1e.
        """
        calls = []
        monkeypatch.setattr(ad, "teardown", lambda: calls.append("teardown"),
                 raising=False)
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["enabled"] = True
        cfg["state"] = "up"
        cfg["idle_minutes"] = 10
        cfg["last_activity_iso"] = None  # empty — the warm-up case
        ad.save_config(cfg)
        agents = self._write_agents(tmp_path, [{"name": "a", "status": "idle"}])

        ad.cycle(
            kanban_db=_FakeKb([]),
            agents_file=agents,
            now=NOW,
            keepalive_ok=lambda: True,
        )
        assert calls == []
        # Warm-up guard stamped last_activity_iso with "now" (our injected NOW).
        assert ad.load_config()["last_activity_iso"] == NOW.isoformat()

    # --- unreadable agents.json ⇒ fail-safe -------------------------------
    def test_unreadable_agents_does_not_teardown(
            self, autodown_file, tmp_path, monkeypatch):
        """Missing/unreadable agents.json ⇒ NOT idle ⇒ no teardown."""
        calls = []
        monkeypatch.setattr(ad, "teardown", lambda: calls.append("teardown"),
                 raising=False)
        self._ready(autodown_file)
        missing = str(tmp_path / "no-such-agents.json")  # does not exist
        ad.cycle(
            kanban_db=_FakeKb([]),
            agents_file=missing,
            now=NOW,
            keepalive_ok=lambda: True,
        )
        assert calls == []

    # --- state down/waking ⇒ returns without teardown ---------------------
    @pytest.mark.parametrize("state", ["down", "waking"])
    def test_down_or_waking_returns_without_teardown(
            self, state, autodown_file, tmp_path, monkeypatch):
        """state down/waking ⇒ Phase 3 does NOT handle wake ⇒ no teardown."""
        calls = []
        monkeypatch.setattr(ad, "teardown", lambda: calls.append("teardown"),
                 raising=False)
        cfg = self._ready(autodown_file)
        cfg["state"] = state
        ad.save_config(cfg)
        agents = self._write_agents(tmp_path, [{"name": "a", "status": "idle"}])
        # Even fully idle, down/waking never triggers teardown this phase.
        ad.cycle(
            kanban_db=_FakeKb([]),
            agents_file=agents,
            now=NOW,
            keepalive_ok=lambda: True,
        )
        assert calls == []

