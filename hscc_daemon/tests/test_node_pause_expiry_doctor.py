"""Doctor coverage for the ``node_pause_expiry`` stream (card t_005e87fd).

The row is the doctor half of the stream t_110adc1a landed: ``verify`` can
already say "the stream ran", only ``hscc doctor`` can say "the operator's
schedule was not honoured". Every posture is pinned against files the feature's
OWN writers create — the pause through ``node_pauses.pause(directory=...)`` (the
CLI's path) and the stamp through ``state.write_state`` / the real
``expiry_tick`` (the daemon's path) — not against a stubbed reader, so a row that
misreads the stamp shape fails here instead of looking green.

The five postures, in the order the card sets them:

1. no stamp + daemon running + not predating this build -> fatal RED (the
   orch-waker class — the wiring itself is broken, so a past-due pause parks
   forever and nothing notices);
2. newest stamp ``ok: false`` -> warn RED naming the hosts out of ``kept`` /
   ``skipped``, the ``checked_at`` the pass judged against and the tick's own
   message (this one is checked BEFORE staleness, so a red-and-stale stamp still
   names the stuck pause);
3. stamp older than ``2*cadence + slack`` -> warn;
4. stamp written before this build, or the daemon down -> warn, never fatal
   (doctor is bootstrap's Stage-1 gate and runs before a daemon exists);
5. anything unreadable, or the row itself blowing up -> a warn that says so. A
   check that did not run prints that it did not run; it never goes quiet and it
   never claims ok.

Hermetic by construction (``nolive-rule.txt``): hosts are RFC 5737
documentation addresses (192.0.2.x), every read is of a temp root, the daemon
liveness and predates-build probes are injected (no ``os.kill``, no real pid
file), and no test here spawns a subprocess or touches a node. The
conftest's ``_isolate_hscc`` seal already redirects ``state.STATE_DIR`` and
``node_pauses.HSCC_DIR``; the default-root cases guard-assert that seal before
they run, because a row that leaked would read the operator's live stamp and
overlay.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import hscc_daemon.node_pause_exec as npe
from hscc_daemon import daemon_ops, heal_breaker, node_pauses, state

# ── the fixture fleet (placeholders only) ──────────────────────────────────
W1 = "192.0.2.12"
W2 = "192.0.2.13"
W3 = "192.0.2.14"

#: One fixed clock for every age assertion, so "fresh" and "stale" are facts
#: about the fixture and not about when pytest ran.
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()

#: The stamp path the row prints when it has no root — pinned so a RED row's
#: fix text and its detail can never name two different files.
DEFAULT_HINT = f"~/.hscc/state/{npe.STREAM_NAME}.json"

RUNNING = lambda: True          # noqa: E731 - injected liveness probe
DOWN = lambda: False            # noqa: E731
NOT_PREDATING = lambda: False   # noqa: E731 - the running build has the stream
PREDATING = lambda: True        # noqa: E731 - upgrade race


def ago(seconds):
    """The ISO the stamp writer should claim for a pass ``seconds`` old."""
    return (NOW - timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def sealed(tmp_path):
    """Guard-assert the conftest's ~/.hscc seal, then hand out a temp root.

    Read out of ``HOME``, not ``expanduser("~/.hscc")``: the daemon leg's seal
    redirects ``expanduser`` too, so the obvious probe would compare the sealed
    root against itself and pass anywhere (the same reason
    ``test_node_pause_expiry.py``'s ``overlay`` fixture does this).
    """
    real = os.path.join(os.environ["HOME"], ".hscc")
    assert os.path.realpath(node_pauses.HSCC_DIR) != os.path.realpath(real), (
        "the overlay's default root is NOT sealed — a default-root read here "
        "would report on a real node")
    assert os.path.realpath(state.STATE_DIR) != os.path.realpath(
        os.path.join(real, "state")), (
        "the stream-state root is NOT sealed — a default-root read would read "
        "the operator's live stamp")
    assert os.path.realpath(heal_breaker.HEAL_BREAKER_FILE) != os.path.realpath(
        os.path.join(real, "heal-breaker.json")), "the heal ledger is NOT sealed"
    root = tmp_path / "hscc-root"
    (root / "state").mkdir(parents=True)
    assert os.path.realpath(str(root)).startswith(str(tmp_path)), (
        "the row's root must be inside the test's tmp dir")
    return str(root)


def write_stamp(root, *, age_s=12, **entry):
    """Stamp the stream the daemon's way: ``state.write_state``, real writer.

    ``state.now_iso`` is patched rather than the file being hand-written, so
    the age comes from the same field the real stream writes (``timestamp``) and
    the row is proven against the shape ``expiry_tick`` actually produces.
    """
    def _now_iso():
        return ago(age_s)
    state_dir = os.path.join(root, "state")
    original = state.now_iso
    state.now_iso = _now_iso
    try:
        return state.write_state(npe.STREAM_NAME, {
            "ok": True, "message": "no expired pauses", "resumed": [],
            "kept": [], "skipped": [], "deferred": [], **entry},
            directory=state_dir)
    finally:
        state.now_iso = original


def park(root, host=W1, *, due=True, reason="training"):
    """Record a pause through the overlay's own writer (the CLI's path)."""
    return node_pauses.pause(
        host, directory=root, reason=reason, now=NOW,
        until=ago(3600) if due else (NOW + timedelta(hours=2)).isoformat(),
        units=["w1"], span_nodes=[host])


def park_real_clock(root, host=W1, reason="training"):
    """A pause already past due against the REAL wall clock.

    The two tick-driven cases cannot use the fixed fixture clock: they let the
    real ``expiry_tick`` run, which stamps with ``state.now_iso()`` (wall clock)
    and judges ``until`` against a wall-clock ``now``. Seeding from the fixture
    clock would make the pause a future one and the pass would idle.
    """
    return node_pauses.pause(
        host, directory=root, reason=reason,
        until=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        units=["w1"], span_nodes=[host])


def ask(root, **kw):
    """The row, with both daemon probes injected (no os.kill, no real pid)."""
    kw.setdefault("now", NOW_ISO)
    kw.setdefault("_daemon_running", RUNNING)
    kw.setdefault("_predates_build", NOT_PREDATING)
    return npe.doctor_check(root=root, **kw)


# ── the cadence, which the row must not invent ─────────────────────────────
def test_the_freshness_budget_is_this_streams_own_cadence():
    """The budget is arithmetic on the cadence the LOOP declares, not a second
    constant: ``verify`` reads the same ``PERIODIC_INTERVALS`` entry, so a row
    that restated 60 would go stale-on-one-side the first time the cadence
    moves."""
    assert npe.stream_cadence() == daemon_ops.PERIODIC_INTERVALS[npe.STREAM_NAME]
    assert npe.doctor_freshness_limit_s() == (
        2 * npe.stream_cadence() + npe.DOCTOR_SLACK_S)


# ── the green posture ──────────────────────────────────────────────────────
def test_a_recent_pass_with_nothing_parked_is_green(sealed):
    write_stamp(sealed)

    res = ask(sealed)

    assert res["ok"] is True and res["fatal"] is False
    assert res["name"] == npe.CHECK_NAME
    assert res["detail"].startswith("expiry stream healthy")
    assert "no node pauses parked" in res["detail"]
    assert res["fix"] == "", "a green row has nothing to fix"
    assert "192.0.2." not in res["detail"], "nothing is parked, so name nobody"


def test_the_green_row_says_what_is_parked_and_how_much_is_overdue(sealed):
    """The count is what makes "healthy" mean something: an operator with two
    parked nodes needs to see them, and needs to see whether the deadline has
    passed — the same predicate the tick applies, over one snapshot."""
    park(sealed, W1, due=True)
    park(sealed, W2, due=False)

    detail = ask(sealed)["detail"]

    assert "2 pause(s) parked, 1 past due" in detail
    assert "192.0.2.12" not in detail, "a green row lists counts, not hosts"


def test_a_pass_that_deferred_a_backlog_is_green_but_says_so(sealed):
    """``MAX_RESUMES_PER_TICK`` parks the rest of a backlog onto the next tick
    by design — green is right, silence about it is not."""
    park(sealed, W1)
    write_stamp(sealed, deferred=[W2, W3])

    detail = ask(sealed)["detail"]

    assert "2 deferred to the next tick" in detail
    assert "1 pause(s) parked, 1 past due" in detail


def test_the_age_and_budget_are_printed_from_the_stamp(sealed):
    """The row must show its work: the age is the operator's only clue about
    whether the next pass has even had a chance to run."""
    write_stamp(sealed, age_s=37)

    detail = ask(sealed)["detail"]

    assert "last pass 37s ago" in detail
    assert f"budget {npe.doctor_freshness_limit_s():.0f}s" in detail


# ── posture 2: the stamped RED, the row's reason for existing ───────────────
def test_a_red_stamp_names_the_hosts_the_clock_and_the_reason(sealed):
    """THE row this card exists for: a pause is past due and still parked. It
    names every host the tick gave up on (``kept`` = the restart failed,
    ``skipped`` = another pause still holds part of the span), the ``checked_at``
    the pass judged against, and the tick's own message."""
    park(sealed, W1)
    park(sealed, W2, reason="tp span partner")
    write_stamp(sealed, ok=False, kept=[W1], skipped=[W2], checked_at=NOW_ISO,
                message="1 pause(s) could not be resumed (192.0.2.12); "
                        "1 pause(s) held by a spanning pause (192.0.2.13)")

    res = ask(sealed)

    assert res["ok"] is False, "a past-due pause that is still parked is not ok"
    assert res["fatal"] is False, (
        "the daemon IS trying; a warn is the honest posture, and doctor is "
        "bootstrap's Stage-1 gate")
    detail = res["detail"]
    assert detail.startswith("PAST DUE, STILL PARKED:")
    assert "restart FAILED on 192.0.2.12" in detail
    assert "span HELD by another pause on 192.0.2.13" in detail
    assert f"checked_at {NOW_ISO}" in detail, "which clock the pass judged"
    assert "2 pause(s) parked, 2 past due" in detail
    assert "could not be resumed" in detail, "the tick's own words are quoted"
    assert "hscc node resume" in res["fix"], "the fix names the manual escape"


def test_a_red_stamp_wins_over_the_staleness_check(sealed):
    """The card's priority order: a pass that ran 20 minutes ago and FAILED is
    reported as the failure it is — the age is printed with it, never instead
    of it."""
    park(sealed, W1)
    write_stamp(sealed, age_s=900, ok=False, kept=[W1], checked_at=ago(900),
                message="sparkrun refused the restart")

    detail = ask(sealed)["detail"]

    assert "PAST DUE, STILL PARKED" in detail
    assert "192.0.2.12" in detail
    assert "last pass 900s ago" in detail
    assert not detail.startswith("stale:")


def test_a_red_stamp_that_names_no_host_still_says_what_failed(sealed):
    """``ok: false`` with empty ``kept``/``skipped`` is the raising-tick shape
    (``expiry tick failed: …``). It must not render as a blank list of hosts."""
    write_stamp(sealed, ok=False, kept=[], skipped=[],
                message="expiry tick failed: simulated reader blow-up")

    res = ask(sealed)

    assert res["ok"] is False and res["fatal"] is False
    assert "the last pass reported ok=False" in res["detail"]
    assert "simulated reader blow-up" in res["detail"]


def test_the_quoted_message_is_capped_but_the_row_stays_readable(sealed):
    """The full text stays in the stamp file; the row quotes enough to act on
    and does not print a paragraph into the check list."""
    write_stamp(sealed, ok=False, kept=[W1], message="x" * 400)

    detail = ask(sealed)["detail"]

    assert detail.count("x") <= npe._MAX_QUOTED_MESSAGE


# ── posture 3: the stream is not running ───────────────────────────────────
def test_a_stale_stamp_is_a_warn_that_names_the_budget(sealed):
    """One skipped tick is a slow sparkrun call; three is drift. The budget is
    the row's own, so the row must print it rather than leave the operator to
    guess what "stale" means."""
    write_stamp(sealed, age_s=int(npe.doctor_freshness_limit_s()) + 2)

    res = ask(sealed)

    assert res["ok"] is False and res["fatal"] is False
    assert res["detail"].startswith("stale: last expiry pass")
    assert f"2*{npe.stream_cadence()}s+{npe.DOCTOR_SLACK_S}s" in res["detail"]
    assert "hscc start" in res["fix"]


def test_a_stamp_just_inside_the_budget_is_still_green(sealed):
    """The other side of the same line — a warn that fires one tick early
    trains an operator to ignore warns."""
    write_stamp(sealed, age_s=int(npe.doctor_freshness_limit_s()) - 1)

    assert ask(sealed)["ok"] is True


# ── posture 1: never stamped (the only fatal) ──────────────────────────────
def test_never_stamped_while_the_daemon_runs_is_fatal(sealed):
    """The orch-waker class: something IS ticking (the pid is alive), the
    stream has never stamped, and this build has the stream — so the wiring is
    broken, and a pause whose ``until`` passed would park forever. Only this
    posture may be fatal."""
    park(sealed, W1)

    res = ask(sealed)

    assert res["ok"] is False and res["fatal"] is True
    assert "never stamped while the daemon runs" in res["detail"]
    assert os.path.join(sealed, "state", f"{npe.STREAM_NAME}.json") \
        in res["detail"], "the row names the file it looked for"
    assert "1 pause(s) parked, 1 past due" in res["detail"], (
        "a fatal that cannot say what it is failing to honour is not actionable")
    assert "hscc start" in res["fix"]


def test_a_daemon_that_predates_this_build_is_a_warn_not_a_fatal(sealed):
    """The upgrade race: doctor is Stage 1 and the daemon restart is a later
    stage, so during an upgrade the running daemon has no such stream to stamp.
    Fatal here would abort an install over a race bootstrap itself caused."""
    park(sealed, W1)

    res = ask(sealed, _predates_build=PREDATING)

    assert res["ok"] is False and res["fatal"] is False
    assert "predates this build" in res["detail"]
    assert "bootstrap" in res["detail"]


def test_no_daemon_is_a_warn_because_stage_one_has_none_yet(sealed):
    park(sealed, W1)

    res = ask(sealed, _daemon_running=DOWN)

    assert res["ok"] is False and res["fatal"] is False
    assert "the daemon is not running" in res["detail"]
    assert "hscc start" in res["fix"]
    assert "1 pause(s) parked" in res["detail"]


def test_a_stamp_the_reader_cannot_parse_is_never_green(sealed):
    """A truncated or half-written stamp must fail LOUD. Absence of a readable
    timestamp is not evidence of a fresh stream, so this lands on the same
    never-stamped posture as a missing file."""
    path = os.path.join(sealed, "state", f"{npe.STREAM_NAME}.json")
    with open(path, "w") as fh:
        fh.write('{"ok": true, "timestamp": ')      # truncated write

    res = ask(sealed)

    assert res["ok"] is False and res["fatal"] is True
    assert "no readable stamp" in res["detail"]


def test_a_stamp_with_no_timestamp_field_at_all_is_never_green(sealed):
    write_stamp(sealed)
    path = os.path.join(sealed, "state", f"{npe.STREAM_NAME}.json")
    with open(path) as fh:
        data = json.load(fh)
    del data["timestamp"]
    with open(path, "w") as fh:
        json.dump(data, fh)

    assert ask(sealed)["ok"] is False


# ── the row's own honesty ──────────────────────────────────────────────────
def test_the_row_never_raises_and_never_goes_quiet(sealed, monkeypatch):
    """A report that throws takes the rest of ``hscc doctor`` down with it, so
    an internal blow-up becomes a warn naming itself — never a crash, never a
    silent ok."""
    monkeypatch.setattr(npe, "_pause_note",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            RuntimeError("simulated reader blow-up")))

    res = npe.doctor_check(now=NOW_ISO, root=sealed,
                           _daemon_running=RUNNING,
                           _predates_build=NOT_PREDATING)

    assert res["ok"] is False and res["fatal"] is False
    assert "could not be evaluated" in res["detail"]
    assert "simulated reader blow-up" in res["detail"]
    assert set(res) == {"name", "ok", "detail", "fix", "fatal"}


def test_a_dirty_overlay_read_is_marked_as_one(sealed, monkeypatch):
    """``node_pauses.load`` answers "no pauses" for a corrupt overlay and says
    so only through ``_warn``. Safe for a repair path, wrong for a count: the
    row must not print "no node pauses parked" as a fact about the fleet — and
    must give the logger back."""
    before = node_pauses._warn
    with open(node_pauses.overlay_path(sealed), "w") as fh:
        fh.write('{"pauses": {"192.0.2.5": ')      # truncated write
    write_stamp(sealed)

    res = ask(sealed)

    assert "not readable cleanly" in res["detail"]
    assert "node pauses" in res["detail"], "point at the row that owns this"
    assert node_pauses._warn is before, "the capture leaked — WARNs would be lost"


def test_the_row_reads_one_root_for_all_three_reads(sealed, tmp_path):
    """Stamp, overlay and pid are resolved under ONE root per call. Two roots in
    one verdict is the t_adb66608 / t_be690204 class: the row would answer about
    a fleet it was not asked about (and the other root's hosts would leak in)."""
    elsewhere = str(tmp_path / "other-root")
    os.makedirs(os.path.join(elsewhere, "state"))
    park(elsewhere, W3)
    write_stamp(elsewhere, ok=False, kept=[W3], checked_at=NOW_ISO,
                message="1 pause(s) could not be resumed (192.0.2.14)")
    # The asked-about root IS healthy, so the only reason this row could go red
    # or name a host is a read that walked into the other root.
    write_stamp(sealed)

    res = ask(sealed)

    assert "192.0.2.14" not in res["detail"]
    assert res["ok"] is True, "the empty root is honestly empty"
    assert "no node pauses parked" in res["detail"]


# ── the seam the card named: the row reads what the tick writes ────────────
def test_the_stamp_the_tick_writes_is_the_stamp_the_row_reads(sealed):
    """End-to-end through the REAL writers: the daemon's ``expiry_tick`` runs a
    pass whose restart fails, and ``doctor_check`` reads the result — same
    file, same keys, no fixture in between. If the tick renames ``kept`` or
    stops writing ``checked_at``, this fails and the row cannot drift."""
    host = W1
    park_real_clock(sealed, host)
    calls = []

    def failing_resume(h, entry, **kw):
        calls.append(h)
        return {"ok": False, "error": "sparkrun refused the restart",
                "action": "refused"}

    state_dir = os.path.join(sealed, "state")
    entry = npe.expiry_tick(directory=sealed, state_dir=state_dir,
                            resume=failing_resume, echo=lambda text: None)
    assert calls == [host] and entry["ok"] is False

    res = npe.doctor_check(root=sealed, _daemon_running=RUNNING,
                           _predates_build=NOT_PREDATING)

    assert res["ok"] is False and res["fatal"] is False
    assert "PAST DUE, STILL PARKED" in res["detail"]
    assert host in res["detail"]
    # The tick's OWN words are quoted (the per-host error text stays in the
    # stamp's `results`, which is where a deeper read goes).
    assert "could not be resumed" in res["detail"]
    assert "checked_at" in res["detail"]


def test_the_stamp_a_successful_tick_writes_reads_green(sealed):
    """The mirror half: a pass that resumed the expired pause clears it from the
    overlay, so the row goes green and names what it brought back."""
    park_real_clock(sealed, W1)

    def working_resume(h, entry, **kw):
        node_pauses.clear(h, directory=sealed)
        return {"ok": True, "action": npe.RESUME_RESUMED}

    state_dir = os.path.join(sealed, "state")
    entry = npe.expiry_tick(directory=sealed, state_dir=state_dir,
                            resume=working_resume, echo=lambda text: None)
    assert entry["ok"] is True and entry["resumed"] == [W1]

    res = npe.doctor_check(root=sealed, _daemon_running=RUNNING,
                           _predates_build=NOT_PREDATING)

    assert res["ok"] is True
    assert "no node pauses parked" in res["detail"]
    assert f"last pass resumed {W1}" in res["detail"]


# ── the fence: a suite must never read the operator's state ────────────────
def test_the_default_root_is_refused_inside_pytest(monkeypatch):
    """``root=None`` means the operator's ``~/.hscc``. Inside a suite that is
    refused, and pinned by making a default-root read impossible — the same
    fence every operator-state row has, applied to BOTH of this row's reads."""
    core = node_pauses

    def _never(*a, **kw):
        raise AssertionError("the row read the default root inside pytest")

    monkeypatch.setattr(core, "load", _never)
    monkeypatch.setattr(core, "list_pauses", _never)
    monkeypatch.setattr(state, "read_state", _never)

    res = npe.doctor_check()

    assert res["name"] == npe.CHECK_NAME
    assert res["ok"] is False and res["fatal"] is False
    assert "refused inside a pytest process" in res["detail"]
    assert "192.0.2." not in res["detail"], "a refusal reports no host"


def test_the_pid_path_is_the_stamp_writers_root(monkeypatch):
    """The liveness probe must ask about the same root the stamp writer uses.
    A second ``expanduser("~/.hscc")`` literal would drift the moment one of the
    two is relocated (the t_adb66608 rule: derive, never restate)."""
    assert npe._pid_path_for(None) == os.path.join(
        os.path.dirname(os.path.abspath(state.STATE_DIR)), "daemon.pid")

    def _boom(*a, **kw):
        raise AssertionError("the row must not read a pid file when asked a root")

    monkeypatch.setattr(os, "kill", _boom)
    assert npe._pid_path_for("/tmp/row-root") == "/tmp/row-root/daemon.pid"


def test_the_row_name_is_one_spelling_for_row_refusal_and_fallback():
    """Three modules print this name (the row, doctor's refusal, doctor's
    UNVERIFIABLE fallback); only a constant can keep them identical."""
    assert npe.CHECK_NAME == "node pause expiry (durable-until stream)"
    assert npe.refused_check()["name"] == npe.CHECK_NAME
