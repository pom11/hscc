"""`hscc cards monitor [--live]`: one panel per running card, streaming its worker.

`hscc kanban monitor` answers "who is working on what"; this view answers "what
is each of them doing right now". Every running card on every board gets a
bordered panel that follows its worker's transcript (the dispatcher's per-task
log, ``<kanban root>/logs/<task>.log``). The grid re-flows on every refresh: a
card that starts gets a panel, a card that stops keeps its panel for
``LINGER_S`` with its outcome (done / blocked / ...) and then drops out. Cards
waiting for a slot are listed along the bottom.

Panel header: board · card · worker, then run time, heartbeat age and the
worker session's tokens — fields are dropped right-to-left when the panel is
too narrow. Body: the full transcript, or only tool calls (``t`` toggles).

Keys (live mode, interactive terminal): Tab / arrows move the focus, Enter or
``z`` zooms the focused panel to full screen, Esc leaves zoom, ``t`` toggles
full transcript / tools only, ``+``/``-`` make panels bigger/smaller (fewer /
more columns, ``0`` back to auto), ``p`` pauses scrolling, ``q`` quits. Glyph
size belongs to the terminal (iTerm: Cmd +/-), not to this view.

Read-only: card data comes from ``kanban_live.collect`` + ``attach_tokens``
(the same pass `hscc kanban monitor` makes) and logs are only read. The board,
clock, terminal and log root are injectable so the layout tests offline.
"""

from __future__ import annotations

import collections
import contextlib
import math
import os
import re
import select
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
TOOL_PREFIX = "┊"            # Hermes prints every tool call on a ┊ line
OUTCOME_STYLE = {"completed": ("done", "ok"), "blocked": ("blocked", "error"),
                 "gave_up": ("gave up", "error"), "crashed": ("crashed", "error"),
                 "timed_out": ("timed out", "error"),
                 "spawn_failed": ("spawn failed", "error"),
                 "reclaimed": ("reclaimed", "warn"),
                 "block_loop_detected": ("block loop", "error")}

# The hscc palette is all gold; a wall of ~10 transcripts in it is too loud.
# Each card is a single header rule (no box — more room for the transcript):
# the title is always gold, the rule itself gold only on the focused card;
# tool calls muted blue (explicit colours, readable on dark and light).
FOCUS_BORDER = "#FFBF00"
TITLE_STYLE = "bold #FFD700"
IDLE_BORDER = "grey35"
TOOL_STYLE = "#7A9CC6"
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


class LogTail:
    """Incremental reader of one worker log: starts at the last
    ``INITIAL_TAIL_BYTES`` and then reads only what was appended."""

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

    def view(self, tools_only: bool) -> list[str]:
        rows = list(self.lines)
        if self.partial.strip():
            rows.append(clean_line(self.partial))
        return [r for r in rows if is_tool_line(r)] if tools_only else rows


# ── state ──────────────────────────────────────────────────────────────────

class Monitor:
    """Tracks the running set across refreshes: which cards have panels, the
    log tail of each, and the lingering panels of cards that just stopped."""

    def __init__(self, board=None, kanban_db=None, hermes_home=None, clock=time.time,
                 collect=None):
        self.board = board
        self.kanban_db = kanban_db
        self.hermes_home = hermes_home
        self.clock = clock
        self._collect = collect or self._default_collect
        self.cards: dict[tuple, dict] = {}     # (board, id) -> row (+ "ended")
        self.tails: dict[tuple, LogTail] = {}
        self.order: list[tuple] = []           # stable panel order
        self.data: dict = {"running": [], "ready": [], "events": [], "caps": (None, None),
                           "errors": [], "boards": []}

    def _default_collect(self):
        return kanban_live.attach_tokens(
            kanban_live.collect(kanban_db=self.kanban_db, board=self.board),
            hermes_home=self.hermes_home)

    def refresh(self):
        now = self.clock()
        self.data = self._collect()
        live = {(r["board"], r["id"]): r for r in self.data["running"]}
        outcomes = {}
        for e in sorted(self.data.get("events", []), key=lambda e: e["at"]):
            outcomes[(e["board"], e["id"])] = e["kind"]
        for key, row in live.items():
            self.cards[key] = dict(row)
            if key not in self.tails:
                self.tails[key] = LogTail(log_path(key[0], key[1], self.hermes_home))
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


def header_fields(row: dict, now: float) -> tuple[str, list[str]]:
    """Title (``board · id · worker``) and the droppable detail fields."""
    title = f"{row['board']} · {row['id']} · {row['worker']}"
    if "ended" in row:
        label, _ = OUTCOME_STYLE.get(row.get("outcome"), (row.get("outcome", "stopped"), "warn"))
        return title, [label]
    details = [f"run {kanban_live.fmt_age(row.get('run_s'))}",
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
        elif re.search(r"\b(error|traceback|failed|exception)\b", r, re.I):
            text.append(r, style=ERROR_LINE_STYLE)
        else:
            text.append(r)
    return text


def card_panel(mon: Monitor, key: tuple, width: int, height: int, focused: bool,
               tools_only: bool, now: float) -> Group:
    """One card: a header rule carrying the title, then the transcript tail.
    No side/bottom border; one blank column on the right separates cards."""
    row = mon.cards[key]
    title, details = header_fields(row, now)
    header = fit_header(title, details, max(width - 6, 8))  # "─ " + title + " ─" + gap
    tail = mon.tails.get(key)
    lines = tail.view(tools_only) if tail else []
    body = _body(lines, max(width - 1, 1), max(height - 1, 1), tools_only)
    if "ended" in row:
        _, line_style = OUTCOME_STYLE.get(row.get("outcome"), ("", "warn"))
    else:
        line_style = FOCUS_BORDER if focused else IDLE_BORDER
    rule = Rule(Text(header, style=TITLE_STYLE), align="left", style=line_style)
    return Group(Padding(rule, (0, 1, 0, 0)), Padding(body, (0, 1, 0, 0)))


def status_line(mon: Monitor, tools_only: bool, paused: bool, zoom: bool, now: float,
                cols: int | None = None) -> Text:
    d = mon.data
    cap = d.get("caps", (None, None))[0]
    running = len(d.get("running", []))
    slots = f"{running}/{cap}" if cap else str(running)
    t = Text()
    t.append("hscc cards", style="title")
    t.append(f"  slots {slots} · waiting {len(d.get('ready', []))} · "
             f"{time.strftime('%H:%M:%S', time.localtime(now))}", style="text")
    flags = ["tools" if tools_only else "full"] + (["PAUSED"] if paused else []) \
        + (["zoom"] if zoom else []) + ([f"cols {cols}"] if cols else [])
    t.append(f"  [{' '.join(flags)}]", style="warn" if paused else MUTED)
    t.append("  tab/arrows focus · enter zoom · +/- size · t tools · p pause · q quit",
             style=MUTED)
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
           cols: int | None = None) -> Layout:
    """The whole screen as one Layout: status line, card grid, waiting cards,
    errors. Region sizes sum to ``height`` so the grid fills exactly the space
    left and the bottom lines stay on screen."""
    now = mon.clock() if now is None else now
    keys = mon.panels()
    top = status_line(mon, tools_only, paused, zoom, now, cols)
    foot = ready_lines(mon, width) + [Text(e, style="error")
                                      for e in mon.data.get("errors", [])][:2]
    grid_h = max(height - 1 - len(foot), 3)
    grid = Layout(name="grid", size=grid_h)
    if not keys:
        grid.update(Panel(Text("no cards running", style=MUTED), border_style=IDLE_BORDER))
    elif zoom:
        k = keys[focus % len(keys)]
        grid.update(card_panel(mon, k, width, grid_h, True, tools_only, now))
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
                                             tools_only, now)) for k in cells])
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
          "\x1b": "unzoom", "t": "tools", "p": "pause", "q": "quit", "\x03": "quit",
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
    def __init__(self, tools_only=False):
        self.cols: int | None = None   # None = auto grid; +/- pin a column count
        self.focus = 0
        self.zoom = False
        self.tools_only = tools_only
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
        elif action == "pause":
            self.paused = not self.paused
        elif action == "quit":
            self.quit = True


def run_live(console, interval: int = DEFAULT_INTERVAL, board: str | None = None,
             tools_only: bool = False, kanban_db=None) -> int:
    from rich.live import Live

    mon = Monitor(board=board, kanban_db=kanban_db)
    view = ViewState(tools_only)
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
                                   view.tools_only, view.paused, cols=view.cols), refresh=True)
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
             kanban_db=None) -> int:
    mon = Monitor(board=board, kanban_db=kanban_db)
    mon.refresh()
    mon.poll_logs()
    height = max(console.height, 12 * max(len(mon.panels()), 1))
    height = min(height, 200)
    console.print(render(mon, console.width, height, tools_only=tools_only), height=height)
    return 1 if mon.data["errors"] and not mon.data["boards"] else 0


# ── CLI ────────────────────────────────────────────────────────────────────

HELP = """\
hscc cards monitor [--live] [--interval <s>] [--board <slug>] [--tools]

One panel per running kanban card, streaming its worker's transcript; the
grid re-flows as cards start and finish (a finished card lingers ~10 s with
its outcome). Waiting cards are listed at the bottom. Each panel header shows
board · card · worker │ run · heartbeat · tokens (as space allows).

  --live          full-screen, refreshing until q / Ctrl-C
  --interval <s>  card-list refresh period for --live (default 3; logs stream
                  every 0.5 s regardless)
  --board <slug>  only this board
  --tools         start in tools-only view (t toggles in --live)

Keys (--live): tab/arrows focus · enter/z zoom · esc unzoom · +/- bigger
  or smaller panels (fewer/more columns; 0 = auto) · t full/tools · p
  pause · q quit. Font size itself is the terminal's (iTerm: Cmd +/-).
  Read-only: never claims, promotes or archives.
"""


def _parse(rest):
    opts = {"live": False, "interval": DEFAULT_INTERVAL, "board": None, "tools": False}
    it = iter(rest)
    for tok in it:
        if tok == "--live":
            opts["live"] = True
        elif tok == "--tools":
            opts["tools"] = True
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
        return run_live(console, opts["interval"], opts["board"], opts["tools"])
    return snapshot(console, opts["board"], opts["tools"])
