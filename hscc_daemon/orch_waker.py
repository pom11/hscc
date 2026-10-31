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


# =========================================================================== #
# W3 — operator config (``orch_waker.enabled`` + knobs) and the status read
# the CLI/doctor consume.
#
# Shape mirrors the daemon's other daemon-resident features exactly —
# autodown owns ``~/.hscc/autodown.json`` with a DEFAULT_CONFIG dict merged
# over what is on disk, api_cli owns ``~/.hscc/api.json``; the waker owns
# ``~/.hscc/orch_waker.json``. One flat JSON object, one loader, one writer,
# and the config re-read EVERY tick, so a mid-run ``hscc orch waker disable``
# takes effect on the next tick with no daemon restart (same hot-reload
# contract as ``_autodown_tick``). The card's dotted key names
# (``orch_waker.enabled``) are the FEATURE's namespace — the file itself is
# the namespace, exactly like autodown.json's own keys.
#
# ``enabled`` defaults ON, and that is the ONE deliberate difference from
# autodown, called out in the card, not an oversight: autodown fails CLOSED
# (absent/corrupt config => teardown never happens) because its action is
# destructive. The waker's action is a nudge into an idle REPL, the operator
# asked for it always-on, and a lost config file that silently stopped the
# waking is the exact failure the stopgap exists to prevent. So a missing
# file, a corrupt file, and a non-dict file all resolve to ENABLED.
# =========================================================================== #

#: The waker's config file. Written 0600 (the daemon's umask(0o077) owns the
#: whole ~/.hscc tree). Distinct from the waker's STATE file
#: (``state/orch_waker.json``) the way autodown.json is distinct from the
#: daemon's stream-state files: config is operator-owned (the CLI writes it),
#: state is daemon-owned (the tick writes it).
DEFAULT_CONFIG_PATH = "~/.hscc/orch_waker.json"

#: Defaults (W3): ON, and the stopgap's tuning verbatim. ``poll_s`` mirrors
#: ``POLL_S`` — the daemon's stream cadence and this knob are the same number
#: on purpose (see ``stream_interval``), and doctor derives its freshness
#: budget from HERE so the two cannot drift.
CONFIG_DEFAULTS = {
    "enabled": True,      # default ON (W3 card) — see the fail-open note above
    "debounce_s": DEBOUNCE_S,
    "min_gap_s": MIN_GAP_S,
    "poll_s": POLL_S,
}

#: Hard floors for the numeric knobs. Zero/negative would turn the debounce
#: into "nudge on every event" and the rate limit into "nudge every tick",
#: i.e. exactly the flood the batching rules exist to prevent.
_KNOB_FLOOR = {"debounce_s": 0, "min_gap_s": 0, "poll_s": 1}


def _coerce_knob(key, value):
    """A sane numeric value for ``key``, or the default.

    Booleans are rejected (``True`` is an ``int`` in Python; a bool in a
    seconds field is a typo, not a value). Non-finite values too — an
    ``Infinity`` rate limit would silence the board forever.
    """
    default = CONFIG_DEFAULTS[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return default
    if value < _KNOB_FLOOR[key]:
        return default
    return value


def config_path(path=None) -> str:
    """Resolved path of the daemon operator config file (expanduser at call)."""
    return _resolve(path or DEFAULT_CONFIG_PATH)


def load_config(path=None) -> dict:
    """The waker's config file merged over defaults (always a COMPLETE dict).

    Absent file, unreadable file, invalid JSON and a non-object document all
    resolve to the defaults — i.e. ENABLED, at the stopgap's tuning (the
    fail-open rationale is in the header above). Only keys in
    ``CONFIG_DEFAULTS`` are returned, so a stray hand-edit cannot reach the
    tick, and a partial file (or one written by an older build) still yields
    every knob.
    """
    cfg = dict(CONFIG_DEFAULTS)
    file = config_path(path)
    try:
        with open(file) as f:
            doc = json.load(f)
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError) as e:
        LOG.warning("orch-waker: config %s unreadable (%s) — using defaults",
                    file, e)
        return cfg
    if not isinstance(doc, dict):
        LOG.warning("orch-waker: config %s is not an object — using defaults",
                    file)
        return cfg
    cfg["enabled"] = bool(doc.get("enabled", CONFIG_DEFAULTS["enabled"]))
    for key in ("debounce_s", "min_gap_s", "poll_s"):
        cfg[key] = _coerce_knob(key, doc.get(key, CONFIG_DEFAULTS[key]))
    return cfg


def set_enabled(enabled: bool, path=None) -> dict:
    """Flip ``enabled`` in the waker's config file. Returns the new block.

    The ONLY writer of this file besides the operator's own editor, and the
    CLI never rewrites it by hand — the daemon imports this same function, so
    a flip is visible to the tick on its very next pass with no restart and no
    second bookkeeping path to drift. Writes the COMPLETE schema (merged over
    ``CONFIG_DEFAULTS``), keeps any unrelated operator key that is already in
    the file, creates file/parents when missing, and swaps atomically (tmp +
    ``os.replace``) — the same discipline ``save_state`` and
    ``autodown.save_config`` use. Raises ``OSError`` on a failed read/write so
    the CLI reports a refusal instead of claiming a flip that did not happen,
    and refuses to clobber a document that is not a JSON object.
    """
    file = config_path(path)
    doc = {}
    if os.path.exists(file):
        with open(file) as f:
            raw = f.read()
        if raw.strip():
            loaded = json.loads(raw)
            if not isinstance(loaded, dict):
                raise ValueError(
                    f"{file} is not a JSON object — refusing to rewrite it")
            doc = loaded
    merged = dict(CONFIG_DEFAULTS)
    merged.update({k: v for k, v in doc.items() if k in CONFIG_DEFAULTS})
    merged["enabled"] = bool(enabled)
    doc.update(merged)
    parent = os.path.dirname(file)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, file)
    return {k: doc[k] for k in CONFIG_DEFAULTS}


def stream_interval(cfg=None, path=None) -> int:
    """The configured ``poll_s``, clamped to a safe range.

    Bounded to ``[1, 3600]`` so a typo (``0`` => a hot loop; ``100000`` => a
    stream that never polls) cannot hurt. NOTE: this is the CONFIG value, not
    necessarily the scan cadence — the loop ticks the stream at
    ``PERIODIC_INTERVALS["orch_waker"]`` and the tick only POLLS at
    ``poll_s`` when that is LONGER than the stream cadence (a poll_s shorter
    than the cadence cannot make the waker scan faster than the loop wakes;
    see :func:`effective_scan_interval`).
    """
    cfg = cfg if cfg is not None else load_config(path)
    try:
        interval = int(cfg.get("poll_s", CONFIG_DEFAULTS["poll_s"]))
    except (TypeError, ValueError):
        interval = POLL_S
    return max(1, min(3600, interval or POLL_S))


#: Fallback for the stream cadence when ``daemon_ops`` cannot be consulted
#: (kept == its PERIODIC_INTERVALS entry; the card pins both to 30).
_STREAM_CADENCE_FALLBACK = 30


def stream_cadence() -> int:
    """The daemon loop's cadence for this stream (the SOT, 30 s).

    Reads ``daemon_ops.PERIODIC_INTERVALS`` — the same source
    ``verify._live_stream_intervals`` reads, so cadence and the staleness math
    cannot drift. Imported inside the function: daemon_ops owns the import of
    THIS module (its CHECKS table), and a module-level import in the other
    direction would only work by accident.
    """
    try:
        from .daemon_ops import PERIODIC_INTERVALS
        return int(PERIODIC_INTERVALS[STREAM_NAME])
    except Exception:  # pragma: no cover — defensive; the dict is a constant
        return _STREAM_CADENCE_FALLBACK


def effective_scan_interval(cfg=None, path=None) -> int:
    """Seconds between real board scans, honouring both cadences.

    ``max(stream cadence, poll_s)``: the loop cannot wake the stream faster
    than its cadence, and the tick will not scan more often than ``poll_s``.
    At the defaults (30 s / 30 s) this is exactly the stopgap's cadence.
    """
    return max(stream_cadence(), stream_interval(cfg, path))


# ── W3: the daemon-loop entry + the status surface ──────────────────────────

#: The daemon's stream name for the waker — the key in PERIODIC_INTERVALS
#: (daemon_ops), the state file ``~/.hscc/state/orch_waker.json`` (state.py),
#: the row ``hscc verify`` sees, and the field the doctor check reads.
#: (Also referenced by ``stream_cadence`` above.)
STREAM_NAME = "orch_waker"


def read_audit_tail(audit_path=None, lines=5) -> list:
    """Last ``lines`` audit entries as dicts (newest last), [] when absent.

    The JSONL file is the operator's evidence trail; the CLI prints the last
    few lines. Broken lines are skipped (never a crash — a half-written audit
    line must not blind the status view), a missing file reads as empty.
    """
    path = _resolve(audit_path or DEFAULT_AUDIT_PATH)
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.readlines()[-max(0, int(lines)):] if lines else []
    except (FileNotFoundError, OSError):
        return []
    out = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def waker_status(now=None, config_path=None, state_path=None,
                 audit_path=None, audit_lines=5) -> dict:
    """Everything ``hscc orch waker status`` and the doctor check render from.

    Reads (never writes): the config block, the daemon stream state file
    (last tick stamp), the waker's own state file (per-board last-id +
    pending count) and the audit tail. Pure file reads — safe to call from
    a CLI process while the daemon ticks, and it never touches a board DB.
    """
    if now is None:
        now = _real_time()
    cfg = load_config(config_path)
    from . import state as _state
    stream = _state.read_state(STREAM_NAME) or {}
    st = load_state(state_path)
    boards = {}
    for b in sorted(set(st["last"]) | set(st["pending"])):
        boards[b] = {
            "last_id": st["last"].get(b),
            "pending": len(st["pending"].get(b) or []),
        }
    last_tick = stream.get("last_tick")
    return {
        "enabled": bool(cfg["enabled"]),
        "config": cfg,
        "stream": {
            "name": STREAM_NAME,
            "interval_s": stream_cadence(),
            "scan_every_s": effective_scan_interval(cfg),
            "last_tick": last_tick,
            "tick_age_s": (round(now - last_tick, 1)
                           if isinstance(last_tick, (int, float)) else None),
            "ok": stream.get("ok"),
            "message": stream.get("message"),
        },
        "boards": boards,
        "audit": read_audit_tail(audit_path, audit_lines),
    }


def waker_tick(now=None, config_path=None, state_path=None, paused_path=None,
               profiles_dir=None, hermes_home=None, boards_dir=None,
               audit_path=None, inject=None, read=None, write=None) -> dict:
    """One supervised daemon tick: read config -> (scan | skip) -> stamp stream.

    The body of :func:`check_orch_waker` below — i.e. what the ``orch_waker``
    stream in ``daemon_ops.PERIODIC_INTERVALS`` runs every 30 s. The config is
    re-read EVERY tick — that is the whole hot-reload story, exactly like
    ``_autodown_tick``: an ``hscc orch waker disable`` from another process
    takes effect on the next tick without restarting the daemon, and so does a
    knob change.

    Three outcomes, all of which STAMP the stream (that is the point — a
    stream the daemon is deliberately idling must not read as a dead one to
    ``verify``'s staleness math):
      * disabled  -> no scan, no board read, no injection; ``skipped=disabled``.
      * poll gate -> the stream ticks every 30 s (the loop's cadence, the SOT
        verify's per-stream window is built from) but the waker only POLLS at
        ``poll_s``: when less than ``poll_s`` has elapsed since the last scan
        the tick stamps and returns without touching a board. At the default
        ``poll_s=30`` the gate never fires, so the built-in behaves exactly
        like the stopgap.
      * scan      -> :func:`run_tick` (scan -> decide -> inject -> audit) with
        the configured ``debounce_s``/``min_gap_s``.

    Returns the stamped stream entry. NEVER raises: a waker failure must not
    take the stream thread down (:func:`run_tick` already swallows its own
    errors; the guard here also covers the config/state seams around it), and
    a failed tick still stamps — with ``ok: False`` — so the failure is
    VISIBLE instead of looking like a stalled stream.
    """
    if now is None:
        now = _real_time()
    if write is None:
        from .state import write_state as write
    if read is None:
        from .state import read_state as read
    cfg = load_config(config_path)
    entry: dict = {"enabled": bool(cfg["enabled"]), "last_tick": now}
    try:
        if not cfg["enabled"]:
            entry.update(ok=True, skipped="disabled",
                         message="orch-waker disabled (orch_waker.enabled=false)")
            return entry
        poll_s = stream_interval(cfg)
        prev = read(STREAM_NAME) or {}
        # The scan gate compares against the last SCAN, falling back to the
        # last TICK (a restart-adjacent tick still honours the cadence).
        ref = prev.get("last_scan")
        if not isinstance(ref, (int, float)):
            ref = prev.get("last_tick")
        if not isinstance(ref, (int, float)):
            ref = 0.0
        since_scan = now - float(ref)
        if since_scan < poll_s:
            entry.update(ok=True, skipped="poll_gate", last_scan=prev.get("last_scan"),
                         message=f"skipped (poll {poll_s}s, {since_scan:.0f}s since scan)")
            return entry
        decisions = run_tick(
            now=now, state_path=state_path, paused_path=paused_path,
            profiles_dir=profiles_dir, hermes_home=hermes_home,
            boards_dir=boards_dir, audit_path=audit_path, inject=inject,
            debounce_s=cfg["debounce_s"], min_gap_s=cfg["min_gap_s"])
        nudged = sum(1 for d in decisions if d.get("ok") is True)
        failed = sum(1 for d in decisions if d.get("ok") is False)
        entry.update(ok=True, decisions=len(decisions), nudged=nudged,
                     delivery_failed=failed, last_scan=now,
                     message=(f"{nudged} nudged, {failed} failed, "
                              f"{len(decisions)} decided" if decisions
                              else "steady"))
        return entry
    except Exception as e:  # noqa: BLE001 — the stream must survive anything
        LOG.exception("orch-waker: daemon tick failed")
        entry.update(ok=False, message=f"tick failed: {e}")
        return entry
    finally:
        # The stamp is what keeps the stream non-stale; write_state adds its
        # own ISO `timestamp` (what verify parses), `last_tick` is the numeric
        # wall-clock this feature's status/doctor read.
        try:
            write(STREAM_NAME, entry)
        except Exception:  # pragma: no cover — write_state itself never raises
            LOG.exception("orch-waker: stream state write failed")


def check_orch_waker() -> bool:
    """The daemon's ``orch_waker`` stream check (PERIODIC_INTERVALS body).

    The CHECKS-table entry in ``daemon_ops.run_daemon_loop`` — so the waker
    rides the loop's standard per-stream machinery: its own supervised
    thread, error isolation from every other stream (a raising check is
    logged and the thread respawned, never taken down), and the same
    per-stream staleness math in ``hscc verify``. ``waker_tick`` stamps the
    stream itself on every path; this wrapper only converts the entry to the
    check-fn truth convention (``ok``) and guarantees a raise cannot escape
    even if the tick's own guard did (defence in depth, matching
    ``post_boot.check_post_boot_relaunch``).
    """
    try:
        return bool(waker_tick().get("ok"))
    except Exception as e:  # pragma: no cover — waker_tick swallows internally
        LOG.exception("orch-waker: stream check raised")
        log_waker_failure(e)
        return False


def log_waker_failure(exc) -> None:
    """Best-effort daemon-log line for a stream-check failure."""
    try:
        from .daemon_ops import log
        log(f"orch-waker check error: {exc}", "ERROR")
    except Exception:  # pragma: no cover — logging must never raise here
        pass


# ── W3: the doctor check ────────────────────────────────────────────────────
#
# ``hscc doctor``'s orch-waker verdict lives HERE rather than in
# ``hscc-bootstrap/doctor.py`` for the same reason ``doctor_node_recovery`` and
# ``doctor_notifier_mask`` are self-contained: the check reads the feature's
# OWN files through the feature's OWN loader, so the doctor can never disagree
# with the daemon about what "enabled" or "last tick" means. doctor.py adapts
# the returned dict to its five Check fields and nothing else.

#: The check name as it prints in the doctor checklist and in ``--json``.
CHECK_NAME = "orch-waker (orchestrator waker)"

#: Freshness budget: the stream must have ticked within ``2 * poll_s + this``.
#: Two full missed ticks is the same tolerance verify uses for any stream; the
#: +60 absorbs a slow tick (the daemon may be mid-sweep) without crying wolf.
DOCTOR_SLACK_S = 60

#: The daemon's pid file, as a ``~``-template resolved AT CALL TIME (never at
#: import, matching DEFAULT_STATE_PATH): the suite's autouse fixture redirects
#: ``~/.hscc`` at runtime, and a baked-in path would read the operator's live
#: daemon instead of the per-test one.
DAEMON_PID_PATH = "~/.hscc/daemon.pid"


def _daemon_pid_alive(pid_file=None) -> bool:
    """Is a daemon process alive? (pid file + ``os.kill(pid, 0)``, read-only).

    Deliberately NOT ``daemon_ops.daemon_liveness``: importing daemon_ops from
    here would be a cycle (daemon_ops imports this module for its CHECKS
    table), and the doctor only needs the one bit — is anything ticking.
    Never raises; an unreadable/absent pid file reads "not running".
    """
    path = _resolve(pid_file or DAEMON_PID_PATH)
    try:
        with open(path) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def tick_freshness_limit_s(poll_s) -> float:
    """The doctor's freshness budget for a stream ticking every ``poll_s``."""
    return 2 * float(poll_s) + DOCTOR_SLACK_S


def _daemon_predates_build(pid_file=None, build_file=None) -> bool:
    """Did the RUNNING daemon start before this build of the waker landed?

    The upgrade race the fatal verdict must never mis-fire on: doctor is
    bootstrap's Stage-1 gate and the daemon restart is a LATER stage, so
    during an upgrade the live daemon was started from code that has no
    waker stream at all. "Enabled + daemon alive + never stamped" only proves
    broken wiring when the running process is newer than the module — the
    signal available out-of-process is file mtimes: the pid file is written at
    daemon start, so ``pid mtime >= orch_waker.py mtime`` means the daemon
    could have stamped and did not (broken); an older pid means it never could
    (upgrade). Either stat failing answers False — an unverdictable check must
    not sink an install (the same fail-open posture as the not-running case).
    """
    try:
        path = _resolve(pid_file or DAEMON_PID_PATH)
        started = os.path.getmtime(path)
        built = os.path.getmtime(build_file or __file__)
    except OSError:
        return False
    return started < built


def doctor_check(now=None, config_path=None, state_dir=None,
                 stream_state=None, _daemon_running=None,
                 hscc_dir=None, _predates_build=None) -> dict:
    """Doctor verdict: is the built-in waker alive and doing its job?

    * ``disabled``                -> ok, detail says ``disabled`` (the operator
      turned it off; that is a choice, not a fault, and must not read red).
    * enabled + fresh tick        -> ok, detail names the age and the budget.
    * enabled + stale tick        -> ok=False, ``fatal=False`` → doctor prints
      it as a WARNING (○): the loop is alive but this stream is behind.
    * enabled + never ticked AND the daemon is running -> ok=False,
      ``fatal=True`` → doctor prints RED (✗): the feature is on, something is
      ticking, and this stream has never stamped, so the wiring itself is
      broken (a daemon older than this build, an import failure, wrong dir).
    * enabled + never ticked AND no daemon running -> ok=False,
      ``fatal=False``: the honest verdict is "nothing is ticking", NOT a broken
      waker. It must not be fatal: ``doctor`` is bootstrap's Stage-1 hard gate
      and runs before the daemon has ever started, so a fatal here would stop
      an install over a service that has not had a chance to run.

    Reads only (config + the stream state file), so it is safe to run while the
    daemon ticks and it never touches a board DB. ``stream_state=`` injects the
    stream entry and ``_daemon_running=`` the liveness probe (tests); otherwise
    they read ``<state_dir>/orch_waker.json`` / ``<root>/daemon.pid``.
    ``hscc_dir=`` (the ``doctor_notifier_mask`` convention — run_doctor passes
    ``os.environ.get("HSCC_DIR")``) relocates the WHOLE ``~/.hscc`` root for
    this read: config, stream state and the daemon pid file all resolve under
    it. That is what keeps a sandboxed bootstrap (HSCC_DIR = tmp, HOME = the
    operator's real one — bootstrap.sh never fakes HOME) from reading the
    OPERATOR's live daemon.pid while looking at the sandbox's missing stream
    state and firing the fatal verdict over someone else's machine. Without
    the env var every path is the plain ``~/.hscc`` default.
    ``_predates_build=`` injects the upgrade-race probe (tests; the real one
    is :func:`_daemon_predates_build`, which needs NO injection because the
    pid file it stats is the same file the default liveness probe read).
    Never raises: an unreadable state file is "never ticked", an unreadable
    config falls back to the defaults exactly as the daemon's tick does, so
    doctor and daemon always agree.
    """
    if now is None:
        now = _real_time()
    pid_file = None
    if hscc_dir:
        root = _resolve(hscc_dir)
        if config_path is None:
            config_path = os.path.join(root, "orch_waker.json")
        if state_dir is None:
            state_dir = os.path.join(root, "state")
        pid_file = os.path.join(root, "daemon.pid")
        if _daemon_running is None:
            _daemon_running = lambda: _daemon_pid_alive(pid_file)  # noqa: E731
    if _daemon_running is None:
        _daemon_running = _daemon_pid_alive
    if _predates_build is None:
        _predates_build = lambda: _daemon_predates_build(pid_file)  # noqa: E731
    cfg = load_config(config_path)
    limit = tick_freshness_limit_s(stream_interval(cfg))
    fix = ("start/restart the hscc daemon (`hscc start`) and re-run; if it "
           "stays red check ~/.hscc/daemon.log for an orch-waker error")

    if not cfg["enabled"]:
        return {"name": CHECK_NAME, "ok": True, "fatal": False,
                "detail": "disabled (orch_waker.enabled=false)",
                "fix": "re-enable with `hscc orch waker enable` when you want "
                       "orchestrators woken again"}

    if stream_state is None:
        stream_state = _read_stream_state(state_dir)
    last_tick = (stream_state or {}).get("last_tick")
    if not isinstance(last_tick, (int, float)):
        state_hint = (os.path.join(_resolve(state_dir), f"{STREAM_NAME}.json")
                      if state_dir else "~/.hscc/state/orch_waker.json")
        running = _daemon_running()   # probe ONCE: a restart mid-check must
        # not flip the verdict between the liveness bit and the age comparison.
        if running and not _predates_build():
            return {"name": CHECK_NAME, "ok": False, "fatal": True,
                    "detail": f"ENABLED but never ticked while the daemon runs "
                              f"(no stream state at {state_hint})",
                    "fix": fix}
        if _daemon_running():
            # Upgrade race, not broken wiring: doctor is Stage 1, the daemon
            # restart is a LATER stage, so this live daemon was started from
            # code without the waker stream. Warn; it goes green on its own
            # after the restart.
            return {"name": CHECK_NAME, "ok": False, "fatal": False,
                    "detail": "enabled but the RUNNING daemon predates this "
                              "build (never ticked yet) — the daemon restart "
                              "later in bootstrap starts the stream",
                    "fix": fix}
        return {"name": CHECK_NAME, "ok": False, "fatal": False,
                "detail": "enabled but the daemon is not running — nothing "
                          "ticks (start it with `hscc start`)",
                "fix": fix}

    age = now - float(last_tick)
    if age > limit:
        return {"name": CHECK_NAME, "ok": False, "fatal": False,
                "detail": f"stale: last tick {age:.0f}s ago "
                          f"(budget {limit:.0f}s = 2*{cfg['poll_s']}s+{DOCTOR_SLACK_S}s)",
                "fix": fix}
    return {"name": CHECK_NAME, "ok": True, "fatal": False,
            "detail": (f"enabled, last tick {age:.0f}s ago "
                       f"(budget {limit:.0f}s, poll {cfg['poll_s']}s)"),
            "fix": ""}


def _read_stream_state(state_dir=None) -> dict:
    """The waker stream's state entry ({} when absent/unreadable).

    Uses the daemon's own ``state`` reader when no explicit dir is given — the
    same funnel the check function writes through, including its test-time
    redirect — and a plain JSON read otherwise (doctor may be pointed at a
    throwaway tree). Never raises.
    """
    if state_dir is None:
        try:
            from .state import read_state
            return read_state(STREAM_NAME) or {}
        except Exception:  # pragma: no cover — defensive, read_state is local
            return {}
    path = os.path.join(_resolve(state_dir), f"{STREAM_NAME}.json")
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


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
