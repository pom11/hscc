"""GAP-F-4 (t_51931733): kanban enforce as a supervised daemon stream.

Audit F found `hscc kanban enforce` — the verb that re-holds cards an
in-flight orchestrator turn leaves ready/running on a PAUSED board — had no
scheduler anywhere (its only call site was the CLI) and no doctor/verify
coverage. The feature's own contract said "safe to run from a loop or the
fleet check"; this suite pins that the loop now EXISTS and is wired the way
the W3 precedent (orch-waker) wired its stream:

  * cadence in daemon_ops.PERIODIC_INTERVALS (the SOT verify reads);
  * CHECKS maps the stream to kanban_pause.check_kanban_enforce, and the
    loop hands it to the standard per-stream supervision;
  * enforce_tick stamps its stream file on EVERY path (idle / holding /
    failing) so a deliberately-idle enforcer never reads as a dead stream;
  * the stamp is its OWN file — kanban_paused.json is never stamped (that
    file's non-"_" keys are BOARD NAMES; stamping it would make verify
    healthy while auto_decompose could never come back);
  * doctor_check's verdict table (fresh/stale/failed/never-stamped/predates/
    no-daemon + policy coherence), with fatal RED reserved for the
    orch-waker class.
"""

import json

import pytest

from hscc_daemon import kanban_pause as kp


# ── fakes (same shape as test_kanban_pause.py's FakeHermes) ────────────────

class FakeHermes:
    def __init__(self, boards):
        self.boards = {b: {c: dict(s) for c, s in cards.items()}
                       for b, cards in boards.items()}
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        board, verb, rest = args[1], args[2], args[3:]
        cards = self.boards[board]

        class R:
            returncode, stdout, stderr = 0, "", ""
        r = R()
        if verb == "list":
            r.stdout = json.dumps([{"id": i, "status": c["status"]}
                                   for i, c in cards.items()])
        elif verb == "reclaim":
            cards[rest[0]]["status"] = "ready"
        elif verb == "block":
            cid = rest[2]
            if cards[cid]["status"] in ("todo", "triage"):
                r.returncode = 1
            else:
                cards[cid]["status"] = "blocked"
        return r


@pytest.fixture(autouse=True)
def _no_real_side_effects(monkeypatch, tmp_path):
    """A stream test must never shell out to hermes/cron/tmux or read the
    OPERATOR's default paused-state file. Tests pass explicit state_path; the
    default is fenced to a path that cannot exist."""
    monkeypatch.setattr(kp, "_load_repl_inject", lambda: None)

    class R:
        returncode, stdout, stderr = 0, "No scheduled jobs.\n", ""
    monkeypatch.setattr(kp, "_run_cron", lambda profile, args: R())
    monkeypatch.setattr(kp, "STATE_PATH",
                        str(tmp_path / "default_state_must_not_be_read.json"))


@pytest.fixture
def env(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("kanban:\n  auto_decompose: true\n")
    return {"state_path": str(tmp_path / "kanban_paused.json"),
            "config_path": str(cfg)}


def _stamp_capture():
    seen = []
    return seen, (lambda stream, entry: seen.append((stream, dict(entry))))


# ── cadence seam (PERIODIC_INTERVALS stays the SOT) ────────────────────────

def test_stream_cadence_reads_the_daemons_single_source_of_truth():
    from hscc_daemon import daemon_ops
    assert (kp.stream_cadence()
            == daemon_ops.PERIODIC_INTERVALS["kanban_enforce"] == 60)


def test_verify_per_stream_rule_widens_it_to_the_600s_floor():
    """The GAP-B-6 rule: max(600, 2*interval+90). 60 s is a fast stream, so it
    lands on the same floor as every other fast stream — the rule is not
    adjusted for it, and a tightened flat window cannot cry wolf on it."""
    from hscc_daemon import daemon_ops
    interval = daemon_ops.PERIODIC_INTERVALS["kanban_enforce"]
    assert max(600, 2 * interval + 90) == 600


def test_verify_reads_the_cadence_from_the_sot():
    from hscc_daemon import daemon_ops, verify
    intervals = verify._live_stream_intervals()
    assert intervals.get("kanban_enforce") == daemon_ops.PERIODIC_INTERVALS["kanban_enforce"]


# ── daemon-loop wiring (same proof shape as test_orch_waker_w3) ────────────

def test_enforce_stream_check_is_registered_in_the_loop(monkeypatch):
    """run_daemon_loop must hand the enforcer's check + the SOT cadence to the
    standard per-stream supervision — a missing CHECKS row would start NO
    thread (daemon_ops' own comment about the globals().get trap). The
    supervision seam is patched: this proves the WIRING, not the tick."""
    from hscc_daemon import daemon_ops, daemon_singleton as ds
    seen = {}

    def fake_supervised(stop_event, check_fn, interval, stream_name):
        if stream_name == "kanban_enforce":
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
    monkeypatch.setattr(ds, "hold_daemon_loop_lock_fail_open", lambda **k: (True, ""))
    monkeypatch.setattr(daemon_ops, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon_ops, "run_autodown_loop", lambda ev, **k: None)
    monkeypatch.setattr(daemon_ops, "_run_supervised_periodic", fake_supervised)
    monkeypatch.setattr(daemon_ops, "run_heartbeat_loop", lambda ev, **k: ev.wait(2))
    monkeypatch.setattr(daemon_ops, "write_stopped", lambda: None)
    monkeypatch.setattr(daemon_ops, "prune_dead_files",
                        lambda *a, **k: {"removed_dead": 0, "pruned_bak": 0})

    daemon_ops.run_daemon_loop()

    assert seen.get("interval") == 60, "cadence must come from PERIODIC_INTERVALS"
    assert seen.get("fn") is kp.check_kanban_enforce


# ── the tick: every path stamps, the idle path costs nothing ───────────────

def test_idle_tick_stamps_ok_and_touches_no_board(env):
    """Nothing paused: the stamp still goes out (a stream the daemon is
    deliberately idling must not read dead to verify), and zero hermes calls
    happen (enforce() early-returns before enumerating any board)."""
    h = FakeHermes({"low": {"q1": {"status": "ready"}}})
    seen, write = _stamp_capture()
    entry = kp.enforce_tick(now=1000.0, state_path=env["state_path"], run=h,
                            write=write)
    assert entry["ok"] is True and entry["last_enforce"] == 1000.0
    assert entry["message"] == "no boards paused"
    assert seen == [("kanban_enforce", entry)]
    assert h.calls == []                      # idle = zero subprocess cost


def test_tick_holds_new_cards_and_reports_them(env):
    h = FakeHermes({"low": {"q1": {"status": "ready"}}, "high": {"z": {"status": "ready"}}})
    kp.pause(["low"], run=h, state_path=env["state_path"], config_path=env["config_path"])
    h.boards["low"]["new"] = {"status": "ready"}          # orchestrator leaked one
    h.boards["high"]["new2"] = {"status": "ready"}
    seen, write = _stamp_capture()
    entry = kp.enforce_tick(now=2000.0, state_path=env["state_path"], run=h, write=write)
    assert entry["ok"] is True
    assert entry["held"] == 1 and entry["boards"] == {"low": ["new"]}
    assert h.boards["low"]["new"]["status"] == "blocked"
    assert h.boards["high"]["new2"]["status"] == "ready"  # unpaused board untouched
    assert "1 card(s)" in entry["message"]


def test_failing_tick_stamps_ok_false(env, monkeypatch):
    """A raising enforce still stamps — the failure is VISIBLE (ok False),
    never a stalled-looking stream (GAP-C-1's inert-but-green rule)."""
    def boom(**kw):
        raise RuntimeError("board unreadable")
    monkeypatch.setattr(kp, "enforce", boom)
    seen, write = _stamp_capture()
    entry = kp.enforce_tick(now=3000.0, state_path=env["state_path"], write=write)
    assert entry["ok"] is False and "board unreadable" in entry["message"]
    assert seen == [("kanban_enforce", entry)]


def test_check_fn_reports_the_tick_verdict_and_never_raises(monkeypatch):
    """The loop's check-fn convention: ok iff the tick stamped ok (same shape
    as check_orch_waker / post_boot)."""
    monkeypatch.setattr(kp, "enforce_tick", lambda **kw: {"ok": True})
    assert kp.check_kanban_enforce() is True
    monkeypatch.setattr(kp, "enforce_tick", lambda **kw: {"ok": False})
    assert kp.check_kanban_enforce() is False

    def boom(**kw):
        raise RuntimeError("escaped")
    monkeypatch.setattr(kp, "enforce_tick", boom)
    monkeypatch.setattr("hscc_daemon.daemon_ops.log", lambda *a, **k: None)
    assert kp.check_kanban_enforce() is False


# ── stream separation: the policy file is NEVER the stamp file ─────────────

def test_tick_writes_only_its_own_stream_file_never_the_paused_state(env, monkeypatch):
    """The t_fa9a2faf lesson, cross-process variant: kanban_paused.json's
    non-"_" keys are BOARD NAMES (paused_boards only skips "_"-prefixed), so
    stamping ok/timestamp INTO it would (a) invent phantom paused "boards"
    named ok/timestamp/last_enforce — auto_decompose could never be restored —
    and (b) put two full-document writers on one file. The stamp lives in
    kanban_enforce.json via state.write_state; the policy file only ever
    carries board entries + the "_" bookkeeping keys."""
    from hscc_daemon import state
    import pathlib
    tmp_state = pathlib.Path(env["config_path"]).parent / "state"
    tmp_state.mkdir()
    monkeypatch.setattr(state, "STATE_DIR", str(tmp_state))
    state.ensure_state_dir()

    h = FakeHermes({"low": {"q1": {"status": "ready"}}})
    kp.pause(["low"], run=h, state_path=env["state_path"],
             config_path=env["config_path"], now=100)
    h.boards["low"]["new"] = {"status": "ready"}          # leaked behind the pause
    kp.enforce_tick(now=1000.0, state_path=env["state_path"], run=h)

    stream_file = tmp_state / "kanban_enforce.json"
    assert stream_file.exists()
    stamped = json.loads(stream_file.read_text())
    assert stamped["ok"] is True and stamped["last_enforce"] == 1000.0
    assert "timestamp" in stamped                         # what verify parses

    # The policy file gained the held card but NEVER a stamp key: whichever
    # reader enumerates it cannot mistake ok/timestamp/last_enforce for a
    # paused board.
    st = kp.load_state(env["state_path"])
    assert kp.paused_boards(st) == ["low"]
    assert {"ok", "timestamp", "last_enforce", "stream"}.isdisjoint(st)
    assert "new" in [c["id"] for c in st["low"]["cards"]]


# ── doctor_check verdict table ──────────────────────────────────────────────

def _doctor(**kw):
    kw.setdefault("_daemon_running", lambda: True)
    kw.setdefault("_predates_build", lambda: False)
    return kp.doctor_check(**kw)


def test_doctor_ok_idle_when_nothing_paused_and_stream_fresh():
    res = _doctor(now=1000.0, paused_path="/nonexistent/kanban_paused.json",
                  stream_state={"last_enforce": 990.0, "ok": True})
    assert res["ok"] is True and res["fatal"] is False
    assert "no boards paused" in res["detail"] and "last enforce 10s" in res["detail"]


def test_doctor_ok_names_paused_boards_and_enforce_age(tmp_path):
    (tmp_path / "kanban_paused.json").write_text(json.dumps(
        {"low": {"paused_at": 1, "cards": [{"id": "q1", "was": "ready"}]},
         "_auto_decompose_was": True}))
    res = _doctor(now=1000.0, paused_path=str(tmp_path / "kanban_paused.json"),
                  stream_state={"last_enforce": 970.0, "ok": True})
    assert res["ok"] is True
    assert "1 board(s) paused (low)" in res["detail"] and "last enforce 30s" in res["detail"]


def test_doctor_warns_on_a_failed_tick(tmp_path):
    """Fresh but ok=False: the loop is alive, the hold is erroring — warn,
    and say the hold is NOT being enforced."""
    (tmp_path / "kanban_paused.json").write_text(json.dumps(
        {"low": {"paused_at": 1, "cards": []}}))
    res = _doctor(now=1000.0, paused_path=str(tmp_path / "kanban_paused.json"),
                  stream_state={"last_enforce": 995.0, "ok": False,
                                "message": "enforce tick failed: boom"})
    assert res["ok"] is False and res["fatal"] is False
    assert "FAILED" in res["detail"] and "NOT being enforced" in res["detail"]


def test_doctor_warns_on_staleness_at_its_own_budget():
    """Budget = 2*cadence + slack = 2*60+90 = 210 s; 250 s old warns, 180 s
    old passes (the budget math pinned, same style as orch_waker's row)."""
    assert kp.doctor_freshness_limit_s() == 210.0
    res = _doctor(now=1000.0, paused_path="/nonexistent/x.json",
                  stream_state={"last_enforce": 750.0, "ok": True})
    assert res["ok"] is False and res["fatal"] is False and "stale" in res["detail"]
    res = _doctor(now=1000.0, paused_path="/nonexistent/x.json",
                  stream_state={"last_enforce": 820.0, "ok": True})
    assert res["ok"] is True


def test_doctor_fatal_never_stamped_while_daemon_runs(tmp_path):
    """The orch-waker class: policy could be live, the daemon ticks, this
    stream has NEVER stamped — the wiring itself is broken. fatal=True."""
    res = _doctor(now=1000.0, paused_path="/nonexistent/x.json", stream_state={})
    assert res["ok"] is False and res["fatal"] is True
    assert "never enforced" in res["detail"]


def test_doctor_upgrades_race_is_a_warn_not_fatal():
    res = _doctor(now=1000.0, paused_path="/nonexistent/x.json", stream_state={},
                  _predates_build=lambda: True)
    assert res["ok"] is False and res["fatal"] is False
    assert "predates this build" in res["detail"]


def test_doctor_daemon_down_is_a_warn():
    """Doctor is Stage 1 and runs BEFORE the daemon has ever started — 'no
    stream + no daemon' must never sink an install."""
    res = _doctor(now=1000.0, paused_path="/nonexistent/x.json", stream_state={},
                  _daemon_running=lambda: False)
    assert res["ok"] is False and res["fatal"] is False
    assert "not running" in res["detail"]


def test_doctor_names_a_malformed_policy_entry(tmp_path):
    """Policy-state coherence (the audit's second half): a paused entry the
    reader cannot trust is named even when the stream reads healthy — and the
    enforce tick over such an entry stamps ok=False, so the two halves agree
    on 'the hold is not trustworthy right now'."""
    (tmp_path / "kanban_paused.json").write_text(json.dumps(
        {"broken": {"paused_at": 1}}))          # no cards list
    res = _doctor(now=1000.0, paused_path=str(tmp_path / "kanban_paused.json"),
                  stream_state={"last_enforce": 999.0, "ok": True})
    assert "malformed" in res["detail"] and "broken" in res["detail"]


def test_doctor_honours_hscc_dir_sandbox(tmp_path, monkeypatch):
    """The doctor_notifier_mask convention: hscc_dir relocates the WHOLE
    ~/.hscc root (state dir + paused file + daemon.pid) so a sandboxed
    bootstrap never reads the OPERATOR's live daemon."""
    root = tmp_path / ".hscc"
    (root / "state").mkdir(parents=True)
    (root / "state" / "kanban_paused.json").write_text(json.dumps(
        {"low": {"paused_at": 1, "cards": []}}))
    (root / "state" / "kanban_enforce.json").write_text(json.dumps(
        {"ok": True, "last_enforce": 990.0, "timestamp": "x"}))
    (root / "daemon.pid").write_text("4242")
    res = kp.doctor_check(now=1000.0, hscc_dir=str(root))
    assert res["ok"] is True and "1 board(s) paused (low)" in res["detail"]
    # Never-stamped leg: the liveness probe + the state hint must resolve
    # under the SANDBOX root too (killed==[4242] proves the sandbox pid file
    # was the one stat'd, not the operator's).
    (root / "state" / "kanban_enforce.json").unlink()
    killed = []
    monkeypatch.setattr(kp.os, "kill",
                        lambda pid, sig: killed.append(pid) or None)
    res2 = kp.doctor_check(now=1000.0, hscc_dir=str(root),
                           _predates_build=lambda: False)
    assert res2["fatal"] is True and str(root) in res2["detail"]
    assert killed == [4242]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
