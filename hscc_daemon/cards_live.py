"""`hscc cards monitor [--live]`: one panel per running card, streaming its worker.

`hscc kanban monitor` answers "who is working on what"; this view answers "what
is each of them doing right now". Every running card on every board gets a
bordered panel that follows its worker's transcript. The grid re-flows on every
refresh: a card that starts gets a panel, a card that stops keeps its panel for
``LINGER_S`` with its outcome (done / blocked / ...) and then drops out. Cards
waiting for a slot are listed along the bottom.

Transcript source (``SessionTail`` is the primary, ``LogTail`` the fallback):
under Hermes v0.23 the dispatcher spawns ``hermes_cli.kanban_worker_client``, a
thin RPC client that writes ``<kanban root>/logs/<task>.log`` with only its own
stderr (``Session: local-<hash>``, then ``[kanban-worker-exit] rc=N`` plus the
final reply at exit). The agent itself runs inside the gateway owner as one
session per run, so the live transcript is in that session's rows in the
worker profile's ``state.db`` — which is what ``SessionTail`` follows, by the
run's own ``session_id``/``session_db`` (``kanban_live._collect_board`` takes
them from the run's ``owner_admitted`` event). The panel's header carries the
source it is reading as its first detail field (``src session`` /
``src log (fallback)``); a panel never mixes the two. The note above
``SessionTail`` records why the transcript is read with read-only ``sqlite3``
rather than through ``hermes_state``.

Panel header: board · card · worker, then run time, heartbeat age and the
worker session's tokens — fields are dropped right-to-left when the panel is
too narrow. Body: the worker's reasoning (dim italic, one logical line per row,
whole by default) above that row's reply text, plus one ``┊`` line per tool
call; ``t`` narrows a panel to the tool calls alone — no reasoning lives in that
view. ``a`` switches every transcript line between the whole text (the default)
and the short preview: thoughts keep a 320-char cap with a ``…+N more`` marker,
tool arguments are cut at 110 chars and user turns at 120, exactly as this view
always printed before whole text became the default.

Keys (live mode, interactive terminal): Tab / arrows move the focus, Enter or
``z`` zooms the focused panel to full screen, Esc leaves zoom, ``t`` full/tools,
``a`` all/short, ``+``/``-`` make panels bigger/smaller (fewer / more columns,
``0`` back to auto), ``p`` pauses scrolling, ``q`` quits. Glyph size belongs to
the terminal (iTerm: Cmd +/-), not to this view.

Read-only: card data comes from ``kanban_live.collect`` + ``attach_tokens``
(the same pass `hscc kanban monitor` makes) and logs are only read. The board,
clock, terminal and log root are injectable so the layout tests offline.
"""

from __future__ import annotations

import collections
import contextlib
import json
import math
import os
import re
import select
import sqlite3
import sys
import time

from rich.console import Group
from rich.layout import Layout
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from hscc_daemon import kanban_live
from hscc_daemon.cluster_live import fmt_tokens

DEFAULT_INTERVAL = 3         # card list refresh; logs are re-read every tick
TICK_S = 0.5
LINGER_S = 10                # a finished card's panel stays this long
INITIAL_TAIL_BYTES = 64 * 1024
MAX_LINES = 600              # kept per card; the panel shows the tail that fits
READY_ROWS = 3               # waiting-card lines at the bottom (+N more)
TOOL_PREFIX = "┊"            # the marker this view prints every tool call with
REASON_PREFIX = "░"          # the marker this view prints a row's thinking with
#: Per-row cap on the SHORT-mode reasoning preview; all mode shows the thought
#: whole. Measured on one operator store (n=5396 reasoning rows): p50 ~0.8 KB,
#: p95 ~21 KB, max ~70 KB per row, so a capped thought is what keeps a short
#: view from scrolling a ~15-row panel off the screen by itself. ``wrap_tail``
#: already bounds the width of whatever we emit; in short mode this bounds the
#: height. The marker names how much was dropped (house style: ``+N more``).
REASON_PREVIEW = 320
SESSION_PAGE = 400           # transcript rows read per tick (appended rows only)
INITIAL_SESSION_ROWS = 200   # a new session tail starts this many rows back
TOOL_PREVIEW = 110           # short-mode tool-argument preview length
PROMPT_PREVIEW = 120         # short-mode user-turn preview (hermes' timeline: 120)
ANNOUNCED_MAX = 4096         # call ids remembered so results are not repeated
#: Label + colour for the outcome a lingering panel shows. EVERY kind
#: ``kanban_live.OUTCOME_KINDS`` can deliver has an entry here (MON-23-FIELDS):
#: without one the header printed the raw snake_case kind under a blanket warn
#: rule, and the two constants lived in different files with nothing checking
#: that they agreed. ``test_every_outcome_kind_has_a_label`` pins it.
OUTCOME_STYLE = {"completed": ("done", "ok"), "blocked": ("blocked", "error"),
                 "gave_up": ("gave up", "error"), "crashed": ("crashed", "error"),
                 "timed_out": ("timed out", "error"),
                 "spawn_failed": ("spawn failed", "error"),
                 "reclaimed": ("reclaimed", "warn"),
                 "block_loop_detected": ("block loop", "error"),
                 # board bookkeeping that can legitimately be a panel's last word
                 "decomposed": ("decomposed", "warn"),
                 "spawned": ("spawned", "dim"),
                 # the kernel's own alarms (kanban_live.OUTCOME_KINDS)
                 "protocol_violation": ("protocol violation", "error"),
                 "suspected_hallucinated_references": ("hallucinated refs", "error"),
                 "completion_blocked_empty_result": ("empty result", "error"),
                 "completion_blocked_hallucination": ("hallucinated result", "error"),
                 "terminal_worker_reaped": ("worker reaped", "error"),
                 "rate_limited": ("rate limited", "warn"),
                 # the review lane: needs a person, not a pager
                 "changes_requested": ("changes requested", "warn"),
                 "review_requested": ("in review", "warn")}

# The hscc palette is all gold; a wall of ~10 transcripts in it is too loud.
# Each card is a single header rule (no box — more room for the transcript):
# the title is always gold, the rule itself gold only on the focused card;
# tool calls muted blue (explicit colours, readable on dark and light).
FOCUS_BORDER = "#FFBF00"
TITLE_STYLE = "bold #FFD700"
IDLE_BORDER = "grey35"
TOOL_STYLE = "#7A9CC6"
#: Thinking is the most voluminous thing on the panel and the least
#: load-bearing per line, so it gets the quietest style we have: grey + italic
#: separates it from the worker's actual reply text (default style) without
#: competing with the gold title or the blue tool lines.
REASON_STYLE = "italic grey58"
ERROR_LINE_STYLE = "#C97B6B"
MUTED = "grey50"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(\x07|\x1b\\)")


# ── log following ──────────────────────────────────────────────────────────

def log_path(board: str, task_id: str, hermes_home: str | None = None) -> str:
    """Where the dispatcher writes a card's worker log (default board = root)."""
    root = os.path.join(hermes_home or kanban_live.HERMES_HOME, "kanban")
    if board not in (None, "", "default"):
        root = os.path.join(root, "boards", board)
    return os.path.join(root, "logs", f"{task_id}.log")


def clean_line(line: str) -> str:
    """Strip ANSI codes and carriage-return redraws (keep the last redraw)."""
    line = _ANSI.sub("", line)
    if "\r" in line:
        parts = [p for p in line.split("\r") if p.strip()]
        line = parts[-1] if parts else ""
    return line.replace("\t", "    ").rstrip()


def is_tool_line(line: str) -> bool:
    s = line.lstrip()
    return s.startswith(TOOL_PREFIX) and "preparing " not in s


# ── session-store following (the v0.23 transcript source) ─────────────────
#
# Under Hermes v0.23 the worker's transcript is NOT in the per-task log the
# dispatcher writes; it is the run's own session in the worker profile's
# ``state.db`` (``sessions`` + ``messages``). Reading it through
# ``hermes_state`` / ``hermes_state_timeline`` was considered and rejected on
# two measurements (t_a5c49a41):
#
#   * ``hermes_state_timeline.get_session_timeline()`` is a *prompt index*, not
#     a transcript: it runs ``_display_rows_sql(..., users_only=True)`` and
#     returns ``{"entries": [{row_id, preview, timestamp}]}`` of user turns
#     collapsed to 120 chars. There is no assistant text and no tool row in it,
#     so it cannot render this panel at all (and ``db`` is a live ``SessionDB``
#     handle, not a path or a connection).
#   * importing that tree into the interpreter that ships ``hscc`` is not
#     dependable: the import pulls in ``agent.*`` → ``hermes_cli.config`` →
#     ``hermes_yaml`` → ``ruamel``, and a probe of it left the process running
#     the standalone Hermes python (at-fail ``sys.path`` had the managed
#     interpreter's stdlib and ``hermes-agent`` twice, and no hscc
#     site-packages) — the signature of ``hermes_bootstrap``'s launch
#     ``os.execv``. A view that re-renders every 0.5 s must not carry a
#     mid-render re-exec risk, and a heavy import chain buys a monitor nothing.
#
# So this reads the same rows with a read-only ``sqlite3`` handle and hermes'
# own display rules spelled out in the WHERE clause (``active``/``compacted``
# display set, model-only rows excluded) and hermes' own keyset paging
# (``get_messages(after_id=...)`` is ``id > ? ORDER BY id``), advancing a row-id
# cursor so a tick only pays for the rows appended since the last one.
# Read-only: ``mode=ro``, a 1 s busy timeout, and any ``sqlite3.Error`` is a
# blank panel, never a traceback. The one ``PRAGMA table_info`` probe per db
# file (below) rides that same read-only handle.

_MESSAGES_TABLE = "messages"


def _open_ro(path: str):
    """Read-only sqlite handle on ``path``; the caller must close it.

    ``closing()`` because sqlite3's own context manager only ends a
    transaction — a 0.5 s loop over every running card would leak handles.
    Mirrors ``kanban_live._open_ro`` on purpose (same guarantee, no import of
    another card's private helper: that module is a live hotspot this card may
    not touch).
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    return contextlib.closing(conn)


# One page of the transcript. The display set is hermes' own:
# ``(active = 1 OR compacted = 1)`` plus the model-only exclusion from
# ``hermes_state_messages.DISPLAY_VISIBLE_SQL`` — CASE so SQLite cannot
# short-circuit into ``json_extract`` on a row whose metadata is not JSON.
# ``id > :after ORDER BY id`` is hermes' keyset paging (insertion order, never
# timestamp: clocks regress).
#
# The SELECT list is a parameter because the reasoning columns are OPTIONAL:
# a store that predates them must read the same rows, so which columns are
# asked for is the only thing that ever varies. Keeping the WHERE clause in
# exactly one place is what guarantees that — reasoning is an additive column
# read, never a re-scoping of the display set.
BASE_COLUMNS = ("id", "role", "content", "tool_name", "tool_calls",
                "tool_call_id", "display_kind")
#: Preference order, and AT MOST ONE of these is ever rendered. Measured on one
#: operator store: all 5396 reasoning-bearing assistant rows fill BOTH of these
#: with text that is identical after ``TRIM`` (0 rows differ), so concatenating
#: them — or rendering whichever of the two a row carries — would print every
#: thought twice. ``reasoning_details`` / ``codex_reasoning_items`` exist but
#: hold nothing on this fleet (0 filled rows) and are deliberately not read.
REASONING_COLUMNS = ("reasoning", "reasoning_content")


def _transcript_sql(columns) -> str:
    """One page of the transcript, selecting ``columns``."""
    return f"""
    SELECT {", ".join(columns)}
    FROM {_MESSAGES_TABLE}
    WHERE session_id = :sid AND id > :after
      AND (active = 1 OR compacted = 1)
      AND CASE WHEN display_metadata IS NULL OR NOT json_valid(display_metadata)
               THEN 1 ELSE COALESCE(json_extract(display_metadata, '$.model_only'), 0) = 0 END
    ORDER BY id ASC LIMIT :limit
"""


#: The pre-reasoning form of the page: what a store without the reasoning
#: columns reads, and what any caller that passes no reasoning gets.
TRANSCRIPT_SQL = _transcript_sql(BASE_COLUMNS)

#: ``PRAGMA table_info`` for one db file is stable for the life of the reader
#: (hermes never ALTERs ``messages`` under a live reader), and the panel
#: re-opens every store every 0.5 s tick — so probe once per file and cache.
#: Bounded because the daemon is long-lived and boards/profiles come and go.
_SCHEMA_CACHE_MAX = 32
_reasoning_cache: "collections.OrderedDict[str, tuple]" = collections.OrderedDict()


def reasoning_columns(conn, path: str) -> tuple:
    """The reasoning columns this store actually has, in preference order.

    ``()`` when the store predates them (or the pragma reads empty), which
    makes the caller select ``BASE_COLUMNS`` and the panel render exactly as it
    did before reasoning existed — never a red error panel. Cached per
    resolved db path and probed at most once per file. Single-threaded by
    design, like the live loop that calls it.
    """
    key = os.path.abspath(path)
    hit = _reasoning_cache.get(key)
    if hit is not None:
        _reasoning_cache.move_to_end(key)
        return hit
    try:
        names = {row[1] for row in conn.execute(
            f"PRAGMA table_info({_MESSAGES_TABLE})")}
    except sqlite3.Error:
        names = set()
    found = tuple(c for c in REASONING_COLUMNS if c in names)
    _reasoning_cache[key] = found
    while len(_reasoning_cache) > _SCHEMA_CACHE_MAX:
        _reasoning_cache.popitem(last=False)      # oldest entry first
    return found


def _col(row, name: str):
    """``row[name]``, or None when the SELECT did not ask for that column.

    A ``sqlite3.Row`` raises ``IndexError`` and a plain dict (what the tests
    hand ``render_rows``) ``KeyError`` for an unselected column — which is
    precisely how the renderer stays correct against a store with no reasoning
    columns: the row simply has no such cell, and no line is emitted.
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return None



def _content_text(content) -> str:
    """Plain text of a ``content`` column: str, or hermes' ``\\x00json:`` parts."""
    if content is None:
        return ""
    if isinstance(content, (bytes, bytearray)):
        content = content.decode("utf-8", errors="replace")
    if isinstance(content, str):
        if content.startswith("\x00json:"):
            try:
                content = json.loads(content[len("\x00json:"):])
            except (TypeError, ValueError):
                return content[len("\x00json:"):]
    if isinstance(content, str):
        return content
    if isinstance(content, list):            # multimodal parts: keep the text ones
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    out.append(text)
        return "\n".join(out)
    return "" if content is None else str(content)


_ARG_KEYS = ("command", "path", "file_path", "pattern", "query", "url", "goal",
             "title", "note", "reason", "message", "skill", "selector", "text")


def _tool_summary(arguments) -> tuple[str, str]:
    """``(full, short)`` one line naming what a tool call was pointed at.

    Never raises. Short mode keeps the 110-char argument preview this view has
    always printed; all mode returns the whole collapsed argument.
    """
    if not isinstance(arguments, str):
        arguments = "" if arguments is None else json.dumps(arguments)
    try:
        args = json.loads(arguments)
    except (TypeError, ValueError):
        args = None
    value = ""
    if isinstance(args, dict):
        for key in _ARG_KEYS:
            v = args.get(key)
            if isinstance(v, str) and v.strip():
                value = v
                break
        else:
            for v in args.values():
                if isinstance(v, str) and v.strip():
                    value = v
                    break
    elif isinstance(args, list):
        value = " ".join(str(v) for v in args[:3])
    else:
        value = arguments
    one = " ".join(str(value).split())
    short = one[:TOOL_PREVIEW] + "…" if len(one) > TOOL_PREVIEW else one
    return one, short


def _reasoning_lines(row) -> tuple[str, str]:
    """``(full, short)`` ``░`` lines of a row's thinking, or ``("", "")``.

    Reads at most ONE column, in the ``REASONING_COLUMNS`` preference order —
    the live store fills both with the same text, so a concat or a
    ``COALESCE``-then-append would double-print every thought. The row's whole
    thought collapses to a single logical line. Short mode cuts it at
    ``REASON_PREVIEW`` with an explicit ``…+N more`` marker: the panel gives a
    card ~15 rows and a thought can be 70 KB, so one row must never own the
    screen in the short view. ``clean_line`` runs per source line
    (ANSI/redraws), then the whitespace collapses; ``wrap_tail`` still does the
    width bound downstream, exactly like every other line here.
    """
    for col in REASONING_COLUMNS:
        text = _content_text(_col(row, col))
        if not text.strip():
            continue
        collapsed = " ".join(word for part in text.splitlines()
                             for word in clean_line(part).split())
        full = f"{REASON_PREFIX} {collapsed}"
        short = collapsed
        if len(collapsed) > REASON_PREVIEW:
            dropped = len(collapsed) - REASON_PREVIEW
            short = collapsed[:REASON_PREVIEW].rstrip() + f" …+{dropped} more"
        return full, f"{REASON_PREFIX} {short}"
    return "", ""


def _row_lines(rows, announced=None) -> list[tuple[str, str]]:
    """``(full, short)`` display lines for transcript rows: thinking, then
    prose, then one ``┊`` line per call.

    A tool call is announced by the assistant row whose ``tool_calls`` names it
    — that is the moment the operator wants to see it, before the result
    arrives — so the result row (``role='tool'``) is not repeated unless it was
    never announced (a tail that starts mid-turn). ``announced`` carries the
    call ids across polls; ``role='user'`` rows become one collapsed line.

    Both text variants are built here, once, when the rows are read: the stored
    entry can then switch between all and short without re-reading the store or
    rebuilding the text. Reasoning belongs to the row that thought it, so only
    the assistant branch emits it — and the tools-only view stays empty of it
    for free: a ``REASON_PREFIX`` line never passes :func:`is_tool_line`.
    """
    seen = announced if announced is not None else set()
    out: list[tuple[str, str]] = []
    for row in rows:
        role = row["role"]
        if role == "assistant":
            # Thinking leads: it is what preceded the words, and usually what
            # preceded the tool call on the same row, so it reads as the row's
            # lead-in rather than a footnote.
            full, short = _reasoning_lines(row)
            if full:
                out.append((full, short))
            text = _content_text(row["content"])
            for para in text.split("\n"):
                para = clean_line(para)
                if para.strip():
                    out.append((para, para))
            calls = row["tool_calls"]
            if isinstance(calls, str):
                try:
                    calls = json.loads(calls)
                except (TypeError, ValueError):
                    calls = None
            for call in calls or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                name = fn.get("name") or call.get("name") or "tool"
                cid = call.get("id") or call.get("call_id")
                if cid:
                    seen.add(cid)
                full_arg, short_arg = _tool_summary(fn.get("arguments"))
                head = f"{TOOL_PREFIX} {name}  "
                out.append((f"{head}{full_arg}".rstrip(),
                            f"{head}{short_arg}".rstrip()))
        elif role == "tool":
            if row["tool_call_id"] and row["tool_call_id"] in seen:
                continue
            line = f"{TOOL_PREFIX} {row['tool_name'] or 'tool'}".rstrip()
            out.append((line, line))
        elif role == "user":
            one = " ".join(_content_text(row["content"]).split())
            if one:
                short = (one[:PROMPT_PREVIEW] + "…"
                         if len(one) > PROMPT_PREVIEW else one)
                out.append(("▸ " + one, "▸ " + short))
    return out


def render_rows(rows, announced=None, short: bool = False) -> list[str]:
    """Display lines for transcript rows, in the requested text mode.

    ``short=False`` (the default) shows thoughts, tool arguments and user turns
    whole; ``short=True`` is exactly the preview this view printed before all
    mode existed. See :func:`_row_lines` for the row rules and the ``announced``
    set, which carries tool-call ids across polls.
    """
    return [s if short else f for f, s in _row_lines(rows, announced)]


class SessionTail:
    """Incremental reader of one run's transcript from the session store.

    Starts at the last ``INITIAL_SESSION_ROWS`` rows and then reads only the
    rows appended since (``id > cursor``). A missing/unreadable store, a
    session id that the store has not seen yet or a row set that moved
    backwards (a rewritten/rewound store) never raise — they restart the tail
    or add nothing, exactly the guarantees ``LogTail.poll`` gives for a log file.
    """

    source = "session"
    label = "session"

    def __init__(self, db_path: str, session_id: str, page: int = SESSION_PAGE):
        self.path = db_path
        self.session_id = session_id
        self.page = page
        self.cursor = None            # None = not positioned yet
        self.announced: set[str] = set()
        #: ``(full, short)`` per display line, both built once on read.
        self.lines: collections.deque[tuple[str, str]] = \
            collections.deque(maxlen=MAX_LINES)

    def _max_id(self, conn) -> int | None:
        row = conn.execute(f"SELECT MAX(id) AS hi FROM {_MESSAGES_TABLE} "
                           f"WHERE session_id = ?", (self.session_id,)).fetchone()
        return row["hi"] if row and row["hi"] is not None else None

    def poll(self) -> bool:
        """Read appended rows; True when display lines were added."""
        if not self.path or not self.session_id or not os.path.isfile(self.path):
            return False
        try:
            with _open_ro(self.path) as conn:
                # One pragma per db file, cached (see ``reasoning_columns``): a
                # store that predates the reasoning columns selects
                # BASE_COLUMNS and renders exactly as it did before, which is
                # why a missing column can never reach SQL as an error panel.
                cols = reasoning_columns(conn, self.path)
                hi = self._max_id(conn)
                if hi is None:
                    return False            # the session has no rows yet
                if self.cursor is None or hi < self.cursor:
                    # First look, or the row ids moved backwards (a store that
                    # was rewritten or rewound): restart the tail from its end
                    # instead of raising, and forget the ids that anchored
                    # result lines — they may no longer exist.
                    self.cursor = max(0, hi - self.page)
                    self.announced.clear()
                if hi == self.cursor:
                    return False            # nothing appended since the last tick
                rows = conn.execute(_transcript_sql(BASE_COLUMNS + cols),
                                    {"sid": self.session_id, "after": self.cursor,
                                     "limit": self.page}).fetchall()
        except (sqlite3.Error, OSError):
            return False                    # unreadable store = no new lines
        added = False
        if rows:
            lines = _row_lines(rows, self.announced)
            if len(self.announced) > ANNOUNCED_MAX:
                self.announced.clear()      # bounded; costs at most one repeat line
            for entry in lines:
                self.lines.append(entry)
            added = bool(lines)
        self.cursor = rows[-1]["id"] if rows else hi
        return added

    def view(self, tools_only: bool, short: bool = False) -> list[str]:
        rows = list(self.lines)
        if tools_only:
            rows = [r for r in rows if is_tool_line(r[0])]
        return [r[1 if short else 0] for r in rows]


def _session_of(row: dict) -> str | None:
    """The run's session id that ``kanban_live._collect_board`` put on the row,
    or None when the owner has not admitted this run (yet)."""
    sid = row.get("session_id")
    return sid.strip() if isinstance(sid, str) and sid.strip() else None


def make_tail(row: dict, hermes_home: str | None, board: str, task_id: str):
    """The transcript follower for one running row.

    A session id on the row (the run's ``owner_admitted`` event) selects the
    session store — ``session_db`` when the owner named one, else the worker
    profile's own ``state.db`` (``_collect_board`` cannot know the caller's
    hermes home, so it hands the id and lets the reader resolve the path).
    Without a session id the panel degrades to the worker LOG file, which under
    v0.23 carries only the RPC client's stderr; ``tail.source`` is what the
    header prints so the operator can tell which one they are reading.
    """
    sid = _session_of(row)
    if sid:
        path = row.get("session_db") or kanban_live._state_db_path(
            hermes_home or kanban_live.HERMES_HOME, row.get("worker"))
        return SessionTail(path, sid)
    return LogTail(log_path(board, task_id, hermes_home))


class LogTail:
    """Incremental reader of one worker log: starts at the last
    ``INITIAL_TAIL_BYTES`` and then reads only what was appended."""

    source = "log"

    def __init__(self, path: str):
        self.path = path
        self.offset = None
        self.partial = ""
        self.lines: collections.deque[str] = collections.deque(maxlen=MAX_LINES)

    def poll(self) -> bool:
        """Read new bytes; True when lines were added. A missing or
        truncated file never raises (it just restarts the tail)."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return False
        if self.offset is None or size < self.offset:
            self.offset = max(0, size - INITIAL_TAIL_BYTES)
            self.partial, first = "", True
        else:
            first = False
        if size == self.offset:
            return False
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read(size - self.offset)
        except OSError:
            return False
        self.offset += len(chunk)
        text = self.partial + chunk.decode("utf-8", errors="replace")
        rows = text.split("\n")
        self.partial = rows.pop()
        if first and self.offset - len(chunk) > 0 and rows:
            rows = rows[1:]  # the first row of a mid-file tail is a fragment
        for r in rows:
            self.lines.append(clean_line(r))
        return bool(rows)

    def view(self, tools_only: bool, short: bool = False) -> list[str]:
        # A log file has no row caps, so both text modes return the same lines.
        rows = list(self.lines)
        if self.partial.strip():
            rows.append(clean_line(self.partial))
        return [r for r in rows if is_tool_line(r)] if tools_only else rows


# ── state ──────────────────────────────────────────────────────────────────

class Monitor:
    """Tracks the running set across refreshes: which cards have panels, the
    transcript tail of each, and the lingering panels of cards that just
    stopped. Each panel follows ONE source (session store, else worker log);
    a panel switches source only when the run's session id appears, and then
    it starts that source clean rather than continuing the old one."""

    def __init__(self, board=None, kanban_db=None, hermes_home=None, clock=time.time,
                 collect=None):
        self.board = board
        self.kanban_db = kanban_db
        self.hermes_home = hermes_home
        self.clock = clock
        self._collect = collect or self._default_collect
        self.cards: dict[tuple, dict] = {}     # (board, id) -> row (+ "ended")
        self.tails: dict[tuple, "LogTail | SessionTail"] = {}
        self.order: list[tuple] = []           # stable panel order
        self.data: dict = {"running": [], "ready": [], "events": [], "outcomes": {},
                           "caps": (None, None), "errors": [], "boards": []}

    def _default_collect(self):
        return kanban_live.attach_tokens(
            kanban_live.collect(kanban_db=self.kanban_db, board=self.board),
            hermes_home=self.hermes_home)

    def _outcomes(self) -> dict:
        """``{(board, card): kind}`` for every card that had an outcome in the
        last hour, newest kind each.

        ``collect()``'s ``outcomes`` is the authority: it answers the question for
        every card. ``data["events"]`` is the panel's newest-``RECENT_LIMIT`` rows
        shared across every board, which cannot answer it for a card that stopped
        behind 12 busier events — that card used to linger as a bare ``stopped``.
        The events pass stays as the fallback so a collector that predates
        ``outcomes`` (or a test stub) still labels what it can.
        """
        outcomes = {}
        for board, by_card in (self.data.get("outcomes") or {}).items():
            for card_id, kind in (by_card or {}).items():
                outcomes[(board, card_id)] = kind
        for e in sorted(self.data.get("events", []), key=lambda e: e["at"]):
            outcomes.setdefault((e["board"], e["id"]), e["kind"])
        return outcomes

    def refresh(self):
        now = self.clock()
        self.data = self._collect()
        live = {(r["board"], r["id"]): r for r in self.data["running"]}
        outcomes = self._outcomes()
        for key, row in live.items():
            self.cards[key] = dict(row)
            tail = self.tails.get(key)
            wanted = "session" if _session_of(row) else "log"
            if tail is None or tail.source != wanted:
                # New panel — or the owner admitted this run since the last
                # tick, so the session became readable. Replacing the tail is
                # what keeps the two sources from ever sharing one panel.
                self.tails[key] = make_tail(row, self.hermes_home, key[0], key[1])
                if tail is None:
                    self.order.append(key)
        for key, row in list(self.cards.items()):
            if key in live:
                continue
            if "ended" not in row:
                row["ended"] = now
                row["outcome"] = outcomes.get(key, "stopped")
            elif now - row["ended"] >= LINGER_S:
                self.cards.pop(key)
                self.tails.pop(key, None)
                self.order.remove(key)

    def poll_logs(self):
        for t in self.tails.values():
            t.poll()

    def panels(self) -> list[tuple]:
        return [k for k in self.order if k in self.cards]


# ── rendering ──────────────────────────────────────────────────────────────

def grid_shape(n: int, width: int, height: int, cols: int | None = None) -> tuple[int, int]:
    """``(cols, rows)`` for ``n`` panels: as square as the terminal allows,
    counting a cell as ~2.2x taller than wide. ``cols`` forces the column
    count (the +/- density zoom), clamped to ``1..n``."""
    if n <= 0:
        return 0, 0
    if cols is None:
        aspect = max(width, 1) / max(height * 2.2, 1)
        cols = round(math.sqrt(n * aspect))
    cols = max(1, min(n, cols))
    rows = math.ceil(n / cols)
    cols = math.ceil(n / rows)          # drop empty columns
    return cols, rows


def header_fields(row: dict, now: float, source: str | None = None) -> tuple[str, list[str]]:
    """Title (``board · id · worker``) and the droppable detail fields.

    ``source`` — ``session`` (the run's transcript in the session store) or
    ``log`` (the worker log, the fallback) — goes FIRST so it is the LAST thing
    ``fit_header`` drops: a panel that is quietly reading the v0.23 RPC client's
    stderr instead of the worker's transcript has to stay visible as such.
    """
    title = f"{row['board']} · {row['id']} · {row['worker']}"
    if "ended" in row:
        label, _ = OUTCOME_STYLE.get(row.get("outcome"), (row.get("outcome", "stopped"), "warn"))
        return title, [label]
    details = []
    if source == "session":
        details.append("src session")
    elif source == "log":
        details.append("src log (fallback)")
    details += [f"run {kanban_live.fmt_age(row.get('run_s'))}",
                f"beat {kanban_live.fmt_age(row.get('hb_s'))}"]
    if row.get("tok_in") is not None:
        details.append(f"tok {fmt_tokens(row['tok_in'])}/{fmt_tokens(row.get('tok_out'))}")
    return title, details


def fit_header(title: str, details: list[str], width: int) -> str:
    """Title plus as many details as fit ``width`` (drop right-to-left)."""
    keep = list(details)
    while keep:
        s = f"{title} │ {' · '.join(keep)}"
        if len(s) <= width:
            return s
        keep.pop()
    return title if len(title) <= width else title[: max(1, width - 1)] + "…"


def wrap_tail(lines: list[str], width: int, height: int) -> list[str]:
    """The last ``height`` visual rows of ``lines`` hard-wrapped to ``width``."""
    out: list[str] = []
    width = max(width, 1)
    for line in reversed(lines):
        chunks = [line[i:i + width] for i in range(0, len(line), width)] or [""]
        out[:0] = chunks
        if len(out) >= height:
            break
    return out[-height:] if height > 0 else []


def _body(lines: list[str], width: int, height: int, tools_only: bool) -> Text:
    text = Text(no_wrap=True, overflow="crop")
    rows = wrap_tail(lines, width, height)
    if not rows:
        text.append("(waiting for output)" if not tools_only else "(no tool calls yet)",
                    style=MUTED)
        return text
    for i, r in enumerate(rows):
        if i:
            text.append("\n")
        s = r.lstrip()
        if s.startswith(TOOL_PREFIX):
            text.append(r, style=TOOL_STYLE)
        elif s.startswith(REASON_PREFIX):
            # BEFORE the error rule: a thought about a failing test says
            # "error" a lot and must not come back in the warning colour.
            # (A reply line that genuinely opens with this glyph is styled as
            # if it were thinking — cosmetic only, and the same trade the
            # tool-prefix rule already makes.)
            text.append(r, style=REASON_STYLE)
        elif re.search(r"\b(error|traceback|failed|exception)\b", r, re.I):
            text.append(r, style=ERROR_LINE_STYLE)
        else:
            text.append(r)
    return text


def card_panel(mon: Monitor, key: tuple, width: int, height: int, focused: bool,
               tools_only: bool, now: float, short: bool = False) -> Group:
    """One card: a header rule carrying the title, then the transcript tail.
    No side/bottom border; one blank column on the right separates cards."""
    row = mon.cards[key]
    tail = mon.tails.get(key)
    title, details = header_fields(row, now, tail.source if tail else None)
    header = fit_header(title, details, max(width - 6, 8))  # "─ " + title + " ─" + gap
    lines = tail.view(tools_only, short) if tail else []
    body = _body(lines, max(width - 1, 1), max(height - 1, 1), tools_only)
    if "ended" in row:
        _, line_style = OUTCOME_STYLE.get(row.get("outcome"), ("", "warn"))
    else:
        line_style = FOCUS_BORDER if focused else IDLE_BORDER
    rule = Rule(Text(header, style=TITLE_STYLE), align="left", style=line_style)
    return Group(Padding(rule, (0, 1, 0, 0)), Padding(body, (0, 1, 0, 0)))


def status_line(mon: Monitor, tools_only: bool, paused: bool, zoom: bool, now: float,
                cols: int | None = None, short: bool = False) -> Text:
    d = mon.data
    cap = d.get("caps", (None, None))[0]
    running = len(d.get("running", []))
    slots = f"{running}/{cap}" if cap else str(running)
    t = Text()
    t.append("hscc cards", style="title")
    t.append(f"  slots {slots} · waiting {len(d.get('ready', []))} · "
             f"{time.strftime('%H:%M:%S', time.localtime(now))}", style="text")
    flags = ["tools" if tools_only else "full"] \
        + (["short"] if short else []) \
        + (["PAUSED"] if paused else []) \
        + (["zoom"] if zoom else []) + ([f"cols {cols}"] if cols else [])
    t.append(f"  [{' '.join(flags)}]", style="warn" if paused else MUTED)
    t.append("  tab/arrows focus · enter zoom · +/- size · t tools · a all/short"
             " · p pause · q quit", style=MUTED)
    return t


def ready_lines(mon: Monitor, width: int) -> list[Text]:
    ready = mon.data.get("ready", [])
    out = []
    for r in ready[:READY_ROWS]:
        s = (f"⏳ {r['board']} · {r['id']} · {r['worker']} · wait "
             f"{kanban_live.fmt_age(r.get('wait_s'))} · {r['title']}")
        out.append(Text(s[:max(width, 10)], style=MUTED, no_wrap=True, overflow="crop"))
    if len(ready) > READY_ROWS:
        out.append(Text(f"   +{len(ready) - READY_ROWS} more waiting", style=MUTED))
    return out


def render(mon: Monitor, width: int, height: int, focus: int = 0, zoom: bool = False,
           tools_only: bool = False, paused: bool = False, now: float | None = None,
           cols: int | None = None, short: bool = False) -> Layout:
    """The whole screen as one Layout: status line, card grid, waiting cards,
    errors. Region sizes sum to ``height`` so the grid fills exactly the space
    left and the bottom lines stay on screen."""
    now = mon.clock() if now is None else now
    keys = mon.panels()
    top = status_line(mon, tools_only, paused, zoom, now, cols, short)
    errors = mon.data.get("errors", [])
    blind = not mon.data.get("boards") and bool(errors)
    foot = ready_lines(mon, width)
    if errors and not blind:
        foot += [Text(e, style="error") for e in errors][:2]
    grid_h = max(height - 1 - len(foot), 3)
    grid = Layout(name="grid", size=grid_h)
    if blind:
        # A blind board read is not an idle fleet: never paint "no cards
        # running" over errors that mean nothing could be read (t_a594ce03).
        grid.update(kanban_live.board_error_panel(errors))
    elif not keys:
        grid.update(Panel(Text("no cards running", style=MUTED), border_style=IDLE_BORDER))
    elif zoom:
        k = keys[focus % len(keys)]
        grid.update(card_panel(mon, k, width, grid_h, True, tools_only, now, short))
    else:
        focus %= len(keys)
        cols, rows = grid_shape(len(keys), width, grid_h, cols)
        row_layouts = []
        for r in range(rows):
            cells = keys[r * cols:(r + 1) * cols]
            row_h = grid_h // rows + (1 if r < grid_h % rows else 0)
            cell_w = width // max(len(cells), 1)
            rl = Layout(size=row_h)
            rl.split_row(*[Layout(card_panel(mon, k, cell_w, row_h, keys.index(k) == focus,
                                             tools_only, now, short)) for k in cells])
            row_layouts.append(rl)
        grid.split_column(*row_layouts)
    root = Layout()
    parts = [Layout(top, size=1), grid]
    if foot:
        parts.append(Layout(Group(*foot), size=len(foot)))
    root.split_column(*parts)
    return root


# ── keys + live loop ───────────────────────────────────────────────────────

KEYMAP = {"\t": "next", "\x1b[C": "next", "\x1b[B": "next", "\x1b[Z": "prev",
          "\x1b[D": "prev", "\x1b[A": "prev", "\r": "zoom", "\n": "zoom", "z": "zoom",
          "\x1b": "unzoom", "t": "tools", "a": "short", "p": "pause",
          "q": "quit", "\x03": "quit",
          "+": "bigger", "=": "bigger", "-": "smaller", "_": "smaller", "0": "auto"}


def read_key(fd, timeout: float) -> str | None:
    """One key (arrow escape sequences included) or None after ``timeout``."""
    r, _, _ = select.select([fd], [], [], timeout)
    if not r:
        return None
    ch = os.read(fd, 1).decode(errors="ignore")
    if ch != "\x1b":
        return ch
    seq = ch
    while select.select([fd], [], [], 0.02)[0] and len(seq) < 6:
        seq += os.read(fd, 1).decode(errors="ignore")
    return seq


@contextlib.contextmanager
def _cbreak(fd):
    import termios
    import tty
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


class ViewState:
    def __init__(self, tools_only=False, short=False):
        self.cols: int | None = None   # None = auto grid; +/- pin a column count
        self.focus = 0
        self.zoom = False
        self.tools_only = tools_only
        self.short = short             # False = whole text, True = short preview
        self.paused = False
        self.quit = False

    def apply(self, action: str | None, n: int, auto_cols: int = 1):
        """``auto_cols`` = the column count the auto grid uses right now, so
        the first +/- steps from what is on screen."""
        if action in ("bigger", "smaller"):
            cur = self.cols or auto_cols
            step = -1 if action == "bigger" else 1      # bigger panels = fewer columns
            self.cols = max(1, min(max(n, 1), cur + step))
        elif action == "auto":
            self.cols = None
        elif action == "next" and n:
            self.focus = (self.focus + 1) % n
        elif action == "prev" and n:
            self.focus = (self.focus - 1) % n
        elif action == "zoom":
            self.zoom = not self.zoom
        elif action == "unzoom":
            self.zoom = False
        elif action == "tools":
            self.tools_only = not self.tools_only
        elif action == "short":
            self.short = not self.short
        elif action == "pause":
            self.paused = not self.paused
        elif action == "quit":
            self.quit = True


def run_live(console, interval: int = DEFAULT_INTERVAL, board: str | None = None,
             tools_only: bool = False, kanban_db=None, short: bool = False) -> int:
    from rich.live import Live

    mon = Monitor(board=board, kanban_db=kanban_db)
    view = ViewState(tools_only, short)
    fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    keys_ctx = _cbreak(fd) if fd is not None else contextlib.nullcontext()
    next_refresh = 0.0
    try:
        with keys_ctx, Live(console=console, auto_refresh=False, screen=True) as live:
            while not view.quit:
                now = time.time()
                if now >= next_refresh:
                    mon.refresh()
                    next_refresh = now + interval
                if not view.paused:
                    mon.poll_logs()
                live.update(render(mon, console.width, console.height, view.focus, view.zoom,
                                   view.tools_only, view.paused, cols=view.cols,
                                   short=view.short), refresh=True)
                if fd is None:
                    time.sleep(TICK_S)
                    continue
                key = read_key(fd, TICK_S)
                n = len(mon.panels())
                auto = grid_shape(n, console.width, max(console.height - 5, 3))[0]
                view.apply(KEYMAP.get(key) if key else None, n, auto)
    except KeyboardInterrupt:
        pass
    return 0


def snapshot(console, board: str | None = None, tools_only: bool = False,
             kanban_db=None, short: bool = False) -> int:
    mon = Monitor(board=board, kanban_db=kanban_db)
    mon.refresh()
    mon.poll_logs()
    height = max(console.height, 12 * max(len(mon.panels()), 1))
    height = min(height, 200)
    console.print(render(mon, console.width, height, tools_only=tools_only, short=short),
                  height=height)
    return 1 if mon.data["errors"] and not mon.data["boards"] else 0


# ── CLI ────────────────────────────────────────────────────────────────────

HELP = """\
hscc cards monitor [--live] [--interval <s>] [--board <slug>]
                  [--tools] [--short]

One panel per running kanban card, streaming its worker's transcript — each
row's thinking (dim italic, one logical line, whole text by default) above its
reply text, and one ┊ line per tool call; the grid re-flows as cards start and
finish (a finished card lingers ~10 s with its outcome). Waiting cards are
listed at the bottom. Each panel header shows board · card · worker │ run ·
heartbeat · tokens (as space allows).

  --live          full-screen, refreshing until q / Ctrl-C
  --interval <s>  card-list refresh period for --live (default 3; logs stream
                  every 0.5 s regardless)
  --board <slug>  only this board
  --tools         start in tools-only view (t toggles in --live; the tools-only
                  view shows no reasoning)
  --short         start in short preview mode (a toggles in --live; thoughts,
                  tool arguments and user turns are capped and thoughts end
                  with a …+N more marker)

Keys (--live): tab/arrows focus · enter/z zoom · esc unzoom · +/- bigger
  or smaller panels (fewer/more columns; 0 = auto) · t full/tools ·
  a all/short · p pause · q quit. Font size itself is the terminal's
  (iTerm: Cmd +/-). Read-only: never claims, promotes or archives.
"""


def _parse(rest):
    opts = {"live": False, "interval": DEFAULT_INTERVAL, "board": None, "tools": False,
            "short": False}
    it = iter(rest)
    for tok in it:
        if tok == "--live":
            opts["live"] = True
        elif tok == "--tools":
            opts["tools"] = True
        elif tok == "--short":
            opts["short"] = True
        elif tok in ("--interval", "--board"):
            val = next(it, None)
            if val is None:
                return opts, f"{tok} needs a value"
            if tok == "--board":
                opts["board"] = val
                continue
            try:
                opts["interval"] = int(val)
            except ValueError:
                return opts, f"--interval must be whole seconds, got {val!r}"
            if opts["interval"] < 1:
                return opts, "--interval must be >= 1"
        else:
            return opts, f"unknown argument for 'hscc cards monitor': {tok}"
    return opts, None


def cmd_cards(argv, console=None) -> int:
    if not argv or argv[0] in ("-h", "--help", "help") or "-h" in argv or "--help" in argv:
        print(HELP, end="")
        return 0
    if argv[0] != "monitor":
        print(f"unknown cards subcommand: {argv[0]}\nValid subcommands: monitor",
              file=sys.stderr)
        return 2
    opts, err = _parse(argv[1:])
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    if console is None:
        from hscc_daemon.cli_theme import make_console
        console = make_console()
    if opts["live"]:
        return run_live(console, opts["interval"], opts["board"], opts["tools"],
                        short=opts["short"])
    return snapshot(console, opts["board"], opts["tools"], short=opts["short"])
