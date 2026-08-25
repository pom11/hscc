"""Inbound-message queue + replay for idle autodown wake (§4 / design card
t_a4e700ee).

The problem it solves (reproduced live): after a full autodown teardown, an
inbound Telegram message wakes the cluster but is then LOST. The Hermes
gateway drains the message on arrival, calls a model that is still loading
(~9 min), fails, and errors out — it never retries. The user must re-send.

This module implements the HSCC-side fix: while ``autodown.json`` state is
``down`` or ``waking``, every inbound message observed by the gateway-log probe
(``probe_telegram_activity``) is captured into a durable queue under
``~/.hscc/``, and when the wake completes (autoup confirms readiness) the
queued messages are replayed in arrival order so they are processed instead of
being silently lost.

Capture lives in ``autodown.probe_telegram_activity`` (it already owns the
gateway-log scan + offset); this module owns the durable queue, the once-per-
wake waiting notice, and the replay engine. The physical hand-off of a
replayed message to a model/session is deliberately abstracted behind an
injectable ``deliver_message`` so the engine is testable without any real
Telegram or model, and so the production delivery path is one well-defined seam
an operator can point at the supported interface of their choice.

Queue bounds (§4.4): a queue is capped at ``MAX_QUEUED_MESSAGES`` (100) and
messages older than ``MAX_MESSAGE_AGE_MINUTES`` (180) are not replayed. A
message that is over-cap OR over-age is DROPPED with a clear notice — never
silently executed against a cluster that has moved on hours later.
"""

import datetime
import json
import os

from .state import now_iso
from .daemon_ops import log


# Path to the durable queue file. Overridden in tests via monkeypatch, exactly
# mirroring how AUTODOWN_FILE / WATCHDOG_BLOCK_FILE are overridden.
QUEUE_FILE = os.path.expanduser("~/.hscc/queued_messages.json")

# Max queued messages — cap the queue (documented in docs/design/idle-autodown.md
# §4.4). Oldest messages are dropped with a notice beyond this.
MAX_QUEUED_MESSAGES = 100

# Max age of a queued message, in minutes. A message queued longer than this is
# dropped (with a notice) rather than silently executed against a changed
# cluster. 180m = 3h.
MAX_MESSAGE_AGE_MINUTES = 180

# Substring (prefix of each gateway-log line) that marks a real inbound message
# line as opposed to log noise. Kept in replay.py as well as autodown.py so
# parse_gateway_line stays self-contained and testable independent of autodown.
_INBOUND_PREFIX = "inbound message:"


def _empty_state():
    """A fresh, valid queue-state dict (fail-closed shape)."""
    return {
        "next_seq": 1,
        "notice_sent_this_wake": False,
        "messages": [],
    }


def load_queue(path=None):
    """Load the queue state, failing closed to an empty queue.

    An absent/unreadable/corrupt queue file yields ``_empty_state()`` (nothing
    queued, nothing to replay). Never raises.
    """
    p = path or QUEUE_FILE
    state = _empty_state()
    try:
        with open(p) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return state
    if not isinstance(data, dict):
        return state
    messages = data.get("messages")
    if isinstance(messages, list):
        # Sanitize: only dict entries with the fields we need survive.
        state["messages"] = [
            m for m in messages if isinstance(m, dict)
        ]
    ns = data.get("next_seq")
    if isinstance(ns, int) and ns > 0:
        state["next_seq"] = ns
    state["notice_sent_this_wake"] = bool(data.get("notice_sent_this_wake"))
    return state


def save_queue(state, path=None):
    """Persist ``state`` atomically (tmp + os.replace, like save_config).

    Merges over the empty shape so a partial dict can never write a queue file
    missing keys. Uses a UNIQUE tmp name (pid+seq) so two writers can never
    race each other's tmp file.
    """
    p = path or QUEUE_FILE
    merged = _empty_state()
    merged["next_seq"] = state.get("next_seq", 1)
    merged["notice_sent_this_wake"] = bool(state.get("notice_sent_this_wake"))
    merged["messages"] = list(state.get("messages", []))
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = "%s.tmp.%d" % (p, os.getpid())
    with open(tmp, "w") as f:
        json.dump(merged, f, indent=2, default=str)
    os.replace(tmp, p)


def _now_aware(now=None):
    """Return ``now`` if it's an aware datetime, else the real UTC now.

    Accepts either a datetime or an ISO string (tests often pass ISO).
    """
    if now is None:
        return datetime.datetime.now(datetime.timezone.utc)
    if isinstance(now, datetime.datetime):
        return now if now.tzinfo else now.replace(tzinfo=datetime.timezone.utc)
    if isinstance(now, str):
        try:
            return datetime.datetime.fromisoformat(now)
        except (ValueError, TypeError):
            return datetime.datetime.now(datetime.timezone.utc)
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_iso(ts):
    """Parse an ISO timestamp into an aware datetime, or None."""
    try:
        return datetime.datetime.fromisoformat(ts)
    except (ValueError, TypeError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Parsing the gateway log line into routing metadata
# ---------------------------------------------------------------------------
# The gateway writes (gateway/run.py:_handle_message_with_agent, ~line 15507):
#
#     inbound message: platform=%s user=%s chat=%s msg=%r reply_to_id=%s reply_to_text=%r
#
# e.g.
#     inbound message: platform=telegram user=desac chat=-1003906355027
#       msg='hello' reply_to_id=None reply_to_text=''
#
# ``msg`` and ``reply_to_text`` use Python ``%r`` (repr), so they are quoted
# with single quotes and may contain the WHOLE original text up to 80 chars
# (the gateway truncates the preview to 80 chars — we capture what it prints;
# a longer original is not in the log and cannot be reconstructed from it).

def _extract_field(line, key, start):
    """Return (value, next_start) for one ``key=value`` pair starting at
    ``start`` in ``line``. Handles three shapes:

      * ``key=bareword``   -> the token up to whitespace
      * ``key='quoted'``   -> the single-quoted string (may contain spaces;
                              may contain backslash-escaped quotes)
      * ``key=None``       -> None (a bare ``None`` literal)

    Returns ``(None, None)`` when no ``key=`` occurs at ``start``.
    """
    if not line.startswith(key + "=", start):
        return None, None
    value_start = start + len(key) + 1
    if value_start >= len(line):
        return "", None
    if line[value_start] == "'":
        # Quoted (repr) value.
        i = value_start + 1
        chars = []
        while i < len(line):
            c = line[i]
            if c == "\\":
                # Backslash escape: take the next char literally.
                if i + 1 < len(line):
                    chars.append(line[i + 1])
                    i += 2
                    continue
                i += 1
                continue
            if c == "'":
                return "".join(chars), i + 1
            chars.append(c)
            i += 1
        return "".join(chars), None
    # Bareword value up to whitespace.
    j = value_start
    while j < len(line) and not line[j].isspace():
        j += 1
    token = line[value_start:j]
    return (None if token == "None" else token), j


def parse_gateway_line(line):
    """Parse one gateway-log inbound line into routing metadata.

    Returns a dict with keys ``platform``, ``user``, ``chat_id``,
    ``thread_id``, ``reply_to_id``, ``text``, or None if the line is not an
    inbound-message line (missing the ``inbound message:`` prefix). This is the
    data needed to replay faithfully — same chat / topic routing so the reply
    lands where the user expects.

    Bounds: ``text`` is whatever the gateway printed (truncated to ~80 chars by
    the gateway itself). ``thread_id`` is best-effort: the gateway log line
    does not carry the topic explicitly, so we capture ``reply_to_id`` (which
    the gateway prints) and leave ``thread_id``/``topic`` as None unless a
    ``thread_id=`` field is present — faithful topic routing depends on the
    delivery path being able to resolve the topic from the chat + reply context.
    """
    idx = line.find(_INBOUND_PREFIX)
    if idx < 0:
        return None
    # Everything after the marker is a key=value stream.
    rest = line[idx + len(_INBOUND_PREFIX):]
    fields = {}
    pos = 0
    order = ["platform", "user", "chat", "msg", "reply_to_id", "reply_to_text",
             "thread_id", "topic"]
    while pos < len(rest):
        # Skip whitespace.
        while pos < len(rest) and rest[pos].isspace():
            pos += 1
        if pos >= len(rest):
            break
        # Try each known key at this position.
        matched = False
        for key in order:
            val, nxt = _extract_field(rest, key, pos)
            if nxt is None and val is None and not rest.startswith(key + "=", pos):
                continue
            if nxt is None and val is None:
                # "key=" with empty value — a degenerate marker; treat as empty.
                fields[key] = ""
                pos = len(rest)
                matched = True
                break
            fields[key] = val
            pos = nxt if nxt is not None else pos + len(key) + 1
            matched = True
            break
        if not matched:
            # Unknown field — skip to the next whitespace so we don't loop.
            while pos < len(rest) and not rest[pos].isspace():
                pos += 1
    return {
        "platform": fields.get("platform"),
        "user": fields.get("user"),
        "chat_id": fields.get("chat"),
        "thread_id": fields.get("thread_id"),
        "topic": fields.get("topic"),
        "reply_to_id": fields.get("reply_to_id"),
        "text": fields.get("msg"),
    }


def _is_replayable(msg, now):
    """True when a queued message is still within the age bound.

    A message with no arrival timestamp is treated as replayable (fail-open on
    the age check so a legacy/corrupt entry is not forever dropped; the cap
    still bounds the queue).
    """
    arrived = msg.get("arrived_iso")
    if not arrived:
        return True
    dt = _parse_iso(arrived)
    if dt is None:
        return True
    age_min = (now - dt).total_seconds() / 60.0
    return age_min <= MAX_MESSAGE_AGE_MINUTES


# ---------------------------------------------------------------------------
# Queueing entry point (called by autodown.probe_telegram_activity)
# ---------------------------------------------------------------------------

def enqueue_inbound(message, path=None, now=None, notify_fn=None):
    """Durably enqueue one inbound message, preserving arrival order.

    ``message`` is a dict of routing metadata (see ``parse_gateway_line``).
    A ``seq`` (monotonic) and ``arrived_iso`` are stamped here if absent, and
    the message is appended to the queue in arrival order. Returns a dict:

      ``queued``   -> True if the message was added
      ``dropped``  -> True if it was dropped (over-cap)
      ``drop_reason`` -> why it was dropped
      ``noticed``  -> True if the once-per-wake waiting notice was sent now
      ``queue_len``-> the queue length afterwards

    Bound (cap): beyond ``MAX_QUEUED_MESSAGES`` the OLDEST message is dropped
    with a clear notice, keeping the queue bounded and the newest messages.

    Once-per-wake notice: the FIRST message enqueued in a wake (when
    ``notice_sent_this_wake`` is still False) triggers the waiting notice via
    ``notify_fn`` and sets the flag, so it fires once per wake, not per message
    and not per tick. The flag is reset when the queue is successfully replayed
    and emptied (see ``replay_queued``), readying the next wake.

    Never raises on the notify path (a broken notifier must not lose an
    inbound message). Returns a result dict.
    """
    state = load_queue(path)
    now_dt = _now_aware(now)

    msg = dict(message)
    if msg.get("seq") is None:
        msg["seq"] = state["next_seq"]
        state["next_seq"] += 1
    if not msg.get("arrived_iso"):
        msg["arrived_iso"] = now_dt.isoformat()

    result = {"queued": True, "dropped": False, "drop_reason": None,
              "noticed": False, "queue_len": 0}

    # Cap: drop the OLDEST when over the cap.
    if len(state["messages"]) >= MAX_QUEUED_MESSAGES:
        old = state["messages"].pop(0)
        result["dropped"] = True
        result["drop_reason"] = (
            f"queue over capacity ({MAX_QUEUED_MESSAGES}); dropped oldest "
            f"message seq={old.get('seq')}"
        )
        try:
            log(result["drop_reason"], "WARN")
        except Exception:
            pass

    state["messages"].append(msg)
    result["queue_len"] = len(state["messages"])
    save_queue(state, path)

    # Once-per-wake waiting notice — only on the first message of a wake.
    if not state["notice_sent_this_wake"]:
        try:
            if notify_fn is not None:
                notify_fn()
            result["noticed"] = True
            state["notice_sent_this_wake"] = True
            save_queue(state, path)
        except Exception as e:
            # A broken notifier must never lose the queued message — log + go on.
            try:
                log(f"Autodown waking notice failed: {e}", "WARN")
            except Exception:
                pass
    return result


# ---------------------------------------------------------------------------
# Replay engine (called by autodown.autoup after readiness is confirmed)
# ---------------------------------------------------------------------------

def _is_replayable_wrapper(msg, now):
    return _is_replayable(msg, now)


def replay_queued(deliver_message=None, path=None, now=None):
    """Replay queued messages in arrival order; empty the queue on success.

    Called AFTER ``autoup()`` confirms readiness (the point where the watchdog
    block is cleared). Delivers each queued message via ``deliver_message``
    (an injectable ``callable(msg_dict) -> bool``; True = successfully handed
    off). Guarantees:

    * **Ordered** — messages are delivered strictly in ``next_seq`` order
      (arrival order).
    * **Idempotent / no double-send** — a message is removed from the durable
      queue ONLY after ``deliver_message`` returns True. The queue file is
      persisted after each successful dequeue, so a crash between replaying one
      message and the next cannot re-send an already-handed-off message: the
      handoff is durable before the dequeue, and the dequeue is durable before
      the next handoff. The only residual double-send window is a crash
      precisely between the external handoff returning success and the local
      atomic file write landing — at-least-once delivery, the industry-standard
      guarantee for a non-idempotent remote (documented in §4.4).
    * **Bounded** — over-age messages are dropped with a clear notice rather
      than silently executed against a changed cluster.
    * **Failure-safe** — if a delivery fails (returns False or raises), the
      message STAYS in the queue and a loud error is logged/notified; we stop
      and keep the queue rather than silently discarding the user's message.

    ``now`` injectable for deterministic age tests (datetime or ISO string).

    Returns a dict: ``{delivered, failed, dropped_aged, empty}`` where
    ``empty`` is True when the queue started empty (normal no-op on a wake with
    nothing queued).
    """
    state = load_queue(path)
    messages = state["messages"]
    if not messages:
        return {"delivered": 0, "failed": 0, "dropped_aged": 0, "empty": True}
    if deliver_message is None:
        deliver_message = default_deliver_message

    now_dt = _now_aware(now)
    delivered = 0
    failed = 0
    dropped_aged = 0
    remaining = list(messages)

    for msg in messages:
        # Bound (age) first: an over-age message is dropped with a notice, never
        # silently executed against a cluster that has moved on.
        if not _is_replayable(msg, now_dt):
            dropped_aged += 1
            remaining = [
                m for m in remaining if m.get("seq") != msg.get("seq")
            ]
            state["messages"] = remaining
            try:
                log(f"Autodown replay: DROPPING over-age message seq={msg.get('seq')} "
                    f"(queued {msg.get('arrived_iso')}, older than "
                    f"{MAX_MESSAGE_AGE_MINUTES}m) — not executed", "WARN")
            except Exception:
                pass
            continue
        # Deliver. On success the message is removed from the durable queue.
        ok = False
        try:
            if deliver_message is None:
                raise RuntimeError("replay_queued: no deliver_message callable — "
                                   "cannot hand the message to a model/session")
            ok = bool(deliver_message(msg))
        except Exception as e:
            ok = False
            try:
                log(f"Autodown replay: delivery raised for seq={msg.get('seq')}: "
                    f"{e}", "ERROR")
            except Exception:
                pass
        if ok:
            delivered += 1
            # Dequeue: remove this message from the durable set BEFORE the next
            # message's handoff. Handoff-before-dequeue is what makes retry
            # idempotent — the message is only persisted as removed AFTER its
            # delivery was confirmed, so a crash cannot re-send an already
            # handed-off message.
            remaining = [
                m for m in remaining if m.get("seq") != msg.get("seq")
            ]
            state["messages"] = remaining
            _persist_after_handoff(state, path, msg)
        else:
            failed += 1
            # FAILURE-SAFE: keep the message queued. Log loudly and stop — if we
            # continued, a cluster-side problem would silently eat every message.
            try:
                log(f"Autodown replay: delivery FAILED for seq={msg.get('seq')} — "
                    f"message RETAINED in queue, NOT replayed", "ERROR")
            except Exception:
                pass
            break

    if delivered > 0 or dropped_aged > 0:
        # Re-save to reflect the final durable state (remaining messages +
        # reset notice flag if the queue is now empty).
        _finalize(state, path)

    return {"delivered": delivered, "failed": failed,
            "dropped_aged": dropped_aged, "empty": False}


def _persist_after_handoff(state, path, handed_off_msg):
    """Persist the queue with ``handed_off_msg`` removed (durable dequeue).

    Called AFTER a successful handoff so a crash before this write keeps the
    message queued (correct: it may not have been handed off) and a crash after
    this write has the message durably removed (correct: no re-send). Remaining
    messages are persisted so ordering survives a restart.
    """
    state["messages"] = [
        m for m in state.get("messages", []) if m.get("seq") != handed_off_msg.get("seq")
    ]
    save_queue(state, path)


def _finalize(state, path):
    """Persist final queue state after a replay pass.

    ``state["messages"]`` already holds the remaining (never successfully
    handed-off) messages — ``_persist_after_handoff`` saved the removal of each
    delivered message. Here we only reset ``notice_sent_this_wake`` when the
    queue is now empty (so the NEXT wake sends its own once-per-wake notice)
    and save the final state.
    """
    if not state["messages"]:
        state["notice_sent_this_wake"] = False
    save_queue(state, path)


# ---------------------------------------------------------------------------
# Production delivery seam
# ---------------------------------------------------------------------------
# The replay engine hands each queued message to a model/session via an
# injectable ``deliver_message`` callable so it stays testable without any real
# Telegram or model. In production the default is ``default_deliver_message``,
# which delivers the message through components that ACTUALLY exist on this
# host:
#
#   1. Map the message's chat/topic to an orchestrator profile+session (the
#      ``<project>-orch`` / session ``<project>`` convention from
#      hscc-roles/orchestrators.py, catch-all ``general-orch`` / ``general``),
#      resolved via the flightdeck registry's per-project ``topic`` binding.
#   2. Run the message through that orchestrator exactly the way the HSCC API
#      does (routes_orchestrator.py:_backing_invoke): shell Hermes headlessly as
#      the orchestrator profile in its NAMED session, quiet mode so the reply
#      is the only thing on stdout:
#
#          hermes -p <profile> chat -Q --continue <session> -q <text>
#
#   3. Post the resulting reply back to the message's ORIGINAL chat/topic via
#      the telegram adapter path HSCC already uses for operator notices
#      (telegram.send_message / notify_operations — Bot API, CPU-side, proven
#      working).
#
# Mapping policy (deliberate, documented in docs/design/idle-autodown.md §4.4):
#   * platform != "telegram"            -> UNMAPPABLE (delivery FAILURE). The
#     installed reply path is telegram-only, so a non-telegram message has no
#     chat/topic we can route a reply back to. It stays queued + reported
#     loudly — never guessed.
#   * telegram + thread/topic id equals a registry project's ``topic``
#     -> that project's orchestrator (<project>-orch / <project>).
#   * telegram + no project topic matches (General topic, direct chat, or an
#     unbound topic) -> the catch-all general-orch / general. This is NOT a
#     silent guess: the ``general`` orchestrator exists precisely to own
#     messages with no specific project.
#
# Like the old webhook seam, this function fails LOUDLY (returns False) on any
# failure so the message stays queued — never silently discarded. It never
# raises so the replay engine's own try/except stays clean.
#
# ``default_deliver_message`` returns True only on the FULL round trip
# (orchestrator produced a non-empty reply AND the reply was posted back to the
# original chat/topic). If the orchestrator ran but the reply could not be
# delivered, we still return False (the user's message was not faithfully
# answered in their chat) — the message is retained and the retained-
# replay is at-least-once, consistent with the documented contract. A
# pathological orchestrator-ran-reply-failed case logs VERY loudly so the
# operator sees the double-process risk is theirs to clear.

# Registry path mirrors hscc-roles/orchestrators.py (REGISTRY_PATH resolves
# ``~/.flightdeck/registry.yaml``, overridable via HSCC_REGISTRY) — the source
# of truth for which project owns which Telegram topic. Overridable so tests
# can point at a fixture without touching the real registry.
REGISTRY_PATH = os.environ.get("HSCC_REGISTRY", "~/.flightdeck/registry.yaml")

# Catch-all orchestrator identity (mirrors hscc-roles/orchestrators.py).
GENERAL_PROFILE = "general-orch"
GENERAL_SESSION = "general"

# How long to wait for the orchestrator's reply before treating delivery as
# failed (matches routes_orchestrator.py::_DEFAULT_TIMEOUT — an orchestrator
# can legitimately take a while).
ORCHESTRATOR_TIMEOUT = 180.0

# Path to the ``hermes`` executable. ``shutil.which`` would resolve it, but an
# explicit, overridable constant keeps the subprocess argv deterministic for
# tests and lets an operator point at a specific install if ``hermes`` is not
# on PATH.
HERMES_BIN = os.environ.get("HSCC_HERMES_BIN", "hermes")


def _registry_projects(path=None):
    """Read the flightdeck registry's ``projects:`` list, fail-closed.

    Returns a list of dicts ``{"name", "topic"}`` for projects that have a
    bound ``topic``. A missing/unreadable registry or a missing ``topic`` leads
    to that project being absent from the result — never an error, so an
    unmappable message degrades to the catch-all (or, for a non-telegram
    message, a hard delivery failure) instead of crashing.

    Reads the SAME registry file the hscc-roles resolver uses
    (``~/.flightdeck/registry.yaml``), matching its ``HSCC_REGISTRY`` env
    override, so the daemon never drifts from the operator's project source of
    truth. Uses ``yaml`` lazily (already an optional import elsewhere in this
    package, e.g. verify.py:15, health.py:307) — no new dependency.
    """
    p = os.path.expanduser(path or REGISTRY_PATH)
    out = []
    try:
        import yaml
        with open(p) as f:
            data = yaml.safe_load(f) or {}
    except (FileNotFoundError, OSError, ValueError):
        return out
    except Exception:
        # Any parse oddity — fail closed to no projects (catch-all / failure).
        return out
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, list):
        return out
    for row in projects:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        topic = row.get("topic")
        if name and topic is not None:
            out.append({"name": str(name).strip(), "topic": str(topic).strip()})
    return out


def _resolve_identity(msg):
    """Map a queued message to an orchestrator identity, or None if unmappable.

    Returns a dict ``{"profile", "session", "project"}`` (``project`` is None
    for the catch-all) or None when the message cannot be mapped at all — a
    delivery FAILURE, never a silent guess. See the mapping policy above.
    """
    if (msg.get("platform") or "").lower() != "telegram":
        # Installed reply path is telegram-only: a non-telegram message has no
        # chat/topic to route a reply back to.
        return None
    thread = str(msg.get("thread_id") or msg.get("topic") or "").strip()
    if thread:
        for row in _registry_projects():
            if row["topic"] == thread:
                return {
                    "profile": f"{row['name']}-orch",
                    "session": row["name"],
                    "project": row["name"],
                }
    # No project topic matched (General topic / direct chat / unbound topic):
    # deliberate catch-all, not a guess.
    return {
        "profile": GENERAL_PROFILE,
        "session": GENERAL_SESSION,
        "project": None,
    }


def _invoke_orchestrator(profile, session, prompt, timeout=ORCHESTRATOR_TIMEOUT):
    """Run a prompt through an orchestrator and return its reply text.

    Mirrors routes_orchestrator.py::_backing_invoke: shell Hermes headlessly
    as the orchestrator profile in its NAMED session, quiet mode so stdout is
    only the reply. argv is a LIST — ``prompt`` is a plain element, never
    interpolated into a shell string (no shell-injection). Returns the reply
    text. Raises on failure (caller decides), never returns a partial reply.
    """
    argv = [HERMES_BIN, "-p", profile, "chat", "-Q", "--continue", session,
            "-q", prompt]
    import subprocess
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    err = (proc.stderr or "").strip()
    # A clean "no such session yet" failure — the orchestrator's named session
    # must exist before it can be continued. Surface honestly, never synthesise.
    if proc.returncode != 0 or "Session not found" in err:
        raise RuntimeError(
            f"orchestrator session {session!r} not ready (create it first): "
            f"{err or ('exit ' + str(proc.returncode))}"
        )
    reply = (proc.stdout or "").strip()
    if not reply:
        raise RuntimeError(
            f"orchestrator {profile!r} returned an empty reply"
        )
    return reply


def default_deliver_message(msg):
    """Production default delivery: orchestrator round-trip to the original chat.

    Delivers the queued message through components that exist on this host:

      1. Resolve the message's chat/topic to an orchestrator profile/session
         (:func:`_resolve_identity`). Unmappable => return False (retained).
      2. Run the message through that orchestrator (:func:`_invoke_orchestrator`).
      3. Post the reply back to the message's original chat/topic via
         ``telegram.send_message``.

    Returns True only when the full round trip succeeds (orchestrator produced
    a non-empty reply AND the reply was posted). On ANY failure it logs loudly
    and returns False so ``replay_queued`` retains the message — never silently
    discarded. Never raises: exceptions are caught and converted to False so
    the replay engine's own try/except stays clean.

    Uses only stdlib + the same telegram/yaml transport the daemon already
    uses — no new dependencies.
    """
    # Resolve, invoke, notify are module-level seams so tests can monkeypatch
    # them without spawning a real agent or touching Telegram.
    try:
        identity = _resolve_identity(msg)
    except Exception as e:
        try:
            log(f"Autodown replay: cannot resolve orchestrator for "
                f"seq={msg.get('seq')}: {e}", "ERROR")
        except Exception:
            pass
        return False
    if identity is None:
        # Delivery FAILURE — keep the message queued and report loudly.
        try:
            log(f"Autodown replay: message seq={msg.get('seq')} platform="
                f"{msg.get('platform')!r} cannot be mapped to an orchestrator "
                f"(the reply path is telegram-only) — RETAINED, NOT replayed",
                "ERROR")
        except Exception:
            pass
        return False

    text = (msg.get("text") or "").strip()
    chat_id = msg.get("chat_id") or ""
    if not text or not chat_id:
        # A message with no text or no chat_id cannot be faithfully delivered.
        try:
            log(f"Autodown replay: message seq={msg.get('seq')} missing text "
                f"or chat_id — RETAINED, NOT replayed", "ERROR")
        except Exception:
            pass
        return False
    thread_id = msg.get("thread_id") or msg.get("topic")
    profile = identity["profile"]
    session = identity["session"]

    # Step 2 — run the message through the orchestrator.
    try:
        reply = _invoke_orchestrator(profile, session, text)
    except Exception as e:
        try:
            log(f"Autodown replay: orchestrator {profile!r} failed for "
                f"seq={msg.get('seq')}: {e} — message RETAINED (not replayed)",
                "ERROR")
        except Exception:
            pass
        return False

    # Step 3 — post the reply back to the message's original chat/topic.
    from .telegram import send_message
    delivered = False
    try:
        delivered = send_message(chat_id, reply, thread_id=thread_id,
                                 reply_to_id=msg.get("reply_to_id"))
    except Exception as e:
        delivered = False
        try:
            log(f"Autodown replay: reply-post to chat {chat_id} raised: {e}",
                "ERROR")
        except Exception:
            pass
    if not delivered:
        # The orchestrator ran, but the user never got the reply in their chat.
        # This is NOT a faithful delivery — retain the message and say so VERY
        # loudly (a later replay would re-run the orchestrator = at-least-once;
        # the operator sees the risk and can clear the queue deliberately).
        try:
            log(f"Autodown replay: orchestrator processed seq={msg.get('seq')} "
                f"but the reply could NOT be posted to chat {chat_id} "
                f"(thread_id={thread_id!r}) — message RETAINED for manual "
                f"review; re-running it would re-process the prompt "
                f"(at-least-once)", "CRITICAL")
        except Exception:
            pass
        return False
    return True
