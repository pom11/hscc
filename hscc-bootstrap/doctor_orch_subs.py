"""Doctor coverage for kanban wake subscriptions (card t_639b2834).

One check, answering one question: **is any ``notify+wake`` subscription owned by
a pane-backed profile pointed at a session other than that profile's live pane
session?**

Why this is drift
-----------------
A profile whose live REPL sits in a tmux pane has exactly one sanctioned wake
path: the pane plus the hscc orch-waker (``hscc_daemon/orch_waker.py``). A
gateway ``notify+wake`` subscription owned by that same profile is a SECOND
driver of the same identity — the gateway takes a turn in a gateway session
while the real pane REPL is idle, and both work the boards. That is the
2026-11-23 incident: 307 ``notify+wake`` subs (boards ``hscc`` 242, ``general``
47, ``headless-display`` 18) all owned by one orchestrator profile and all
targeting one gateway session while the real orchestrator sat in a pane. It ran
for about two weeks, cost on the order of 460M input tokens, and the duplicate
minted duplicate cards (the same concern twice, on two boards). The operator
removed the subscriptions; the dated backups under
``~/fleet-check/backups/notify-subs-*-20261123.txt`` must never be restored.

The subscribe-side fix is ``hscc-roles/orch_subscribe.py`` (card t_31c8b258):
it turns Hermes' ``kanban.auto_subscribe_on_create`` gate OFF for a pane-backed
orchestrator. This module is the regression alarm behind it — the gate is new
and a pane can appear after a profile was provisioned, so the posture has to be
re-measured rather than trusted.

What deliberately does NOT count
--------------------------------
* **A subscription that points AT the live pane session.** Same identity, right
  session: the wake lands where the operator is. This is the clean posture and
  it is the only target this row accepts.
* **A profile with no live CLI lease.** "Cannot prove a pane" is not proof of
  drift — the same fail-open rule ``orch_subscribe`` applies when it decides
  whether to touch the gate. Those subscriptions are counted and named in the
  green detail, never reported as findings.
* **Chat-channel platforms (telegram, whatsapp, discord, ...).** Their
  ``chat_id`` is a chat, not a session, so it can never equal a session id and
  the comparison is meaningless; the wake is also what answers a human who
  subscribed themselves in chat. Claiming those would call an operator's own
  decision a failure, and a row like that gets ignored. The same reasoning
  excludes ``api_server``, where the subscription is how a stateless HTTP caller
  gets its answer back.
* **A plain ``notify`` subscription.** No agent takes a turn, so there is no
  second driver (hermes ``kanban_db_notify._NOTIFY_DELIVERY_MODES``).
* **Rows with no ``notifier_profile``** (legacy / unattributed): no profile to
  resolve a pane for. Counted, never claimed.
* **The root profile** (``default``): the operator's interactive ``hermes`` and
  the gateway, same exclusion ``doctor_main_only`` makes for the root
  ``config.yaml``.

Both id spaces the fleet actually uses are handled by one string equality: a
tmux-pane CLI lease carries a timestamp-shaped id (``YYYYMMDD_HHMMSS_xxxxx``)
while a gateway-created session carries a ``local-<digest>`` id, and a
subscription is clean exactly when its ``chat_id`` is one of the owner's live
CLI lease ids — whatever shape that lease happens to have.

Fence and purity
----------------
READ-ONLY: this module opens board DBs with ``mode=ro`` URIs (the same
discipline ``orch_waker`` applies) and reads lease files; it never writes a
subscription, a board, a config or a registry. The repair belongs to the
operator's ``hermes kanban notify-unsubscribe`` / ``notify-subscribe`` and to
the subscribe-side gate; the fix text says so.

Silent, too — and that is the second half of read-only, not a nicety. A log line
is a file write next to the DBs, so opening everything ``mode=ro`` proves nothing
if the code path then creates ``$HSCC_HOME/daemon.log``. The one borrowed call
that can log (``autodown.board_db_paths`` on its unreachable-library path) runs
inside :func:`_silent_read`, which hands the logger back afterwards — including
the process's once-per-WARN budget. Pinned by
``hscc-cluster/tests/test_doctor_tool.py::TestReadOnly::test_the_read_writes_nothing_anywhere``,
which lives in a DIFFERENT top-level directory from this module: the row's own
suite is not its whole seam test (t_2c218de8 shipped red on exactly that gap).

Inside a pytest process the row's real inputs are the operator's live boards and
live session registries, so an uninjected call returns a refusal ERROR naming the
seams instead (the ``doctor_node_pauses`` / ``doctor_main_only`` fence shape).
The fence counts a process that a suite SPAWNED as being inside the suite too —
see :func:`_under_pytest`, because the door this row feeds runs in one
(``doctor_tools`` execs ``doctor.py --json`` rather than importing it). Tests
inject fake subscription rows and a fake session registry and therefore never
touch a gateway, a board or a live registry.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

#: The row's printed name. Kept here (not only in doctor.py) so the verdict and
#: the row cannot drift; doctor.py carries a fallback constant for the case where
#: this module itself is not importable, and a test pins the two equal.
CHECK_NAME = "orch wake subs (notify+wake target = live pane session)"

#: The ONE table, and the columns the check reads. hermes owns the schema
#: (``hermes_cli/kanban_db.py``); a board whose table lacks any of these yields
#: "no knowledge", never a guess — the rule ``orch_waker.gateway_wake_cursors``
#: applies to the same table.
SUBS_TABLE = "kanban_notify_subs"
_SUBS_COLUMNS = ("task_id", "platform", "chat_id", "thread_id",
                 "notifier_profile", "delivery_mode")

#: ``delivery_mode`` values that make hermes' gateway notifier WAKE an agent.
#: Fallback only — the authority is ``hscc_daemon.orch_waker.GATEWAY_WAKE_MODES``
#: (itself pinned to hermes' ``_NOTIFY_DELIVERY_MODES``), and a test pins this
#: tuple equal to it so the two cannot drift.
_FALLBACK_WAKE_MODES = ("notify+wake", "wake")

#: Platforms whose ``chat_id`` names a SESSION (so it is comparable to a lease
#: ``session_id``). ``local`` is the incident's platform; ``tui`` is hermes'
#: desktop/TUI surface, whose ``chat_id`` is likewise a session key
#: (``tools/kanban_tools.py::_resolve_notify_target``). Chat channels and
#: ``api_server`` are excluded — see the module docstring — and a witness test
#: reddens if a chat platform is added to this tuple.
SESSION_PLATFORMS = ("local", "tui")

#: The root profile's name: the operator's interactive ``hermes`` plus the
#: gateway, out of scope the way the root ``config.yaml`` is out of scope for
#: ``doctor_main_only``. Not an orchestrator identity — no fleet name is spelled
#: anywhere in this module.
ROOT_PROFILE = "default"

#: Findings rendered per detail line before the rest is counted instead of
#: spelled out. Session ids are long and a wall of them is unreadable; the count
#: is what tells the operator this is a posture, not one stray row.
_MAX_RENDERED = 4

#: Per-board open budget. A locked or half-written board DB must not stall the
#: doctor: an unreadable board is reported as unchecked, never guessed around.
_SQLITE_TIMEOUT_S = 2.0

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_ROLES_DIR = os.path.join(_REPO_ROOT, "hscc-roles")


# ── keeping a read silent ───────────────────────────────────────────────────

def _silent(*_args, **_kwargs) -> None:
    """A logger that writes nothing. Stands in for a borrowed module's logger."""


@contextmanager
def _silent_read(module):
    """Borrow ``module``'s logger for the duration of one read, then give it back.

    Why this exists: :func:`enumerate_boards` delegates board enumeration to
    ``hscc_daemon.autodown.board_db_paths``, and when the Hermes library is
    unreachable that call reports it through ``_note_kanban_unavailable``, which
    writes ONE WARN through ``daemon_ops.log`` — a file under ``$HSCC_HOME``.
    For the daemon that is right: the interlock is its own business and it
    budgets one line per process. For this row it is wrong: ``doctor`` is a
    read, and
    ``hscc-cluster/tests/test_doctor_tool.py::TestReadOnly::test_the_read_writes_nothing_anywhere``
    snapshots a sandbox tree around exactly this door call and asserts that
    nothing in it changed. The row therefore borrows the logger rather than
    silencing the module — ``module.log`` is swapped for :func:`_silent`, the one
    call runs, and the ``finally`` gives the module back what it had.

    Restoring the WARN budget is the half that keeps this honest.
    ``_note_kanban_unavailable`` sets ``_KANBAN_LOAD["warned"] = True`` *before*
    it logs, so a caller that muted only the log would also silently spend the
    process's single WARN: a daemon that imports this row would then never tell
    its operator about a genuine interlock failure — a fail-open dressed up as a
    fix. The row's read is allowed to be quiet; it is not allowed to spend
    somebody else's alarm. So the budget is captured and restored too.

    Nothing becomes invisible. Only the FILE write is muted:
    ``_note_kanban_unavailable`` records ``ok``/``reason`` in
    ``_KANBAN_LOAD`` BEFORE it logs, so ``hscc autodown status`` still names why
    the interlock is unevaluable — the surface that already owns that answer —
    and this row still reports its own board set as UNCHECKED rather than clean.

    Scoped to the ONE call: the swap is undone in ``finally``, so the daemon's
    and every other consumer's logging is untouched the moment the read returns.
    """
    real_log = getattr(module, "log", None)
    load = getattr(module, "_KANBAN_LOAD", None)
    budget = load.get("warned") if isinstance(load, dict) else None
    if callable(real_log):
        module.log = _silent
    try:
        yield
    finally:
        if real_log is not None:
            module.log = real_log
        if isinstance(load, dict) and budget is not None:
            load["warned"] = budget


# ── the two declarations this check refuses to duplicate ────────────────────

def wake_modes() -> tuple:
    """The ``delivery_mode`` values that wake an agent.

    Read from ``hscc_daemon.orch_waker.GATEWAY_WAKE_MODES`` — the repo's one
    statement of "which subscriptions make the gateway take a turn" — so doctor
    and the waker cannot disagree about what a wake is. The fallback exists for
    a bootstrap-only install with no daemon sibling, and a test pins it equal to
    the real tuple.
    """
    try:
        from hscc_daemon import orch_waker  # noqa: PLC0415
        modes = tuple(orch_waker.GATEWAY_WAKE_MODES)
        if modes:
            return modes
    except Exception:  # noqa: BLE001 - no daemon sibling on a bootstrap-only box
        pass
    return _FALLBACK_WAKE_MODES


def _load_orch_subscribe():
    """Import ``hscc-roles/orch_subscribe.py`` — the pane resolver, not a copy.

    The sibling dir is APPENDED to ``sys.path``, never prepended:
    ``hscc-bootstrap`` and ``hscc-roles`` each ship a ``_theme.py``, and a
    prepend would let a later ``import _theme`` on the bootstrap side silently
    pick up the roles copy (the reason documented at ``doctor_main_only``'s
    ``_load_main_orch``). ``orch_subscribe`` shadows nothing.

    "Which lease is a live pane" is the parent card's rule
    (``surface == "cli"`` in Hermes' ``<profile>/runtime/active_sessions.json``)
    and lives there, once. A second implementation here would let the gate and
    the alarm disagree about what a pane is.
    """
    if _ROLES_DIR not in sys.path:
        sys.path.append(_ROLES_DIR)
    import orch_subscribe  # noqa: PLC0415 - sibling plugin, path added above
    return orch_subscribe


# ── reading the two inputs ──────────────────────────────────────────────────

def _resolve(path) -> str:
    return os.path.expanduser(str(path))


def enumerate_boards() -> Dict[str, str]:
    """``{slug: kanban.db path}`` from Hermes' own enumeration.

    Delegates to ``hscc_daemon.autodown.board_db_paths`` — the repo's ONE board
    enumeration (the PATH view of Hermes' ``list_boards``) — so this row cannot
    grow a second glob of ``kanban/boards/`` and cannot miss ``default``, which
    Hermes resolves to the legacy root ``kanban.db``. An empty dict means "could
    not enumerate" (the seam fails closed when Hermes' kanban library is
    unreachable), which :func:`doctor_check` reports instead of calling clean.

    The delegated call runs inside :func:`_silent_read`: its failure path writes
    a WARN into ``$HSCC_HOME/daemon.log``, and a doctor read writes nothing
    anywhere — the guard
    ``hscc-cluster/tests/test_doctor_tool.py::TestReadOnly::test_the_read_writes_nothing_anywhere``
    snapshots a sandbox tree around the door child and fails on that file.
    """
    try:
        from hscc_daemon import autodown  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - bootstrap-only layout has no daemon sibling
        return {}
    try:
        with _silent_read(autodown):
            paths = autodown.board_db_paths()
    except Exception:  # noqa: BLE001 - enumeration is Hermes' and may be dark
        return {}
    return {str(slug): str(db) for slug, db in (paths or {}).items()
            if db and os.path.exists(_resolve(db))}


def read_wake_subs(boards: Mapping[str, str]) -> tuple:
    """``(subs, unreadable_boards)`` — every wake-capable subscription, read-only.

    Each sub becomes ``{"board", "task_id", "platform", "chat_id", "thread_id",
    "notifier_profile", "delivery_mode"}``. Opens with the waker's discipline:
    ``file:...?mode=ro`` + a short timeout, so a doctor run can never migrate,
    create or write a board.

    A board whose table or columns are missing, or whose open fails, lands in
    ``unreadable_boards`` — "not checked", which the row reports. Silently
    skipping it would let the row call an unchecked fleet clean.
    """
    subs: List[Dict[str, Any]] = []
    unreadable: List[str] = []
    modes = tuple(m.lower() for m in wake_modes())
    placeholders = ",".join("?" * len(modes))
    cols = ", ".join(_SUBS_COLUMNS)
    for slug in sorted(boards):
        path = _resolve(boards[slug])
        try:
            uri = os.path.abspath(path).replace("%", "%25")
            conn = sqlite3.connect(
                "file:" + _as_uri_path(uri) + "?mode=ro", uri=True,
                timeout=_SQLITE_TIMEOUT_S)
        except (sqlite3.Error, OSError, ValueError):
            unreadable.append(slug)
            continue
        try:
            have = {r[1] for r in conn.execute(
                "PRAGMA table_info(%s)" % SUBS_TABLE)}
            if not set(_SUBS_COLUMNS).issubset(have):
                unreadable.append(slug)
                continue
            rows = conn.execute(
                "SELECT %s FROM %s WHERE LOWER(delivery_mode) IN (%s)"
                % (cols, SUBS_TABLE, placeholders), modes).fetchall()
        except sqlite3.Error:
            unreadable.append(slug)
            continue
        finally:
            conn.close()
        for row in rows:
            rec = dict(zip(_SUBS_COLUMNS, row))
            rec["board"] = slug
            subs.append(rec)
    return subs, unreadable


def _as_uri_path(path: str) -> str:
    """Absolute path in the ``file:`` form sqlite accepts on every platform."""
    from pathlib import Path  # noqa: PLC0415 - only needed for this transform
    return Path(path).as_uri().removeprefix("file://")


def live_panes(profile: str, *, hermes_home: Optional[str],
               registry: Optional[Any] = None) -> Optional[List[str]]:
    """The profile's live pane (CLI-lease) session ids, or None when unknowable.

    ``registry`` is the injectable fake the tests use — an iterable of lease
    dicts or a callable ``(profile_home) -> iterable``, exactly the seam
    ``orch_subscribe.live_pane_sessions`` exposes. Without it the profile's
    ``runtime/active_sessions.json`` is read read-only.

    None (rather than ``[]``) means the resolver itself was unreachable: the
    row then says it could not check, instead of passing the posture. An empty
    list genuinely means "no live pane" — fail-open, no claim.
    """
    if registry is not None:
        # The caller owns the answer; still route it through the real predicate
        # so the surface rule stays in one place.
        try:
            mod = _load_orch_subscribe()
        except Exception:  # noqa: BLE001
            return None
        try:
            return list(mod.live_pane_sessions(
                profile, registry=registry, hermes_home=hermes_home))
        except Exception:  # noqa: BLE001
            return None
    try:
        mod = _load_orch_subscribe()
    except Exception:  # noqa: BLE001
        return None
    try:
        return list(mod.live_pane_sessions(profile,
                                           hermes_home=hermes_home))
    except Exception:  # noqa: BLE001
        return None


# ── the verdict ─────────────────────────────────────────────────────────────

def sub_id(sub: Mapping[str, Any]) -> str:
    """The subscription's identity as hermes stores it (it has no surrogate id).

    ``hermes_cli/kanban_db_notify.py`` keys subscriptions on
    ``(task_id, platform, chat_id, thread_id)``, so that tuple IS the id, and
    the ``notify-unsubscribe`` verb takes exactly those parts.
    """
    return "%s@%s/%s/%s" % (sub.get("task_id"), sub.get("board"),
                            sub.get("platform"), sub.get("thread_id") or "-")


def scan(subs: Iterable[Mapping[str, Any]], *, pane_of: Callable[[str], Optional[List[str]]],
         wake: Sequence[str], root_profile: str = ROOT_PROFILE,
         platforms: Sequence[str] = SESSION_PLATFORMS) -> Dict[str, Any]:
    """Partition wake-capable subscriptions into drift and accounted-for.

    Pure: takes records, returns records, no I/O — so every posture below is
    testable from a fake without a board, a gateway or a session registry.

    Returns ``{"findings": [...], "paneless": int, "at_pane": int,
    "not_session_platform": int, "unattributed": int, "unknown": int}``.
    Each finding is ``{"profile", "board", "sub_id", "task_id", "platform",
    "thread_id", "target_session", "pane_sessions"}`` — the shape both the row
    and the tests assert on, so the wording and the predicate cannot disagree.
    """
    findings: List[Dict[str, Any]] = []
    counts = {"paneless": 0, "at_pane": 0, "not_session_platform": 0,
              "unattributed": 0, "unknown": 0}
    panes_cache: Dict[str, Optional[List[str]]] = {}
    wake = tuple(str(m).strip().lower() for m in wake)
    platforms = tuple(str(p).strip().lower() for p in platforms)

    for sub in subs or ():
        if not isinstance(sub, Mapping):
            continue
        mode = str(sub.get("delivery_mode") or "").strip().lower()
        if mode not in wake:
            continue
        profile = str(sub.get("notifier_profile") or "").strip()
        if not profile or profile == root_profile:
            counts["unattributed"] += 1
            continue
        platform = str(sub.get("platform") or "").strip().lower()
        if platform not in platforms:
            counts["not_session_platform"] += 1
            continue
        if profile not in panes_cache:
            panes_cache[profile] = pane_of(profile)
        panes = panes_cache[profile]
        if panes is None:
            # Cannot say what the pane is: not evidence of drift, and the row
            # must not pretend it checked.
            counts["unknown"] += 1
            continue
        if not panes:
            counts["paneless"] += 1
            continue
        target = str(sub.get("chat_id") or "")
        if target and target in panes:
            counts["at_pane"] += 1
            continue
        findings.append({
            "profile": profile,
            "board": str(sub.get("board") or "?"),
            "sub_id": sub_id(sub),
            "task_id": str(sub.get("task_id") or "?"),
            "platform": platform,
            "thread_id": str(sub.get("thread_id") or ""),
            "target_session": target,
            "pane_sessions": list(panes),
        })
    return {"findings": findings, **counts}


def _render(findings: List[Dict[str, Any]]) -> str:
    """The one-line detail: who, which subscription, target vs expected."""
    parts = []
    for f in findings[:_MAX_RENDERED]:
        parts.append(
            "%s [%s] target=%s expected-pane=%s" % (
                f["profile"], f["sub_id"], f["target_session"] or "<empty>",
                ",".join(f["pane_sessions"])))
    tail = ""
    if len(findings) > _MAX_RENDERED:
        tail = " (+%d more, same posture)" % (len(findings) - _MAX_RENDERED)
    return "; ".join(parts) + tail


def _fix(findings: List[Dict[str, Any]]) -> str:
    """The repair, named once per posture rather than once per row."""
    if not findings:
        return ""
    first = findings[0]
    unsub = ("hermes kanban --board %s notify-unsubscribe %s --platform %s "
             "--chat-id %s" % (first["board"], first["task_id"],
                               first["platform"], first["target_session"]))
    if first["thread_id"]:
        unsub += " --thread-id " + first["thread_id"]
    repoint = ("or repoint it at %s's live pane session (%s): hermes kanban "
               "--board %s notify-subscribe %s --platform %s --chat-id %s "
               "--notifier-profile %s --delivery-mode notify+wake" % (
                   first["profile"], first["pane_sessions"][0], first["board"],
                   first["task_id"], first["platform"],
                   first["pane_sessions"][0], first["profile"]))
    return ("%s %s — a pane-backed profile's wake path is its tmux pane plus the "
            "hscc orch-waker, so a gateway notify+wake sub aimed at another "
            "session drives a SECOND copy of the same orchestrator. The durable "
            "switch is the profile's kanban.auto_subscribe_on_create=false "
            "(hscc-roles/orch_subscribe.py, card t_31c8b258); re-run bootstrap "
            "or 'hscc main start' to set it. Do not restore the 2026-11-23 "
            "backup files ~/fleet-check/backups/notify-subs-*-20261123.txt — "
            "they are the posture this row reports." % (unsub, repoint))


def _under_pytest() -> bool:
    """True when this process is a pytest run — **or a process one spawned**.

    ``"pytest" in sys.modules`` is process-local, and the door this row feeds is
    a separate interpreter: ``doctor_tools.doctor_findings`` execs
    ``python3 hscc-bootstrap/doctor.py --json`` instead of importing it (the
    flat sibling imports only resolve when the script runs as a script). So the
    row renders in a child that never imported pytest, and the fence alone reads
    as though it had fired while refusing nothing.

    That is not hypothetical — it is how this row was caught (t_14eae4b5). The
    door's read-only guard went red the moment the row was registered, because
    the child took the ambient path: it enumerated and OPENED the operator's
    boards, and the fail-closed enumeration reached
    ``autodown._note_kanban_unavailable``, whose once-per-process WARN appended
    a line to ``$HOME/.hscc/daemon.log`` — a new file inside a HOME the caller
    had sealed for exactly that assertion. The mute added by t_2c218de8 makes the
    read quiet; it does not make the read refuse. A suite-spawned door still
    resolving the operator's boards is the thing this fence exists to forbid, and
    it is now invisible to a file-diffing guard, so it is pinned by
    ``test_doctor_orch_subs_fence_reaches_children_t_14eae4b5.py`` instead.

    ``PYTEST_CURRENT_TEST`` is the channel that does cross the spawn: pytest
    exports it for the duration of each item and every child inherits the
    environment. It is the same marker the repo already asks of a child rather
    than inventing a second one (``hscc_daemon/main_cli.py``,
    ``hscc-api/repl_inject.py``, ``hscc-project/flightdeck/core/tmux_chat.py``).
    """
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def doctor_check(hermes_home: Optional[str] = None, *,
                 boards: Optional[Mapping[str, str]] = None,
                 subscriptions: Optional[Sequence[Mapping[str, Any]]] = None,
                 registry: Optional[Any] = None) -> dict:
    """The row's verdict dict (the five doctor ``Check`` fields).

    Every input is injectable, because every real one belongs to the operator:

    ``boards``        ``{slug: kanban.db path}`` — default: Hermes' own
                      enumeration through ``autodown.board_db_paths``.
    ``subscriptions`` the sub records themselves — default: read from
                      ``boards``, read-only.
    ``registry``      the fake session registry forwarded to
                      ``orch_subscribe.live_pane_sessions`` — default: each
                      profile's ``runtime/active_sessions.json``.

    ``hermes_home`` is what ``doctor_rows.render_rows`` forwards for a row
    declaring it: it locates the profile registries the row resolves a pane in.
    It does NOT relocate the boards — those are Hermes' and are found through
    Hermes' own enumeration — so this row declares ``row_kwargs=("hermes_home",)``
    and nothing else.

    A process that injects nothing gets :func:`refused` — a pytest process, or
    one a pytest process spawned (see :func:`_under_pytest`): a suite must not
    decide its assertions from the operator's live boards, and must not open them.
    Precisely, a *live* read is the ambient board enumeration (no ``boards=`` and
    no ``subscriptions=``) or the ambient lease files (no ``registry=``). Paths a
    test created itself are not live, so a suite may exercise the real read path
    against tmp boards.
    """
    live_boards = boards is None
    live_subs = subscriptions is None
    live_registry = registry is None
    ambient_boards = live_boards and live_subs
    if _under_pytest() and (ambient_boards or live_registry):
        return refused()

    modes = wake_modes()
    if live_subs:
        if live_boards:
            boards = enumerate_boards()
        else:
            boards = {str(k): str(v) for k, v in boards.items()}
        if not boards:
            return {
                "name": CHECK_NAME, "ok": False, "fatal": False,
                "detail": "boards could not be enumerated — no subscription was "
                          "read, so the notify+wake posture is UNCHECKED (this is "
                          "not a clean result)",
                "fix": "Run doctor from an install where Hermes' kanban library "
                       "is importable (the same seam 'hscc autodown status' "
                       "reports; a missing dependency such as ruamel is the "
                       "usual cause) and re-run."}
        subs, unreadable = read_wake_subs(boards)
    else:
        subs = list(subscriptions or ())
        # Injected rows carry their own board; nothing to enumerate, nothing to
        # open. ``boards`` stays whatever the caller supplied.
        unreadable = []

    def pane_of(profile):
        return live_panes(profile, hermes_home=hermes_home, registry=registry)

    result = scan(subs, pane_of=pane_of, wake=modes)
    findings = result["findings"]
    suffix = ""
    if unreadable:
        suffix = (" %d board(s) unreadable (%s) — their subscriptions were not "
                  "checked." % (len(unreadable), ", ".join(sorted(unreadable))))
    if result["unknown"]:
        suffix += (" %d wake sub(s) could not be checked because no pane "
                   "resolver was reachable." % result["unknown"])

    if not findings:
        detail = (
            "clean: %d wake sub(s) across %d board(s) — %d point at their "
            "owner's live pane session, %d sit on paneless owners (no pane to "
            "expect), %d on chat/other platforms, %d unattributed%s"
            % (len(subs), len(boards or {}), result["at_pane"],
               result["paneless"], result["not_session_platform"],
               result["unattributed"], suffix))
        return {"name": CHECK_NAME, "ok": True, "fatal": False,
                "detail": detail, "fix": ""}
    return {"name": CHECK_NAME, "ok": False, "fatal": False,
            "detail": "%d notify+wake subscription(s) owned by a pane-backed "
                      "profile point off its live pane session — the "
                      "2026-11-23 duplicate-orchestrator pattern: %s%s"
                      % (len(findings), _render(findings), suffix),
            "fix": _fix(findings)}


def refused() -> dict:
    """The in-suite refusal: an ERROR naming the seams, never a silent pass and
    never a read of the operator's boards or session registries."""
    return {"name": CHECK_NAME, "ok": False, "fatal": False,
            "detail": "orch wake-subscription check refused inside a pytest "
                      "process — inject subscriptions= and registry= (and "
                      "boards= when the board enumeration matters)",
            "fix": "Call doctor_orch_subs.doctor_check(boards={...}, "
                   "subscriptions=[{...}], registry=[{...}]) with fake records "
                   "and fake leases, or pass "
                   "_orch_wake_subs_check=lambda: {...} to pin the row's "
                   "routing; a suite must never open a live board or a live "
                   "active-session registry."}


if __name__ == "__main__":  # standalone probe: python3 doctor_orch_subs.py
    import argparse
    ap = argparse.ArgumentParser(
        description="kanban notify+wake subscription posture (read-only)")
    ap.add_argument("--home", default=None, help="HERMES_HOME holding the "
                    "profiles whose pane sessions are resolved")
    args = ap.parse_args()
    print(json.dumps(doctor_check(args.home), indent=2, sort_keys=True))
