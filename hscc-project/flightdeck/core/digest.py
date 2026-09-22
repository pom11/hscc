"""digest.py — a READABLE, BOUNDED digest of a project's history.

``project digest <name>`` synthesises the project's real history into a short
human extract sized for a few thousand tokens — NEVER the raw transcripts.
The primary source is the archive markdown export, ``~/.hermes/archive/telegram/
<project>/``: one timestamped ``.md`` file per session, each holding the
session's full body (operator's user/assistant words in full, tool dumps
compressed). Because those files are the durable transcript of the project's
Telegram history, this module reads them directly.

This is a SYNTHESIS, never a merge: it reads/mutates nothing about the sessions
themselves. It never physically merges sessions (no tool_call adjacency, no
compaction-header hijack), never writes to ``state.db``, the registry, or a
kanban board. It only ever READS archive markdown files and the binding store.

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
_META_RE = {
    "session_id": re.compile(r"^\*\*session id\*\*:\s*`([^`]*)`"),
    "message_count": re.compile(r"^\*\*message count\*\*:\s*(\d+)"),
    "started": re.compile(r"^\*\*started\*\*:\s*(.*)$"),
    "ended": re.compile(r"^\*\*ended\*\*:\s*(.*)$"),
}

# A message header line, e.g. ``**user** · 2026-07-02 10:05:16``. Only user and
# assistant blocks feed the ``decision`` extract — tool dumps are never read.
_MSG_HEAD = re.compile(r"^\*\*(user|assistant)\*\*")
_MSG_HEAD_TIME = re.compile(r"^\*\*(user|assistant)\*\*\s*·\s*(.*)$")


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

def _parse_session_file(path: Path) -> dict:
    """Parse ONE archive markdown file into a digest entry.

    Returns a dict with ``title``, ``session_id``, ``message_count``, ``started``,
    ``ended`` (raw strings from the header; ``(unknown)``-style when absent) and
    ``decision`` (the bounded head/tail user+assistant extract). Never raises on
    a malformed file — everything falls back to defaults so one bad file cannot
    sink the whole digest.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    title = ""
    for ln in lines:
        if ln.startswith("# "):
            title = ln[2:].strip()
            break

    meta: dict[str, str] = {}
    in_body = False
    body_lines: list[str] = []
    for ln in lines:
        if not in_body and ln.strip() == "---":
            in_body = True
            continue
        if not in_body:
            for field, pat in _META_RE.items():
                m = pat.search(ln)
                if m:
                    meta[field] = m.group(1).strip()
                    break
        else:
            body_lines.append(ln)

    session_id = meta.get("session_id", "")
    started = meta.get("started", "")
    ended = meta.get("ended", "")
    try:
        message_count = int(meta.get("message_count") or 0)
    except ValueError:
        message_count = 0

    decision = _bounded_decision(body_lines)

    # The resume line needs a real session id to be useful.
    return {
        "title": title or path.stem,
        "session_id": session_id,
        "message_count": message_count,
        "started": started,
        "ended": ended,
        "decision": decision,
    }


def _collect_words(body_lines: list[str]) -> list[tuple[str, str]]:
    """Extract ``(role, text)`` for user/assistant messages from the body.

    A block starts at a ``**user**`` / ``**assistant**`` header and runs until
    the next ``**<role>**`` header or end of file. Content lines are joined;
    non-words (tool dumps) never appear because only user/assistant blocks are
    collected.
    """
    out: list[tuple[str, str]] = []
    cur_role: str | None = None
    cur: list[str] = []
    for ln in body_lines:
        head = _MSG_HEAD.match(ln)
        if head:
            if cur_role is not None and cur:
                out.append((cur_role, "\n".join(cur).strip()))
            cur_role = head.group(1)
            cur = []
        else:
            if cur_role is not None:
                cur.append(ln)
    if cur_role is not None and cur:
        out.append((cur_role, "\n".join(cur).strip()))
    return out


def _bounded_decision(body_lines: list[str]) -> str:
    """A short, bounded extract of the session's own words (never a full body).

    Mirrors ``map_sessions.bounded_sample``: first ``SLICE_HEAD`` + last
    ``SLICE_TAIL`` user/assistant message bodies with the omitted middle marked,
    hard-capped at ``SLICE_MAX_CHARS``. Flattens each message to a one-liner.
    """
    words = _collect_words(body_lines)
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
) -> dict:
    """Build a bounded digest of project ``name``'s archive history.

    Reads the per-project archive markdown files (``<archive_dir>/<name>/``)
    and optional canonical binding store, producing one bounded entry per
    session. Returns a dict::

        {
          "project": name,
          "archive_dir": "<resolved absolute path>",
          "sessions": [ <entry>, ... ],   # newest first
          "total_messages": <int>,
          "runtime_error": <str | None>,  # set when Hermes runtime unimportable
        }

    Each ``entry`` is ``{title, session_id, message_count, started, ended,
    decision, resume}`` where ``resume`` is the ``hermes -p default --resume
    <id>`` line. The digest NEVER merges or mutates sessions — it only reads
    archive files. ``archive_dir``/``mapping_path`` default to the real paths
    and are injectable for tests; ``_runtime_error_fn`` injects the env-fault
    probe (default: real probe) so tests control the runtime deterministically.
    """
    root = _resolve(archive_dir, DEFAULT_ARCHIVE_DIR)
    proj_dir = root / name

    rt = None
    if _runtime_error_fn is None:
        rt = _probe_runtime()
    else:
        rt = _runtime_error_fn()

    files = list_session_files(proj_dir)
    entries = [_parse_session_file(p) for p in files]

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
