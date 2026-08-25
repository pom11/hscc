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


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

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

        def _fake_active(kanban_db=None):
            autodown._note_blocking("hscc")
            return True

        autodown_file.write_text(json.dumps({
            "enabled": True, "idle_minutes": 10, "state": "up",
            "last_activity_iso": "2026-08-25T09:19:51+00:00",
        }))
        monkeypatch.setattr(autodown, "_has_active_work", _fake_active)
        rc = cmd_autodown(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "blocked by:      kanban work on board 'hscc'" in out

        # --json carries the same machine-readable signal.
        rc = cmd_autodown(["status", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["blocked_by"] == "kanban work on board 'hscc'"

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
