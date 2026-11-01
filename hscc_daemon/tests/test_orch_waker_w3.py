"""W3 — the orch-waker's daemon wiring, config knobs, status + doctor check.

Offline only (like the W1/W2 suites): tmp config/state/audit paths, an
injected clock, stub stream-state and stub run_tick. No live daemon, no tmux,
no injections, no real ~/.hscc (conftest's autouse fixture redirects the whole
tree and ``state.STATE_DIR`` per test).

What each group pins:

  * config — the card's knobs (``orch_waker.enabled`` default ON +
    debounce_s/min_gap_s/poll_s) resolve to the stopgap's tuning when absent,
    fail OPEN to ON on garbage (deliberately NOT autodown's fail-closed), and
    a garbage knob can never become "nudge every tick".
  * loop wiring — the cadence lives in ``PERIODIC_INTERVALS`` (the SOT), the
    CHECKS table maps the stream to the waker's check, and the loop really
    starts THAT pair (captured through a patched supervision seam); a raising
    tick is isolated AND still stamps, and the stamp flows into verify's
    per-stream staleness math (600 s floor for a 30 s stream).
  * disabled — the tick is a no-op (no board touched) but the stream is still
    stamped, so staleness never false-fires on a deliberately-off feature;
    and an out-of-process flip takes effect on the NEXT tick (hot reload).
  * status/doctor — the CLI/doctor surface renders the truth it is given and
    the doctor verdicts are ok / warn / disabled + the never-ticked RED rule.
"""

import json
import os
import pathlib

import pytest

from hscc_daemon import orch_waker as ow

NOW = 1_800_000_000


def cfg_path(tmp_path):
    """A per-test config path in its OWN dir (tmp_path itself also holds the
    suite's fence fixtures, so dir-level assertions must be scoped to this)."""
    d = tmp_path / "cfg"
    d.mkdir(exist_ok=True)
    return str(d / "orch_waker.json")


class StreamSpy:
    """Stands in for state.write_state/read_state so stamps are inspectable."""

    def __init__(self, initial=None):
        self.writes = []
        self.stored = dict(initial or {})

    def write(self, name, data):
        entry = {"stream": name, **data}
        self.stored = entry
        self.writes.append(entry)
        return entry

    def read(self, name):
        return dict(self.stored) if self.stored.get("stream") == name else None


# ── config ──────────────────────────────────────────────────────────────────

def test_config_defaults_are_on_with_the_stopgap_tuning():
    assert ow.CONFIG_DEFAULTS == {"enabled": True, "debounce_s": 90,
                                  "min_gap_s": 300, "poll_s": 30}


def test_config_defaults_when_file_absent(tmp_path):
    """Absent config = ENABLED. The card's default-ON rule, and deliberately
    NOT autodown's fail-closed: a lost file that silenced the waker is the
    exact failure the stopgap exists to prevent."""
    cfg = ow.load_config(cfg_path(tmp_path))
    assert cfg["enabled"] is True
    assert (cfg["debounce_s"], cfg["min_gap_s"], cfg["poll_s"]) == (90, 300, 30)


def test_config_corrupt_file_fails_open_to_enabled(tmp_path):
    p = cfg_path(tmp_path)
    with open(p, "w") as f:
        f.write("{not json at all")
    assert ow.load_config(p)["enabled"] is True


def test_config_non_object_document_fails_open(tmp_path):
    p = cfg_path(tmp_path)
    with open(p, "w") as f:
        json.dump(["a", "list"], f)
    assert ow.load_config(p)["enabled"] is True


def test_config_partial_file_still_complete(tmp_path):
    p = cfg_path(tmp_path)
    with open(p, "w") as f:
        json.dump({"enabled": False}, f)
    cfg = ow.load_config(p)
    assert cfg["enabled"] is False
    assert (cfg["debounce_s"], cfg["min_gap_s"], cfg["poll_s"]) == (90, 300, 30)


def test_config_stray_key_never_reaches_the_tick(tmp_path):
    p = cfg_path(tmp_path)
    with open(p, "w") as f:
        json.dump({"enabled": True, "poll_s": 45, "rm_rf": True}, f)
    cfg = ow.load_config(p)
    assert set(cfg) == set(ow.CONFIG_DEFAULTS)
    assert cfg["poll_s"] == 45


@pytest.mark.parametrize("bad", [True, "90", None, [], -5, float("nan"),
                                 float("inf")])
def test_knob_garbage_falls_back_to_the_default(tmp_path, bad):
    """A typo in a seconds field must not become 'nudge every tick'."""
    p = cfg_path(tmp_path)
    with open(p, "w") as f:
        json.dump({"debounce_s": bad}, f)
    assert ow.load_config(p)["debounce_s"] == ow.DEBOUNCE_S


def test_config_zero_poll_s_cannot_create_a_hot_loop(tmp_path):
    """poll_s has a floor of 1; 0 (=> scan every instant) is rejected and the
    default cadence is used instead — a typo can never make the stream hot."""
    p = cfg_path(tmp_path)
    with open(p, "w") as f:
        json.dump({"poll_s": 0}, f)
    assert ow.stream_interval(ow.load_config(p)) == ow.POLL_S == 30


def test_set_enabled_creates_file_and_flips(tmp_path):
    p = cfg_path(tmp_path)
    assert ow.load_config(p)["enabled"] is True          # absent -> ON
    block = ow.set_enabled(False, p)
    assert block["enabled"] is False
    assert ow.load_config(p)["enabled"] is False
    assert json.loads(open(p).read())["enabled"] is False
    ow.set_enabled(True, p)
    assert ow.load_config(p)["enabled"] is True


def test_set_enabled_writes_complete_schema_keeps_other_keys(tmp_path):
    """The file is operator-owned: a flip writes every knob but must never
    drop an unrelated key the operator added by hand."""
    p = cfg_path(tmp_path)
    with open(p, "w") as f:
        json.dump({"enabled": True, "operator_note": "keep me"}, f)
    ow.set_enabled(False, p)
    doc = json.loads(open(p).read())
    assert doc["operator_note"] == "keep me"
    assert set(ow.CONFIG_DEFAULTS) <= set(doc)


def test_set_enabled_refuses_a_non_object_document(tmp_path):
    p = cfg_path(tmp_path)
    with open(p, "w") as f:
        f.write("[1, 2, 3]")
    with pytest.raises(ValueError):
        ow.set_enabled(True, p)
    assert open(p).read() == "[1, 2, 3]"       # untouched, not clobbered


def test_set_enabled_leaves_no_tmp_litter(tmp_path):
    p = cfg_path(tmp_path)
    ow.set_enabled(True, p)
    assert not os.path.exists(p + ".tmp")
    # The config dir holds ONLY the config file (tmp_path itself also has the
    # suite's fakebin/, so the check is scoped to the directory we created).
    cfg_dir = os.path.dirname(p)
    assert sorted(os.listdir(cfg_dir)) == ["orch_waker.json"]


# ── cadence seam (PERIODIC_INTERVALS stays the SOT) ────────────────────────

def test_stream_cadence_reads_the_daemons_single_source_of_truth():
    from hscc_daemon import daemon_ops
    assert (ow.stream_cadence()
            == daemon_ops.PERIODIC_INTERVALS["orch_waker"] == 30)


def test_effective_scan_interval_is_cadence_and_poll_the_max():
    assert ow.effective_scan_interval({"poll_s": 30}) == 30    # defaults
    assert ow.effective_scan_interval({"poll_s": 120}) == 120  # slower poll
    assert ow.effective_scan_interval({"poll_s": 5}) == 30     # cannot beat loop


# ── daemon-loop wiring ──────────────────────────────────────────────────────

def test_waker_stream_declares_its_cadence_in_the_sot():
    """The card's rule: cadence is declared in PERIODIC_INTERVALS, not a
    hardcoded sleep loop — 30 s, and verify's per-stream rule then widens it
    to max(600, 2*30+90) = the 600 s floor like any other stream."""
    from hscc_daemon import daemon_ops
    assert daemon_ops.PERIODIC_INTERVALS["orch_waker"] == 30
    _JITTER_S, _EXTRA = 90, 2
    limit = max(600, _EXTRA * daemon_ops.PERIODIC_INTERVALS["orch_waker"]
                + _JITTER_S)
    assert limit == 600


def test_waker_stream_check_is_registered_in_the_loop(monkeypatch):
    """run_daemon_loop must hand the waker's check + PERIODIC_INTERVALS'
    cadence to the standard per-stream supervision — if the CHECKS table had
    no entry the cadence would start NO thread (daemon_ops' own comment about
    the globals().get trap). The supervision seam is patched, so the loop
    never runs a check body here; it proves the WIRING, not the tick."""
    from hscc_daemon import daemon_ops, daemon_singleton as ds
    seen = {}

    def fake_supervised(stop_event, check_fn, interval, stream_name):
        if stream_name == "orch_waker":
            seen["fn"] = check_fn
            seen["interval"] = interval
        stop_event.set()

    for path in ("hscc_daemon.autodown.resume_from_restart_defensive",
                 "hscc_daemon.serving.resolve_cluster_config",
                 "hscc_daemon.sparkrun_bin.ensure_on_path",
                 "hscc_daemon.recover.recover_engine_wedge",
                 "hscc_daemon.dispatcher_wedge.recover_dispatcher_wedge",
                 "hscc_daemon.lifecycle.pipeline_watchdog",
                 "hscc_daemon.trigger.trigger_engine"):
        monkeypatch.setattr(path, lambda *a, **k: None)
    monkeypatch.setattr(ds, "hold_daemon_loop_lock_fail_open",
                        lambda **k: (True, ""))
    monkeypatch.setattr(daemon_ops, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon_ops, "run_autodown_loop", lambda ev, **k: None)
    monkeypatch.setattr(daemon_ops, "_run_supervised_periodic",
                        fake_supervised)
    monkeypatch.setattr(daemon_ops, "run_heartbeat_loop",
                        lambda ev, **k: ev.wait(2))
    monkeypatch.setattr(daemon_ops, "write_stopped", lambda: None)
    monkeypatch.setattr(daemon_ops, "prune_dead_files",
                        lambda *a, **k: {"removed_dead": 0, "pruned_bak": 0})

    daemon_ops.run_daemon_loop()

    assert seen.get("interval") == 30, "cadence must come from PERIODIC_INTERVALS"
    assert seen.get("fn") is ow.check_orch_waker


def test_check_orch_waker_reports_the_tick_verdict(tmp_path, monkeypatch):
    """The loop's check-fn convention: the stream is ok iff the tick stamped
    ok (post_boot.check_post_boot_relaunch uses the same convention)."""
    monkeypatch.setattr(ow, "waker_tick", lambda **kw: {"ok": True})
    assert ow.check_orch_waker() is True
    monkeypatch.setattr(ow, "waker_tick", lambda **kw: {"ok": False})
    assert ow.check_orch_waker() is False


def test_check_orch_waker_survives_a_raising_tick(tmp_path, monkeypatch):
    """Defence in depth: even if waker_tick itself ever let a raise escape,
    the check body must return False, not propagate (a raise out of a check
    is what the supervisor treats as a thread death)."""
    def boom(**kw):
        raise RuntimeError("escaped")
    monkeypatch.setattr(ow, "waker_tick", boom)
    monkeypatch.setattr("hscc_daemon.daemon_ops.log", lambda *a, **k: None)
    assert ow.check_orch_waker() is False


def test_failing_waker_tick_is_isolated_and_still_stamps(tmp_path, monkeypatch):
    """A raising waker must not take the stream down (the loop's supervision
    respawns the thread — pinned by test_daemon_ops for every stream) and the
    stamp must still land, so a failure reads as a FAILED stream rather than
    a stalled one."""
    spy = StreamSpy()
    monkeypatch.setattr(ow, "run_tick",
                        lambda **kw: (_ for _ in ()).throw(
                            RuntimeError("waker internal boom")))
    entry = ow.waker_tick(now=NOW, config_path=cfg_path(tmp_path),
                          write=spy.write, read=spy.read)
    assert entry["ok"] is False
    assert "boom" in entry["message"]
    assert spy.writes[0]["last_tick"] == NOW, "a failed tick still stamps"
    assert ow.check_orch_waker.__doc__     # the loop entry point exists


def test_stream_still_flows_into_the_staleness_math(tmp_path):
    """verify's per-stream check reads every *.json in the state dir: the
    waker's stamp must satisfy its own widened window, and a genuinely dead
    waker must still be caught — the 'like any other stream' requirement."""
    from datetime import datetime, timedelta, timezone

    from hscc_daemon.state import STATE_DIR
    from hscc_daemon.verify import check_daemon_streams

    os.makedirs(STATE_DIR, exist_ok=True)

    def stamp(age_s):
        ts = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
        with open(os.path.join(STATE_DIR, "orch_waker.json"), "w") as f:
            json.dump({"ok": True, "timestamp": ts, "last_check": ts,
                       "stream": "orch_waker", "last_tick": NOW}, f)

    stamp(400)                       # 400s old: inside the 600s floor
    assert check_daemon_streams(state_dir=STATE_DIR)["ok"] is True
    stamp(700)                       # beyond it: stale, like any dead stream
    res = check_daemon_streams(state_dir=STATE_DIR)
    assert res["ok"] is False and "orch_waker.json: stale" in res["detail"]


# ── config off => no-op + stream stamped ────────────────────────────────────

def test_disabled_tick_is_a_noop_but_still_stamps(tmp_path, monkeypatch):
    """The card's rule: disabled => the tick does nothing but STILL stamps, so
    staleness never false-fires on a deliberately-off feature."""
    p = cfg_path(tmp_path)
    ow.set_enabled(False, p)
    called = []
    monkeypatch.setattr(ow, "run_tick", lambda **kw: called.append(kw) or [])
    spy = StreamSpy()
    entry = ow.waker_tick(now=NOW, config_path=p, write=spy.write,
                          read=spy.read, state_path=str(tmp_path / "state.json"))
    assert called == [], "disabled must not scan a single board"
    assert entry["skipped"] == "disabled" and entry["ok"] is True
    assert spy.writes[0]["last_tick"] == NOW


def test_enabled_after_disable_takes_effect_next_tick(tmp_path, monkeypatch):
    """Hot reload: the tick re-reads the file every pass, so an out-of-process
    flip needs no daemon restart (the _autodown_tick contract)."""
    p = cfg_path(tmp_path)
    spy = StreamSpy()
    seen = []
    monkeypatch.setattr(ow, "run_tick", lambda **kw: seen.append(1) or [])
    ow.waker_tick(now=NOW, config_path=p, write=spy.write, read=spy.read)
    assert len(seen) == 1
    ow.set_enabled(False, p)                       # CLI flip, another process
    ow.waker_tick(now=NOW + 30, config_path=p, write=spy.write, read=spy.read)
    assert len(seen) == 1, "the next tick must already honour the flip"
    ow.set_enabled(True, p)
    ow.waker_tick(now=NOW + 60, config_path=p, write=spy.write, read=spy.read)
    assert len(seen) == 2


def test_poll_gate_skips_the_scan_but_still_stamps(tmp_path, monkeypatch):
    """poll_s longer than the stream cadence: the loop still ticks every 30 s
    (the stream stays fresh) while real scans only happen every poll_s."""
    p = cfg_path(tmp_path)
    with open(p, "w") as f:
        json.dump({"enabled": True, "poll_s": 120}, f)
    spy = StreamSpy()
    scans = []
    monkeypatch.setattr(ow, "run_tick", lambda **kw: scans.append(kw) or [])
    ow.waker_tick(now=NOW, config_path=p, write=spy.write, read=spy.read)
    assert len(scans) == 1, "first tick scans (nothing scanned yet)"
    ow.waker_tick(now=NOW + 30, config_path=p, write=spy.write, read=spy.read)
    assert len(scans) == 1, "30s < poll 120s must skip the scan"
    assert spy.writes[-1]["skipped"] == "poll_gate"
    assert spy.writes[-1]["last_tick"] == NOW + 30, "skipped tick still stamps"
    ow.waker_tick(now=NOW + 150, config_path=p, write=spy.write, read=spy.read)
    assert len(scans) == 2, "past the poll window it scans again"


def test_tick_knobs_reach_the_run(tmp_path, monkeypatch):
    """debounce_s/min_gap_s from the config must reach the core tick — the
    knob seam W2 left for W3."""
    p = cfg_path(tmp_path)
    with open(p, "w") as f:
        json.dump({"enabled": True, "debounce_s": 15, "min_gap_s": 60}, f)
    seen = {}

    def spy_run(**kw):
        seen.update(kw)
        return []
    monkeypatch.setattr(ow, "run_tick", spy_run)
    ow.waker_tick(now=NOW, config_path=p, write=StreamSpy().write,
                  read=StreamSpy().read)
    assert seen["debounce_s"] == 15 and seen["min_gap_s"] == 60


# ── status surface ──────────────────────────────────────────────────────────

def test_waker_status_reports_enabled_tick_boards_and_audit(tmp_path,
                                                            monkeypatch):
    import hscc_daemon.state as dstate

    p = cfg_path(tmp_path)
    with open(p, "w") as f:
        json.dump({"enabled": True, "poll_s": 30, "debounce_s": 90,
                   "min_gap_s": 300}, f)
    st = tmp_path / "waker-state.json"
    ow.save_state({"last": {"hscc": 41},
                   "pending": {"hscc": [[NOW, "x"], [NOW, "y"]]},
                   "sent": {}}, str(st))
    audit = tmp_path / "orch_waker.jsonl"
    audit.write_text(
        json.dumps({"ts": NOW, "board": "hscc", "profile": "hscc-orch",
                    "ok": True, "outcome": "sent", "msg": "m1"}) + "\n"
        + "THIS LINE IS NOT JSON\n"
        + json.dumps({"ts": NOW + 1, "board": "inc", "profile": "inc-orch",
                      "ok": False, "outcome": "failed: no pane",
                      "msg": "m2"}) + "\n")
    stream_entry = {"stream": "orch_waker", "last_tick": NOW, "ok": True}
    monkeypatch.setattr(dstate, "read_state",
                        lambda name: dict(stream_entry))

    status = ow.waker_status(now=NOW + 45, config_path=p, state_path=str(st),
                             audit_path=str(audit))
    assert status["enabled"] is True
    assert status["stream"]["name"] == "orch_waker"
    assert status["stream"]["last_tick"] == NOW
    assert status["stream"]["tick_age_s"] == 45.0
    assert status["stream"]["interval_s"] == 30
    assert status["boards"] == {"hscc": {"last_id": 41, "pending": 2}}
    # the broken line is skipped, the two good ones kept in order
    assert [e["board"] for e in status["audit"]] == ["hscc", "inc"]


def test_waker_status_never_touched_and_no_state_files(tmp_path):
    """A fresh install: nothing has ever ticked. The status must say so
    honestly (never ticked / no deliveries) rather than fabricate numbers."""
    status = ow.waker_status(now=NOW, config_path=cfg_path(tmp_path),
                             state_path=str(tmp_path / "none.json"),
                             audit_path=str(tmp_path / "none.jsonl"))
    assert status["stream"]["last_tick"] is None
    assert status["stream"]["tick_age_s"] is None
    assert status["boards"] == {}
    assert status["audit"] == []


def test_read_audit_tail_caps_at_requested_lines(tmp_path):
    audit = tmp_path / "orch_waker.jsonl"
    audit.write_text("".join(
        json.dumps({"ts": NOW + i, "board": "b", "ok": True}) + "\n"
        for i in range(9)))
    tail = ow.read_audit_tail(str(audit), lines=5)
    assert [e["ts"] for e in tail] == [NOW + 4, NOW + 5, NOW + 6, NOW + 7,
                                       NOW + 8]


def test_waker_status_is_read_only(tmp_path):
    """status must not write anything: the daemon owns the state files."""
    st = tmp_path / "waker-state.json"
    ow.save_state({"last": {"b": 7}, "pending": {}, "sent": {}}, str(st))
    audit = tmp_path / "a.jsonl"
    audit.write_text(json.dumps({"ts": NOW, "board": "b", "ok": True}) + "\n")
    cfgp = pathlib.Path(cfg_path(tmp_path))
    ow.set_enabled(True, str(cfgp))                 # real config on disk
    watch = [st, audit, cfgp]
    before = [p.read_bytes() for p in watch]
    ow.waker_status(now=NOW, config_path=str(cfgp), state_path=str(st),
                    audit_path=str(audit))
    assert [p.read_bytes() for p in watch] == before
    assert not (tmp_path / "a.jsonl.tmp").exists()


# ── doctor check ────────────────────────────────────────────────────────────

def test_doctor_ok_when_enabled_and_fresh(tmp_path):
    res = ow.doctor_check(now=NOW, config_path=cfg_path(tmp_path),
                          stream_state={"last_tick": NOW - 40},
                          _daemon_running=lambda: True)
    assert res["ok"] is True and res["fatal"] is False
    assert "enabled" in res["detail"] and "40s" in res["detail"]
    assert res["name"] == ow.CHECK_NAME


def test_doctor_budget_is_two_polls_plus_slack():
    assert ow.tick_freshness_limit_s(30) == 2 * 30 + ow.DOCTOR_SLACK_S == 120


def test_doctor_warn_on_stale_tick(tmp_path):
    """Stale is a WARNING (ok=False, fatal=False -> doctor prints ○), never a
    hard failure: it is drift to fix, not a reason to abort an install."""
    res = ow.doctor_check(now=NOW, config_path=cfg_path(tmp_path),
                          stream_state={"last_tick": NOW - 500},
                          _daemon_running=lambda: True)
    assert res["ok"] is False and res["fatal"] is False
    assert "stale" in res["detail"] and "500s" in res["detail"]
    assert "hscc start" in res["fix"]


def test_doctor_boundary_exactly_at_budget_is_ok(tmp_path):
    p = cfg_path(tmp_path)
    limit = ow.tick_freshness_limit_s(30)
    assert ow.doctor_check(now=NOW, config_path=p,
                           stream_state={"last_tick": NOW - limit},
                           _daemon_running=lambda: True)["ok"] is True
    assert ow.doctor_check(now=NOW, config_path=p,
                           stream_state={"last_tick": NOW - limit - 1},
                           _daemon_running=lambda: True)["ok"] is False


def test_doctor_red_only_when_enabled_but_never_ticked_while_running(tmp_path):
    res = ow.doctor_check(now=NOW, config_path=cfg_path(tmp_path),
                          stream_state={}, _daemon_running=lambda: True,
                          _predates_build=lambda: False)
    assert res["ok"] is False and res["fatal"] is True
    assert "never ticked" in res["detail"]


def test_doctor_upgrade_race_is_a_warning_never_fatal(tmp_path):
    """THE fresh/upgrade-bootstrap case (t_2b34b5c7 review finding): doctor is
    Stage 1 and the daemon restart is a LATER stage, so the live daemon may
    have been started from code that has NO waker stream. 'Enabled + alive +
    never stamped' then proves nothing about the wiring — fatal would abort
    every upgrade bootstrap (it did, on the first run of the isolated suite).
    Must print as a warning naming the upgrade race."""
    res = ow.doctor_check(now=NOW, config_path=cfg_path(tmp_path),
                          stream_state={}, _daemon_running=lambda: True,
                          _predates_build=lambda: True)
    assert res["ok"] is False and res["fatal"] is False
    assert "predates this build" in res["detail"]


def test_predates_build_compares_pid_mtime_to_the_module(tmp_path):
    """pid file is written at daemon START, so its mtime is the process age;
    a pid older than this module = the daemon cannot have this stream."""
    pid = tmp_path / "daemon.pid"
    build = tmp_path / "orch_waker.py"
    pid.write_text("4242")
    build.write_text("# module\n")
    os.utime(pid, (NOW - 3600, NOW - 3600))       # started an hour ago
    os.utime(build, (NOW, NOW))                   # module newer
    assert ow._daemon_predates_build(str(pid), str(build)) is True
    os.utime(pid, (NOW + 10, NOW + 10))           # daemon restarted after it
    assert ow._daemon_predates_build(str(pid), str(build)) is False


def test_predates_build_unverdictable_answers_no(tmp_path):
    """Missing pid file (daemon never ran) or missing module: False — an
    unverdictable probe must never turn the fatal branch on (fail-open, same
    posture as the not-running case)."""
    assert ow._daemon_predates_build(str(tmp_path / "no.pid"),
                                     str(tmp_path / "no.py")) is False


def test_doctor_hscc_dir_relocates_every_read(tmp_path):
    """The doctor_notifier_mask convention under bootstrap's sandbox: HOME
    stays the operator's real one, HSCC_DIR points at tmp. With the sandbox
    root given, EVERY read (config, stream state, daemon.pid) resolves under
    it — a live OPERATOR daemon must not make the sandbox's untouched waker
    read fatal, and the state hint in the detail names the sandbox path."""
    root = tmp_path / "sandbox-hscc"
    (root / "state").mkdir(parents=True)
    pid = root / "daemon.pid"
    pid.write_text(str(os.getpid()))          # sandbox daemon IS alive
    res = ow.doctor_check(now=NOW, hscc_dir=str(root),
                          _predates_build=lambda: False)
    assert res["ok"] is False and res["fatal"] is True
    assert str(root) in res["detail"], "the hint names the sandbox state path"
    # flip the sandbox's own config -> disabled verdict proves config reloc too
    ow.set_enabled(False, str(root / "orch_waker.json"))
    res = ow.doctor_check(now=NOW, hscc_dir=str(root))
    assert res["ok"] is True and "disabled" in res["detail"]
    # stamp the sandbox stream (config back ON) -> the relocated read goes
    # green: the stamp lives under the sandbox root, not the operator's
    ow.set_enabled(True, str(root / "orch_waker.json"))
    (root / "state" / "orch_waker.json").write_text(json.dumps(
        {"ok": True, "last_tick": NOW - 5}))
    res = ow.doctor_check(now=NOW, hscc_dir=str(root))
    assert res["ok"] is True and "5s ago" in res["detail"]


def test_doctor_hscc_dir_sandbox_dead_daemon_is_not_running(tmp_path):
    """Symmetric case: sandbox pid file garbage/absent while the OPERATOR's
    real daemon runs — the relocated probe must answer 'not running' (warn),
    never fatal: whoever runs the sandboxed doctor gets a verdict about the
    sandbox, not about someone else's machine."""
    root = tmp_path / "sandbox-hscc2"
    (root / "state").mkdir(parents=True)
    (root / "daemon.pid").write_text("not-a-pid")
    res = ow.doctor_check(now=NOW, hscc_dir=str(root))
    assert res["ok"] is False and res["fatal"] is False
    assert "not running" in res["detail"]


def test_doctor_not_red_when_the_daemon_is_not_running(tmp_path):
    """Bootstrap runs doctor BEFORE the daemon has ever started; a fatal here
    would stop an install over a service that has not had a chance to tick."""
    res = ow.doctor_check(now=NOW, config_path=cfg_path(tmp_path),
                          stream_state={}, _daemon_running=lambda: False)
    assert res["ok"] is False and res["fatal"] is False
    assert "not running" in res["detail"]


def test_doctor_disabled_is_ok_with_a_disabled_note(tmp_path):
    p = cfg_path(tmp_path)
    ow.set_enabled(False, p)
    res = ow.doctor_check(now=NOW, config_path=p, stream_state={},
                          _daemon_running=lambda: True)
    assert res["ok"] is True and res["fatal"] is False
    assert "disabled" in res["detail"]


def test_doctor_disabled_wins_over_a_stale_stamp(tmp_path):
    """An operator who switched the feature OFF must not be warned about a
    stale tick: the staleness is the intended consequence of the choice."""
    p = cfg_path(tmp_path)
    ow.set_enabled(False, p)
    res = ow.doctor_check(now=NOW, config_path=p,
                          stream_state={"last_tick": NOW - 99999},
                          _daemon_running=lambda: True)
    assert res["ok"] is True and "disabled" in res["detail"]


def test_doctor_follows_poll_s_for_the_budget(tmp_path):
    p = cfg_path(tmp_path)
    with open(p, "w") as f:
        json.dump({"enabled": True, "poll_s": 300}, f)
    # 400s ago: stale for poll=30 (budget 120), fresh for poll=300 (budget 660).
    assert ow.doctor_check(now=NOW, config_path=p,
                           stream_state={"last_tick": NOW - 400},
                           _daemon_running=lambda: True)["ok"] is True


def test_doctor_never_raises_on_garbage_stream_state(tmp_path):
    p = cfg_path(tmp_path)
    for junk in (None, {}, {"last_tick": "yesterday"}, {"last_tick": None},
                 {"last_tick": [1, 2]}):
        res = ow.doctor_check(now=NOW, config_path=p, stream_state=junk,
                              _daemon_running=lambda: True)
        assert set(res) == {"name", "ok", "fatal", "detail", "fix"}


def test_doctor_reads_the_stream_state_file(tmp_path):
    """No injected stream_state => it reads the daemon's own state file
    (through the redirected state.STATE_DIR, never the operator's)."""
    import hscc_daemon.state as dstate

    res = ow.doctor_check(now=NOW, config_path=cfg_path(tmp_path),
                          _daemon_running=lambda: True)
    assert res["ok"] is False            # nothing written yet
    dstate.write_state("orch_waker", {"ok": True, "last_tick": NOW - 10})
    res = ow.doctor_check(now=NOW, config_path=cfg_path(tmp_path),
                          _daemon_running=lambda: True)
    assert res["ok"] is True and "10s" in res["detail"]


def test_doctor_result_carries_exactly_the_five_check_fields(tmp_path):
    """doctor.py copies only Check's five fields into its list, so the check
    must not be relied on to carry more; the shape is pinned here."""
    res = ow.doctor_check(now=NOW, config_path=cfg_path(tmp_path),
                          stream_state={"last_tick": NOW},
                          _daemon_running=lambda: True)
    assert set(res) == {"name", "ok", "fatal", "detail", "fix"}


def test_daemon_pid_probe_is_read_only_and_never_raises(tmp_path):
    assert ow._daemon_pid_alive(str(tmp_path / "missing.pid")) is False
    p = tmp_path / "daemon.pid"
    p.write_text("not-a-pid")
    assert ow._daemon_pid_alive(str(p)) is False
    p.write_text("999999")           # no such pid -> not alive
    assert ow._daemon_pid_alive(str(p)) is False
    p.write_text(str(os.getpid()))   # this process is definitely alive
    assert ow._daemon_pid_alive(str(p)) is True
