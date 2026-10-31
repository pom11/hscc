"""orch-waker — the built-in orchestrator waker (W1 core + W2 delivery).

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
import sys
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


# =========================================================================== #
# W2 — the real seams: orch resolution, paused read, repl_inject delivery,
# JSONL audit, and the ``run_tick`` entry the daemon loop (W3) calls.
#
# ``tick`` above stays pure (injected seams only). Everything BELOW the line
# is the wiring: it reads the two stopgap rule-files, resolves the
# ``<board>-orch`` profile on disk, and delivers through the in-process
# ``repl_inject`` parity module (t_ff665ff9, hscc-api/repl_inject.py) — the
# same entry point ``orch_message`` (t_a15f3047) and the flightdeck CLI
# (hscc-project/flightdeck/core/repl_delivery.py) use. NEVER a shell-out to
# ~/bin/hermes-inject, NEVER raw tmux from here: pane discovery, the
# exactly-one-pane rule and the busy→/queue mode decision all belong to
# repl_inject and are inherited, not reimplemented.
# =========================================================================== #

#: JSONL audit log — one line per delivery ATTEMPT, append-only.
DEFAULT_AUDIT_PATH = "~/.hscc/state/orch_waker.jsonl"
#: Paused-board file (the ``hscc kanban pause`` state; keys are board slugs).
DEFAULT_PAUSED_PATH = "~/.hscc/state/kanban_paused.json"
#: How much of the failure text the audit ``outcome`` keeps (stopgap log tail).
ERROR_TAIL = 200
#: Longest nudge text kept in the audit ``msg`` (stopgap logged msg[:300]).
AUDIT_MSG_MAX = 300


def orch_name(board: str) -> str:
    """Board -> orchestrator profile NAME by convention (no disk check).

    Reuses the repo's single naming rule — ``kanban_pause.orchestrator_for``
    (``<board>-orch``), the same convention ``kanban_live`` renders and
    ``hscc kanban pause`` steers. If that helper is somehow unavailable the
    rule is reproduced verbatim, so the fallback cannot drift from it.
    """
    try:
        from . import kanban_pause
        return kanban_pause.orchestrator_for(board)
    except Exception:  # pragma: no cover — same-package import, defensive only
        return f"{board}-orch"


def resolve_orch(board: str, profiles_dir=None, hermes_home=None):
    """Board -> ``<board>-orch`` when that profile exists on disk, else None.

    The stopgap's existence rule (``~/.hermes/profiles/<board>-orch`` is a
    directory), fed by the repo-wide naming rule above — no hardcoded board
    list anywhere. A board without an orch profile is never nudged and never
    audited (there is no REPL to reach).
    """
    home = _resolve(hermes_home or DEFAULT_HERMES_HOME)
    pdir = _resolve(profiles_dir) if profiles_dir else os.path.join(home, "profiles")
    name = orch_name(board)
    return name if os.path.isdir(os.path.join(pdir, name)) else None


def read_paused(paused_path=None) -> set:
    """Paused board slugs from the ``hscc kanban pause`` state file.

    The stopgap rule (file keys = paused boards), but read through
    ``kanban_pause`` so the ``_``-prefixed bookkeeping keys the pause tool
    stores alongside boards (e.g. its saved ``auto_decompose`` value) are
    filtered exactly the way the pause tool filters them. A missing or
    unreadable file means nothing is paused — the waker never dies on it.
    """
    try:
        from . import kanban_pause
        state = kanban_pause.load_state(_resolve(paused_path or DEFAULT_PAUSED_PATH))
    except Exception as e:  # noqa: BLE001 — a waker never dies reading a rule file
        LOG.warning("orch-waker: paused file unreadable (%s) — treating as none", e)
        return set()
    return set(kanban_pause.paused_boards(state))


def load_repl_inject():
    """Import the in-process ``repl_inject`` parity module, or None.

    Same sibling-dir loader as ``api_cli._load_api_server`` /
    ``orch_message._load_repl_inject`` / ``repl_delivery._repl_inject``: the
    ``hscc-api`` dir sits beside ``hscc_daemon`` in the repo AND in the
    deployed ``~/.hermes/plugins`` layout, so one rule covers both. On a
    failed import the sys.path entry is removed again (a missing sibling
    must not leave the process with a polluted path).
    """
    try:
        import repl_inject  # noqa: F401  (already importable)
        return repl_inject
    except ImportError:
        pass
    api_dir = str(Path(__file__).resolve().parent.parent / "hscc-api")
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


def append_audit(entry: dict, audit_path=None) -> bool:
    """Append one JSONL audit line. True on success, False on failure.

    One line per attempt, never rewritten — the file is the operator's
    evidence trail (same posture as orch_message's audit log). A failure to
    write is logged but never raises: losing the audit line must never stop
    the waker mid-tick and must never lose a delivery attempt that already
    happened.
    """
    path = _resolve(audit_path or DEFAULT_AUDIT_PATH)
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except OSError as e:
        LOG.warning("orch-waker: cannot append audit line to %s (%s)", path, e)
        return False


def _outcome_text(exc) -> str:
    """Audit ``outcome`` tail for a failed attempt (bounded, one line)."""
    text = " ".join(str(exc or "delivery error").split())
    return f"failed: {text[-ERROR_TAIL:]}"


def make_inject(repl=None, mode="auto", profiles_dir=None, hermes_home=None,
                audit_path=None, clock=None):
    """Build the ``tick(inject=...)`` callable that delivers for real.

    ``(board, msg) -> bool``: resolves the board's orch profile, hands the
    text to ``repl_inject.inject(profile, msg, mode="auto")`` — the parity
    module's mode logic sends ``/queue`` while the orch is busy and a plain
    message when idle, so a nudge never cuts off a running turn — and
    appends the audit line for the attempt either way. Exceptions (InjectError
    kinds included) become ``ok=False`` + a failed audit line; the core's
    pending-retry rule then redelivers on a later tick, auditing that
    attempt too. ``repl``/``clock``/paths are injectable so tests never touch
    tmux; ``mode`` stays injectable but the built-in always uses ``auto``.
    """
    def inject(board, msg):
        profile = resolve_orch(board, profiles_dir, hermes_home) or orch_name(board)
        module = repl if repl is not None else load_repl_inject()
        if module is None:
            ok, detail = False, _outcome_text(
                RuntimeError("repl_inject not importable (hscc-api plugin missing)"))
        else:
            try:
                module.inject(profile, msg, mode=mode)
                ok, detail = True, "sent"
            except Exception as e:  # noqa: BLE001 — InjectError kinds + any tmux fail
                ok, detail = False, _outcome_text(e)
        append_audit({
            "ts": round((clock or _real_time)(), 3),
            "board": board,
            "profile": profile,
            "ok": ok,
            "outcome": detail,
            "msg": (msg or "")[:AUDIT_MSG_MAX],
        }, audit_path)
        if not ok:
            LOG.warning("orch-waker %s -> %s: %s", board, profile, detail)
        return ok
    return inject


def _real_time() -> float:
    import time
    return time.time()


#: One shared ``warned`` set for the daemon loop: "unreadable board" warns once
#: per board per *run*, matching the stopgap (tick's fresh set would re-warn
#: every 30 s tick forever).
_WARNED_ONCE = set()


def run_tick(now=None, boards=None, state_path=None, paused_path=None,
             profiles_dir=None, hermes_home=None, boards_dir=None,
             inject=None, has_orch=None, repl=None, mode="auto",
             audit_path=None, warned=None,
             debounce_s=DEBOUNCE_S, min_gap_s=MIN_GAP_S):
    """One waker pass wired end to end: scan -> decide -> inject -> audit.

    The public entry (W3 calls it every ~30 s from the daemon loop). Defaults
    plug in the real seams — paused file, ``<board>-orch`` disk resolution,
    in-process repl_inject delivery at ``mode="auto"`` and the JSONL audit —
    while every one of them stays injectable for tests. Returns ``tick``'s
    decision list; NEVER raises: any unexpected failure is logged and
    swallowed, because the daemon calls this forever and a waker that dies
    silently stops waking every orchestrator.
    """
    try:
        return tick(
            now=now,
            boards=boards,
            paused=read_paused(paused_path),
            has_orch=has_orch if has_orch is not None else (
                lambda b: resolve_orch(b, profiles_dir, hermes_home)),
            inject=inject if inject is not None else make_inject(
                repl=repl, mode=mode, profiles_dir=profiles_dir,
                hermes_home=hermes_home, audit_path=audit_path),
            state_path=state_path, boards_dir=boards_dir,
            hermes_home=hermes_home,
            warned=_WARNED_ONCE if warned is None else warned,
            debounce_s=debounce_s, min_gap_s=min_gap_s)
    except Exception:  # noqa: BLE001 — the daemon loop must survive anything
        LOG.exception("orch-waker: tick failed — will retry next tick")
        return []
