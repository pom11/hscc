"""The cursor stopgap is still load-bearing — pin WHY, and what retires it.

`t_ff520296` shipped `gateway_wake_cursors()` / `gateway_already_delivered()` to
interpret the gateway notifier's `kanban_notify_subs.last_event_id`, and
`docs/UPSTREAM-kanban-wake-once-gate.md:201` makes *deleting them* the
acceptance test for hermes-agent#152206 §3.1: "if the stopgap cannot be deleted
cleanly, the API is wrong". That sentence is the goal, not today's state, and a
future card must not read it as permission. Deletion is **not reachable** yet,
for a structural reason that is asserted here rather than believed:

1. a Hermes that predates §3.1 (which is every Hermes deployed today, and
   upstream `origin/main` too — 0 occurrences of `kanban_wake_claims` in either)
   resolves to "no claim knowledge", so `BoardWakeClaim.suppressions()` is the
   EMPTY SET for every board. The claim channel cannot suppress anything;
2. and no other emitter writes a row either, so on a real board there is no
   claim to find — the cursor ledger is the only channel that can silence a
   duplicate, and it is the only thing standing between the orchestrator and
   the doubled wake that card was opened for.

Measured, not asserted (`t_3367cb04`, throwaway detached worktree at
`ae162a7f`, the stopgap plus its wiring and the `wake_ledger` seam removed —
129 lines): 7 tests red in `test_orch_waker_dedupe.py`, **4 of them an actual
doubled wake** (`test_one_event_gateway_first_wakes_the_orchestrator_once`,
`…_waker_first_…`, `test_two_events_same_task_same_second_are_not_collapsed`,
`test_replay_after_restart_still_wakes_once`) and 3 the removed seam
(`tick() got an unexpected keyword argument 'wake_ledger'`, missing
`report["suppressed"]`, no `gateway_wake_cursors` attribute). The 29 tests in
`test_orch_waker_wake_claim.py` stayed green — they inject their own claim
layer, which is exactly the point: the claim half is only ever tested against a
row somebody put there, and today nobody puts one there.

So the deletion is reachable only by deleting those six pinned behaviours with
it, which is a rollback of `t_ff520296`, not a migration. What "reachable"
looks like, in order: §3.1 merged and *deployed* in the Hermes the notifier
runs (not just on a branch), the notifier's own claim wiring shipped so every
wake-bearing event gets a row, and then — and only then — delete the two
functions, retarget `test_orch_waker_dedupe.py`'s six cases at the public claim
API, and drop the `suppressed=` stopgap wording from the stream stamp (§9.3 of
the same doc). `test_a_claim_row_alone_is_enough` below is the handover case: it
pins that once rows exist, the claim channel alone holds the line, so the
predicate is "rows exist", never "the claim API is insufficient".

Two invariants this file deliberately does NOT "simplify" (both from the parents
of t_3367cb04, both easy to lose in a cleanup):

* a claim THIS emitter holds is fan-out, not suppression (two subscriptions in
  one profile are two destinations = two wakes) — the notifier decides that with
  `wake_claim_holder()`;
* this waker needs no holder filter only because it never re-reads a claim it
  holds before it sends, which holds only while ONE claim means ONE wake per
  `(profile, task, event)`. If the notifier ever moves to per-destination
  claims, `suppressions` here must gain the same holder filter, or a claim taken
  for destination A silently mutes this REPL wake.

The `kanban_notify_subs` DDL mirrors hermes' real one (as
`test_orch_waker_dedupe.py` does, from `hermes_cli/kanban_db.py`) so the
production reader is exercised against production column names, and there is
deliberately NO `kanban_wake_claims` table in these boards — that absence is the
deployed shape, and it is the whole finding.
"""

import os
import sqlite3

import pytest

from hscc_daemon import orch_waker as ow

NOW = 1_800_000_000
BOARD = "hscc"
ORCH = "hscc-orch"            # what ow.orch_name("hscc") resolves to
TASK = "t_ff520296"           # the card whose measured double-wake motivated this
TITLE = "one event must wake the orchestrator once"

SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT);
CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
  run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
CREATE TABLE kanban_notify_subs (
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    thread_id     TEXT NOT NULL DEFAULT '',
    user_id       TEXT,
    user_id_alt   TEXT,
    chat_type     TEXT,
    notifier_profile TEXT,
    delivery_mode TEXT NOT NULL DEFAULT 'notify',
    delivery_metadata TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    last_ping_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id));
"""

#: What the cursor-only suppression must report on `report["suppressed"]`. The
#: entry schema is byte-identical to the claim channel's on purpose (see
#: t_2cd692f0), so this is the ONLY way to tell the two apart from the outside:
#: the channel is named in the log line, not in the record.
SUPPRESSED = {"board": BOARD, "task": TASK, "kind": "completed",
              "event_id": 1, "event_at": NOW}


def make_board(path, *, sub=None, events=()):
    """A board with one card. ``sub`` = ``(claimed, sub_created_at)`` or None.

    No `kanban_wake_claims` table is ever created here: that absence is what a
    board written by a Hermes that predates §3.1 looks like, and it is the
    condition that keeps the stopgap load-bearing.
    """
    path = str(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO tasks (id, title) VALUES (?, ?)", (TASK, TITLE))
    if sub is not None:
        claimed, sub_created = sub
        conn.execute(
            "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, "
            "thread_id, notifier_profile, delivery_mode, created_at, "
            "last_event_id) VALUES (?, 'local', ?, '', ?, 'notify+wake', ?, ?)",
            (TASK, f"local-{ORCH}", ORCH, sub_created, claimed))
    conn.executemany(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, 1, 'completed', NULL, ?)", [(TASK, at) for at in events])
    conn.commit()
    conn.close()
    return path


def add_events(path, ats):
    """Append `completed` events AFTER the board has been tailed once.

    The waker TAIL-STARTS a board it has never seen, so a fixture that pre-seeds
    the event asserts on a batch that can never exist — that is how "0 wakes"
    would pass for the wrong reason (`test_orch_waker_dedupe.py` says the same).
    """
    conn = sqlite3.connect(str(path))
    conn.executemany(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, 1, 'completed', NULL, ?)", [(TASK, at) for at in ats])
    conn.commit()
    conn.close()


def has_claims_table(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='kanban_wake_claims'").fetchone() is not None
    finally:
        conn.close()


class StubInject:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, board, msg):
        self.calls.append((board, msg))
        return self.ok


class ClaimedOnly:
    """A claim layer that behaves like a board WHERE ROWS EXIST.

    Everything is already claimed by the other emitter, so this waker owes
    nothing — the shape the deployed system will have once the notifier's own
    wiring ships, with no cursor knowledge in play at all.
    """

    def __init__(self):
        self.reads = 0

    def suppressions(self, db_path, profile, idents):
        self.reads += 1
        return {(i[0], i[3]) for i in idents}

    def take(self, db_path, profile, idents):
        return set()

    def confirm(self, db_path, profile, idents):
        pass

    def release(self, db_path, profile, idents):
        pass


def no_ledger(*_args, **_kw):
    """"I do not know" from the cursor channel — the deletion's effect exactly."""
    return {}


def prime(db, state_path):
    """Tick a never-seen board so the NEXT tick tails from its watermark.

    Without this the waker tail-starts and the event is invisible forever, which
    is how "no wake" would pass for the wrong reason.
    """
    ow.tick(now=NOW - 1, boards={BOARD: str(db)}, state_path=state_path)


def deliver_tick(now, db, state_path, inject, **kw):
    """The delivery-decision tick (matured past the 90 s default debounce)."""
    kw.setdefault("has_orch", lambda b: ORCH)
    return ow.tick(now=now, boards={BOARD: str(db)}, state_path=state_path,
                   inject=inject, **kw)


@pytest.fixture
def state_path(tmp_path):
    return str(tmp_path / "hscc-state" / "orch_waker.json")


# ── fact 1: without §3.1 the claim channel knows NOTHING about any board ────

def test_no_3_1_helpers_means_the_claim_channel_suppresses_nothing(tmp_path):
    """`BoardWakeClaim` on a deployed Hermes = the empty set, always.

    This is the half that makes the stopgap irreplaceable: the adapter is
    correct (it fails open towards DELIVERING), which is precisely why it cannot
    carry the dedupe on a Hermes that has none of the four helpers.
    """
    db = make_board(tmp_path / "kanban.db", sub=(1, NOW - 1000), events=[NOW])
    assert not has_claims_table(db), "this fixture must look like a pre-§3.1 board"

    claim = ow.BoardWakeClaim(lib=None)         # "this caller KNOWS there are none"
    assert claim.lib is None
    assert claim.suppressions(str(db), ORCH,
                              [[TASK, "completed", NOW, 1]]) == set()


def test_the_default_claim_layer_is_inert_on_a_pre_3_1_board(tmp_path, state_path):
    """The DEFAULT `wake_claim` (no injection) is inert too — host-independent.

    `BoardWakeClaim()` resolves hermes through the repo's single import seam, so
    on a host with a §3.1 checkout it would find helpers; it must still suppress
    nothing here, because suppression needs a ROW and nothing wrote one. Pinning
    this at the default keeps the finding true even on a developer machine that
    happens to have the branch checked out.
    """
    db = make_board(tmp_path / "kanban.db", sub=(1, NOW - 1000), events=[NOW])
    claim = ow.BoardWakeClaim()
    assert claim.suppressions(str(db), ORCH, [[TASK, "completed", NOW, 1]]) == set()


# ── fact 2: the cursor ledger is the ONLY thing silencing the duplicate ─────

def test_cursor_ledger_silences_the_waker_when_no_claim_row_exists(tmp_path,
                                                                   state_path):
    """The deployed shape: cursor says delivered, claim table absent → silent.

    No `wake_claim` is injected: this is `tick()` exactly as the daemon calls it
    today, against a board a pre-§3.1 Hermes wrote.
    """
    db = make_board(tmp_path / "kanban.db", sub=(1, NOW - 1000))
    prime(db, state_path)
    add_events(db, [NOW])
    report = {}

    ow.tick(now=NOW, boards={BOARD: str(db)}, state_path=state_path, report=report)

    assert report.get("suppressed") == [SUPPRESSED]
    inject = StubInject()
    deliver_tick(NOW + 95, db, state_path, inject)
    assert inject.calls == [], "the waker re-woke a card the gateway delivered"


def test_remove_the_ledger_and_the_same_event_wakes_twice(tmp_path, state_path):
    """THE load-bearing pin: with the cursor channel blind, the wake doubles.

    `wake_ledger=no_ledger` is exactly what deleting `gateway_wake_cursors`
    leaves behind — the claim channel is still mounted and still knows nothing.
    The event goes out: one wake from the notifier (the cursor) plus one from
    here. Deleting the stopgap does not "simplify" anything, it re-opens
    t_ff520296.
    """
    db = make_board(tmp_path / "kanban.db", sub=(1, NOW - 1000))
    prime(db, state_path)
    add_events(db, [NOW])

    ow.tick(now=NOW, boards={BOARD: str(db)}, state_path=state_path,
            wake_ledger=no_ledger)
    inject = StubInject()
    deliver_tick(NOW + 95, db, state_path, inject, wake_ledger=no_ledger)

    assert len(inject.calls) == 1, (
        "the cursor ledger was the only evidence of the gateway's delivery; "
        "blind it and the orchestrator is woken twice for one event")


# ── the handover: what makes the stopgap deletable, pinned as a behaviour ───

def test_a_claim_row_alone_is_enough(tmp_path, state_path):
    """Once rows exist, the claim channel holds the line WITHOUT the ledger.

    This is the deletion predicate made executable, and it is written to SURVIVE
    the deletion: no `wake_ledger` seam and no subscription row at all, so the
    only evidence in the board is the claim. It passes on today's code AND on a
    tree where the stopgap has been deleted (measured both ways in t_3367cb04) —
    which is the point. It is not the claim API that is missing, it is the rows;
    keep this green when the stopgap goes, and retarget `test_orch_waker_dedupe.py`'s
    six cases at exactly this shape.
    """
    db = make_board(tmp_path / "kanban.db")          # no sub => no cursor knowledge
    claims = ClaimedOnly()
    prime(db, state_path)
    add_events(db, [NOW])

    ow.tick(now=NOW, boards={BOARD: str(db)}, state_path=state_path,
            wake_claim=claims)
    inject = StubInject()
    deliver_tick(NOW + 95, db, state_path, inject, wake_claim=claims)

    assert claims.reads >= 1, "the claim channel was never consulted"
    assert inject.calls == [], "a claimed event must not wake the orchestrator"
