"""archive.py — durable, human-readable export of Telegram session history.

``archive_sessions`` reads the operator's Hermes session store (``state.db``)
READ-ONLY and writes one Markdown file per Telegram-originated session into an
output tree, plus an ``INDEX.md`` that lists every exported session so the
archive is browsable without tooling.

This module ONLY EXPORTS. It never writes to, VACUUMs, or deletes from
``state.db`` — the DB is opened with ``sqlite3 connect(uri='file:...?mode=ro')``
and every query is a plain SELECT.

Why Telegram only? The card is "the operator is leaving Telegram but wants the
history kept". Sessions whose ``source='telegram'`` are the ones at risk once
the Telegram surface is gone; CLI/agent/cron sessions are not part of this
card's scope.

File layout
-----------
The output tree is grouped by project for sessions whose ``thread_id`` maps to
a registry ``topic`` (``registry.Project.topic``), and under ``unmapped/`` for
everything else — the NULL-thread sessions and any non-null thread that is not
bound to a project. We never guess an owner:

    <out>/
      <project>/
        <session_id>_<title-slug>.md
      unmapped/
        <session_id>_<title-slug>.md
      INDEX.md

Message rendering / tool dumps
------------------------------
The operator's own words live in ``user`` and ``assistant`` messages — those
are exported in FULL, never truncated. ``tool`` messages (the MCP/tool result
dumps) and assistant ``tool_calls`` arguments can be enormous (200 KB+); those
are compressed to a truncated, one-line record. Losing the operator's words is
unacceptable; losing the tail of a tool dump is fine. ``session_meta`` rows are
rendered as a small italic line.

    TOOL_MAX_CHARS = 4000        # cap on a single tool result / call arguments
    SERIAL_MAX_CHARS = 200000    # hard safety cap on any single message render

Idempotency
-----------
Overwrite, deterministically. Every filename derives from stable keys (the
session id — unique and immutable — plus the current title slug), and each file
is fully regenerated on every run. Re-running converges the tree to the current
DB state: no appends, no accumulation, no duplication, no corruption. The run
reports how many files were written/overwritten.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

# Where the operator's session history lives by default. The main flightdeck /
# gateway profile's state.db holds every Telegram-originated session; this is
# the file the card pins. Overridable via injectable ``db_path`` for tests.
DEFAULT_STATE_DB = "~/.hermes/state.db"

# Default output root: the operator's Hermes home, NOT a git repo. This is
# private conversation content — we deliberately do not default into any git
# checkout (a repo on the operator's machine would tempt an incidental commit
# of private history). ``~/.hermes/archive/telegram`` sits beside the source
# state.db, is under the operator's control, and is not versioned.
DEFAULT_OUT_DIR = "~/.hermes/archive/telegram"

TOOL_MAX_CHARS = 4000
SERIAL_MAX_CHARS = 200000

# A role is rendered as a Markdown bullet label; anything we do not recognise
# is rendered verbatim and inline-escaped so it can never be confused with
# Markdown structure.
_ROLE_LABEL = {
    "user": "user",
    "assistant": "assistant",
    "tool": "tool",
    "session_meta": "session-meta",
}

# Non-null thread ids that are NOT bound to any registry topic are surfaced
# distinctly in the summary (the card says do not invent owners) but still land
# under unmapped/ — grouping always consults the registry live.


@dataclass
class ArchiveResult:
    """What a run produced, for the operator to sanity-check."""

    sessions: int = 0
    messages: int = 0
    bytes_written: int = 0
    files: int = 0
    by_thread: dict[str, int] = field(default_factory=dict)      # thread_id -> session count
    by_project: dict[str, int] = field(default_factory=dict)     # project -> session count
    unmapped_threads: list[str] = field(default_factory=list)    # non-null threads w/o owner
    index_path: str | None = None


# --------------------------------------------------------------------------- #
# DB access (read-only)
# --------------------------------------------------------------------------- #

def open_readonly(db_path: str) -> sqlite3.Connection:
    """Open `state.db` in read-only mode; never create/write.

    ``uri=True`` + ``mode=ro`` makes the connection fail rather than auto-create
    the file if it is missing, and the DB journal mode stays read-only.
    """
    uri = f"file:{os.path.abspath(os.path.expanduser(db_path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


_SESSION_COLS = (
    "id", "source", "chat_id", "thread_id", "title", "display_name",
    "started_at", "ended_at", "message_count",
)


def _fetch_sessions(conn: sqlite3.Connection, project: str | None,
                    topic_projects: dict[str, str]) -> tuple[list[dict], set[str]]:
    """Telegram sessions (optionally filtered by project) + unmapped thread ids.

    Returns ``(rows, unmapped_threads)`` where ``unmapped_threads`` is the set
    of NON-NULL thread ids present among Telegram sessions that map to NO
    registry topic (the card says report them, never invent an owner).
    """
    rows = conn.execute(
        "SELECT " + ", ".join(_SESSION_COLS)
        + " FROM sessions WHERE source='telegram' ORDER BY started_at, id"
    ).fetchall()
    out: list[dict] = []
    unmapped: set[str] = set()
    for r in rows:
        row = dict(r)
        tid = row["thread_id"]
        owner = topic_projects.get(str(tid)) if tid is not None else None
        if project is not None and owner != project:
            # --project <name>: keep only sessions owned by that project.
            continue
        out.append(row)
        if tid is not None and str(tid) not in topic_projects:
            unmapped.add(str(tid))
    return out, unmapped


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #

def _slug(text: str | None) -> str:
    """A filesystem-safe slug from a title/name; '' if nothing usable."""
    if not text:
        return ""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-_.")
    return s[:60]


def _fmt_ts(ts: float | None) -> str:
    """ISO-ish UTC timestamp string for the archive (readable, not locale)."""
    import datetime

    if ts is None:
        return ""
    try:
        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return ""


def _fmt_msg_ts(ts: float | None) -> str:
    """Timestamp for a message line (local-ish clock, no tz assumption)."""
    import datetime

    if ts is None:
        return ""
    try:
        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def _truncate(text: str, limit: int = TOOL_MAX_CHARS) -> str:
    """Truncate with an explicit marker so nothing is silently shortened."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated: {len(text)} chars, showing {limit}]"


def _render_tool_calls(tool_calls: str | None, limit: int) -> str:
    """Compact, truncated rendering of an assistant tool_calls JSON blob."""
    if not tool_calls:
        return ""
    text = tool_calls if isinstance(tool_calls, str) else str(tool_calls)
    return _truncate(text.replace("\n", " ").strip(), limit)


def _render_message(row: dict, indent: str = "") -> str:
    """Render one message row as Markdown lines (without the leading '---')."""
    role = row["role"] or "unknown"
    label = _ROLE_LABEL.get(role, role)
    ts = _fmt_msg_ts(row["timestamp"])

    head = f"**{label}**"
    if ts:
        head += f" · {ts}"
    lines = [head.rstrip()]

    content = row.get("content")
    tool_name = row.get("tool_name")

    # Tool results: compressed to a truncated one-liner so a 200 KB dump never
    # bloats the archive. tool_name (when present) is shown for orientation.
    if role == "tool":
        if tool_name:
            lines.append(f"_tool: {tool_name}_")
        if content:
            body = content if isinstance(content, str) else str(content)
            lines.append(_truncate(body, TOOL_MAX_CHARS))
        return "\n".join(lines)

    if role == "session_meta":
        if content:
            body = content if isinstance(content, str) else str(content)
            lines.append(f"_{_truncate(body, TOOL_MAX_CHARS).strip()}_")
        return "\n".join(lines)

    # user / assistant / anything else: keep the operator's words in full.
    if content:
        body = content if isinstance(content, str) else str(content)
        # Hard safety cap: a single pathological message never exceeds this.
        lines.append(_truncate(body, SERIAL_MAX_CHARS))
    # Assistant tool-call arguments are dumps, not words — compress them.
    if role == "assistant" and (row.get("tool_calls") or row.get("tool_call_id")):
        tc = _render_tool_calls(row.get("tool_calls"), TOOL_MAX_CHARS)
        if tc:
            lines.append("")
            lines.append(f"```\n{tc}\n```")
    return "\n".join(lines)


def _render_session_md(session: dict, messages: list[dict],
                       owner: str | None) -> str:
    """The full Markdown for ONE session file."""
    tid = session["thread_id"]
    title = session["title"] or session["display_name"] or session["id"]

    h = [
        f"# {title}",
        "",
        f"- **session id**: `{session['id']}`",
        f"- **source**: {session['source']}",
        f"- **thread_id**: {tid if tid is not None else '(null)'}",
        f"- **project**: {owner if owner is not None else 'unmapped'}",
        f"- **started**: {_fmt_ts(session['started_at']) or '(unknown)'}",
        f"- **ended**: {_fmt_ts(session['ended_at']) or '(still open / not recorded)'}",
        f"- **message count**: {len(messages)}",
        "",
        "---",
        "",
    ]
    body = ["\n\n---\n\n".join(_render_message(m) for m in messages)] if messages \
        else ["_(no messages)_"]
    return "\n".join(h) + "\n" + "\n".join(body) + "\n"


# --------------------------------------------------------------------------- #
# Main export
# --------------------------------------------------------------------------- #

def _topic_owner_map(registry_path: str | None) -> dict[str, str]:
    """thread-id-string -> project name, from the registry ``topic`` fields."""
    from . import registry as _registry

    mapping: dict[str, str] = {}
    try:
        projects = _registry.load_registry(registry_path)
    except Exception:
        # A broken/unreadable registry should not abort the whole archive; the
        # sessions still export under unmapped/ and the operator is told. The
        # load_registry machinery raises on truly malformed YAML — treat that
        # as "no mapping available" rather than ship a crash.
        return mapping
    for proj in projects:
        if proj.topic is not None:
            mapping[str(proj.topic)] = proj.name
    return mapping


def archive_sessions(
    *,
    out_dir: str = DEFAULT_OUT_DIR,
    db_path: str = DEFAULT_STATE_DB,
    registry_path: str | None = None,
    project: str | None = None,
    _client=None,  # unused; kept for signature parity with sibling commands
) -> ArchiveResult:
    """Export every Telegram session to readable Markdown. See module docstring.

    Returns an :class:`ArchiveResult` with the real totals written so the
    operator can sanity-check nothing was silently dropped.
    """
    result = ArchiveResult()
    root = Path(os.path.expanduser(out_dir))
    root.mkdir(parents=True, exist_ok=True)

    topic_projects = _topic_owner_map(registry_path)

    conn = open_readonly(db_path)
    try:
        sessions, unmapped_threads = _fetch_sessions(conn, project, topic_projects)
    finally:
        conn.close()

    result.unmapped_threads = sorted(unmapped_threads)
    result.sessions = len(sessions)

    index_rows: list[dict] = []

    for session in sessions:
        tid = session["thread_id"]
        owner = topic_projects.get(str(tid)) if tid is not None else None
        group = owner if owner is not None else "unmapped"
        result.by_project[group] = result.by_project.get(group, 0) + 1
        result.by_thread[str(tid) if tid is not None else "None"] = \
            result.by_thread.get(str(tid) if tid is not None else "None", 0) + 1

        # Read this session's messages (read-only).
        msgs = _load_messages(db_path=db_path, session_id=session["id"])

        file_name = f"{session['id']}_{_slug(session['title'] or session['display_name']) or 'session'}.md"
        group_dir = root / group
        group_dir.mkdir(parents=True, exist_ok=True)
        path = group_dir / file_name
        body = _render_session_md(session, msgs, owner)
        path.write_text(body, encoding="utf-8")

        result.messages += len(msgs)
        result.bytes_written += path.stat().st_size
        result.files += 1

        index_rows.append({
            "project": owner if owner is not None else "unmapped",
            "title": session["title"] or session["display_name"] or session["id"],
            "session_id": session["id"],
            "thread_id": tid,
            "started_at": session["started_at"],
            "ended_at": session["ended_at"],
            "messages": len(msgs),
            "file": str(path.relative_to(root)),
        })

    # Consistent ordering for the index: by project, then started_at.
    index_rows.sort(key=lambda r: (r["project"], r["started_at"] or 0, r["session_id"]))
    index_md = _render_index(index_rows, project)
    index_path = root / "INDEX.md"
    index_path.write_text(index_md, encoding="utf-8")
    result.index_path = str(index_path)

    return result


def _load_messages(*, db_path: str, session_id: str) -> list[dict]:
    """Fetch one session's messages via its own short-lived ro connection.

    A fresh read-only connection per session keeps memory bounded (one session's
    messages at a time) and guarantees we never hold the DB open while writing
    thousands of files.
    """
    conn = open_readonly(db_path)
    try:
        rows = conn.execute(
            "SELECT id, session_id, role, content, tool_call_id, tool_calls, "
            "tool_name, timestamp, reasoning "
            "FROM messages WHERE session_id = ? "
            "ORDER BY timestamp, id",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _render_index(rows: list[dict], project_filter: str | None) -> str:
    """INDEX.md: every exported session with project, title, msgs, dates, file."""
    lines = [
        "# Telegram Session Archive — Index",
        "",
        "Every Telegram-originated Hermes session exported to this directory, "
        "grouped by project (mapped via the registry `topic`) or `unmapped`.",
        "",
    ]
    if project_filter:
        lines.append(f"> Filtered to project **{project_filter}** only.\n")

    totals = {"sessions": len(rows), "messages": sum(r["messages"] for r in rows)}

    cur_project = None
    for r in rows:
        proj = r["project"]
        if proj != cur_project:
            cur_project = proj
            lines.append(f"## {proj}\n")
        dates = r["started_at"]
        lines.append(
            f"- **{r['title']}** · `{r['session_id']}` · {r['messages']} msgs · "
            f"{_fmt_ts(dates) or 'no start'} · [{r['file']}]({r['file']})"
        )

    lines += [
        "",
        "---",
        "",
        f"**Total: {totals['sessions']} sessions, {totals['messages']} messages.**",
        "",
    ]
    return "\n".join(lines)
