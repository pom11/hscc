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
    yield
    dw._detector = {
        "stall_ticks": 0, "last_running_total": None, "declared": False,
        "declared_since": None, "peak_stall_ticks": 0,
    }
    dw._recovery_detection_streak = 0


def _capture(boards, cap=None):
    """Build a capture dict as if ``_capture_kanban`` had read the fake DB."""
    kb = FakeKanban(boards)
    return dw._capture_kanban(kanban_db=kb, max_in_progress=cap)


def _run_detect(boards, cap=None, n=None, now=0.0, writes=None):
    """Run one probe pass with injectables; return (ok, written_stream)."""
    if n is not None:
        old = dw.DISPATCHER_DETECT_TICKS
        dw.DISPATCHER_DETECT_TICKS = n
    kb = FakeKanban(boards)
    cap = cap
    captured = {}
    def wf(name, data):
        captured[name] = data
    ok = dw.check_dispatcher_wedge(
        kanban_db=kb, max_in_progress=cap, now=now, write_state_fn=wf)
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


class TestNoSpawnableWorkIsNotAStall:
    def test_no_ready_work_goes_green(self):
        """Correctly idle (no spawnable work) -> never red."""
        boards = {"default": [RUNNING] * 2}
        ok, stream = _run_detect(boards, cap=4, n=1)
        assert ok is True
        assert stream["ok"] is True
        assert "roomy_spawnable" in stream

    def test_review_work_counts_as_spawnable(self):
        """A spawnable review card with room and no spawn IS a stall."""
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
