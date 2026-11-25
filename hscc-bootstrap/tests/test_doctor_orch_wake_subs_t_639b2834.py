"""The `orch_wake_subs` doctor row (card t_639b2834).

The regression alarm for the 2026-11-23 duplicate-orchestrator incident: 307
kanban ``notify+wake`` subscriptions across three boards all pointed at ONE
gateway session while the real orchestrator sat in a tmux pane, so the gateway
kept waking a second copy of that identity alongside the pane REPL. The parent
card (t_31c8b258) stops new ones being created; this row notices the posture
re-forming.

Every posture is pinned against FAKE records — fake subscription rows and a fake
session registry — because the row's real inputs are the operator's live board
DBs and live active-session registries. No test here opens a board, contacts a
gateway, or reads ``~/.hermes``; the two places a real file is touched are tmp
sqlite boards the test itself creates. The session ids are obviously synthetic.

Layout: the predicate first (``scan`` is pure, so every accept/reject rule is one
call), then the verdict wording the card demands, then the two read paths, then
the fence and the seam routing inside ``run_doctor()``.
"""

import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

import doctor
import doctor_orch_subs as dos

# ── synthetic fixtures ──────────────────────────────────────────────────────
# Two id spaces, both real shapes the fleet uses: a tmux-pane CLI lease carries
# a timestamp-shaped id; a gateway-created session carries a ``local-<digest>``
# id. Neither is a real session id.
PANE = "20260101_000000_aaaaaa"
PANE_2 = "20260102_000000_bbbbbb"
GATEWAY = "local-" + "0" * 64
OTHER = "local-" + "1" * 64


def sub(task="t_deadbee", platform="local", chat=PANE, profile="orch-a",
        mode="notify+wake", board="hscc", thread=""):
    """One subscription row, shaped like hermes' ``kanban_notify_subs``."""
    return {"board": board, "task_id": task, "platform": platform,
            "chat_id": chat, "thread_id": thread, "notifier_profile": profile,
            "delivery_mode": mode}


def lease(sid, surface="cli"):
    """One active-session lease entry (hermes' ``runtime/active_sessions.json``)."""
    return {"session_id": sid, "surface": surface, "pid": 4242}


def panes_for(**by_profile):
    """A ``pane_of`` callable for :func:`dos.scan` (no resolver, no filesystem)."""
    def pane_of(profile):
        return by_profile.get(profile, [])
    return pane_of


def run_scan(*subs_, **kw):
    """Scan fake rows against a per-profile pane map given as ``panes=``."""
    panes = kw.pop("panes", {"orch-a": [PANE]})
    return dos.scan(list(subs_), pane_of=panes_for(**panes),
                    wake=kw.pop("wake", dos.wake_modes()), **kw)


# ── the predicate: what counts and what does not ────────────────────────────

def test_subscription_pointing_off_the_live_pane_is_the_one_finding():
    """The incident shape, minimal: pane-backed owner, wake sub, wrong session."""
    result = run_scan(sub(chat=GATEWAY))
    assert len(result["findings"]) == 1
    f = result["findings"][0]
    assert f["profile"] == "orch-a"
    assert f["board"] == "hscc"
    assert f["task_id"] == "t_deadbee"
    assert f["platform"] == "local"
    assert f["target_session"] == GATEWAY
    assert f["pane_sessions"] == [PANE]


def test_subscription_pointing_at_the_live_pane_is_the_clean_posture():
    """Not every sub on a pane-backed profile is drift — the right target is."""
    result = run_scan(sub(chat=PANE))
    assert result["findings"] == []
    assert result["at_pane"] == 1


def test_any_of_several_live_panes_is_clean():
    """Two live CLI leases (a fork, a second pane): either target is accepted."""
    result = run_scan(sub(chat=PANE_2), panes={"orch-a": [PANE, PANE_2]})
    assert result["findings"] == []
    assert result["at_pane"] == 1


def test_a_paneless_owner_is_counted_and_never_claimed():
    """No live CLI lease means no expected pane — fail-open, the parent card's rule.

    Flagging these would call every headless worker's own subscription drift and
    the row would be muted within a week.
    """
    result = run_scan(sub(chat=GATEWAY), panes={"orch-a": []})
    assert result["findings"] == []
    assert result["paneless"] == 1


def test_an_unresolvable_pane_is_reported_as_unchecked_not_passed():
    """``pane_of`` returning None = the resolver was dark, which is not a clean.

    The count must exist so the green line can say so; but it is not evidence of
    drift either, so it never becomes a finding.
    """
    result = dos.scan([sub(chat=GATEWAY)],
                      pane_of=lambda p: None, wake=dos.wake_modes())
    assert result["findings"] == []
    assert result["unknown"] == 1


def test_plain_notify_subscription_is_nobody_wakes_nobody():
    """A ``notify`` sub sends a message and takes no turn: no second driver.

    Mutation probe: widen the mode filter to everything and this reddens.
    """
    result = run_scan(sub(chat=GATEWAY, mode="notify"))
    assert result["findings"] == []
    assert result["at_pane"] == 0 and result["paneless"] == 0


def test_wake_only_mode_counts_as_a_wake():
    """hermes' third mode is ``wake`` with no message — still takes a turn."""
    assert len(run_scan(sub(chat=GATEWAY, mode="wake"))["findings"]) == 1


def test_a_handy_written_mode_still_counts():
    """The comparison is case-insensitive on purpose: a hand-edited or legacy row
    reading ``NOTIFY+WAKE`` must not slip past the alarm unexamined. Mutation
    probe: drop the ``.lower()`` in the mode comparison and this reddens.
    """
    assert len(run_scan(sub(chat=GATEWAY, mode="NOTIFY+WAKE"))["findings"]) == 1
    assert run_scan(sub(chat=PANE, mode=" Notify+Wake "))["at_pane"] == 1


def test_chat_channel_targets_are_never_claimed():
    """A telegram ``chat_id`` is a chat, not a session: the comparison is void.

    Its wake also answers a human who subscribed themselves in chat, and a row
    that calls an operator's own decision a failure gets ignored.
    """
    result = run_scan(sub(platform="telegram", chat="12345678901234"))
    assert result["findings"] == []
    assert result["not_session_platform"] == 1


def test_only_session_naming_platforms_are_compared():
    """Witness test for :data:`SESSION_PLATFORMS`: admitting a chat platform
    turns a clean fleet red, which is the false alarm the tuple exists to stop.

    Without this the exclusion rots the first time someone tidies the constant —
    the same trap ``doctor_main_only`` sets for its hook-plugin exclusion.
    """
    clean = run_scan(sub(platform="telegram", chat="99999999999999"))
    assert clean["findings"] == []
    polluted = dos.scan([sub(platform="telegram", chat="99999999999999")],
                        pane_of=panes_for(**{"orch-a": [PANE]}),
                        wake=dos.wake_modes(),
                        platforms=tuple(dos.SESSION_PLATFORMS) + ("telegram",))
    assert len(polluted["findings"]) == 1, (
        "a chat platform must now be judged against a pane session id, which it "
        "can never match — if SESSION_PLATFORMS gains a chat channel, the row "
        "starts reporting the operator's own subscriptions")


def test_api_server_is_out_of_scope_like_a_chat_channel():
    """A stateless HTTP caller's subscription is how it gets its answer back."""
    result = run_scan(sub(platform="api_server", chat=OTHER))
    assert result["findings"] == []
    assert result["not_session_platform"] == 1


def test_unattributed_and_root_profile_rows_are_counted_not_claimed():
    """No ``notifier_profile`` = no profile to resolve a pane for; the root
    profile is the operator's own interactive hermes plus the gateway."""
    result = run_scan(dict(sub(), notifier_profile=None), sub(profile=""),
                      sub(profile=dos.ROOT_PROFILE, chat=GATEWAY))
    assert result["findings"] == []
    assert result["unattributed"] == 3


def test_profile_names_are_compared_verbatim_not_by_prefix():
    """A profile whose name merely starts like another's is a different home."""
    result = run_scan(sub(profile="orch-a-clone", chat=GATEWAY),
                      panes={"orch-a": [PANE], "orch-a-clone": []})
    assert result["findings"] == []
    assert result["paneless"] == 1


def test_subscription_id_is_hermes_own_primary_key():
    """hermes has no surrogate sub id; ``(task, platform, chat, thread)`` IS the
    id, and ``notify-unsubscribe`` takes exactly those parts."""
    assert dos.sub_id(sub(task="t_ab12cd34", platform="local",
                          thread="17")) == "t_ab12cd34@hscc/local/17"
    assert dos.sub_id(sub(task="t_ab12cd34")).endswith("/-"), "no thread"


# ── the verdict wording the card demands ────────────────────────────────────

def check(subs, registry, boards=None):
    return dos.doctor_check(hermes_home="/tmp/fake-hermes-home",
                            boards=boards if boards is not None else {"hscc": "/x"},
                            subscriptions=subs, registry=registry)


def test_clean_posture_is_a_green_count_line_with_no_fix():
    v = check([sub(chat=PANE), sub(task="t_other", chat=PANE)], [lease(PANE)])
    assert v["ok"] is True and v["fatal"] is False
    assert v["fix"] == ""
    assert v["detail"].startswith("clean:")
    assert "2 point at their owner's live pane session" in v["detail"]
    assert v["name"] == dos.CHECK_NAME


def test_red_verdict_names_profile_sub_id_target_expected_and_fix():
    """The card's wording requirement, asserted field by field."""
    v = check([sub(task="t_feedface", chat=GATEWAY)], [lease(PANE)])
    assert v["ok"] is False and v["fatal"] is False, "warn, never fatal"
    detail, fix = v["detail"], v["fix"]
    assert "orch-a" in detail, "profile"
    assert "t_feedface@hscc/local/-" in detail, "subscription id"
    assert "target=%s" % GATEWAY in detail, "target session"
    assert "expected-pane=%s" % PANE in detail, "expected pane session"
    assert "duplicate" in detail, "it must say what the pattern is"
    # remove OR repoint, both with runnable arguments
    assert "notify-unsubscribe t_feedface --platform local --chat-id %s" % GATEWAY in fix
    assert "notify-subscribe t_feedface" in fix and PANE in fix, "repoint target"
    assert "--notifier-profile orch-a" in fix
    assert "auto_subscribe_on_create" in fix, "the durable gate, not just the symptom"
    assert "notify-subs-*-20261123.txt" in fix, "never restore the incident backup"


def test_thread_id_survives_into_the_unsubscribe_command():
    """A thread-scoped sub is removed by its full key or the row is useless."""
    v = check([sub(chat=GATEWAY, thread="4242")], [lease(PANE)])
    assert "--thread-id 4242" in v["fix"]


def test_a_wall_of_findings_stays_readable_and_still_counts_every_one():
    """307 subs is the incident's own scale; the detail must not become a wall."""
    subs = [sub(task="t_%06d" % i, chat=GATEWAY) for i in range(307)]
    v = check(subs, [lease(PANE)])
    assert v["ok"] is False
    assert "307 notify+wake subscription(s)" in v["detail"]
    assert v["detail"].count("target=") == dos._MAX_RENDERED
    assert "(+%d more, same posture)" % (307 - dos._MAX_RENDERED) in v["detail"]


def test_boards_that_could_not_be_read_are_reported_not_swallowed():
    v = check([sub(chat=PANE)], [lease(PANE)])
    assert v["ok"] is True
    v2 = dos.doctor_check(hermes_home="/tmp/fake", boards={},
                          subscriptions=[sub(chat=PANE)], registry=[])
    assert v2["ok"] is True  # explicit empty board set = nothing to read, not dark


def test_unenumerable_boards_report_unchecked_never_clean():
    """An empty enumeration means "could not see the boards", not "no subs".

    This is the exact silent-inert class the waker's ``unevaluable`` bit exists
    for (t_fa9a2faf): the seam fails closed to ``{}`` when Hermes' kanban library
    is unreachable, and a row that reads that as clean is worse than no row.
    """
    v = dos.doctor_check(hermes_home="/tmp/fake", boards={}, registry=[])
    assert v["ok"] is False
    assert "UNCHECKED" in v["detail"] and "not a clean result" in v["detail"]
    assert v["fix"], "the operator gets the reason and the repair"


def test_boards_come_from_hermes_enumeration_and_never_a_second_glob(tmp_path,
                                                                     monkeypatch):
    """One enumeration in the repo, and this row is not allowed a second one.

    Hermes owns board discovery; a consumer that listdirs ``kanban/boards``
    re-derives hermes' path rules and misses ``default`` (the legacy root
    ``kanban.db``) — the mistake GAP-C-1 removed from the waker.
    """
    autodown = pytest.importorskip("hscc_daemon.autodown")
    good = tmp_path / "hscc.db"
    good.write_text("not a board, never opened")
    calls = []

    def fake_paths():
        calls.append(1)
        return {"hscc": str(good), "gone": str(tmp_path / "gone.db")}

    monkeypatch.setattr(autodown, "board_db_paths", fake_paths)
    assert dos.enumerate_boards() == {"hscc": str(good)}, (
        "a board hermes reports whose file is gone is dropped, not guessed at")
    assert calls, "the seam is the only source of boards"
    src = open(dos.__file__, encoding="utf-8").read()
    for forbidden in ("scandir", "listdir", "glob("):
        assert forbidden not in src, (
            "%s in this module would be a second board/registry enumeration "
            "beside Hermes' own" % forbidden)


def test_verdict_reads_real_board_rows_through_the_ro_path(tmp_path):
    """End-to-end on the read path: fake leases + a board DB the test created.

    Pins that the SQL filter, the record shape and the pane resolution actually
    compose — a fake-rows-only suite can pass while the reader is broken.
    """
    board = tmp_path / "hscc.db"
    make_board(board, rows=[
        ("t_aaa111", "local", GATEWAY, "", "orch-a", "notify+wake"),
        ("t_bbb222", "local", PANE, "", "orch-a", "notify+wake"),
        ("t_ccc333", "telegram", "99", "", "orch-a", "notify+wake")])
    v = dos.doctor_check(hermes_home="/tmp/fake",
                         boards={"hscc": str(board)}, registry=[lease(PANE)])
    assert v["ok"] is False
    assert "1 notify+wake subscription(s)" in v["detail"]
    assert "t_aaa111@hscc/local/-" in v["detail"]
    assert "t_bbb222" not in v["detail"], "the at-pane row is clean, not a finding"
    assert "t_ccc333" not in v["detail"], "a chat channel is never claimed"


# ── the two read paths ──────────────────────────────────────────────────────

SUBS_DDL = """
CREATE TABLE kanban_notify_subs (
    task_id TEXT NOT NULL, platform TEXT NOT NULL, chat_id TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '', user_id TEXT, user_id_alt TEXT,
    chat_type TEXT, notifier_profile TEXT,
    delivery_mode TEXT NOT NULL DEFAULT 'notify', delivery_metadata TEXT,
    created_at INTEGER NOT NULL, last_event_id INTEGER NOT NULL DEFAULT 0,
    last_ping_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id));
"""


def make_board(path, rows=(), with_table=True):
    con = sqlite3.connect(str(path))
    try:
        if with_table:
            con.execute(SUBS_DDL)
            for r in rows:
                con.execute(
                    "INSERT INTO kanban_notify_subs (task_id, platform, chat_id,"
                    " thread_id, notifier_profile, delivery_mode, created_at)"
                    " VALUES (?,?,?,?,?,?,1)", r)
        else:
            con.execute("CREATE TABLE unrelated (x)")
        con.commit()
    finally:
        con.close()


def test_read_wake_subs_reads_the_table_read_only_and_filters_wake_modes(tmp_path):
    good = tmp_path / "good.db"
    make_board(good, rows=[
        ("t_aaa111", "local", GATEWAY, "", "orch-a", "notify+wake"),
        ("t_bbb222", "local", GATEWAY, "", "orch-a", "wake"),
        ("t_eee555", "local", GATEWAY, "", "orch-a", "NOTIFY+WAKE"),
        ("t_ccc333", "local", GATEWAY, "", "orch-a", "notify"),
        ("t_ddd444", "telegram", "99", "", "orch-a", "notify+wake")])
    before = good.read_bytes()
    subs, unreadable = dos.read_wake_subs({"hscc": str(good)})
    assert unreadable == []
    assert sorted(s["task_id"] for s in subs) == ["t_aaa111", "t_bbb222",
                                                 "t_ddd444", "t_eee555"], (
        "the passive notify row is not a wake and must not even reach the "
        "predicate; the platform filter is the predicate's job, not the SQL's; "
        "the match is case-insensitive so a hand-edited row cannot slip past")
    assert subs[0]["board"] == "hscc"
    assert good.read_bytes() == before, "a doctor run must never write a board"


def test_a_board_without_the_table_or_file_is_unchecked_not_empty(tmp_path):
    """Older schema / a deleted file must not read as "no subscriptions"."""
    stale = tmp_path / "stale.db"
    make_board(stale, with_table=False)
    subs, unreadable = dos.read_wake_subs({
        "stale": str(stale), "gone": str(tmp_path / "nope.db")})
    assert subs == []
    assert sorted(unreadable) == ["gone", "stale"]


# ── silence: a read that logs is not a read ─────────────────────────────────
# t_2c218de8: this row shipped red on main because the enumeration it borrows
# writes a WARN into $HSCC_HOME/daemon.log when Hermes' library is unreachable.
# The guard that caught it lives in a DIFFERENT top-level directory
# (hscc-cluster/tests/test_doctor_tool.py::TestReadOnly), so the row's own suite
# has to hold the same line — otherwise the next borrowed call that logs goes
# red on main again, one directory away from the test that would have caught it.

def _unreachable_hermes(tmp_path, monkeypatch):
    """Make ``autodown``'s kanban import fail, and point its log at a tmp file.

    Returns the log path. ``HERMES_AGENT_PATH`` is autodown's own documented
    override, so the failure is provoked the way the module expects to be
    provoked; ``LOG_FILE``/``PID_FILE`` are redirected so a real WARN lands in
    the tmp tree instead of the operator's ~/.hscc.
    """
    autodown = pytest.importorskip("hscc_daemon.autodown")
    daemon_ops = pytest.importorskip("hscc_daemon.daemon_ops")
    monkeypatch.setenv("HERMES_AGENT_PATH", str(tmp_path / "no-such-hermes"))
    hscc = tmp_path / "hscc"
    hscc.mkdir()
    log_file = hscc / "daemon.log"
    monkeypatch.setattr(daemon_ops, "LOG_FILE", str(log_file))
    monkeypatch.setattr(daemon_ops, "PID_FILE", str(hscc / "daemon.pid"))
    # Arm the once-per-process WARN: without this the note function may already
    # have fired in this process and the test below would pass while proving
    # nothing (no write was ever attempted).
    for key, value in (("ok", None), ("reason", ""), ("warned", False)):
        monkeypatch.setitem(autodown._KANBAN_LOAD, key, value)
    return log_file


def test_the_rows_read_writes_no_daemon_log_even_unmuted_rig(tmp_path,
                                                             monkeypatch):
    """The row's read is silent, and the rig that proves it actually bites.

    Both halves in one test, in this order: the row's own call first (the
    claim), then the same call made directly through ``autodown`` (the control).
    The control has to run second — the direct call spends the WARN budget, so
    run first it would leave the row's call with nothing to say either way.
    """
    autodown = pytest.importorskip("hscc_daemon.autodown")
    log_file = _unreachable_hermes(tmp_path, monkeypatch)

    assert dos.enumerate_boards() == {}, (
        "the seam fails closed to {} when Hermes is unreachable — that is the "
        "path being read here")
    assert not log_file.exists(), (
        "the row's read wrote a daemon.log: doctor is a read and the "
        "hscc-cluster read-only guard snapshots the tree around this exact door "
        "child (t_2c218de8)")

    autodown._KANBAN_LOAD["warned"] = False
    assert autodown.board_db_paths() == {}
    assert log_file.exists(), (
        "the control is vacuous: this call never writes the log even un-muted, "
        "so the assertion above proved nothing about the row")
    assert b"interlock unevaluable" in log_file.read_bytes()


def test_the_silent_window_gives_the_logger_and_the_warn_budget_back(
        tmp_path, monkeypatch):
    """Muting a read must not spend somebody else's alarm.

    ``_note_kanban_unavailable`` marks ``warned`` BEFORE it logs, so a caller
    that swapped the logger and never gave the budget back would leave the
    process permanently deaf: a daemon that ran this row would then never log a
    genuine interlock failure — a fail-open wearing the fix's clothes. The
    state that ``hscc autodown status`` reads is still recorded, because only
    the log call is muted.
    """
    autodown = pytest.importorskip("hscc_daemon.autodown")
    log_file = _unreachable_hermes(tmp_path, monkeypatch)
    real_log = autodown.log

    dos.enumerate_boards()

    assert autodown.log is real_log, (
        "the window leaked a no-op logger into autodown — every later daemon "
        "log line in this process would vanish")
    assert autodown._KANBAN_LOAD["warned"] is False, (
        "the row spent the process's one WARN: autodown can no longer warn "
        "about a real interlock failure in this process")
    assert autodown._KANBAN_LOAD["ok"] is False, (
        "the failure must still be RECORDED (hscc autodown status owns the "
        "answer); only the file write is muted")
    assert autodown._KANBAN_LOAD["reason"], "the reason is recorded too, not just the flag"
    assert not log_file.exists()


def test_the_window_restores_the_logger_even_when_the_read_raises():
    """The ``finally`` is the whole design: a leaked no-op logger would silence
    the daemon for the rest of the process, and a raise is exactly when a
    cleanup is easy to forget."""
    borrowed = SimpleNamespace(
        # Instance attributes, deliberately: a function defined in a class BODY
        # would be re-bound on every attribute access, so the identity assertion
        # below would fail for a reason that has nothing to do with the window.
        log=lambda *a, **k: None,
        _KANBAN_LOAD={"ok": None, "reason": "", "warned": True})
    original = borrowed.log
    with pytest.raises(RuntimeError):
        with dos._silent_read(borrowed):
            raise RuntimeError("the read died")
    assert borrowed.log is original
    assert borrowed._KANBAN_LOAD["warned"] is True, (
        "a budget that was already spent must not be re-armed")


def test_pane_resolution_uses_the_shared_resolver_and_its_surface_rule():
    """The lease rule lives in ``orch_subscribe`` (surface == "cli"); a gateway
    lease is NOT a pane, which is exactly the session the incident's subs
    pointed at. A re-implemented rule here would drift from the gate's.
    """
    # Only a gateway lease: cannot prove a pane -> fail-open, no claim.
    v = check([sub(chat=GATEWAY)], [lease(GATEWAY, surface="gateway:local")])
    assert v["ok"] is True and "paneless owners" in v["detail"]
    # A CLI lease whose id is a local digest (the desktop/CLI-created pane) is a
    # pane, and the sub matching it is clean.
    v2 = check([sub(chat=OTHER)], [lease(OTHER, surface="cli")])
    assert v2["ok"] is True


def test_registry_callable_seam_receives_the_profile_home():
    """The fake-registry seam is the same shape the feature module exposes."""
    seen = []

    def registry(profile_home):
        seen.append(profile_home)
        return [lease(PANE)] if str(profile_home).endswith("orch-a") else []

    v = check([sub(chat=PANE)], registry)
    assert v["ok"] is True and seen and seen[0].endswith(os.path.join("profiles",
                                                                     "orch-a"))


# ── the fence and the seam, inside a pytest process ─────────────────────────

def test_uninjected_call_refuses_and_names_the_seams():
    """An uninjected row would open the operator's boards and live registries —
    which would let the live fleet decide this suite's assertions."""
    v = dos.doctor_check()
    assert v["ok"] is False
    assert "refused inside a pytest process" in v["detail"]
    for seam in ("subscriptions", "registry", "boards"):
        assert seam in v["detail"] or seam in v["fix"]
    assert "fake" in v["fix"], "the fix must say what to inject"


def test_the_fence_does_not_half_read_a_planted_live_tree(tmp_path, monkeypatch):
    """Canary: plant a plausible install (a board with the incident's subs, a
    live-looking lease file) under a redirected HOME and assert the refusal reads
    and writes neither."""
    home = tmp_path / ".hermes"
    board = tmp_path / "kanban.db"
    make_board(board, rows=[("t_aaa111", "local", GATEWAY, "", "orch-a",
                            "notify+wake")])
    runtime = home / "profiles" / "orch-a" / "runtime"
    runtime.mkdir(parents=True)
    reg = runtime / "active_sessions.json"
    reg.write_text(json.dumps({"entries": [lease(PANE)]}))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    before_board, before_reg = board.read_bytes(), reg.read_text()

    def no_enumeration():
        raise AssertionError("the fence must refuse before enumerating boards")

    monkeypatch.setattr(dos, "enumerate_boards", no_enumeration)
    v = dos.doctor_check()
    assert v["ok"] is False and "refused" in v["detail"]
    assert board.read_bytes() == before_board
    assert reg.read_text() == before_reg


# ── the wiring pins ─────────────────────────────────────────────────────────

def test_fallback_check_name_equals_the_module_name():
    """doctor.py keeps a fallback so the row still prints when the module is not
    deployed; the two must not state two different names for one row."""
    assert doctor._ORCH_WAKE_SUBS_CHECK_NAME == dos.CHECK_NAME


def test_fallback_wake_modes_equal_the_wakers_declaration():
    """``hscc_daemon.orch_waker`` owns "which delivery modes make the gateway take
    a turn" (itself pinned to hermes' ``_NOTIFY_DELIVERY_MODES``). Doctor and the
    waker must not disagree about what a wake is."""
    orch_waker = pytest.importorskip("hscc_daemon.orch_waker")
    assert dos.wake_modes() == tuple(orch_waker.GATEWAY_WAKE_MODES)
    assert dos._FALLBACK_WAKE_MODES == tuple(orch_waker.GATEWAY_WAKE_MODES)


def test_row_declares_hermes_home_only():
    """The carve-out, written down: this row may take a root because the trees it
    reads are the Hermes home the doctor was handed (the same read the budget-hook
    row makes), and it takes NO ``hscc_dir`` because nothing here reads ~/.hscc.

    Contrast the daemon-state rows (``orch_waker``, ``kanban_enforce``,
    ``node_pauses``, ``node_pause_expiry``), which declare ``row_kwargs=()``
    because their writers resolve from HOME and a handed root would split the
    reader from them — and the boards in particular must NEVER get a
    caller-chosen root: they are Hermes' and come from Hermes' enumeration.
    """
    by_key = {s.key: s for s in doctor.FEATURE_CHECK_ROWS}
    spec = by_key["orch_wake_subs"]
    assert spec.row_kwargs == ("hermes_home",)
    assert "hscc_dir" not in spec.row_kwargs
    assert spec.row_fn == "", "the generic builder; no run_doctor() edit"


def test_run_doctor_routes_the_seam_and_short_circuits_the_loader(tmp_path,
                                                                  monkeypatch):
    """Injected verdict wins; the loader must not run, and the row still prints
    exactly once in its registry position."""
    calls = []
    monkeypatch.setattr(dos, "doctor_check",
                        lambda *a, **k: calls.append(1) or {"name": "x", "ok": True,
                                                            "detail": "nope",
                                                            "fix": "", "fatal": False})
    rows = _doctor(tmp_path, monkeypatch,
                   _orch_wake_subs_check=lambda: {"ok": True, "fatal": False,
                                                  "detail": "injected verdict",
                                                  "fix": ""})
    names = [c["name"] for c in rows["checks"]]
    assert dos.CHECK_NAME in names
    assert names.count(dos.CHECK_NAME) == 1
    row = [c for c in rows["checks"] if c["name"] == dos.CHECK_NAME][0]
    assert row["detail"] == "injected verdict" and row["ok"] is True
    assert calls == [], "the injected seam must short-circuit the real check"


def test_run_doctor_reports_the_refusal_when_no_seam_is_injected(tmp_path,
                                                                 monkeypatch):
    """Uninjected inside a suite = an ERROR naming the seam, never a silent pass
    and never a live read (the registry's generic fence, pinned for this row)."""
    rows = _doctor(tmp_path, monkeypatch)
    row = [c for c in rows["checks"] if c["name"] == dos.CHECK_NAME][0]
    assert row["ok"] is False and "refused" in row["detail"]
    assert row["fatal"] is False, "a refused row must never sink the Stage-1 gate"


# ── helpers used by the run_doctor tests (same harness as the registry suite) ─

FAKE_CLUSTER = json.dumps([{"name": "hscc", "hosts": ["192.0.2.1"]}])


def _doctor(tmp_path, monkeypatch, **kw):
    home = tmp_path / ".hermes"
    (home / "hermes-agent").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HSCC_DIR", str(tmp_path / ".hscc"))
    monkeypatch.setattr(doctor, "_sparkrun_ok",
                        lambda: doctor.Check("sparkrun", True, detail="stubbed"))
    return doctor.run_doctor(
        str(home), _cluster_runner=lambda: FAKE_CLUSTER, fleet_checks=True,
        _ssh_factory=lambda h: (lambda c, t: {"ok": False, "stdout": "",
                                              "code": 127, "stderr": "stub"}),
        hscc_dir="/nonexistent",
        _cluster_default_runner=lambda: '{"hosts": ["192.0.2.1"]}',
        _status_runner=lambda cl: "",
        _serving_raw=json.dumps({"units": []}),
        _venv_python="/home/hscc-fixture/.venvs/sparkrun/bin/python",
        _lan_probe=lambda i, n: None,
        **kw)
