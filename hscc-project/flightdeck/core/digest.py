"""digest.py — a READABLE, BOUNDED digest of a project's history.

``project digest <name>`` synthesises the project's real history into a short
human extract sized for a few thousand tokens — NEVER the raw transcripts.

The DECISION CONTENT and message COUNTERS come from the DEFAULT profile's
``state.db`` ``messages`` table, read through Hermes' OWN ``SessionDB``
(``get_messages``, default = ``active = 1`` only). The archive markdown export
``~/.hermes/archive/telegram/<project>/`` is used ONLY for the per-session
header metadata (title / session_id / started / ended) — never for bodies or
counts, because its renderer re-inlines raw ``tool_calls`` JSON as text, which
is exactly the corruption this module must never repeat.

Decision extraction applies a strict whitelist over the ``messages`` rows (all
must hold): ``role in ('user','assistant')`` (excludes ``tool``/``system``/
``session_meta``), NOT an assistant message with ``tool_calls`` set (excludes
the JSON leak), content must not start with ``[CONTEXT COMPACTION`` (excludes
the compaction notice), and ``active = 1``.

This is a SYNTHESIS, never a merge: it reads/mutates nothing about the sessions
themselves. It never physically merges sessions (no tool_call adjacency, no
compaction-header hijack), never writes to ``state.db``, the registry, or a
kanban board. It only ever READS archive markdown headers and ``state.db``
messages via the sanctioned read-only ``SessionDB`` opener.

Bounding (the hard requirement)
------------------------------
Per thread the digest keeps only: title, date range, message count, a SHORT
``decision`` extract, and the resume line. The ``decision`` extract mirrors
``map_sessions.bounded_sample``: the first N + last N user/assistant message
bodies with the omitted middle marked, hard-capped — so one huge session can
never blow the digest. If a session genuinely needs a model to summarise, that
stays an injectable seam that fails CLOSED to the head/tail extract; the digest
never emits a raw transcript body.

ENV FAULT VS DATA (mandatory)
-----------------------------
An unreadable Hermes runtime raises ``HermesRuntimeUnavailable`` and surfaces
as ``runtime_error`` on the digest — never as an empty digest implying the
project has no history. The archive files themselves need no runtime to read,
but the resume lines reference Hermes profiles, so an environment fault must
still be reported distinctly rather than silently rendered as \"no data\".
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Default archive export root (matches archive.py DEFAULT_OUT_DIR). The digest
# reads the per-project subdirectory ``<root>/<name>/``.
DEFAULT_ARCHIVE_DIR = "~/.hermes/archive/telegram"

# Total cap on the assembled digest text (a few thousand tokens is the target;
# this is the hard ceiling every caller can rely on).
DIGEST_MAX_CHARS = 12000

# Per-thread ``decision`` extract limits (mirror map_sessions.bounded_sample):
# first N + last N user/assistant message bodies, omitted middle marked, hard
# char cap so one huge session can never blow the digest.
SLICE_HEAD = 3
SLICE_TAIL = 2
SLICE_MAX_CHARS = 2000

# The resume profile for archived (Telegram) sessions — they live on the DEFAULT
# profile, never the <name>-orch one. Matches the card's "profile = default for
# telegram".
DIGEST_PROFILE = "default"

# A session file's metadata header, as archive.py renders it. Parsed (never
# regexed over the whole file) so a malformed file can never crash the digest.
# Each line has the shape ``- **<field>**: <value>`` — the ``^- `` prefix is
# part of the archive render, not of the value.
_META_RE = {
    "session_id": re.compile(r"^-\s*\*\*session id\*\*:\s*`([^`]*)`"),
    "started": re.compile(r"^-\s*\*\*started\*\*:\s*(.*)$"),
    "ended": re.compile(r"^-\s*\*\*ended\*\*:\s*(.*)$"),
}


def _resolve(path: str | None, default: str) -> Path:
    return Path(os.path.expanduser(path or default))


def _probe_runtime() -> str | None:
    """Return a message when the Hermes runtime is unimportable, else None.

    Mirrors ``session_discovery._runtime_error``: an unreadable runtime must
    render as ``runtime_error``, never as an empty digest implying \"no
    history\". Reading archive FILES needs no runtime, but the resume lines
    reference Hermes profiles, so an env fault is still surfaced distinctly.
    """
    from . import project_lifecycle as _lifecycle

    try:
        _lifecycle._open_profile_session_db("default", read_only=True)
    except _lifecycle.HermesRuntimeUnavailable as exc:
        return str(exc)
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------- #
# Archive file parsing (bounded; a malformed file cannot crash the digest)
# --------------------------------------------------------------------------- #

def _parse_session_header(path: Path) -> dict:
    """Parse ONE archive markdown file for the per-session HEADER metadata.

    Returns ``title``, ``session_id``, ``started``, ``ended`` (raw strings from
    the header; ``(unknown)``-style when absent). The body and message count are
    deliberately NOT read from this file — they come from ``state.db`` (see
    ``_fill_from_state_db``) because the archive renderer re-inlines raw
    ``tool_calls`` JSON as text. Never raises on a malformed file — everything
    falls back to defaults so one bad file cannot sink the whole digest.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    title = ""
    for ln in lines:
        if ln.startswith("# "):
            title = ln[2:].strip()
            break

    meta: dict[str, str] = {}
    for ln in lines:
        if ln.strip() == "---":
            break
        for field, pat in _META_RE.items():
            m = pat.search(ln)
            if m:
                meta[field] = m.group(1).strip()
                break

    return {
        "title": title or path.stem,
        "session_id": meta.get("session_id", ""),
        "started": meta.get("started", ""),
        "ended": meta.get("ended", ""),
    }


def _is_message_whitelisted(msg: dict) -> bool:
    """The decision-whitelist: a ``messages`` row that may appear in a digest.

    ALL conditions must hold (orchestrator-settled, do not relax):
      * ``role in ('user','assistant')``            — excludes tool/system/session_meta
      * NOT (assistant with ``tool_calls`` set)     — excludes the JSON leak
      * content must NOT start with ``[CONTEXT COMPACTION`` — excludes the
        active compaction notice (no reliable column flag marks that row;
        ``compacted`` marks the SUPERSEDED rows, not the notice)
      * ``active = 1``                              — excludes inert/rewound rows
    """
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return False
    if role == "assistant" and msg.get("tool_calls"):
        return False
    content = msg.get("content") or ""
    if content.startswith("[CONTEXT COMPACTION"):
        return False
    if not msg.get("active", 1):  # belt-and-suspenders; get_messages already filters
        return False
    return True


def _bounded_decision(messages: list[dict]) -> str:
    """A short, bounded extract of the session's own human words.

    ``messages`` is the WHITELISTED, ordered (chronological) subset already
    filtered by ``_is_message_whitelisted``. Mirrors ``map_sessions.bounded_sample``:
    first ``SLICE_HEAD`` + last ``SLICE_TAIL`` user/assistant message bodies with
    the omitted middle marked, hard-capped at ``SLICE_MAX_CHARS``. Flattens each
    message to a one-liner.
    """
    words = [
        (m.get("role", ""), (m.get("content") or "").strip())
        for m in messages
        if (m.get("content") or "").strip()
    ]
    if not words:
        return ""

    def flat(role: str, text: str) -> str:
        body = text.replace("\n", " ").strip()[:800]
        return f"[{role}] {body}"

    rendered = [flat(r, t) for r, t in words]
    head_chunk = rendered[:SLICE_HEAD]
    tail_chunk = rendered[-SLICE_TAIL:] if len(rendered) > SLICE_HEAD + SLICE_TAIL else []
    middle = ["(… %d message(s) omitted …)" % (len(rendered) - SLICE_HEAD - SLICE_TAIL)] if tail_chunk else []
    sample = "\n".join(head_chunk + middle + tail_chunk)
    if len(sample) > SLICE_MAX_CHARS:
        sample = sample[:SLICE_MAX_CHARS]
        sample += "\n[… decision extract truncated: over %d chars]" % SLICE_MAX_CHARS
    return sample


def _resolve_session_db(_session_db):
    """Resolve the session-db provider seam (default: open the real profile).

    Mirrors ``project_lifecycle._resolve_session_db``: ``None`` means "open the
    DEFAULT profile's state.db read-only" via the sanctioned opener; an injected
    callable (the test seam) is used as-is.
    """
    from .project_lifecycle import _open_profile_session_db as _open

    return _session_db if _session_db is not None else _open


def _fill_from_state_db(entry: dict, db) -> None:
    """Populate ``entry``'s ``message_count`` and ``decision`` from ``db``.

    ``db`` is an opened ``SessionDB`` (read-only in production) or ``None``
    (then the fields keep their defaults: count 0, empty decision). ``message_count``
    counts the session's ACTIVE rows — the exact number ``project sessions``
    shows — so digest and sessions reconcile by construction. The ``decision``
    extract uses ONLY the whitelisted subset of those rows (see
    ``_is_message_whitelisted``).

    All reads go through Hermes' OWN ``get_messages`` (default = ``active = 1``
    only); no hand-rolled SQL. Fail-soft: any error leaves the entry at its
    defaults rather than sinking the whole digest.
    """
    sid = entry.get("session_id")
    if db is None or not sid:
        entry.setdefault("message_count", 0)
        entry.setdefault("decision", "")
        return
    try:
        messages = db.get_messages(sid)  # default = active=1 only
    except Exception:
        entry.setdefault("message_count", 0)
        entry.setdefault("decision", "")
        return
    entry["message_count"] = len(messages)
    whitelisted = [m for m in messages if _is_message_whitelisted(m)]
    entry["decision"] = _bounded_decision(whitelisted)


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #

def list_session_files(archive_dir: Path) -> list[Path]:
    """The ``.md`` session files in ``archive_dir``, in deterministic order."""
    if not archive_dir.is_dir():
        return []
    return sorted(p for p in archive_dir.iterdir() if p.is_file() and p.suffix == ".md")


def _sort_entries(entries: list[dict]) -> list[dict]:
    """Newest-first (by started), session_id tiebreak — matches discovery."""
    def key(e: dict) -> tuple:
        started = e.get("started", "") or ""
        return (started, e.get("session_id", "") or "")
    return sorted(entries, key=key, reverse=True)


def build_digest(
    name: str,
    *,
    archive_dir: str | None = None,
    mapping_path: str | None = None,
    _runtime_error_fn=None,
    _session_db=None,
) -> dict:
    """Build a bounded digest of project ``name``'s archive history.

    Reads each session's HEADER metadata (title/session_id/started/ended) from
    the per-project archive markdown files (``<archive_dir>/<name>/``), and the
    session's `message_count` and `decision` from Hermes' OWN `state.db`
    ``messages`` table via SessionDB — the exact source ``project sessions``
    uses — so digest and sessions reconcile. ``mapping_path`` is the optional
    canonical binding store. Returns a dict::

        {
          "project": name,
          "archive_dir": "<resolved absolute path>",
          "sessions": [ <entry>, ... ],   # newest first
          "total_messages": <int>,
          "runtime_error": <str | None>,  # set when Hermes runtime unimportable
        }

    Each ``entry`` is ``{title, session_id, message_count, started, ended,
    decision, resume}`` where ``resume`` is the ``hermes -p default --resume
    <id>`` line and ``message_count``/``decision`` come from ``state.db`` (the
    count of ACTIVE rows — identical to ``project sessions``). The digest NEVER
    merges or mutates sessions — it only reads. ``archive_dir``/``mapping_path``
    default to the real paths and are injectable for tests; ``_runtime_error_fn``
    injects the env-fault probe and ``_session_db`` injects the session-db
    provider (see ``_resolve_session_db``) so tests control both deterministically.
    """
    root = _resolve(archive_dir, DEFAULT_ARCHIVE_DIR)
    proj_dir = root / name

    rt = None
    if _runtime_error_fn is None:
        rt = _probe_runtime()
    else:
        rt = _runtime_error_fn()

    files = list_session_files(proj_dir)
    entries = [_parse_session_header(p) for p in files]

    # Open the DEFAULT profile's state.db read-only (sanctioned opener) once and
    # fill message_count + decision from its messages table; WITHOUT a usable
    # runtime the entries keep message_count=0 and empty decision (honest).
    db = None
    if rt is None:
        try:
            db = _resolve_session_db(_session_db)("default", read_only=True)
        except Exception:
            db = None
    try:
        for e in entries:
            _fill_from_state_db(e, db)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    # Resume line: archived (Telegram) sessions live on the DEFAULT profile.
    for e in entries:
        sid = e["session_id"]
        if sid:
            e["resume"] = f"hermes -p {DIGEST_PROFILE} --resume {sid}"
        else:
            e["resume"] = ""

    entries = _sort_entries(entries)
    total_messages = sum(e["message_count"] for e in entries)

    return {
        "project": name,
        "archive_dir": str(proj_dir.resolve()),
        "sessions": entries,
        "total_messages": total_messages,
        "runtime_error": rt,
    }


# --------------------------------------------------------------------------- #
# Human-readable text output (bounded)
# --------------------------------------------------------------------------- #

def _trim(value: str, limit: int = 80) -> str:
    v = (value or "").strip()
    return v if len(v) <= limit else v[: limit - 1] + "…"


def format_digest(digest: dict) -> str:
    """Render a digest as bounded, human-readable text (never a raw transcript).

    The whole text is capped at ``DIGEST_MAX_CHARS`` as a hard guarantee; the
    per-thread ``decision`` extracts are already individually bounded.
    """
    name = digest["project"]
    lines: list[str] = []
    lines.append(f"# project {name} — digest")
    lines.append(f"archive: {digest['archive_dir']}")

    if digest.get("runtime_error"):
        lines.append("")
        lines.append("cannot read session history: " + digest["runtime_error"])
        lines.append("  - retry under the Hermes venv, e.g.")
        lines.append("    ~/.hermes/hermes-agent/venv/bin/hscc project digest " + name)
        return "\n".join(lines)

    entries = digest.get("sessions", [])
    total_msgs = digest.get("total_messages", 0)
    if not entries:
        lines.append("")
        lines.append(f"no archived history for {name!r} (nothing under the "
                     "archive directory).")
        lines.append("  - `hscc project archive` exports Telegram history here; "
                     "`hscc project link <name> <session-id>` can add sessions.")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"{len(entries)} session(s), {total_msgs} message(s) total.")
    lines.append("")

    for e in entries:
        date_range = f"{_trim(e['started']) or '(unknown)'} .. {_trim(e['ended']) or '(open/unknown)'}"
        lines.append(f"## {e['title']}")
        lines.append(f"session: {e['session_id']}")
        lines.append(f"dates: {date_range}")
        lines.append(f"messages: {e['message_count']}")
        if e["decision"]:
            lines.append("decided / outcome:")
            for dl in e["decision"].splitlines():
                lines.append("  " + dl)
        else:
            lines.append("decided / outcome: _(no user/assistant messages in this session)_")
        if e.get("resume"):
            lines.append("resume: " + e["resume"])
        lines.append("")

    text = "\n".join(lines)
    if len(text) > DIGEST_MAX_CHARS:
        text = text[:DIGEST_MAX_CHARS]
        text += "\n[… digest truncated: over %d chars]" % DIGEST_MAX_CHARS
    return text
