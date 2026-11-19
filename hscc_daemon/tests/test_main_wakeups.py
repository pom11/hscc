"""M5 — the daemon-owned wake-up schedule for the main orchestrator.

Hermetic by construction: ``now`` is injected, delivery is a fake callable or a
fake repl module, and every config/state path is under ``tmp_path``. No test
touches tmux, the daemon or the operator's ``~/.hscc``.

What is pinned (the card's test list):
  * schedule math — interval, daily local time, weekly weekday/time;
  * restart persistence — due-ness comes from the state file, not memory, so a
    second pass inside the window cannot double-fire and a later pass still
    remembers;
  * busy -> queue — delivery always asks for ``--mode auto`` and records the
    resolved ``queue`` mode;
  * missing pane — skipped, logged, persisted, and reported by doctor.
"""

import io
import json
from contextlib import redirect_stdout
from datetime import datetime

import pytest

import hscc_daemon.hscc as unified  # noqa: F401  (dispatch smoke in CLI tests)
from hscc_daemon import daemon_ops
from hscc_daemon import main_cli as mc
from hscc_daemon import main_wakeups as mw


def at(*args):
    """Local wall-clock timestamp — daily/weekly schedules are local time."""
    return datetime(*args).timestamp()


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class Recorder:
    """Fake delivery callable; records text/mode and can fail like repl_inject."""

    def __init__(self, mode="plain", busy=False, error=None):
        self.mode = mode
        self.busy = busy
        self.error = error
        self.calls = []

    def __call__(self, profile, text, mode="auto"):
        self.calls.append({"profile": profile, "text": text, "mode": mode})
        if self.error is not None:
            raise self.error
        return {"mode": self.mode if self.mode is not None else mode,
                "busy": self.busy, "profile": profile}


class PaneError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class BusyRepl:
    """Fake repl_inject module: auto resolves to queue while busy."""

    def __init__(self, busy=True):
        self.busy = busy
        self.calls = []

    def inject(self, profile, text, mode="auto"):
        self.calls.append({"profile": profile, "text": text, "mode": mode})
        resolved = "queue" if (mode == "auto" and self.busy) else "plain"
        return {"profile": profile, "mode": resolved, "busy": self.busy}


def _paths(tmp_path):
    return (str(tmp_path / "main_wakeups.json"),
            str(tmp_path / "state" / "main_wakeups.json"))


def _write_config(path, enabled):
    cfg = mw._default_config()
    for name in mw.NAMES:
        cfg["wakeups"][name]["enabled"] = name in enabled
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return path


# --------------------------------------------------------------------------- #
# Defaults + prompts
# --------------------------------------------------------------------------- #

def test_defaults_match_the_design():
    assert mw.DEFAULT_WAKEUPS == {
        "health": {"enabled": True, "schedule": "every 2h"},
        "sweep": {"enabled": True, "schedule": "every 3h"},
        "memory": {"enabled": True, "schedule": "daily 07:00"},
        "settings": {"enabled": True, "schedule": "weekly Monday 07:30"},
    }
    cfg = mw.load_config("/nonexistent/main_wakeups.json")
    assert [cfg["wakeups"][n]["schedule"] for n in mw.NAMES] == [
        "every 2h", "every 3h", "daily 07:00", "weekly Monday 07:30"]
    assert all(cfg["wakeups"][n]["enabled"] for n in mw.NAMES)


def test_prompts_are_short_english_and_name_their_soul_duty():
    assert set(mw.PROMPTS) == set(mw.NAMES)
    needles = {
        "health": ("soul", "nodes", "serving", "proxy", "daemon",
                   "autodown", "caps"),
        "sweep": ("soul", "board", "pane", "watch"),
        "memory": ("soul", "per-project"),
        "settings": ("soul", "skills", "config"),
    }
    for name, words in needles.items():
        text = mw.PROMPTS[name]
        assert len(text) < 300, (name, len(text))
        for word in words:
            assert word in text.lower(), (name, word, text)


# --------------------------------------------------------------------------- #
# Schedule math
# --------------------------------------------------------------------------- #

def test_interval_schedule_math():
    start = at(2026, 11, 19, 10, 0)
    assert mw.normalize_schedule("every 2 hours") == "every 2h"
    assert mw.normalize_schedule("every 90m") == "every 90m"
    assert mw.next_fire("every 2h", start) == start + 2 * 3600
    assert mw.is_due("every 2h", start, start + 2 * 3600 - 1) is False
    assert mw.is_due("every 2h", start, start + 2 * 3600) is True


def test_daily_schedule_uses_local_wall_clock():
    before = at(2026, 11, 19, 6, 0)
    after = at(2026, 11, 19, 8, 0)
    assert mw.next_fire("daily 07:00", before) == at(2026, 11, 19, 7, 0)
    assert mw.next_fire("daily 07:00", after) == at(2026, 11, 20, 7, 0)
    assert mw.is_due("daily 07:00", before, at(2026, 11, 19, 7, 0)) is True
    assert mw.is_due("daily 07:00", before,
                     at(2026, 11, 19, 6, 59, 59)) is False


def test_weekly_schedule_math():
    # 2026-11-19 is a Thursday; the default is Monday 07:30.
    thursday = at(2026, 11, 19, 12, 0)
    assert mw.next_fire("weekly Monday 07:30", thursday) == \
        at(2026, 11, 23, 7, 30)
    # Already Monday but before the clock time: later the same day.
    monday_early = at(2026, 11, 23, 7, 0)
    assert mw.next_fire("weekly Monday 07:30", monday_early) == \
        at(2026, 11, 23, 7, 30)
    # Already Monday after the clock time: next week.
    monday_late = at(2026, 11, 23, 8, 0)
    assert mw.next_fire("weekly Monday 07:30", monday_late) == \
        at(2026, 11, 30, 7, 30)


@pytest.mark.parametrize("bad", ["", "sometimes", "every 0h", "daily 25:00",
                                 "weekly Funday 07:30", "every 2 weeks"])
def test_invalid_schedules_are_refused(bad):
    with pytest.raises(mw.WakeupError):
        mw.normalize_schedule(bad)


# --------------------------------------------------------------------------- #
# Config writer
# --------------------------------------------------------------------------- #

def test_set_schedule_writes_canonical_complete_config(tmp_path):
    cfg_path, _ = _paths(tmp_path)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"note": "keep me",
                   "wakeups": {"health": {"schedule": "every 90 minutes",
                                          "custom": 7}}}, f)

    res = mw.set_schedule("health", "every 1 hour", path=cfg_path)
    assert res["ok"] and res["schedule"] == "every 1h"
    assert res["previous"] == "every 90m"
    data = json.loads(open(cfg_path, encoding="utf-8").read())
    assert data["note"] == "keep me"
    assert data["wakeups"]["health"] == {
        "enabled": True, "schedule": "every 1h", "custom": 7}
    # The writer fills the rest of the schema so the daemon never sees a
    # partial file.
    assert data["wakeups"]["settings"]["schedule"] == "weekly Monday 07:30"


def test_disable_persists_and_loads_back(tmp_path):
    cfg_path, _ = _paths(tmp_path)
    mw.set_enabled("memory", False, path=cfg_path)
    cfg = mw.load_config(cfg_path)
    assert cfg["wakeups"]["memory"]["enabled"] is False
    assert cfg["wakeups"]["health"]["enabled"] is True
    with pytest.raises(mw.WakeupError):
        mw.set_enabled("nope", False, path=cfg_path)
    with pytest.raises(mw.WakeupError):
        mw.set_schedule("health", "someday", path=cfg_path)


def test_corrupt_config_falls_back_to_defaults(tmp_path):
    cfg_path, _ = _paths(tmp_path)
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("{not json")
    cfg = mw.load_config(cfg_path)
    assert cfg["wakeups"]["health"]["schedule"] == "every 2h"
    assert all(cfg["wakeups"][n]["enabled"] for n in mw.NAMES)


# --------------------------------------------------------------------------- #
# Tick: seeding, firing, restart persistence
# --------------------------------------------------------------------------- #

def test_first_tick_seeds_instead_of_dumping_four_prompts(tmp_path):
    cfg_path, state_path = _paths(tmp_path)
    _write_config(cfg_path, enabled=mw.NAMES)
    rec = Recorder()
    now = at(2026, 11, 19, 10, 0)

    res = mw.tick(now=now, config_path=cfg_path, state_path=state_path,
                  inject=rec, profile="general")
    assert res["ok"] is True and res["fired"] == []
    assert set(res["seeded"]) == set(mw.NAMES)
    assert rec.calls == []
    st = mw.load_state(state_path)
    assert st["seeded_at"] == now
    assert set(st["last_fired"]) == set(mw.NAMES)


def test_each_interval_fires_once_and_restart_remembers(tmp_path):
    cfg_path, state_path = _paths(tmp_path)
    _write_config(cfg_path, enabled=("health",))
    rec = Recorder()
    start = at(2026, 11, 19, 10, 0)
    mw.tick(now=start, config_path=cfg_path, state_path=state_path,
            inject=rec, profile="general")

    first = mw.tick(now=start + 2 * 3600, config_path=cfg_path,
                    state_path=state_path, inject=rec, profile="general")
    assert first["fired"] == ["health"]
    assert rec.calls[0]["text"] == mw.PROMPTS["health"]
    assert rec.calls[0]["mode"] == "auto"

    # 60 s later (a daemon restart for all this module knows) -> persisted
    # last-fired suppresses the duplicate.
    again = mw.tick(now=start + 2 * 3600 + 60, config_path=cfg_path,
                    state_path=state_path, inject=rec, profile="general")
    assert again["fired"] == [] and len(rec.calls) == 1
    assert mw.load_state(state_path)["last_fired"]["health"] == \
        start + 2 * 3600

    # The NEXT interval fires exactly once, from the persisted timestamp.
    next_one = mw.tick(now=start + 4 * 3600, config_path=cfg_path,
                       state_path=state_path, inject=rec, profile="general")
    assert next_one["fired"] == ["health"] and len(rec.calls) == 2


def test_daily_and_weekly_fire_at_their_local_wall_clock(tmp_path):
    cfg_path, state_path = _paths(tmp_path)
    _write_config(cfg_path, enabled=("memory", "settings"))
    rec = Recorder()
    start = at(2026, 11, 19, 6, 0)  # Thursday
    mw.tick(now=start, config_path=cfg_path, state_path=state_path,
            inject=rec, profile="general")

    daily = mw.tick(now=at(2026, 11, 19, 7, 0), config_path=cfg_path,
                    state_path=state_path, inject=rec, profile="general")
    assert daily["fired"] == ["memory"]

    # Monday 07:30 also catches up the daily wake-up (its Friday/Saturday/...
    # occurrences passed while the daemon was down), but settings must fire.
    weekly = mw.tick(now=at(2026, 11, 23, 7, 30), config_path=cfg_path,
                     state_path=state_path, inject=rec, profile="general")
    assert "settings" in weekly["fired"]


# --------------------------------------------------------------------------- #
# Delivery: busy -> queue, missing pane -> skip + doctor
# --------------------------------------------------------------------------- #

def test_busy_pane_is_queued_via_auto_mode(tmp_path):
    cfg_path, state_path = _paths(tmp_path)
    _write_config(cfg_path, enabled=("health",))
    repl = BusyRepl(busy=True)
    start = at(2026, 11, 19, 10, 0)
    mw.tick(now=start, config_path=cfg_path, state_path=state_path,
            repl=repl, profile="general")

    res = mw.tick(now=start + 2 * 3600, config_path=cfg_path,
                  state_path=state_path, repl=repl, profile="general")
    assert res["fired"] == ["health"]
    assert repl.calls[0]["mode"] == "auto", "delivery must let auto decide"
    result = mw.load_state(state_path)["last_result"]["health"]
    assert result["ok"] is True and result["mode"] == "queue"


def test_missing_pane_is_skipped_logged_and_persisted(tmp_path, caplog):
    cfg_path, state_path = _paths(tmp_path)
    _write_config(cfg_path, enabled=("health",))
    rec = Recorder(error=PaneError(
        "no_panes", "no tmux pane running a Hermes REPL for profile 'general'"))
    start = at(2026, 11, 19, 10, 0)
    mw.tick(now=start, config_path=cfg_path, state_path=state_path,
            inject=rec, profile="general")
    with caplog.at_level("WARNING"):
        res = mw.tick(now=start + 2 * 3600, config_path=cfg_path,
                      state_path=state_path, inject=rec, profile="general")
    assert res["ok"] is True
    assert res["fired"] == []
    assert res["skipped"][0]["name"] == "health"
    assert res["skipped"][0]["reason"] == "pane-missing"
    assert "skipped health" in caplog.text
    st = mw.load_state(state_path)
    assert st["last_result"]["health"]["skipped"] == "pane-missing"
    # The occurrence was handled: no retry storm on the next 30 s tick.
    retry = mw.tick(now=start + 2 * 3600 + 30, config_path=cfg_path,
                    state_path=state_path, inject=rec, profile="general")
    assert retry["skipped"] == [] and len(rec.calls) == 1


def test_doctor_says_the_pane_is_missing(tmp_path):
    cfg_path, state_path = _paths(tmp_path)
    _write_config(cfg_path, enabled=("health",))
    rec = Recorder(error=PaneError("no_panes", "no pane"))
    start = at(2026, 11, 19, 10, 0)
    mw.tick(now=start, config_path=cfg_path, state_path=state_path,
            inject=rec, profile="general")
    mw.tick(now=start + 2 * 3600, config_path=cfg_path,
            state_path=state_path, inject=rec, profile="general")

    live = mw.doctor_check(
        now=start + 2 * 3600 + 60, config_path=cfg_path,
        state_path=state_path, pane_check=lambda: {"state": "missing"},
        daemon_running=lambda: True)
    assert live["ok"] is False and live["fatal"] is False
    assert "pane missing" in live["detail"]
    assert live["fix"] == "hscc main start"

    # Even after the pane comes back, the persisted skip still says so until
    # the next successful fire.
    after = mw.doctor_check(
        now=start + 2 * 3600 + 120, config_path=cfg_path,
        state_path=state_path, pane_check=lambda: {"state": "running"},
        daemon_running=lambda: True)
    assert after["ok"] is False and "pane missing" in after["detail"]


def test_doctor_healthy_when_pane_present_and_results_are_sent(tmp_path):
    cfg_path, state_path = _paths(tmp_path)
    _write_config(cfg_path, enabled=("health", "sweep"))
    rec = Recorder(mode="plain")
    start = at(2026, 11, 19, 10, 0)
    mw.tick(now=start, config_path=cfg_path, state_path=state_path,
            inject=rec, profile="general")
    mw.tick(now=start + 3 * 3600, config_path=cfg_path,
            state_path=state_path, inject=rec, profile="general")
    res = mw.doctor_check(
        now=start + 3 * 3600 + 60, config_path=cfg_path,
        state_path=state_path, pane_check=lambda: {"state": "running"},
        daemon_running=lambda: True)
    assert res["ok"] is True and res["fatal"] is False
    assert "health every 2h" in res["detail"]


def test_all_disabled_is_a_clean_doctor_pass(tmp_path):
    cfg_path, state_path = _paths(tmp_path)
    _write_config(cfg_path, enabled=())
    res = mw.doctor_check(now=at(2026, 11, 19, 10, 0),
                          config_path=cfg_path, state_path=state_path,
                          pane_check=lambda: {"state": "missing"})
    assert res["ok"] is True and "disabled" in res["detail"]


# --------------------------------------------------------------------------- #
# Daemon wiring
# --------------------------------------------------------------------------- #

def test_daemon_runs_the_wakeup_stream(monkeypatch):
    assert daemon_ops.PERIODIC_INTERVALS["main_wakeups"] == 30
    assert mw.STREAM_NAME == "main_wakeups"
    monkeypatch.setattr(mw, "tick", lambda: {"ok": True})
    assert mw.check_main_wakeups() is True


def test_tick_stamps_a_verify_readable_stream_state(tmp_path):
    cfg_path, state_path = _paths(tmp_path)
    rec = Recorder()
    mw.tick(now=at(2026, 11, 19, 10, 0), config_path=cfg_path,
            state_path=state_path, inject=rec, profile="general")
    doc = json.loads(open(state_path, encoding="utf-8").read())
    assert doc["stream"] == "main_wakeups"
    assert doc["ok"] is True
    assert doc["timestamp"], "verify needs the ISO stamp"
    assert doc["last_tick"] == at(2026, 11, 19, 10, 0)


# --------------------------------------------------------------------------- #
# CLI surface: hscc main wakeups list|set|enable|disable
# --------------------------------------------------------------------------- #

def _cli(argv):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = mc.cmd_main(argv)
    return rc, out.getvalue()


def test_cli_list_shows_the_four_defaults(tmp_path):
    rc, out = _cli(["wakeups", "list"])
    assert rc == 0, out
    assert f"main wakeups ({mw._main_profile()})" in out
    for name in mw.NAMES:
        assert name in out
    assert "every 2h" in out and "weekly Monday 07:30" in out


def test_cli_set_and_disable_round_trip(tmp_path):
    rc, out = _cli(["wakeups", "set", "health", "every 1h", "--json"])
    assert rc == 0, out
    payload = json.loads(out)
    assert payload["action"] == "set" and payload["schedule"] == "every 1h"
    assert {w["name"]: w for w in payload["wakeups"]}["health"][
        "schedule"] == "every 1h"

    rc, out = _cli(["wakeups", "disable", "memory"])
    assert rc == 0 and "memory disabled" in out
    rc, out = _cli(["wakeups", "list", "--json"])
    assert rc == 0
    memory = {w["name"]: w for w in json.loads(out)["wakeups"]}["memory"]
    assert memory["enabled"] is False
    assert memory["prompt"] == mw.PROMPTS["memory"]


def test_cli_errors_are_refusals_not_writes(tmp_path):
    rc, out = _cli(["wakeups", "set", "health", "sometimes"])
    assert rc == 1 and "unrecognised schedule" in out
    rc, out = _cli(["wakeups", "enable", "nope"])
    assert rc == 1 and "unknown wake-up" in out
    rc, out = _cli(["wakeups", "bogus"])
    assert rc == 1 and "unknown main wakeups subcommand" in out


def test_cli_help_is_inert(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("help must not read or write wake-ups")

    monkeypatch.setattr(mw, "list_receipt", boom)
    monkeypatch.setattr(mw, "set_schedule", boom)
    monkeypatch.setattr(mw, "set_enabled", boom)
    for argv in (["wakeups"], ["wakeups", "--json"],
                 ["wakeups", "--help"], ["wakeups", "list", "-h"],
                 ["wakeups", "set", "--help"]):
        rc, out = _cli(argv)
        assert rc == 0, (argv, out)
        assert "hscc main wakeups" in out
