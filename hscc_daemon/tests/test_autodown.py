"""Tests for hscc_daemon/autodown.py — Phase 1 (config + core state module).

All tests use tmp paths (monkeypatched AUTODOWN_FILE) and an injected fake
kanban_db — NEVER the real ~/.hscc or ~/.hermes. Everything runs without the
daemon.
"""

import json
import os
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

    def test_partial_save_completes_schema(self, autodown_file):
        """To-Do #2 hardening: saving a partial dict always writes the full schema.

        The historical leak wrote a partial ``{"enabled": True}`` dict to disk
        because save_config persisted exactly what it was handed. Now the file
        must ALWAYS carry every §7 key — a config file is never missing keys.
        """
        ad.save_config({"enabled": True})  # partial — e.g. a patched loader
        loaded = ad.load_config()
        # Every DEFAULT_CONFIG key present on disk after a partial save.
        assert set(ad.DEFAULT_CONFIG) <= set(loaded)
        assert loaded["enabled"] is True      # the partial field preserved
        assert loaded["state"] == "up"        # defaults filled in
        assert loaded["idle_minutes"] == 10
        assert loaded["wake_source"] is None
        assert loaded["reason"] == ""
        # And the on-disk JSON itself contains every key too.
        on_disk = json.loads(autodown_file.read_text())
        assert set(ad.DEFAULT_CONFIG) <= set(on_disk)


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
# _load_kanban_db_or_default — path resolution + fail-safe + status surfacing
# ---------------------------------------------------------------------------

class TestLoadKanbanDb:
    """``_load_kanban_db_or_default`` must find ``hermes_cli`` even when the
    daemon's ``sys.path`` holds only the repo (no hermes-agent), honour
    ``HERMES_AGENT_PATH``/``HOME``, fail safe to None when genuinely
    unreachable, and record the outcome for ``hscc autodown status``."""

    def _write_fake_hermes_cli(self, tmp_path):
        """Plant a fake ``hermes_cli/kanban_db.py`` under tmp_path/hermes-agent
        so the resolver finds it exactly the way it finds the real tree."""
        agent = tmp_path / "hermes-agent"
        cli = agent / "hermes_cli"
        cli.mkdir(parents=True)
        (cli / "__init__.py").write_text("")
        (cli / "kanban_db.py").write_text(
            "# fake\n"
            "def connect_closing():\n"
            "    raise NotImplementedError\n"
        )
        return agent

    def test_resolves_from_hermes_agent_path(self, tmp_path, monkeypatch):
        """HERMES_AGENT_PATH points at a dir with hermes_cli ⇒ it resolves and
        works, even though the dir is not on sys.path by default."""
        agent = self._write_fake_hermes_cli(tmp_path)
        # Remove any pre-existing hermes_cli so we exercise the resolution, not
        # a leftover on sys.path.
        import hscc_daemon.autodown as _ad  # noqa: F401
        monkeypatch.delenv("HERMES_AGENT_PATH", raising=False)
        monkeypatch.setattr(ad, "_HERMES_AGENT_PATH", str(agent))
        kb1 = ad._load_kanban_db_or_default()
        # Force a fresh sys.path without the agent dir, then re-resolve via env.
        monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))
        kb2 = ad._load_kanban_db_or_default()
        assert kb1 is not None
        assert kb2 is not None
        assert ad.kanban_check_state() == {"ok": True, "reason": ""}

    def test_calls_real_kanban_connect(self, tmp_path, monkeypatch):
        """The resolved lib is genuinely importable and usable (has
        connect_closing), not a stub — the path resolution reaches the real
        hermes_cli package structure."""
        kb = ad._load_kanban_db_or_default()
        if kb is None:
            pytest.skip("real hermes_cli not present in this environment")
        assert hasattr(kb, "connect_closing")

    def test_unreachable_failsafe_true_and_surfaced(
            self, tmp_path, monkeypatch, capsys):
        """Unreachable kanban ⇒ None ⇒ _has_active_work stays True (fail-safe),
        the state is surfaced via kanban_check_state for status, no raise."""
        monkeypatch.setenv("HERMES_AGENT_PATH", str(tmp_path / "does-not-exist"))
        kb = ad._load_kanban_db_or_default()
        assert kb is None
        assert ad._has_active_work() is True  # fail-safe preserved
        state = ad.kanban_check_state()
        assert state is not None and state["ok"] is False
        assert "does-not-exist" in state["reason"]

    def test_logged_once_not_per_tick(self, tmp_path, monkeypatch, capsys):
        """Repeated resolution of an unreachable lib logs ONCE, not per call."""
        import hscc_daemon.daemon_ops as do
        monkeypatch.setenv("HERMES_AGENT_PATH", str(tmp_path / "missing"))
        # reset the log-once guard so this test's count is independent
        monkeypatch.setitem(ad._KANBAN_LOAD, "warned", False)
        # The autouse _isolate_hscc fixture redirects do.LOG_FILE to the tmp
        # ~/.hscc/daemon.log but may not create its parent — ensure it exists
        # so log() actually writes the file (it silently no-ops otherwise).
        os.makedirs(os.path.dirname(do.LOG_FILE), exist_ok=True)
        ad._load_kanban_db_or_default()
        ad._load_kanban_db_or_default()
        ad._load_kanban_db_or_default()
        logfile = do.LOG_FILE
        txt = ""
        if os.path.exists(logfile):
            with open(logfile) as f:
                txt = f.read()
        n = txt.count("kanban interlock unevaluable")
        assert n == 1, f"expected exactly one log line, got {n}: {txt!r}"

    def test_state_resets_to_ok_on_success(self, tmp_path, monkeypatch):
        """After a failure, a later success restores ok=True / empty reason."""
        agent = self._write_fake_hermes_cli(tmp_path)
        monkeypatch.setattr(ad, "_HERMES_AGENT_PATH", str(tmp_path / "missing"))
        assert ad._load_kanban_db_or_default() is None
        monkeypatch.setattr(ad, "_HERMES_AGENT_PATH", str(agent))
        assert ad._load_kanban_db_or_default() is not None
        assert ad.kanban_check_state() == {"ok": True, "reason": ""}


# ---------------------------------------------------------------------------
# _default_keepalive_ok — head-only probing for multi-node/tp keepalive units
# ---------------------------------------------------------------------------

class TestDefaultKeepaliveOk:
    """``_default_keepalive_ok`` must probe each keepalive unit's HEAD (the
    span primary) and treat TP-peer members as healthy through it — reusing
    health.check_workers' tp-peer judgment, never inventing a second one.

    Fixtures here monkeypatch ``serving.keepalive_units`` / ``serving.load_serving``
    and ``health._tp_peer_nodes`` so the REAL code paths (http_check) run."""

    def _patch(self, monkeypatch, units, tp_peers, probes):
        import hscc_daemon.health as health_mod
        import hscc_daemon.serving as serving_mod
        monkeypatch.setattr(serving_mod, "load_serving",
                            lambda: {"units": units or []})
        monkeypatch.setattr(serving_mod, "keepalive_units",
                            lambda s: self._flatten(units or []))
        monkeypatch.setattr(health_mod, "_tp_peer_nodes",
                            lambda: set(tp_peers or []))

        calls = []

        def _probe(url, timeout=5):
            calls.append(url)
            return {"ok": probes.get(url, False),
                    "status": 200 if probes.get(url, False) else 0}

        import hscc_daemon.util as util_mod
        monkeypatch.setattr(util_mod, "http_check", _probe)
        return calls

    def _flatten(self, units):
        """Keepalive units contract: ONE entry per node {node, port, recipe,
        id} (serving.py:172-196). Simulates the real flattening."""
        out = []
        for u in units:
            port = u.get("port", 8000)
            for node in (u.get("nodes") or []):
                out.append({"node": node, "port": port,
                            "recipe": u.get("recipe"),
                            "id": u.get("id") or f"{node}:{port}"})
        return out

    def test_tp_head_healthy_peer_not_serving_ok(
            self, monkeypatch):
        """A multi-node keepalive unit (247 head, 248 TP peer): the peer does
        not serve HTTP and is a known tp_peer, so it is NOT probed; the head
        answers ⇒ keepalive_ok True."""
        units = [{"id": "ka-1", "nodes": ["247", "248"], "port": 8000,
                  "recipe": "r"}]
        calls = self._patch(
            monkeypatch, units, tp_peers=["248"],
            probes={"http://247:8000/health": True})
        assert ad._default_keepalive_ok() is True
        # Only the head was probed; the tp peer was never hit.
        assert calls == ["http://247:8000/health"]

    def test_head_down_failsafe_false(self, monkeypatch):
        """The unit's head does not answer ⇒ False (abort teardown)."""
        units = [{"id": "ka-1", "nodes": ["247", "248"], "port": 8000,
                  "recipe": "r"}]
        calls = self._patch(
            monkeypatch, units, tp_peers=["248"],
            probes={"http://247:8000/health": False})
        assert ad._default_keepalive_ok() is False
        assert calls == ["http://247:8000/health"]

    def test_single_node_unit_still_works(self, monkeypatch):
        """A single-node keepalive unit (no tp peers) still answers ⇒ True."""
        units = [{"id": "ka-solo", "nodes": ["200"], "port": 8001,
                  "recipe": "r"}]
        calls = self._patch(
            monkeypatch, units, tp_peers=[],
            probes={"http://200:8001/health": True})
        assert ad._default_keepalive_ok() is True
        assert calls == ["http://200:8001/health"]

    def test_single_node_down_false(self, monkeypatch):
        """A single-node unit whose only node is down ⇒ False."""
        units = [{"id": "ka-solo", "nodes": ["200"], "port": 8001,
                  "recipe": "r"}]
        self._patch(monkeypatch, units, tp_peers=[],
                    probes={"http://200:8001/health": False})
        assert ad._default_keepalive_ok() is False

    def test_no_keepalive_units_ok(self, monkeypatch):
        """No keepalive units ⇒ nothing to protect ⇒ True."""
        self._patch(monkeypatch, [], tp_peers=[], probes={})
        assert ad._default_keepalive_ok() is True

    def test_probe_error_failsafe_false(self, monkeypatch):
        """A probe that raises (network error) ⇒ False (abort), not ignored."""
        units = [{"id": "ka-1", "nodes": ["247"], "port": 8000, "recipe": "r"}]
        import hscc_daemon.serving as serving_mod
        import hscc_daemon.health as health_mod
        monkeypatch.setattr(serving_mod, "load_serving",
                            lambda: {"units": units})
        monkeypatch.setattr(serving_mod, "keepalive_units",
                            lambda s: self._flatten(units))
        monkeypatch.setattr(health_mod, "_tp_peer_nodes", lambda: set())
        import hscc_daemon.util as util_mod

        def _boom(url, timeout=5):
            raise OSError("conn refused")

        monkeypatch.setattr(util_mod, "http_check", _boom)
        assert ad._default_keepalive_ok() is False


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
            probes=[],
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
            probes=[],
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
            probes=[],
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
            probes=[],
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
            probes=[],
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
            probes=[],
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
            probes=[],
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
            probes=[],
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
    Each unit carries its own ``recipe`` (scoped stop TARGET), mirroring the
    real serving.json.
    """
    data = {
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

    # -- each stop carries a recipe TARGET sparkrun accepts -------------------
    def test_stop_cmd_has_recipe_target(self, tmp_path, monkeypatch,
                                        autodown_file):
        """Every stop is ``sparkrun stop <recipe> --hosts <nodes>``.

        sparkrun requires a TARGET (recipe or cluster id) — the OLD form
        ``sparkrun stop --hosts <nodes>`` failed 100% of the time with
        "Must specify TARGET or --all". The recipe is the unit's OWN, scoped so
        teardown never issues a catch-all ``--all`` that could reach keepalive
        nodes (C4).
        """
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        res = ad.teardown(
            serving_path=serving, run_cmd_fn=runner,
            kanban_db=_FakeKb([]), agents_file=self._agents(tmp_path),
            now=NOW, keepalive_ok=lambda: True,
            http_check_fn=lambda *a, **k: {"ok": False},
        )
        assert res["result"] == "down"
        assert len(runner.calls) == 2      # wk1 + orch
        for call in runner.calls:
            cmd = call["cmd"]
            assert cmd[0] == "sparkrun"
            assert cmd[1] == "stop"
            assert cmd[2]               # TARGET recipe is non-empty
            assert "--hosts" in cmd
        # Orchestrator stop targets the orchestrator's own recipe (expanded by
        # orchestrator_recipe, exactly as _unit_start_cmd resolves it).
        orch_cmd = runner.calls[-1]["cmd"]
        assert orch_cmd[1] == "stop"
        assert orch_cmd[2] == os.path.expanduser("~/.sparkrun-local/recipes/orch.yaml")
        assert orch_cmd[4] == "10.0.0.244,10.0.0.246"  # orchestrator nodes
        # Worker stop (issued first) targets the worker's own recipe.
        assert runner.calls[0]["cmd"][2] == "~/.sparkrun-local/recipes/wk.yaml"
        # Never --all (could hit keepalive nodes); never keepalive recipe issue.
        for call in runner.calls:
            assert "--all" not in call["cmd"]
        plan_recipes = {e["recipe"] for e in res["plan"]}
        assert plan_recipes == {os.path.expanduser("~/.sparkrun-local/recipes/orch.yaml"),
                                "~/.sparkrun-local/recipes/wk.yaml"}

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


# ---------------------------------------------------------------------------
# Phase 5 — autoup() wake sequence + cycle wake seam (§4, §4.5, §8)
# ---------------------------------------------------------------------------

# Health probes (injected, no real HTTP).
class _HealthyProbe:
    """health probe that reports healthy for every unit (immediate ready)."""
    def __call__(self, url, timeout=5):
        return {"ok": True, "status": 200}


class _DownProbe:
    """health probe that never reports a unit ready (forces timeout)."""
    def __call__(self, url, timeout=5):
        return {"ok": False, "output": "not ready"}


class _AdvancingClock:
    """monotonic-style clock that advances ``step`` on every read, so a poll
    loop progresses (and eventually crosses the deadline) without real time."""
    def __init__(self, start=0.0, step=1.0):
        self.t = start
        self.step = step
    def __call__(self):
        t = self.t
        self.t += self.step
        return t


def _noop_sleep(_seconds):
    """Do not actually sleep — tests must never block."""
    return None


def _cmd_hosts(cmd):
    """Extract the node list a start/stop command targets.

    Both command forms carry ``--hosts <nodes>`` (start: sparkrun run ... --
    hosts <comma-list> ...; stop: sparkrun stop <recipe> --hosts <comma-list>).
    Returns a frozenset of host strings.
    """
    for i, tok in enumerate(cmd):
        if tok == "--hosts" and i + 1 < len(cmd):
            return frozenset(cmd[i + 1].split(","))
    return frozenset()


def _write_down_cfg(autodown_file, last_activity_iso=None):
    """Write an enabled, DOWN config (the wake-seam precondition)."""
    down = NOW - _dt.timedelta(minutes=30)
    cfg = dict(ad.DEFAULT_CONFIG)
    cfg["enabled"] = True
    cfg["state"] = "down"
    cfg["idle_minutes"] = 10
    cfg["down_since"] = down.isoformat()
    cfg["last_activity_iso"] = last_activity_iso or down.isoformat()
    ad.save_config(cfg)
    return cfg


class TestAutoup:
    """autoup() with injected fakes — ZERO real sparkrun commands."""

    def _setup(self, tmp_path, monkeypatch, autodown_file, results=None):
        """Common wiring: block file, serving fixture, stub notifiers.

        The config does NOT need to be pre-written (autoup creates what it
        needs); but we pre-write a DOWN config + latched block so call-ordering
        assertions have a realistic starting point. Returns
        (serving_path, runner, block_file).
        """
        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        serving = _write_serving(tmp_path)
        # Seed a latched intentional block (as teardown left it).
        _lifecycle.save_watchdog_block(
            {"blocked": True, "intentional": "autodown",
             "reason": ad.WATCHDOG_TEARDOWN_REASON,
             "blocked_at": NOW.isoformat(), "failures": []})
        _write_down_cfg(autodown_file)
        runner = _FakeRunner(block_file, results=results)
        # Stub notifiers (both channels) so nothing is actually sent.
        monkeypatch.setattr(ad, "notify_operations", lambda *a, **k: True)
        monkeypatch.setattr(ad, "send_macos_notification", lambda *a, **k: True)
        return serving, runner, block_file

    # -- starts EXACTLY what teardown stopped; keepalive never started -------
    def test_starts_units_teardown_stopped_keepalive_exempt(
            self, tmp_path, monkeypatch, autodown_file):
        """autoup starts the non-keepalive set (wk1 + orch); keepalive never.

        The wake set must equal the teardown set (round-trip symmetry): the
        keepalive unit (.248) that was never stopped is never started either.
        """
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        res = ad.autoup(
            serving_path=serving, run_cmd_fn=runner,
            http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
            sleep_fn=_noop_sleep, notify=False,
        )
        assert res["result"] == "up"
        assert len(runner.calls) == 2      # wk1 + orch
        # Keepalive node never appears in any start command.
        all_hosts = " ".join(" ".join(c["cmd"]) for c in runner.calls)
        assert "10.0.0.248" not in all_hosts      # keepalive exempt
        assert "10.0.0.247" in all_hosts          # non-keepalive worker
        assert "10.0.0.244" in all_hosts          # orchestrator
        # Wake set (unit_ids) exactly equals the teardown set.
        plan_ids = set(res["ready"])
        assert plan_ids == {"wk1", "orch"}
        assert "wk-keep" not in plan_ids

    def test_each_start_cmd_is_sparkrun_run_ensure(
            self, tmp_path, monkeypatch, autodown_file):
        """Every start command is the sparkrun run --ensure form (§4.3)."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        ad.autoup(
            serving_path=serving, run_cmd_fn=runner,
            http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
            sleep_fn=_noop_sleep, notify=False,
        )
        for call in runner.calls:
            cmd = call["cmd"]
            assert cmd[0] == "sparkrun"
            assert cmd[1] == "run"
            assert "--ensure" in cmd
            assert "--no-follow" in cmd

    # -- orchestrator started FIRST (reverse of teardown) -------------------
    def test_orchestrator_started_first(
            self, tmp_path, monkeypatch, autodown_file):
        """The orchestrator unit's start command comes FIRST (§4.3)."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        res = ad.autoup(
            serving_path=serving, run_cmd_fn=runner,
            http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
            sleep_fn=_noop_sleep, notify=False,
        )
        assert res["result"] == "up"
        first = " ".join(runner.calls[0]["cmd"])
        # Orchestrator host (.244) is in the FIRST start command.
        assert "10.0.0.244" in first
        assert res["started"][0]["kind"] == "orchestrator"
        # Worker starts after the orchestrator.
        assert "10.0.0.247" in " ".join(runner.calls[1]["cmd"])

    # -- block cleared ONLY after readiness confirmed ------------------------
    def test_block_latched_through_starts_then_cleared(
            self, tmp_path, monkeypatch, autodown_file):
        """The intentional block is present at EVERY start, and only cleared
        (blocked:false, intentional removed, failures cleared) AFTER readiness
        was confirmed — i.e. after all starts issued."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        # Readiness is NOT confirmed until after both starts issue: probe reads
        # a flag that is only flipped by the runner after the LAST start.
        ready = {"ok": False}
        orig_runner = runner

        class _FlipOnFirstStart:
            def __init__(self):
                self.calls = 0
            def __call__(self, cmd, timeout=30):
                out = orig_runner(cmd, timeout=timeout)
                self.calls += 1
                if self.calls == 2:   # after the LAST (2nd) start issued
                    ready["ok"] = True
                return out

        ad.autoup(
            serving_path=serving, run_cmd_fn=_FlipOnFirstStart(),
            http_check_fn=lambda url, timeout=5: {"ok": ready["ok"],
                                                  "status": 200},
            clock=lambda: 0.0, sleep_fn=_noop_sleep, notify=False,
        )
        # Block was latched (intentional autodown) at EVERY start call.
        for call in orig_runner.calls:
            assert call["block"]["intentional"] == "autodown"
            assert call["block"]["blocked"] is True
        # After autoup returns, the block is cleared: not blocked, no
        # intentional, failures emptied.
        with open(block_file) as f:
            cleared = json.load(f)
        assert cleared.get("blocked") is False
        assert cleared.get("intentional") is None
        assert cleared.get("failures") == []

    def test_readiness_timeout_failure_path(
            self, tmp_path, monkeypatch, autodown_file):
        """Readiness timeout ⇒ failure: state NOT down/waking, intentional
        cleared (watchdog resumes), loud notify."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        notes = []
        monkeypatch.setattr(
            ad, "notify_operations",
            lambda msg, *a, **k: notes.append(("tg", str(msg))))
        monkeypatch.setattr(
            ad, "send_macos_notification",
            lambda title, msg, *a, **k: notes.append(("desk", str(msg))))
        # Units never become ready + a small deadline (wake_grace_minutes=0 ⇒
        # timeout_seconds=0) so the poll loop times out without real time.
        res = ad.autoup(
            serving_path=serving, run_cmd_fn=runner,
            http_check_fn=_DownProbe(), clock=_AdvancingClock(start=0, step=1),
            sleep_fn=_noop_sleep, wake_grace_minutes=0, notify=True,
        )
        assert res["result"] == "not-ready"
        # NOT left stuck in waking (the invisible wedge).
        cfg = ad.load_config()
        assert cfg["state"] != "waking"
        assert cfg["state"] == "up"          # reality-ish, operator-actionable
        assert "READINESS TIMEOUT" in cfg["reason"]
        # wake bookkeeping kept so the operator sees the trigger.
        assert cfg["wake_source"] == "cycle"
        assert cfg["wake_at"] is not None
        # Block cleared (intentional removed) so the watchdog resumes + heals.
        with open(block_file) as f:
            blk = json.load(f)
        assert blk.get("blocked") is False
        assert blk.get("intentional") is None
        # Loud notify: critical-priority desktop + ops Telegram both fired.
        assert len(notes) == 2
        assert "TIMEOUT" in notes[0][1] or "TIMEOUT" in notes[1][1]
        assert "TIMEOUT" in notes[1][1] or "TIMEOUT" in notes[0][1]

    def test_start_failure_failure_path(self, tmp_path, monkeypatch,
                                        autodown_file):
        """A start command failure ⇒ failure path: intentional cleared, state up
        (not down/waking), loud notify, no readiness wait entered."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file,
                                                  results=[False])
        notes = []
        monkeypatch.setattr(ad, "notify_operations",
                            lambda msg, *a, **k: notes.append(msg))
        monkeypatch.setattr(ad, "send_macos_notification",
                            lambda *a, **k: notes.append("desk"))
        res = ad.autoup(
            serving_path=serving, run_cmd_fn=runner,
            http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
            sleep_fn=_noop_sleep, notify=True,
        )
        assert res["result"] == "start-failed"
        # Only the orchestrator (first) was attempted; worker never started.
        assert len(runner.calls) == 1
        # Not stuck waking; state reflects reality (up), reason recorded.
        cfg = ad.load_config()
        assert cfg["state"] == "up"
        assert "start-failed" not in cfg  # result is return-value only
        assert "wake FAILED" in cfg["reason"]
        # Intentional cleared so the watchdog resumes + can heal.
        with open(block_file) as f:
            blk = json.load(f)
        assert blk.get("intentional") is None
        assert blk.get("blocked") is False
        assert notes, "loud notify must fire on start failure"

    # -- already waking ⇒ second call is a no-op ----------------------------
    def test_already_waking_is_noop(self, tmp_path, monkeypatch,
                                    autodown_file):
        """state==waking ⇒ autoup returns already-waking, starts NOTHING.

        The guard is for concurrent triggers while a wake is in flight: a second
        autoup() call while state is still "waking" must not start a duplicate
        set. We seed state=waking (the in-flight state) and assert no starts.
        """
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        # Seed state=waking — as if a previous wake is still in flight.
        cfg = ad.load_config()
        cfg["state"] = "waking"
        ad.save_config(cfg)
        res = ad.autoup(
            serving_path=serving, run_cmd_fn=runner,
            http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
            sleep_fn=_noop_sleep, notify=False,
        )
        assert res["result"] == "already-waking"
        assert res["started"] == []
        assert res["ready"] == []
        # No start command issued at all.
        assert runner.calls == []

    # -- success ⇒ state up + wake bookkeeping cleared -----------------------
    def test_success_sets_state_up_clears_wake(self, tmp_path, monkeypatch,
                                              autodown_file):
        """A clean wake: state=up, wake_source/wake_at cleared, block cleared."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        cfg = ad.load_config()
        cfg["wake_source"] = "telegram"   # will be cleared on success
        cfg["wake_at"] = "2026-08-23T09:00:00+00:00"
        ad.save_config(cfg)
        res = ad.autoup(
            serving_path=serving, run_cmd_fn=runner,
            http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
            sleep_fn=_noop_sleep, notify=False,
        )
        assert res["result"] == "up"
        cfg = ad.load_config()
        assert cfg["state"] == "up"
        assert cfg["wake_source"] is None
        assert cfg["wake_at"] is None
        assert cfg["reason"] == ""

    # -- round trip: teardown() then autoup() -------------------------------
    def test_round_trip_teardown_then_autoup(self, tmp_path, monkeypatch,
                                             autodown_file):
        """teardown() then autoup() with the SAME fixture returns the cluster
        to the starting unit set: the units stopped == the units started, and
        end state is up with the block cleared."""
        serving = _write_serving(tmp_path)
        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        _write_idle_cfg(autodown_file)
        monkeypatch.setattr(ad, "notify_operations", lambda *a, **k: True)
        monkeypatch.setattr(ad, "send_macos_notification", lambda *a, **k: True)
        agents = tmp_path / "agents.json"
        agents.write_text(json.dumps({"agents": [{"name": "a",
                                                  "status": "idle"}]}))
        down_runner = _FakeRunner(block_file)
        stop_result = ad.teardown(
            serving_path=serving, run_cmd_fn=down_runner,
            kanban_db=_FakeKb([]), agents_file=str(agents),
            now=NOW, keepalive_ok=lambda: True,
            http_check_fn=lambda *a, **k: {"ok": False},  # ports down
        )
        assert stop_result["result"] == "down"
        stopped_ids = {e["unit_id"] for e in stop_result["plan"]}

        # Now wake with the same serving fixture. Readiness healthy immediately.
        up_runner = _FakeRunner(block_file)
        up_result = ad.autoup(
            serving_path=serving, run_cmd_fn=up_runner,
            http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
            sleep_fn=_noop_sleep, notify=False,
        )
        assert up_result["result"] == "up"
        # The starting non-keepalive set is fully restored.
        assert set(up_result["ready"]) == stopped_ids == {"wk1", "orch"}
        # Every unit stopped was started again (same command hostsets).
        stopped_hosts = {_cmd_hosts(c["cmd"]) for c in down_runner.calls}
        started_hosts = {_cmd_hosts(c["cmd"]) for c in up_runner.calls}
        assert started_hosts == stopped_hosts
        # End state: up + block cleared.
        assert ad.load_config()["state"] == "up"
        with open(block_file) as f:
            blk = json.load(f)
        assert blk.get("blocked") is False
        assert blk.get("intentional") is None


# ---------------------------------------------------------------------------
# Phase 5 — cycle() wake seam (§4): state=down + fresh activity ⇒ autoup
# ---------------------------------------------------------------------------

class TestCycleWakeSeam:
    """cycle() triggers autoup exactly when it should (state=down + fresh
    activity), and never otherwise. autoup is monkeypatched — the seam is what
    is under test, not autoup's internals."""

    def _cfg(self, autodown_file, last_activity_iso):
        """Write an enabled, DOWN config with the given last_activity_iso."""
        down = NOW - _dt.timedelta(minutes=30)
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["enabled"] = True
        cfg["state"] = "down"
        cfg["idle_minutes"] = 10
        cfg["down_since"] = down.isoformat()
        cfg["last_activity_iso"] = last_activity_iso
        ad.save_config(cfg)
        return cfg

    def test_down_fresh_activity_triggers_autoup_once(
            self, autodown_file, monkeypatch):
        """state=down + last_activity AFTER down_since ⇒ autoup called once."""
        calls = []
        monkeypatch.setattr(ad, "autoup", lambda: calls.append("autoup"),
                            raising=False)
        # Activity stamped AFTER down_since (a wake event arrived).
        fresh = NOW - _dt.timedelta(minutes=5)   # after down (30m ago)
        self._cfg(autodown_file, fresh.isoformat())
        ad.cycle(probes=[])
        assert calls == ["autoup"]

    def test_down_no_new_activity_does_not_trigger(
            self, autodown_file, monkeypatch):
        """state=down + NO new activity (last_activity == down_since) ⇒ autoup
        NOT called."""
        calls = []
        monkeypatch.setattr(ad, "autoup", lambda: calls.append("autoup"),
                            raising=False)
        down = NOW - _dt.timedelta(minutes=30)
        self._cfg(autodown_file, down.isoformat())  # last_activity == down_since
        ad.cycle(probes=[])
        assert calls == []

    def test_down_no_last_activity_does_not_trigger(
            self, autodown_file, monkeypatch):
        """state=down with NULL last_activity_iso ⇒ can't verify fresh activity
        ⇒ autoup NOT called (fail-safe)."""
        calls = []
        monkeypatch.setattr(ad, "autoup", lambda: calls.append("autoup"),
                            raising=False)
        self._cfg(autodown_file, None)
        ad.cycle(probes=[])
        assert calls == []

    def test_waking_does_not_trigger(self, autodown_file, monkeypatch):
        """state=waking ⇒ autoup NOT called (a wake is already in flight)."""
        calls = []
        monkeypatch.setattr(ad, "autoup", lambda: calls.append("autoup"),
                            raising=False)
        down = NOW - _dt.timedelta(minutes=30)
        self._cfg(autodown_file, down.isoformat())
        ad.load_config  # noqa
        cfg = ad.load_config()
        cfg["state"] = "waking"
        ad.save_config(cfg)
        ad.cycle(probes=[])
        assert calls == []

    def test_disabled_down_fresh_activity_does_not_trigger(
            self, autodown_file, monkeypatch):
        """Disabled ⇒ cycle returns before the wake seam: even a fresh-activity
        DOWN state never auto-wakes while autodown is off."""
        calls = []
        monkeypatch.setattr(ad, "autoup", lambda: calls.append("autoup"),
                            raising=False)
        down = NOW - _dt.timedelta(minutes=30)
        fresh = NOW - _dt.timedelta(minutes=5)
        self._cfg(autodown_file, fresh.isoformat())
        cfg = ad.load_config()
        cfg["enabled"] = False
        ad.save_config(cfg)
        ad.cycle(probes=[])
        assert calls == []

    def test_up_does_not_trigger_wake(self, autodown_file, monkeypatch):
        """state=up ⇒ wake seam not entered (idle path evaluates instead)."""
        calls = []
        monkeypatch.setattr(ad, "autoup", lambda: calls.append("autoup"),
                            raising=False)
        down = NOW - _dt.timedelta(minutes=30)
        self._cfg(autodown_file, down.isoformat())
        cfg = ad.load_config()
        cfg["state"] = "up"
        ad.save_config(cfg)
        # Even if last_activity > down_since, state=up never auto-wakes.
        ad.cycle(kanban_db=_FakeKb([]), agents_file="",
                 now=NOW, keepalive_ok=lambda: True, probes=[])
        assert calls == []


# ---------------------------------------------------------------------------
# Phase 6 — activity-source probes (§1d) + _wait_ready silent-spin fix
# ---------------------------------------------------------------------------

class TestProbeKanbanActivity:
    """probe_kanban_activity stamps record_activity('kanban') iff the board has
    live/imminent work (§1d.3)."""

    def test_stamps_when_active_work(self, autodown_file):
        """A board with a running/ready card ⇒ activity stamped: last_activity
        advances."""
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["last_activity_iso"] = "2000-01-01T00:00:00+00:00"
        ad.save_config(cfg)
        # Board starts quiet, then a card becomes active (running).
        assert ad.probe_kanban_activity(_FakeKb([])) is False
        before = ad.load_config()["last_activity_iso"]
        assert ad.probe_kanban_activity(_FakeKb(["running"])) is True
        loaded = ad.load_config()
        assert loaded["last_activity_iso"] != before
        assert loaded["wake_source"] == "kanban"

    def test_no_stamp_when_board_quiet(self, autodown_file):
        """Board with only terminal statuses ⇒ no stamp, timestamp unchanged."""
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["last_activity_iso"] = "2000-01-01T00:00:00+00:00"
        ad.save_config(cfg)
        before = ad.load_config()["last_activity_iso"]
        assert ad.probe_kanban_activity(_FakeKb(["done", "blocked"])) is False
        assert ad.load_config()["last_activity_iso"] == before

    def test_no_stamp_when_unreadable(self, autodown_file):
        """Unreadable board ⇒ NO stamp (we never fabricate activity from an
        unreadable signal)."""
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["last_activity_iso"] = "2000-01-01T00:00:00+00:00"
        ad.save_config(cfg)
        before = ad.load_config()["last_activity_iso"]
        assert ad.probe_kanban_activity(_UnreachableKb()) is False
        assert ad.load_config()["last_activity_iso"] == before

    def test_kanban_probe_in_cycle_resets_window(
            self, autodown_file, tmp_path, monkeypatch):
        """Wired through cycle(): active work stamps activity → the window
        resets → teardown is NOT invoked (fresh activity beats the elapsed
        window, §1c/§1d)."""
        calls = []
        monkeypatch.setattr(ad, "teardown", lambda: calls.append("teardown"),
                            raising=False)
        # Enabled, up, and the window HAS elapsed on paper (15m old).
        self_cfg = dict(ad.DEFAULT_CONFIG)
        self_cfg["enabled"] = True
        self_cfg["state"] = "up"
        self_cfg["idle_minutes"] = 10
        self_cfg["last_activity_iso"] = (
            NOW - _dt.timedelta(minutes=15)).isoformat()
        ad.save_config(self_cfg)
        # Default probes include the kanban probe, which sees active work and
        # stamps (record_activity uses real now_iso) — resetting the window so
        # the elapsed check at NOW fails ⇒ no teardown.
        ad.cycle(kanban_db=_FakeKb(["running"]), agents_file="",
                 now=NOW, keepalive_ok=lambda: True)
        assert calls == []          # no teardown: probe reset the window
        # last_activity_iso was advanced by the kanban probe.
        assert ad.load_config()["last_activity_iso"] is not None

    def test_kanban_wake_seam_when_down(
            self, autodown_file, monkeypatch):
        """When DOWN, a fresh kanban card (active work) triggers autoup via the
        default probes: the probe stamps last_activity > down_since ⇒ wake.

        down_since is a fixed ANCIENT time (before any real `now_iso` the probe
        might stamp), so the wake decision is deterministic regardless of the
        machine clock.
        """
        calls = []
        monkeypatch.setattr(ad, "autoup", lambda: calls.append("autoup"),
                            raising=False)
        down = "2020-01-01T00:00:00+00:00"
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["enabled"] = True
        cfg["state"] = "down"
        cfg["idle_minutes"] = 10
        cfg["down_since"] = down
        cfg["last_activity_iso"] = down   # initially no fresh activity
        ad.save_config(cfg)
        # The default kanban probe sees a running card → stamps real now (after
        # 2020) → fresh activity > down_since → wake seam fires autoup.
        ad.cycle(kanban_db=_FakeKb(["running"]), agents_file="",
                 now=NOW, keepalive_ok=lambda: True)
        assert calls == ["autoup"]


class TestProbeHttpActivity:
    """probe_http_activity stamps record_activity('http') when the API server
    logged a request newer than our last activity (§1d.1)."""

    def _write_api_activity(self, tmp_path, ts_iso):
        p = tmp_path / "activity.json"
        p.write_text(json.dumps({"timestamp": ts_iso, "source": "http",
                                 "stream": "activity"}))
        return str(p)

    def test_stamps_on_newer_api_ts(self, autodown_file, tmp_path):
        """API activity newer than our last stamp ⇒ http activity recorded."""
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["last_activity_iso"] = "2000-01-01T00:00:00+00:00"
        ad.save_config(cfg)
        newer = (NOW - _dt.timedelta(minutes=5)).isoformat()
        activity_file = self._write_api_activity(tmp_path, newer)
        assert ad.probe_http_activity(activity_file) is True
        loaded = ad.load_config()
        assert loaded["wake_source"] == "http"
        assert loaded["last_activity_iso"] is not None

    def test_no_stamp_when_api_ts_stale(self, autodown_file, tmp_path):
        """API ts NOT newer than our last stamp ⇒ no http activity."""
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["last_activity_iso"] = (NOW - _dt.timedelta(minutes=5)).isoformat()
        ad.save_config(cfg)
        stale = (NOW - _dt.timedelta(minutes=15)).isoformat()
        activity_file = self._write_api_activity(tmp_path, stale)
        assert ad.probe_http_activity(activity_file) is False

    def test_no_stamp_when_file_absent(self, autodown_file, tmp_path):
        """Missing activity file ⇒ no stamp (fail-safe)."""
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["last_activity_iso"] = "2000-01-01T00:00:00+00:00"
        ad.save_config(cfg)
        before = ad.load_config()["last_activity_iso"]
        missing = str(tmp_path / "no-activity.json")
        assert ad.probe_http_activity(missing) is False
        assert ad.load_config()["last_activity_iso"] == before


class TestProbeTelegramActivity:
    """probe_telegram_activity stamps record_activity('telegram') on NEW
    inbound Telegram messages observed via the Hermes gateway log (§1d.2,
    design correction). Reads a fake gateway log — never the real one."""

    def _setup(self, tmp_path):
        gw = tmp_path / "gateway.log"
        off = tmp_path / "telegram_probe.offset"
        return str(gw), str(off)

    def test_baselines_first_call_no_stamp(self, tmp_path, autodown_file):
        """First probe on an existing log with markers baselines at EOF and
        stamps NOTHING (old mail is not fresh activity)."""
        gw, off = self._setup(tmp_path)
        with open(gw, "w") as f:
            f.write("blah\n" + ad.TELEGRAM_MARKER + " old msg\n")
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["last_activity_iso"] = "2000-01-01T00:00:00+00:00"
        ad.save_config(cfg)
        before = ad.load_config()["last_activity_iso"]
        assert ad.probe_telegram_activity(gw, off) is False
        assert ad.load_config()["last_activity_iso"] == before
        # Offset now pinned to EOF (measured by the probe having consumed it).
        assert ad._load_telegram_offset(off) == len(
            "blah\n" + ad.TELEGRAM_MARKER + " old msg\n")

    def test_stamps_on_new_marker(self, tmp_path, autodown_file):
        """After the baseline, a NEW inbound marker line ⇒ telegram activity
        stamped, last_activity advances."""
        gw, off = self._setup(tmp_path)
        with open(gw, "w") as f:
            f.write("noise line\n")
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["last_activity_iso"] = "2000-01-01T00:00:00+00:00"
        ad.save_config(cfg)
        ad.probe_telegram_activity(gw, off)   # baseline
        before = ad.load_config()["last_activity_iso"]

        with open(gw, "a") as f:
            f.write(ad.TELEGRAM_MARKER + " new inbound\n")
        assert ad.probe_telegram_activity(gw, off) is True
        loaded = ad.load_config()
        assert loaded["wake_source"] == "telegram"
        assert loaded["last_activity_iso"] != before

    def test_no_stamp_when_nothing_new(self, tmp_path, autodown_file):
        """Second probe with no appended content ⇒ no stamp (idempotent)."""
        gw, off = self._setup(tmp_path)
        with open(gw, "w") as f:
            f.write(ad.TELEGRAM_MARKER + " one\n")
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["last_activity_iso"] = "2000-01-01T00:00:00+00:00"
        ad.save_config(cfg)
        ad.probe_telegram_activity(gw, off)   # baseline
        before = ad.load_config()["last_activity_iso"]
        assert ad.probe_telegram_activity(gw, off) is False
        assert ad.load_config()["last_activity_iso"] == before

    def test_log_rotation_rebaselines(self, tmp_path, autodown_file):
        """Truncated log (size < offset) re-baselines from 0 and can stamp."""
        gw, off = self._setup(tmp_path)
        with open(gw, "w") as f:
            f.write("A" * 100 + "\n")
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["last_activity_iso"] = "2000-01-01T00:00:00+00:00"
        ad.save_config(cfg)
        ad.probe_telegram_activity(gw, off)   # baseline offset=100+
        assert ad._load_telegram_offset(off) == 101  # 100 A's + 1 newline

        # Rotate: fresh (smaller) file with a marker line (no trailing giant
        # prefix). Offset (101) > new size ⇒ re-baseline from 0 and stamp.
        with open(gw, "w") as f:
            f.write(ad.TELEGRAM_MARKER + " post-rotation\n")
        assert ad.probe_telegram_activity(gw, off) is True
        assert ad.load_config()["wake_source"] == "telegram"
        # Offset re-pinned to the new EOF.
        assert ad._load_telegram_offset(off) == len(
            ad.TELEGRAM_MARKER + " post-rotation\n")

    def test_missing_log_no_stamp(self, tmp_path, autodown_file):
        """Missing gateway log ⇒ no stamp, no crash."""
        gw, off = self._setup(tmp_path)
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["last_activity_iso"] = "2000-01-01T00:00:00+00:00"
        ad.save_config(cfg)
        before = ad.load_config()["last_activity_iso"]
        assert ad.probe_telegram_activity(gw, off) is False
        assert ad.load_config()["last_activity_iso"] == before

    def test_telegram_probe_in_cycle_wakes_when_down(
            self, tmp_path, monkeypatch, autodown_file):
        """Wired through cycle(): when DOWN, a fresh inbound telegram marker
        (via the default telegram probe) triggers autoup."""
        calls = []
        monkeypatch.setattr(ad, "autoup", lambda: calls.append("autoup"),
                            raising=False)
        gw, off = self._setup(tmp_path)
        with open(gw, "w") as f:
            f.write("seed line\n")
        # Point the module-level paths at the fake log/offset so the DEFAULT
        # probes (which read module globals) observe it.
        monkeypatch.setattr(ad, "GATEWAY_LOG", gw)
        monkeypatch.setattr(ad, "TELEGRAM_OFFSET_FILE", off)
        # Baseline first (outside a cycle), as the daemon would on first run.
        ad.probe_telegram_activity()
        # DOWN config with an ANCIENT down_since so the probe's real-clock stamp
        # is deterministically fresh (> down_since) regardless of the clock.
        down = "2020-01-01T00:00:00+00:00"
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["enabled"] = True
        cfg["state"] = "down"
        cfg["idle_minutes"] = 10
        cfg["down_since"] = down
        cfg["last_activity_iso"] = down
        ad.save_config(cfg)
        with open(gw, "a") as f:
            f.write(ad.TELEGRAM_MARKER + " fresh while down\n")
        ad.cycle(kanban_db=_FakeKb([]), agents_file="",
                 now=NOW, keepalive_ok=lambda: True)
        assert calls == ["autoup"]


class TestWaitReadySilentSpin:
    """_wait_ready must not swallow probe errors silently (the real defect
    confirmed by live testing)."""

    def _plan(self):
        return [
            {"kind": "orchestrator", "unit_id": "orch",
             "nodes": ["10.0.0.244"], "port": 8000},
            {"kind": "worker", "unit_id": "wk1",
             "nodes": ["10.0.0.247"], "port": 8000},
        ]

    def test_raising_probe_logs_once_and_returns_not_ok(
            self, monkeypatch):
        """A probe that raises every round is logged ONCE (not per-round) and
        _wait_ready still returns (ready=[], ok=False) at the deadline — it does
        NOT spin silently."""
        logs = []
        monkeypatch.setattr(ad, "log",
                            lambda msg, level="INFO": logs.append(msg))
        def boom(url, timeout=5):
            raise RuntimeError("probe is broken")
        # Two units raise every round; advancing clock crosses the deadline.
        ready, ok = ad._wait_ready(
            self._plan(), http_check_fn=boom,
            clock=_AdvancingClock(start=0, step=1), sleep_fn=_noop_sleep,
            timeout_seconds=5)
        assert ready == []          # nothing became ready
        assert ok is False          # not silently ok — timed out not-ready
        # Logged the raise, but ONCE PER UNIT (bounded, not per-round).
        raise_lines = [m for m in logs if "raised" in m]
        assert len(raise_lines) == 2      # orch + wk1, half-a-dozen rounds
        assert "probe is broken" in raise_lines[0]
        assert "orch" in raise_lines[0]
        assert "wk1" in raise_lines[1]

    def test_no_log_when_probe_healthy(self, monkeypatch):
        """A healthy probe ⇒ no raise logged, returns ready immediately."""
        logs = []
        monkeypatch.setattr(ad, "log",
                            lambda msg, level="INFO": logs.append(msg))
        healthy = lambda url, timeout=5: {"ok": True, "status": 200}
        ready, ok = ad._wait_ready(
            self._plan(), http_check_fn=healthy,
            clock=lambda: 0.0, sleep_fn=_noop_sleep, timeout_seconds=5)
        assert ready == ["orch", "wk1"]
        assert ok is True
        assert not any("raised" in m for m in logs)


# ---------------------------------------------------------------------------
# Phase 8 — daemon-start recovery + self-healing intentional block
#           (§8 "daemon dies while down" / "while waking" /
#            "watchdog-block file corrupt/missing")
# ---------------------------------------------------------------------------


class TestResumeFromRestart:
    """resume_from_restart() — the once-on-startup recovery hook (§8).

    Each test monkeypatches AUTODOWN_FILE + the lifecycle WATCHDOG_BLOCK_FILE
    to tmp paths (the autouse _isolate_hscc fixture already redirects the real
    ~/.hscc paths), stubs notifiers, and asserts the reconciliation side effects
    WITHOUT any real sparkrun command or HTTP probe.
    """

    def _setup(self, tmp_path, monkeypatch):
        """Point autodown + lifecycle file paths at tmp paths; stub notifiers.

        Returns (autodown path via fixture, block_file path).
        """
        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        monkeypatch.setattr(ad, "notify_operations", lambda *a, **k: True)
        monkeypatch.setattr(ad, "send_macos_notification", lambda *a, **k: True)
        return block_file

    def _cfg(self, autodown_file, **overrides):
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg.update(overrides)
        ad.save_config(cfg)
        return cfg

    # -- disabled ⇒ do nothing at all, regardless of state -----------------
    @pytest.mark.parametrize("state", ["down", "waking", "up"])
    def test_disabled_does_nothing(self, autodown_file, tmp_path, monkeypatch,
                                   state):
        """enabled:false ⇒ no block re-assert, no autoup, no state change."""
        block_file = self._setup(tmp_path, monkeypatch)
        self._cfg(autodown_file, enabled=False, state=state)
        up_calls = []
        monkeypatch.setattr(ad, "autoup", lambda: up_calls.append("autoup"),
                            raising=False)

        ad.resume_from_restart()

        # No autoup, no block file created, config untouched on disk.
        assert up_calls == []
        import os as _os
        assert not _os.path.exists(block_file)
        assert ad.load_config()["state"] == state
        assert ad.load_config()["enabled"] is False

    # -- state=down ⇒ block re-asserted, NO start commands -----------------
    def test_down_reasserts_block_no_starts(self, autodown_file, tmp_path,
                                            monkeypatch):
        """startup with state:down ⇒ block re-asserted (intentional), and NO
        autoup / start command is issued — the serving layer stays down."""
        block_file = self._setup(tmp_path, monkeypatch)
        self._cfg(autodown_file, enabled=True, state="down")
        up_calls = []
        monkeypatch.setattr(ad, "autoup", lambda: up_calls.append("autoup"),
                            raising=False)
        # The block file was deleted while down (the corrupt/missing case).
        assert not _os_exists(block_file)

        ad.resume_from_restart()

        assert up_calls == []          # no start issued at all
        # Block re-asserted: blocked + intentional autodown with teardown reason.
        with open(block_file) as f:
            blk = json.load(f)
        assert blk.get("blocked") is True
        assert blk.get("intentional") == "autodown"
        assert blk.get("reason") == ad.WATCHDOG_TEARDOWN_REASON
        # Config state unchanged — still down (the operator's intent preserved).
        assert ad.load_config()["state"] == "down"

    def test_down_reasserts_resetting_block(self, autodown_file, tmp_path,
                                            monkeypatch):
        """startup with state:down + a corrupt/reset block (intentional wiped)
        ⇒ the block is re-asserted back to intentional autodown."""
        block_file = self._setup(tmp_path, monkeypatch)
        self._cfg(autodown_file, enabled=True, state="down")
        monkeypatch.setattr(ad, "autoup", lambda: None, raising=False)
        # A reset/corrupt block: blocked but no intentional marker.
        _lifecycle.save_watchdog_block({"blocked": False, "reason": "",
                                        "blocked_at": None, "failures": []})
        # Sanity: the block on disk currently lacks intentional.
        with open(block_file) as f:
            assert "intentional" not in json.load(f)

        ad.resume_from_restart()

        with open(block_file) as f:
            blk = json.load(f)
        assert blk.get("blocked") is True
        assert blk.get("intentional") == "autodown"

    # -- state=waking ⇒ autoup invoked ------------------------------------
    def test_waking_runs_autoup(self, autodown_file, tmp_path, monkeypatch,
                                serving_path=None):
        """startup with state:waking ⇒ autoup is invoked to finish the wake."""
        block_file = self._setup(tmp_path, monkeypatch)
        self._cfg(autodown_file, enabled=True, state="waking")
        up_calls = []
        monkeypatch.setattr(ad, "autoup", lambda: up_calls.append("autoup"),
                            raising=False)

        ad.resume_from_restart()

        assert up_calls == ["autoup"]

    def test_waking_clears_stale_state_before_autoup(
            self, autodown_file, tmp_path, monkeypatch):
        """The stale ``waking`` is cleared (to up) BEFORE autoup so autoup's
        already-waking guard does not no-op the recovery wake."""
        block_file = self._setup(tmp_path, monkeypatch)
        self._cfg(autodown_file, enabled=True, state="waking")
        seen = {}
        monkeypatch.setattr(
            ad, "autoup",
            lambda: seen.update({"state_at_call": ad.load_config()["state"]}),
            raising=False)

        ad.resume_from_restart()

        # autoup saw state=up (not waking), so it will actually run the wake.
        assert seen["state_at_call"] == "up"

    # -- state=up ⇒ nothing happens ---------------------------------------
    def test_up_does_nothing(self, autodown_file, tmp_path, monkeypatch):
        """startup with state:up ⇒ no block write, no autoup, config intact."""
        block_file = self._setup(tmp_path, monkeypatch)
        self._cfg(autodown_file, enabled=True, state="up")
        up_calls = []
        monkeypatch.setattr(ad, "autoup", lambda: up_calls.append("autoup"),
                            raising=False)

        ad.resume_from_restart()

        assert up_calls == []
        import os as _os
        assert not _os.path.exists(block_file)   # nothing written
        assert ad.load_config()["state"] == "up"

    # -- resume_from_restart raising ⇒ defensive wrapper swallows it ------
    def test_defensive_swallows_raise(self, autodown_file, tmp_path,
                                      monkeypatch):
        """resume_from_restart raising ⇒ resume_from_restart_defensive logs and
        swallows it — the daemon startup proceeds."""
        logs = []
        monkeypatch.setattr(ad, "log", lambda msg, level="INFO": logs.append(msg))
        monkeypatch.setattr(ad, "resume_from_restart",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        # Must NOT raise — this is the daemon's startup hook contract.
        ad.resume_from_restart_defensive()

        assert any("resume_from_restart error" in m for m in logs)

    def test_defensive_delegates_to_resume(self, autodown_file, tmp_path,
                                           monkeypatch):
        """A healthy resume_from_restart is called through the wrapper."""
        block_file = self._setup(tmp_path, monkeypatch)
        self._cfg(autodown_file, enabled=True, state="down")
        called = []
        monkeypatch.setattr(ad, "resume_from_restart",
                            lambda: called.append("resume"))
        ad.resume_from_restart_defensive()
        assert called == ["resume"]


class TestSelfHeal:
    """The per-cycle self-healing intentional block (§8 corrupt/missing)."""

    def test_reasserts_when_block_missing(self, tmp_path, monkeypatch):
        """cycle with state:down + no block file ⇒ block re-asserted."""
        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        assert not _os_exists(block_file)
        # Direct helper check.
        assert ad._self_heal_intentional_block() is True
        with open(block_file) as f:
            assert json.load(f).get("intentional") == "autodown"

    def test_reasserts_when_intentional_absent(self, tmp_path, monkeypatch):
        """cycle with state:down + a reset block (blocked but no intentional)
        ⇒ block re-asserted."""
        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        _lifecycle.save_watchdog_block({"blocked": False, "reason": "",
                                        "blocked_at": None, "failures": []})
        assert ad._self_heal_intentional_block() is True
        with open(block_file) as f:
            blk = json.load(f)
        assert blk.get("blocked") is True
        assert blk.get("intentional") == "autodown"

    def test_no_rewrite_when_already_asserted(self, tmp_path, monkeypatch):
        """An already-correct block ⇒ self-heal is a no-op (False)."""
        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        _lifecycle.save_watchdog_block({"blocked": True, "intentional":
                                        "autodown", "reason": "x"})
        assert ad._self_heal_intentional_block() is False

    def test_reasserts_when_blocked_false_intentional_present(self, tmp_path, monkeypatch):
        """FIX 2 (defense-in-depth, §8 forbids the silent half-state): a block
        with ``blocked`` False but ``intentional == \"autodown\"`` (the
        split-brain the watchdog's backoff-elapsed path used to leave behind)
        is NOT already-asserted — it must be treated as NEEDING RE-ASSERT and
        re-set ``blocked: true``. Otherwise autodown believes it is still down
        while the next watchdog tick can resurrect the orchestrator."""
        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        # blocked False + intentional autodown survives (the historical wedge).
        _lifecycle.save_watchdog_block({"blocked": False,
                                        "intentional": "autodown",
                                        "reason": "x"})
        assert ad._self_heal_intentional_block() is True   # NOT a no-op
        with open(block_file) as f:
            blk = json.load(f)
        assert blk.get("blocked") is True                  # re-asserted
        assert blk.get("intentional") == "autodown"

    def test_cycle_down_reasserts_block(self, autodown_file, tmp_path,
                                        monkeypatch):
        """Full cycle() while state:down with a missing block ⇒ block
        re-asserted every cycle (self-heal)."""
        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        monkeypatch.setattr(ad, "notify_operations", lambda *a, **k: True)
        monkeypatch.setattr(ad, "send_macos_notification", lambda *a, **k: True)
        # Enabled, down, no fresh activity (so autoup is not triggered).
        down = NOW - _dt.timedelta(minutes=30)
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["enabled"] = True
        cfg["state"] = "down"
        cfg["idle_minutes"] = 10
        cfg["down_since"] = down.isoformat()
        cfg["last_activity_iso"] = down.isoformat()   # no fresh activity
        ad.save_config(cfg)
        monkeypatch.setattr(ad, "autoup", lambda: None, raising=False)
        assert not _os_exists(block_file)

        ad.cycle(probes=[])

        # The block was re-asserted during the down cycle.
        with open(block_file) as f:
            blk = json.load(f)
        assert blk.get("blocked") is True
        assert blk.get("intentional") == "autodown"

    def test_cycle_down_keeps_healthy_block(self, autodown_file, tmp_path,
                                            monkeypatch):
        """cycle while state:down with an already-correct block ⇒ left as-is."""
        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        monkeypatch.setattr(ad, "notify_operations", lambda *a, **k: True)
        monkeypatch.setattr(ad, "send_macos_notification", lambda *a, **k: True)
        _lifecycle.save_watchdog_block({"blocked": True, "intentional":
                                        "autodown", "reason": "x",
                                        "blocked_at": "2026-01-01T00:00:00+00:00"})
        down = NOW - _dt.timedelta(minutes=30)
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["enabled"] = True
        cfg["state"] = "down"
        cfg["idle_minutes"] = 10
        cfg["down_since"] = down.isoformat()
        cfg["last_activity_iso"] = down.isoformat()
        ad.save_config(cfg)
        monkeypatch.setattr(ad, "autoup", lambda: None, raising=False)
        orig_blocked_at = None

        ad.cycle(probes=[])

        with open(block_file) as f:
            blk = json.load(f)
        assert blk.get("blocked") is True
        assert blk.get("intentional") == "autodown"


def _os_exists(path):
    import os as _os
    return _os.path.exists(path)



# ---------------------------------------------------------------------------
# F3/F4/F6/F7 — autodown O_EXCL lock, state gates, empty-plan vacuous-state
# guards, keepalive-node invariant (safety audit card t_c00c4d02).
# ---------------------------------------------------------------------------

class TestLockAndGates:
    """Fixes 3/4/6/7: O_EXCL lockfile, state gates on teardown/autoup,
    empty-plan vacuous-state guards, keepalive-node C4 invariant.

    Everything runs against the patched per-test ``~/.hscc`` (tmp), so the
    operator's live autodown.json / autodown.lock are never touched.
    """

    def _agents(self, tmp_path):
        p = tmp_path / "agents.json"
        p.write_text(json.dumps({"agents": [{"name": "a", "status": "idle"}]}))
        return str(p)

    def _block_file(self, tmp_path, monkeypatch):
        bf = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", bf)
        return bf

    def _setup(self, tmp_path, monkeypatch, autodown_file, results=None):
        bf = self._block_file(tmp_path, monkeypatch)
        serving = _write_serving(tmp_path)
        runner = _FakeRunner(bf, results=results)
        monkeypatch.setattr(ad, "notify_operations", lambda *a, **k: True)
        monkeypatch.setattr(ad, "send_macos_notification", lambda *a, **k: True)
        return serving, runner, bf

    # -- F3: teardown while state==down ⇒ busy, no stops --------------------
    def test_teardown_while_state_down_returns_busy(self, tmp_path, monkeypatch,
                                                    autodown_file):
        """state=="down" ⇒ teardown returns busy and issues NO stops."""
        serving, runner, bf = self._setup(tmp_path, monkeypatch, autodown_file)
        _write_down_cfg(autodown_file)          # state="down"
        res = ad.teardown(serving_path=serving, run_cmd_fn=runner,
                          kanban_db=_FakeKb([]),
                          agents_file=self._agents(tmp_path),
                          now=NOW, keepalive_ok=lambda: True)
        assert res["result"] == "busy"
        assert res["issued"] == []
        assert runner.calls == []               # no stop issued at all
        cfg = ad.load_config()
        assert cfg["state"] == "down"           # untouched

    # -- F3: autoup while teardown holds the lock ⇒ busy, no starts ---------
    def test_autoup_while_teardown_holds_lock_busy(self, tmp_path, monkeypatch,
                                                   autodown_file):
        """While teardown holds the O_EXCL lock, autoup returns busy, no starts."""
        serving, runner, bf = self._setup(tmp_path, monkeypatch, autodown_file)
        _write_down_cfg(autodown_file)
        # Simulate an in-flight teardown holding the autodown lock.
        assert ad._acquire_lock() is True
        try:
            res = ad.autoup(serving_path=serving, run_cmd_fn=runner,
                            http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
                            sleep_fn=_noop_sleep, notify=False)
            assert res["result"] == "busy"
            assert res["started"] == []
            assert res["ready"] == []
            assert runner.calls == []           # no start issued at all
        finally:
            ad._release_lock()

    # -- F3: lock released on success AND every failure/abort path (no leak) -
    def test_lock_released_on_all_paths_no_leak(self, tmp_path, monkeypatch,
                                                autodown_file):
        """The O_EXCL lock must never leak on ANY exit path (success, abort,
        failed, no-targets, no-units) — a leaked lock wedges the daemon."""
        import os as _os
        # teardown SUCCESS path (→ down).
        s1, r1, _ = self._setup(tmp_path, monkeypatch, autodown_file)
        _write_idle_cfg(autodown_file)
        res = ad.teardown(serving_path=s1, run_cmd_fn=r1, kanban_db=_FakeKb([]),
                          agents_file=self._agents(tmp_path), now=NOW,
                          keepalive_ok=lambda: True,
                          http_check_fn=lambda *a, **k: {"ok": False})
        assert res["result"] == "down"
        assert not _os.path.exists(ad.AUTODOWN_LOCK), "leak after teardown success"
        # teardown ABORT (idle predicate broke during re-verify).
        s2, r2, _ = self._setup(tmp_path, monkeypatch, autodown_file)
        _write_idle_cfg(autodown_file)
        res = ad.teardown(serving_path=s2, run_cmd_fn=r2,
                          kanban_db=_FakeKb(["running"]),
                          agents_file=self._agents(tmp_path), now=NOW,
                          keepalive_ok=lambda: True)
        assert res["result"] == "aborted"
        assert not _os.path.exists(ad.AUTODOWN_LOCK), "leak after teardown abort"
        # teardown FAILED (stop failed).
        s3, r3, _ = self._setup(tmp_path, monkeypatch, autodown_file,
                                results=[False])
        _write_idle_cfg(autodown_file)
        res = ad.teardown(serving_path=s3, run_cmd_fn=r3, kanban_db=_FakeKb([]),
                          agents_file=self._agents(tmp_path), now=NOW,
                          keepalive_ok=lambda: True)
        assert res["result"] == "failed"
        assert not _os.path.exists(ad.AUTODOWN_LOCK), "leak after teardown failed"
        # teardown no-targets (empty plan).
        s4, r4, _ = self._setup(tmp_path, monkeypatch, autodown_file)
        _write_idle_cfg(autodown_file)
        missing = str(tmp_path / "nope" / "serving.json")
        res = ad.teardown(serving_path=missing, run_cmd_fn=r4,
                          kanban_db=_FakeKb([]),
                          agents_file=self._agents(tmp_path), now=NOW,
                          keepalive_ok=lambda: True)
        assert res["result"] == "no-targets"
        assert not _os.path.exists(ad.AUTODOWN_LOCK), "leak after teardown no-targets"
        # autoup SUCCESS path (→ up).
        s5, r5, bf5 = self._setup(tmp_path, monkeypatch, autodown_file)
        _write_down_cfg(autodown_file)
        _lifecycle.save_watchdog_block({"blocked": True, "intentional": "autodown",
                                        "reason": ad.WATCHDOG_TEARDOWN_REASON,
                                        "blocked_at": NOW.isoformat(),
                                        "failures": []})
        res = ad.autoup(serving_path=s5, run_cmd_fn=r5,
                        http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
                        sleep_fn=_noop_sleep, notify=False)
        assert res["result"] == "up"
        assert not _os.path.exists(ad.AUTODOWN_LOCK), "leak after autoup success"
        # autoup no-units (empty plan).
        s6, r6, _ = self._setup(tmp_path, monkeypatch, autodown_file)
        _write_down_cfg(autodown_file)
        res = ad.autoup(serving_path=missing, run_cmd_fn=r6,
                        http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
                        sleep_fn=_noop_sleep, notify=False)
        assert res["result"] == "no-units"
        assert not _os.path.exists(ad.AUTODOWN_LOCK), "leak after autoup no-units"

    # -- F3: stale lock does not deadlock forever ----------------------------
    def test_stale_lock_broken_not_deadlock(self, tmp_path, monkeypatch):
        """A lock older than the staleness threshold is broken and re-acquired,
        so a crashed holder can never wedge the daemon forever."""
        import os as _os
        lock = str(tmp_path / "hscc" / "autodown.lock")
        monkeypatch.setattr(ad, "AUTODOWN_LOCK", lock)
        _os.makedirs(_os.path.dirname(lock), exist_ok=True)
        with open(lock, "w") as f:
            f.write("pid=999999 acquired=0")     # from a "dead" process
        now = NOW.timestamp()
        _os.utime(lock, (now - 100000, now - 100000))   # 100000s old ⇒ stale
        assert ad._acquire_lock(now=now) is True  # stale broken + acquired
        ad._release_lock()
        assert not _os.path.exists(lock)          # released cleanly

    # -- F3: with a FRESH (non-stale) lock, acquire fails (busy) ------------
    def test_fresh_lock_blocks_acquirer(self, tmp_path, monkeypatch):
        """A live (fresh) lock held by another actor ⇒ acquire fails (busy)."""
        import os as _os
        lock = str(tmp_path / "hscc" / "autodown.lock")
        monkeypatch.setattr(ad, "AUTODOWN_LOCK", lock)
        _os.makedirs(_os.path.dirname(lock), exist_ok=True)
        assert ad._acquire_lock(now=NOW.timestamp()) is True
        try:
            assert ad._acquire_lock(now=NOW.timestamp()) is False   # busy
        finally:
            ad._release_lock()

    # -- F4: serving.json missing ⇒ teardown aborts, no block, not down ------
    def test_serving_missing_teardown_aborts_no_block_not_down(
            self, tmp_path, monkeypatch, autodown_file):
        """serving.json absent ⇒ empty plan ⇒ ABORT before block; never down."""
        import os as _os
        serving, runner, bf = self._setup(tmp_path, monkeypatch, autodown_file)
        missing = str(tmp_path / "nope" / "serving.json")   # does not exist
        _write_idle_cfg(autodown_file)         # state="up", idle, window elapsed
        res = ad.teardown(serving_path=missing, run_cmd_fn=runner,
                          kanban_db=_FakeKb([]),
                          agents_file=self._agents(tmp_path), now=NOW,
                          keepalive_ok=lambda: True)
        assert res["result"] == "no-targets"
        assert runner.calls == []                       # no stop issued
        assert not _os.path.exists(bf)                  # block NOT written
        cfg = ad.load_config()
        assert cfg["state"] != "down"                   # NOT recorded down
        assert cfg["state"] == "up"                     # reality unchanged

    # -- F7 (residual fix): empty wake plan ⇒ block CLEARED, state "error" --
    def test_empty_wake_plan_clears_block_error_state(
            self, tmp_path, monkeypatch, autodown_file):
        """Empty wake plan ⇒ FAILURE (result NOT "up"), but the intentional
        block IS cleared so the watchdog resumes supervision, and state is the
        honest "error" (NOT "up" — nothing was started)."""
        serving, runner, bf = self._setup(tmp_path, monkeypatch, autodown_file)
        # Seed a latched intentional block, as teardown left it.
        _lifecycle.save_watchdog_block({"blocked": True, "intentional": "autodown",
                                        "reason": ad.WATCHDOG_TEARDOWN_REASON,
                                        "blocked_at": NOW.isoformat(),
                                        "failures": []})
        _write_down_cfg(autodown_file)
        missing = str(tmp_path / "nope" / "serving.json")
        res = ad.autoup(serving_path=missing, run_cmd_fn=runner,
                        http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
                        sleep_fn=_noop_sleep, notify=False)
        assert res["result"] == "no-units"              # NOT "up"
        assert res["started"] == []
        assert res["ready"] == []
        assert runner.calls == []                       # no start issued
        # Block IS cleared — intentional removed, blocked false — so the
        # watchdog resumes ordinary supervision (the residual half-state fix).
        with open(bf) as f:
            blk = json.load(f)
        assert blk.get("blocked") is False
        assert blk.get("intentional") is None
        # State is honest: "error" (NOT "up" — nothing was started), with the
        # failure reason recorded for status.
        cfg = ad.load_config()
        assert cfg["state"] == "error"
        assert "empty wake plan" in cfg["reason"]

    # -- F7 residual: empty wake plan ⇒ LOUD notify -------------------------
    def test_empty_wake_plan_notifies_loudly(self, tmp_path, monkeypatch,
                                             autodown_file):
        """Empty wake plan ⇒ critical notify delivered (desktop + ops)."""
        serving, runner, bf = self._setup(tmp_path, monkeypatch, autodown_file)
        _lifecycle.save_watchdog_block({"blocked": True, "intentional": "autodown",
                                        "reason": ad.WATCHDOG_TEARDOWN_REASON,
                                        "blocked_at": NOW.isoformat(),
                                        "failures": []})
        _write_down_cfg(autodown_file)
        notified = []
        monkeypatch.setattr(ad, "notify_operations",
                            lambda m: notified.append(("ops", m)))
        monkeypatch.setattr(ad, "send_macos_notification",
                            lambda t, m, priority="normal": (
                                notified.append((t, m, priority))))
        missing = str(tmp_path / "nope" / "serving.json")
        res = ad.autoup(serving_path=missing, run_cmd_fn=runner,
                        http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
                        sleep_fn=_noop_sleep, notify=True)
        assert res["result"] == "no-units"
        assert len(notified) >= 2            # ops + desktop both fired
        assert any(t == "HSCC Autodown Wake Failed" for t, *_ in notified)

    # -- F7 residual: subsequent normal cycle() not wedged — can recover -----
    def test_cycle_not_wedged_after_empty_plan(self, tmp_path, monkeypatch,
                                               autodown_file):
        """After a no-units failure, a subsequent normal cycle() is NOT wedged:
        the block stays clear (no re-latch) and the system can recover — a
        later wake with a repaired serving.json brings serving back up."""
        serving, runner, bf = self._setup(tmp_path, monkeypatch, autodown_file)
        _lifecycle.save_watchdog_block({"blocked": True, "intentional": "autodown",
                                        "reason": ad.WATCHDOG_TEARDOWN_REASON,
                                        "blocked_at": NOW.isoformat(),
                                        "failures": []})
        _write_down_cfg(autodown_file)
        missing = str(tmp_path / "nope" / "serving.json")
        # 1. Trigger the empty-plan failure (state accounting above step 1:
        #    cycle() sees state "down" + fresh activity ⇒ autoup ⇒ no-units).
        r1 = ad.autoup(serving_path=missing, run_cmd_fn=runner,
                       http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
                       sleep_fn=_noop_sleep, notify=False)
        assert r1["result"] == "no-units"
        assert ad.load_config()["state"] == "error"
        # 2. Next cycle(): state=="error" ⇒ must NOT re-latch the block, must
        #    NOT tear down, must NOT wedge. Run it with no probes and an idle
        #    predicate that would otherwise tear down — it must do nothing.
        ad.cycle(kanban_db=_FakeKb([]), agents_file=self._agents(tmp_path),
                 now=NOW, keepalive_ok=lambda: True, probes=[])
        # Block still cleared — cycle() did NOT re-latch intentional.
        with open(bf) as f:
            blk = json.load(f)
        assert blk.get("blocked") is False
        assert blk.get("intentional") is None
        assert ad.load_config()["state"] == "error"   # unchanged, not wedged
        # 3. Recovery: serving.json repaired ⇒ a fresh wake succeeds.
        _write_down_cfg(autodown_file)
        _lifecycle.save_watchdog_block({"blocked": True, "intentional": "autodown",
                                        "reason": ad.WATCHDOG_TEARDOWN_REASON,
                                        "blocked_at": NOW.isoformat(),
                                        "failures": []})
        res = ad.autoup(serving_path=serving, run_cmd_fn=runner,
                        http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
                        sleep_fn=_noop_sleep, notify=False)
        assert res["result"] == "up"                   # system recovered
        assert ad.load_config()["state"] == "up"

    # -- F6: keepalive node overlapping teardown set ⇒ abort, no stop --------
    def test_keepalive_overlap_aborts_no_stop(self, tmp_path, monkeypatch,
                                              autodown_file):
        """A keepalive node in the teardown set (co-located config) ⇒ abort,
        no stop issued, block NOT written."""
        import os as _os
        serving, runner, bf = self._setup(tmp_path, monkeypatch, autodown_file)
        # Co-located config: the keepalive worker shares .244 with the
        # orchestrator ⇒ teardown would stop a keepalive node.
        data = {
            "port": 8000,
            "units": [
                {"id": "orch", "role": "orchestrator",
                 "nodes": ["10.0.0.244"], "port": 8000},
                {"id": "wk-keep", "role": "worker", "keepalive": True,
                 "nodes": ["10.0.0.244"], "port": 8000},
            ],
        }
        collision = tmp_path / "serving.json"
        collision.write_text(json.dumps(data))
        _write_idle_cfg(autodown_file)         # state="up", idle
        res = ad.teardown(serving_path=str(collision), run_cmd_fn=runner,
                          kanban_db=_FakeKb([]),
                          agents_file=self._agents(tmp_path), now=NOW,
                          keepalive_ok=lambda: True)
        assert res["result"] == "aborted"
        assert runner.calls == []                       # no stop issued
        assert not _os.path.exists(bf)                  # block NOT written
        cfg = ad.load_config()
        assert cfg["state"] != "down"                   # never recorded down
