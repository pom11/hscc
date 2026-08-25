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
# which delivers the message through the Hermes gateway's WEBHOOK platform — the
# supported, existing interface for an external process to trigger an agent run
# whose reply is routed to a specific platform chat/topic (``_deliver_cross_platform``
# in gateway/platforms/webhook.py). Faithful topic routing depends on the route's
# ``deliver_extra`` carrying the chat_id/topic from the queued message; enabling
# + configuring that webhook route is an OPERATOR step (the gateway's webhook
# platform is not on by default — see docs/design/idle-autodown.md §4.4).
#
# When no webhook platform is available/configured this function fails LOUDLY
# (returns False) so the message stays queued — never silently discarded. It
# never raises so the replay engine's own try/except stays clean.
WEBHOOK_DELIVER_URL = os.environ.get(
    "HSCC_REPLAY_WEBHOOK_URL",
    "http://127.0.0.1:8644/webhooks/autodown-replay",
)
WEBHOOK_DELIVER_SECRET = os.environ.get("HSCC_REPLAY_WEBHOOK_SECRET", "")


def default_deliver_message(msg):
    """Production default delivery: POST the message to the gateway webhook.

    Attempts faithful delivery through the gateway's supported webhook platform
    (external HTTP -> agent run -> reply routed to the message's chat/topic).
    ``msg`` is a parsed queue entry (platform/chat_id/thread_id/reply_to_id/
    text). Returns True only on a 2xx; on any error or when the webhook platform
    is unavailable it returns False so ``replay_queued`` retains the message.

    Uses stdlib ``urllib.request`` (no new dependencies).
    """
    text = (msg.get("text") or "").strip()
    if not text:
        return False
    chat_id = msg.get("chat_id") or ""
    if not chat_id:
        return False
    # Build a webhook payload: the target chat/topic is carried in deliver_extra
    # so the gateway routes the reply to where the user expects.
    payload = {
        "prompt": text,
        "deliver_extra": {
            "chat_id": chat_id,
            "thread_id": msg.get("thread_id"),
            "reply_to_id": msg.get("reply_to_id"),
        },
    }
    import urllib.request
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if WEBHOOK_DELIVER_SECRET:
        import hmac
        digest = hmac.new(WEBHOOK_DELIVER_SECRET.encode(), data,
                          "sha256").hexdigest()
        headers["X-Hub-Signature-256"] = "sha256=" + digest
    req = urllib.request.Request(
        WEBHOOK_DELIVER_URL, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except Exception:
        # Webhook platform unavailable / route not configured → fail loudly;
        # the caller retains the message in the queue.
        try:
            log(f"Autodown replay: gateway webhook delivery unavailable at "
                f"{WEBHOOK_DELIVER_URL} — message RETAINED (is the gateway "
                f"webhook platform enabled for the autodown-replay route?)",
                "ERROR")
        except Exception:
            pass
        return False
