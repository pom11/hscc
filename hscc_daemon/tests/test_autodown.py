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


# ---------------------------------------------------------------------------
# Phase 4 — teardown sequence + watchdog block coordination (§3, §5)
# ---------------------------------------------------------------------------

import hscc_daemon.lifecycle as _lifecycle


def _write_serving(tmp_path):
    """Write a 3-unit serving.json fixture and return its path.

    Units:
      - orchestrator unit "orch": nodes [.244, .246], port 8000
      - NON-keepalive worker "wk1": nodes [.247], port 8000  (teardown target)
      - KEEPALIVE worker "wk-keep": nodes [.248], port 8000  (C4 EXEMPT)
    Top-level port 8000 (for the serving_port fallback path).
    """
    data = {
        "port": 8000,
        "units": [
            {"id": "orch", "role": "orchestrator",
             "nodes": ["10.0.0.244", "10.0.0.246"], "port": 8000},
            {"id": "wk1", "role": "worker", "keepalive": False,
             "nodes": ["10.0.0.247"], "port": 8000},
            {"id": "wk-keep", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.248"], "port": 8000},
        ],
    }
    path = tmp_path / "serving.json"
    path.write_text(json.dumps(data))
    return str(path)


class _FakeRunner:
    """Fake sparkrun command runner that records every call + the block file.

    On each ``__call__`` it snapshots the watchdog block file (the moment the
    stop was issued) so a test can assert the block was written BEFORE every
    stop. ``results`` is an optional list of per-call ``ok`` values (defaults to
    True); extra calls beyond ``results`` default to True.
    """

    def __init__(self, block_file, results=None):
        self.block_file = block_file
        self.results = list(results or [])
        self.calls = []
        self._i = 0

    def __call__(self, cmd, timeout=30):
        block = None
        try:
            with open(self.block_file) as f:
                block = json.load(f)
        except Exception:
            block = None
        ok = self.results[self._i] if self._i < len(self.results) else True
        self._i += 1
        self.calls.append({"cmd": list(cmd), "block": block, "ok": ok})
        return {"ok": ok, "output": "" if ok else "stop command failed"}


def _write_idle_cfg(autodown_file, cancel=False):
    """Write an enabled, up, idle-window-elapsed config (teardown-able)."""
    back = NOW - _dt.timedelta(minutes=15)
    cfg = dict(ad.DEFAULT_CONFIG)
    cfg["enabled"] = True
    cfg["state"] = "up"
    cfg["idle_minutes"] = 10
    cfg["last_activity_iso"] = back.isoformat()
    cfg["cancel_requested"] = cancel
    ad.save_config(cfg)
    return cfg


class TestTeardown:
    """teardown() with injected fakes — ZERO real sparkrun commands.

    Every test injects a fake command runner and a fixture serving.json; the
    watchdog block file and autodown.json are monkeypatched to tmp paths and
    the notifiers are stubbed, so NOTHING touches the live cluster.
    """

    def _setup(self, tmp_path, monkeypatch, autodown_file, results=None):
        """Common wiring: block file, serving fixture, idle config, stub notifiers.

        Returns (serving_path, runner, block_file).
        """
        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        serving = _write_serving(tmp_path)
        _write_idle_cfg(autodown_file)
        runner = _FakeRunner(block_file, results=results)
        # Stub notifiers so no notification is actually attempted.
        monkeypatch.setattr(ad, "notify_operations", lambda *a, **k: True)
        monkeypatch.setattr(ad, "send_macos_notification", lambda *a, **k: True)
        return serving, runner, block_file

    def _agents(self, tmp_path):
        p = tmp_path / "agents.json"
        p.write_text(json.dumps({"agents": [{"name": "a", "status": "idle"}]}))
        return str(p)

    # -- abort when re-verify finds work (no stop issued at all) ----------
    def test_abort_when_reverify_finds_work(self, tmp_path, monkeypatch,
                                            autodown_file):
        """Work arrived after the timer decided ⇒ ABORT, NO stops issued."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        # Idle predicate breaks because kanban now has a running card.
        res = ad.teardown(
            serving_path=serving, run_cmd_fn=runner,
            kanban_db=_FakeKb(["running"]),   # the changed signal
            agents_file=self._agents(tmp_path),
            now=NOW, keepalive_ok=lambda: True,
        )
        assert res["result"] == "aborted"
        assert runner.calls == []        # no stop issued at all
        assert res["issued"] == []
        # No block written on abort (re-verify runs before the block write,
        # so on a failed re-verify the block file is never even created).
        import os as _os
        assert not _os.path.exists(block_file)

    def test_abort_when_busy_agent(self, tmp_path, monkeypatch, autodown_file):
        """Agent busy during re-verify ⇒ ABORT, no stops issued."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        res = ad.teardown(
            serving_path=serving, run_cmd_fn=runner,
            kanban_db=_FakeKb([]),
            agents_file=self._agents(tmp_path),
            now=NOW, keepalive_ok=lambda: False,  # keepalive went sick
        )
        assert res["result"] == "aborted"
        assert runner.calls == []

    # -- block written BEFORE any stop (explicit call ordering) ------------
    def test_block_written_before_any_stop(self, tmp_path, monkeypatch,
                                           autodown_file):
        """The watchdog block is on disk (intentional) before EVERY stop.

        The fake runner snapshots the block file at each stop; a valid teardown
        must have the intentional autodown block present for every single one.
        """
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        res = ad.teardown(
            serving_path=serving, run_cmd_fn=runner,
            kanban_db=_FakeKb([]), agents_file=self._agents(tmp_path),
            now=NOW, keepalive_ok=lambda: True,
            http_check_fn=lambda *a, **k: {"ok": False},  # ports down
        )
        assert res["result"] == "down"
        assert len(runner.calls) == 2      # wk1 + orch
        for call in runner.calls:
            blk = call["block"]
            assert blk is not None
            assert blk.get("blocked") is True
            assert blk.get("intentional") == "autodown"
            assert blk.get("reason") == ad.WATCHDOG_TEARDOWN_REASON
        # Explicit: the block (with intentional) was saved before the first stop.
        assert runner.calls[0]["block"]["intentional"] == "autodown"

    # -- keepalive units NEVER appear in issued stop commands ---------------
    def test_keepalive_never_in_stop_commands(self, tmp_path, monkeypatch,
                                              autodown_file):
        """The keepalive unit's nodes (.248) are never in any stop command."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        res = ad.teardown(
            serving_path=serving, run_cmd_fn=runner,
            kanban_db=_FakeKb([]), agents_file=self._agents(tmp_path),
            now=NOW, keepalive_ok=lambda: True,
            http_check_fn=lambda *a, **k: {"ok": False},
        )
        assert res["result"] == "down"
        all_hosts = []
        for call in runner.calls:
            all_hosts += call["cmd"]
        joined = " ".join(all_hosts)
        assert "10.0.0.248" not in joined     # keepalive node excluded
        assert "10.0.0.247" in joined          # non-keepalive worker stopped
        # Plan (teardown set) never contains the keepalive unit id.
        plan_ids = {e["unit_id"] for e in res["plan"]}
        assert "wk-keep" not in plan_ids
        assert {"wk1", "orch"} == plan_ids

    # -- orchestrator stopped LAST -----------------------------------------
    def test_orchestrator_stopped_last(self, tmp_path, monkeypatch,
                                       autodown_file):
        """Workers (non-keepalive) stop before the orchestrator unit."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        res = ad.teardown(
            serving_path=serving, run_cmd_fn=runner,
            kanban_db=_FakeKb([]), agents_file=self._agents(tmp_path),
            now=NOW, keepalive_ok=lambda: True,
            http_check_fn=lambda *a, **k: {"ok": False},
        )
        kinds = [call["cmd"] for call in runner.calls]
        assert res["result"] == "down"
        assert kinds[0][-1] == "10.0.0.247"        # worker first
        assert "10.0.0.244" in kinds[-1][-1]       # orchestrator last
        assert res["issued"][0]["kind"] == "worker"
        assert res["issued"][-1]["kind"] == "orchestrator"

    # -- stop failure ⇒ block rolled back + failure recorded ----------------
    def test_stop_failure_rolls_back_and_records(self, tmp_path, monkeypatch,
                                                 autodown_file):
        """A failed stop ⇒ no latched block, failure recorded, not state down."""
        # Make the FIRST stop (worker) fail.
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file,
                                                  results=[False])
        res = ad.teardown(
            serving_path=serving, run_cmd_fn=runner,
            kanban_db=_FakeKb([]), agents_file=self._agents(tmp_path),
            now=NOW, keepalive_ok=lambda: True,
            http_check_fn=lambda *a, **k: {"ok": False},
        )
        assert res["result"] == "failed"
        # Block rolled back → intentional removed, blocked back to False.
        with open(block_file) as f:
            rolled = json.load(f)
        assert rolled.get("intentional") is None
        assert rolled.get("blocked") is False
        # Failure recorded in autodown.json: state up (reality), reason set.
        cfg = ad.load_config()
        assert cfg["state"] == "up"
        assert "failed" in cfg["reason"]
        assert cfg["down_since"] is None
        # Notified.
        # Stop issued for the failed worker only, orchestrator never attempted.
        assert len(runner.calls) == 1

    def test_stop_failure_recorded(self, tmp_path, monkeypatch, autodown_file,
                                   capsys):
        """Failure path persists a reason mentioning the failure into config."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file,
                                                  results=[True, False])
        # First stop ok, second (orchestrator) fails.
        res = ad.teardown(
            serving_path=serving, run_cmd_fn=runner,
            kanban_db=_FakeKb([]), agents_file=self._agents(tmp_path),
            now=NOW, keepalive_ok=lambda: True,
            http_check_fn=lambda *a, **k: {"ok": False},
        )
        assert res["result"] == "failed"
        cfg = ad.load_config()
        assert cfg["state"] == "up"
        assert "teardown failed" in cfg["reason"]

    # -- cancel_requested mid-teardown ⇒ stops, rolls back, cancelled -------
    def test_cancel_mid_teardown(self, tmp_path, monkeypatch, autodown_file):
        """cancel_requested before a stop ⇒ stop issuing, roll block back,
        report cancelled."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        # cancel_requested set on disk so the loop's pre-stop check sees it.
        cfg = ad.load_config()
        cfg["cancel_requested"] = True
        ad.save_config(cfg)
        res = ad.teardown(
            serving_path=serving, run_cmd_fn=runner,
            kanban_db=_FakeKb([]), agents_file=self._agents(tmp_path),
            now=NOW, keepalive_ok=lambda: True,
            http_check_fn=lambda *a, **k: {"ok": False},
        )
        assert res["result"] == "cancelled"
        # No stop was issued — cancel was set before the first stop check.
        assert runner.calls == []
        # Block rolled back → intentional removed.
        with open(block_file) as f:
            rolled = json.load(f)
        assert rolled.get("intentional") is None
        # State reflects reality (not down), reason recorded, cancel persisted.
        cfg = ad.load_config()
        assert cfg["state"] == "up"
        assert "cancelled" in cfg["reason"]

    def test_cancel_after_first_stop(self, tmp_path, monkeypatch, autodown_file):
        """Cancel set between stops ⇒ first stop issued, subsequent not."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        # After the FIRST stop issues (via runner), set cancel on disk.
        class _RunnerWithCancel:
            def __init__(self, inner, set_cancel):
                self.inner = inner
                self.set_cancel = set_cancel
            def __call__(self, cmd, timeout=30):
                out = self.inner(cmd, timeout=timeout)
                self.set_cancel()   # set cancel_requested AFTER this stop
                return out

        def set_cancel():
            c = ad.load_config()
            c["cancel_requested"] = True
            ad.save_config(c)

        r2 = _RunnerWithCancel(runner, set_cancel)
        res = ad.teardown(
            serving_path=serving, run_cmd_fn=r2,
            kanban_db=_FakeKb([]), agents_file=self._agents(tmp_path),
            now=NOW, keepalive_ok=lambda: True,
            http_check_fn=lambda *a, **k: {"ok": False},
        )
        assert res["result"] == "cancelled"
        # Only the worker (first) was stopped; orchestrator never attempted.
        assert len(runner.calls) == 1
        assert runner.calls[0]["cmd"][-1] == "10.0.0.247"
        # Block rolled back.
        with open(block_file) as f:
            rolled = json.load(f)
        assert rolled.get("intentional") is None

    # -- success ⇒ autodown.json state == "down" with down_since set -------
    def test_success_sets_state_down(self, tmp_path, monkeypatch, autodown_file):
        """A clean teardown writes state=down + down_since + reason, and the
        intended watchdog block stays latched (intentional)."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        res = ad.teardown(
            serving_path=serving, run_cmd_fn=runner,
            kanban_db=_FakeKb([]), agents_file=self._agents(tmp_path),
            now=NOW, keepalive_ok=lambda: True,
            http_check_fn=lambda *a, **k: {"ok": False},
        )
        assert res["result"] == "down"
        cfg = ad.load_config()
        assert cfg["state"] == "down"
        assert cfg["down_since"] is not None
        assert cfg["reason"] == "autodown: intentional idle teardown"
        # No intentional field duplicated into autodown.json (one source of
        # truth per fact — it lives in the watchdog block only, §3.5).
        assert "intentional" not in cfg
        # The watchdog block remains latched with intentional autodown.
        with open(block_file) as f:
            blk = json.load(f)
        assert blk.get("blocked") is True
        assert blk.get("intentional") == "autodown"

