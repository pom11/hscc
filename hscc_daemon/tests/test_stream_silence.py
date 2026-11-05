"""Stream-silence watchdog (t_291ced43, incident 2026-11-05).

Hermetic only: fake roster/completion/notify/state seams, fabricated tmp log
files. No live hermes, board, desktop or network access.

The card's matrix:
  (a) fresh heartbeat + completion 60 s ago -> quiet;
  (b) fresh heartbeat + last completion 700 s ago -> WARN + ONE notify, then
      cooldown (WARN still logged, no second notify);
  (c) stale heartbeat -> suppressed (dead-pane cases belong to orch_waker);
  (d) no running cards -> no alert/state churn;
  plus: the real log parser reads `latency=` out of a fabricated agent.log and
  ignores `Stream stale` lines, and the verify row is never red by itself.
"""

import datetime
import json
import os

from hscc_daemon import stream_silence as ss


class Harness:
    def __init__(self, roster, completions, threshold=600, heartbeat_fresh=600,
                 cooldown=1800, prior=None):
        self.roster = roster
        self.completions = completions
        self.threshold = threshold
        self.heartbeat_fresh = heartbeat_fresh
        self.cooldown = cooldown
        self.prior = dict(prior or {})
        self.calls = {"notify": [], "states": [], "events": [], "logs": []}

    def roster_fn(self, now):
        return self.roster

    def completion_fn(self, profile, now):
        return self.completions.get(profile) or {
            "last_completed_age_s": None, "source": "no agent.log"}

    def notify(self, title, body, priority="normal"):
        self.calls["notify"].append((title, body, priority))

    def write_state(self, stream, data):
        assert stream == ss.STREAM
        self.calls["states"].append(dict(data))
        self.prior = dict(data)

    def read_state(self, stream):
        return dict(self.prior)

    def emit(self, event_type, payload, severity="info"):
        self.calls["events"].append((event_type, payload, severity))

    def tick(self, now=1000000.0):
        real_log = ss.log
        ss.log = lambda msg, level="INFO": self.calls["logs"].append(
            (level, msg))
        try:
            return ss.check_stream_silence(
                roster_fn=self.roster_fn,
                completion_fn=self.completion_fn,
                notify_fn=self.notify,
                write_state_fn=self.write_state,
                read_state_fn=self.read_state,
                emit_fn=self.emit,
                now=now, wall_now=now,
                threshold_s=self.threshold,
                heartbeat_fresh_s=self.heartbeat_fresh,
                notify_cooldown_s=self.cooldown,
            )
        finally:
            ss.log = real_log

    def last_state(self):
        return self.calls["states"][-1]

    def warned(self):
        return [m for lvl, m in self.calls["logs"] if lvl == "WARN"]


def _card(key="card:hscc:t_1", profile="worker", hb=30):
    return {"key": key, "kind": "card", "profile": profile,
            "heartbeat_age_s": hb}


def test_fresh_heartbeat_and_recent_completion_is_quiet():
    h = Harness({"sessions": [_card(hb=30)]},
                {"worker": {"last_completed_age_s": 60, "source": "agent.log"}})
    assert h.tick() is True
    st = h.last_state()
    assert st["ok"] is True and st["status"] == "healthy"
    assert st["violations"] == [] and st["alerted"] == {}
    assert h.calls["notify"] == []
    assert h.warned() == []
    assert st["sessions"][0]["last_completed_age_s"] == 60


def test_silent_running_card_warns_once_then_cooldown():
    h = Harness({"sessions": [_card(hb=20)]},
                {"worker": {"last_completed_age_s": 700, "source": "agent.log"}})
    assert h.tick() is True
    st = h.last_state()
    assert st["ok"] is True and st["status"] == "silent"
    assert st["violations"] == ["card:hscc:t_1"]
    assert len(h.calls["notify"]) == 1
    assert h.calls["notify"][0][2] == "high"
    assert [e[0] for e in h.calls["events"]] == ["stream_silence.warn"]
    assert any("has not completed a model call" in m for m in h.warned())

    # 60 s later: still silent, inside the 1800 s cooldown -> WARN, no notify
    h.completions["worker"] = {
        "last_completed_age_s": 760, "source": "agent.log"}
    assert h.tick(1000060.0) is True
    assert len(h.calls["notify"]) == 1
    assert any("cooldown" in m for m in h.warned())
    assert h.last_state()["alerted"]["card:hscc:t_1"] == 1000000.0

    # After the cooldown, a still-silent session escalates again (one notify).
    h.completions["worker"] = {
        "last_completed_age_s": 2500, "source": "agent.log"}
    assert h.tick(1001900.0) is True
    assert len(h.calls["notify"]) == 2


def test_stale_heartbeat_is_suppressed():
    h = Harness({"sessions": [_card(hb=900)]},
                {"worker": {"last_completed_age_s": 700, "source": "agent.log"}})
    assert h.tick() is True
    st = h.last_state()
    assert st["status"] == "suppressed"
    assert st["suppressed"] == ["card:hscc:t_1"]
    assert st["violations"] == [] and h.calls["notify"] == []
    assert h.warned() == []


def test_no_running_cards_makes_no_alert_state():
    h = Harness({"sessions": []}, {})
    assert h.tick() is True
    st = h.last_state()
    assert st["status"] == "healthy"
    assert st["sessions"] == [] and st["alerted"] == {}
    assert st["violations"] == [] and h.calls["notify"] == []


def test_unknown_completion_is_never_guessed_or_notified():
    h = Harness({"sessions": [_card(hb=10)]}, {})
    assert h.tick() is True
    st = h.last_state()
    assert st["status"] == "unknown"
    assert st["unknown"] == ["card:hscc:t_1"]
    assert st["sessions"][0]["last_completed_age_s"] is None
    assert h.calls["notify"] == [] and h.warned() == []


def _write_log(tmp_path, lines):
    home = tmp_path / "hermes"
    pdir = home / "profiles" / "worker"
    pdir.mkdir(parents=True)
    path = pdir / "agent.log"
    path.write_text("\n".join(lines) + "\n")
    return home, path


def test_log_parser_uses_newest_latency_completion(tmp_path):
    now = 1_000_000.0
    fmt = lambda age: datetime.datetime.fromtimestamp(
        now - age).strftime("%Y-%m-%d %H:%M:%S")
    home, _ = _write_log(tmp_path, [
        f"{fmt(1000)} INFO call complete latency=12.3s",
        f"{fmt(60)} INFO call complete latency=8.1s tokens=42",
        f"{fmt(5)} WARN Stream stale for 300s (no bytes)",
    ])
    age, source = ss.last_completion_age("worker", now=now, hermes_home=home)
    assert 58 <= age <= 62
    assert source == "agent.log"

    # Only a stale warning, no completion: UNKNOWN, never treated as fresh.
    home2, _ = _write_log(tmp_path / "second", [
        f"{fmt(5)} WARN Stream stale for 300s (no bytes)"])
    age2, why = ss.last_completion_age("worker", now=now, hermes_home=home2)
    assert age2 is None and "no completion" in why


def test_log_parser_bounds_to_tail(tmp_path):
    now = 1_000_000.0
    old = datetime.datetime.fromtimestamp(
        now - 5000).strftime("%Y-%m-%d %H:%M:%S")
    new = datetime.datetime.fromtimestamp(
        now - 120).strftime("%Y-%m-%d %H:%M:%S")
    filler = "x" * 2048
    home = tmp_path / "hermes"
    pdir = home / "profiles" / "worker"
    pdir.mkdir(parents=True)
    (pdir / "agent.log").write_text(
        "\n".join([f"{old} INFO latency=1s"] +
                  [f"{old} INFO {filler}"] * 200 +
                  [f"{new} INFO latency=2s"]) + "\n")
    age, _ = ss.last_completion_age("worker", now=now, hermes_home=home)
    assert 118 <= age <= 122


def test_verify_row_is_informational_and_marks_unknown(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / f"{ss.STREAM}.json").write_text(json.dumps({
        "ok": True, "status": "silent", "violations": ["card:b:t_1"],
        "sessions": [
            {"key": "card:b:t_1", "last_completed_age_s": 700,
             "heartbeat_age_s": 20, "status": "silent"},
            {"key": "orch:b-orch", "last_completed_age_s": None,
             "heartbeat_age_s": 10, "status": "unknown"},
        ],
    }))
    row = ss.stream_silence_row(state_dir=str(state_dir))
    assert row["name"] == "stream_silence" and row["ok"] is True
    assert "card:b:t_1=11m WARN" in row["detail"]
    assert "orch:b-orch=○" in row["detail"]

    missing = ss.stream_silence_row(state_dir=str(tmp_path / "absent"))
    assert missing["ok"] is True and "skipped" in missing["detail"]
