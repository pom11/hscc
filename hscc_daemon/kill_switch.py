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
"""

from __future__ import annotations

from typing import Any, Optional

from hscc_daemon import autodown

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


def list_running_tasks(kanban_db=None) -> dict:
    """Enumerate every running kanban task across ALL boards with kill detail.

    This is the "name exactly what will be stopped" half of the kill switch:
    it returns each running task with board, id, title, assignee, the worker
    PID (``worker_pid`` else from ``claim_lock``), ``host_local`` (True when
    the worker runs on THIS host and we could actually signal it), and the
    ISO timestamp it started.

    Returns ``{boards: [...], tasks: [...], errors: [...], count: N}``.
    ``tasks`` is sorted by board then id. Unreadable boards are reported in
    ``errors`` and never crash the listing (matches ``list_stale_tasks``).

    ``kanban_db`` is injectable so tests can hand a fake lib and never touch
    the live ``~/.hermes``. When omitted it loads the real lib via the SAME
    lazy loader ``autodown`` uses.
    """
    if kanban_db is None:
        kanban_db = autodown._load_kanban_db_or_default()
    result: dict[str, Any] = {"boards": [], "tasks": [], "errors": []}
    if kanban_db is None:
        reason = autodown._KANBAN_LOAD.get("reason") or "kanban lib unreachable"
        result["errors"].append(reason)
        result["count"] = 0
        return result
    try:
        boards, multi = autodown._enum_board_names(kanban_db)
    except Exception as e:  # enumeration failure — report, don't crash
        result["errors"].append(f"could not enumerate kanban boards: {e}")
        result["count"] = 0
        return result

    # Local host prefix — the SAME ``_claimer_id()`` Hermes uses for
    # ``claim_lock``, so the ``host_local`` we present matches what the kill
    # will actually enforce.
    local_host = None
    try:
        local_host = str(kanban_db._claimer_id()).split(":", 1)[0]
    except Exception:  # pragma: no cover - defensive
        local_host = None

    for b in boards:
        label = b if b else DEFAULT_BOARDS
        result["boards"].append(label)
        try:
            if multi:
                with kanban_db.connect_closing(board=b) as conn:
                    rows = _running_rows(conn)
            else:
                with kanban_db.connect_closing() as conn:
                    rows = _running_rows(conn)
        except Exception as e:
            result["errors"].append(f"board {label!r} unreadable: {e}")
            continue
        for r in rows:
            claim_lock = r.get("claim_lock")
            pid = _pid_for(r)
            started = r.get("started_at")
            import datetime
            started_iso = None
            if started:
                try:
                    started_iso = datetime.datetime.fromtimestamp(
                        int(started), datetime.timezone.utc
                    ).isoformat()
                except (TypeError, ValueError, OSError):
                    started_iso = None
            result["tasks"].append({
                "board": label,
                "id": r["id"],
                "title": r["title"],
                "assignee": r["assignee"],
                "status": r["status"],
                "pid": pid,
                "host_local": _host_local(claim_lock, local_host),
                "started_at": started_iso,
            })
    result["tasks"].sort(key=lambda t: (t["board"], t["id"]))
    result["count"] = len(result["tasks"])
    return result


# --------------------------------------------------------------------------- #
# Kill
# --------------------------------------------------------------------------- #

def _find_running_task(kanban_db, task_id: str, *, boards, multi):
    """Locate a single running task by id across boards.

    Returns ``(board_label, row)`` or ``(None, None)``. Only ``status ==
    'running'`` tasks are candidates — we never kill a terminal, blocked, or
    parked card. Board read failures are raised by the caller's try/except.
    """
    for b in boards:
        label = b if b else DEFAULT_BOARDS
        try:
            if multi:
                with kanban_db.connect_closing(board=b) as conn:
                    rows = _running_rows(conn)
            else:
                with kanban_db.connect_closing() as conn:
                    rows = _running_rows(conn)
        except Exception:
            # Board unreadable — keep scanning other boards; the caller
            # reports not-found rather than an enumeration error.
            continue
        for r in rows:
            if r["id"] == task_id:
                return label, r
    return None, None


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
    try:
        boards, multi = autodown._enum_board_names(kanban_db)
    except Exception as e:
        not_found["error"] = f"could not enumerate kanban boards: {e}"
        return not_found

    label, row = _find_running_task(kanban_db, task_id, boards=boards, multi=multi)
    if row is None:
        return not_found

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
        "task": {
            "board": label,
            "id": row["id"],
            "title": row["title"],
            "assignee": row["assignee"],
            "pid": pid,
            "host_local": _host_local(claim_lock, local_host),
        },
        "termination": termination,
    }
