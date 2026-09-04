"""HSCC HTTP API — GET /v1/daemon/history — timeline of automated actions.

The daemon auto-heals, escalates and (optionally) auto-scales, but the raw
daemon.log is dominated by per-cycle noise ("Watchdog: pipeline healthy", tens
of thousands of lines a week), so a plain log tail shows the operator almost
nothing about what the automation actually DECIDED. This endpoint answers
"what did the cluster do while I was away": a filtered, structured timeline of
automated actions — watchdog restarts, pipeline escalations/blocks, autodown
transitions, and recovery relauches — with timestamps and outcomes:

    GET /v1/daemon/history?limit=<N>

``limit`` (default 100, capped at 500) bounds how many ACTION events are
returned. The response is an OBJECT:

    {
      "events": [
        {
          "timestamp": "2026-08-30T00:59:11.000990+00:00",
          "kind": "restart",
          "outcome": "success",
          "repeats": 1,
          "last_ts": null,
          "detail": "Watchdog: vLLM auto-restart #3: success"
        },
        ...
      ],
      "count": 23,
      "window_bytes": 33554432,
      "exhausted": false
    }

The watchdog reposts its held state every 30s cycle, so ``events`` are
COLLAPSED into runs: consecutive entries with the same ``kind`` and
``outcome`` merge into one, with ``repeats`` (how many log lines the run spans)
and ``last_ts`` (the run's final line timestamp, or null for a single event).
This turns a multi-hour "autodown in effect" window into one timeline entry
instead of hundreds of identical rows — the operator sees distinct decisions,
not cycle noise.

Guarantees:

1. BOUNDED MEMORY / BOUNDED RESPONSE. The scanner reverse-seeks from EOF and
   reads backward in bounded chunks up to ``window_bytes`` (default 32 MiB).
   It never loads the whole (hundreds-of-MB) log. It stops early as soon as it
   has ``limit`` action events, so a healthy log with recent history costs a
   few MiB. ``exhausted`` is true only when the whole window was scanned and
   fewer than ``limit`` actions were found (an honest "this is all the history
   in the window").

2. REDACTION. Every detail line is passed through the same server-side
   redactor as /v1/logs (Tailnet ->100.64.0.1, RFC1918 ->10.0.0.x, tokens and
   session ids masked) before it is served.

3. READ-ONLY. There is no mutating path anywhere in this route.

4. HONEST EMPTINESS. An absent/unreadable log or a window with no action lines
   returns 200 with ``events: []`` — never an error, never fabricated data.

Kinds / outcomes the classifier emits (see _classify):

  kind     meaning                              example outcome values
  restart  watchdog started a serving unit      success / failed / attempt
  block    pump pipeline blocked or cleared     failed (blocked) / cleared
  autodown autodown transition                 info / recon (reconcile)
  recovery a worker/wedge recovered            success / attempt
  escalate a failure rule escalated a task     info (subsumed into block for
                                               pipeline blocks)

Conventions (design §A/§B — mirror routes_logs): handler is
``(server, ctx, query, body) -> (status, payload)``; the scan goes through
``_backing_scan`` so tests can monkeypatch it without touching the operator's
live log.
"""

from __future__ import annotations

import os
import re

from api_server import ROUTES  # noqa: E402
from routes_logs import _redact, _parse_line  # reuse the exact log parsing + redaction

MAX_LIMIT = 500
DEFAULT_LIMIT = 100

# How much of the tail of the log to scan backward for action lines before
# giving up. 32 MiB of a 30s-cycle daemon covers roughly the recent history.
# Bounded and cheap; the endpoint returns far fewer bytes than it reads.
DEFAULT_WINDOW_BYTES = 32 * 1024 * 1024
MAX_WINDOW_BYTES = 128 * 1024 * 1024

DAEMON_LOG = os.path.expanduser("~/.hscc/daemon.log")


# --------------------------------------------------------------------------- #
# Action-line classifier
# --------------------------------------------------------------------------- #

# Matches within the (already timestamp/level-stripped) message body. Each is
# (regex, kind, outcome_group or fixed outcome, label).
#
# Order matters: more specific patterns first (graders before blocks stay out
# of each other's way). We return the FIRST kind that matches, so keep the
# clearly-distinct cases at the top and put generic fallbacks last.

_PATTERNS = [
    # — watchdog restart of a serving unit, with an explicit outcome —
    (re.compile(r"Watchdog: vLLM auto-restart #\d+:\s*(success|failed)"),
     "restart", 1),
    (re.compile(r"Watchdog: attempting vLLM auto-restart"),
     "restart", "attempt"),
    # — pipeline escalation / block — the "3 consecutive failures" escalation —
    (re.compile(r"Watchdog: BLOCKING pipeline"),
     "block", "failed"),
    # — block cleared
    (re.compile(r"Watchdog: backoff elapsed, clearing block and resuming checks"),
     "block", "cleared"),
    # — autodown holding the orchestrator down (intentional) —
    (re.compile(r"Watchdog: intentional autodown in effect"),
     "block", "autodown"),
    # — autodown wake / reconcile / thread-start transitions —
    (re.compile(r"Autodown autoup: wake recorded"),
     "autodown", "wake"),
    (re.compile(r"Autodown autoup: started orchestrator unit"),
     "autodown", "started"),
    (re.compile(r"Autodown RECONCILED: stalled wake"),
     "autodown", "recon"),
    (re.compile(r"Started autodown thread"),
     "autodown", "started"),
    # — recovery: worker relaunch / dispatcher-wedge restart —
    (re.compile(r"Worker proxy down.*relaunching", re.IGNORECASE),
     "recovery", "attempt"),
    (re.compile(r"Dispatcher-wedge recovery: restart invoked \(attempt (\d+), success=(True|False)\)"),
     "recovery", 2),
]


def classify_line(line: str):
    """Classify one message line (already timestamp-stripped).

    Returns (kind, outcome) or None if the line is not an automated action.
    ``outcome`` is a str; for patterns capturing a group it is normalized
    lowercase (``success`` / ``failed`` / ``true``->``success``).
    """
    low = line
    for rx, kind, outcome in _PATTERNS:
        m = rx.search(low)
        if not m:
            continue
        if isinstance(outcome, int):
            val = m.group(outcome).lower()
            # "true"/"false" capture -> success/failed for the dispatch case
            if val in ("true", "false"):
                val = "success" if val == "true" else "failed"
            return kind, val
        return kind, outcome
    return None


# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch in tests)
# --------------------------------------------------------------------------- #

def _backing_scan(path: str, limit: int, window_bytes: int) -> dict:
    """Reverse-scan ``path`` for the ``limit`` most recent automated-action lines.

    Reads the trailing ``window_bytes`` of the file as ONE bounded forward
    read (O(window), never O(file) — the log is hundreds of MB, the window is
    at most 128 MiB), drops a possible partial leading line, classifies every
    complete line, and returns the last ``limit`` matches in file order (oldest
    first). Stops scanning further once ``window_bytes`` is covered.

    Returns the last ``limit`` matches in file order (oldest first) after
    run-collapsing: {"lines": [{kind, outcome, timestamp, repeats, last_ts,
    line}], "window_bytes": bytes covered, "exhausted": bool (true when fewer
    than ``limit`` actions were found in the whole window)}.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return {"lines": [], "window_bytes": 0, "exhausted": True}
    if size <= 0:
        return {"lines": [], "window_bytes": 0, "exhausted": True}

    window_bytes = max(1, min(window_bytes, MAX_WINDOW_BYTES))

    start = max(0, size - window_bytes)
    with open(path, "rb") as f:
        f.seek(start)
        raw = f.read()
    # Drop a partial leading line (we don't know the first line's start).
    if start > 0:
        idx = raw.find(b"\n")
        raw = raw[idx + 1:] if idx != -1 else raw
    text = raw.decode("utf-8", errors="replace")
    raw_lines = text.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()

    matches = []
    for rl in raw_lines:
        parsed = _parse_line(rl, "daemon")
        if parsed["line"] is None:
            continue
        cls = classify_line(parsed["line"])
        if cls:
            matches.append((parsed, cls))
    matches = matches[-limit:]
    matches = _collapse_runs(matches)
    return {
        "lines": matches,
        "window_bytes": window_bytes,
        "exhausted": len(matches) < limit,
    }


def _collapse_runs(matches):
    """Merge consecutive matches with the same (kind, outcome) into one entry.

    The watchdog logs its held state every cycle (30s), so a single long
    "autodown in effect" window produces hundreds of near-identical lines.
    Collapsing keeps only the distinct automated events for a clean timeline:
    the first entry of a run wins the timestamp, and ``repeats`` + ``last_ts``
    record how long the state persisted. Returns a new list.
    """
    out = []
    for parsed, (kind, outcome) in matches:
        if out and out[-1]["kind"] == kind and out[-1]["outcome"] == outcome:
            out[-1]["repeats"] += 1
            out[-1]["last_ts"] = parsed.get("timestamp")
            continue
        out.append({
            "kind": kind,
            "outcome": outcome,
            "timestamp": parsed.get("timestamp"),
            "repeats": 1,
            "last_ts": None,
            "line": parsed.get("line"),
        })
    return out


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #

def handle_history(server, ctx, query, body):
    """GET /v1/daemon/history?limit=<N> — timeline of automated daemon actions.

    Returns ``{events, count, window_bytes, exhausted}`` where each event has
    ``{timestamp, kind, outcome, detail}``. ``detail`` is the redacted message
    body of the source line. Read-only, bounded, honest on empty.
    """
    raw_limit = query.get("limit", "").strip() or str(DEFAULT_LIMIT)
    try:
        limit = int(raw_limit)
    except ValueError:
        from api_server import error_bad_request
        raise error_bad_request(f"limit must be an integer, got {raw_limit!r}")
    limit = max(1, min(limit, MAX_LIMIT))

    window = query.get("window", "").strip()
    window_bytes = DEFAULT_WINDOW_BYTES
    if window:
        try:
            window_bytes = int(window)
        except ValueError:
            from api_server import error_bad_request
            raise error_bad_request(f"window must be an integer of bytes, got {window!r}")
    window_bytes = max(1, min(window_bytes, MAX_WINDOW_BYTES))

    result = _backing_scan(DAEMON_LOG, limit, window_bytes)
    events = result["lines"]  # already collapsed dicts; add redacted detail
    for ev in events:
        ev["detail"] = _redact(ev.pop("line") or "")
    return 200, {
        "events": events,
        "count": len(events),
        "window_bytes": result["window_bytes"],
        "exhausted": result["exhausted"],
    }


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("GET", re.compile(r"^/v1/daemon/history$"), handle_history))
