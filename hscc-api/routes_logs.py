"""HSCC HTTP API — bounded, redacted GET /v1/logs.

Read-only tail of the cluster's three primary logs (t_3a995be5), which the
iOS LogsView (t_2eda26a6) is built against:

    GET /v1/logs?source=daemon|api|worker&limit=<N>

``source`` selects which log to tail; ``limit`` (default 50, capped at 200)
bounds how many of the most recent lines are returned. The response is a BARE
ARRAY of entries — ``[{timestamp, level, source, line}]`` — matching the iOS
contract ``LogsResponse = [LogEntry]`` (see Models.swift).

Three guarantees, all enforced on the server:

1. BOUNDED MEMORY. The handler never loads the whole log into memory. It
   reverse-seeks from EOF and reads only the last ``limit`` lines (plus a
   little slop for very long lines), so a 280 MB daemon.log costs O(limit)
   bytes regardless of file size.

2. BOUNDED RESPONSE. ``limit`` is clamped to [1, 200]. The payload holds at
   most ``limit`` entries, each tiny.

3. REDACTED BEFORE SERVING. LogRedactor (iOS) is the SECOND line of defence;
   the server must never serve raw secrets. Every line is passed through
   ``_redact`` which masks Tailnet hosts to ``100.64.0.1``, RFC1918 to
   ``10.0.0.x``, other IPv4 to ``[REDACTED_IP]``, plus bearer tokens,
   token=/apikey= values, session ids, and long opaque runs.

Conventions (design §A/§B — mirror routes_cron / routes_ops):
  * handler is ``(server, ctx, query, body) -> (status, payload)``;
  * the backing tail goes through ``_backing_tail`` so tests can monkeypatch
    it without ever touching the operator's live logs;
  * an unreadable/absent log degrades to a 200 with an empty array (an empty
    tail is an honest answer — never crash, never fabricate).
"""

from __future__ import annotations

import os
import re
import builtins

from api_server import ROUTES  # noqa: E402

# Maximum number of lines the endpoint will ever return (the iOS client also
# caps at 200 — both sides agree on this bound).
MAX_LIMIT = 200
DEFAULT_LIMIT = 50

# source -> absolute log path. Worker maps to the Hermes agent log, where the
# running worker agent's job-execution output lands.
_LOG_PATH = {
    "daemon": os.path.expanduser("~/.hscc/daemon.log"),
    "api": os.path.expanduser("~/.hscc/api.log"),
    "worker": os.path.expanduser("~/.hermes/logs/agent.log"),
}

_KNOWN_SOURCES = tuple(_LOG_PATH.keys())

# ---- timestamp+level parsing (tolerant; both known log formats) --------- #
# daemon/api:  "[2026-09-03T16:05:17.732336+00:00] [ WARN] <msg>"
# agent:       "2026-09-03 19:00:58,245 INFO cron.scheduler: <msg>"
_TS_DAEMON = re.compile(
    r"^\s*\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)\]\s*\[\s*(?P<level>[A-Za-z]+)\s*\]\s*(?P<line>.*)$"
)
_TS_AGENT = re.compile(
    r"^\s*(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2},\d{3})\s+(?P<level>[A-Za-z]+)\s+(?P<line>.*)$"
)


# --------------------------------------------------------------------------- #
# Redaction (server side). Mirrors iOS LogRedactor — see ios-app/.../LogRedactor.swift
# --------------------------------------------------------------------------- #

_TAILNET_RE = re.compile(r"\b100\.(?:[6-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b")
_RFC1918_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3})\b"
)
_OTHER_IP_RE = re.compile(r"\b(?!100\.64\.0\.1\b)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
_BEARER_INLINE_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}")
_TOKEN_VAL_RE = re.compile(
    r"(?i)\b(token|apikey|api_key|secret|password|auth|access_token|client_secret)"
    r"\s*[=:]\s*[^&\s'\"]+"
)
_SESSION_RE = re.compile(
    r"(?i)(\bsess_[A-Za-z0-9_-]+|\bsession\s*[=:]\s*[A-Za-z0-9_-]+|\bsession\s+[A-Za-z0-9_-]{8,})"
)
_LONG_RUN_RE = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9_-]{19,}\b")


def _redact(text: str) -> str:
    """Fully redact a single raw log line (server-side, before serving)."""
    out = _TAILNET_RE.sub("100.64.0.1", text)
    out = _RFC1918_RE.sub("10.0.0.x", out)
    out = _OTHER_IP_RE.sub("[REDACTED_IP]", out)
    out = _BEARER_INLINE_RE.sub("Bearer ***", out)
    out = _TOKEN_VAL_RE.sub(r"\1=***", out)
    out = _SESSION_RE.sub("sess_***", out)
    out = _LONG_RUN_RE.sub("***", out)
    return out


# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch these in tests)
# --------------------------------------------------------------------------- #

def _backing_tail(path: str, limit: int) -> list[str]:
    """Return the most recent ``limit`` raw lines of ``path``.

    Reads only the trailing portion of the file — reverse-seeks from EOF and
    reads backward in bounded chunks until ``limit`` newlines are collected or
    the start of the file is reached. The whole file is NEVER loaded into
    memory (daemon.log can be hundreds of MB).

    An absent/unreadable file returns an empty list (honest, not an error).
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    if size <= 0:
        return []

    with builtins.open(path, "rb") as f:
        # Read backward in 16 KiB chunks. Keep a small trailing buffer to
        # capture the final (possibly partial) line at EOF.
        chunk = 16 * 1024
        pos = size
        buf = b""
        newline_count = 0
        # We want `limit` lines; the last line may be unterminated. Allow one
        # extra newline so an unterminated final line still counts.
        target = limit + 1
        while pos > 0 and newline_count < target:
            read_start = max(0, pos - chunk)
            f.seek(read_start)
            data = f.read(pos - read_start)
            buf = data + buf
            newline_count = buf.count(b"\n")
            pos = read_start
        # `buf` now holds the trailing (limit+1)-line region. Take the last
        # `limit` complete lines from it.
        lines = buf.split(b"\n")
        if lines and lines[-1] == b"" and len(lines) > 1:
            lines.pop()  # drop the trailing empty piece after the final \n
        want = lines[-limit:]
        return [l.decode("utf-8", errors="replace") for l in want]


def _parse_line(row_line: str, source: str) -> dict:
    """Split one raw line into {timestamp, level, source, line} (all str)."""
    line = row_line.rstrip("\r\n")
    for ts_re in (_TS_DAEMON, _TS_AGENT):
        m = ts_re.match(line)
        if m:
            return {
                "timestamp": m.group("ts"),
                "level": m.group("level").upper(),
                "source": source,
                "line": m.group("line"),
            }
    # No recognised header — serve the whole line (redacted downstream).
    return {"timestamp": None, "level": None, "source": source, "line": line}


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #

def handle_logs(server, ctx, query, body):
    """GET /v1/logs?source=daemon|api|worker&limit=<N> — bounded redacted tail.

    Returns a BARE ARRAY of ``{timestamp, level, source, line}`` entries for
    the ``limit`` most recent lines of the requested log, each line redacted
    before serving (Tailnet->100.64.0.1, RFC1918->10.0.0.x, secrets/session
    ids masked). Never reads more than O(limit) of the file into memory.

    - unknown/omitted ``source``  -> 400 bad_request naming the valid values;
    - a malformed ``limit``       -> 400 bad_request;
    - an absent/unreadable log    -> 200 with an empty array (honest tail).
    """
    source = (query.get("source") or "").strip().lower()
    if source not in _KNOWN_SOURCES:
        from api_server import error_bad_request
        raise error_bad_request(
            "source must be one of: " + ", ".join(_KNOWN_SOURCES)
        )

    raw_limit = query.get("limit", "").strip() or str(DEFAULT_LIMIT)
    try:
        limit = int(raw_limit)
    except ValueError:
        from api_server import error_bad_request
        raise error_bad_request(f"limit must be an integer, got {raw_limit!r}")
    limit = max(1, min(limit, MAX_LIMIT))

    raw_lines = _backing_tail(_LOG_PATH[source], limit)
    entries = [
        {**parsed, "line": _redact(parsed["line"])}
        for parsed in (_parse_line(rl, source) for rl in raw_lines)
    ]
    # Never serve more than `limit` entries even if the backing tail overshot
    # on the final (unterminated) line.
    return 200, entries[-limit:]


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("GET", re.compile(r"^/v1/logs$"), handle_logs))
