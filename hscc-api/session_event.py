"""HSCC API — session event frames + event store (the chat wire contract).

This module defines THE wire contract the iOS chat surface and the WebSocket
bridge (t_47f51a71) implement against, and the per-project append-only event
store the history endpoint (t_2776ea3c) reads from. It is pure Python with no
I/O, so it is unit-testable in isolation and pins the JSON shapes the iOS
decoders (t_1ff4dcbd) and the history pager (t_2776ea3c) depend on.

Contract summary (locked here, in code):

* Every event on the wire is an *envelope*::

      {"seq": 42, "type": "message", "ts": "2026-08-29T00:00:00Z",
       "payload": {...}}

  - ``seq`` — monotonically increasing per project. THE reconnect cursor for
    gap-free, duplicate-free resume (t_218cb9ec). History (old seq) and the
    live WebSocket stream (new seq) share ONE sequence space, so a client can
    page history down to ``seq`` and then subscribe from ``seq+1`` with no gap
    and no duplicate. ``seq`` starts at 1 per project and never reuses a value.
    It is NOT a monotonic clock — it is the event ordinal.
  - ``type`` — one of the constants below.
  - ``ts`` — ISO-8601 UTC timestamp the event was produced (server wall clock).
  - ``payload`` — type-specific shape (see the ``*Event`` dataclasses).

* Event types (constants): hello, message, tool_call, card, agent, system, error.

* Sequence continuity: store seq values are contiguous (1..N) — the bridge
  assigns seq == len(events)+1 before append, so reads and live stream both
  agree on "next". A client that has seen up to seq ``s`` asks for history
  ``before=s`` and lives from ``s+1``.

* The store is an in-process, per-project ring buffer (bounded at
  :data:`EVENT_STORE_CAPACITY`). On restart the buffer is empty; a *future*
  increment persists history so a long-lived project keeps history across an
  api-server bounce (out of scope for the first contract commit).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Any, Optional

# --- Event type constants (the ``type`` field on every envelope) ----------- #

# hello — emitted at the start of every live WebSocket stream. Carries the
# project's current high-water seq so a fresh client knows where to continue:
# the very first event a subscribed client must render is ``seq + 1`` (any
# message was already delivered as history).
TYPE_HELLO = "hello"
# message — a streaming text fragment on the project's orchestrator session.
TYPE_MESSAGE = "message"
# tool_call — a tool ran (start) or finished (finish), with args / result.
TYPE_TOOL_CALL = "tool_call"
# card — a kanban card changed state (moved, created, blocked, closed, ...).
TYPE_CARD = "card"
# agent — a subagent/orchestrator spawned or finished.
TYPE_AGENT = "agent"
# system — an ambient session fact the operator decided was worth surfacing
# (cron firing, worker crash, escalation, compaction, session rotated...).
TYPE_SYSTEM = "system"
# error — a named, actionable failure (model unreachable, session missing, ...).
TYPE_ERROR = "error"

EVENT_TYPES = frozenset({
    TYPE_HELLO, TYPE_MESSAGE, TYPE_TOOL_CALL, TYPE_CARD,
    TYPE_AGENT, TYPE_SYSTEM, TYPE_ERROR,
})

# Feature flags / contract version the iOS app can gate on.
CONTRACT_VERSION = 1

# Per-project in-memory event history cap. Keeps memory bounded; a client that
# pages back past this silently hits the oldest retained frame (the bridge may
# restart seq if persistence is later added — see ``persist`` note above).
EVENT_STORE_CAPACITY = 2000


# --------------------------------------------------------------------------- #
# Payload dataclasses (one per event type). Each ``to_json`` returns the exact
# dict the iOS decoder receives. Keep these shapes STABLE — they are the contract.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class HelloPayload:
    """TYPE_HELLO — stream-open handshake."""

    # The next seq the client should expect AFTER this hello; i.e. the
    # high-water mark of stored history. Live frames follow from ``next_seq``.
    next_seq: int

    def to_json(self) -> dict:
        return {"next_seq": self.next_seq}


@dataclass(frozen=True)
class MessagePayload:
    """TYPE_MESSAGE — a token delta on the orchestrator session.

    Streams as an ordered sequence of ``delta`` fragments; the final fragment
    for a given turn carries ``done=True``. ``role`` is "user" (echo of the
    operator's own prompt) or "assistant" (the model's reply).
    """

    role: str
    delta: str
    done: bool = False

    def to_json(self) -> dict:
        return {"role": self.role, "delta": self.delta, "done": self.done}


@dataclass(frozen=True)
class ToolCallPayload:
    """TYPE_TOOL_CALL — a tool invocation started or finished.

    A tool call is TWO frames sharing ``call_id``: one with ``status="start"``
    (name + args; result absent) and one with ``status="finish"`` (name, and
    result + elapsed). The iOS view pairs them by ``call_id`` and renders a
    collapsible chip.
    """

    call_id: str
    name: str
    status: str  # "start" | "finish"
    args: dict = field(default_factory=dict)
    result: Any = None
    duration_s: Optional[float] = None

    def to_json(self) -> dict:
        d: dict = {
            "call_id": self.call_id,
            "name": self.name,
            "status": self.status,
        }
        if self.args:
            d["args"] = self.args
        if self.result is not None:
            d["result"] = self.result
        if self.duration_s is not None:
            d["duration_s"] = self.duration_s
        return d


@dataclass(frozen=True)
class CardPayload:
    """TYPE_CARD — a kanban card changed state (tappable chip)."""

    board: str
    id: str
    title: str
    status: str  # new | ready | running | blocked | done | error | ...

    def to_json(self) -> dict:
        return {
            "board": self.board,
            "id": self.id,
            "title": self.title,
            "status": self.status,
        }


@dataclass(frozen=True)
class AgentPayload:
    """TYPE_AGENT — a subagent/orchestrator spawned or finished."""

    role: str          # profile name, e.g. "researcher-a"
    action: str        # "spawned" | "finished"
    task: str = ""     # short description (optional)

    def to_json(self) -> dict:
        d: dict = {"role": self.role, "action": self.action}
        if self.task:
            d["task"] = self.task
        return d


@dataclass(frozen=True)
class SystemPayload:
    """TYPE_SYSTEM — an ambient session fact worth surfacing.

    ``kind`` is the machine-slug; ``details`` carries kind-specific data.
    Known kinds (extensible): cron, worker_crash, escalation, compaction,
    session_rotated, gateway.
    """

    kind: str
    details: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        d: dict = {"kind": self.kind}
        if self.details:
            d["details"] = self.details
        return d


@dataclass(frozen=True)
class ErrorPayload:
    """TYPE_ERROR — a named, actionable failure.

    ``code`` is a stable machine slug and ``message`` a one-human-sentence
    explanation. The iOS UI maps known ``code`` values to a friendly row and
    always lets the operator act (resend, open settings), never dead-ends.
    """

    code: str
    message: str

    def to_json(self) -> dict:
        return {"code": self.code, "message": self.message}


# Registry: type constant -> payload constructor. The bridge/WebSocket layer
# uses ``EVENT_TYPES`` for validation and these constructors to build frames.
PAYLOAD_TYPES = {
    TYPE_HELLO: HelloPayload,
    TYPE_MESSAGE: MessagePayload,
    TYPE_TOOL_CALL: ToolCallPayload,
    TYPE_CARD: CardPayload,
    TYPE_AGENT: AgentPayload,
    TYPE_SYSTEM: SystemPayload,
    TYPE_ERROR: ErrorPayload,
}


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Event:
    """One complete frame on the wire: envelope + payload."""

    seq: int
    type: str
    payload: Any
    ts: str = ""          # ISO-8601 UTC; filled by the store on append

    def to_json(self) -> dict:
        return {
            "seq": self.seq,
            "type": self.type,
            "ts": self.ts,
            "payload": self.payload.to_json() if hasattr(self.payload, "to_json")
            else self.payload,
        }


def make_event(type_: str, payload: Any, seq: int, ts: Optional[str] = None) -> Event:
    """Build an :class:`Event`, stamping ``ts`` (ISO-8601 UTC) by default.

    ``ts`` is supplied by the caller only for deterministic tests / replay;
    production appends get the wall clock. Raises ValueError on an unknown
    event type so a typo fails loud, not silently.
    """
    if type_ not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {type_!r}")
    if ts is None:
        ts = _now_iso()
    return Event(seq=seq, type=type_, payload=payload, ts=ts)


def _now_iso() -> str:
    """ISO-8601 UTC with second precision, e.g. '2026-08-29T12:00:00Z'."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- #
# Event store (per project). Thread-safe append + seq cursor + history paging.
# --------------------------------------------------------------------------- #

class SessionEventStore:
    """Append-only, bounded, per-project event history.

    Semantics the downstream cards rely on:

    * ``next_seq`` is the high-water mark — the seq the NEXT append will get.
      ``len(events) == next_seq - 1`` while seq is contiguous from 1.
    * ``append`` assigns ``next_seq`` (contiguous, never reused).
    * ``history(before=..., limit=...)`` returns frames in seq ASCENDING order
      strictly with ``seq < before``, newest ``limit`` of that window last.
      The returned dict's ``next_before`` is the cursor for the next older page
      (or ``None`` at the oldest frame).
    * The ring buffer evicts the oldest frames once capacity is exceeded, so
      ``oldest_seq`` reports the first seq still retained (for ``speak`` and
      client backward-scan awareness).
    """

    def __init__(self, capacity: int = EVENT_STORE_CAPACITY):
        self._capacity = capacity
        self._events: list[Event] = []   # seq-ascending, contiguous from oldest_seq
        self._first_seq = 1              # seq of self._events[0]
        self._lock = threading.Lock()
        self._listeners: set = set()     # callables(Event) notified on append

    # -- live-stream subscription ---------------------------------------- #

    def subscribe(self, listener) -> None:
        """Register ``listener(Event)`` to be called on every future append.

        Listeners drive the live WebSocket fan-out: the bridge relay and any
        connected app WS both subscribe here so a single append (from hermes
        serve) reaches every live client exactly once, in seq order. The
        listener must be reentrant / not block on the store lock (callbacks run
        WITHOUT the lock held); a ``queue.put`` is the idiomatic use.
        """
        with self._lock:
            self._listeners.add(listener)

    def unsubscribe(self, listener) -> None:
        """Remove a listener registered with :meth:`subscribe` (idempotent)."""
        with self._lock:
            self._listeners.discard(listener)

    def snapshot_and_subscribe(self, after: int) -> tuple:
        """Atomically subscribe and return ``(boundary, replay_events)``.

        This is the gap-free reconnect primitive. It must be called by a WS
        connection AFTER it decides on its resume cursor ``after`` (the seq it
        has already rendered). It:

        1. subscribes a fresh listener queue first, then
        2. captures ``boundary == store.next_seq``, then
        3. snapshots the stored events with ``after < seq < boundary``.

        Because the listener is registered BEFORE ``boundary`` is read, no
        event appended between the two is dropped: an event that lands there is
        both in the stored snapshot (seq <= boundary) and will arrive on the
        listener queue — the connection dedupes by skipping queue events with
        ``seq <= boundary``. Events appended after ``boundary`` appear ONLY on
        the queue and are streamed live. Net effect: every event with
        ``seq > after`` is delivered exactly once, in order — no gap, no dupe.

        Returns ``(boundary, replay_events, queue)``. ``replay_events`` is
        seq-ascending with ``after < seq < boundary``; callers drain ``queue``
        via ``get_nowait``/``get(timeout=...)``.
        """
        sub_queue: Queue = Queue()
        self.subscribe(sub_queue.put)
        with self._lock:
            boundary = self._first_seq + len(self._events)
            replay = [e for e in self._events if after < e.seq < boundary]
        return boundary, replay, sub_queue


    # -- writes ----------------------------------------------------------- #

    def append(self, type_: str, payload: Any, ts: Optional[str] = None) -> int:
        """Append one event; returns its seq. Thread-safe.

        Fan-outs to subscribed live listeners AFTER releasing the store lock,
        so a slow/reentrant listener never stalls other appends. Each listener
        receives the fully-stamped :class:`Event` (seq + ts already assigned).
        """
        with self._lock:
            seq = self._first_seq + len(self._events)
            ev = make_event(type_, payload, seq, ts=ts)
            self._events.append(ev)
            if len(self._events) > self._capacity:
                # Evict the oldest frame(s); shift the contiguous base.
                over = len(self._events) - self._capacity
                del self._events[:over]
                self._first_seq += over
            listeners = list(self._listeners)

        for listener in listeners:
            try:
                listener(ev)
            except Exception:
                # A misbehaving listener must not kill the append path.
                pass
        return seq

    @property
    def next_seq(self) -> int:
        """Seq the next append will receive (the live stream high-water mark)."""
        with self._lock:
            return self._first_seq + len(self._events)

    @property
    def oldest_seq(self) -> int:
        """First seq still retained in history (== 1 until the ring evicts)."""
        with self._lock:
            return self._first_seq

    # -- reads ------------------------------------------------------------ #

    def history(self, before: Optional[int] = None,
                limit: int = 200) -> dict:
        """Page through stored events (seq ASCENDING).

        ``before`` — return only events with ``seq < before`` (exclusive).
        Omitted/None = newest page (the tail). ``limit``-clamped to
        :data:`EVENT_STORE_CAPACITY`; the caller may pass ``limit=0`` meaning
        "just the cursor metadata (next_seq/oldest_seq), no frames".

        Returns ``{"events": [..], "next_before": int|None,
                    "oldest_seq": int, "next_seq": int}``.
        """
        with self._lock:
            events = list(self._events)
            first = self._first_seq

        seqs = [e.seq for e in events]
        # Effective exclusive upper bound.
        if before is None:
            upper_idx = len(seqs)          # one past the last stored event
            cursor_bound = None
        else:
            # index of the last event with seq < before (Python bisect on the
            # contiguous ascending seq list).
            upper_idx = _lower_bound(seqs, before)
            cursor_bound = before

        start = max(0, upper_idx - limit)
        window = events[start:upper_idx]

        # next_before = the ``before`` value to request the next OLDER page —
        # i.e. the oldest seq returned on this page. Requesting
        # ``before=<oldest of this page>`` returns everything strictly older.
        # ``None`` when this page is empty (e.g. limit=0 metadata-only query)
        # or already reaches the oldest retained frame.
        if window and start > 0:
            next_before = events[start].seq
        else:
            next_before = None

        return {
            "events": [e.to_json() for e in window],
            "next_before": next_before,
            "oldest_seq": first,
            "next_seq": self._first_seq + len(self._events),
        }


def _lower_bound(seqs: list, value: int) -> int:
    """Index of the first seq >= value in an ascending list (bisect)."""
    lo, hi = 0, len(seqs)
    while lo < hi:
        mid = (lo + hi) // 2
        if seqs[mid] < value:
            lo = mid + 1
        else:
            hi = mid
    return lo


# --------------------------------------------------------------------------- #
# Global per-project store registry.
# The bridge relay (WebSocket ws <-> hermes serve) appends here; the history
# endpoint reads here. One store per project so seq is per-project-contiguous.
# --------------------------------------------------------------------------- #

_lock = threading.Lock()
_STORES: dict[str, SessionEventStore] = {}


def get_store(project: str) -> SessionEventStore:
    """Return (creating on first use) the event store for a project."""
    with _lock:
        store = _STORES.get(project)
        if store is None:
            store = SessionEventStore()
            _STORES[project] = store
        return store


def reset_stores() -> None:
    """Drop all stores (test isolation only)."""
    global _STORES
    with _lock:
        _STORES = {}
