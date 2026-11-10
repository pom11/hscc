"""HSCC — kill switch for a running kanban worker.

Stops a runaway agent/worker from the phone. Today that needs SSH on the box
running the gateway: the worker is a ``hermes -p <profile> chat -q "work kanban
task <id>"`` subprocess, and stopping it is an OS-level signal to that PID.
This module is the backend the API + iOS app wrap so the operator can do it
from the phone instead.

Design — every requirement from the card maps to a concrete behaviour here:

  * CONFIRM-GATED: the API mutation requires ``confirm: true`` (see
    routes_kanban.handle_kanban_kill); this module only performs the kill when
    asked, and never re-implements the confirm gate.

  * NAMES EXACTLY WHAT WILL BE STOPPED: ``list_running_tasks`` enumerates each
    running task with its board, id, title, assignee, PID, and whether it is
    host-local — so the phone can say "stop task t_abc (worker, 'Untitled') on
    board 'default'", not "stop the worker".

  * REPORTS WHAT ACTUALLY DIED: the kill itself is delegated to Hermes core's
    own ``kanban_db._terminate_reclaimed_worker`` — SIGTERM, poll up to
    10×0.5s, escalate to SIGKILL — and its result dict
    ``{prev_pid, host_local, termination_attempted, terminated, sigkill}`` is
    surfaced verbatim. We NEVER re-implement process signalling; we wrap the
    same primitive the reclaim loop uses, so "what actually died" is measured
    by the same liveness probe (``_pid_alive``, zombie-aware).

Enumeration reuses ``autodown``'s board discovery (``_load_kanban_db_or_default``
+ ``_enum_board_names``) so the boards this fights over are the SAME boards
that block autodown / appear in stale — one enumeration, not three.

Safety: only ``status='running'`` tasks are killable (never a terminal or
blocked card). A task whose ``claim_lock`` names a different host is reported
with ``host_local=False`` and is NOT touched by the kill (the same host-guard
``_terminate_reclaimed_worker`` already enforces — we surface it before the
fact too). A ``signal_fn`` seam lets tests inject a no-op killer so a test
never signals a real process.

The board-pin decision (t_147201fa)
-----------------------------------
A dispatched worker shell carries ``HERMES_KANBAN_DB``, and hermes resolves that
env pin ahead of an explicit ``board=`` under a worker/delegation fence — so a
naive scan of every enumerated board opened the SAME pinned DB once per slug
(see ``hscc_daemon/board_pin.py`` and ``docs/audits/board-pin-t_b014c2b8.md``).
t_b014c2b8 left this module fenced on purpose: scrubbing it would let a worker
shell point a destructive verb at boards it does not own. That left two
consequences nobody wanted — the listing reported one pinned board as the
fleet, *and* the find-by-id behind the verb could match a **different** board's
same-id card (task ids are per-board) and signal an unrelated worker.

The decision taken here is the card's **option (a)**: *guard the read, refuse
the verb*, and compute both from **one** scan so they cannot disagree.

  * **The read is guarded, not fenced.** ``_scan_running_tasks`` opens every
    enumerated board through ``board_pin.open_board(..., mode="read")``: one
    warning per (pin, board) naming the pin value, the board asked for and both
    resolved paths, then the REAL fleet. This is the same information class
    ``hscc kanban monitor`` already publishes (``kanban_live.collect`` was
    guarded the same way by t_b014c2b8), so the read half of the fence is
    weakened here for exactly the reason it was weakened there — a monitor that
    silently describes one board as the fleet is worse than a loud one.
  * **The verb refuses.** ``kill_running_task`` raises
    ``board_pin.BoardPinConflict`` (whose message names the pin, the board asked
    for and both resolved paths) whenever the card it located sits on a board
    the pin outranks. A worker shell may therefore still stop a worker on the
    board the pin points at — that is the board it owns — and nothing anywhere
    else. The mutation half of the fence is intact, and the refusal happens
    BEFORE any pid is read or any signal is sent.
  * **Listing and verb agree by construction.** Both surfaces walk
    ``_scan_running_tasks`` and get the same rows with the same per-board
    conflict verdict, so a row the listing shows as ``killable: false`` is
    exactly a row the verb refuses with that same message, and vice versa.
    ``killable``/``pin_conflict`` on each row plus ``pin_conflicts`` on the
    listing are what let the panel say so out loud instead of showing cards the
    verb will not touch. ``killable`` is about the PIN only — host-locality is
    the separate, pre-existing ``host_local`` key.
  * **Nothing changes when there is no pin** (the daemon's normal environment,
    and every operator shell): ``board_pin.pin_conflict`` short-circuits on an
    unset pin, so every row is ``killable: true``, nothing is warned, and the
    legacy ``multi=False`` single-board shape keeps its no-``board=``
    ``connect_closing()`` call verbatim.

``BoardPinConflict`` is deliberately NOT swallowed by the two
``except Exception: continue``-shaped handlers below — the same re-raise
``autodown.archive_stale_task`` and ``kanban_blocked.recover_blocked_task``
carry — because a refused board must never be reported as merely "unreadable".
"""

from __future__ import annotations

from typing import Any, Optional

from hscc_daemon import autodown, board_pin

__all__ = ["list_running_tasks", "kill_running_task", "DEFAULT_BOARDS"]


# Default board label used when a board slug is falsy (legacy flat DB).
DEFAULT_BOARDS = "default"


def _host_local(claim_lock: Optional[str], local_host: Optional[str]) -> bool:
    """True when ``claim_lock`` names the ``local_host`` (same host as us).

    ``claim_lock`` is a ``"host:pid"`` string. ``local_host`` is the hostname
    prefix of THIS process (``_claimer_id().split(':', 1)[0]``). A worker is
    host-local only when the lock's host portion equals ours — otherwise it
    runs on another node and we cannot (and must not) signal it from here.
    """
    if not claim_lock:
        return False
    try:
        lock_host = str(claim_lock).split(":", 1)[0]
    except (AttributeError, ValueError):
        return False
    return bool(local_host) and lock_host == local_host


def _pid_for(row) -> Optional[int]:
    """Best PID for a running task: the explicit ``worker_pid`` column first,
    else the pid embedded in ``claim_lock`` (``host:pid``).

    Hermes post-v2 writes ``worker_pid`` on the task row; older runs recorded
    only ``claim_lock``. Prefer the explicit column; fall back to parsing the
    lock so the enumerator works against both schema generations.
    """
    wp = row.get("worker_pid")
    if wp:
        try:
            return int(wp)
        except (TypeError, ValueError):
            return None
    cl = row.get("claim_lock")
    if cl:
        try:
            pid = str(cl).split(":", 1)[1]
        except (AttributeError, ValueError, IndexError):
            return None
        try:
            return int(pid) if pid.isdigit() else None
        except (AttributeError, ValueError):
            return None
    return None


def _running_rows(conn) -> list[dict]:
    """SELECT the running-task columns we need from one board's ``tasks``."""
    cur = conn.execute(
        "SELECT id, title, assignee, status, claim_lock, worker_pid, "
        "started_at, claim_expires "
        "FROM tasks WHERE status = 'running' "
        "ORDER BY started_at"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _started_iso(started) -> Optional[str]:
    """ISO-8601 UTC for a task's ``started_at``, or None when it is unusable."""
    if not started:
        return None
    import datetime
    try:
        return datetime.datetime.fromtimestamp(
            int(started), datetime.timezone.utc
        ).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _scan_running_tasks(kanban_db):
    """ONE pinned-aware scan of every board's ``status='running'`` rows.

    BOTH public surfaces go through here — the listing the phone shows and the
    find-by-id behind the kill. That is a safety property, not tidiness: with
    two loops, the verb can act on a row the listing never showed (task ids
    repeat across boards, so a fenced loop and a scrubbed loop can each find a
    DIFFERENT card for the same id and signal the wrong worker).

    Returns ``(entries, boards, errors, conflicts)``:

      * ``entries`` — one dict per running task with ``task`` (the public row,
        ready for the API payload), ``row`` (the raw board row — the verb needs
        its ``claim_lock``) and ``conflict`` (the
        :class:`board_pin.PinConflict` that outranked this board, else None).
      * ``boards`` — every board label we attempted, unreadable ones included
        (unchanged from the pre-guard listing, which reported them).
      * ``errors`` — per-board read failures, phrased exactly as before;
        ``BoardPinConflict`` is re-raised rather than reported here.
      * ``conflicts`` — ``{board_label: PinConflict}`` for every board the
        pin outranked, which is the authorization answer the verb reuses.

    The read is opened ``mode="read"`` (warn once + scrub for our own open) and
    the authorization question — "may the verb touch a card on this board?" — is
    the SAME hermes-derived answer: ``pin_conflict`` is None precisely when
    hermes would hand us the board we named (no pin, no fence, or the pin *is*
    that board). No path shape is ever compared here.
    """
    entries: list[dict] = []
    boards: list[str] = []
    errors: list[str] = []
    conflicts: dict[str, Any] = {}
    try:
        names, multi = autodown._enum_board_names(kanban_db)
    except Exception as e:  # enumeration failure — report, don't crash
        return entries, boards, [f"could not enumerate kanban boards: {e}"], conflicts

    # Local host prefix — the SAME ``_claimer_id()`` Hermes uses for
    # ``claim_lock``, so the ``host_local`` we present matches what the kill
    # will actually enforce.
    try:
        local_host = str(kanban_db._claimer_id()).split(":", 1)[0]
    except Exception:  # pragma: no cover - defensive
        local_host = None

    for b in names:
        label = b if b else DEFAULT_BOARDS
        boards.append(label)
        # ONE authority for "did the pin outrank this board?", used both to
        # label the row and to decide the verb. ``open_board`` asks hermes'
        # resolver again for its own decision, which is fine: the guard is
        # idempotent and its warning dedupes per (pin, board).
        conflict = board_pin.pin_conflict(kanban_db, b)
        if conflict is not None:
            conflicts[label] = conflict
        try:
            with board_pin.open_board(kanban_db, b, multi=multi,
                                       mode="read") as conn:
                rows = _running_rows(conn)
        except board_pin.BoardPinConflict:
            # A refused board is not an unreadable one — never fold it into
            # ``errors`` (see the module docstring; same posture as
            # autodown.archive_stale_task).
            raise
        except Exception as e:
            errors.append(f"board {label!r} unreadable: {e}")
            continue
        for r in rows:
            claim_lock = r.get("claim_lock")
            entries.append({
                "task": {
                    "board": label,
                    "id": r["id"],
                    "title": r["title"],
                    "assignee": r["assignee"],
                    "status": r["status"],
                    "pid": _pid_for(r),
                    "host_local": _host_local(claim_lock, local_host),
                    "started_at": _started_iso(r.get("started_at")),
                    # The listing/verb agreement, per row: what the panel shows
                    # as un-killable is what the verb refuses, same message.
                    "killable": conflict is None,
                    "pin_conflict": conflict.write_error() if conflict else None,
                },
                "row": r,
                "conflict": conflict,
            })
    entries.sort(key=lambda e: (e["task"]["board"], e["task"]["id"]))
    return entries, boards, errors, conflicts


def list_running_tasks(kanban_db=None) -> dict:
    """Enumerate every running kanban task across ALL boards with kill detail.

    This is the "name exactly what will be stopped" half of the kill switch:
    it returns each running task with board, id, title, assignee, the worker
    PID (``worker_pid`` else from ``claim_lock``), ``host_local`` (True when
    the worker runs on THIS host and we could actually signal it), and the
    ISO timestamp it started.

    Returns ``{boards: [...], tasks: [...], errors: [...], pin_conflicts:
    [...], count: N}``. ``tasks`` is sorted by board then id. Unreadable boards
    are reported in ``errors`` and never crash the listing (matches
    ``list_stale_tasks``). ``pin_conflicts`` names every board whose rows were
    read *despite* a worker ``HERMES_KANBAN_DB`` outranking them, and each such
    row carries ``killable: false`` + the same message — because the verb will
    refuse those cards (see the module docstring).

    ``kanban_db`` is injectable so tests can hand a fake lib and never touch
    the live ``~/.hermes``. When omitted it loads the real lib via the SAME
    lazy loader ``autodown`` uses.
    """
    if kanban_db is None:
        kanban_db = autodown._load_kanban_db_or_default()
    result: dict[str, Any] = {"boards": [], "tasks": [], "errors": [],
                              "pin_conflicts": []}
    if kanban_db is None:
        reason = autodown._KANBAN_LOAD.get("reason") or "kanban lib unreachable"
        result["errors"].append(reason)
        result["count"] = 0
        return result

    entries, boards, errors, conflicts = _scan_running_tasks(kanban_db)
    result["boards"] = boards
    result["errors"] = errors
    result["tasks"] = [e["task"] for e in entries]
    result["pin_conflicts"] = sorted({c.write_error()
                                      for c in conflicts.values()})
    result["count"] = len(result["tasks"])
    return result


# --------------------------------------------------------------------------- #
# Kill
# --------------------------------------------------------------------------- #

def _find_running_task(kanban_db, task_id: str):
    """Locate a single running task by id across boards.

    Returns the ``_scan_running_tasks`` entry (``task``/``row``/``conflict``)
    or ``None``. Only ``status == 'running'`` tasks are candidates — we never
    kill a terminal, blocked, or parked card. It IS the same scan the listing
    runs, on purpose: the card this finds, the pid it derives and the board
    verdict it carries are the ones the operator was shown. Board read
    failures are dropped by the scan (the caller reports not-found rather than
    an enumeration error); a ``BoardPinConflict`` is never dropped.
    """
    entries, _boards, _errors, _conflicts = _scan_running_tasks(kanban_db)
    for e in entries:
        if e["task"]["id"] == task_id:
            return e
    return None


def kill_running_task(task_id: str, kanban_db=None, *, signal_fn=None) -> dict:
    """Stop ONE running kanban task and report what actually died.

    This is the "reports what actually died" half of the kill switch. It finds
    the running task by id, then delegates the process termination to Hermes
    core's own ``_terminate_reclaimed_worker`` (SIGTERM → poll → SIGKILL) and
    returns its result dict verbatim alongside the task detail:

      ``{found, task: {board, id, title, assignee, pid, host_local},
          termination: {prev_pid, host_local, termination_attempted,
                        terminated, sigkill}, error?}``

    Semantics of the returned fields:
      * ``found`` False — no ``status='running'`` task with that id on any
        board → the caller maps this to 404 "not found".
      * ``found`` True + ``termination`` — the task exists and is running; the
        ``termination`` dict tells the truth about the kill:
          - ``host_local`` False → it runs on another node; we did NOT touch
            it (there is nothing more we can do from this host).
          - ``termination_attempted`` True → we sent SIGTERM.
          - ``terminated`` True → it is gone (either we killed it, or it was
            already dead — both are "stopped", and the call reports success).
          - ``sigkill`` True → it needed SIGKILL escalation.

    Raises :class:`board_pin.BoardPinConflict` when the task was found on a
    board that a worker shell's ``HERMES_KANBAN_DB`` outranks — i.e. a board
    this process does not own. The refusal happens BEFORE the pid is read or any
    signal is sent, its message names the pin, the board asked for and both
    resolved paths, and it matches the ``pin_conflict`` the listing already
    published on that row. Callers that render this (routes_kanban) surface it
    as a failed kill with the message verbatim; it is NOT a "not found".

    ``signal_fn`` is a test seam: inject a no-op killer to exercise the full
    call path without signalling a real process. It receives ``(pid, sig)``.
    """
    if kanban_db is None:
        kanban_db = autodown._load_kanban_db_or_default()
    not_found = {
        "found": False,
        "task": None,
        "termination": None,
    }
    if kanban_db is None:
        reason = autodown._KANBAN_LOAD.get("reason") or "kanban lib unreachable"
        not_found["error"] = reason
        return not_found

    entry = _find_running_task(kanban_db, task_id)
    if entry is None:
        return not_found
    if entry["conflict"] is not None:
        # Loud refusal, naming both values, before any side effect — this is
        # the same verdict that set ``killable: false`` on the listing row.
        raise board_pin.BoardPinConflict(entry["conflict"].write_error())

    row = entry["row"]
    claim_lock = row.get("claim_lock")
    pid = _pid_for(row)
    try:
        local_host = str(kanban_db._claimer_id()).split(":", 1)[0]
    except Exception:  # pragma: no cover - defensive
        local_host = None

    termination = kanban_db._terminate_reclaimed_worker(
        pid, claim_lock, signal_fn=signal_fn
    )
    return {
        "found": True,
        "task": entry["task"],
        "termination": termination,
    }
