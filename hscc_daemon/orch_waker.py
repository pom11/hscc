"""orch-waker W1 — the PURE CORE of the built-in orchestrator waker.

Card events (a card completing, blocking, crashing, asking for review) never
wake an orchestrator REPL today: ``kanban notify+wake`` only reaches gateway
chat platforms (zero subscribers), there is no wait tool, and the orch SOUL
bans polling — so blocked/done cards sit until someone types. This module is
the event-scan/decision half of the built-in fix (W1 of 4: W2 wires delivery,
W3 the daemon loop, W4 the docs).

REFERENCE SPEC: the operator's live stopgap ``~/bin/orch-waker``. This core is
behavior-identical to it — same event kinds, same describe formats, same
debounce/rate-limit rules, same tail-start-on-first-sight — and it reads the
stopgap's state file schema *verbatim* (``{"last", "pending", "sent"}``), so
retiring the stopgap is just pointing the daemon at ``~/.hscc/state/
orch_waker.json`` (the takeover is seamless: the first tick continues exactly
where the stopgap left off).

Purity contract (this module and nothing else in W1):
  * ZERO side effects beyond the state file — no subprocess, no tmux, no
    network, no writes to any kanban.db (every board connection is opened
    ``mode=ro``).
  * The clock is injected (``now`` argument), delivery is an injected
    callable (``inject``), orch resolution is an injected callable
    (``has_orch``), and the paused-board set is an injected collection —
    so the whole module tests offline and W2/W3 supply the real seams.
  * No delivery, no paused-file reading, no profile resolution HERE: W2
    owns those (it passes them in as arguments).

Board enumeration mirrors ``kanban_live``/``autodown`` in covering EVERY
board that holds a ``kanban.db`` under ``~/.hermes/kanban/boards/`` PLUS the
legacy ``default`` board (``~/.hermes/kanban.db``), which the stopgap missed
— per Hermes' own resolution rules ``default``'s dir only holds board
metadata, its DB stays at the legacy path. It does not reuse
``kanban_db.connect_closing`` because that opens read-write and runs
schema-init/migrations: a pure waker must not be able to mutate a board, so
this module enumerates the same board dirs and opens read-only URIs directly.
Board resolution stays injectable (``boards=`` dict or slug list) so tests
and W2 fully control the target set.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

LOG = logging.getLogger(__name__)

#: Poll cadence the daemon loop (W3) should call ``tick`` on. Exported here
#: so the stopgap's tuning lives in exactly one place in the built-in.
POLL_S = 30
#: A board nudges only once its OLDEST pending line is this old — batches a
#: burst of events into one nudge instead of one per card.
DEBOUNCE_S = 90
#: Minimum seconds between two nudges to the same board; while suppressed,
#: events keep accumulating in ``pending`` (nothing is lost).
MIN_GAP_S = 300
#: Per-board sqlite open/query budget; an unreadable board is skipped, never
#: allowed to stall the loop.
SQLITE_TIMEOUT_S = 2.0
#: Longest tail of distinct event lines in one nudge before "+N more".
MAX_LINES = 6
#: Truncation budgets (stopgap-identical).
REASON_MAX = 140
TITLE_MAX = 70

#: The event kinds an orchestrator must act on. Everything else (spawned,
#: reclaimed, decomposed, comment, heartbeat, unknown future kinds) stays
#: invisible to the waker.
KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out",
         "spawn_failed", "review_requested", "block_loop_detected")

#: Human label per kind in the nudge line. ``blocked`` is special-cased in
#: ``describe`` (it carries the block reason).
LABEL = {
    "completed": "done",
    "gave_up": "GAVE UP",
    "crashed": "CRASHED",
    "timed_out": "timed out",
    "spawn_failed": "spawn failed",
    "review_requested": "review requested",
    "block_loop_detected": "block loop",
}

DEFAULT_BOARD = "default"

#: Path TEMPLATES resolved via ``os.path.expanduser`` AT CALL TIME (never at
#: import), matching update_check.py — the suite's autouse fixture redirects
#: ``~/.hscc`` at runtime and rebinds module constants; baking expanduser in
#: here would defeat both halves of that isolation.
DEFAULT_STATE_PATH = "~/.hscc/state/orch_waker.json"
DEFAULT_BOARDS_DIR = "~/.hermes/kanban/boards"
DEFAULT_HERMES_HOME = "~/.hermes"

_STATE_KEYS = ("last", "pending", "sent")


def _resolve(path) -> str:
    return os.path.expanduser(str(path))


def empty_state() -> dict:
    """Fresh waker state — exactly the stopgap's schema, nothing more."""
    return {"last": {}, "pending": {}, "sent": {}}


def load_state(state_path=None) -> dict:
    """Read the stopgap-shaped state file; unreadable/corrupt -> fresh state.

    A fresh state means every board first-sights and tail-starts, so a lost
    state file can never cause a backlog flood. A *present but unparseable*
    file is logged, because silently resetting it would drop pending batches.
    """
    path = _resolve(state_path or DEFAULT_STATE_PATH)
    try:
        with open(path) as f:
            st = json.load(f)
    except FileNotFoundError:
        return empty_state()
    except (OSError, ValueError) as e:
        LOG.warning("orch-waker: state %s unreadable (%s) — starting fresh", path, e)
        return empty_state()
    if not isinstance(st, dict):
        LOG.warning("orch-waker: state %s is not an object — starting fresh", path)
        return empty_state()
    for key in _STATE_KEYS:
        if not isinstance(st.get(key), dict):
            st[key] = {}
    return st


def save_state(st: dict, state_path=None) -> None:
    """tmp + os.replace atomic write of the state file (stopgap mechanism)."""
    path = _resolve(state_path or DEFAULT_STATE_PATH)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, path)


def enumerate_boards(boards_dir=None, hermes_home=None) -> dict:
    """``{slug: kanban.db path}`` for every board the waker scans.

    Named boards = sub-dirs of ``~/.hermes/kanban/boards`` holding a
    ``kanban.db`` (the stopgap rule). The legacy ``default`` board is added
    with its own last-id key at ``~/.hermes/kanban.db``; a ``default`` SUBDIR
    never wins (per hermes ``kanban_db_path`` it only holds board metadata).
    Missing/unreadable dirs enumerate to nothing rather than raising.
    """
    bdir = _resolve(boards_dir or DEFAULT_BOARDS_DIR)
    home = _resolve(hermes_home or DEFAULT_HERMES_HOME)
    boards: dict = {}
    try:
        names = sorted(os.listdir(bdir))
    except OSError:
        names = []
    for name in names:
        if name == DEFAULT_BOARD:
            continue
        db = os.path.join(bdir, name, "kanban.db")
        if os.path.exists(db):
            boards[name] = db
    legacy = os.path.join(home, "kanban.db")
    if os.path.exists(legacy):
        boards[DEFAULT_BOARD] = legacy
    return boards


def scan_board(db_path, after_id):
    """New events + the board's current MAX(event id). Raises sqlite3.Error.

    ``mode=ro`` URI + 2s timeout: the waker can observe a board but never
    mutate, migrate, or even touch its WAL/schema-init path.
    """
    uri = Path(_resolve(db_path)).absolute().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=SQLITE_TIMEOUT_S)
    try:
        marks = ",".join("?" * len(KINDS))
        rows = conn.execute(
            f"""
            SELECT e.id, e.task_id, e.run_id, e.kind, e.payload, e.created_at, t.title
            FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id
            WHERE e.id > ? AND e.kind IN ({marks}) ORDER BY e.id""",
            (after_id, *KINDS)).fetchall()
        top = conn.execute("SELECT COALESCE(MAX(id),0) FROM task_events").fetchone()[0]
    finally:
        conn.close()
    return rows, top


def describe(ev):
    """One nudge line for an event row, or None when it must not wake.

    Row shape is ``scan_board``'s SELECT: (id, task_id, run_id, kind, payload,
    created_at, title). A ``blocked`` raised WITHOUT a run_id was raised by
    the orchestrator (or the operator) themselves — the orch does not need to
    be woken for its own action — so it is skipped. Worker-raised blocks
    (run_id NOT NULL) carry their payload reason, newline-flattened and cut
    to REASON_MAX.
    """
    _id, task, run_id, kind, payload, _at, title = ev
    if kind == "blocked" and run_id is None:
        return None
    label = LABEL.get(kind, kind)
    if kind == "blocked":
        try:
            reason = (json.loads(payload or "{}").get("reason") or "").replace("\n", " ")
        except (ValueError, AttributeError):
            reason = ""
        label = f"BLOCKED: {reason[:REASON_MAX]}" if reason else "BLOCKED"
    return f"{task} {label} — {(title or '')[:TITLE_MAX]}"


def build_message(board: str, pending) -> str:
    """The nudge text for one board's batch (stopgap format, byte-exact)."""
    uniq = list(dict.fromkeys(p[1] for p in pending))
    return (f"[orch-waker] board {board}: " + " · ".join(uniq[:MAX_LINES])
            + (f" · +{len(uniq) - MAX_LINES} more" if len(uniq) > MAX_LINES else "")
            + " — review/merge/unblock as needed, then carry on.")


def _clean_pending(entries):
    """Keep only well-formed ``[ts, line]`` entries (defence for hand-edited
    or half-written state the takeover inherits)."""
    out = []
    for e in entries or []:
        try:
            ts, line = e[0], e[1]
        except (TypeError, IndexError, KeyError):
            continue
        if isinstance(line, str) and isinstance(ts, (int, float)):
            out.append([ts, line])
    return out


def tick(now=None, boards=None, paused=(), has_orch=None, inject=None,
         state_path=None, boards_dir=None, hermes_home=None,
         warned=None, debounce_s=DEBOUNCE_S, min_gap_s=MIN_GAP_S):
    """One waker pass: scan every board, batch/debounce/rate-limit, decide.

    Applies and PERSISTS the state file (the only side effect); returns the
    list of decisions made this pass, each ``{"board", "msg", "lines", "ok"}``
    where ``ok`` is None when delivery is not wired (``inject=None``, the W1
    default — the decision is reported and ``pending`` is kept so the caller
    can decide), True on a confirmed delivery (pending cleared, sent stamped)
    and False on a failed one (pending kept for the next tick).

    Injectable seams (all of W2/W3 plugs in here, tests use stubs):
      * ``now``          — the clock (defaults to ``time.time()``).
      * ``boards``       — dict ``{slug: db_path}`` or an iterable of slugs
                           resolved through ``enumerate_boards``; None scans
                           every enumerated board.
      * ``paused``       — slugs whose nudges are suppressed (W2 reads
                           kanban_paused.json). Their pending batches are
                           dropped; the last-id watermark still advances.
      * ``has_orch``     — ``board -> truthy target`` (W2 resolves the
                           ``<board>-orch`` profile); falsy/None suppresses
                           delivery the same way paused does. None = assume
                           every board has an orch.
      * ``inject``       — ``callable(board, msg) -> truthy on success``
                           (W2 wires repl_inject). Exceptions count as a
                           failed delivery, never a crash.
      * ``warned``       — a caller-owned set for once-per-board warning
                           across ticks; a fresh set (once per board per
                           tick) is used when omitted.
    """
    if now is None:
        import time
        now = time.time()
    st = load_state(state_path)
    if boards is None:
        targets = enumerate_boards(boards_dir, hermes_home)
    elif isinstance(boards, dict):
        targets = dict(boards)
    else:
        allb = enumerate_boards(boards_dir, hermes_home)
        targets = {b: allb[b] for b in boards if b in allb}
    paused = set(paused or ())
    if warned is None:
        warned = set()

    decisions = []
    for b in sorted(targets):
        last = st["last"].get(b)
        try:
            rows, top = scan_board(targets[b], last if last is not None else (1 << 62))
        except sqlite3.Error as e:
            # Unreadable board: warn once (per board per run) and keep going —
            # one broken board must never stall the other 25.
            if b not in warned:
                warned.add(b)
                LOG.warning("orch-waker %s: unreadable (%s) — skipping", b, e)
            continue
        if last is None:  # first sight of this board: tail start, no backlog flood
            st["last"][b] = top
            continue
        for ev in rows:
            st["last"][b] = max(st["last"][b], ev[0])
            line = describe(ev)
            if line is not None:
                st["pending"].setdefault(b, []).append([now, line])

        target = has_orch(b) if has_orch is not None else True
        if b in paused or not target:
            st["pending"].pop(b, None)
            continue
        pend = _clean_pending(st["pending"].get(b))
        if not pend:
            st["pending"].pop(b, None)
            continue
        st["pending"][b] = pend
        if now - pend[0][0] < debounce_s or now - st["sent"].get(b, 0) < min_gap_s:
            continue
        msg = build_message(b, pend)
        decision = {"board": b, "msg": msg,
                    "lines": list(dict.fromkeys(p[1] for p in pend))}
        if inject is None:
            decisions.append(dict(decision, ok=None))
            continue
        try:
            ok = bool(inject(b, msg))
        except Exception as e:  # noqa: BLE001 — a waker never crashes on delivery
            ok = False
            LOG.warning("orch-waker %s: inject raised: %s", b, e)
        decision["ok"] = ok
        if ok:
            st["sent"][b] = now
            st["pending"].pop(b, None)
        else:
            LOG.warning("orch-waker %s: delivery FAILED — pending kept for retry", b)
        decisions.append(decision)

    save_state(st, state_path)
    return decisions
