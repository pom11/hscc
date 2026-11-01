"""orch_message — orchestrator-to-orchestrator nudges (queue-only, signed, audited).

An orchestrator profile sometimes needs to tell *another* orchestrator profile
something short ("the template you asked about is applied", "your card is
blocked on mine"). Doing it by hand means typing into another pane, which is
exactly the thing `repl_inject` exists to do safely. This tool is the narrow,
policed version of that: it is a *nudge* channel between orchestrators, not a
general messaging API.

Everything the operator asked for is enforced in code here, not in prose:

1. **Both ends must be orchestrators.** The sender is the *running* profile's
   identity (`HERMES_PROFILE`, falling back to the profile dir in `HERMES_HOME`)
   — a caller cannot claim to be someone else. Sender and target must both be
   `*-orch` profiles; worker profiles and `default` are refused, so this tool
   can never be used to inject into a worker's pane or the operator's default
   REPL.
2. **Queue-only — never an interrupt.** Delivery always goes through
   `repl_inject.inject(..., mode="auto")`, which resolves to `/queue` while the
   target is busy and a plain send when it is idle. `steer` (and the legacy
   interrupting `plain`-while-busy combination) is never chosen by this tool:
   a nudge must not cut off a running turn. The exactly-one-pane rule is
   inherited from `repl_inject.resolve_pane` — a missing *or* ambiguous pane is
   a refusal, never a guess.
3. **Auto-signed.** The body is always prefixed `[from <sender-orch>] `, plus
   `re: t_xxxx ` when `ref_card` names a kanban task. A receiver can always see
   who is speaking and what it is about.
4. **Loop / rate guards.** At most `max_per_hour` messages per
   `(sender, to)` pair (default 6; `orch_message.max_per_hour` in
   `~/.hscc/cluster.json`, `HSCC_ORCH_MSG_MAX_PER_HOUR` env override). AND the
   reply loop guard: when the *inbound message of the current turn* is itself
   a signed nudge (starts with `[from `), answering it with another
   `orch_message` is refused — a chain of agents replying to each other would
   otherwise loop until one of them runs out of context. The inbound text comes
   from the `user_task` kwarg hermes injects into plugin tool handlers
   (plugins/AGENTS.md context kwargs); where that signal is absent the guard
   cannot fire and is a documented no-op — see :func:`_loop_guard_refusal`.
5. **Audit.** Every call — sent *and* refused — appends one JSON line to
   `~/.hscc/state/orch_messages.jsonl`: `ts`, `from`, `to`, `ref_card`,
   `chars`, `delivered`, `verb`, `reason`, `pane`, `body_preview` (first 200
   chars). The full body is deliberately NOT stored. The audit log is also the
   rate limiter's only state: there is no second counter to fall out of sync.

Fail-closed by design: if the audit line cannot be written, the message is NOT
sent (a rate limit you cannot record is a rate limit you cannot enforce).

Delivery is reached through `_load_repl_inject()` — the sibling `hscc-api`
plugin dir on `sys.path`, the same loader `hscc-cluster/hscc.py` and
`hscc_daemon/kanban_pause.py` use. `repl_inject` owns pane discovery, the
busy read and the mode→verb table; this module re-implements none of it and
never calls tmux itself.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

# ── Policy constants (one place, all of them named in tests) ────────────────

#: Default per-(sender,to) hourly budget. The operator's number.
DEFAULT_MAX_PER_HOUR = 6

#: Config surface: the plugin's own ~/.hscc/cluster.json, block key + subkey
#: (same shape as node_recovery's opt-out block).
CLUSTER_JSON_BASENAME = "cluster.json"
CONFIG_BLOCK_KEY = "orch_message"
CONFIG_MAX_SUBKEY = "max_per_hour"

#: Env override, checked after the config file.
ENV_MAX_PER_HOUR = "HSCC_ORCH_MSG_MAX_PER_HOUR"

#: Audit log lives under <hscc-dir>/state/, next to reasoning_policy.json.
AUDIT_DIRNAME = "state"
AUDIT_BASENAME = "orch_messages.jsonl"

#: How much of the body the audit line keeps. Never more.
BODY_PREVIEW_CHARS = 200

#: Rate window, seconds.
RATE_WINDOW_SEC = 3600

#: Orchestrator profiles are the ones ending in `-orch` (hscc-roles names them
#: `<project>-orch` / `hscc-orch`). `default` and every worker profile fail it.
ORCH_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*-orch$")

#: Kanban task ids, as scripts/changelog_fragments.py names them.
REF_CARD_RE = re.compile(r"^t_[0-9a-f]{6,}$")

#: What every signed nudge starts with — the loop guard's marker.
SIGNED_PREFIX = "[from "

# ── Refusal / outcome reasons (machine-readable; asserted by tests) ─────────

REASON_SENT = "sent"
REASON_BAD_ARGS = "bad_args"
REASON_EMPTY = "empty_message"
REASON_BAD_REF = "bad_ref_card"
REASON_SENDER_NOT_ORCH = "sender_not_orch"
REASON_TARGET_NOT_ORCH = "target_not_orch"
REASON_SELF = "self_message"
REASON_LOOP = "loop_guard"
REASON_RATE = "rate_limited"
REASON_NO_REPL = "repl_inject_missing"
REASON_NO_PANE = "no_pane"
REASON_AMBIGUOUS = "ambiguous_pane"
REASON_TRANSPORT = "transport_error"
REASON_AUDIT = "audit_unwritable"

#: repl_inject InjectError.kind -> our reason. An unmapped kind is transport-ish
#: and reported as such rather than silently treated as a pane problem.
_INJECT_KIND_REASON = {
    "no_panes": REASON_NO_PANE,
    "ambiguous": REASON_AMBIGUOUS,
    "tmux": REASON_TRANSPORT,
    "empty": REASON_EMPTY,
}


# ── Schemas (OpenAI function-parameters style, as fleet.py/template_tools) ──

ORCH_MESSAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "to": {
            "type": "string",
            "description": "Target orchestrator profile name — must end in "
                           "'-orch' (worker profiles and 'default' are refused).",
        },
        "message": {
            "type": "string",
            "description": "Short nudge text. The '[from <sender-orch>] ' "
                           "signature is added for you; do not add it yourself.",
        },
        "ref_card": {
            "type": "string",
            "description": "Optional kanban task id this nudge is about "
                           "(t_xxxxxxxx); rendered as 're: t_xxxxxxxx'.",
        },
    },
    "required": ["to", "message"],
    "additionalProperties": False,
}


# ── Paths / config (env-resolved, never a baked ~/.hscc) ────────────────────

def hscc_dir() -> str:
    """HSCC state dir, env-resolved (same rule as node_recovery.hscc_dir())."""
    return os.environ.get("HSCC_DIR") or os.path.expanduser("~/.hscc")


def audit_path() -> str:
    """The JSONL audit log — also the rate limiter's state (single source)."""
    return os.path.join(hscc_dir(), AUDIT_DIRNAME, AUDIT_BASENAME)


def audit_writable(path: str | None = None) -> bool:
    """True when the audit log can be appended to right now.

    The pre-flight probe behind :data:`REASON_AUDIT`: the audit log is the ONLY
    place the hourly count lives, so if it cannot be written the limit cannot be
    enforced — and an uncountable nudge is exactly what the loop guard exists to
    stop. Delivery therefore refuses before touching a pane rather than sending
    something nobody will ever count.
    """
    target = path or audit_path()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a", encoding="utf-8"):
            pass
        return True
    except OSError:
        return False


def cluster_json_path() -> str:
    """The plugin's config file (cluster.json, which already carries the
    ``node_recovery`` opt-out block)."""
    return os.path.join(hscc_dir(), CLUSTER_JSON_BASENAME)


def max_per_hour(path: str | None = None) -> int:
    """Hourly per-(sender,to) budget: config file, then env, then default.

    Read the way node_recovery reads its own block: a missing file, a missing
    key, a non-dict block or a non-int value each fall through to the next
    source instead of raising, and the default (6) applies when all of them are
    unusable. An env value must be a positive integer to count; a garbage
    override is ignored rather than fatal (a typo in an env var must not turn
    the nudge channel off, and must not make it unlimited either).
    """
    try:
        with open(path or cluster_json_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        block = (data or {}).get(CONFIG_BLOCK_KEY)
        if isinstance(block, dict):
            value = block.get(CONFIG_MAX_SUBKEY)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
                return value
    except (OSError, ValueError):
        pass
    raw = os.environ.get(ENV_MAX_PER_HOUR)
    if raw:
        try:
            value = int(str(raw).strip())
        except ValueError:
            return DEFAULT_MAX_PER_HOUR
        if value >= 1:
            return value
    return DEFAULT_MAX_PER_HOUR


# ── Identity ────────────────────────────────────────────────────────────────

def running_profile() -> str:
    """The RUNNING profile's name — the only identity a sender may claim.

    hermes sets ``HERMES_PROFILE`` for a profile session and points
    ``HERMES_HOME`` at ``<hermes-home>/profiles/<name>``; the fallback keeps the
    answer honest when only one of the two is set. Anything else is
    ``"default"``, which rule 1 refuses anyway. The sender is NOT an argument:
    an orchestrator cannot sign a nudge as someone else.
    """
    name = (os.environ.get("HERMES_PROFILE") or "").strip()
    if name:
        return name
    home = (os.environ.get("HERMES_HOME") or "").strip()
    if home:
        parent, _, leaf = os.path.normpath(home).rpartition(os.sep)
        if os.path.basename(parent) == "profiles" and leaf:
            return leaf
    return "default"


def is_orch_profile(name: str) -> bool:
    """True for an orchestrator profile name (ends in ``-orch``).

    ``default`` and worker profiles (``backend-engineer``, ``*-engineer``, …)
    are False, so neither end of the channel can be a worker.
    """
    return bool(name) and ORCH_PROFILE_RE.match(name) is not None


# ── Body composition ────────────────────────────────────────────────────────

def compose_body(sender: str, message: str, ref_card: str | None = None) -> str:
    """`[from <sender-orch>] [re: <ref_card> ]<message>` — the signed body.

    The signature is always present (rule 3), so a receiver can attribute the
    nudge; the reference is inserted between the signature and the text when
    ``ref_card`` is given.
    """
    body = f"{SIGNED_PREFIX}{sender}] "
    if ref_card:
        body += f"re: {ref_card} "
    return body + message


# ── Audit log ───────────────────────────────────────────────────────────────

def _preview(text: str) -> str:
    return (text or "")[:BODY_PREVIEW_CHARS]


def append_audit(entry: dict, path: str | None = None) -> bool:
    """Append one JSONL audit line. True on success, False on failure.

    Failures are swallowed to a boolean on purpose: the caller decides, and for
    a *send* the decision is fail-closed (no audit line -> no send), because the
    audit log is what the rate limit is computed from.
    """
    target = path or audit_path()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except OSError:
        return False


def sent_messages(path: str | None = None) -> list[dict]:
    """Every audit line that records a DELIVERED message, oldest first.

    Malformed lines are skipped (one corrupt line must not blind the guard).
    """
    target = path or audit_path()
    out = []
    try:
        with open(target, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("delivered") is True:
            out.append(entry)
    return out


def delivered_in_window(sender: str, to: str, now: float | None = None,
                        path: str | None = None) -> int:
    """How many messages already went from ``sender`` to ``to`` in this hour.

    Fail-closed on an unreadable timestamp: a delivered line whose ``ts`` is
    missing or unparseable cannot be placed outside the window, so it counts.
    Under-counting would hand out extra budget, which is the wrong direction for
    a loop guard.
    """
    now = time.time() if now is None else now
    cutoff = now - RATE_WINDOW_SEC
    count = 0
    for entry in sent_messages(path):
        if entry.get("from") != sender or entry.get("to") != to:
            continue
        try:
            ts = float(entry.get("ts"))
        except (TypeError, ValueError):
            count += 1
            continue
        if ts >= cutoff:
            count += 1
    return count


# ── repl_inject loader (sibling hscc-api plugin dir) ────────────────────────

def _load_repl_inject():
    """Import ``repl_inject`` from the sibling hscc-api plugin dir, or None.

    Same sibling-dir approach as ``hscc-cluster/hscc.py::_load_repl_inject`` and
    ``hscc_daemon/kanban_pause.py``: that module owns pane discovery, the
    exactly-one-pane rule and the mode vocabulary, so this plugin never
    re-implements them and never runs tmux itself. A ``sys.path`` entry added
    here is removed again when the import fails, so a missing sibling cannot
    leave the process with a polluted path.
    """
    try:
        import repl_inject  # noqa: F401  (already importable)
        return repl_inject
    except ImportError:
        pass
    api_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hscc-api")
    added = None
    if os.path.isdir(api_dir) and api_dir not in sys.path:
        sys.path.insert(0, api_dir)
        added = 0
    try:
        import repl_inject  # noqa: F401
        return repl_inject
    except ImportError:
        return None
    finally:
        if added is not None and added < len(sys.path) and sys.path[added] == api_dir:
            sys.path.pop(added)


# ── Guards ──────────────────────────────────────────────────────────────────

def _loop_guard_refusal(user_task) -> bool:
    """True when this turn's inbound message is itself a signed nudge.

    The reply-loop rule: a message that arrived already signed `[from ` must
    not be auto-answered with another `orch_message` in the same turn — the
    receiver replies in prose, or does the work, but does not bounce a nudge
    back into the channel. Two orchestrators each auto-answering the other's
    signed message would otherwise ping-pong until a context window gave out.

    The signal is the `user_task` kwarg hermes injects into plugin tool
    handlers (the current turn's inbound user text). It is per-turn, which is
    exactly the scope the rule needs: the same orchestrator may legitimately
    open a nudge on a later turn. When the kwarg is absent (a caller that does
    not supply it) the guard has no evidence and stays silent — documented, and
    covered by a test so the behaviour cannot change by accident.
    """
    if not isinstance(user_task, str):
        return False
    return user_task.lstrip().startswith(SIGNED_PREFIX)


# ── The tool ────────────────────────────────────────────────────────────────

def orch_message(args, *, user_task=None, session_id=None, task_id=None,
                 **_context) -> dict:
    """Send an orchestrator-to-orchestrator nudge (queue-only, signed, audited).

    ``args``: ``{"to": <orch profile>, "message": <text>, "ref_card": t_xxxx?}``.
    ``user_task`` is the current turn's inbound text (hermes context kwarg) and
    feeds the reply loop guard; ``**_context`` absorbs the rest of the plugin
    context kwargs so a hermes addition can never break the call.
    Returns a machine-readable dict (the plugin wrapper JSON-stringifies it):
    ``ok``, ``sent``, ``reason``, ``detail``, ``from``, ``to``, ``chars``,
    ``body_preview``, and — on a real delivery — ``mode``/``verb``/``pane``/
    ``target``/``busy`` from the repl_inject receipt. ``remaining`` is the
    hourly budget left for this pair after the call (None when unknown).

    Every outcome writes one audit line. A refusal never touches the pane.
    """
    if not isinstance(args, dict):
        args = {}
    to = args.get("to")
    message = args.get("message")
    ref_card = args.get("ref_card")
    sender = running_profile()

    def outcome(ok: bool, reason: str, detail: str, *, sent: bool = False,
                verb=None, mode=None, pane=None, target=None, busy=None,
                remaining=None) -> dict:
        body = (compose_body(sender, message, ref_card)
                if isinstance(message, str) else str(message))
        entry = {
            "ts": round(time.time(), 3),
            "from": sender,
            "to": to,
            "ref_card": ref_card,
            "chars": len(message) if isinstance(message, str) else 0,
            "delivered": bool(sent),
            "verb": verb,
            "reason": reason,
            "pane": pane,
            "body_preview": _preview(body),
        }
        if session_id:
            entry["session_id"] = session_id
        audit_ok = append_audit(entry)
        result = {
            "ok": ok,
            "sent": bool(sent),
            "reason": reason,
            "detail": detail,
            "from": sender,
            "to": to,
            "ref_card": ref_card,
            "chars": entry["chars"],
            "body_preview": entry["body_preview"],
            "audit": audit_ok,
        }
        if sent:
            result.update({"mode": mode, "verb": verb, "pane": pane,
                           "target": target, "busy": busy})
        if remaining is not None:
            result["remaining"] = remaining
        return result

    # 1. Argument shape.
    if not isinstance(to, str) or not to.strip():
        return outcome(False, REASON_BAD_ARGS, "'to' must be a non-empty string.")
    to = to.strip()
    if not isinstance(message, str) or not message.strip():
        return outcome(False, REASON_EMPTY, "'message' must be a non-empty string.")
    if ref_card is not None:
        if not isinstance(ref_card, str) or not REF_CARD_RE.match(ref_card.strip()):
            return outcome(False, REASON_BAD_REF,
                           "'ref_card' must look like a kanban task id (t_ + hex).")
        ref_card = ref_card.strip()

    # 2. Both ends must be orchestrators (rule 1); sender is the running profile.
    if not is_orch_profile(sender):
        return outcome(False, REASON_SENDER_NOT_ORCH,
                       f"sender {sender!r} is not an orchestrator profile "
                       f"(name must end in '-orch'); this tool is orch-to-orch only.")
    if not is_orch_profile(to):
        return outcome(False, REASON_TARGET_NOT_ORCH,
                       f"target {to!r} is not an orchestrator profile "
                       f"(name must end in '-orch'); workers and 'default' are "
                       f"never injectable through this tool.")
    if to == sender:
        return outcome(False, REASON_SELF,
                       "the sender and the target are the same profile — an "
                       "orchestrator nudging itself is a loop, not a message.")

    # 3. Reply loop guard (rule 4b).
    if _loop_guard_refusal(user_task):
        return outcome(False, REASON_LOOP,
                       "this turn was opened by a signed '[from ...]' nudge, so "
                       "it must not be answered with another orch_message. "
                       "Reply in prose (or do the work) instead — the sender "
                       "sees your pane either way.")

    # 4. Rate limit (rule 4a) — computed from the audit log itself.
    limit = max_per_hour()
    used = delivered_in_window(sender, to)
    if used >= limit:
        return outcome(False, REASON_RATE,
                       f"hourly limit reached for {sender} -> {to} "
                       f"({used}/{limit} in the last {RATE_WINDOW_SEC}s). "
                       f"Raise it with {CONFIG_BLOCK_KEY}.{CONFIG_MAX_SUBKEY} in "
                       f"{cluster_json_path()} or {ENV_MAX_PER_HOUR}=N.",
                       remaining=0)

    # 5. Delivery — repl_inject owns pane resolution and the busy/verb decision.
    #    Fail-closed pre-flight first: the audit log is the only place the
    #    hourly count lives, so a nudge that could not be recorded is a nudge
    #    that cannot be rate-limited. Refuse before touching a pane.
    if not audit_writable():
        return outcome(False, REASON_AUDIT,
                       f"cannot append to the audit log at {audit_path()} — the "
                       f"rate limit would be unenforceable, so nothing was sent. "
                       f"Fix the directory (or set HSCC_DIR) and retry.")
    repl = _load_repl_inject()
    if repl is None:
        return outcome(False, REASON_NO_REPL,
                       "repl_inject is not importable — the hscc-api plugin must "
                       "be deployed next to hscc-cluster for orch_message.")
    body = compose_body(sender, message, ref_card)
    try:
        # mode="auto" is the ONLY mode this tool uses: /queue while the target
        # is busy, plain when idle. 'steer' is never chosen from here.
        receipt = repl.inject(to, body, mode="auto")
    except Exception as exc:  # InjectError kinds, plus any tmux-side failure
        kind = getattr(exc, "kind", None)
        reason = _INJECT_KIND_REASON.get(kind, REASON_TRANSPORT)
        return outcome(False, reason, str(exc) or type(exc).__name__)

    verb = receipt.get("verb")
    if verb is None:
        # Older repl_inject (pre-parity) receipts carry `mode` but no `verb`.
        mode = receipt.get("mode") or ""
        table = getattr(repl, "VERB_PREFIX", None) or {"plain": "", "queue": "/queue "}
        verb = table.get(mode)
    remaining = max(limit - (used + 1), 0)
    return outcome(True, REASON_SENT,
                   f"queued to {to}" if receipt.get("mode") == "queue"
                   else f"delivered to {to}",
                   sent=True, verb=verb, mode=receipt.get("mode"),
                   pane=receipt.get("pane"), target=receipt.get("target"),
                   busy=receipt.get("busy"), remaining=remaining)
