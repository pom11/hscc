"""Tests for hscc_daemon/autodown_cli.py — Phase 7 (CLI verb group).

These exercise ``cmd_autodown`` directly with tmp paths (monkeypatched
autodown.AUTODOWN_FILE and lifecycle.WATCHDOG_BLOCK_FILE) and stubbed fakes —
NEVER the real ~/.hscc, ~/.hermes, or the live cluster. For the specific
required case ``wake`` (which would otherwise start real vLLM units via
``autoup()``), autoup is monkeypatched with a fake so no real cluster command
is ever issued.

The real end-to-end CLI (``python hscc_daemon/hscc.py autodown ...`` against a
tmp HOME) is smoke-tested separately in the task report.
"""

import json

import pytest

import hscc_daemon.autodown as ad
from hscc_daemon import lifecycle
from hscc_daemon.autodown_cli import (cmd_autodown, _parse_idle_minutes,
                                      DEFAULT_IDLE_MINUTES)

# The REAL cron reader, captured at import (before the autouse _no_active_crons
# fixture replaces ad.list_active_cron_jobs with a []-returning stub per test).
# Tests that want the production active/inactive filtering restore this.
_REAL_LIST_ACTIVE_CRON_JOBS = ad.list_active_cron_jobs


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_active_crons(monkeypatch):
    """Default: NO active Hermes cron jobs, so existing enable/status tests arm
    normally. The cron-guard itself is tested explicitly (those tests overwrite
    this via their own monkeypatch of ``ad.list_active_cron_jobs``, which — LIFO
    teardown — wins during the test). Also keeps every test off the real
    ``~/.hermes/cron/jobs.json``.
    """
    monkeypatch.setattr(ad, "list_active_cron_jobs", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def _no_active_prci(monkeypatch):
    """Default: PR/CI interlock CLEAR, so existing status tests are hermetic.

    ``_cmd_status`` evaluates ``_has_active_pr_ci()`` (the real ``gh`` screen)
    to surface the PR/CI interlock. Stubbing it clear keeps every status test
    off the real GitHub/network and deterministic; PR/CI surfacing itself is
    tested explicitly (those tests overwrite this via their own monkeypatch,
    which — LIFO teardown — wins during the test).
    """
    monkeypatch.setattr(ad, "_has_active_pr_ci", lambda *a, **k: False)
    monkeypatch.setattr(ad, "prci_blocking_signal", lambda: None)
    monkeypatch.setattr(ad, "prci_check_state", lambda: {
        "ok": True, "reason": "", "blocking": None})

@pytest.fixture
def autodown_file(tmp_path, monkeypatch):
    """Point autodown.AUTODOWN_FILE at a tmp path and return the path."""
    path = tmp_path / "hscc" / "autodown.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ad, "AUTODOWN_FILE", str(path))
    # record_activity() reads the module-level AUTODOWN_FILE — already covered.
    return path


@pytest.fixture
def block_file(tmp_path, monkeypatch):
    """Point lifecycle.WATCHDOG_BLOCK_FILE at a tmp path and return it."""
    path = str(tmp_path / "hscc" / "watchdog-block.json")
    monkeypatch.setattr(lifecycle, "WATCHDOG_BLOCK_FILE", path)
    return path


@pytest.fixture
def closed_env(monkeypatch):
    """Stub notifications so no verb actually tries to notify."""
    monkeypatch.setattr(ad, "notify_operations", lambda *a, **k: True)
    monkeypatch.setattr(ad, "send_macos_notification", lambda *a, **k: True)


def _read_config(path):
    return json.loads(path.read_text())


def _read_block(path):
    return json.loads(__import__("builtins").open(path).read())


# ---------------------------------------------------------------------------
# _parse_idle_minutes — validation unit
# ---------------------------------------------------------------------------

class TestParseIdleMinutes:
    def test_absent_returns_default_sentinel(self):
        value, err = _parse_idle_minutes([])
        assert value is None and err is None

    def test_valid_integer(self):
        value, err = _parse_idle_minutes(["--idle-minutes", "15"])
        assert value == 15 and err is None

    def test_zero_valid(self):
        # 0 = only via explicit wake / never auto (§7).
        value, err = _parse_idle_minutes(["--idle-minutes", "0"])
        assert value == 0 and err is None

    def test_rejects_non_integer(self, capsys):
        value, err = _parse_idle_minutes(["--idle-minutes", "abc"])
        assert value is None
        assert err and "non-negative integer" in err

    def test_rejects_negative(self):
        value, err = _parse_idle_minutes(["--idle-minutes", "-5"])
        assert value is None
        assert err and "non-negative" in err

    def test_rejects_missing_value(self):
        value, err = _parse_idle_minutes(["--idle-minutes"])
        assert value is None
        assert err and "requires a value" in err


# ---------------------------------------------------------------------------
# status — read-only, never mutates
# ---------------------------------------------------------------------------

class TestStatus:
    def test_never_enabled_reports_disabled_and_creates_nothing(
            self, capsys, autodown_file, block_file):
        # Machine that never enabled autodown → file does not exist yet.
        assert not autodown_file.exists()
        rc = cmd_autodown(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "DISABLED" in out
        # status is read-only: must not create or mutate autodown.json, nor the
        # watchdog block file.
        assert not autodown_file.exists()
        assert not __import__("os").path.exists(block_file)

    def test_status_json_valid(self, capsys, autodown_file, block_file):
        rc = cmd_autodown(["status", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["enabled"] is False
        assert data["state"] == "up"
        assert "idle_minutes" in data

    def test_status_reports_enabled_state(self, capsys, autodown_file,
                                          block_file):
        autodown_file.write_text(json.dumps({
            "enabled": True, "idle_minutes": 7, "state": "down",
            "last_activity_iso": "2026-08-23T00:00:00+00:00",
            "down_since": "2026-08-23T00:10:00+00:00",
            "wake_source": "api", "reason": "idle",
        }))
        rc = cmd_autodown(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "ENABLED" in out
        assert "idle_minutes=7" in out
        assert "down" in out.lower()

    def test_status_surfaces_unevaluable_kanban_interlock(
            self, capsys, autodown_file, block_file, monkeypatch, tmp_path):
        """When the kanban lib cannot be resolved, status shows the interlock
        is UNEVALUABLE and why — the operator is never left guessing."""
        monkeypatch.setenv("HERMES_AGENT_PATH", str(tmp_path / "missing"))
        from hscc_daemon import autodown
        autodown._load_kanban_db_or_default()  # records the failure
        rc = cmd_autodown(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "UNEVALUABLE" in out
        assert "missing" in out

    def test_status_json_has_kanban_fields(self, capsys, autodown_file,
                                           block_file, monkeypatch):
        # Status evaluates the kanban interlock itself (to name the blocker).
        # Stub the predicate so no real board is touched and the result is
        # deterministic: quiet ⇒ no blocker, kanban resolution untouched.
        from hscc_daemon import autodown
        monkeypatch.setitem(autodown._KANBAN_LOAD, "ok", None)
        monkeypatch.setitem(autodown._KANBAN_LOAD, "reason", "")
        monkeypatch.setattr(autodown, "_has_active_work", lambda *a, **k: False)
        rc = cmd_autodown(["status", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        # kanban_ok is None until something resolves the kanban lib.
        assert data["kanban_ok"] is None
        assert "kanban_reason" in data
        # No live work ⇒ no blocking signal.
        assert data["blocked_by"] is None

    def test_status_names_blocking_signal(self, capsys, autodown_file,
                                          block_file, monkeypatch):
        """When kanban work blocks teardown, status names the board — the
        operator can tell healthy-and-waiting from stuck-on-an-interlock."""
        from hscc_daemon import autodown
        import sqlite3

        def _fake_active(kanban_db=None):
            autodown._note_blocking("hscc")
            return True

        # Stub the board the predicate named so status does NOT read the
        # operator's real boards (tests never touch live boards).
        kb = sqlite3.connect(":memory:")
        kb.row_factory = sqlite3.Row
        kb.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, "
                   "assignee TEXT, status TEXT, created_at INTEGER)")
        kb.execute("INSERT INTO tasks VALUES "
                   "('t-1', 'forgotten card', 'w', 'todo', 0), "
                   "('t-2', 'other', 'w', 'running', 0)")
        kb.commit()

        class _Kb:
            def list_boards(self):
                return [{"slug": "hscc"}]
            def connect_closing(self, board=None):
                class _CM:
                    def __enter__(_self):
                        return kb
                    def __exit__(_self, *exc):
                        return False
                return _CM()

        def _fake_load():
            return _Kb()

        autodown_file.write_text(json.dumps({
            "enabled": True, "idle_minutes": 10, "state": "up",
            "last_activity_iso": "2026-08-25T09:19:51+00:00",
        }))
        monkeypatch.setattr(autodown, "_has_active_work", _fake_active)
        monkeypatch.setattr(autodown, "_load_kanban_db_or_default", _fake_load)
        rc = cmd_autodown(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "kanban work on board 'hscc': t-1 (forgotten card), " \
               "t-2 (other)" in out

        # --json carries the same machine-readable signal.
        rc = cmd_autodown(["status", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["blocked_by"] == \
            "kanban work on board 'hscc': t-1 (forgotten card), t-2 (other)"

    def test_status_surfaces_prci_interlock(self, capsys, autodown_file,
                                            block_file, monkeypatch):
        """When an open PR / active CI run blocks teardown, status names it —
        both the human line and --json — and preprends it to any kanban
        blocker instead of hiding one interlock behind the other."""
        from hscc_daemon import autodown

        # Override the autouse hermetic stub (LIFO wins) so the PR/CI
        # interlock is ACTIVE — like an open PR on a tracked repo.
        monkeypatch.setattr(autodown, "_has_active_pr_ci",
                            lambda *a, **k: True)
        monkeypatch.setattr(autodown, "prci_blocking_signal",
                            lambda: "PR/CI")
        monkeypatch.setattr(autodown, "prci_check_state", lambda: {
            "ok": True, "reason": "", "blocking": "PR/CI"})

        autodown_file.write_text(json.dumps({
            "enabled": True, "idle_minutes": 10, "state": "up",
            "last_activity_iso": "2026-08-25T09:19:51+00:00",
        }))
        rc = cmd_autodown(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "PR/CI interlock:  ACTIVE" in out

        rc = cmd_autodown(["status", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["prci_active"] is True
        assert "open PR / active CI run" in data["blocked_by"]

    def test_status_prci_unreachable_surfaced(self, capsys, autodown_file,
                                              block_file, monkeypatch):
        """An unreachable PR/CI source is surfaced as fail-safe active, so an
        operator sees WHY autodown holds rather than a bare state: up."""
        from hscc_daemon import autodown
        monkeypatch.setattr(autodown, "_has_active_pr_ci",
                            lambda *a, **k: True)
        monkeypatch.setattr(autodown, "prci_blocking_signal",
                            lambda: autodown._PRCI_UNREACHABLE)
        monkeypatch.setattr(autodown, "prci_check_state", lambda: {
            "ok": False, "reason": "PR/CI source unreachable", "blocking":
            autodown._PRCI_UNREACHABLE})

        autodown_file.write_text(json.dumps({
            "enabled": True, "idle_minutes": 10, "state": "up",
            "last_activity_iso": "2026-08-25T09:19:51+00:00",
        }))
        rc = cmd_autodown(["status", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["prci_active"] is True
        assert "unreachable" in data["blocked_by"]
        assert data["prci_ok"] is False

    def test_status_no_down_since_line_when_null(self, capsys, autodown_file,
                                                 block_file):
        """Up state ⇒ down_since null ⇒ NO 'down since' line at all (the old
        code printed 'down since: None'). --json carries down_since: null,
        reflecting the same truth: the fleet is up."""
        autodown_file.write_text(json.dumps({
            "enabled": True, "idle_minutes": 10, "state": "up",
            "last_activity_iso": "2026-08-25T09:19:51+00:00",
            "down_since": None,
        }))
        rc = cmd_autodown(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "down since" not in out          # no line when null
        # Human output is unambiguous: state up, no down-since contradiction.
        assert "state:           up" in out
        # --json shows the same truth: down_since is null, state up.
        rc = cmd_autodown(["status", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["state"] == "up"
        assert data["down_since"] is None

    def test_status_shows_down_since_when_present(self, capsys, autodown_file,
                                                  block_file):
        """Down ⇒ down_since set ⇒ the 'down since' line IS printed with the
        value (both human and --json)."""
        autodown_file.write_text(json.dumps({
            "enabled": True, "idle_minutes": 10, "state": "down",
            "last_activity_iso": "2026-08-25T09:19:51+00:00",
            "down_since": "2026-08-25T07:59:01+00:00",
        }))
        rc = cmd_autodown(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "down since:      2026-08-25T07:59:01+00:00" in out
        rc = cmd_autodown(["status", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["state"] == "down"
        assert data["down_since"] == "2026-08-25T07:59:01+00:00"

    def test_status_notes_cpu_only_cron_and_model_cron(
            self, capsys, autodown_file, block_file, monkeypatch):
        """status reads active crons and notes CPU-only watchdogs (they never
        block arming) AND model-requiring jobs (the abort/force reason)."""
        monkeypatch.setattr(ad, "list_active_cron_jobs", lambda *a, **k: [
            _CPU_ONLY_JOBS[0], _MODEL_JOBS[0]])
        rc = cmd_autodown(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "active cron (cpu-only): hscc-dep-watcher" in out
        assert "do not block arming" in out
        assert "active cron (model):    gpu-daily-report" in out

    def test_status_json_notes_cron_classification(
            self, capsys, autodown_file, block_file, monkeypatch):
        monkeypatch.setattr(ad, "list_active_cron_jobs", lambda *a, **k: [
            _CPU_ONLY_JOBS[0], _CPU_ONLY_JOBS[1], _MODEL_JOBS[0]])
        rc = cmd_autodown(["status", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert sorted(data["active_cron_cpu_only"]) == [
            "hscc-dep-watcher", "hscc-escalate-watcher"]
        assert data["active_cron_model"] == ["gpu-daily-report"]


# ---------------------------------------------------------------------------
# enable — resets the idle timer, non-acting
# ---------------------------------------------------------------------------

class TestEnable:
    def test_enable_arms_and_resets_idle_timer(self, capsys, autodown_file,
                                               block_file):
        # Simulate prior activity far in the past (would be idle-eligible if an
        # empty window had elapsed).
        autodown_file.write_text(json.dumps({
            "enabled": False, "idle_minutes": 10, "state": "up",
            "last_activity_iso": "2020-01-01T00:00:00+00:00",
        }))
        rc = cmd_autodown(["enable", "--idle-minutes", "5"])
        assert rc == 0
        cfg = _read_config(autodown_file)
        assert cfg["enabled"] is True
        assert cfg["idle_minutes"] == 5
        # last_activity_iso must be reset to (roughly) now — NOT the stale 2020.
        assert cfg["last_activity_iso"] != "2020-01-01T00:00:00+00:00"
        assert cfg["last_activity_iso"]

    def test_enable_default_idle_minutes(self, capsys, autodown_file,
                                         block_file):
        rc = cmd_autodown(["enable"])
        assert rc == 0
        cfg = _read_config(autodown_file)
        assert cfg["idle_minutes"] == DEFAULT_IDLE_MINUTES == 10

    def test_enable_bad_idle_minutes_nonzero(self, capsys, autodown_file,
                                             block_file):
        rc = cmd_autodown(["enable", "--idle-minutes", "abc"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "non-negative integer" in err
        # enable must NOT have been persisted with a bad value.
        assert not autodown_file.exists()

    def test_enable_negative_nonzero(self, capsys, autodown_file, block_file):
        rc = cmd_autodown(["enable", "--idle-minutes", "-3"])
        assert rc == 1
        assert "non-negative" in capsys.readouterr().err

    def test_enable_json(self, capsys, autodown_file, block_file):
        rc = cmd_autodown(["enable", "--idle-minutes", "9", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["enabled"] is True
        assert data["idle_minutes"] == 9
        assert data["last_activity_iso"]


# ---------------------------------------------------------------------------
# enable — Hermes cron-job guard (feat t_2b711a94 §7, refined t_c94f8b8c)
# ---------------------------------------------------------------------------
# Active scheduled jobs and an idle-down cluster conflict: autodown may power
# the GPU serving layer down exactly when a job is due. So `enable` reads
# Hermes' source of truth (~/.hermes/cron/jobs.json) and ABORTS when active
# MODEL-REQUIRING jobs exist (unless --force). CPU-only watchdogs
# (no_agent:true, model:null) do NOT block — they are informational notes.
# Unreadable/absent cron config ⇒ "cannot determine" ⇒ abort (fail-safe).

# The real host's two active jobs — both CPU-only script watchdogs.
_CPU_ONLY_JOBS = [
    {"id": "j1", "name": "hscc-dep-watcher", "schedule_display": "0 8 * * *",
     "next_run_at": "2026-08-26T08:00:00+03:00",
     "no_agent": True, "model": None, "cpu_only": True},
    {"id": "j2", "name": "hscc-escalate-watcher",
     "schedule_display": "*/15 * * * *", "next_run_at": "2026-08-26T00:45:00+03:00",
     "no_agent": True, "model": None, "cpu_only": True},
]

# A model-requiring job (runs an agent / needs the GPU serving layer).
_MODEL_JOBS = [
    {"id": "m1", "name": "gpu-daily-report", "schedule_display": "0 9 * * *",
     "next_run_at": "2026-08-26T09:00:00+03:00",
     "no_agent": False, "model": "gpt-4o", "cpu_only": False},
]


class TestEnableCronGuard:
    def _use_real_reader(self, monkeypatch):
        """Restore the REAL ``list_active_cron_jobs`` (the autouse stub returns
        []), so the production filtering runs end-to-end against the
        conftest-redirected CRON_JOBS_FILE."""
        monkeypatch.setattr(ad, "list_active_cron_jobs",
                            _REAL_LIST_ACTIVE_CRON_JOBS)

    def _stub_active(self, monkeypatch, jobs):
        monkeypatch.setattr(ad, "list_active_cron_jobs", lambda *a, **k: jobs)

    def test_aborts_nonzero_and_lists_model_jobs(self, capsys, autodown_file,
                                                 block_file, monkeypatch):
        """A model-requiring active job ⇒ `enable` aborts non-zero and lists
        it (the blocker). CPU-only jobs, if present too, are NOT named."""
        self._stub_active(monkeypatch, _MODEL_JOBS + _CPU_ONLY_JOBS)
        rc = cmd_autodown(["enable", "--idle-minutes", "5"])
        assert rc == 1
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "NOT armed" in combined
        assert "model-requiring" in combined
        assert "gpu-daily-report" in combined
        assert "0 9 * * *" in combined
        assert "2026-08-26T09:00:00+03:00" in combined
        # CPU-only job is NOT named as a blocker.
        assert "hscc-dep-watcher" not in combined
        # Must NOT have armed: no config file created.
        assert not autodown_file.exists()

    def test_abort_json_names_only_model_jobs(self, capsys, autodown_file,
                                              block_file, monkeypatch):
        self._stub_active(monkeypatch, _MODEL_JOBS + _CPU_ONLY_JOBS)
        rc = cmd_autodown(["enable", "--json"])
        assert rc == 1
        data = json.loads(capsys.readouterr().out)
        assert data["exit"] == 1
        assert "aborting" in data["error"]
        names = [j["name"] for j in data["active_cron_jobs"]]
        assert names == ["gpu-daily-report"]   # only the model-requiring one
        assert not autodown_file.exists()

    def test_cpu_only_active_jobs_arm_and_are_noted(self, capsys,
                                                    autodown_file, block_file,
                                                    monkeypatch):
        """CPU-only active jobs (no_agent:true, model:null) ⇒ enable ARMS and
        notes them informationally — no abort, no --force needed."""
        self._stub_active(monkeypatch, _CPU_ONLY_JOBS)
        rc = cmd_autodown(["enable", "--idle-minutes", "5"])
        assert rc == 0
        cfg = _read_config(autodown_file)
        assert cfg["enabled"] is True
        # Clean arm over CPU-only jobs — NOT force-armed, nothing overridden.
        assert cfg["force_armed"] is False
        assert cfg["force_armed_overrides"] == []
        out = capsys.readouterr().out
        assert "ENABLED" in out
        # CPU-only watchdog is noted informationally.
        assert "hscc-dep-watcher" in out
        assert "hscc-escalate-watcher" in out
        assert "CPU-only" in out

    def test_no_active_jobs_arms(self, capsys, autodown_file, block_file,
                                  monkeypatch):
        """No active jobs ⇒ enable works as before (returns 0, arms)."""
        self._stub_active(monkeypatch, [])
        rc = cmd_autodown(["enable", "--idle-minutes", "5"])
        assert rc == 0
        cfg = _read_config(autodown_file)
        assert cfg["enabled"] is True
        assert cfg["force_armed"] is False
        assert cfg["force_armed_overrides"] == []

    def test_unreadable_cron_config_aborts(self, capsys, autodown_file,
                                           block_file, monkeypatch):
        """Unreadable/absent cron config ⇒ abort with 'cannot determine'."""
        self._stub_active(monkeypatch, ad.CRON_UNREADABLE)
        rc = cmd_autodown(["enable"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "cannot determine" in err
        assert not autodown_file.exists()

    def test_ambiguous_missing_fields_abort_failsafe(self, capsys,
                                                     autodown_file, block_file,
                                                     monkeypatch):
        """A job whose nature cannot be determined (missing no_agent/model)
        is treated as model-requiring and ABORTS — never arm on an unverifiable
        signal."""
        self._stub_active(monkeypatch, [
            {"id": "u1", "name": "mystery-job",
             "schedule_display": "0 10 * * *", "next_run_at": "..."}])
        rc = cmd_autodown(["enable"])
        assert rc == 1
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "NOT armed" in combined
        assert "mystery-job" in combined
        assert not autodown_file.exists()

    def test_inactive_jobs_do_not_block(self, capsys, autodown_file,
                                        block_file, monkeypatch):
        """Disabled/paused jobs do not block arming — the REAL reader filters
        them out of the active set, so the CLI arms normally."""
        autodown_file.parent.mkdir(parents=True, exist_ok=True)
        jobs_file = autodown_file.parent / "cron" / "jobs.json"
        jobs_file.parent.mkdir(parents=True, exist_ok=True)
        jobs_file.write_text(json.dumps({"jobs": [
            {"id": "x", "name": "paused-job", "enabled": False,
             "schedule_display": "0 8 * * *", "next_run_at": "..."}]}))
        self._use_real_reader(monkeypatch)
        rc = cmd_autodown(["enable", "--idle-minutes", "5"])
        assert rc == 0
        cfg = _read_config(autodown_file)
        assert cfg["enabled"] is True
        assert cfg["force_armed"] is False

    def test_force_arms_and_records_only_model_overrides(
            self, capsys, autodown_file, block_file, monkeypatch):
        """--force over a model-requiring job arms, prints the override, and
        records force_armed + ONLY the model-requiring job(s). CPU-only jobs
        are NOT recorded as overrides (they never conflicted) but are noted."""
        self._stub_active(monkeypatch, _MODEL_JOBS + _CPU_ONLY_JOBS)
        rc = cmd_autodown(["enable", "--idle-minutes", "5", "--force"])
        assert rc == 0
        cfg = _read_config(autodown_file)
        assert cfg["enabled"] is True
        assert cfg["force_armed"] is True
        assert cfg["force_armed_overrides"] == ["gpu-daily-report"]
        captured = capsys.readouterr()
        assert "FORCED" in captured.out
        assert "gpu-daily-report" in captured.out
        # CPU-only watchdogs not in the override record, but noted.
        assert "hscc-dep-watcher" not in cfg["force_armed_overrides"]
        assert "CPU-only" in captured.out

    def test_force_records_only_actually_active_model_jobs(
            self, capsys, autodown_file, block_file, monkeypatch):
        """--force records only the ACTIVE MODEL-REQUIRING jobs it overrode.
        Uses the REAL reader against a real jobs.json so the active/inactive
        AND cpu-only/model splits all happen in production code, not a stub."""
        autodown_file.parent.mkdir(parents=True, exist_ok=True)
        jobs_file = autodown_file.parent / "cron" / "jobs.json"
        jobs_file.parent.mkdir(parents=True, exist_ok=True)
        jobs_file.write_text(json.dumps({"jobs": [
            {"id": "j1", "name": "hscc-dep-watcher", "enabled": True,
             "no_agent": True, "model": None,
             "schedule_display": "0 8 * * *",
             "next_run_at": "2026-08-26T08:00:00+03:00"},
            {"id": "p", "name": "paused-one", "enabled": False,
             "schedule_display": "0 8 * * *", "next_run_at": "..."},
            {"id": "m", "name": "gpu-daily-report", "enabled": True,
             "no_agent": False, "model": "gpt-4o",
             "schedule_display": "0 9 * * *",
             "next_run_at": "2026-08-26T09:00:00+03:00"},
        ]}))
        self._use_real_reader(monkeypatch)
        rc = cmd_autodown(["enable", "--force"])
        assert rc == 0
        cfg = _read_config(autodown_file)
        assert cfg["force_armed"] is True
        assert cfg["force_armed_overrides"] == ["gpu-daily-report"]
        captured = capsys.readouterr()
        assert "paused-one" not in captured.out
        assert "hscc-dep-watcher" not in cfg["force_armed_overrides"]
        assert "gpu-daily-report" in captured.out

    def test_force_arms_when_config_unreadable(self, capsys, autodown_file,
                                               block_file, monkeypatch):
        """--force overrides even a 'cannot determine' config (arms, but does
        NOT record overrides since nothing determinate was overridden)."""
        self._stub_active(monkeypatch, ad.CRON_UNREADABLE)
        rc = cmd_autodown(["enable", "--force"])
        assert rc == 0
        cfg = _read_config(autodown_file)
        assert cfg["enabled"] is True
        assert cfg["force_armed"] is False
        assert cfg["force_armed_overrides"] == []

    def test_status_surfaces_force_armed(self, capsys, autodown_file,
                                         block_file):
        """status shows force_armed + overridden jobs so the operator can see
        why autodown is armed despite scheduled jobs."""
        autodown_file.write_text(json.dumps({
            "enabled": True, "idle_minutes": 5, "state": "up",
            "last_activity_iso": "2026-08-26T00:00:00+00:00",
            "force_armed": True,
            "force_armed_overrides": ["hscc-dep-watcher"],
        }))
        rc = cmd_autodown(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "force-armed:     YES" in out
        assert "hscc-dep-watcher" in out

    def test_status_json_surfaces_force_armed(self, capsys, autodown_file,
                                              block_file):
        autodown_file.write_text(json.dumps({
            "enabled": True, "idle_minutes": 5, "state": "up",
            "last_activity_iso": "2026-08-26T00:00:00+00:00",
            "force_armed": True,
            "force_armed_overrides": ["hscc-dep-watcher"],
        }))
        rc = cmd_autodown(["status", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["force_armed"] is True
        assert data["force_armed_overrides"] == ["hscc-dep-watcher"]


# ---------------------------------------------------------------------------
# disable — clears the intentional block, does not restart serving
# ---------------------------------------------------------------------------

class TestDisable:
    def test_disable_clears_intentional_block(self, capsys, autodown_file,
                                              block_file):
        # Precondition: intentionally down (block latched with intentional).
        autodown_file.write_text(json.dumps({
            "enabled": True, "idle_minutes": 10, "state": "down",
            "last_activity_iso": "2026-08-23T00:00:00+00:00",
            "down_since": "2026-08-23T00:10:00+00:00",
        }))
        lifecycle.save_watchdog_block({
            "blocked": True, "reason": "autodown: intentional idle teardown",
            "blocked_at": "2026-08-23T00:10:00+00:00",
            "intentional": "autodown", "failures": [],
        })
        rc = cmd_autodown(["disable"])
        assert rc == 0
        cfg = _read_config(autodown_file)
        assert cfg["enabled"] is False
        # state reflects reality (unchanged — disable does not touch serving).
        assert cfg["state"] == "down"
        block = _read_block(block_file)
        assert block["blocked"] is False
        assert "intentional" not in block
        # disable must NOT run autoup (no restart of serving).
        out = capsys.readouterr().out
        assert "wake" in out  # help text mentions the separate wake path

    def test_disable_when_never_enabled(self, capsys, autodown_file,
                                        block_file):
        rc = cmd_autodown(["disable"])
        assert rc == 0
        cfg = _read_config(autodown_file)
        assert cfg["enabled"] is False

    def test_disable_json(self, capsys, autodown_file, block_file):
        rc = cmd_autodown(["disable", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["enabled"] is False


# ---------------------------------------------------------------------------
# wake — force autoup (autoup faked; must never run real cluster commands)
# ---------------------------------------------------------------------------

class TestWake:
    def test_wake_calls_autoup(self, capsys, autodown_file, block_file,
                               monkeypatch, closed_env):
        called = {}
        def fake_autoup(**kwargs):
            called["hit"] = True
            return {"result": "up", "started": [], "ready": []}
        monkeypatch.setattr(ad, "autoup", fake_autoup)
        rc = cmd_autodown(["wake"])
        assert rc == 0
        assert called["hit"] is True
        out = capsys.readouterr().out
        assert "UP" in out

    def test_wake_advances_idle_timer(self, capsys, autodown_file, block_file,
                                      monkeypatch, closed_env):
        autodown_file.write_text(json.dumps({
            "enabled": True, "idle_minutes": 10, "state": "down",
            "last_activity_iso": "2026-08-23T00:00:00+00:00",
            "down_since": "2026-08-23T00:10:00+00:00",
        }))
        monkeypatch.setattr(ad, "autoup",
                            lambda **k: {"result": "up", "started": [], "ready": []})
        rc = cmd_autodown(["wake"])
        assert rc == 0
        cfg = _read_config(autodown_file)
        # record_activity("cli") advanced last_activity_iso past down_since.
        assert cfg["last_activity_iso"] != "2026-08-23T00:00:00+00:00"

    def test_wake_json(self, capsys, autodown_file, block_file, monkeypatch,
                       closed_env):
        monkeypatch.setattr(ad, "autoup",
                            lambda **k: {"result": "up", "started": [], "ready": []})
        rc = cmd_autodown(["wake", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["result"] == "up"


# ---------------------------------------------------------------------------
# cancel — sets cancel_requested
# ---------------------------------------------------------------------------

class TestCancel:
    def test_cancel_sets_flag(self, capsys, autodown_file, block_file):
        autodown_file.write_text(json.dumps({
            "enabled": True, "idle_minutes": 10, "state": "down",
        }))
        rc = cmd_autodown(["cancel"])
        assert rc == 0
        cfg = _read_config(autodown_file)
        assert cfg["cancel_requested"] is True

    def test_cancel_json(self, capsys, autodown_file, block_file):
        rc = cmd_autodown(["cancel", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["cancel_requested"] is True


# ---------------------------------------------------------------------------
# dispatch — help + unknown subcommand
# ---------------------------------------------------------------------------

class TestDispatch:
    def test_no_subcommand_prints_help(self, capsys):
        rc = cmd_autodown([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Usage: hscc autodown" in out
        assert "status" in out and "enable" in out and "wake" in out

    def test_help_flag_prints_help(self, capsys):
        assert cmd_autodown(["--help"]) == 0
        assert "Usage: hscc autodown" in capsys.readouterr().out

    def test_unknown_subcommand_exits_nonzero(self, capsys):
        rc = cmd_autodown(["bogus"])
        assert rc == 1
        # Matches api_cli.cmd_api convention: error printed to stdout (not
        # stderr), then non-zero exit.
        out = capsys.readouterr().out
        assert "unknown autodown subcommand" in out
        assert "bogus" in out
