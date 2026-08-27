"""Tests for hscc_daemon/autodown.py — Phase 1 (config + core state module).

All tests use tmp paths (monkeypatched AUTODOWN_FILE) and an injected fake
kanban_db — NEVER the real ~/.hscc or ~/.hermes. Everything runs without the
daemon.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

import hscc_daemon.autodown as ad

# The whole-fleet stop command built by the shared builder, scoped to the
# configured cluster. Referenced here (not hardcoded) so these assertions stay
# in sync with serving.fleet_down_cmd().
from hscc_daemon import serving as _serving
FLEET_STOP_CMD = _serving.fleet_down_cmd()


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


@pytest.fixture(autouse=True)
def _no_prci_network(monkeypatch):
    """Default: PR/CI interlock CLEAR so cycle/teardown tests are hermetic.

    ``_is_idle`` now evaluates ``_has_active_pr_ci()``; when no injectable
    checker is passed that falls through to the real ``gh`` screen (which
    issues live subprocess calls). Stubbing the *screen source* to a clear
    result keeps every existing cycle / teardown / autoup / lock test off the
    real GitHub/network and deterministic (the "all clear ⇒ teardown" cases
    need PR/CI positively clear) while leaving the real ``_has_active_pr_ci``
    logic intact — tests that pass an injectable checker or override the
    screen via their own monkeypatch (LIFO teardown wins) still exercise it.
    """
    monkeypatch.setattr(ad, "_screen_prci",
                        lambda *a, **k: {"open_prs": 0, "active_runs": 0,
                                         "repos": []})


class _FakeKb:
    """Fake Hermes kanban library backed by real in-memory sqlite boards.

    Exposes the interface (the new) _has_active_work uses: ``list_boards()``
    plus a board-aware ``connect_closing(board=...)``. ``conn_closed`` records
    that a connection was released (no leaks); ``opened`` records which boards
    were actually opened so tests can assert short-circuiting (it does NOT open
    them all).

    ``__init__`` accepts either a flat list of statuses (→ a single ``default``
    board, the legacy shape) or a dict ``{slug: [statuses...]}`` for
    multi-board scenarios.
    """

    def __init__(self, boards=None):
        self.conn_closed = 0
        self.opened = []
        self._conns = {}
        if boards is None:
            boards = {"default": []}
        if isinstance(boards, (list, tuple)):
            boards = {"default": list(boards)}
        for slug, statuses in boards.items():
            conn = sqlite3.connect(":memory:")
            conn.execute(
                "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)"
            )
            for sid, status in enumerate(statuses):
                conn.execute(
                    "INSERT INTO tasks (id, status) VALUES (?, ?)",
                    (f"{slug}-{sid}", status),
                )
            conn.commit()
            self._conns[slug] = conn

    def list_boards(self):
        return [{"slug": slug} for slug in self._conns]

    @contextmanager
    def connect_closing(self, board=None):
        self.opened.append(board)
        conn = self._conns.get(board) or self._conns.get("default")
        try:
            yield conn
        finally:
            # Real Hermes connect_closing closes the connection; we just mark it
            # so tests can assert no leak, then reopen for re-use.
            self.conn_closed += 1


class _UnreachableKb:
    """kanban lib whose connect raises — exercises the fail-safe True path."""

    @contextmanager
    def connect_closing(self, board=None):
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
# list_active_cron_jobs — Hermes cron source of truth (feat t_2b711a94 §7)
# ---------------------------------------------------------------------------

class TestListActiveCronJobs:
    """list_active_cron_jobs reads Hermes' jobs.json and returns ACTIVE jobs,
    or CRON_UNREADABLE on an unreadable/absent/malformed store — fail-closed,
    so the CLI can abort on 'cannot determine' instead of arming on an
    unverifiable signal."""

    def _write(self, tmp_path, jobs):
        p = tmp_path / "jobs.json"
        p.write_text(json.dumps({"jobs": jobs}))
        return str(p)

    def test_active_jobs_only(self, tmp_path):
        p = self._write(tmp_path, [
            {"id": "a", "name": "active-one", "enabled": True,
             "no_agent": True, "model": None,
             "schedule_display": "0 8 * * *", "next_run_at": "2026-08-26T08:00:00+03:00"},
            {"id": "b", "name": "paused-two", "enabled": False,
             "schedule_display": "0 9 * * *", "next_run_at": "..."},
            {"id": "c", "name": "also-active", "enabled": True,
             "no_agent": False, "model": "gpt-4o",
             "schedule": {"kind": "cron", "expr": "*/15 * * * *",
                          "display": "*/15 * * * *"},
             "next_run_at": "2026-08-26T00:45:00+03:00"},
        ])
        active = ad.list_active_cron_jobs(p)
        assert active == [
            {"id": "a", "name": "active-one", "schedule_display": "0 8 * * *",
             "next_run_at": "2026-08-26T08:00:00+03:00",
             "no_agent": True, "model": None, "cpu_only": True},
            {"id": "c", "name": "also-active", "schedule_display": "*/15 * * * *",
             "next_run_at": "2026-08-26T00:45:00+03:00",
             "no_agent": False, "model": "gpt-4o", "cpu_only": False},
        ]

    def test_absent_file_is_unreadable(self, tmp_path):
        res = ad.list_active_cron_jobs(str(tmp_path / "nope.json"))
        assert res is ad.CRON_UNREADABLE

    def test_corrupt_json_is_unreadable(self, tmp_path):
        p = tmp_path / "jobs.json"
        p.write_text("{ not json")
        assert ad.list_active_cron_jobs(str(p)) is ad.CRON_UNREADABLE

    def test_top_level_not_dict_is_unreadable(self, tmp_path):
        p = tmp_path / "jobs.json"
        p.write_text("[1,2,3]")
        assert ad.list_active_cron_jobs(str(p)) is ad.CRON_UNREADABLE

    def test_jobs_not_list_is_unreadable(self, tmp_path):
        p = tmp_path / "jobs.json"
        p.write_text(json.dumps({"jobs": {"a": 1}}))
        assert ad.list_active_cron_jobs(str(p)) is ad.CRON_UNREADABLE


# ---------------------------------------------------------------------------
# cron_job_is_cpu_only — the model-vs-CPU classification (feat t_c94f8b8c)
# ---------------------------------------------------------------------------

class TestCronJobIsCpuOnly:
    """CPU-only is positively asserted ONLY for no_agent:true AND model:null.
    Everything else (a real model, no_agent false/absent, or unreadable
    fields) is model-requiring — fail-safe, so the CLI aborts on it rather
    than arming on a signal it cannot verify."""

    def test_no_agent_true_model_null_is_cpu_only(self):
        assert ad.cron_job_is_cpu_only(
            {"no_agent": True, "model": None}) is True

    def test_model_present_is_model_requiring(self):
        # A job with a real model needs the GPU — never CPU-only.
        assert ad.cron_job_is_cpu_only(
            {"no_agent": True, "model": "gpt-4o"}) is False
        assert ad.cron_job_is_cpu_only(
            {"no_agent": False, "model": "gpt-4o"}) is False

    def test_no_agent_false_is_model_requiring(self):
        # no_agent:false ⇒ runs an agent ⇒ needs the serving layer.
        assert ad.cron_job_is_cpu_only(
            {"no_agent": False, "model": None}) is False

    def test_ambiguous_missing_fields_is_model_requiring(self):
        # Cannot determine the job's nature ⇒ not CPU-only (fail-safe).
        assert ad.cron_job_is_cpu_only({}) is False
        assert ad.cron_job_is_cpu_only({"no_agent": True}) is False   # model absent
        assert ad.cron_job_is_cpu_only({"model": None}) is False      # no_agent absent

    def test_none_is_model_requiring(self):
        # A null job is not a CPU-side watchdog — never treat as safe.
        assert ad.cron_job_is_cpu_only(None) is False


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

    # -- per-board scanning (§1a): real work lives in per-board DBs, not just
    # the legacy flat DB. _has_active_work must consider ALL boards.

    def test_true_when_live_work_on_a_per_board_db(self):
        """A live card on a NON-default board ⇒ True (this is the core bug)."""
        kb = _FakeKb({"default": ["done", "archived"],
                      "hscc": ["running"],
                      "flosana": ["done"]})
        assert ad._has_active_work(kb) is True
        # The flat/default board alone is quiet — the hit comes from 'hscc'.
        assert ad.kanban_blocking_board() == "hscc"

    def test_true_when_live_work_only_on_flat_db(self):
        """Live work only on the legacy flat ``default`` DB ⇒ True (legacy
        path still works — ``list_boards`` resolves default to the flat DB)."""
        kb = _FakeKb({"default": ["running"], "hscc": ["done"]})
        assert ad._has_active_work(kb) is True
        assert ad.kanban_blocking_board() == "default"

    def test_false_when_all_boards_quiet(self):
        """Every board terminal/parked ⇒ False (genuinely idle)."""
        kb = _FakeKb({"default": ["done"], "hscc": ["archived", "blocked"],
                      "flosana": ["done"]})
        assert ad._has_active_work(kb) is False
        assert ad.kanban_blocking_board() is None

    def test_true_when_one_board_unreadable(self):
        """One unreadable board ⇒ True (fail-safe) + surfaced for status."""
        class _BrokenKb(_FakeKb):
            @contextmanager
            def connect_closing(self, board=None):
                if board == "broken":
                    raise RuntimeError("DB corrupt")
                with super().connect_closing(board=board) as conn:
                    yield conn

        broken = _BrokenKb({"default": ["done"], "hscc": ["done"],
                            "broken": []})
        assert ad._has_active_work(broken) is True
        # The unreadable board is named as the blocker (status also carries
        # the "board 'broken' unreadable" reason via kanban_check_state).
        assert ad.kanban_blocking_board() == "broken"

    def test_many_boards_short_circuit_on_first_hit(self):
        """Many boards ⇒ stops probing at the first live board (does NOT open
        them all)."""
        boards = {f"b{i}": (["done"] if i < 2 else ["done"]) for i in range(10)}
        # Plant live work on the SECOND board so at least the first two open.
        boards["b1"] = ["running"]
        kb = _FakeKb(boards)
        assert ad._has_active_work(kb) is True
        # It opened b0 (quiet) then b1 (hit) and stopped — never reached b2.
        assert kb.opened == ["b0", "b1"]

    def test_unreadable_board_surfaced(self):
        """An unreadable board is surfaced via kanban_check_state (same as the
        unreachable-kanban case) and fails safe to True."""
        class _BadKb(_FakeKb):
            @contextmanager
            def connect_closing(self, board=None):
                if board == "hscc":
                    raise RuntimeError("boom")
                with super().connect_closing(board=board) as conn:
                    yield conn

        bad = _BadKb({"default": ["done"], "hscc": ["done"]})
        assert ad._has_active_work(bad) is True
        state = ad.kanban_check_state()
        assert state is not None and state["ok"] is False
        assert "board 'hscc' unreadable" in state["reason"]


class _PrciClear:
    """Injectable PR/CI checker reporting no open PR / no active CI run."""

    def __call__(self):
        return {"open_prs": 0, "active_runs": 0, "repos": []}


class _PrciOpenPr:
    """Injectable PR/CI checker reporting ONE open PR."""

    def __call__(self):
        return {"open_prs": 1, "active_runs": 0, "repos": ["pom11/hscc"]}


class _PrciCiRunning:
    """Injectable PR/CI checker reporting ONE active CI run."""

    def __call__(self):
        return {"open_prs": 0, "active_runs": 1, "repos": ["pom11/hscc"]}


class _PrciUnreachable:
    """Injectable PR/CI checker reporting the source cannot be reached."""

    def __call__(self):
        return ad._PRCI_UNREACHABLE


class _PrciRaises:
    """Injectable PR/CI checker that RAISES (simulates a broken source)."""

    def __call__(self):
        raise RuntimeError("gh network down")


class _PrciGarbage:
    """Injectable PR/CI checker returning an unreadable shape (fail-safe)."""

    def __call__(self):
        return "definitely-not-a-screen"


class TestHasActivePrCi:
    """``_has_active_pr_ci`` PR/CI idle interlock (§1a extension).

    True ⇒ active ⇒ not idle ⇒ never tear down. False ONLY when the screen is
    POSITIVELY clear. Every failure/unreachable/ambiguous shape fails safe to
    True — identical direction to the kanban interlock.
    """

    def test_open_pr_is_active(self):
        assert ad._has_active_pr_ci(_PrciOpenPr()) is True

    def test_ci_running_is_active(self):
        assert ad._has_active_pr_ci(_PrciCiRunning()) is True

    def test_both_clear_is_idle(self):
        # Both open PRs and active runs at zero ⇒ w.r.t. PR/CI the fleet is
        # idle (the caller's kanban check still runs separately).
        assert ad._has_active_pr_ci(_PrciClear()) is False

    def test_unreachable_failsafe_active(self):
        assert ad._has_active_pr_ci(_PrciUnreachable()) is True

    def test_raising_checker_failsafe_active(self):
        assert ad._has_active_pr_ci(_PrciRaises()) is True

    def test_garbage_shape_failsafe_active(self):
        assert ad._has_active_pr_ci(_PrciGarbage()) is True

    def test_reset_state_between_calls(self):
        # Each call stands alone — a clear then an active must not leak state.
        assert ad._has_active_pr_ci(_PrciClear()) is False
        assert ad._has_active_pr_ci(_PrciOpenPr()) is True
        assert ad._has_active_pr_ci(_PrciClear()) is False


class TestPrciScreenActive:
    """``_prci_screen_active`` normalizes a screen to a bool (fail-safe)."""

    def test_unreachable_is_active(self):
        assert ad._prci_screen_active(ad._PRCI_UNREACHABLE) is True

    def test_open_pr_active(self):
        assert ad._prci_screen_active({"open_prs": 2, "active_runs": 0,
                                       "repos": []}) is True

    def test_ci_running_active(self):
        assert ad._prci_screen_active({"open_prs": 0, "active_runs": 3,
                                       "repos": []}) is True

    def test_clear_is_idle(self):
        assert ad._prci_screen_active({"open_prs": 0, "active_runs": 0,
                                       "repos": []}) is False

    def test_ambiguous_shape_failsafe_active(self):
        assert ad._prci_screen_active("nonsense") is True
        assert ad._prci_screen_active(None) is True
        assert ad._prci_screen_active({"open_prs": "x", "active_runs": 0}) is True


class TestProbePrciActivity:
    """``probe_prci_activity`` stamps ONLY on positively-confirmed activity."""

    def test_stamps_on_open_pr(self, autodown_file):
        assert ad.probe_prci_activity(_PrciOpenPr()) is True
        assert ad.load_config()["wake_source"] == "prci"

    def test_stamps_on_ci_running(self, autodown_file):
        assert ad.probe_prci_activity(_PrciCiRunning()) is True
        assert ad.load_config()["wake_source"] == "prci"

    def test_no_stamp_when_clear(self, autodown_file):
        assert ad.probe_prci_activity(_PrciClear()) is False
        assert ad.load_config()["wake_source"] is None

    def test_no_stamp_when_unreachable(self, autodown_file):
        # Unverifiable source ⇒ no stamp (would fabricate perpetual activity).
        assert ad.probe_prci_activity(_PrciUnreachable()) is False
        assert ad.load_config()["wake_source"] is None

    def test_no_stamp_when_raising(self, autodown_file):
        assert ad.probe_prci_activity(_PrciRaises()) is False
        assert ad.load_config()["wake_source"] is None


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

    def test_waking(self):
        """blocked + intentional autodown + state waking ⇒ waking (distinct
        from expected_down and should_be_up — a normal transition)."""
        block = {"blocked": True, "intentional": "autodown"}
        state = {"state": "waking"}
        assert ad.classify(block, state) == "waking"

    def test_should_be_up_when_block_latched_state_up(self):
        """block latched but state not confirmed down ⇒ should_be_up."""
        block = {"blocked": True, "intentional": "autodown"}
        state = {"state": "up"}
        assert ad.classify(block, state) == "should_be_up"

    def test_should_be_up_when_state_error(self):
        """block latched but wake failed (error) ⇒ should_be_up — NOT a window;
        the layer should be up, so verify must not excuse a fault."""
        block = {"blocked": True, "intentional": "autodown"}
        state = {"state": "error"}
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


class TestIntentionalWindow:
    """intentional_window — the breadth-of-window predicate built on classify():
    True only for the non-fault transition states (expected_down, waking)."""

    def test_window_covers_expected_down_and_waking(self):
        assert ad.intentional_window("expected_down") is True
        assert ad.intentional_window("waking") is True

    def test_window_excludes_should_be_up_and_healthy(self):
        assert ad.intentional_window("should_be_up") is False
        assert ad.intentional_window("healthy") is False
        assert ad.intentional_window(None) is False

    def test_window_matches_classify_for_each_state(self):
        block = {"blocked": True, "intentional": "autodown"}
        for state, verdict in (("down", "expected_down"),
                               ("waking", "waking"),
                               ("up", "should_be_up"),
                               ("error", "should_be_up")):
            got = ad.classify(block, {"state": state})
            assert got == verdict, (state, got)
            # A stream is excused precisely when classify says it's in the
            # intentional window.
            assert ad.intentional_window(got) is (got in ("expected_down", "waking"))


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

    # --- PR/CI interlock (§1a extension) ----------------------------------
    def test_open_pr_blocks_teardown(self, autodown_file, tmp_path, monkeypatch):
        """Open PR on a tracked repo ⇒ active ⇒ no teardown."""
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
            prci_checker=_PrciOpenPr(),  # broken interlock
        )
        assert calls == []

    def test_ci_running_blocks_teardown(
            self, autodown_file, tmp_path, monkeypatch):
        """Active CI run on a tracked repo ⇒ active ⇒ no teardown."""
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
            prci_checker=_PrciCiRunning(),  # broken interlock
        )
        assert calls == []

    def test_prci_clear_falls_through_to_kanban(
            self, autodown_file, tmp_path, monkeypatch):
        """Both PR/CI clear ⇒ predicate falls through to the kanban check.

        Proves PR/CI-clear does NOT short-circuit the rest of the conjunction:
        with PR/CI clear but kanban work active, teardown is still suppressed
        (the kanban interlock is evaluated next); with PR/CI AND kanban clear,
        teardown proceeds. Autodown is only idle when EVERY signal is clear.
        """
        calls = []
        monkeypatch.setattr(ad, "teardown", lambda: calls.append("teardown"),
                 raising=False)
        self._ready(autodown_file)
        agents = self._write_agents(tmp_path, [{"name": "a", "status": "idle"}])
        clear = _PrciClear()
        # (a) PR/CI clear + kanban ACTIVE ⇒ still blocked by kanban work.
        ad.cycle(
            kanban_db=_FakeKb(["running"]),
            agents_file=agents,
            now=NOW,
            keepalive_ok=lambda: True,
            probes=[],
            prci_checker=clear,
        )
        assert calls == []
        # (b) PR/CI clear + kanban clear ⇒ fully idle ⇒ teardown.
        ad.cycle(
            kanban_db=_FakeKb([]),
            agents_file=agents,
            now=NOW,
            keepalive_ok=lambda: True,
            probes=[],
            prci_checker=_PrciClear(),
        )
        assert calls == ["teardown"]

    def test_prci_unreachable_blocks_teardown(
            self, autodown_file, tmp_path, monkeypatch):
        """Unreachable PR/CI source ⇒ active (fail-safe) ⇒ no teardown."""
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
            prci_checker=_PrciUnreachable(),  # fail-safe active
        )
        assert calls == []

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
        """The watchdog block is on disk (intentional) before the fleet stop.

        The fake runner snapshots the block file at each stop; a valid teardown
        must have the intentional autodown block present for every single stop
        issued. With the whole-fleet decision the teardown issues ONE
        ``sparkrun stop --all`` covering every unit, so there is exactly one
        call and it must see the block latched.
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
        assert len(runner.calls) == 1      # the single fleet stop
        for call in runner.calls:
            blk = call["block"]
            assert blk is not None
            assert blk.get("blocked") is True
            assert blk.get("intentional") == "autodown"
            assert blk.get("reason") == ad.WATCHDOG_TEARDOWN_REASON
        # Explicit: the block (with intentional) was saved before the first stop.
        assert runner.calls[0]["block"]["intentional"] == "autodown"

    # -- keepalive units ARE in the whole-fleet stop (C4 reversed) ----------
    def test_fleet_stop_covers_all_units_incl_keepalive(
            self, tmp_path, monkeypatch, autodown_file):
        """The whole-fleet stop uses ``--all`` and covers keepalive units too.

        C4 is reversed: autodown powers the ENTIRE serving layer down, so the
        keepalive unit's nodes (.248) ARE in the set — nothing is exempt.
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
        # Exactly ONE fleet stop, using the accepted `--all` (no TARGET needed).
        assert len(runner.calls) == 1
        assert runner.calls[0]["cmd"] == FLEET_STOP_CMD
        # Plan verifies EVERY unit's head node goes down — including keepalive.
        verify = res["plan"][0]["verify"]
        verify_ids = {v["unit_id"] for v in verify}
        assert verify_ids == {"wk1", "orch", "wk-keep"}
        assert res["issued"][0]["kind"] == "fleet"

    # -- whole-fleet down: one `--all` stop, no per-unit ordering -------------
    def test_teardown_issues_single_fleet_stop(self, tmp_path, monkeypatch,
                                               autodown_file):
        """Teardown issues ONE `sparkrun stop --all` — no per-unit ordering.

        The old orchestrator-last ordering was per-unit; with the whole-fleet
        ``--all`` stop there is a single command covering every unit, so there
        is no per-unit order to assert beyond the one fleet stop.
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
        # The single fleet stop is exactly the shared builder's command.
        assert runner.calls == [{"cmd": FLEET_STOP_CMD,
                                 "block": runner.calls[0]["block"],
                                 "ok": True}]
        assert res["issued"][0]["kind"] == "fleet"
        assert res["issued"][0]["cmd"] == FLEET_STOP_CMD

    # -- each stop carries a recipe TARGET sparkrun accepts -------------------
    def test_stop_cmd_is_whole_fleet_all(self, tmp_path, monkeypatch,
                                         autodown_file):
        """The single stop is ``sparkrun stop --all`` — no per-unit TARGET.

        sparkrun requires a TARGET (recipe or cluster id) or ``--all`` — the
        OLD per-unit form ``sparkrun stop --hosts <nodes>`` failed 100% of the
        time with "Must specify TARGET or --all". The operator decision is a
        WHOLE-FLEET down: one ``sparkrun stop --all`` (built by
        ``serving.fleet_down_cmd()``) that stops EVERY unit, including keepalive
        units — no per-unit recipe TARGET, no keepalive exemption (C4 reversed).
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
        # Exactly one stop using the `--all` form sparkrun accepts.
        assert len(runner.calls) == 1
        assert runner.calls[0]["cmd"] == FLEET_STOP_CMD
        # Plan is the single fleet entry (unit_id "fleet"), whose verify set
        #   covers every unit (orch + wk1 + keepalive wk-keep) for the
        #   verify-down probes.
        assert len(res["plan"]) == 1
        assert res["plan"][0]["unit_id"] == "fleet"
        verify_ids = {v["unit_id"] for v in res["plan"][0]["verify"]}
        assert verify_ids == {"wk1", "orch", "wk-keep"}

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
        """Failure path persists a NON-EMPTY, diagnosable reason into config.

        PART 3: the recorded reason was previously empty because only stdout
        was read and sparkrun's diagnostic went to stderr. The failure reason
        must capture BOTH stdout and stderr so it is diagnosable without a
        manual probe. With the whole-fleet down there is one stop, so a single
        failure on it exercises the capture path.
        """
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file,
                                                  results=[False])

        class _StderrRunner:
            def __call__(self, cmd, timeout=30):
                return {"ok": False, "output": "stdout-noise",
                        "stderr": "sparkrun: Must specify TARGET or --all"}

        res = ad.teardown(
            serving_path=serving, run_cmd_fn=_StderrRunner(),
            kanban_db=_FakeKb([]), agents_file=self._agents(tmp_path),
            now=NOW, keepalive_ok=lambda: True,
            http_check_fn=lambda *a, **k: {"ok": False},
        )
        assert res["result"] == "failed"
        cfg = ad.load_config()
        assert cfg["state"] == "up"
        assert "teardown failed" in cfg["reason"]
        # PART 3: the reason carries the non-empty stderr text (the actual
        # sparkrun diagnostic), not a truncated empty string.
        assert "Must specify TARGET or --all" in cfg["reason"]
        assert "stop command failed" not in cfg["reason"]

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
        """Cancel set after the single fleet stop issues ⇒ teardown completes.

        With the whole-fleet down there is ONE ``sparkrun stop --all`` (no
        per-unit sequence), so once it issues there are no more stops to cancel.
        A cancel_requested that lands after the stop does not un-run it: the
        teardown completes as "down". (Cancel set BEFORE the stop is the
        ``test_cancel_mid_teardown`` path — no stop issued.)
        """
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        # After the (single) fleet stop issues (via runner), set cancel on disk.
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
        # The single fleet stop already issued — result is "down", not "cancelled".
        assert res["result"] == "down"
        assert len(runner.calls) == 1
        assert runner.calls[0]["cmd"] == FLEET_STOP_CMD
        # Block stays latched (autodown proceeded to record down), not rolled back.
        with open(block_file) as f:
            blk = json.load(f)
        assert blk.get("intentional") == "autodown"

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

    def test_stop_command_uses_timeout_well_above_default(
            self, tmp_path, monkeypatch, autodown_file):
        """The fleet `sparkrun stop --all` is invoked with an explicit, generous
        timeout — NOT run_cmd's 30s default (audit of the mirror-image bug: a
        slow graceful stop must not be killed mid-way and misread as a hard
        failure)."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        seen = []

        class _RecordingRunner:
            def __call__(self, cmd, timeout=30):
                seen.append(timeout)
                return {"ok": True, "output": ""}

        res = ad.teardown(
            serving_path=serving, run_cmd_fn=_RecordingRunner(),
            kanban_db=_FakeKb([]), agents_file=self._agents(tmp_path),
            now=NOW, keepalive_ok=lambda: True,
            http_check_fn=lambda *a, **k: {"ok": False},  # ports down
        )
        assert res["result"] == "down"
        assert len(seen) == 1
        # Well above the 30s default (the historic defect threshold).
        assert seen[0] > 30
        assert seen[0] == 180


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

    # -- autoup restores EVERY unit incl. keepalive (C4 reversed) ------------
    def test_starts_every_unit_incl_keepalive(
            self, tmp_path, monkeypatch, autodown_file):
        """autoup starts EVERY serving.json unit — orch, non-keepalive AND
        keepalive workers.

        Fleet down powered the whole layer down (C4 reversed), so fleet up
        restores the whole layer: the keepalive unit (.248) IS started. The
        wake set equals the full serving.json unit set.
        """
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        res = ad.autoup(
            serving_path=serving, run_cmd_fn=runner,
            http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
            sleep_fn=_noop_sleep, notify=False,
        )
        assert res["result"] == "up"
        assert len(runner.calls) == 3      # orch + wk1 + wk-keep
        # Keepalive node IS in a start command now (C4 reversed).
        all_hosts = " ".join(" ".join(c["cmd"]) for c in runner.calls)
        assert "10.0.0.248" in all_hosts      # keepalive restored
        assert "10.0.0.247" in all_hosts      # non-keepalive worker
        assert "10.0.0.244" in all_hosts      # orchestrator
        # Wake set (ready) is the FULL unit set.
        plan_ids = set(res["ready"])
        assert plan_ids == {"wk1", "orch", "wk-keep"}

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

    # -- wake commands carry --served-model-name (alias survive a wake) -----
    def test_wake_cmd_carries_served_model_name_alias(
            self, tmp_path, monkeypatch, autodown_file):
        """Every wake start command CONTAINS --served-model-name with the role
        alias — the regression for t_cbce664b: autodown must bring the fleet
        back the SAME way the sanctioned template path does, so worker-model /
        orchestrator-model survive a wake."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        ad.autoup(
            serving_path=serving, run_cmd_fn=runner,
            http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
            sleep_fn=_noop_sleep, notify=False,
        )
        # Every issued start command must carry --served-model-name.
        for call in runner.calls:
            cmd = call["cmd"]
            assert "--served-model-name" in cmd, cmd
            i = cmd.index("--served-model-name")
            # The alias derived from the unit's ROLE: orchestrator-model on the
            # orchestrator's start, worker-model on every worker start.
            host = cmd[cmd.index("--hosts") + 1]
            if "10.0.0.244" in host:      # orchestrator head node
                assert cmd[i + 1].endswith(" orchestrator-model"), cmd[i + 1]
            else:
                assert cmd[i + 1].endswith(" worker-model"), cmd[i + 1]
        # Orchestrator start explicitly advertises orchestrator-model.
        orch_hosts = runner.calls[0]["cmd"]
        oi = orch_hosts.index("--served-model-name")
        assert "orchestrator-model" in orch_hosts[oi + 1]

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
        # Worker starts (kind == worker) come after the orchestrator.
        kinds = [res["started"][i]["kind"] for i in range(len(runner.calls))]
        assert kinds[0] == "orchestrator"
        assert set(kinds[1:]) == {"worker"}
        # All worker hosts present across the worker start commands.
        all_worker_hosts = " ".join(
            " ".join(runner.calls[i]["cmd"]) for i in range(1, len(runner.calls)))
        assert "10.0.0.247" in all_worker_hosts
        assert "10.0.0.248" in all_worker_hosts

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
        # FAILED wake ⇒ the fleet is NOT confirmed up, so down_since is STILL
        # retained (set iff down/waking — the honesty fix).
        assert cfg["down_since"] is not None
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
        # FAILED wake ⇒ fleet NOT confirmed up ⇒ down_since retained (honest).
        assert cfg["down_since"] is not None
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
        """A clean wake: state=up, wake_source/wake_at/down_since cleared."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        cfg = ad.load_config()
        cfg["wake_source"] = "telegram"   # will be cleared on success
        cfg["wake_at"] = "2026-08-23T09:00:00+00:00"
        cfg["down_since"] = "2026-08-23T07:59:00+00:00"  # stale; cleared on success
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
        assert cfg["down_since"] is None   # the honesty fix — no stale down
        assert cfg["reason"] == ""

    # -- start timeout / slow-launch readiness (the wake-fails fix) ----------
    def test_start_command_uses_timeout_well_above_default(
            self, tmp_path, monkeypatch, autodown_file):
        """Every `sparkrun run` start is invoked with an explicit timeout well
        above run_cmd's 30s default (the wake-fails incident: a 40s+ launch
        phase was killed at 30s). Sourced from VLLM_LOAD_GRACE_MINUTES (def 20
        ⇒ 1200s)."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)

        seen = []

        class _RecordingRunner:
            def __call__(self, cmd, timeout=30):
                seen.append(timeout)
                return {"ok": True, "output": ""}

        res = ad.autoup(
            serving_path=serving, run_cmd_fn=_RecordingRunner(),
            http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
            sleep_fn=_noop_sleep, notify=False,
        )
        assert res["result"] == "up"
        assert len(seen) == 3                     # one start per unit
        # Every start got the grace-scaled timeout, NOT the 30s default.
        assert all(t >= _lifecycle.VLLM_LOAD_GRACE_MINUTES * 60 for t in seen)

    def test_slow_launch_but_becomes_ready_succeeds(
            self, tmp_path, monkeypatch, autodown_file):
        """A start that takes its time but whose units eventually answer healthy
        ⇒ wake SUCCEEDS. Readiness, not process exit, is the success signal —
        the command never errors (ok), readiness just takes a few poll rounds.
        This is exactly the case the 30s start-timeout used to kill."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        # Readiness probe returns ok on the 2nd round (slow-but-successful
        # launch), within a generous grace window. NO start ever fails.
        probes = {"n": 0}

        def _slow_healthy(url, timeout=5):
            probes["n"] += 1
            return {"ok": probes["n"] > 1, "status": 200}

        res = ad.autoup(
            serving_path=serving, run_cmd_fn=runner,
            http_check_fn=_slow_healthy, clock=_AdvancingClock(start=0, step=1),
            sleep_fn=_noop_sleep, notify=False,
        )
        assert res["result"] == "up"
        assert set(res["ready"]) == {"orch", "wk1", "wk-keep"}
        # Block cleared + state up (readiness confirmed ⇒ success).
        cfg = ad.load_config()
        assert cfg["state"] == "up"
        with open(block_file) as f:
            blk = json.load(f)
        assert blk.get("blocked") is False
        assert blk.get("intentional") is None

    def test_start_timeout_does_not_abort_before_readiness(
            self, tmp_path, monkeypatch, autodown_file):
        """The pipe is NOT 'start must succeed within N seconds or the whole
        wake aborts'. Even with wake_grace_minutes=0 (readiness window forced
        to zero), the START commands still run with the full grace-scaled
        timeout and the wake degrades to the honest readiness-timeout
        failure path — it never prematurely reports the starts themselves as
        failed on the 30s default."""
        serving, runner, block_file = self._setup(tmp_path, monkeypatch,
                                                  autodown_file)
        seen = []

        class _RecordingRunner:
            def __call__(self, cmd, timeout=30):
                seen.append(timeout)
                return {"ok": True, "output": ""}

        res = ad.autoup(
            serving_path=serving, run_cmd_fn=_RecordingRunner(),
            http_check_fn=_DownProbe(), clock=_AdvancingClock(start=0, step=1),
            sleep_fn=_noop_sleep, wake_grace_minutes=0, notify=False,
        )
        # Readiness window is 0 ⇒ readiness timeout, NOT start failure.
        assert res["result"] == "not-ready"
        # But every start still used the full grace-scaled timeout (>30s).
        assert len(seen) == 3
        assert all(t >= _lifecycle.VLLM_LOAD_GRACE_MINUTES * 60 for t in seen)

    # -- round trip: teardown() then autoup() -------------------------------
    def test_round_trip_teardown_then_autoup(self, tmp_path, monkeypatch,
                                             autodown_file):
        """teardown() then autoup() with the SAME fixture returns the cluster
        to the starting unit set: the whole fleet goes down and comes fully
        back up, and end state is up with the block cleared."""
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
        # One whole-fleet stop, using `--all` (the shared builder's command).
        assert down_runner.calls == [{"cmd": FLEET_STOP_CMD,
                                      "block": down_runner.calls[0]["block"],
                                      "ok": True}]
        # The teardown plan's verify set covers EVERY unit incl. keepalive.
        all_down_units = {v["unit_id"]
                          for v in stop_result["plan"][0]["verify"]}
        assert all_down_units == {"wk1", "orch", "wk-keep"}

        # Now wake with the same serving fixture. Readiness healthy immediately.
        up_runner = _FakeRunner(block_file)
        up_result = ad.autoup(
            serving_path=serving, run_cmd_fn=up_runner,
            http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
            sleep_fn=_noop_sleep, notify=False,
        )
        assert up_result["result"] == "up"
        # The ENTIRE serving layer is restored — orch + keepalive + workers.
        assert set(up_result["ready"]) == {"wk1", "orch", "wk-keep"}
        assert len(up_runner.calls) == 3      # one start per unit
        # End state: up + block cleared.
        assert ad.load_config()["state"] == "up"
        end_cfg = ad.load_config()
        assert end_cfg["down_since"] is None  # cleared after successful wake
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

    def _cfg(self, autodown_file, last_activity_iso, monkeypatch):
        """Write an enabled, DOWN config with the given last_activity_iso."""
        # Hermetic: the reconcile probe reports the layer NOT up and the streak
        # is reset, so every state=down cycle here only exercises the wake seam
        # regardless of the live fleet's up/down (no real-cluster read, no
        # cross-test contamination).
        monkeypatch.setattr(ad, "_reconcile_up_fn", lambda **kw: False)
        ad._reconcile_up_streak = 0
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
        self._cfg(autodown_file, fresh.isoformat(), monkeypatch)
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
        self._cfg(autodown_file, down.isoformat(), monkeypatch)  # == down_since
        ad.cycle(probes=[])
        assert calls == []

    def test_down_no_last_activity_does_not_trigger(
            self, autodown_file, monkeypatch):
        """state=down with NULL last_activity_iso ⇒ can't verify fresh activity
        ⇒ autoup NOT called (fail-safe)."""
        calls = []
        monkeypatch.setattr(ad, "autoup", lambda: calls.append("autoup"),
                            raising=False)
        self._cfg(autodown_file, None, monkeypatch)
        ad.cycle(probes=[])
        assert calls == []

    def test_waking_does_not_trigger(self, autodown_file, monkeypatch):
        """state=waking with a LIVE lock holder (a wake genuinely in flight)
        ⇒ autoup NOT called — a real wake must never be doubled or interrupted.
        (With the §8 stalled-wake fix, holder liveness distinguishes in-flight
        from stalled: a LIVE holder is always left alone across the debounce.)"""
        calls = []
        monkeypatch.setattr(ad, "autoup", lambda: calls.append("autoup"),
                            raising=False)
        # A LIVE holder = an authentic in-flight wake. Reset the stall counter
        # so this test is hermetic regardless of prior tests' debounce state.
        ad._wake_stall_streak = 0
        monkeypatch.setattr(ad, "_lock_holder_alive", lambda: True)
        down = NOW - _dt.timedelta(minutes=30)
        self._cfg(autodown_file, down.isoformat(), monkeypatch)
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
        self._cfg(autodown_file, fresh.isoformat(), monkeypatch)
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
        self._cfg(autodown_file, down.isoformat(), monkeypatch)
        cfg = ad.load_config()
        cfg["state"] = "up"
        ad.save_config(cfg)
        # Even if last_activity > down_since, state=up never auto-wakes.
        ad.cycle(kanban_db=_FakeKb([]), agents_file="",
                 now=NOW, keepalive_ok=lambda: True, probes=[])
        assert calls == []


# ---------------------------------------------------------------------------
# §8 Fix 2 — reconcile-to-reality: state must self-describe what is running
# ---------------------------------------------------------------------------

class TestReconcileToReality:
    """SAFETY BLOCKER (Fix 2): even with check_workers gated, a recorded "down"
    state must NOT contradict a serving layer that is actually UP (§8 forbids
    the silent half-state). When state==down but the orchestrator head probes
    healthy across the debounce, cycle() reconciles: clears the intentional
    block (so the watchdog supervises what is running) and sets state=up, loud.

    The "actually up" definition is documented in _serving_actually_up: the
    ORCHESTRATOR unit's head node answers healthy — the definitive serving head
    (and the one unit neither check_workers nor check_proxy auto-keeps alive).
    """

    def _down_cfg(self, autodown_file):
        down = "2026-01-01T00:00:00+00:00"
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["enabled"] = True
        cfg["state"] = "down"
        cfg["idle_minutes"] = 10
        cfg["down_since"] = down
        cfg["last_activity_iso"] = down   # no fresh activity ⇒ no wake seam
        ad.save_config(cfg)
        return cfg

    # ── the "actually up" predicate (the definition we chose + documented) ──
    def test_actually_up_true_when_orch_up(self, tmp_path, monkeypatch):
        """Orchestrator head probe ok ⇒ the layer is actually up."""
        serving_path = _write_serving(tmp_path)
        monkeypatch.setattr(ad, "_util_http_check_probe",
                            lambda: _HealthyProbe())
        assert ad._serving_actually_up(serving_path=serving_path) is True

    def test_actually_up_false_when_orch_down(self, tmp_path, monkeypatch):
        """Orchestrator head probe not-ok ⇒ the layer is NOT actually up (a
        real drain may still be in progress)."""
        serving_path = _write_serving(tmp_path)
        monkeypatch.setattr(ad, "_util_http_check_probe", lambda: _DownProbe())
        assert ad._serving_actually_up(serving_path=serving_path) is False

    def test_actually_up_false_when_no_serving(self, tmp_path):
        """Missing serving.json / no orchestrator unit ⇒ NOT confidently up
        (do not reconcile on an unverifiable signal)."""
        missing = str(tmp_path / "nope" / "serving.json")
        assert ad._serving_actually_up(serving_path=missing) is False

    # ── debounce + reconcile via the real cycle() ──────────────────────────
    def test_down_reconciles_to_up_when_layer_actually_up(
            self, autodown_file, tmp_path, monkeypatch):
        """state=down but the layer probes UP across the debounce ⇒ reconcile:
        clear intentional block, set state=up, clear down_since, log loudly."""
        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        monkeypatch.setattr(ad, "notify_operations", lambda *a, **k: True)
        monkeypatch.setattr(ad, "send_macos_notification", lambda *a, **k: True)
        monkeypatch.setattr(ad, "autoup", lambda: None, raising=False)
        self._down_cfg(autodown_file)
        # latch an intentional block, as a real teardown leaves behind
        _lifecycle.save_watchdog_block({"blocked": True, "intentional": "autodown",
                                        "reason": ad.WATCHDOG_TEARDOWN_REASON})
        # The layer is ACTUALLY up — simulate something external starting it.
        monkeypatch.setattr(ad, "_reconcile_up_fn", lambda **kw: True)
        ad._reconcile_up_streak = 0

        # First (DEBOUNCE-1) down-cycles: still recording the sustained-up signal,
        # no reconcile yet — state stays down, block stays latched.
        for _ in range(ad.RECONCILE_UP_DEBOUNCE - 1):
            ad.cycle(probes=[])
            assert ad.load_config()["state"] == "down"
        # Nth consecutive up-cycle: reconcile fires to reality.
        ad.cycle(probes=[])
        cfg = ad.load_config()
        assert cfg["state"] == "up"
        assert cfg["down_since"] is None
        # intentional block cleared so the watchdog supervises what is running
        blk = json.loads(Path(block_file).read_text())
        assert blk.get("blocked") is False
        assert blk.get("intentional") is None
        assert "reconciled" in blk.get("reason", "").lower() or \
            "actually up" in blk.get("reason", "").lower()

    def test_reconcile_not_fire_while_drain_still_in_progress(
            self, autodown_file, tmp_path, monkeypatch):
        """reconcile does NOT fire while a teardown is still draining: if the
        layer flips up once then back down, the debounce never accumulates —
        state stays honestly down (a real drain or a flaky one-off up)."""
        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        monkeypatch.setattr(ad, "notify_operations", lambda *a, **k: True)
        monkeypatch.setattr(ad, "send_macos_notification", lambda *a, **k: True)
        monkeypatch.setattr(ad, "autoup", lambda: None, raising=False)
        self._down_cfg(autodown_file)
        _lifecycle.save_watchdog_block({"blocked": True, "intentional": "autodown",
                                        "reason": ad.WATCHDOG_TEARDOWN_REASON})
        # Flaky probe: up once, then down (a slow-draining stop that briefly
        # answered) — simulate by toggling _reconcile_up_fn between cycles.
        state = {"up": True}
        def flaky(**kw):
            return state["up"]
        monkeypatch.setattr(ad, "_reconcile_up_fn", flaky)
        ad._reconcile_up_streak = 0

        # cycle 1: up (streak 1) — never crosses debounce alone
        ad.cycle(probes=[])
        # now it drains back down before the next cycle
        state["up"] = False
        # cycle 2 (and several more): all down ⇒ streak resets to 0
        for _ in range(ad.RECONCILE_UP_DEBOUNCE * 2):
            ad.cycle(probes=[])
        # Never reconciled — state stays honestly down, block stays latched.
        assert ad.load_config()["state"] == "down"
        blk = json.loads(Path(block_file).read_text())
        assert blk.get("intentional") == "autodown"

    def test_reconcile_not_fire_while_waking(self, autodown_file, tmp_path,
                                             monkeypatch):
        """reconcile does NOT fire while a wake is legitimately in progress:
        state=waking returns from cycle() BEFORE the reconcile probe runs."""
        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        monkeypatch.setattr(ad, "notify_operations", lambda *a, **k: True)
        monkeypatch.setattr(ad, "send_macos_notification", lambda *a, **k: True)
        monkeypatch.setattr(ad, "autoup", lambda: None, raising=False)
        self._down_cfg(autodown_file)
        _lifecycle.save_watchdog_block({"blocked": True, "intentional": "autodown",
                                        "reason": ad.WATCHDOG_TEARDOWN_REASON})
        cfg = ad.load_config()
        cfg["state"] = "waking"
        ad.save_config(cfg)
        # Even a layer that is actually up must NOT reconcile while waking.
        monkeypatch.setattr(ad, "_reconcile_up_fn", lambda **kw: True)
        ad._reconcile_up_streak = 0
        ad.cycle(probes=[])
        assert ad.load_config()["state"] == "waking"   # untouched
        blk = json.loads(Path(block_file).read_text())
        assert blk.get("intentional") == "autodown"     # block not cleared

    def test_reconcile_logs_loudly(self, autodown_file, tmp_path, monkeypatch):
        """A reconciliation is logged at ERROR (loud) — the surprise up is the
        silent half-state §8 forbids, so it must be unmissable."""
        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        monkeypatch.setattr(ad, "notify_operations", lambda *a, **k: True)
        monkeypatch.setattr(ad, "send_macos_notification", lambda *a, **k: True)
        self._down_cfg(autodown_file)
        _lifecycle.save_watchdog_block({"blocked": True, "intentional": "autodown"})
        logs = []
        monkeypatch.setattr(ad, "log",
                            lambda msg, level="INFO": logs.append((msg, level)))
        monkeypatch.setattr(ad, "_reconcile_up_fn", lambda **kw: True)
        ad._reconcile_up_streak = 0
        for _ in range(ad.RECONCILE_UP_DEBOUNCE):
            ad.cycle(probes=[])
        assert any("RECONCILED" in m and l == "ERROR" for m, l in logs)


# ---------------------------------------------------------------------------
# §8 Fix 2 (extended to waking) — stalled-wake recovery
# ---------------------------------------------------------------------------
# The reproduced defect: a wake process KILLED mid-wake leaves state=waking, a
# dead holder PID in the leaked lock, and the intentional block latched forever
# while the fleet is actually up. No live lock holder ⇒ nothing is progressing.
# After a bounded debounce, cycle() must act — reconcile to up if the units are
# actually healthy, else resume the wake (autoup is idempotent). A LIVE holder
# is the "in-flight" signal and must be left alone. Tests drive liveness via
# monkeypatched _lock_holder_alive and reality via _reconcile_up_fn — never a
# real cluster read and never a real telegram/sparkrun.

class TestStalledWakeRecovery:
    """cycle() recovers a STALLED wake (state=waking, no live lock holder)."""

    def _cfg(self, autodown_file):
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["enabled"] = True
        cfg["state"] = "waking"
        cfg["idle_minutes"] = 10
        cfg["down_since"] = (NOW - _dt.timedelta(minutes=30)).isoformat()
        ad.save_config(cfg)
        return cfg

    def _setup(self, autodown_file, tmp_path, monkeypatch):
        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        monkeypatch.setattr(ad, "notify_operations", lambda *a, **k: True)
        monkeypatch.setattr(ad, "send_macos_notification", lambda *a, **k: True)
        self._cfg(autodown_file)
        _lifecycle.save_watchdog_block(
            {"blocked": True, "intentional": "autodown",
             "reason": ad.WATCHDOG_TEARDOWN_REASON,
             "blocked_at": NOW.isoformat(), "failures": []})
        # Reset both debounce counters so each test starts clean.
        ad._wake_stall_streak = 0
        ad._reconcile_up_streak = 0
        return block_file

    def _run_stalled_cycles(self):
        """Run WAKE_STALL_DEBOUNCE waking cycles with no live holder so the
        stall debounce trips and _handle_stalled_wake fires."""
        for _ in range(ad.WAKE_STALL_DEBOUNCE):
            ad.cycle(probes=[])

    # -- state=waking + DEAD holder + units HEALTHY ⇒ reconcile to up --------
    def test_stalled_wake_units_healthy_reconciles_to_up(
            self, autodown_file, tmp_path, monkeypatch):
        """state=waking, dead lock holder, units ACTUALLY healthy ⇒ reconcile:
        clear the intentional block, set state=up, clear down_since."""
        block_file = self._setup(autodown_file, tmp_path, monkeypatch)
        monkeypatch.setattr(ad, "_lock_holder_alive", lambda: False)
        monkeypatch.setattr(ad, "_reconcile_up_fn", lambda **kw: True)
        ap_calls = []
        monkeypatch.setattr(ad, "autoup",
                            lambda: ap_calls.append("autoup"), raising=False)

        self._run_stalled_cycles()

        assert ap_calls == []                 # healthy ⇒ reconcile, NOT autoup
        cfg = ad.load_config()
        assert cfg["state"] == "up"
        assert cfg["down_since"] is None
        blk = json.loads(Path(block_file).read_text())
        assert blk.get("blocked") is False    # block cleared — watchdog resumes
        assert blk.get("intentional") is None
        assert "stalled wake" in blk.get("reason", "")

    # -- state=waking + DEAD holder + units DOWN ⇒ wake resumed --------------
    def test_stalled_wake_units_down_resumes_autoup(
            self, autodown_file, tmp_path, monkeypatch):
        """state=waking, dead lock holder, units NOT healthy ⇒ resume the wake
        (idempotent autoup), do NOT reconcile to a false up."""
        self._setup(autodown_file, tmp_path, monkeypatch)
        monkeypatch.setattr(ad, "_lock_holder_alive", lambda: False)
        monkeypatch.setattr(ad, "_reconcile_up_fn", lambda **kw: False)
        ap_calls = []
        monkeypatch.setattr(ad, "autoup",
                            lambda: ap_calls.append("autoup"), raising=False)

        self._run_stalled_cycles()

        assert ap_calls == ["autoup"]         # interrupted wake resumed
        # State stays waking — the resumed wake owns the transition to up.
        assert ad.load_config()["state"] == "waking"

    # -- state=waking + LIVE holder ⇒ left alone (no double wake) ------------
    def test_stalled_wake_live_holder_left_alone(
            self, autodown_file, tmp_path, monkeypatch):
        """state=waking with a LIVE lock holder (an authentic in-flight wake)
        ⇒ cycle() leaves it alone across the debounce: no reconcile, no autoup
        — a legitimate wake must never be interrupted or doubled."""
        block_file = self._setup(autodown_file, tmp_path, monkeypatch)
        monkeypatch.setattr(ad, "_lock_holder_alive", lambda: True)
        monkeypatch.setattr(ad, "_reconcile_up_fn", lambda **kw: True)
        ap_calls = []
        monkeypatch.setattr(ad, "autoup",
                            lambda: ap_calls.append("autoup"), raising=False)

        for _ in range(ad.WAKE_STALL_DEBOUNCE * 2):
            ad.cycle(probes=[])

        assert ap_calls == []                 # never doubled
        assert ad.load_config()["state"] == "waking"   # never reconciled
        blk = json.loads(Path(block_file).read_text())
        assert blk.get("intentional") == "autodown"    # block never cleared
        assert ad._wake_stall_streak == 0      # live holder resets the counter

    # -- debounce: a single no-holder waking cycle does NOT act immediately --
    def test_single_no_holder_cycle_does_not_act(
            self, autodown_file, tmp_path, monkeypatch):
        """One waking cycle with no live holder does NOT act yet — the debounce
        must be sustained (a just-transitioning wake is never misread)."""
        block_file = self._setup(autodown_file, tmp_path, monkeypatch)
        monkeypatch.setattr(ad, "_lock_holder_alive", lambda: False)
        monkeypatch.setattr(ad, "_reconcile_up_fn", lambda **kw: True)
        ap_calls = []
        monkeypatch.setattr(ad, "autoup",
                            lambda: ap_calls.append("autoup"), raising=False)

        ad.cycle(probes=[])   # one cycle only
        assert ap_calls == []
        assert ad.load_config()["state"] == "waking"
        blk = json.loads(Path(block_file).read_text())
        assert blk.get("intentional") == "autodown"    # not yet cleared


# ---------------------------------------------------------------------------
# §8 mirror reconcile — STALE intentional block (state not down/waking)
# ---------------------------------------------------------------------------
# The reproduced mirror wedge: an interrupted wake (daemon restarted mid-autoup,
# or a teardown/wake cycle that never unwound) left ``state: up`` (or error)
# while the watchdog block is still latched with ``intentional: "autodown"``.
# Nothing cleared it — the existing two reconcile paths only fire on ``down``
# and on a stalled ``waking``. Reconciliation must be driven by the
# CONTRADICTION (block intentional × state not down/waking), not by one state;
# a LEGITIMATE in-flight teardown/wake (state down/waking, live holder) must
# be left untouched. Tests drive state + block directly via tmp files — never a
# real cluster, never a real telegram/sparkrun.

class TestReconcileStaleIntentionalBlock:
    """cycle() / resume_from_restart() clear a STALE intentional block."""

    def _cfg(self, autodown_file, state="up"):
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["enabled"] = True
        cfg["state"] = state
        cfg["idle_minutes"] = 10
        cfg["down_since"] = None
        cfg["last_activity_iso"] = (NOW - _dt.timedelta(hours=2)).isoformat()
        ad.save_config(cfg)
        return cfg

    def _latch_block(self, tmp_path, monkeypatch, intentional="autodown"):
        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        monkeypatch.setattr(ad, "notify_operations", lambda *a, **k: True)
        monkeypatch.setattr(ad, "send_macos_notification", lambda *a, **k: True)
        block = {"blocked": True, "reason": ad.WATCHDOG_TEARDOWN_REASON,
                 "blocked_at": NOW.isoformat(), "failures": []}
        if intentional is not None:
            block["intentional"] = intentional
        _lifecycle.save_watchdog_block(block)
        return block_file

    def _read_block(self, block_file):
        return json.loads(Path(block_file).read_text())

    # -- the mirror case: state=up + block intentional ⇒ cleared, logged -----
    def test_up_with_latched_intentional_block_cleared_and_logged(
            self, autodown_file, tmp_path, monkeypatch):
        """state=up + watchdog block latched intentional=autodown (the
        reproduced interrupted-wake wedge) ⇒ clear the stale block, restore
        supervision, log loudly at ERROR. cycle() must reconcile even when the
        layer is NOT idle (no teardown fires — the stale block is the bug)."""
        block_file = self._latch_block(tmp_path, monkeypatch)
        self._cfg(autodown_file, state="up")
        logs = []
        monkeypatch.setattr(ad, "log",
                            lambda msg, level="INFO": logs.append((msg, level)))
        # Not idle AND teardown is a no-op — we only want the reconcile side
        # effects, not a teardown.
        monkeypatch.setattr(ad, "_is_idle", lambda *a, **k: False)
        monkeypatch.setattr(ad, "_invoke_teardown", lambda: None, raising=False)

        ad.cycle(probes=[])

        blk = self._read_block(block_file)
        assert blk.get("blocked") is False          # watchdog supervises again
        assert blk.get("intentional") is None       # staleness cleared
        assert "stale" in blk.get("reason", "").lower()
        assert any("RECONCILED" in m and l == "ERROR" for m, l in logs)

    # -- state=up + block NOT intentional (real breaker) ⇒ never cleared ------
    def test_non_intentional_block_never_cleared_by_reconcile(
            self, autodown_file, tmp_path, monkeypatch):
        """NEGATIVE CONTROL: a real breaker latch (blocked: true, NO autodown
        intentional marker) must NEVER be cleared by reconcile — that is the
        watchdog's latched fault and clearing it would re-open a downed unit."""
        block_file = self._latch_block(tmp_path, monkeypatch,
                                       intentional=None)
        self._cfg(autodown_file, state="up")
        monkeypatch.setattr(ad, "_is_idle", lambda *a, **k: False)
        monkeypatch.setattr(ad, "_invoke_teardown", lambda: None, raising=False)

        ad.cycle(probes=[])

        blk = self._read_block(block_file)
        assert blk.get("blocked") is True           # untouched
        assert blk.get("intentional") is None

    # -- state=waking + LIVE holder ⇒ NEVER cleared (legit in-flight) ---------
    def test_waking_with_live_holder_keeps_block(self, autodown_file, tmp_path,
                                                 monkeypatch):
        """state=waking + LIVE lock holder ⇒ a LEGITIMATE in-flight wake; the
        block must be KEPT (the new contradiction reconcile must NOT touch it —
        state IS in an intentional window)."""
        block_file = self._latch_block(tmp_path, monkeypatch)
        cfg = self._cfg(autodown_file, state="waking")
        cfg["down_since"] = (NOW - _dt.timedelta(minutes=5)).isoformat()
        ad.save_config(cfg)
        monkeypatch.setattr(ad, "_lock_holder_alive", lambda: True)

        for _ in range(ad.WAKE_STALL_DEBOUNCE * 2):
            ad.cycle(probes=[])

        blk = self._read_block(block_file)
        assert blk.get("blocked") is True           # still latched
        assert blk.get("intentional") == "autodown" # not cleared
        assert ad.load_config()["state"] == "waking"

    # -- state=down (legit teardown) ⇒ block KEPT -----------------------------
    def test_down_legitimate_teardown_keeps_block(self, autodown_file,
                                                  tmp_path, monkeypatch):
        """state=down is a LEGITIMATE intentional teardown — the block must be
        KEPT. The contradiction reconcile does NOT run for state=down (that
        window is the existing _reconcile_if_actually_up path, which only clears
        when the layer probes ACTUALLY up). Here the layer is genuinely down
        (probe False) ⇒ block stays."""
        block_file = self._latch_block(tmp_path, monkeypatch)
        cfg = self._cfg(autodown_file, state="down")
        cfg["down_since"] = (NOW - _dt.timedelta(minutes=5)).isoformat()
        ad.save_config(cfg)
        # Layer NOT actually up — a real drain is in progress.
        monkeypatch.setattr(ad, "_reconcile_up_fn", lambda **kw: False)
        ad._reconcile_up_streak = 0

        for _ in range(ad.RECONCILE_UP_DEBOUNCE * 2):
            ad.cycle(probes=[])

        blk = self._read_block(block_file)
        assert blk.get("blocked") is True
        assert blk.get("intentional") == "autodown"
        assert ad.load_config()["state"] == "down"

    # -- existing direction still works: state=down, layer up ⇒ reconciles ----
    def test_down_layer_actually_up_still_reconciles(self, autodown_file,
                                                     tmp_path, monkeypatch):
        """The EXISTING reconcile direction must keep working: state=down but
        the layer probes ACTUALLY up across the debounce ⇒ reconcile to up
        (clear block, set state=up). The new code must not regress it."""
        block_file = self._latch_block(tmp_path, monkeypatch)
        self._cfg(autodown_file, state="down")
        cfg = ad.load_config()
        cfg["down_since"] = (NOW - _dt.timedelta(minutes=5)).isoformat()
        ad.save_config(cfg)
        monkeypatch.setattr(ad, "_reconcile_up_fn", lambda **kw: True)
        ad._reconcile_up_streak = 0

        for _ in range(ad.RECONCILE_UP_DEBOUNCE):
            ad.cycle(probes=[])

        assert ad.load_config()["state"] == "up"
        blk = self._read_block(block_file)
        assert blk.get("blocked") is False
        assert blk.get("intentional") is None

    # -- resume_from_restart: state=up + block intentional ⇒ cleared ----------
    def test_resume_from_restart_up_with_latched_block_cleared(
            self, autodown_file, tmp_path, monkeypatch):
        """resume_from_restart with state=up + block latched intentional (the
        interrupted-wake-on-restart trigger) ⇒ the stale block is cleared so a
        restart cannot leave the watchdog suppressed."""
        block_file = self._latch_block(tmp_path, monkeypatch)
        self._cfg(autodown_file, state="up")
        up_calls = []
        monkeypatch.setattr(ad, "autoup",
                            lambda: up_calls.append("autoup"), raising=False)

        ad.resume_from_restart()

        assert up_calls == []            # no wake (nothing to wake)
        blk = self._read_block(block_file)
        assert blk.get("blocked") is False
        assert blk.get("intentional") is None
        assert ad.load_config()["state"] == "up"   # unchanged


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

    def test_captures_trigger_text(self, tmp_path, autodown_file):
        """On a fresh marker the probe ALSO captures the msg= text of the
        triggering message into wake_trigger_text (first ~120 chars), so the
        wake-complete notice can quote it (§4)."""
        gw, off = self._setup(tmp_path)
        with open(gw, "w") as f:
            f.write("seed\n")
        ad.save_config(dict(ad.DEFAULT_CONFIG))
        ad.probe_telegram_activity(gw, off)   # baseline

        long_body = "wake the cluster up please " + ("x" * 200)
        with open(gw, "a") as f:
            f.write(ad.TELEGRAM_MARKER +
                    " ... msg='" + long_body + "' ts=2026-08-25\n")
        assert ad.probe_telegram_activity(gw, off) is True
        loaded = ad.load_config()
        # Truncated to the first 120 chars (the trigger text, not the whole
        # line, and no msg= / quote delimiters).
        assert loaded["wake_trigger_text"] == long_body[:120]
        assert "'" not in loaded["wake_trigger_text"]
        assert "msg=" not in loaded["wake_trigger_text"]

    def test_capture_survives_marker_without_msg_field(
            self, tmp_path, autodown_file):
        """A marker line with NO msg= field doesn't crash and does not clobber
        an existing captured text (nothing to quote ⇒ not an empty quote)."""
        gw, off = self._setup(tmp_path)
        with open(gw, "w") as f:
            f.write("seed\n")
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["wake_trigger_text"] = "older text still waiting"
        ad.save_config(cfg)
        ad.probe_telegram_activity(gw, off)   # baseline
        with open(gw, "a") as f:
            f.write(ad.TELEGRAM_MARKER + " some inbound without msg field\n")
        assert ad.probe_telegram_activity(gw, off) is True
        # Existing captured text is left untouched (not overwritten / cleared).
        assert ad.load_config()["wake_trigger_text"] == "older text still waiting"

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
        # Hermetic: reconcile probe reports NOT up so this only exercises the
        # wake seam (no real-cluster read, no cross-test contamination).
        monkeypatch.setattr(ad, "_reconcile_up_fn", lambda **kw: False)
        ad._reconcile_up_streak = 0
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


# ---------------------------------------------------------------------------
# §4 — telegram wake notices (the wake-triggering message is NOT processed)
# ---------------------------------------------------------------------------
# The gateway CONSUMES the wake-triggering Telegram message on arrival while the
# model is still loading, so it is never answered; the operator must re-send it.
# autodown notifies the operator: a "waking" notice at wake-trigger time (the
# message will NOT be processed) and, on wake complete, an "up" notice that
# QUOTES the triggering message so the operator can re-send without hunting. It
# does NOT auto-replay the message (§4).
class TestWakeNotices:
    """The two telegram wake notices fire exactly when they should (§4):
    waking-notice once at wake-trigger, up-notice with a quote at wake complete.
    Never for CLI/HTTP/kanban triggers, never when disabled, never a false
    up-notice on a failed wake. Uses injected fakes — no real telegram sent.
    """

    def _setup(self, tmp_path, monkeypatch, autodown_file):
        """Wire a wakeable DOWN config + fakes and capture telegram ops notes."""
        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        serving = _write_serving(tmp_path)
        _lifecycle.save_watchdog_block(
            {"blocked": True, "intentional": "autodown",
             "reason": ad.WATCHDOG_TEARDOWN_REASON,
             "blocked_at": NOW.isoformat(), "failures": []})
        tg_notes = []
        monkeypatch.setattr(ad, "notify_operations",
                            lambda msg, *a, **k: tg_notes.append(str(msg)))
        monkeypatch.setattr(ad, "send_macos_notification", lambda *a, **k: None)
        _write_down_cfg(autodown_file)
        return serving, tg_notes

    def _cfg_telegram(self, autodown_file, text="wake test \u2014 bring it up"):
        cfg = ad.load_config()
        cfg["wake_source"] = "telegram"
        cfg["wake_trigger_text"] = text
        ad.save_config(cfg)
        return cfg

    def test_telegram_wake_sends_waking_notice_once(
            self, tmp_path, monkeypatch, autodown_file):
        """A telegram-triggered wake posts the waking-notice exactly ONCE.

        The notice fires at the state->waking transition (not per tick); a
        second autoup while already waking is a no-op and never re-sends it.
        """
        serving, tg_notes = self._setup(tmp_path, monkeypatch, autodown_file)
        self._cfg_telegram(autodown_file)
        runner = _FakeRunner(str(tmp_path / "watchdog-block.json"))
        res = ad.autoup(
            serving_path=serving, run_cmd_fn=runner,
            http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
            sleep_fn=_noop_sleep, notify=True,
        )
        assert res["result"] == "up"
        waking = [n for n in tg_notes if "NOT be processed" in n]
        assert len(waking) == 1
        # A second autoup while ALREADY WAKING is a no-op and re-sends nothing.
        cfg = ad.load_config()
        cfg["state"] = "waking"
        ad.save_config(cfg)
        ad.autoup(
            serving_path=serving, run_cmd_fn=runner,
            http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
            sleep_fn=_noop_sleep, notify=True,
        )
        waking = [n for n in tg_notes if "NOT be processed" in n]
        assert len(waking) == 1   # still once, never per tick

    def test_wake_complete_quotes_trigger_text(
            self, tmp_path, monkeypatch, autodown_file):
        """On wake complete the up-notice quotes the triggering message."""
        serving, tg_notes = self._setup(tmp_path, monkeypatch, autodown_file)
        trigger = "wake test \u2014 please run the quarterly report"
        self._cfg_telegram(autodown_file, text=trigger)
        runner = _FakeRunner(str(tmp_path / "watchdog-block.json"))
        res = ad.autoup(
            serving_path=serving, run_cmd_fn=runner,
            http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
            sleep_fn=_noop_sleep, notify=True,
        )
        assert res["result"] == "up"
        up = [n for n in tg_notes
              if "wake complete" in n and "NOT processed" in n]
        assert len(up) == 1
        assert trigger in up[0]           # quotes the full trigger text
        assert "NOT processed" in up[0]   # honest: not auto-replayed
        # wake bookkeeping cleared on success (incl. trigger text).
        cfg = ad.load_config()
        assert cfg["wake_source"] is None
        assert cfg["wake_trigger_text"] is None

    def test_non_telegram_wake_no_quote(
            self, tmp_path, monkeypatch, autodown_file):
        """CLI/HTTP/kanban-triggered wake sends NO telegram quote and does not
        crash — there is nothing to quote."""
        for source in ("cli", "http", "kanban"):
            serving, tg_notes = self._setup(tmp_path, monkeypatch,
                                            autodown_file)
            cfg = ad.load_config()
            cfg["wake_source"] = source
            ad.save_config(cfg)
            runner = _FakeRunner(str(tmp_path / "watchdog-block.json"))
            res = ad.autoup(
                serving_path=serving, run_cmd_fn=runner,
                http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
                sleep_fn=_noop_sleep, notify=True,
            )
            assert res["result"] == "up"
            # No waking notice, no quoted up-notice — nothing telegram-specific.
            assert not any("NOT process" in n for n in tg_notes)

    def test_failed_wake_no_false_up_notice(
            self, tmp_path, monkeypatch, autodown_file):
        """A failed wake still sends the honest failure notice and NEVER sends
        a false \"cluster is up\" (quoted) notice."""
        serving, tg_notes = self._setup(tmp_path, monkeypatch, autodown_file)
        self._cfg_telegram(autodown_file)
        runner = _FakeRunner(str(tmp_path / "watchdog-block.json"),
                             results=[False])   # first start fails
        res = ad.autoup(
            serving_path=serving, run_cmd_fn=runner,
            http_check_fn=_HealthyProbe(), clock=lambda: 0.0,
            sleep_fn=_noop_sleep, notify=True,
        )
        assert res["result"] == "start-failed"
        # No false "cluster is UP" telegram quote notice.
        assert not any("wake complete" in n for n in tg_notes)
        # The honest failure notice DID fire on the telegram ops channel too.
        assert any("wake FAILED" in n for n in tg_notes)

    def test_no_notices_when_disabled(self, tmp_path, monkeypatch,
                                      autodown_file):
        """Notices are never sent when autodown is disabled — even if a
        telegram source+text is present. Call the helpers directly with a
        disabled config byte-for-byte (nothing fires)."""
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg["enabled"] = False
        cfg["wake_source"] = "telegram"
        cfg["wake_trigger_text"] = "should not be sent"
        ad.save_config(cfg)
        tg_notes = []
        monkeypatch.setattr(ad, "notify_operations",
                            lambda msg, *a, **k: tg_notes.append(str(msg)))
        ad._notify_wake_triggered(cfg)
        ad._notify_wake_complete(cfg)
        assert tg_notes == []


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
        # Hermetic: the reconcile probe reports the layer NOT up, so this test
        # only exercises the self-heal-block path regardless of the live fleet.
        monkeypatch.setattr(ad, "_reconcile_up_fn", lambda **kw: False)
        ad._reconcile_up_streak = 0
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
        # Hermetic: reconcile probe reports NOT up (no cross-test contamination).
        monkeypatch.setattr(ad, "_reconcile_up_fn", lambda **kw: False)
        ad._reconcile_up_streak = 0
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

    # -- F3 (Fix): lock broken IMMEDIATELY when holder PID is PROVABLY DEAD --
    # The reproduced defect: a killed wake left a lock with pid=73271 DEAD but
    # a YOUNG age, so the old time-only rule held the daemon blocked for the
    # full ~20min grace window for no reason. Liveness must win over wall-clock.
    def test_dead_holder_broken_immediately_even_if_fresh(
            self, tmp_path, monkeypatch):
        """A lock whose holder PID is provably dead is broken IMMEDIATELY —
        regardless of age (the old time-only rule would have waited the full
        grace window). A dead holder must never block the daemon."""
        import os as _os
        lock = str(tmp_path / "hscc" / "autodown.lock")
        monkeypatch.setattr(ad, "AUTODOWN_LOCK", lock)
        _os.makedirs(_os.path.dirname(lock), exist_ok=True)
        with open(lock, "w") as f:
            f.write("pid=999999 acquired=0")     # provably dead holder
        now = NOW.timestamp()
        # FRESH mtime — the old time-only rule would have respected it.
        _os.utime(lock, (now - 1, now - 1))
        assert ad._lock_holder_alive() is False
        assert ad._acquire_lock(now=now) is True   # broken immediately
        # The dead lock was unlinked and re-acquired by this process.
        with open(lock) as f:
            content = f.read()
        assert f"pid={_os.getpid()}" in content
        ad._release_lock()

    def test_live_holder_respected_even_if_old(
            self, tmp_path, monkeypatch):
        """A lock held by a LIVE process is NEVER broken, even past the
        staleness threshold — liveness (not wall-clock) is what distinguishes
        'in flight' from 'stalled'. The old time-only rule would have wrongly
        broken a slow-but-alive wake past ~20min."""
        import os as _os
        lock = str(tmp_path / "hscc" / "autodown.lock")
        monkeypatch.setattr(ad, "AUTODOWN_LOCK", lock)
        _os.makedirs(_os.path.dirname(lock), exist_ok=True)
        with open(lock, "w") as f:
            f.write(f"pid={_os.getpid()} acquired=0")  # LIVE holder (this test)
        now = NOW.timestamp()
        # OLD mtime — past the staleness threshold.
        _os.utime(lock, (now - 100000, now - 100000))
        assert ad._lock_holder_alive() is True
        assert ad._acquire_lock(now=now) is False    # busy — live holder held
        # The live holder's lock was NOT unlinked.
        assert _os.path.exists(lock)

    # -- F3 (Fix): unparseable/missing pid falls back to the age rule --------
    def test_unparseable_lock_falls_back_to_age_rule(
            self, tmp_path, monkeypatch):
        """A lock with an unparseable/missing pid (e.g. a holder on ANOTHER
        host, or a legacy lock) cannot be adjudicated by liveness ⇒ the age
        rule still applies: fresh ⇒ busy, over-age ⇒ broken."""
        import os as _os
        lock = str(tmp_path / "hscc" / "autodown.lock")
        monkeypatch.setattr(ad, "AUTODOWN_LOCK", lock)
        _os.makedirs(_os.path.dirname(lock), exist_ok=True)
        now = NOW.timestamp()
        # No pid field at all — indeterminate holder.
        with open(lock, "w") as f:
            f.write("acquired=0")
        # FRESH ⇒ age rule says NOT stale ⇒ busy.
        _os.utime(lock, (now - 1, now - 1))
        assert ad._lock_holder_alive() is None
        assert ad._acquire_lock(now=now) is False    # busy (fresh, no pid)
        # Over-age ⇒ age rule says stale ⇒ broken + re-acquired.
        _os.utime(lock, (now - 100000, now - 100000))
        assert ad._acquire_lock(now=now) is True     # stale by age ⇒ broken
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
        # FAILED wake ⇒ fleet NOT confirmed up ⇒ down_since retained (honest).
        assert cfg["down_since"] is not None

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

    # -- F6 (C4 reversed): keepalive node overlap NO LONGER aborts ----------
    def test_keepalive_overlap_does_not_abort(self, tmp_path, monkeypatch,
                                              autodown_file):
        """A keepalive node in the set does NOT abort — fleet `--all` covers it.

        C4 is reversed: autodown powers the ENTIRE serving layer down including
        keepalive units, so a co-located config (keepalive worker sharing a node
        with the orchestrator) is fine — the whole-fleet ``sparkrun stop --all``
        stops it. The old keepalive-overlap abort guard is REMOVED (it would
        abort on every teardown, since `--all` always touches keepalive nodes).
        """
        serving, runner, bf = self._setup(tmp_path, monkeypatch, autodown_file)
        # Co-located config: the keepalive worker shares .244 with the
        # orchestrator — previously this triggered the (now removed) guard.
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
        # NOT aborted: the single `--all` stop runs, teardown completes down.
        assert res["result"] == "down"
        assert runner.calls == [{"cmd": FLEET_STOP_CMD,
                                 "block": runner.calls[0]["block"], "ok": True}]
        # Verify set covers BOTH units (orchestrator + the keepalive worker).
        verify_ids = {v["unit_id"] for v in res["plan"][0]["verify"]}
        assert verify_ids == {"orch", "wk-keep"}
        cfg = ad.load_config()
        assert cfg["state"] == "down"               # recorded down
