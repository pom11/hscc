"""Tests for the silent-dispatcher-wedge detector + guarded self-heal.

Coverage maps 1:1 to the card's proof requirements:

  DETECTION (probe):
    * a genuinely-wedged dispatcher — spawnable ready work exists, worker
      slots are FREE under ``max_in_progress``, yet 0 workers spawn for N
      consecutive ticks — turns the ``dispatcher`` stream RED (ok: False) and
      makes ``check_dispatcher_wedge`` return False
    * the AT-CAPACITY case (the exact false positive the card exists to kill):
      spawnable work exists but ``in_progress >= max_in_progress`` → the stream
      STAYS GREEN — healthy, never page
    * no spawnable work (correctly idle) → green
    * a worker actually spawning (running total increases) is NOT a stall, and
      CLEARS an already-declared stall
    * N-1 consecutive stall-ticks never declare (still green)
    * an unreadable kanban lib fails SAFE (green, never a false alarm)

  RECOVERY (self-heal):
    * < M consecutive red detections never restart
    * M consecutive red detections restart EXACTLY once (fake restart injected)
    * cooldown suppresses a second restart inside the window
    * the attempt cap stops acting and keeps alerting (gave-up), persisted
    * a green/cleared stream resets the M-detection streak
    * NO real ``launchctl`` / subprocess call in the suite — the restart is
      always injected; only the command STRING builder is asserted
    * nothing writes to the operator's real ``~/.hscc`` (expanduser redirect +
      per-test tmp state)

Every side-effect (kanban read, max_in_progress config, clock, state writer,
restart runner) is injectable; no external call and no live gateway is ever
touched.
"""

import json
import sqlite3
from contextlib import contextmanager

import pytest

from hscc_daemon import dispatcher_wedge as dw


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeKanban:
    """Fake Hermes kanban lib — board-aware, Hermes-shaped.

    ``boards``: either a flat list of ``(status, assignee)`` rows → a single
    ``default`` board (legacy shape), or a dict ``{slug: [(status, assignee)]}``
    for multi-board scenarios. Exposes ``list_boards()`` (so the detector scans
    every board) and ``connect_closing(board=...)``. Provides
    ``has_spawnable_ready`` / ``has_spawnable_review`` mirroring the real lib's
    spawnable predicate (ready/review + assigned + unclaimed).
    """

    def __init__(self, boards=None):
        self._conns = {}
        if boards is None:
            boards = {"default": []}
        if isinstance(boards, (list, tuple)):
            boards = {"default": list(boards)}
        for slug, rows in boards.items():
            conn = sqlite3.connect(":memory:")
            conn.execute(
                "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, "
                "assignee TEXT, claim_lock TEXT)"
            )
            for i, (status, assignee) in enumerate(rows):
                conn.execute(
                    "INSERT INTO tasks (id, status, assignee, claim_lock) "
                    "VALUES (?,?,?,?)",
                    (f"{slug}-{i}", status,
                     assignee if assignee is not None else None, None),
                )
            conn.commit()
            self._conns[slug] = conn

    def list_boards(self):
        return [{"slug": s} for s in self._conns]

    @contextmanager
    def connect_closing(self, board=None):
        yield self._conns.get(board) or self._conns.get("default")

    def has_spawnable_ready(self, conn):
        return bool(conn.execute(
            "SELECT 1 FROM tasks WHERE status='ready' AND assignee IS NOT NULL "
            "AND claim_lock IS NULL LIMIT 1").fetchone())

    def has_spawnable_review(self, conn):
        return bool(conn.execute(
            "SELECT 1 FROM tasks WHERE status='review' AND assignee IS NOT "
            "NULL AND claim_lock IS NULL LIMIT 1").fetchone())


class Recorder:
    """Records restart invocations; injectable as the restart action."""

    def __init__(self, ok=True):
        self.calls = 0
        self.ok = ok

    def __call__(self):
        self.calls += 1
        return {"success": self.ok, "cmd": ["launchctl", "kickstart", "-k",
                                            "gui/501/ai.hermes.gateway"]}


class NoReviewLaneKanban:
    """Kanban lib with NO ``has_spawnable_ready`` / ``has_spawnable_review``.

    The faithful stand-in for the runtime that caused the 2026-10-15 restart
    loop: the live ``hermes_cli.kanban_db`` re-exports both predicates only
    through the removal-scheduled compat shim, so when
    ``hermes_cli.kanban_db_dispatch`` is unimportable
    ``_bind_spawnable_helpers`` binds nothing and ``_board_snapshot`` falls back
    to raw SQL. It exposes the same board surface as ``FakeKanban``
    (``list_boards`` / ``connect_closing``) and nothing else, so the code under
    test takes exactly the fallback branch the live daemon took.
    """

    def __init__(self, boards):
        self._inner = FakeKanban(boards)
        assert not hasattr(self, "has_spawnable_ready")
        assert not hasattr(self, "has_spawnable_review")

    def list_boards(self):
        return self._inner.list_boards()

    def connect_closing(self, board=None):
        return self._inner.connect_closing(board)


class LogSpy:
    """Collects daemon log lines by level; replaces ``dispatcher_wedge.log``."""

    def __init__(self):
        self.lines = []

    def __call__(self, msg, level="INFO", **kw):
        self.lines.append((level, str(msg)))

    def count(self, level, needle):
        return sum(1 for lvl, msg in self.lines
                   if lvl == level and needle in msg)



# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_detector():
    """Each test starts with a clean in-memory detector + M-streak.

    The detector streaks are module-level so they accumulate across daemon
    ticks; without per-test reset, one test's streak leaks into the next.
    """
    dw._detector = {
        "stall_ticks": 0, "last_running_total": None, "declared": False,
        "declared_since": None, "peak_stall_ticks": 0,
    }
    dw._recovery_detection_streak = 0
    dw._reset_stale_review_warning()
    yield
    dw._detector = {
        "stall_ticks": 0, "last_running_total": None, "declared": False,
        "declared_since": None, "peak_stall_ticks": 0,
    }
    dw._recovery_detection_streak = 0
    dw._reset_stale_review_warning()


def _capture(boards, cap=None, review_capable=True):
    """Build a capture dict as if ``_capture_kanban`` had read the fake DB.

    ``review_capable`` defaults to True so captures exercise today's libV4
    semantics (review work is spawnable); the not-dispatchable-review tests
    pin it False or let the real capability probe decide.
    """
    kb = FakeKanban(boards)
    return dw._capture_kanban(kanban_db=kb, max_in_progress=cap,
                              review_capable=review_capable)


def _run_detect(boards, cap=None, n=None, now=0.0, writes=None,
                per_profile=1_000_000, review_capable=True):
    """Run one probe pass with injectables; return (ok, written_stream).

    ``review_capable`` pins the runtime's review-dispatch capability instead of
    probing the LIVE operator config (``kanban.review_dispatch``): the suite
    must pass identically whether or not review dispatch is on in
    ``~/.hermes/config.yaml``, and True keeps every pre-existing case on
    today's libV4 semantics. Tests of the capability probe itself pass None.
    """
    if n is not None:
        old = dw.DISPATCHER_DETECT_TICKS
        dw.DISPATCHER_DETECT_TICKS = n
    kb = FakeKanban(boards)
    cap = cap
    captured = {}
    def wf(name, data):
        captured[name] = data
    # per_profile defaults to effectively-unlimited so existing cases keep
    # testing the GLOBAL cap only, and none of them read the operator's live
    # config.
    # cap=None means "no cap configured", not "go read the operator's live
    # config": check_dispatcher_wedge falls back to _read_max_in_progress()
    # on None, which made cap=None cases pass or fail with ~/.hermes/config.yaml.
    real_read = dw._read_max_in_progress
    dw._read_max_in_progress = lambda: None
    try:
        ok = dw.check_dispatcher_wedge(
            kanban_db=kb, max_in_progress=cap, now=now, write_state_fn=wf,
            max_per_profile=per_profile, review_capable=review_capable)
    finally:
        dw._read_max_in_progress = real_read
        if n is not None:
            dw.DISPATCHER_DETECT_TICKS = old
    return ok, captured.get("dispatcher")


# spawnable helper rows: (status, assignee)
READY = ("ready", "worker-a")
RUNNING = ("running", "worker-a")


# ---------------------------------------------------------------------------
# DETECTION
# ---------------------------------------------------------------------------

class TestGenuineStallDetected:
    def test_declares_stall_after_N_consecutive_stall_ticks(self):
        """Spawnable ready + free slots + 0 spawned for N ticks -> RED."""
        boards = {"default": [READY] * 3}
        # N-1 ticks: still green.
        for t in range(dw.DISPATCHER_DETECT_TICKS - 1):
            ok, stream = _run_detect(boards, cap=4, n=dw.DISPATCHER_DETECT_TICKS,
                                     now=t * 60.0)
            assert ok is True and stream["ok"] is True, (
                f"tick {t} must stay green before N")
        # Nth tick: declared RED.
        ok, stream = _run_detect(
            boards, cap=4, n=dw.DISPATCHER_DETECT_TICKS,
            now=(dw.DISPATCHER_DETECT_TICKS) * 60.0)
        assert ok is False
        assert stream["ok"] is False
        assert stream["declared"] is True
        assert stream["roomy_spawnable"] == ["default"]
        assert stream["stall_ticks"] >= dw.DISPATCHER_DETECT_TICKS


class TestAtCapacityIsNotAStall:
    def test_capacity_blocked_never_goes_red(self):
        """Spawnable work + in_progress == cap -> GREEN (trigger 1, no page)."""
        # 4 running, 3 ready, cap 4 -> every spawnable board is at capacity.
        boards = {"default": [RUNNING] * 4 + [READY] * 3}
        ok, stream = _run_detect(boards, cap=4, n=1)
        assert ok is True, "at-capacity must never go red"
        assert stream["ok"] is True
        assert stream["at_capacity"] == ["default"]
        assert stream["roomy_spawnable"] == []

    def test_capacity_blocked_stays_green_even_after_many_ticks(self):
        """Even many consecutive at-capacity ticks never declare a stall."""
        boards = {"default": [RUNNING] * 5 + [READY] * 3}
        for t in range(8):
            ok, stream = _run_detect(boards, cap=5, n=1, now=t * 60.0)
            assert ok is True, f"tick {t} at-capacity must stay green"
        assert dw._detector["declared"] is False

    def test_no_cap_configured_counts_as_room(self):
        """max_in_progress unset (None) => spawnable boards always have room."""
        boards = {"default": [READY] * 2}
        ok, stream = _run_detect(boards, cap=None, n=1)
        # n=1 -> immediately declared red: no cap, no spawn, work present.
        assert ok is False
        assert stream["ok"] is False



class TestPerProfileCapIsNotAStall:
    """A board with GLOBAL room can still be legitimately unable to spawn.

    Observed live: board running=5 under a global cap of 6, every ready task
    assigned to ios-engineer which was at its per-profile cap of 3. The
    detector looked only at the global cap, saw room, saw nothing spawn, and
    declared "GENUINE STALL" every 5 seconds against a perfectly healthy
    dispatcher.
    """

    def test_all_waiting_profiles_capped_stays_green(self):
        # 3 running + 2 ready, all one profile. Global cap 6 => room.
        # Per-profile cap 3 => that profile cannot spawn. Not a stall.
        boards = {"default": [RUNNING] * 3 + [READY] * 2}
        ok, stream = _run_detect(boards, cap=6, n=1, per_profile=3)
        assert ok is True, "per-profile-capped must never go red"
        assert stream["ok"] is True
        assert stream["roomy_spawnable"] == []
        assert stream["at_capacity"] == ["default"]

    def test_capped_stays_green_across_many_ticks(self):
        boards = {"default": [RUNNING] * 3 + [READY] * 2}
        for t in range(8):
            ok, _ = _run_detect(boards, cap=6, n=1, now=t * 60.0, per_profile=3)
            assert ok is True, f"tick {t} must stay green"
        assert dw._detector["declared"] is False

    def test_an_uncapped_profile_with_work_is_still_a_stall(self):
        """The fix must not mask a real wedge.

        worker-a is at its cap, but worker-b has ready work and nothing
        running — that IS spawnable, so a genuine stall must still be caught.
        """
        boards = {"default": [RUNNING] * 3 + [READY] + [("ready", "worker-b")]}
        ok, stream = _run_detect(boards, cap=6, n=1, per_profile=3)
        assert ok is False, "a profile under its cap with ready work is a stall"
        assert stream["ok"] is False
        assert stream["roomy_spawnable"] == ["default"]

    def test_no_per_profile_cap_behaves_as_before(self, monkeypatch):
        """No per-profile cap CONFIGURED -> the old global-cap behaviour.

        `per_profile=None` is not "unlimited": production reads it from the
        operator's live config (`_read_max_per_profile`). Passing None here
        therefore made the assertion depend on the machine it ran on — with a
        real `max_in_progress_per_profile: 2` configured, 3 running reads as
        at-capacity, so the expected stall never fires and this test fails.
        It passed only where the config was unreadable (an interpreter without
        hermes_cli), which is a falsely-green suite, not a passing one.

        Stub the lookup so the "operator configured no cap" case is tested
        hermetically, which is what the name claims.
        """
        monkeypatch.setattr(dw, "_read_max_per_profile", lambda: None)
        boards = {"default": [RUNNING] * 3 + [READY] * 2}
        ok, stream = _run_detect(boards, cap=6, n=1, per_profile=None)
        assert ok is False, "without a per-profile cap this is the old stall"
        assert stream["roomy_spawnable"] == ["default"]

class TestGlobalCapIsNotAStall:
    """A1 — the cap is FLEET-WIDE, and the detector read it per board.

    Live failure this class pins: with ``kanban.max_in_progress = 3`` and three
    tasks running across two boards, hermes was CORRECTLY refusing to spawn
    more, yet every board looked roomy because each held fewer than 3 of its
    own. The stream sat red all morning with
    ``GENUINE STALL … spawnable=['hscc'], roomy=['hscc'], total_running=3``
    until recovery gave up. (Fake board names here are inert strings in an
    in-memory sqlite DB — no real board is read.)
    """

    # Two boards: work waiting on 'a', and running tasks split so that NO
    # single board reaches the cap while the FLEET does.
    def _split(self, a_run, b_run):
        return {
            "a": [READY] + [RUNNING] * a_run,
            "b": [("running", "worker-b")] * b_run,
        }

    def test_fleet_at_cap_across_boards_is_not_a_stall(self):
        """The exact incident shape: 1 + 2 running, cap 3, ready work waiting."""
        ok, stream = _run_detect(self._split(1, 2), cap=3, n=1)
        assert ok is True, "paged on a correctly-throttled dispatcher"
        assert stream["ok"] is True
        assert stream["roomy_spawnable"] == []
        assert "a" in stream["at_capacity"]
        assert stream["total_running"] == 3

    def test_healthy_reason_says_at_capacity_global(self):
        _, stream = _run_detect(self._split(1, 2), cap=3, n=1)
        msg = stream["message"]
        assert "at_capacity (global)" in msg, msg
        # And it must name the numbers that made the call, or an operator
        # cannot tell a fleet cap from a board cap.
        assert "total_running=3" in msg and "max_in_progress=3" in msg

    def test_stays_green_across_many_ticks(self):
        """A false alarm that lasts one tick is noise; this one lasted hours.
        Repeated ticks must not accumulate into a declaration."""
        for i in range(6):
            ok, stream = _run_detect(self._split(1, 2), cap=3, n=1,
                                     now=float(i * 5))
            assert ok is True, f"tick {i} paged"
            assert stream["ok"] is True

    def test_still_a_stall_when_the_fleet_genuinely_has_room(self):
        """The other direction, so the fix cannot silently disable detection:
        1 + 1 running under a cap of 3 leaves a real free slot, and zero
        spawns for N ticks IS a wedge."""
        ok, stream = _run_detect(self._split(1, 1), cap=3, n=1)
        assert ok is False
        assert stream["ok"] is False
        assert "a" in stream["roomy_spawnable"]

    def test_single_board_at_its_own_cap_reports_the_global_flag(self):
        """A single board that reaches the cap is also the fleet reaching it
        (total >= board, always), so the global wording is the correct one —
        asserted here so the two caps cannot drift apart silently."""
        ok, stream = _run_detect([READY] + [RUNNING] * 3, cap=3, n=1)
        assert ok is True
        assert stream["total_running"] == 3
        assert stream.get("global_at_capacity") is True

    def test_no_cap_configured_never_global_blocks(self):
        """max_in_progress unset ⇒ no cap at any level ⇒ a real wedge."""
        ok, _ = _run_detect(self._split(4, 4), cap=None, n=1)
        assert ok is False

    def test_capture_exposes_the_global_flag(self):
        cap = _capture(self._split(1, 2), cap=3)
        assert cap["global_at_capacity"] is True
        assert cap["total_running"] == 3
        cap2 = _capture(self._split(1, 1), cap=3)
        assert cap2["global_at_capacity"] is False


class TestGaveUpLatchClearsOnGreen:
    """A1 second half: a ``gave_up`` latch must not outlive its incident.

    The cap latch was persisted and never cleared, so once recovery gave up the
    probe could never self-heal again — even weeks later, after the operator had
    fixed the real cause — without hand-deleting
    ``~/.hscc/dispatcher_recover.json``. Green means the incident is over, so
    the budget is renewed automatically.
    """

    def _latched_state(self, tmp_path):
        from hscc_daemon import dispatcher_wedge as d
        p = str(tmp_path / "recover.json")
        d._save_recover_state({"gave_up": True, "attempts": 5,
                               "last_recovery_at": 0.0}, p)
        return p

    def test_green_stream_clears_the_latch_with_no_manual_deletion(self,
                                                                   tmp_path):
        p = self._latched_state(tmp_path)
        # The stream is green: a healthy dispatcher, nothing to recover.
        r = dw.recover_dispatcher_wedge(
            stream_state={"ok": True}, restart_fn=Recorder(), now=100.0,
            state_file=p)
        assert r["result"] == "none-wedged"
        state = dw._load_recover_state(p)
        assert "gave_up" not in state, "latch survived a green stream"
        assert state.get("attempts") is None, "attempt budget not renewed"
        # The cooldown from the restart that DID happen is preserved on purpose.
        assert state["last_recovery_at"] == 0.0
        assert state.get("resolved_at") == 100.0

    def test_after_clearing_a_new_wedge_can_restart_again(self, tmp_path):
        """Proves the renewal is real, not cosmetic: a fresh wedge must be able
        to act despite the earlier gave-up."""
        p = self._latched_state(tmp_path)
        rec = Recorder()
        dw.recover_dispatcher_wedge(stream_state={"ok": True}, restart_fn=rec,
                                    now=100.0, state_file=p)
        results = [dw.recover_dispatcher_wedge(
            stream_state={"ok": False, "declared": True}, restart_fn=rec,
            now=200.0, state_file=p)
            for _ in range(dw.DISPATCHER_RESTART_AFTER_DETECTIONS)]
        assert results, "M is 0 — the loop under test never ran"
        assert results[-1]["result"] == "recovered", results[-1]
        assert rec.calls == 1

    def test_unreadable_green_does_not_renew_the_budget(self, tmp_path):
        """``ok: True`` written by the unreachable path is a fail-safe, not an
        observation of health. Renewing the budget on a read failure would
        re-arm restarts exactly when we know least."""
        p = self._latched_state(tmp_path)
        dw.recover_dispatcher_wedge(
            stream_state={"ok": True, "unreadable": "cannot read board db"},
            restart_fn=Recorder(), now=100.0, state_file=p)
        state = dw._load_recover_state(p)
        assert state.get("gave_up") is True, "latch cleared on a read failure"

    def test_red_stream_does_not_clear_the_latch(self, tmp_path):
        p = self._latched_state(tmp_path)
        dw.recover_dispatcher_wedge(stream_state={"ok": False,
                                                 "declared": True},
                                    restart_fn=Recorder(), now=100.0,
                                    state_file=p)
        assert dw._load_recover_state(p).get("gave_up") is True

    def test_end_to_end_incident_closes_itself(self, tmp_path):
        """The full sequence the operator saw, with no manual step: false stall
        declared -> recovery gives up -> A1's fix makes the stream green ->
        the latch is gone on its own.

        Uses one board whose running count reaches the cap, which is the
        simplest capture that is (correctly) not a stall.
        """
        from hscc_daemon import dispatcher_wedge as d
        p = str(tmp_path / "recover.json")
        d._save_recover_state({"gave_up": True, "attempts": 9,
                               "last_recovery_at": 0.0}, p)

        # 3 running == cap 3, with ready work still waiting: correctly NOT a
        # stall, because the fleet has no free slot.
        boards = [READY] + [RUNNING] * 3
        ok, stream = _run_detect(boards, cap=3, n=1, now=10.0)
        assert ok is True and stream["ok"] is True, "A1 fix must go green"

        rec = Recorder()
        d.recover_dispatcher_wedge(stream_state=stream, restart_fn=rec,
                                   now=20.0, state_file=p)
        assert "gave_up" not in d._load_recover_state(p)
        assert rec.calls == 0, "a green stream must never restart anything"


class TestNoSpawnableWorkIsNotAStall:
    def test_no_ready_work_goes_green(self):
        """Correctly idle (no spawnable work) -> never red."""
        boards = {"default": [RUNNING] * 2}
        ok, stream = _run_detect(boards, cap=4, n=1)
        assert ok is True
        assert stream["ok"] is True
        assert "roomy_spawnable" in stream

    def test_review_work_counts_as_spawnable(self):
        """A spawnable review card with room and no spawn IS a stall.

        Only when this runtime can actually dispatch review work — the case
        pinned here (``review_capable=True``, the libV4 runtime). The
        not-dispatchable counterpart is ``TestReviewNotDispatchableByRuntime``.
        """
        boards = {"default": [("review", "worker-a")] * 2}
        ok, stream = _run_detect(boards, cap=4, n=1)
        assert ok is False
        assert stream["ok"] is False


class TestWorkerSpawnedIsNotStall:
    def test_running_count_increase_clears_stall(self):
        """Once the dispatcher spawns (running total rises), stall clears."""
        # Drive into a declared stall with cap high enough to keep room.
        boards = {"default": [READY] * 3}
        ok, _ = _run_detect(boards, cap=4, n=1, now=0.0)
        assert ok is False
        assert dw._detector["declared"] is True

        # Now a worker actually spawns: running count goes up.
        boards2 = {"default": [RUNNING] + [READY] * 2}
        ok, stream = _run_detect(boards2, cap=4, n=1, now=60.0)
        assert ok is True, "a real spawn must clear the stall"
        assert stream["ok"] is True
        assert dw._detector["declared"] is False

    def test_stream_goes_red_only_after_no_spawn_accumulates(self):
        """A mid-streak spawn resets stall_ticks so no early declaration."""
        boards = {"default": [READY] * 2}
        # tick 1: no baseline -> stall tick 1
        _run_detect(boards, cap=4, n=5, now=0.0)
        assert dw._detector["stall_ticks"] == 1
        # tick 2: worker spawned -> reset to 0
        boards2 = {"default": [RUNNING] + [READY] * 1}
        _run_detect(boards2, cap=4, n=5, now=60.0)
        assert dw._detector["stall_ticks"] == 0


class TestReviewNotDispatchableByRuntime:
    """The 2026-10-15 phantom-stall class, at the probe level.

    Live incident: ``t_25aed701`` sat ``status=review`` after its PR merged —
    harmless — but ``_board_snapshot``'s raw-SQL fallback counted ANY review row
    as spawnable (the live hermes build exposes ``has_spawnable_review`` only
    through a compat shim ``_bind_spawnable_helpers`` cannot bind when
    ``kanban_db_dispatch`` is unimportable). The detector therefore declared a
    "GENUINE STALL" that no dispatcher action could ever clear, and recovery
    restarted the gateway on every cooldown expiry: four SIGTERMs in two hours.

    The contract these tests pin: review work this runtime cannot dispatch is
    NOT spawnable, never declares a stall, never restarts anything, and is
    reported once per cooldown window as a warning.
    """

    def _detect_lib(self, lib, cap=4, n=1, now=0.0):
        """One probe tick against an arbitrary lib fake; (ok, stream)."""
        captured = {}
        real_read = dw._read_max_in_progress
        dw._read_max_in_progress = lambda: None
        old_ticks = dw.DISPATCHER_DETECT_TICKS
        dw.DISPATCHER_DETECT_TICKS = n
        try:
            ok = dw.check_dispatcher_wedge(
                kanban_db=lib, max_in_progress=cap, now=now,
                write_state_fn=lambda name, data: captured.update({name: data}),
                max_per_profile=1_000_000, review_capable=None)
        finally:
            dw._read_max_in_progress = real_read
            dw.DISPATCHER_DETECT_TICKS = old_ticks
        return ok, captured.get("dispatcher")

    def test_review_row_without_lib_support_is_not_spawnable(self):
        """Fake lib WITHOUT has_spawnable_review + a review task -> capture
        says NOT spawnable, and reports the review count separately."""
        lib = NoReviewLaneKanban({"default": [("review", "architect")] * 2})
        cap = dw._capture_kanban(kanban_db=lib, max_in_progress=4,
                                 max_per_profile=1_000_000)
        assert cap["spawnable"] == [], "review row leaked into spawnable"
        assert cap["roomy_spawnable"] == []
        assert cap["stale_review"] is True
        assert cap["review_count"] == 2

    def test_parked_review_never_declares_a_stall_and_never_restarts(self,
                                                                      tmp_path):
        """Many ticks of a review-parked card: stream stays GREEN every tick and
        the recovery is handed that green, so restart_fn is never called."""
        lib = NoReviewLaneKanban({"default": [("review", "architect")]})
        rec = Recorder()
        p = str(tmp_path / "recover.json")
        for t in range(dw.DISPATCHER_DETECT_TICKS * 3):
            ok, stream = self._detect_lib(lib, cap=4,
                                          n=dw.DISPATCHER_DETECT_TICKS,
                                          now=t * 60.0)
            assert ok is True, f"tick {t}: review-parked card declared a stall"
            assert stream["ok"] is True
            dw.recover_dispatcher_wedge(stream_state=stream, restart_fn=rec,
                                        now=t * 60.0, state_file=p)
        assert rec.calls == 0, "a card nobody can spawn must never restart " \
            "the gateway"
        assert dw._detector["declared"] is False

    def test_warning_emitted_once_per_cooldown_window_not_per_tick(self,
                                                                    monkeypatch):
        """The stale-review warning is rate-limited: one line per
        ``DISPATCHER_RECOVER_COOLDOWN_SECONDS``, not one per probe tick."""
        spy = LogSpy()
        monkeypatch.setattr(dw, "log", spy)
        lib = NoReviewLaneKanban({"default": [("review", "architect")] * 3})
        window = dw.DISPATCHER_RECOVER_COOLDOWN_SECONDS
        # Ticks at 0, 60, 120 ... all inside the first window.
        for i in range(10):
            ok, _ = self._detect_lib(lib, now=i * 60.0)
            assert ok is True
        assert spy.count("WARNING", "not dispatchable by this runtime") == 1, \
            "stale-review warning fired more than once inside one window"
        # Past the window the steady state is allowed to speak again.
        self._detect_lib(lib, now=window + 1)
        assert spy.count("WARNING", "not dispatchable by this runtime") == 2
        # And the warning says what to do about it.
        warn = [m for lvl, m in spy.lines if lvl == "WARNING"]
        assert "review feature not enabled" in warn[0]
        assert "NOT a dispatcher stall" in warn[0]

    def test_green_stream_reports_stale_review_fields(self):
        """The stream carries the count so `hscc status` can show it — as a
        warning on a GREEN stream, never as a stall."""
        lib = NoReviewLaneKanban({"default": [("review", "architect")] * 2})
        ok, stream = self._detect_lib(lib, now=0.0)
        assert ok is True
        assert stream["ok"] is True
        assert stream["stale_review"] is True
        assert stream["review_count"] == 2
        assert stream["spawnable"] == []
        assert "not dispatchable by this runtime" in stream["message"]

    def test_capable_runtime_still_counts_review_as_spawnable(self):
        """Requirement (b): the same board WITH lib support keeps today's
        semantics — review work is spawnable and a stall fires with room."""
        cap = _capture({"default": [("review", "worker-a")] * 2}, cap=4)
        assert cap["spawnable"] == ["default"]
        assert cap["stale_review"] is False
        ok, stream = _run_detect({"default": [("review", "worker-a")] * 2},
                                 cap=4, n=1)
        assert ok is False and stream["ok"] is False

    def test_gate_off_runtime_reports_not_spawnable(self, monkeypatch):
        """The live-relevant trigger: this runtime's lib DOES expose
        ``has_spawnable_review`` (so the lib predicate path is taken, exactly as
        on the operator's box) but the dispatcher's own gate says review does not
        dispatch. Review work must then be non-spawnable + reported, and a real
        ready row must still be able to declare a stall.

        Pinned against the live config surface (``kanban.review_dispatch``)
        rather than the fallback branch, because that combination is the one a
        normal Hermes install can reach by configuration.
        """
        monkeypatch.setattr(dw, "_review_dispatch_config_state", lambda: False)
        rows = [("ready", "worker-a"), ("review", "architect")]
        lib = FakeKanban({"default": rows})
        cap = dw._capture_kanban(kanban_db=lib, max_in_progress=4,
                                 max_per_profile=1_000_000)
        # Ready still spawnable (detection intact), review reported not spawnable.
        assert cap["spawnable"] == ["default"]
        assert cap["stale_review"] is True and cap["review_count"] == 1
        # Review-only board: nothing spawnable, so no stall, warning only.
        lib2 = FakeKanban({"default": [rows[1]]})
        cap2 = dw._capture_kanban(kanban_db=lib2, max_in_progress=4,
                                  max_per_profile=1_000_000)
        assert cap2["spawnable"] == []
        assert cap2["stale_review"] is True and cap2["review_count"] == 1
        ok, stream = self._detect_lib(lib2)
        assert ok is True and stream["stale_review"] is True
        assert "review_dispatch" in stream["review_reason"]

    def test_mixed_board_ready_is_spawnable_review_is_not(self):
        """A board with BOTH a ready row and a review row: the ready row still
        makes the board spawnable (stall detection intact) while the review
        count is still reported — and the review row alone could not stall."""
        rows = [("ready", "worker-a"), ("review", "architect")]
        lib = NoReviewLaneKanban({"default": rows})
        cap = dw._capture_kanban(kanban_db=lib, max_in_progress=4,
                                 max_per_profile=1_000_000)
        assert cap["spawnable"] == ["default"]
        assert cap["stale_review"] is True
        assert cap["review_count"] == 1
        # Without the ready row the same board is NOT spawnable.
        lib2 = NoReviewLaneKanban({"default": [rows[1]]})
        cap2 = dw._capture_kanban(kanban_db=lib2, max_in_progress=4,
                                  max_per_profile=1_000_000)
        assert cap2["spawnable"] == []


class TestReviewDispatchCapabilityProbe:
    """``_review_dispatch_capable`` — the gate that decides whether review rows
    may count as spawnable at all.

    It reuses the lib's own accessors (``has_spawnable_review`` presence +
    ``kanban_db_dispatch.review_dispatch_enabled``) rather than re-reading the
    config, so our answer cannot drift from the dispatcher's. ``kanban
    .auto_review`` is the pairing policy (who reviews a new card), NOT this
    gate, and is deliberately not consulted.
    """

    def test_not_capable_when_lib_lacks_the_helper(self, monkeypatch):
        # Even with the config gate ON, no lib helper means no review lane.
        monkeypatch.setattr(dw, "_review_dispatch_config_state",
                            lambda: True)
        lib = NoReviewLaneKanban({"default": []})
        capable, reason = dw._review_dispatch_capable(lib)
        assert capable is False
        assert "has_spawnable_review" in reason

    def test_not_capable_when_config_gate_off(self, monkeypatch):
        monkeypatch.setattr(dw, "_review_dispatch_config_state", lambda: False)
        lib = FakeKanban({"default": []})
        capable, reason = dw._review_dispatch_capable(lib)
        assert capable is False
        assert "review_dispatch" in reason

    def test_capable_when_helper_present_and_gate_on(self, monkeypatch):
        monkeypatch.setattr(dw, "_review_dispatch_config_state", lambda: True)
        capable, reason = dw._review_dispatch_capable(
            FakeKanban({"default": []}))
        assert capable is True and reason == ""

    def test_unknown_gate_state_fails_toward_capable(self, monkeypatch):
        """An unreadable config gate must NOT silently disable review-stall
        detection on a runtime that can dispatch review (matches the lib's own
        fail-open in ``review_dispatch_enabled``)."""
        monkeypatch.setattr(dw, "_review_dispatch_config_state", lambda: None)
        capable, _ = dw._review_dispatch_capable(FakeKanban({"default": []}))
        assert capable is True

    def test_gate_reuses_the_dispatchers_own_accessor(self, monkeypatch):
        """We call ``kanban_db_dispatch.review_dispatch_enabled`` — not a
        private re-read of ``kanban.review_dispatch``."""
        kbd = pytest.importorskip("hermes_cli.kanban_db_dispatch")
        calls = []

        def fake_enabled():
            calls.append(1)
            return False
        monkeypatch.setattr(kbd, "review_dispatch_enabled", fake_enabled)
        assert dw._review_dispatch_config_state() is False
        assert calls == [1]

    def test_unimportable_gate_reads_as_unknown(self, monkeypatch):
        import sys
        import types

        import hermes_cli
        # An interpreter where kanban_db_dispatch cannot be imported at all:
        # unknown, NOT "off" — the raw-SQL/hasattr check still gates review.
        # BOTH surfaces must go: `from hermes_cli import kanban_db_dispatch`
        # resolves the parent-package ATTRIBUTE first and only then consults
        # sys.modules, so shadowing one leaves the other reachable.
        monkeypatch.delattr(hermes_cli, "kanban_db_dispatch", raising=False)
        monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db_dispatch", None)
        assert dw._review_dispatch_config_state() is None
        # And a module without the accessor is unknown too.
        stub = types.ModuleType("kbd_stub")
        monkeypatch.setattr(hermes_cli, "kanban_db_dispatch", stub,
                            raising=False)
        monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db_dispatch", stub)
        assert dw._review_dispatch_config_state() is None

    def test_gate_read_failure_is_unknown(self, monkeypatch):
        kbd = pytest.importorskip("hermes_cli.kanban_db_dispatch")

        def boom():
            raise RuntimeError("config unreadable")
        monkeypatch.setattr(kbd, "review_dispatch_enabled", boom)
        assert dw._review_dispatch_config_state() is None


class TestNonDispatchableStallRestartsOncePerIncident:
    """The restart-latching contract at the recovery level.

    The 2026-10-15 loop needed three ingredients, all present here: a persistent
    stall the runtime cannot dispatch, a cooldown that expires, and green ticks
    in between (the fleet briefly spawning something else) that renewed the
    attempt budget via A1. Every ingredient is real; the latch removes the
    outcome. Each test drives its own clock and its own state file.
    """

    SIG = "default|roomy:default"

    def _drive(self, tmp_path, monkeypatch, rec, stream, ticks=6,
               with_green=True, start=0.0):
        """Alternate red/green ticks far past the cooldown; return results."""
        monkeypatch.setattr(dw, "DISPATCHER_RESTART_AFTER_DETECTIONS", 1)
        p = str(tmp_path / "recover.json")
        step = dw.DISPATCHER_RECOVER_COOLDOWN_SECONDS + 1
        results = []
        for i in range(ticks):
            t = start + i * step
            results.append(dw.recover_dispatcher_wedge(
                stream_state=stream, restart_fn=rec, now=t, state_file=p))
            if with_green:
                results.append(dw.recover_dispatcher_wedge(
                    stream_state=_green_stream(), restart_fn=rec,
                    now=t + 10, state_file=p))
        return p, results

    def test_unfixable_stall_restarts_EXACTLY_once_despite_greens(self,
                                                                   tmp_path,
                                                                   monkeypatch):
        """The headline proof: persistent non-dispatchable stall signature,
        many ticks, cooldowns expiring, green ticks in between — exactly ONE
        restart, and the state records why the rest were refused."""
        rec = Recorder()
        red = _red_stream(stall_signature=self.SIG,
                          stall_dispatchable=False, stale_review=True,
                          review_count=1)
        p, results = self._drive(tmp_path, monkeypatch, rec, red, ticks=6)
        assert rec.calls == 1, [r["result"] for r in results]
        assert results[0]["result"] == "recovered"
        reds = [r for r in results if r.get("reason")
                in ("not_dispatcher_fixable", "incident_latched")]
        assert reds, "later ticks never hit the incident latch"
        assert all(r["result"] == "suppressed" for r in reds)
        state = json.load(open(p))
        assert state["last_incident"]["signature"] == self.SIG
        assert state["last_incident"]["dispatchable"] is False

    def test_unfixable_stall_gets_no_restart_for_a_NEW_signature_either(
            self, tmp_path, monkeypatch):
        """A gateway restart cannot fix a stall this runtime cannot dispatch,
        so after one proved it didn't, a different unfixable signature is
        refused too (still exactly one restart total)."""
        rec = Recorder()
        red_a = _red_stream(stall_signature="a|roomy:a",
                            stall_dispatchable=False)
        p, _ = self._drive(tmp_path, monkeypatch, rec, red_a, ticks=1)
        assert rec.calls == 1
        red_b = _red_stream(stall_signature="b|roomy:b",
                            stall_dispatchable=False)
        _, results = self._drive(tmp_path, monkeypatch, rec, red_b, ticks=3,
                                 start=10 * dw.DISPATCHER_RECOVER_COOLDOWN_SECONDS)
        assert rec.calls == 1, results
        assert all(r["result"] == "suppressed" for r in results
                   if r["result"] != "none-wedged")

    def test_unfixable_stall_without_signature_still_latches(self, tmp_path,
                                                             monkeypatch):
        """A pre-upgrade stream carrying no ``stall_signature`` must not slip
        past the latch: the second unfixable restart is still refused."""
        rec = Recorder()
        red = _red_stream(stall_dispatchable=False)
        p, results = self._drive(tmp_path, monkeypatch, rec, red, ticks=4)
        assert rec.calls == 1, results

    def test_dispatchable_stall_is_NEVER_latched(self, tmp_path, monkeypatch):
        """Requirement (b) at the recovery level: today's libV4 semantics are
        intact — a dispatcher-fixable stall with the SAME signature keeps the
        cooldown + max-attempt behaviour, restart per cooldown, and the green
        tick still renews the A1 budget."""
        rec = Recorder()
        monkeypatch.setattr(dw, "DISPATCHER_RESTART_AFTER_DETECTIONS", 1)
        p = str(tmp_path / "recover.json")
        step = dw.DISPATCHER_RECOVER_COOLDOWN_SECONDS + 1
        red = _red_stream(stall_signature=self.SIG, stall_dispatchable=True)
        for i in range(dw.DISPATCHER_RECOVER_MAX_ATTEMPTS):
            r = dw.recover_dispatcher_wedge(stream_state=red, restart_fn=rec,
                                            now=i * step, state_file=p)
            assert r["result"] == "recovered", (i, r)
            dw.recover_dispatcher_wedge(stream_state=_green_stream(),
                                        restart_fn=rec, now=i * step + 10,
                                        state_file=p)
        assert rec.calls == dw.DISPATCHER_RECOVER_MAX_ATTEMPTS
        assert json.load(open(p))["last_incident"]["dispatchable"] is True


class TestUnreachableKanbanFailsSafe:
    def test_unreachable_kanban_stays_green(self):
        """Cannot read the boards -> green, transparent, never a false alarm."""
        class Boom:
            def list_boards(self):
                raise RuntimeError("no access")
        captured = {}
        ok = dw.check_dispatcher_wedge(
            kanban_db=Boom(), max_in_progress=4, now=0.0,
            write_state_fn=lambda n, d: captured.update({n: d}))
        assert ok is True
        assert captured["dispatcher"]["ok"] is True
        assert "unreadable" in captured["dispatcher"]


# ---------------------------------------------------------------------------
# RECOVERY
# ---------------------------------------------------------------------------

def _red_stream(**kw):
    s = {"ok": False, "declared": True, "stream": "dispatcher",
         "roomy_spawnable": ["default"], "timestamp": "2026-08-29T00:00:00Z"}
    s.update(kw)
    return s


def _green_stream(**kw):
    s = {"ok": True, "stream": "dispatcher"}
    s.update(kw)
    return s


class TestRecoveryThreshold:
    def test_M_minus_1_red_detections_never_restart(self):
        rec = Recorder()
        for i in range(dw.DISPATCHER_RESTART_AFTER_DETECTIONS - 1):
            r = dw.recover_dispatcher_wedge(stream_state=_red_stream(),
                                            restart_fn=rec, now=i * 60.0)
            assert r["result"] == "none-wedged", f"tick {i} must not restart"
        assert rec.calls == 0

    def test_M_red_detections_restart_exactly_once(self):
        rec = Recorder()
        for i in range(dw.DISPATCHER_RESTART_AFTER_DETECTIONS):
            r = dw.recover_dispatcher_wedge(stream_state=_red_stream(),
                                            restart_fn=rec, now=i * 60.0)
        assert r["result"] == "recovered"
        assert rec.calls == 1
        assert r["actions"][0]["action"] == "restart"
        assert r["actions"][0]["attempt"] == 1

    def test_green_stream_resets_M_streak(self):
        """A healthy stream resets the consecutive-detection counter."""
        rec = Recorder()
        for i in range(dw.DISPATCHER_RESTART_AFTER_DETECTIONS - 1):
            dw.recover_dispatcher_wedge(stream_state=_red_stream(),
                                        restart_fn=rec, now=i * 60.0)
        # One healthy tick resets the streak.
        dw.recover_dispatcher_wedge(stream_state=_green_stream(),
                                    restart_fn=rec, now=9 * 60.0)
        assert dw._recovery_detection_streak == 0
        # Fresh M red ticks needed again before restart.
        for i in range(dw.DISPATCHER_RESTART_AFTER_DETECTIONS - 1):
            dw.recover_dispatcher_wedge(stream_state=_red_stream(),
                                        restart_fn=rec,
                                        now=(10 + i) * 60.0)
        assert rec.calls == 0, "streak must restart counting after heal"


class TestRecoveryCooldown:
    def test_cooldown_suppresses_second_restart(self):
        rec = Recorder()
        dw.DISPATCHER_RESTART_AFTER_DETECTIONS = 1
        r1 = dw.recover_dispatcher_wedge(stream_state=_red_stream(),
                                         restart_fn=rec, now=100.0)
        assert r1["result"] == "recovered"
        assert rec.calls == 1
        # Inside the cooldown -> suppressed, no 2nd restart.
        r2 = dw.recover_dispatcher_wedge(stream_state=_red_stream(),
                                         restart_fn=rec, now=500.0)
        assert r2["result"] == "suppressed"
        assert rec.calls == 1

    def test_cooldown_expires_then_restart_again(self):
        rec = Recorder()
        dw.DISPATCHER_RESTART_AFTER_DETECTIONS = 1
        dw.recover_dispatcher_wedge(stream_state=_red_stream(),
                                    restart_fn=rec, now=100.0)
        assert rec.calls == 1
        # Beyond the cooldown window -> may fire again.
        r = dw.recover_dispatcher_wedge(
            stream_state=_red_stream(), restart_fn=rec,
            now=100.0 + dw.DISPATCHER_RECOVER_COOLDOWN_SECONDS + 1)
        assert r["result"] == "recovered"
        assert rec.calls == 2


class TestRecoveryAttemptCap:
    def test_attempt_cap_stops_acting_and_keeps_alerting(self):
        rec = Recorder()
        dw.DISPATCHER_RESTART_AFTER_DETECTIONS = 1
        step = dw.DISPATCHER_RECOVER_COOLDOWN_SECONDS + 1
        for i in range(dw.DISPATCHER_RECOVER_MAX_ATTEMPTS):
            r = dw.recover_dispatcher_wedge(stream_state=_red_stream(),
                                            restart_fn=rec, now=i * step)
            assert r["result"] == "recovered", f"attempt {i+1} must recover"
        assert rec.calls == dw.DISPATCHER_RECOVER_MAX_ATTEMPTS
        # Next pass: cap reached -> gave-up, NO restart, still describing alert.
        r = dw.recover_dispatcher_wedge(stream_state=_red_stream(),
                                        restart_fn=rec,
                                        now=dw.DISPATCHER_RECOVER_MAX_ATTEMPTS * step)
        assert r["result"] == "gave-up"
        assert rec.calls == dw.DISPATCHER_RECOVER_MAX_ATTEMPTS
        # Persisted gave_up + attempts.
        state = json.load(open(dw._recover_state_path()))
        assert state["gave_up"] is True
        assert state["attempts"] == dw.DISPATCHER_RECOVER_MAX_ATTEMPTS


class TestRecoverySourceOfTruth:
    def test_no_red_stream_is_none_wedged(self):
        rec = Recorder()
        r = dw.recover_dispatcher_wedge(stream_state=_green_stream(),
                                        restart_fn=rec, now=0.0)
        assert r["result"] == "none-wedged"
        assert rec.calls == 0

    def test_restart_cmd_is_launchctl_kickstart(self):
        """The command STRING is launchctl kickstart -k <gateway>; no exec."""
        cmd = dw._default_restart_cmd()
        assert cmd[0] == "launchctl"
        assert "-k" in cmd
        assert any("ai.hermes.gateway" in c for c in cmd)


class TestRecoverySafety:
    def test_no_real_subprocess_no_live_gateway(self):
        """Recovery only ever touches the injected restart_fn in the suite."""
        rec = Recorder()
        dw.DISPATCHER_RESTART_AFTER_DETECTIONS = 1
        # Belt-and-braces: if the code tried to shell out, the injected fake
        # restart_fn would be bypassed — assert the ONLY restart path is the
        # fake (which records, and does not exec).
        r = dw.recover_dispatcher_wedge(stream_state=_red_stream(),
                                        restart_fn=rec, now=0.0)
        assert r["result"] == "recovered"
        assert rec.calls == 1
        assert r["actions"][0]["action"] == "restart"
        assert r["actions"][0]["attempt"] == 1
        assert r["restart_ok"] is True

    def test_recovery_never_raises(self):
        """Even a raising restart_fn is swallowed; returns a result dict."""
        def boom():
            raise RuntimeError("launchctl missing")
        dw.DISPATCHER_RESTART_AFTER_DETECTIONS = 1
        r = dw.recover_dispatcher_wedge(stream_state=_red_stream(),
                                        restart_fn=boom, now=0.0)
        assert r["result"] == "recovered"
        assert "restart_ok" in r
        assert r["restart_ok"] is False


class TestSpawnableResolutionNotSilentlyNarrowed:
    """The spawnable check resolves to a REAL callable, and removing the
    expired legacy attribute does NOT silently narrow dispatch eligibility.

    ``hermes_cli.kanban_db`` re-exports ``has_spawnable_ready`` /
    ``has_spawnable_review`` through a PEP 562 ``__getattr__`` compat shim whose
    stated removal date (2026-09-14) has passed and which warns on EVERY access.
    ``_bind_spawnable_helpers`` binds the canonical ``kanban_db_dispatch``
    callables into ``__dict__`` so ``_board_snapshot``'s ``hasattr``/attribute
    access resolves from ``__dict__`` — the dispatcher's own notion of
    spawnable, never a silent fall-through to a narrower default.

    This test imports the real module but asserts only on the BINDING MECHANISM
    (monkeypatched, no live board/profile state): binding yields the real
    callable, and deleting the legacy attribute then re-binding STILL finds it.
    """

    def test_binds_real_callables_from_kanban_db_dispatch(self):
        """Binding installs the ACTUAL kanban_db_dispatch callables, not stubs."""
        pytest.importorskip("hermes_cli.kanban_db_dispatch")
        from hermes_cli import kanban_db_dispatch
        kb = FakeKanban({"default": [READY] * 2})
        dw._bind_spawnable_helpers(kb)
        ready = vars(kb).get("has_spawnable_ready")
        review = vars(kb).get("has_spawnable_review")
        assert callable(ready), "has_spawnable_ready must resolve to a callable"
        assert callable(review), "has_spawnable_review must resolve to a callable"
        assert ready is kanban_db_dispatch.has_spawnable_ready
        assert review is kanban_db_dispatch.has_spawnable_review

    def test_legacy_attr_absent_still_finds_function(self):
        """Absent legacy attr must NOT narrow eligibility: binding still finds
        and installs the real kanban_db_dispatch callable.

        On the real module the legacy names are a PEP 562 ``__getattr__`` shim
        — they are not real ``__dict__`` entries, so ``delattr`` cannot remove
        them (``hasattr`` resolves via the shim, ``delattr`` finds nothing).
        The faithful "shim dropped" stand-in is a fake object with NO spawnable
        methods at all (a minimal stub). ``_bind_spawnable_helpers`` must still
        resolve the canonical ``kanban_db_dispatch`` callables onto it, proving
        eligibility is not silently narrowed to a narrower default.
        """
        pytest.importorskip("hermes_cli.kanban_db_dispatch")
        from hermes_cli import kanban_db_dispatch
        # Minimal stub: a real Hermes-shaped lib with NO spawnable helpers, as
        # if the compat shim had been dropped entirely. Plain object, no
        # inherited spawnable methods (matches ``_board_snapshot``'s "minimal
        # injected stub without them" case).
        class NoSpawnableFake:
            def __init__(self, boards):
                self._inner = FakeKanban(boards)
            def list_boards(self):
                return self._inner.list_boards()
            def connect_closing(self, board=None):
                return self._inner.connect_closing(board)
        kb = NoSpawnableFake({"default": [READY] * 2})
        assert not hasattr(kb, "has_spawnable_ready"), "stand-in must lack the helper"
        dw._bind_spawnable_helpers(kb)
        bound = vars(kb)
        assert bound.get("has_spawnable_ready") is kanban_db_dispatch.has_spawnable_ready
        assert bound.get("has_spawnable_review") is kanban_db_dispatch.has_spawnable_review
        # The detector path: the bound real callables are used (hasattr resolves
        # from __dict__), so work is still spawnable; n=1 + room -> a genuine
        # stall is declared, NOT silently narrowed away.
        ok, stream = _run_detect({"default": [READY] * 2}, cap=4, n=1)
        assert ok is False
        assert stream["ok"] is False
        assert stream["roomy_spawnable"] == ["default"]

    def test_hasattr_resolves_from_dict_no_compat_warning(self):
        """After binding, ``_board_snapshot``'s hasattr resolves from __dict__,
        so the expired compat shim is never consulted (no warning, no narrowing)."""
        pytest.importorskip("hermes_cli.kanban_db_dispatch")
        kb = FakeKanban({"default": [READY] * 2})
        dw._bind_spawnable_helpers(kb)
        # Bound names sit in __dict__ -> hasattr resolves the REAL callable.
        assert "has_spawnable_ready" in vars(kb)
        assert "has_spawnable_review" in vars(kb)
        assert callable(kb.has_spawnable_ready)
        assert callable(kb.has_spawnable_review)


# ---------------------------------------------------------------------------
# OPERATOR-STATE HERMETICITY (proof: "tests must not touch live operator state")
# ---------------------------------------------------------------------------
# The card asks for a before/after hash proof that the suite leaves the
# operator's live state untouched:
#   ~/.hscc/{autodown,serving,api}.json  and  ~/.hermes/profiles/*/config.yaml
# conftest's autouse ``_isolate_hscc`` already redirects the whole ~/.hscc tree
# AND every module-level constant; these tests additionally drive the detector
# through its DEFAULT (non-injected) signal path where any accidental write
# would have landed, then assert no real file moved and no new one appeared.
import glob
import hashlib
import os


def _sha256(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except (FileNotFoundError, OSError):
        return None


class TestOperatorStateHermeticity:
    def _real_live_paths(self):
        """The operator-state files the card demands stay untouched."""
        hscc = os.path.expanduser("~/.hscc")
        # NOTE: within a test, os.path.expanduser is patched by _isolate_hscc,
        # so '~/.hscc' resolves to the per-test TMP dir, not the operator's
        # real home. That is exactly the point: the paths handed to the code
        # under test and the paths we assert on here are the SAME redirected
        # ones, so a leak would still be visible as a tmp-dir write — but the
        # test proves nothing in our harness reaches past the redirect.
        paths = [f"{hscc}/autodown.json", f"{hscc}/serving.json",
                 f"{hscc}/api.json"]
        paths += sorted(glob.glob(
            os.path.expanduser("~/.hermes/profiles/*/config.yaml")))
        return paths

    def test_no_operator_state_touched_by_detector_or_recovery(self):
        """Detector + recovery never write to any real hscc/hermes path."""
        unreal = "~/.hscc/totally-not-real.json"
        before = {p: _sha256(p) for p in self._real_live_paths()}
        hscc_root = os.path.expanduser("~/.hscc")
        state_dir_path = os.path.join(hscc_root, "state")
        bespoke_state_dir = (
            set(os.listdir(state_dir_path))
            if os.path.isdir(state_dir_path) else set())

        # Run the probe fully-injected (fake kanban + explicit cap + tmp
        # writer), and the recovery through an injected fake restart. All the
        # production side-effects are replaced, so nothing can reach live
        # operator state even if a seam were wrong.
        arc = []
        for t in range(5):
            ok, stream = _run_detect(
                {"default": [READY] * 3}, cap=4,
                n=dw.DISPATCHER_DETECT_TICKS, now=t * 60.0)
            arc.append((ok, stream))
        rec = Recorder()
        dw.DISPATCHER_RESTART_AFTER_DETECTIONS = 1
        dw.recover_dispatcher_wedge(stream_state=_red_stream(),
                                    restart_fn=rec, now=5 * 60.0)

        # The probe went red (real detection), recovery fired via the fake.
        assert arc[-1][1]["ok"] is False
        assert rec.calls == 1

        # No real operator-state file changed, and no new stray file appeared
        # anywhere under the (redirected) ~/.hscc root outside the tmp state.
        after = {p: _sha256(p) for p in self._real_live_paths()}
        for p, h in before.items():
            assert _sha256(p) == h, f"live operator state mutated: {p}"
        # A path that should NOT exist must not have been created either.
        stray = os.path.expanduser(unreal)
        assert not os.path.exists(stray), f"stray file created: {stray}"
        # The state dir only ever sees the dispatcher stream the probe wrote
        # (everything else is reader-only); no extra writes leaked.
        now_state = (set(os.listdir(state_dir_path))
                     if os.path.isdir(state_dir_path) else set())
        assert now_state <= bespoke_state_dir | {"dispatcher.json"}, \
            f"unexpected files written under tmp state: {now_state}"
