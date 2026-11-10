"""One orchestrator wake per event, ACROSS emitters, via the shared claim table (t_2cd692f0).

``test_orch_waker_dedupe.py`` deduped against the gateway notifier's
``kanban_notify_subs.last_event_id`` — a cursor that means "the notifier
CONSUMED this event", not "the orchestrator was woken" (hermes-agent#152206
§3.1). The board DB now carries the durable answer instead:
``kanban_wake_claims``, keyed by event identity alone —
``(profile, task_id, event_id)`` where ``event_id`` is the ``task_events.id``
this waker already carries in its pending identity. This module's job is only
to be a *good citizen* of that table: claim before the keystroke, confirm on
acceptance, release on a refused send, and treat every unknown as "deliver".

What is pinned here, in the waker's own words:

  * the waker takes the claim BEFORE it sends, and the key it writes is the
    same key the gateway notifier writes — one event, one row;
  * a duplicate is suppressed ONLY while the claim is HELD (a confirmed
    delivery, or an unconfirmed lease inside ``lease_s``). An expired lease is
    not a hold: the wake goes out again, doubled rather than dropped;
  * a refused send (``WakeNotAccepted`` — here: an inject that fails or raises)
    leaves NO row, so a dead pane can never mute the other emitter;
  * ``spawn_failed`` and every kind outside ``GATEWAY_WAKE_KINDS`` are never
    asked about at all — not suppressed, never written. The gateway has no wake
    path for them, so a claim would drop the only copy of that wake;
  * nothing is claimed on a tick that does not deliver (``inject=None``, an
    armed backoff window) — a claim without a send mutes the other emitter for
    the whole lease;
  * every failure is fail-open towards DELIVERING: an absent table, an
    un-upgraded Hermes, a locked board, a delegated-child refusal.

The lease/CAS semantics themselves are hermes' and are tested there
(``tests/hermes_cli/test_kanban_wake_claim.py``); :class:`FakeClaims` below
mirrors them so the waker is tested against the table's real meaning, not a
stub that agrees with whatever it is asked.
"""

import json
import os
import sqlite3
import time

import pytest

from hscc_daemon import orch_waker as ow

NOW = 1_800_000_000
#: The delivery-decision clock: 95 s after the collect tick, i.e. past
#: ``DEBOUNCE_S`` (90) and still with nothing sent, so ``MIN_GAP_S`` is inert.
DELIVER = NOW + 95
BOARD = "hscc"
ORCH = "hscc-orch"          # what ow.orch_name("hscc") resolves to
TASK = "t_24e1fd68"
TITLE = "WAKE-ONCE: one event, one orchestrator wake"
GATEWAY_EMITTER = "gateway-notifier"   # the OTHER emitter's name in the table

#: verbatim from hermes_cli/kanban_db.py SCHEMA_SQL (hermes-agent#152206 §3.1)
WAKE_CLAIMS_DDL = """
CREATE TABLE IF NOT EXISTS kanban_wake_claims (
    profile       TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    event_id      INTEGER NOT NULL,
    emitter       TEXT NOT NULL,
    claimed_at    INTEGER NOT NULL,
    confirmed_at  INTEGER,
    PRIMARY KEY (profile, task_id, event_id)
);
"""

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
""" + WAKE_CLAIMS_DDL


def make_board(path, *, with_claims=True, tasks=(), events=()):
    """Seeded board DB. ``events`` are (task, run_id, kind, payload, created_at).

    ``with_claims=False`` builds a board predating §3.1 — the shape on a board
    this waker has no business guessing about.
    """
    path = str(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA if with_claims else SCHEMA.replace(WAKE_CLAIMS_DDL, ""))
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


def add_sub(path, task_id, *, profile=ORCH, mode="notify+wake", claimed=0,
            sub_created=NOW - 1000, platform="local", chat_id=None):
    """A gateway subscription row — the OLD evidence channel.

    Tests that pin the CLAIM default to NO sub at all: ``gateway_wake_cursors``
    then returns ``{}``, so the claim table is the only thing that can suppress
    and an assertion about suppression means the claim did it.
    """
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


class FakeClaims:
    """``kanban_wake_claims`` over the real DDL, as the waker's adapter uses it.

    Deliberately a *table*, not a yes-man: it honours the lease (an unconfirmed
    claim suppresses only inside ``lease_s``, hermes'
    ``_WAKE_CLAIM_LEASE_S``), it is holder-only on confirm/release, and it
    answers the caller's key — so a waker that asked about the wrong profile,
    the wrong task or a non-wake-bearing kind would show up as a wrong answer
    rather than a passing test.
    """

    LEASE_S = 120                      # hermes' _WAKE_CLAIM_LEASE_S

    def __init__(self, db, *, emitter=None, now=NOW, fail=None):
        self.db = str(db)
        self.emitter = emitter or ow.WAKE_CLAIM_EMITTER
        self.now = now
        self.fail = fail               # exception type raised by every method
        self.asked = []                # [(method, profile, sorted(keys))]

    # -- helpers ----------------------------------------------------------
    def rows(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(
                "SELECT profile, task_id, event_id, emitter, claimed_at, "
                "confirmed_at FROM kanban_wake_claims "
                "ORDER BY profile, task_id, event_id", ())]
        except sqlite3.Error:
            return []                 # absent table = no claims, the safe read
        finally:
            conn.close()

    def seed(self, profile, task_id, event_id, *, emitter=GATEWAY_EMITTER,
             claimed_at=None, confirmed_at=None):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT OR REPLACE INTO kanban_wake_claims "
            "(profile, task_id, event_id, emitter, claimed_at, confirmed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (profile, str(task_id), int(event_id), emitter,
             self.now - 10 if claimed_at is None else int(claimed_at),
             confirmed_at))
        conn.commit()
        conn.close()

    def _guard(self, method, profile, idents):
        if self.fail:
            raise self.fail(f"{method}: boom")
        keys = sorted((i[0], i[3]) for i in idents)
        self.asked.append((method, profile, tuple(keys)))
        return keys

    # -- the adapter interface the waker calls ----------------------------
    def suppressions(self, db_path, profile, idents):
        keys = self._guard("suppressions", profile, idents)
        cutoff = self.now - self.LEASE_S
        out = set()
        for task, ev_id in keys:
            conn = sqlite3.connect(self.db)
            try:
                row = conn.execute(
                    "SELECT confirmed_at, claimed_at FROM kanban_wake_claims "
                    "WHERE profile = ? AND task_id = ? AND event_id = ?",
                    (profile, task, ev_id)).fetchone()
            except sqlite3.Error:
                row = None            # absent table: unknown -> not suppressed
            finally:
                conn.close()
            if row and (row[0] is not None or row[1] > cutoff):
                out.add((task, ev_id))
        return out

    def take(self, db_path, profile, idents):
        keys = self._guard("take", profile, idents)
        held = set()
        for task, ev_id in keys:
            conn = sqlite3.connect(self.db)
            try:
                row = conn.execute(
                    "SELECT emitter, claimed_at, confirmed_at "
                    "FROM kanban_wake_claims "
                    "WHERE profile = ? AND task_id = ? AND event_id = ?",
                    (profile, task, ev_id)).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO kanban_wake_claims "
                        "(profile, task_id, event_id, emitter, claimed_at, "
                        " confirmed_at) VALUES (?, ?, ?, ?, ?, NULL)",
                        (profile, task, ev_id, self.emitter, self.now))
                    conn.commit()
                    held.add((task, ev_id))
                elif row[2] is None and self.now - row[1] >= self.LEASE_S:
                    conn.execute(
                        "UPDATE kanban_wake_claims SET emitter = ?, "
                        "claimed_at = ? WHERE profile = ? AND task_id = ? "
                        "AND event_id = ? AND confirmed_at IS NULL",
                        (self.emitter, self.now, profile, task, ev_id))
                    conn.commit()
                    held.add((task, ev_id))
            except sqlite3.Error:
                # Absent table / unreadable board: hermes' `try_claim_wake`
                # answers True here (`_wake_claim_open(..., fallback=True)`) —
                # "cannot claim" means YOU OWE IT, so the caller delivers. The
                # opposite reading is what turns a missing table into a
                # silently silent waker, and it is the mistake this fake was
                # written to be able to catch.
                held.add((task, ev_id))
            finally:
                conn.close()
        return held

    def confirm(self, db_path, profile, idents):
        keys = self._guard("confirm", profile, idents)
        conn = sqlite3.connect(self.db)
        for task, ev_id in keys:
            conn.execute(
                "UPDATE kanban_wake_claims SET confirmed_at = ? "
                "WHERE profile = ? AND task_id = ? AND event_id = ? "
                "AND emitter = ? AND confirmed_at IS NULL",
                (self.now, profile, task, ev_id, self.emitter))
        conn.commit()
        conn.close()

    def release(self, db_path, profile, idents):
        keys = self._guard("release", profile, idents)
        conn = sqlite3.connect(self.db)
        for task, ev_id in keys:
            conn.execute(
                "DELETE FROM kanban_wake_claims WHERE profile = ? "
                "AND task_id = ? AND event_id = ? AND emitter = ?",
                (profile, task, ev_id, self.emitter))
        conn.commit()
        conn.close()


class ExplodingClaims(FakeClaims):
    """Every method raises — 'this waker cannot know', which must mean deliver."""

    def __init__(self, db):
        super().__init__(db, fail=RuntimeError)


class StubInject:
    def __init__(self, ok=True, raises=None):
        self.calls = []
        self.ok = ok
        self.raises = raises

    def __call__(self, board, msg):
        self.calls.append((board, msg))
        if self.raises:
            raise self.raises("tmux: pane not accepted")
        return self.ok


def event(task=TASK, kind="completed", at=NOW, run_id=1):
    return (task, run_id, kind, None, at)


def ids_of(path):
    conn = sqlite3.connect(str(path))
    try:
        return [r[0] for r in conn.execute("SELECT id FROM task_events ORDER BY id")]
    finally:
        conn.close()


def seen_once(db, state_path, *, at=NOW - 1):
    """Tick a never-seen board once so the next tick tails from its watermark.

    Without this the waker tail-starts and the event is invisible forever.
    """
    ow.tick(now=at, boards={BOARD: str(db)}, state_path=state_path)
    return at


def collect(db, state_path, *, at=NOW):
    """Scan what was added since into ``pending``, stamping the batch at ``at``.

    Separate from :func:`deliver_tick` because two tests need to decide for
    themselves whether delivery is wired at all (``inject=None``) — and the
    stamp is what the debounce clock then measures from.
    """
    ow.tick(now=at, boards={BOARD: str(db)}, state_path=state_path)
    return at


def deliver_tick(db, state_path, inject, claims, *, at=DELIVER, **kw):
    """Collect, then make the delivery decision (matured past the debounce).

    The sequence mirrors ``test_orch_waker_dedupe.py``, which is the only shape
    the product can actually produce: the batch is scanned on one tick and
    spoken on a later one, because ``debounce_s`` cannot have elapsed inside a
    single pass. A test that ticks once with a fresh event asserts on a batch
    the waker is not allowed to send yet — which is how "no wake" would pass
    for the wrong reason.
    """
    collect(db, state_path)
    kw.setdefault("has_orch", lambda b: ORCH)
    return ow.tick(now=at, boards={BOARD: str(db)}, state_path=state_path,
                   inject=inject, wake_claim=claims, **kw)


def state_doc(state_path):
    with open(str(state_path), encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# the claim is taken before the keystroke, and settled after it
# --------------------------------------------------------------------------- #

def test_claims_before_sending_and_confirms_on_accepted_wake(tmp_path):
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    state = tmp_path / "state.json"
    seen_once(db, state)
    add_events(db, [event()])
    ev_id = ids_of(db)[0]
    inject = StubInject(ok=True)

    decisions = deliver_tick(db, state, inject, FakeClaims(db))

    assert [d["ok"] for d in decisions] == [True]
    assert len(inject.calls) == 1
    rows = FakeClaims(db).rows()
    assert [(r["profile"], r["task_id"], r["event_id"]) for r in rows] == [
        (ORCH, TASK, ev_id)]
    assert rows[0]["emitter"] == ow.WAKE_CLAIM_EMITTER
    assert rows[0]["confirmed_at"] is not None, "an accepted wake settles the claim"


def test_claim_is_keyed_on_the_recipient_profile_not_the_board(tmp_path):
    """The key is (profile, task, event) — the orchestrator, not the board slug.

    A claim written under the board name would never collide with the gateway
    notifier's row, i.e. a dedupe that silently dedupes nothing.
    """
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    state = tmp_path / "state.json"
    seen_once(db, state)
    add_events(db, [event()])
    claims = FakeClaims(db, now=DELIVER)

    deliver_tick(db, state, StubInject(ok=True), claims,
                 has_orch=lambda b: "orion-orch")

    assert [(r["profile"], r["task_id"]) for r in claims.rows()] == [
        ("orion-orch", TASK)]
    assert claims.asked[0][1] == "orion-orch"


def test_refused_wake_releases_the_claim_so_the_other_emitter_still_owes_it(tmp_path):
    """WakeNotAccepted (here: an inject that fails) must leave NO row.

    A lease left behind suppresses the gateway notifier for ``lease_s`` for a
    wake that never happened — a dropped wake dressed as a dedupe, the one
    outcome this whole table exists to avoid.
    """
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    state = tmp_path / "state.json"
    seen_once(db, state)
    add_events(db, [event()])
    claims = FakeClaims(db, now=DELIVER)

    decisions = deliver_tick(db, state, StubInject(ok=False), claims)

    assert [d["ok"] for d in decisions] == [False]
    assert claims.rows() == []
    assert ("release", ORCH, ((TASK, ids_of(db)[0]),)) in claims.asked
    kept = [p[2][0] for p in state_doc(state)["pending"][BOARD]]
    assert kept == [TASK], "pending kept for retry"


def test_raising_inject_releases_the_claim_too(tmp_path):
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    state = tmp_path / "state.json"
    seen_once(db, state)
    add_events(db, [event()])
    claims = FakeClaims(db, now=DELIVER)

    decisions = deliver_tick(db, state, StubInject(raises=RuntimeError), claims)

    assert [d["ok"] for d in decisions] == [False]
    assert claims.rows() == []


# --------------------------------------------------------------------------- #
# suppression happens ONLY while the claim is held
# --------------------------------------------------------------------------- #

def test_confirmed_claim_by_the_other_emitter_suppresses_our_wake(tmp_path):
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    state = tmp_path / "state.json"
    seen_once(db, state)
    add_events(db, [event()])
    ev_id = ids_of(db)[0]
    claims = FakeClaims(db, now=DELIVER)
    claims.seed(ORCH, TASK, ev_id, emitter=GATEWAY_EMITTER, confirmed_at=NOW + 5)
    inject = StubInject(ok=True)

    decisions = deliver_tick(db, state, inject, claims)

    assert decisions == [], "nothing left to send"
    assert inject.calls == []
    assert "pending" not in state_doc(state) or BOARD not in state_doc(state)["pending"]


def test_the_dropped_duplicate_is_persisted_and_never_re_examined(tmp_path):
    """A suppressed event is dropped from `pending` for good.

    Otherwise every later tick re-reads it, re-logs it and — worse — can race
    the claim table into sending it after the lease expires.
    """
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    state = tmp_path / "state.json"
    seen_once(db, state)
    add_events(db, [event()])
    claims = FakeClaims(db, now=DELIVER)
    claims.seed(ORCH, TASK, ids_of(db)[0], emitter=GATEWAY_EMITTER,
                confirmed_at=NOW + 5)
    inject = StubInject(ok=True)

    deliver_tick(db, state, inject, claims)
    first = state_doc(state)
    deliver_tick(db, state, inject, claims, at=NOW + 400)
    second = state_doc(state)

    assert inject.calls == []
    assert first.get("last", {}).get(BOARD) == second.get("last", {}).get(BOARD)
    assert BOARD not in second.get("pending", {})


def test_live_unconfirmed_lease_suppresses(tmp_path):
    """An unconfirmed claim YOUNGER than the lease is held: someone owes it."""
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    state = tmp_path / "state.json"
    seen_once(db, state)
    add_events(db, [event()])
    claims = FakeClaims(db, now=DELIVER)
    claims.seed(ORCH, TASK, ids_of(db)[0], emitter=GATEWAY_EMITTER,
                claimed_at=DELIVER - 10)  # 10 s old, inside the 120 s lease
    inject = StubInject(ok=True)

    decisions = deliver_tick(db, state, inject, claims)

    assert decisions == []
    assert inject.calls == []


def test_expired_lease_is_not_a_hold_the_wake_goes_out(tmp_path):
    """An unconfirmed claim OLDER than the lease is not held: deliver.

    The crashed-sender case. The cost of the wrong choice here is a dropped
    wake; the cost of this choice is a doubled one, which is the status quo.
    """
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    state = tmp_path / "state.json"
    seen_once(db, state)
    add_events(db, [event()])
    claims = FakeClaims(db, now=DELIVER)
    claims.seed(ORCH, TASK, ids_of(db)[0], emitter=GATEWAY_EMITTER,
                claimed_at=NOW - 500)     # far past the lease
    inject = StubInject(ok=True)

    decisions = deliver_tick(db, state, inject, claims)

    assert [d["ok"] for d in decisions] == [True]
    assert len(inject.calls) == 1
    mine = claims.rows()
    assert len(mine) == 1 and mine[0]["emitter"] == ow.WAKE_CLAIM_EMITTER
    assert mine[0]["confirmed_at"] is not None, "the take-over was settled by us"


def test_claim_held_for_another_profile_does_not_suppress(tmp_path):
    """The claim is per recipient: a human's wake says nothing about the orch."""
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    state = tmp_path / "state.json"
    seen_once(db, state)
    add_events(db, [event()])
    claims = FakeClaims(db, now=DELIVER)
    claims.seed("telegram-user", TASK, ids_of(db)[0], emitter=GATEWAY_EMITTER,
                confirmed_at=NOW + 5)
    inject = StubInject(ok=True)

    decisions = deliver_tick(db, state, inject, claims)

    assert [d["ok"] for d in decisions] == [True]
    assert len(inject.calls) == 1


# --------------------------------------------------------------------------- #
# kinds the gateway never wakes for are never asked about at all
# --------------------------------------------------------------------------- #

def test_spawn_failed_is_never_suppressed_and_never_claimed(tmp_path):
    """``spawn_failed`` is this waker's kind alone.

    Even with a CONFIRMED claim sitting in the table for that exact event, the
    orchestrator must still be told: the gateway has no wake path for it, so
    honouring a claim there drops the only copy of that wake. And the waker
    must not write one either — a row it wrote would mute the other emitter for
    an event that was never delivered to anyone.
    """
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    state = tmp_path / "state.json"
    seen_once(db, state)
    add_events(db, [event(kind="spawn_failed")])
    ev_id = ids_of(db)[0]
    claims = FakeClaims(db, now=DELIVER)
    claims.seed(ORCH, TASK, ev_id, emitter=GATEWAY_EMITTER, confirmed_at=NOW + 5)
    inject = StubInject(ok=True)

    decisions = deliver_tick(db, state, inject, claims)

    assert [d["ok"] for d in decisions] == [True]
    assert len(inject.calls) == 1
    assert ("spawn failed" in decisions[0]["msg"]
            or "spawn_failed" in decisions[0]["msg"])
    for method, _profile, keys in claims.asked:
        assert keys == (), f"{method} must never be asked about spawn_failed"
    rows = claims.rows()
    assert [r for r in rows if r["emitter"] == ow.WAKE_CLAIM_EMITTER] == [], \
        "the waker must not claim an event the gateway cannot wake for"
    assert [r["confirmed_at"] for r in rows if r["event_id"] == ev_id] == [NOW + 5], \
        "and must not settle the other emitter's row either"


def test_scanned_but_not_wake_bearing_kind_is_never_claimed(tmp_path):
    """A pending batch of ONLY non-wake-bearing kinds never touches the table.

    ``spawn_failed`` is in :data:`KINDS` (so it reaches ``pending``) but outside
    :data:`GATEWAY_WAKE_KINDS` (the gateway cannot wake for it), and a
    ``comment``-style row is outside both. Neither may be claimed or suppressed:
    a row written here would be honoured by the other emitter for a wake it can
    never make.
    """
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    state = tmp_path / "state.json"
    seen_once(db, state)
    add_events(db, [event(kind="spawn_failed")])
    claims = FakeClaims(db, now=DELIVER)
    asked = []

    class Spy:
        def suppressions(self, d, profile, idents):
            asked.append(("suppress", [i[1] for i in idents]))
            return set()

        def take(self, d, profile, idents):
            asked.append(("take", [i[1] for i in idents]))
            return {ow._claim_key(i) for i in idents}

        def confirm(self, d, profile, idents):
            asked.append(("confirm", [i[1] for i in idents]))

        def release(self, d, profile, idents):
            asked.append(("release", [i[1] for i in idents]))

    decisions = deliver_tick(db, state, StubInject(ok=True), Spy())

    assert [d["ok"] for d in decisions] == [True]
    assert asked == [], f"the table was consulted for a non-wake kind: {asked}"
    assert claims.rows() == []


def test_every_wake_bearing_kind_the_batch_carries_is_asked_about():
    """The positive half of the filter: each claimable kind is put to the table.

    ``spawn_failed`` is deliberately absent — it is scannable (:data:`KINDS`)
    but not claimable, which is the asymmetry the test above pins.
    """
    asked = []

    class Spy:
        def suppressions(self, d, profile, idents):
            asked.extend(i[1] for i in idents)
            return set()

        def take(self, d, profile, idents):
            return {ow._claim_key(i) for i in idents}

        def confirm(self, d, profile, idents):
            pass

        def release(self, d, profile, idents):
            pass

    pend = [[NOW, f"line {k}", [TASK, k, NOW, n]]
            for n, k in enumerate(ow.GATEWAY_WAKE_KINDS, start=1)]
    ow._suppress_claimed(pend, "db", ORCH, Spy())

    assert sorted(asked) == sorted(ow.GATEWAY_WAKE_KINDS)


def test_claim_layer_partitions_wake_bearing_kinds_on_both_sides():
    """A kind outside the wake set cannot reach the table even by accident.

    Both halves in one: the suppressed event is dropped, the two non-wake-
    bearing ones survive DESPITE a table that claims to hold them, and the only
    kind the table was ever asked about is the one the gateway can wake for.
    """
    asked = []

    class Spy:
        def suppressions(self, db, profile, idents):
            asked.append(("suppress", [i[1] for i in idents]))
            return {(TASK, 1)}          # "completed" is owed by someone else

        def take(self, db, profile, idents):
            asked.append(("take", [i[1] for i in idents]))
            return {ow._claim_key(i) for i in idents}

        def confirm(self, db, profile, idents):
            asked.append(("confirm", [i[1] for i in idents]))

        def release(self, db, profile, idents):
            asked.append(("release", [i[1] for i in idents]))

    pend = [[NOW, "line a", [TASK, "completed", NOW, 1]],
            [NOW, "line b", ["t_b", "spawn_failed", NOW, 2]],
            [NOW, "line c", ["t_c", "comment", NOW, 3]]]
    kept, gone = ow._suppress_claimed(pend, "db", ORCH, Spy())

    assert [p[2][1] for p in kept] == ["spawn_failed", "comment"]
    assert [i[1] for i in gone] == ["completed"]
    assert asked == [("suppress", ["completed"])]


# --------------------------------------------------------------------------- #
# a tick that does not deliver must not claim
# --------------------------------------------------------------------------- #

def test_no_claim_when_delivery_is_not_wired(tmp_path):
    """``inject=None`` reports the decision without speaking.

    Claiming there would hold the lease and mute the gateway notifier for
    ``lease_s`` for a wake nobody sent.
    """
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    state = tmp_path / "state.json"
    seen_once(db, state)
    add_events(db, [event()])
    claims = FakeClaims(db, now=DELIVER)
    collect(db, state)

    decisions = ow.tick(now=DELIVER, boards={BOARD: str(db)},
                        state_path=state, has_orch=lambda b: ORCH,
                        wake_claim=claims)

    assert [d["ok"] for d in decisions] == [None]
    assert claims.rows() == []
    assert [m for m, _p, _k in claims.asked] == ["suppressions"]


def test_no_claim_while_the_dead_pane_backoff_is_armed(tmp_path):
    """An armed backoff window skips the delivery, so it must skip the claim."""
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    state = tmp_path / "state.json"
    seen_once(db, state)
    add_events(db, [event()])
    claims = FakeClaims(db, now=DELIVER)
    for _ in range(3):
        deliver_tick(db, state, StubInject(ok=False), claims)
    claims.asked.clear()

    decisions = deliver_tick(db, state, StubInject(ok=True), claims,
                             fail_threshold=3, backoff_s=900)

    assert decisions == []
    assert [m for m, _p, _k in claims.asked] == ["suppressions"]
    assert [r for r in claims.rows() if r["emitter"] == ow.WAKE_CLAIM_EMITTER] == []


# --------------------------------------------------------------------------- #
# fail-open towards delivering, every direction
# --------------------------------------------------------------------------- #

def test_exploding_claim_layer_still_delivers(tmp_path, caplog):
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    state = tmp_path / "state.json"
    seen_once(db, state)
    add_events(db, [event()])
    inject = StubInject(ok=True)

    with caplog.at_level("WARNING"):
        decisions = deliver_tick(db, state, inject,
                                 ExplodingClaims(db))

    assert [d["ok"] for d in decisions] == [True]
    assert len(inject.calls) == 1
    assert any("claim" in r.getMessage().lower() for r in caplog.records), \
        "a dedupe that cannot see must say so — fail-LOUD, t_fa9a2faf"


def test_absent_claim_table_delivers(tmp_path):
    """A board predating §3.1: no table, no knowledge, no suppression."""
    db = make_board(tmp_path / "kanban.db", with_claims=False,
                    tasks=[(TASK, TITLE)])
    state = tmp_path / "state.json"
    seen_once(db, state)
    add_events(db, [event()])
    claims = FakeClaims(db, now=DELIVER)          # every read hits "no such table"
    inject = StubInject(ok=True)

    decisions = deliver_tick(db, state, inject, claims)

    assert [d["ok"] for d in decisions] == [True]
    assert len(inject.calls) == 1


def test_unreadable_board_path_delivers(tmp_path):
    """A claim layer whose board vanished cannot know anything."""
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    state = tmp_path / "state.json"
    seen_once(db, state)
    add_events(db, [event()])
    claims = FakeClaims(tmp_path / "gone.db")
    inject = StubInject(ok=True)

    decisions = deliver_tick(db, state, inject, claims)

    assert [d["ok"] for d in decisions] == [True]


# --------------------------------------------------------------------------- #
# the real adapter: it borrows hermes' helpers and adds only plumbing
# --------------------------------------------------------------------------- #

class RecordingLib:
    """hermes_cli.kanban_db_notify, as seen from the waker: four callables."""

    def __init__(self, *, claimed=(), owed=(), raise_on=None):
        self.claimed = set(claimed)
        self.owed = set(owed)
        self.raise_on = raise_on
        self.calls = []

    def _check(self, op):
        if self.raise_on == op:
            raise RuntimeError(f"{op}: unavailable")

    def wake_already_claimed(self, conn, *, profile, task_id, event_ids, **kw):
        self._check("read")
        self.calls.append(("read", profile, task_id, tuple(event_ids)))
        return {e for e in event_ids if (task_id, e) in self.claimed}

    def try_claim_wake(self, conn, *, profile, task_id, event_id, emitter, **kw):
        self._check("claim")
        self.calls.append(("claim", profile, task_id, event_id, emitter))
        return (task_id, event_id) in self.owed

    def confirm_wake(self, conn, *, profile, task_id, event_id, emitter, **kw):
        self._check("confirm")
        self.calls.append(("confirm", profile, task_id, event_id, emitter))
        return True

    def release_wake(self, conn, *, profile, task_id, event_id, emitter, **kw):
        self._check("release")
        self.calls.append(("release", profile, task_id, event_id, emitter))
        return True


class SpyConnect:
    """Board-connection seam: records the URI each operation opens."""

    def __init__(self):
        self.uris = []

    def __call__(self, db_path, *, write):
        self.uris.append((write, str(db_path)))
        return sqlite3.connect(str(db_path))


@pytest.fixture
def claim_db(tmp_path):
    """Board path for the adapter tests: absolute, per-test, never in the repo.

    These four tests inject :class:`SpyConnect`, which hands the path straight
    to ``sqlite3.connect`` — so the bare relative literal this fixture replaces
    (``"db.sqlite"``) resolved against the pytest CWD and minted a 0-byte
    ``db.sqlite`` in the repo root on every run. Invisible to ``git status``
    since ``*.sqlite`` is ignored, and shared (so colliding) between two
    workers on one checkout. The real :func:`orch_waker._claim_connect`
    absolutises anyway (``Path(...).absolute().as_uri()``), so an absolute path
    exercises the same code and no assertion here changes.
    """
    return str(tmp_path / "claims.sqlite")


def test_claim_connect_writes_for_real_and_never_creates_a_board(tmp_path):
    """`write=True` must be a real write — and must never create a board.

    The gap this test closes (found by mutation probe, t_2cd692f0): every other
    adapter test injects a `connect` seam, so nothing pinned what
    :func:`_claim_connect` does with the flag. Flip ``mode=rw`` to ``mode=ro``
    and every claim fails open — the waker keeps delivering (safe) and the
    cross-emitter dedupe quietly stops existing, which is this module's
    documented silent-inert failure class. So both halves are asserted
    behaviourally, not by string-matching a URI.
    """
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    row = ("hscc-orch", TASK, 7, ow.WAKE_CLAIM_EMITTER, NOW, None)

    ro = ow._claim_connect(db, write=False)
    try:
        with pytest.raises(sqlite3.Error):
            ro.execute("INSERT INTO kanban_wake_claims VALUES (?, ?, ?, ?, ?, ?)",
                       row)
    finally:
        ro.close()

    rw = ow._claim_connect(db, write=True)
    try:
        rw.execute("INSERT INTO kanban_wake_claims VALUES (?, ?, ?, ?, ?, ?)", row)
        rw.commit()
    finally:
        rw.close()
    assert FakeClaims(db, now=NOW).rows()[0]["emitter"] == ow.WAKE_CLAIM_EMITTER

    missing = tmp_path / "absent" / "kanban.db"
    with pytest.raises(sqlite3.Error):
        ow._claim_connect(missing, write=True)
    assert not missing.exists(), "a stale enumeration entry must not mint a board"


def test_adapter_passes_hermes_its_own_key_emitter_and_never_filters_kinds(claim_db):
    """The adapter is plumbing: same (profile, task, event_id), same emitter.

    The kind filter lives in the waker's decision (``_claim_held_out``), where
    :func:`gateway_already_delivered` keeps its own — so the table sees only
    wake-bearing events and the adapter stays free of policy.
    """
    lib = RecordingLib(owed={(TASK, 7)})
    conn = SpyConnect()
    adapter = ow.BoardWakeClaim(lib=lib, connect=conn)
    idents = [[TASK, "completed", NOW, 7]]

    adapter.suppressions(claim_db, ORCH, idents)
    assert adapter.take(claim_db, ORCH, idents) == {(TASK, 7)}
    adapter.confirm(claim_db, ORCH, idents)
    adapter.release(claim_db, ORCH, idents)

    assert ("claim", ORCH, TASK, 7, ow.WAKE_CLAIM_EMITTER) in lib.calls
    assert ("confirm", ORCH, TASK, 7, ow.WAKE_CLAIM_EMITTER) in lib.calls
    assert ("release", ORCH, TASK, 7, ow.WAKE_CLAIM_EMITTER) in lib.calls
    assert [w for w, _p in conn.uris] == [False, True, True, True], \
        "reads open the board read-only; only claim writes open it read-write"


def test_adapter_reads_through_hermes_and_returns_their_answer(claim_db):
    lib = RecordingLib(claimed={(TASK, 7)})
    adapter = ow.BoardWakeClaim(lib=lib, connect=SpyConnect())
    out = adapter.suppressions(claim_db, ORCH,
                               [[TASK, "completed", NOW, 7],
                                [TASK, "blocked", NOW, 8]])
    assert out == {(TASK, 7)}


@pytest.mark.parametrize("op", ["read", "claim", "confirm", "release"])
def test_adapter_fails_open_when_hermes_raises(op, claim_db):
    """Not just sqlite3.Error.

    ``write_txn`` refuses a delegated-child context with PermissionError, which
    is not a sqlite error and would otherwise escape into the tick and kill a
    delivery that was owed.
    """
    lib = RecordingLib(raise_on=op)
    adapter = ow.BoardWakeClaim(lib=lib, connect=SpyConnect())
    idents = [[TASK, "completed", NOW, 7]]
    if op == "read":
        assert adapter.suppressions(claim_db, ORCH, idents) == set()
    elif op == "claim":
        assert adapter.take(claim_db, ORCH, idents) == {(TASK, 7)}, \
            "'cannot claim' means 'you owe it' — deliver, never suppress"
    else:
        getattr(adapter, op)(claim_db, ORCH, idents)   # must not raise


def test_adapter_without_hermes_helpers_owes_everything(claim_db):
    """A Hermes that predates §3.1 has no table to consult: deliver."""
    conn = SpyConnect()
    adapter = ow.BoardWakeClaim(lib=None, connect=conn)
    idents = [[TASK, "completed", NOW, 7], ["t_b", "blocked", NOW, 8]]

    assert adapter.suppressions(claim_db, ORCH, idents) == set()
    assert adapter.take(claim_db, ORCH, idents) == {(TASK, 7), ("t_b", 8)}
    adapter.confirm(claim_db, ORCH, idents)
    adapter.release(claim_db, ORCH, idents)
    assert conn.uris == [], "no helpers means no board is opened at all"


class _PartialNotify:
    """A §3.1 half-installed: one helper present, three missing."""
    try_claim_wake = staticmethod(lambda *a, **k: True)


class _FullNotify:
    """What hermes_cli.kanban_db_notify must expose for the waker to use it."""
    try_claim_wake = staticmethod(lambda *a, **k: True)
    confirm_wake = staticmethod(lambda *a, **k: True)
    release_wake = staticmethod(lambda *a, **k: True)
    wake_already_claimed = staticmethod(lambda *a, **k: set())


def test_load_wake_claim_lib_requires_all_four_helpers():
    """Half-installed §3.1 is "no knowledge", never a partial claim path.

    A waker that wrote claims its own Hermes could not read (or could read but
    never settle) would mute the other emitter for a wake nobody can finish —
    so the gate is all four callables or none.
    """
    assert ow._load_wake_claim_lib(notify=_PartialNotify) is None
    assert ow._load_wake_claim_lib(notify=_FullNotify) is _FullNotify


def test_load_wake_claim_lib_returns_none_without_hermes(monkeypatch):
    """The hermes-import seam says "cannot reach Hermes" -> no claim layer.

    No sys.modules surgery here: the seam is the loader's own, so this test
    cannot poison the real ``hermes_cli`` for a sibling test in the same
    process (t_2be3b04e is exactly that collision).
    """
    from hscc_daemon import autodown
    monkeypatch.setattr(autodown, "_load_kanban_db_or_default", lambda: None)
    assert ow._load_wake_claim_lib() is None


def test_default_adapter_is_used_when_the_seam_is_omitted(tmp_path, monkeypatch):
    """Existing callers pass no seam, so the default must be the board adapter."""
    seen = {}

    class Default(ow.BoardWakeClaim):
        def __init__(self, *a, **kw):
            seen["made"] = True
            super().__init__(lib=None, *a, **kw)

    monkeypatch.setattr(ow, "BoardWakeClaim", Default)
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    state = tmp_path / "state.json"
    seen_once(db, state)
    add_events(db, [event()])
    collect(db, state)

    decisions = ow.tick(now=DELIVER, boards={BOARD: str(db)},
                        state_path=state, has_orch=lambda b: ORCH,
                        inject=StubInject(ok=True))

    assert [d["ok"] for d in decisions] == [True]
    assert seen.get("made") is True


# --------------------------------------------------------------------------- #
# the waker's OWN opener against hermes' helpers: rows must be NAME-indexable
# --------------------------------------------------------------------------- #
#
# Why a section of its own: every adapter test above injects BOTH ``lib=`` and
# ``connect=SpyConnect()``, and the one test that uses the real
# :func:`orch_waker._claim_connect` only does raw INSERT/read-back with its own
# row factory. So NOTHING in this file ever put a name-indexed read (hermes'
# ``row["confirmed_at"]``, ``int(r["event_id"])``) on a row built by the
# connection the product actually opens. The defect that gap kept alive
# (CR-1, review t_7c5d2669): ``_claim_connect`` returned a bare connection,
# every wake-claim READ raised ``TypeError: tuple indices must be integers or
# slices, not str``, ``TypeError`` is not a ``sqlite3.Error`` so it escaped
# hermes' fail-open and landed in ``BoardWakeClaim._run``'s handler — which
# logged "failing open towards delivering" and answered "nothing claimed,
# everything owed". Safe direction (no wake dropped), and the cross-emitter
# dedupe inert in exactly the case it exists for, with the whole suite green.

class UpstreamClaimHelpers:
    """hermes' §3.1 claim helpers: its SQL, its row access, its semantics.

    Not a spy and not a yes-man — each method runs the statement hermes runs,
    through the connection it is handed, and indexes the resulting row BY NAME
    exactly as ``hermes_cli/kanban_db_notify.py`` does at §3.1 head
    ``1f3da154f1`` (``wake_claim_holder`` per the gateway head ``926650a1fc``):
    ``row["confirmed_at"]`` in ``_wake_claim_row``, ``int(r["event_id"])`` in
    ``wake_already_claimed``, ``str(row["emitter"])`` in ``wake_claim_holder``.
    That name-indexing is the whole point of this twin: it is the property the
    waker's opener has to satisfy, and a stub that ignores the row would let
    CR-1 back in.

    Only ``_kb.write_txn``'s transaction wrapper is simplified to a plain
    commit — hermes' transaction bookkeeping (and its fail-open verdicts) are
    hers and are tested in hermes'. The real module replaces this one wherever
    the deployed Hermes actually has §3.1: see
    :func:`test_deployed_hermes_helpers_read_through_the_real_connector`.

    ``now`` pins the lease clock, because the adapter calls these without one
    (as production does) and the tick's clock is what the lease must be read
    against.
    """

    LEASE_S = 120                       # hermes' _WAKE_CLAIM_LEASE_S

    def __init__(self, *, now=None):
        self.now = now
        self.calls = []                 # [(op, profile, task_id, ...)]

    def _moment(self, now):
        return int(now if now is not None else (self.now or time.time()))

    # -- hermes' read side: every row is indexed by column NAME -------------
    def wake_already_claimed(self, conn, *, profile, task_id, event_ids,
                             now=None, lease_s=None):
        self.calls.append(("read", profile, task_id, tuple(event_ids)))
        ids = sorted({int(e) for e in event_ids})
        if not ids:
            return set()
        cutoff = self._moment(now) - int(lease_s or self.LEASE_S)
        rows = conn.execute(
            "SELECT event_id FROM kanban_wake_claims"
            " WHERE profile = ? AND task_id = ?"
            f" AND event_id IN ({','.join('?' * len(ids))})"
            " AND (confirmed_at IS NOT NULL OR claimed_at > ?)",
            (profile, task_id, *ids, cutoff)).fetchall()
        return {int(r["event_id"]) for r in rows}

    def wake_claim_holder(self, conn, *, profile, task_id, event_id):
        row = conn.execute(
            "SELECT emitter FROM kanban_wake_claims"
            " WHERE profile = ? AND task_id = ? AND event_id = ?",
            (profile, task_id, int(event_id))).fetchone()
        return str(row["emitter"]) if row is not None else None

    def _row(self, conn, *, profile, task_id, event_id):
        return conn.execute(
            "SELECT emitter, claimed_at, confirmed_at FROM kanban_wake_claims"
            " WHERE profile = ? AND task_id = ? AND event_id = ?",
            (profile, task_id, int(event_id))).fetchone()

    # -- hermes' write side: same CAS, same holder-only rules --------------
    def try_claim_wake(self, conn, *, profile, task_id, event_id, emitter,
                       now=None, lease_s=None):
        self.calls.append(("claim", profile, task_id, int(event_id), emitter))
        moment = self._moment(now)
        lease = int(lease_s or self.LEASE_S)
        row = self._row(conn, profile=profile, task_id=task_id, event_id=event_id)
        if row is None:
            conn.execute(
                "INSERT INTO kanban_wake_claims"
                " (profile, task_id, event_id, emitter, claimed_at, confirmed_at)"
                " VALUES (?, ?, ?, ?, ?, NULL)",
                (profile, task_id, int(event_id), emitter, moment))
            conn.commit()
            return True
        if row["confirmed_at"] is not None:
            return False
        if moment - int(row["claimed_at"]) < lease:
            return False
        taken = conn.execute(
            "UPDATE kanban_wake_claims"
            " SET emitter = ?, claimed_at = ?, confirmed_at = NULL"
            " WHERE profile = ? AND task_id = ? AND event_id = ?"
            " AND confirmed_at IS NULL AND claimed_at <= ?",
            (emitter, moment, profile, task_id, int(event_id), moment - lease))
        conn.commit()
        return taken.rowcount > 0

    def confirm_wake(self, conn, *, profile, task_id, event_id, emitter,
                     now=None):
        self.calls.append(("confirm", profile, task_id, int(event_id), emitter))
        cur = conn.execute(
            "UPDATE kanban_wake_claims SET confirmed_at = ?"
            " WHERE profile = ? AND task_id = ? AND event_id = ?"
            " AND emitter = ? AND confirmed_at IS NULL",
            (self._moment(now), profile, task_id, int(event_id), emitter))
        conn.commit()
        return cur.rowcount > 0

    def release_wake(self, conn, *, profile, task_id, event_id, emitter):
        self.calls.append(("release", profile, task_id, int(event_id), emitter))
        cur = conn.execute(
            "DELETE FROM kanban_wake_claims"
            " WHERE profile = ? AND task_id = ? AND event_id = ? AND emitter = ?",
            (profile, task_id, int(event_id), emitter))
        conn.commit()
        return cur.rowcount > 0


def collect_one_event(tmp_path):
    """``(db, state, ev_id)`` — one wake-bearing event, already in ``pending``.

    The tail-start and collect ticks come first, because a fresh event can
    never be spoken on its own tick (``debounce_s``): this is the state a real
    tick is in when it reaches the delivery decision. Whether the gateway holds
    that event's claim is left to the caller — the mirror tests need both.
    """
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    state = tmp_path / "state.json"
    seen_once(db, state)
    add_events(db, [event()])
    return db, state, ids_of(db)[0]


def real_connector_adapter(now=DELIVER, lib=None, **kw):
    """The adapter as the product builds it: real helpers, real opener.

    No ``connect=`` seam — passing one is what hid CR-1 — so every row these
    assertions rest on came out of the shipped :func:`orch_waker._claim_connect`.
    ``lib`` is passed in by the one test that also asks the helpers a direct
    question, so it holds the same object the adapter holds.
    """
    return ow.BoardWakeClaim(lib=lib or UpstreamClaimHelpers(now=now),
                             connect=ow._claim_connect, **kw)


def test_claim_connect_rows_are_name_indexed_like_hermes_opener(tmp_path):
    """A row off our opener must answer to a column NAME.

    The mutation this kills: delete ``conn.row_factory = sqlite3.Row`` from
    ``_claim_connect`` and both modes go red with ``TypeError`` — the exact
    failure CR-1 measured, at the one line that fixes it. Asserted on the
    write connection too because ``try_claim_wake`` reads its row INSIDE the
    write txn, so a read-only-mode-only fix would still leave claims broken.
    """
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    FakeClaims(db).seed(ORCH, TASK, 7, emitter=GATEWAY_EMITTER,
                        claimed_at=NOW - 30, confirmed_at=NOW - 20)

    for write in (False, True):
        conn = ow._claim_connect(db, write=write)
        try:
            row = conn.execute(
                "SELECT event_id, emitter, claimed_at, confirmed_at"
                " FROM kanban_wake_claims WHERE profile = ? AND task_id = ?",
                (ORCH, TASK)).fetchone()
            assert isinstance(row, sqlite3.Row), \
                f"write={write}: hermes' own opener hands out Rows; ours must too"
            assert int(row["event_id"]) == 7
            assert row["emitter"] == GATEWAY_EMITTER
            assert row["claimed_at"] == NOW - 30
            assert row["confirmed_at"] == NOW - 20
        finally:
            conn.close()


def test_upstream_claim_helpers_read_through_the_real_connector(tmp_path, caplog):
    """hermes' read + claim answer correctly over OUR connection.

    The four numbers CR-1 measured wrong, on a confirmed claim held by the
    other emitter (the only situation the second emitter exists to ask about):
    read says claimed, the claim is refused, the holder is named — and the
    notifier's row is still there, untouched, because we were refused it.
    """
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    FakeClaims(db).seed(ORCH, TASK, 7, emitter=GATEWAY_EMITTER,
                        claimed_at=NOW - 30, confirmed_at=NOW - 20)
    helpers = UpstreamClaimHelpers(now=DELIVER)
    adapter = real_connector_adapter(lib=helpers)
    idents = [[TASK, "completed", NOW, 7]]

    with caplog.at_level("WARNING"):
        assert adapter.suppressions(db, ORCH, idents) == {(TASK, 7)}, \
            "a confirmed delivery by the notifier suppresses our wake"
        assert adapter.take(db, ORCH, idents) == set(), \
            "we were refused the claim, so we owe nothing"

    conn = ow._claim_connect(db, write=False)
    try:
        assert helpers.wake_claim_holder(
            conn, profile=ORCH, task_id=TASK, event_id=7) == GATEWAY_EMITTER, \
            "the read reports WHO holds it, not merely that somebody does"
    finally:
        conn.close()

    mine = [r for r in FakeClaims(db).rows() if r["emitter"] == ow.WAKE_CLAIM_EMITTER]
    assert mine == [], "a refused claim must not be taken over by us"
    assert not any("failing open" in r.getMessage() for r in caplog.records), \
        "a read that worked must not be logged as a read that could not happen"


def test_upstream_claim_helpers_write_through_the_real_connector(tmp_path):
    """The same connection must also be able to CLAIM — not just read.

    The mirror half: with no claim row the helpers take it for us and confirm
    settles it. Without this the assertions above could be satisfied by a
    connection that always fails (fail-open answers "claimed: nothing, owed:
    everything" too), which is precisely the shape CR-1 hid behind.
    """
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    adapter = real_connector_adapter()
    idents = [[TASK, "completed", NOW, 7]]

    assert adapter.suppressions(db, ORCH, idents) == set()
    assert adapter.take(db, ORCH, idents) == {(TASK, 7)}
    rows = FakeClaims(db).rows()
    assert [(r["emitter"], r["confirmed_at"]) for r in rows] == \
        [(ow.WAKE_CLAIM_EMITTER, None)], "our lease is the row the board now holds"

    adapter.confirm(db, ORCH, idents)
    assert FakeClaims(db).rows()[0]["confirmed_at"] == DELIVER, \
        "the claim write goes through the same opener the read used"


def test_a_gateway_claim_silences_the_wake_through_the_real_connector(tmp_path,
                                                                      caplog):
    """The product-level version of CR-1: the notifier delivered, we stay shut.

    A full ``tick()`` with the adapter the product would build — real helpers,
    real ``_claim_connect``, no seam anywhere — which is the pairing no test
    had. Before the fix this tick delivered a doubled wake AND logged one
    "failing open towards delivering" warning per board.
    """
    db, state, ev_id = collect_one_event(tmp_path)
    FakeClaims(db).seed(ORCH, TASK, ev_id, emitter=GATEWAY_EMITTER,
                        claimed_at=NOW, confirmed_at=NOW + 5)
    inject = StubInject(ok=True)

    with caplog.at_level("WARNING"):
        decisions = deliver_tick(db, state, inject, real_connector_adapter())

    assert decisions == [] and inject.calls == [], \
        "the second emitter already woke this profile for this event"
    assert not any("failing open" in r.getMessage() for r in caplog.records), \
        "CR-1's signature: a dedupe that silently could not see"


def test_no_claim_row_still_wakes_through_the_real_connector(tmp_path):
    """The same real-connector tick, with nobody holding the event: deliver.

    Companion pin so the suppression above cannot be earned by an opener that
    simply never reads anything.
    """
    db, state, _ev_id = collect_one_event(tmp_path)
    inject = StubInject(ok=True)

    decisions = deliver_tick(db, state, inject, real_connector_adapter())

    assert [d["ok"] for d in decisions] == [True]
    assert len(inject.calls) == 1


def test_deployed_hermes_helpers_read_through_the_real_connector(tmp_path):
    """Same four assertions against hermes' ACTUAL §3.1 module, when installed.

    The fleet's deployed Hermes predates §3.1 (no ``try_claim_wake``, no
    ``kanban_wake_claims`` in its schema), which is what
    :func:`orch_waker._load_wake_claim_lib` returns ``None`` for and what the
    waker itself sees — so today this skips LOUDLY instead of quietly passing,
    and the §3.1-shaped twin above is what guards the connector. The moment
    §3.1 installs, this test turns itself on with no fake in the loop.
    """
    lib = ow._load_wake_claim_lib()
    if lib is None:
        pytest.skip("deployed Hermes has no §3.1 wake-claim helpers — "
                    "UpstreamClaimHelpers above is the stand-in; this test "
                    "activates when §3.1 installs")
    db = make_board(tmp_path / "kanban.db", tasks=[(TASK, TITLE)])
    FakeClaims(db).seed(ORCH, TASK, 7, emitter=GATEWAY_EMITTER,
                        claimed_at=NOW - 30, confirmed_at=NOW - 20)
    adapter = ow.BoardWakeClaim(lib=lib)          # default connect: _claim_connect
    idents = [[TASK, "completed", NOW, 7]]

    assert adapter.suppressions(db, ORCH, idents) == {(TASK, 7)}
    assert adapter.take(db, ORCH, idents) == set()
    holder = getattr(lib, "wake_claim_holder", None)
    if callable(holder):
        conn = ow._claim_connect(db, write=False)
        try:
            assert holder(conn, profile=ORCH, task_id=TASK, event_id=7) \
                == GATEWAY_EMITTER
        finally:
            conn.close()
