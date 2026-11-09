"""One kanban event must wake the orchestrator ONCE (t_ff520296).

Two emitters read the same ``task_events`` row and each woke the orchestrator:
hermes' gateway kanban notifier (``[kanban] Task … completed.``) and this waker
(``[orch-waker] board …``). The measured case is ONE row — t_24e1fd68's single
``completed`` event, id 93871, whose gateway subscription cursor sat exactly on
93871 — delivered twice.

These tests model the other emitter faithfully rather than stubbing it away:
:class:`FakeGateway` claims an event by advancing ``kanban_notify_subs``
``last_event_id`` (what hermes' ``claim_unseen_events_for_sub`` does atomically,
in the SAME board DB) and records the wake the orchestrator would receive. A
test then counts wakes ACROSS BOTH CHANNELS, which is the only assertion that
means "the orchestrator woke once".

Sequence note, because it is a trap: the waker TAIL-STARTS a board it has never
seen (a first tick records the watermark and queues nothing), so every test here
must (1) tick an empty board once, (2) insert the event, (3) tick again. A
fixture that pre-seeds the event asserts on a batch that can never exist — which
is how "0 wakes" would pass for the wrong reason.

The ``kanban_notify_subs`` schema below mirrors hermes' real one
(hermes_cli/kanban_db.py:957) so the production reader is exercised against the
production column names — a fixture narrower than the table it imitates is the
blind spot no assertion can see.
"""

import json
import os
import sqlite3

import pytest

from hscc_daemon import orch_waker as ow

NOW = 1_800_000_000
BOARD = "hscc"
ORCH = "hscc-orch"          # what ow.orch_name("hscc") resolves to
TASK = "t_24e1fd68"
TITLE = "SKILL-GUARD-RANK-TUPLE: rank the tuple"

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


def make_board(path, tasks=(), events=()):
    """Seeded board DB. ``events`` are (task, run_id, kind, payload, created_at)."""
    path = str(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO tasks (id, title) VALUES (?, ?)", tasks)
    if events:
        conn.executemany(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)", events)
    conn.commit()
    conn.close()
    return path


def add_events(path, events):
    conn = sqlite3.connect(str(path))
    conn.executemany(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)", events)
    conn.commit()
    conn.close()


def add_sub(path, task_id, *, profile=ORCH, mode="notify+wake",
            claimed=0, sub_created=NOW - 1000, platform="local", chat_id=None):
    """One subscription row — hermes' durable record of a wake target."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT OR REPLACE INTO kanban_notify_subs "
        "(task_id, platform, chat_id, thread_id, notifier_profile, "
        " delivery_mode, created_at, last_event_id) "
        "VALUES (?, ?, ?, '', ?, ?, ?, ?)",
        (task_id, platform, chat_id or f"local-{profile}", profile, mode,
         sub_created, claimed))
    conn.commit()
    conn.close()
    return path


def cursor_of(path, task_id, profile=ORCH):
    conn = sqlite3.connect(str(path))
    try:
        r = conn.execute(
            "SELECT last_event_id FROM kanban_notify_subs WHERE task_id = ? "
            "AND notifier_profile = ?", (task_id, profile)).fetchone()
        return None if r is None else r[0]
    finally:
        conn.close()


class FakeGateway:
    """The hermes gateway kanban notifier, as this waker can observe it.

    ``deliver`` does what the real notifier does to claim an event — advance the
    subscription's ``last_event_id`` (durable, in the board DB) and take the
    turn — and records both, so a test can count wakes across channels. The sub
    is registered at ``sub_created``, which by default predates every event, as
    it does in life (a task's subscription is written when the card is created).
    """

    def __init__(self, db, task_id=TASK, profile=ORCH, sub_created=NOW - 1000):
        self.db = str(db)
        self.task_id = task_id
        self.profile = profile
        self.wakes = []                      # the orchestrator's OTHER channel
        add_sub(db, task_id, profile=profile, sub_created=sub_created)

    def deliver(self, event_id, kind="completed"):
        add_sub(self.db, self.task_id, profile=self.profile, claimed=event_id)
        self.wakes.append((self.task_id, kind, event_id))

    def total_wakes(self, inject):
        return len(self.wakes) + len(inject.calls)


class StubInject:
    """The waker's delivery seam."""

    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, board, msg):
        self.calls.append((board, msg))
        return self.ok


def completed_event(task=TASK, at=NOW, run_id=1):
    """The measured event: t_24e1fd68's single ``completed`` row (id 93871)."""
    return (task, run_id, "completed", None, at)


def seen_once(db, state_path, *, at=NOW - 1):
    """Tick a never-seen board once so the next tick tails from its watermark.

    Without this the waker tail-starts and the event is invisible forever.
    """
    ow.tick(now=at, boards={BOARD: str(db)}, state_path=state_path)
    return at


def deliver_tick(now, db, state_path, inject, **kw):
    """The delivery-decision tick (matured past the default debounce)."""
    kw.setdefault("has_orch", lambda b: ORCH)
    return ow.tick(now=now, boards={BOARD: str(db)}, state_path=state_path,
                   inject=inject, **kw)


@pytest.fixture
def db(tmp_path):
    """A board with the measured card on it and no events yet."""
    return make_board(tmp_path / "hscc" / "kanban.db", tasks=[(TASK, TITLE)])


@pytest.fixture
def state_path(tmp_path):
    return str(tmp_path / "hscc-state" / "orch_waker.json")


# ── the two orderings of ONE event through BOTH channels ────────────────────

def test_one_event_gateway_first_wakes_the_orchestrator_once(db, state_path):
    """Gateway delivers first (the normal case), waker must stay silent."""
    seen_once(db, state_path)
    gw = FakeGateway(db)
    add_events(db, [completed_event()])
    gw.deliver(1)                                    # claims event id 1
    ow.tick(now=NOW, boards={BOARD: str(db)}, state_path=state_path)
    inject = StubInject()

    deliver_tick(NOW + 95, db, state_path, inject)

    assert inject.calls == [], "the waker re-woke a card the gateway delivered"
    assert gw.total_wakes(inject) == 1


def test_one_event_waker_first_wakes_the_orchestrator_once(db, state_path):
    """The waker scans first, the gateway claims during the debounce window.

    Suppression is decided at DELIVERY time from the then-current cursor, which
    is what makes the rule hold in this order too: the notifier claims on a ~5 s
    cadence while a batch cannot be sent before ``debounce_s`` ages it, so the
    waker is by construction the emitter that finishes second.
    """
    seen_once(db, state_path)
    gw = FakeGateway(db)
    add_events(db, [completed_event()])
    ow.tick(now=NOW, boards={BOARD: str(db)}, state_path=state_path)
    assert ow.load_state(state_path)["pending"][BOARD], "batch should be open"
    gw.deliver(1)                                    # claims mid-window
    inject = StubInject()

    deliver_tick(NOW + 95, db, state_path, inject)

    assert inject.calls == []
    assert gw.total_wakes(inject) == 1


def test_one_event_gateway_never_delivers_wakes_exactly_once(db, state_path):
    """Nobody else delivered, so the wake must still happen — exactly once.

    This is the crash / missed-tick / gateway-down case the card forbids trading
    away, and the guard against a dedupe that eats everything.
    """
    seen_once(db, state_path)
    gw = FakeGateway(db)                             # watching, never claims
    add_events(db, [completed_event()])
    ow.tick(now=NOW, boards={BOARD: str(db)}, state_path=state_path)
    inject = StubInject()

    deliver_tick(NOW + 95, db, state_path, inject)

    assert len(inject.calls) == 1
    assert "t_24e1fd68 done" in inject.calls[0][1]
    assert gw.total_wakes(inject) == 1               # one wake, from us


# ── the collapse guard: distinct events must not merge ─────────────────────

def test_two_distinct_events_seconds_apart_wake_twice(tmp_path, state_path):
    """Two genuine state changes seconds apart -> two wakes (per-event key).

    ``debounce_s``/``min_gap_s`` are zeroed to isolate the dedupe from the
    pre-existing batching rule; the identity keys differ by event id, so the
    second event is never collapsed into the first.
    """
    d = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One"),
                                                        ("t_2", "Two")])
    ow.tick(now=NOW - 1, boards={"b": str(d)}, state_path=state_path)
    inject = StubInject()

    add_events(d, [("t_1", 1, "completed", None, NOW)])
    ow.tick(now=NOW, boards={"b": str(d)}, state_path=state_path,
            inject=inject, has_orch=lambda b: ORCH, debounce_s=0, min_gap_s=0)
    add_events(d, [("t_2", 1, "completed", None, NOW + 3)])
    ow.tick(now=NOW + 5, boards={"b": str(d)}, state_path=state_path,
            inject=inject, has_orch=lambda b: ORCH, debounce_s=0, min_gap_s=0)

    assert len(inject.calls) == 2, "two distinct events collapsed into one wake"
    assert "t_1 done" in inject.calls[0][1] and "t_2 done" not in inject.calls[0][1]
    assert "t_2 done" in inject.calls[1][1]


def test_two_events_same_task_same_second_are_not_collapsed(tmp_path, state_path):
    """The sharpest form of the guard: same task, same SECOND, ids 1 and 2.

    A wall-clock-proximity dedupe — or a key of (task, kind, created_at) alone —
    collapses these. The gateway claimed only event 1, so event 2 must still
    wake the orchestrator.
    """
    d = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    ow.tick(now=NOW - 1, boards={"b": str(d)}, state_path=state_path)
    add_events(d, [("t_1", 1, "completed", None, NOW),
                   ("t_1", 2, "blocked", json.dumps({"reason": "deps"}), NOW)])
    add_sub(d, "t_1", claimed=1)                     # gateway claimed id 1 ONLY
    ow.tick(now=NOW, boards={"b": str(d)}, state_path=state_path)
    inject = StubInject()

    ow.tick(now=NOW + 95, boards={"b": str(d)}, state_path=state_path,
            inject=inject, has_orch=lambda b: ORCH)

    assert len(inject.calls) == 1
    msg = inject.calls[0][1]
    assert "t_1 BLOCKED: deps" in msg
    assert "t_1 done" not in msg, "the gateway-delivered event must not ride along"


def test_two_events_in_one_batch_keep_their_own_identities(tmp_path, state_path):
    """Batching is pre-existing semantics and stays: a burst is ONE nudge whose
    lines (and identities) are all still distinct."""
    d = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One"),
                                                        ("t_2", "Two")])
    ow.tick(now=NOW - 1, boards={"b": str(d)}, state_path=state_path)
    add_events(d, [("t_1", 1, "completed", None, NOW),
                   ("t_2", 1, "blocked", json.dumps({"reason": "x"}), NOW + 2)])

    ow.tick(now=NOW + 2, boards={"b": str(d)}, state_path=state_path)

    st = ow.load_state(state_path)
    assert len(st["pending"]["b"]) == 2
    assert [p[2][0] for p in st["pending"]["b"]] == ["t_1", "t_2"]
    assert [p[2][3] for p in st["pending"]["b"]] == [1, 2]
    assert all(len(p) == 3 for p in st["pending"]["b"])


# ── restart ─────────────────────────────────────────────────────────────────

def test_replay_after_restart_still_wakes_once(db, state_path):
    """State survives a restart and the SAME event is still woken exactly once.

    The dedupe key lives in the persisted pending entry AND in hermes' own
    durable cursor, so a restarted daemon re-derives the same verdict instead of
    re-waking — and the suppression does not have to be re-learned.
    """
    seen_once(db, state_path)
    gw = FakeGateway(db)
    add_events(db, [completed_event()])
    ow.tick(now=NOW, boards={BOARD: str(db)}, state_path=state_path)
    gw.deliver(1)

    # ── the daemon dies here: a fresh process, the same state file on disk ──
    on_disk = json.loads(open(state_path).read())
    assert on_disk["pending"][BOARD][0][2] == [TASK, "completed", NOW, 1]
    assert cursor_of(db, TASK) == 1, "the gateway's claim is the durable ledger"

    inject = StubInject()
    deliver_tick(NOW + 95, db, state_path, inject)
    deliver_tick(NOW + 200, db, state_path, inject)   # and again, and again

    assert inject.calls == []
    assert gw.total_wakes(inject) == 1
    assert not ow.load_state(state_path)["pending"].get(BOARD)


def test_legacy_pending_entry_without_identity_is_never_suppressed(db, state_path,
                                                                   tmp_path):
    """Stopgap/rollback shape: ``[ts, line]`` entries go out even when the
    gateway cursor has passed them — fail-open, never a silent drop."""
    gw = FakeGateway(db)
    gw.deliver(1)
    f = tmp_path / "state" / "orch_waker.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({
        "last": {BOARD: 0},
        "pending": {BOARD: [[NOW, "t_24e1fd68 done — legacy line"]]},
        "sent": {},
    }))
    inject = StubInject()

    ow.tick(now=NOW + 95, boards={BOARD: str(db)}, state_path=str(f),
            inject=inject, has_orch=lambda b: ORCH)

    assert len(inject.calls) == 1 and "legacy line" in inject.calls[0][1]


# ── what may NEVER be suppressed ────────────────────────────────────────────

def test_sub_registered_after_the_event_does_not_suppress(tmp_path, state_path):
    """A sub added later is SEEDED caught-up to MAX(id) — a policy, not a
    delivery. Its cursor must not silence the waker."""
    d = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    ow.tick(now=NOW - 1, boards={"b": str(d)}, state_path=state_path)
    add_events(d, [("t_1", 1, "completed", None, NOW)])
    add_sub(d, "t_1", claimed=1, sub_created=NOW + 10)      # registered LATER
    ow.tick(now=NOW, boards={"b": str(d)}, state_path=state_path)
    inject = StubInject()

    ow.tick(now=NOW + 95, boards={"b": str(d)}, state_path=state_path,
            inject=inject, has_orch=lambda b: ORCH)

    assert len(inject.calls) == 1


def test_notify_only_sub_does_not_suppress(tmp_path, state_path):
    """``delivery_mode=notify`` pings a chat and takes no turn, so its cursor is
    not evidence that anybody was woken."""
    d = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    ow.tick(now=NOW - 1, boards={"b": str(d)}, state_path=state_path)
    add_events(d, [("t_1", 1, "completed", None, NOW)])
    add_sub(d, "t_1", claimed=1, mode="notify")
    ow.tick(now=NOW, boards={"b": str(d)}, state_path=state_path)
    inject = StubInject()

    ow.tick(now=NOW + 95, boards={"b": str(d)}, state_path=state_path,
            inject=inject, has_orch=lambda b: ORCH)

    assert len(inject.calls) == 1


def test_another_profiles_sub_does_not_suppress(tmp_path, state_path):
    """A wake delivered into someone else's profile says nothing about this
    orchestrator's pane."""
    d = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    ow.tick(now=NOW - 1, boards={"b": str(d)}, state_path=state_path)
    add_events(d, [("t_1", 1, "completed", None, NOW)])
    add_sub(d, "t_1", claimed=1, profile="someone-else", platform="telegram")
    ow.tick(now=NOW, boards={"b": str(d)}, state_path=state_path)
    inject = StubInject()

    ow.tick(now=NOW + 95, boards={"b": str(d)}, state_path=state_path,
            inject=inject, has_orch=lambda b: ORCH)

    assert len(inject.calls) == 1


def test_spawn_failed_is_never_suppressed(tmp_path, state_path):
    """``spawn_failed`` is the waker's kind alone — the gateway notifier has no
    wake path for it, so its cursor passing the row is not a delivery."""
    d = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    ow.tick(now=NOW - 1, boards={"b": str(d)}, state_path=state_path)
    add_events(d, [("t_1", 1, "spawn_failed", None, NOW)])
    add_sub(d, "t_1", claimed=1)
    ow.tick(now=NOW, boards={"b": str(d)}, state_path=state_path)
    inject = StubInject()

    ow.tick(now=NOW + 95, boards={"b": str(d)}, state_path=state_path,
            inject=inject, has_orch=lambda b: ORCH)

    assert len(inject.calls) == 1
    assert "t_1 spawn failed" in inject.calls[0][1]


def test_board_without_the_subs_table_fails_open(tmp_path, state_path):
    """A board DB without ``kanban_notify_subs`` at all must not blind the waker
    into silence — no knowledge means deliver."""
    d = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    conn = sqlite3.connect(str(d))
    conn.execute("DROP TABLE kanban_notify_subs")
    conn.commit()
    conn.close()
    ow.tick(now=NOW - 1, boards={"b": str(d)}, state_path=state_path)
    add_events(d, [("t_1", 1, "completed", None, NOW)])
    ow.tick(now=NOW, boards={"b": str(d)}, state_path=state_path)
    inject = StubInject()

    ow.tick(now=NOW + 95, boards={"b": str(d)}, state_path=state_path,
            inject=inject, has_orch=lambda b: ORCH)

    assert len(inject.calls) == 1


def test_unreadable_ledger_fails_open(db, state_path):
    """An injected ledger that raises must produce a WAKE, not a silence."""
    def boom(*a, **k):
        raise RuntimeError("locked")
    seen_once(db, state_path)
    add_events(db, [completed_event()])
    ow.tick(now=NOW, boards={BOARD: str(db)}, state_path=state_path)
    inject = StubInject()

    deliver_tick(NOW + 95, db, state_path, inject, wake_ledger=boom)

    assert len(inject.calls) == 1


# ── the dedupe must not be silently inert ───────────────────────────────────

def test_suppression_is_reported_not_silent(db, state_path):
    """The t_fa9a2faf rule: a dedupe that ate every event would look like a
    healthy quiet board, so every suppression leaves a trace the daemon can
    stamp and doctor can read."""
    seen_once(db, state_path)
    gw = FakeGateway(db)
    add_events(db, [completed_event()])
    report = {}
    ow.tick(now=NOW, boards={BOARD: str(db)}, state_path=state_path,
            report=report)
    gw.deliver(1)
    ow.tick(now=NOW + 95, boards={BOARD: str(db)}, state_path=state_path,
            inject=StubInject(), has_orch=lambda b: ORCH, report=report)

    assert report["suppressed"] == [{"board": BOARD, "task": TASK,
                                     "kind": "completed", "event_id": 1,
                                     "event_at": NOW}]
    # …and the operator-facing stream field names it (never a bare "steady").
    assert "1 wake(s) suppressed as gateway-delivered" in ow._tick_message(
        0, 0, 0, (), 1)
    assert ow._tick_message(0, 0, 0, ()) == "steady"        # shape preserved


def test_suppressed_event_is_not_re_examined_next_tick(db, state_path):
    """The drop is persisted: a later tick neither re-sends nor re-logs it."""
    seen_once(db, state_path)
    gw = FakeGateway(db)
    add_events(db, [completed_event()])
    ow.tick(now=NOW, boards={BOARD: str(db)}, state_path=state_path)
    gw.deliver(1)
    ow.tick(now=NOW + 95, boards={BOARD: str(db)}, state_path=state_path,
            inject=StubInject(), has_orch=lambda b: ORCH)

    report = {}
    inject = StubInject()
    deliver_tick(NOW + 200, db, state_path, inject, report=report)

    assert inject.calls == [] and "suppressed" not in report


# ── the reader's own contract ───────────────────────────────────────────────

def test_gateway_wake_cursors_reads_only_what_it_needs(db):
    add_sub(db, TASK, claimed=0)
    assert ow.gateway_wake_cursors(db, [TASK], ORCH) == {TASK: [(0, NOW - 1000)]}
    assert cursor_of(db, TASK) == 0, "the waker never writes to a board"
    add_sub(db, "t_other", profile=ORCH, claimed=42)
    assert ow.gateway_wake_cursors(db, [TASK], ORCH) == {TASK: [(0, NOW - 1000)]}
    assert ow.gateway_wake_cursors(db, [TASK], "nobody-else") == {}
    assert ow.gateway_wake_cursors(db, [], ORCH) == {}
    assert ow.gateway_wake_cursors(db, [TASK], None) == {}
    assert ow.gateway_wake_cursors(str(db) + ".missing", [TASK], ORCH) == {}
    # Multiple wake subs for one task are all reported, newest claim not merged
    # away: each is judged on its own identity pair. (A second sub is a second
    # CHAT — hermes' primary key is (task, platform, chat, thread).)
    add_sub(db, TASK, claimed=7, sub_created=NOW - 500, chat_id="local-second")
    assert sorted(ow.gateway_wake_cursors(db, [TASK], ORCH)[TASK]) == [
        (0, NOW - 1000), (7, NOW - 500)]


def test_event_identity_is_the_whole_key():
    ev = (7, "t_1", 3, "completed", None, 12345, "Title")
    assert ow.event_identity(ev) == ["t_1", "completed", 12345, 7]
    # Same task and kind, different second or id -> a different key: never
    # collapses two events into one.
    assert ow.event_identity((8, "t_1", 3, "completed", None, 12346, "T")) \
        != ow.event_identity(ev)
    assert ow.event_identity((7, "t_1", 3, "blocked", None, 12345, "T")) \
        != ow.event_identity(ev)
