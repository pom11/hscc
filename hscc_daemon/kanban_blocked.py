"""hscc kanban blocked — SHOW why a card is blocked and recover it.

Hermes' operator-driven ``reclaim_task`` CANNOT recover a ``blocked`` card.
Its early guard at ``hermes_cli/kanban_db.py:4491-4493`` returns False
whenever a task is NOT running AND has no ``claim_lock`` — exactly the state
the dispatcher's circuit-breaker auto-block leaves tasks in (``status='blocked'``
with ``claim_lock=NULL``; see ``kanban_db.py:7789-7809``, which never sets
``block_kind`` and never posts a comment — hence "blocked with no reason").
Reproduced live tonight (card t_ab177036): ``kdb.reclaim_task()`` returned
False for two blocked cards, which had to be recovered with a direct DB update.

So this module gives the operator a WORKING recovery path: it uses the same
direct-DB-update mechanism (``status='blocked' → 'ready'``) that un-stuck the
cards, and it SHOWS why a card is blocked (``block_kind``, ``last_failure_error``,
plus block comments) before the operator decides to recover it.

CRITICAL reuse: like autodown's ``_has_active_work``, this module never
re-implements board enumeration. It reuses ``autodown._load_kanban_db_or_default``
for the import and ``autodown._enum_board_names`` (added by card t_e751e652)
for board slugs, falling back to the same ``list_boards()`` seam inline when
that helper isn't present yet. So the boards "that block autodown" are exactly
the boards "that blocked lists" — a divergence would be its own bug.

Recovery is NEVER automatic and NEVER bulk: ``--recover`` takes exactly ONE
task id, and only a human decides a blocked card is safe to re-run.

stdlib only. Wired into the ``hscc kanban`` group (see hscc.py::main and
kanban_cli.py::VALID_SUBCOMMANDS — this card coordinates with t_e751e652,
which owns that group).
"""

from __future__ import annotations

import datetime
import json
import sys

from hscc_daemon import autodown

HELP_TEXT = """\
HSCC kanban blocked — see WHY a card is blocked and recover it.

Autodown and the dispatcher can both leave cards ``blocked`` with little or no
explanation (the dispatcher's circuit-breaker auto-block sets ``block_kind``
NULL and posts no comment — Hermes behaviour, see
hermes_cli/kanban_db.py:7789-7809). Hermes' ``reclaim_task`` cannot recover a
blocked card (early guard at kanban_db.py:4491-4493 returns False), so this
verb provides a WORKING recovery path via the same direct DB update that
un-stuck tonight's cards.

Usage: hscc kanban <subcommand> [args]

  hscc kanban blocked [--json]
                              List every BLOCKED task across ALL boards, with
                              why it's blocked (block_kind, last error, block
                              comments), oldest first.
  hscc kanban blocked --recover <task_id> [--reason <text>]
                              Recover exactly ONE blocked task to ready. Never
                              automatic, never bulk — a judgement call about
                              whether the work is safe to re-run.
  hscc kanban stale [--older-than <days>] [--json]   (card t_e751e652)
                              List non-terminal cards across all boards.
  hscc kanban --help          This help
"""


def _board_names(kanban_db):
    """Enumerate board slugs via the SAME seam autodown._has_active_work uses.

    Prefers ``autodown._enum_board_names`` (added by card t_e751e652) so this
    module and autodown agree exactly; falls back to the same ``list_boards()``
    logic inline when that helper isn't merged yet (this card's own window).
    A lib without ``list_boards`` is treated as the single ``default`` board
    (back-compat), mirroring autodown.py:172-183.
    """
    helper = getattr(autodown, "_enum_board_names", None)
    if callable(helper):
        return helper(kanban_db)
    multi = hasattr(kanban_db, "list_boards")
    if multi:
        try:
            boards = [str(e["slug"]) for e in kanban_db.list_boards()] or [None]
        except Exception:
            boards = [None]
    else:
        boards = [None]
    return boards, multi


def _age_days(created_at, now):
    """Human-friendly age of a task in (floor) days, from a unix created_at."""
    try:
        ts = float(created_at)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return max(0, int((now - ts) // 86400))


def _block_summary(row):
    """Short human string describing WHY a card is blocked, from its row."""
    parts = []
    kind = row["block_kind"] if "block_kind" in row.keys() else None
    err = row["last_failure_error"] if "last_failure_error" in row.keys() else None
    fails = row["consecutive_failures"] if "consecutive_failures" in row.keys() else None
    if kind:
        parts.append(f"kind={kind}")
    if fails:
        parts.append(f"{fails} consecutive failure(s)")
    if err:
        parts.append(f"error={err}")
    if not parts:
        return "(no block reason recorded)"
    return "; ".join(parts)


def _block_comments(conn, task_id, cap=5):
    """Return up to ``cap`` most-recent comments on a blocked task.

    Reads ``task_comments`` directly (the same table ``kanban_db.list_comments``
    reads), filtering out the automatic resume/claim noise so the operator sees
    why the card is actually stuck. Tolerates any table shape a board has.
    """
    out = []
    try:
        rows = conn.execute(
            "SELECT author, body, created_at FROM task_comments "
            "WHERE task_id = ? ORDER BY created_at DESC LIMIT ?",
            (task_id, cap + 8),
        ).fetchall()
    except Exception:
        return out
    for r in rows:
        author = r["author"] if "author" in r.keys() else None
        body = r["body"] if "body" in r.keys() else None
        body = (body or "").strip()
        if not body:
            continue
        if author in ("hscc-resume", "hermes"):
            continue
        out.append(body)
        if len(out) >= cap:
            break
    return out


def list_blocked_tasks(kanban_db=None):
    """List every BLOCKED task across all boards, with why, oldest first.

    Reuses autodown's enumeration: ``_load_kanban_db_or_default`` for the
    import + ``_board_names`` for board slugs (the same seam ``_has_active_work``
    uses). Returns ``{boards, tasks, errors}`` where each task dict carries
    ``board`` ``id`` ``status`` ``assignee`` ``age_days`` ``block_kind``
    ``why`` ``title`` ``comments``. Unreadable boards are captured in errors and
    never crash the listing.
    """
    if kanban_db is None:
        kanban_db = autodown._load_kanban_db_or_default()
    if kanban_db is None:
        return {"boards": 0, "tasks": [], "errors": ["kanban lib unreachable"]}

    boards, _multi = _board_names(kanban_db)
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    tasks = []
    errors = []

    for board in boards:
        try:
            with kanban_db.connect_closing(board=board) as conn:
                rows = conn.execute(
                    "SELECT id, assignee, created_at, block_kind, status, "
                    "last_failure_error, consecutive_failures, title "
                    "FROM tasks WHERE status = 'blocked'"
                ).fetchall()
        except Exception as e:
            label = board if board else "default"
            errors.append(f"board {label!r} unreadable: {e}")
            continue

        for row in rows:
            tid = row["id"]
            tasks.append({
                "board": board if board else "default",
                "id": tid,
                "status": row["status"],
                "assignee": row["assignee"] if "assignee" in row.keys() else None,
                "age_days": _age_days(row["created_at"], now),
                "block_kind": (row["block_kind"]
                               if "block_kind" in row.keys() else None),
                "why": _block_summary(row) or "(no block reason recorded)",
                "title": (row["title"] if "title" in row.keys() else "") or "",
                "comments": _block_comments(conn, tid),
            })
    # Oldest first (board hygiene's convention, matching the stale command):
    # the card that has sat blocked longest surfaces first.
    tasks.sort(key=lambda t: (t["age_days"] if t["age_days"] is not None else 0,
                              t["board"], t["id"]), reverse=True)
    return {"boards": len(boards), "tasks": tasks, "errors": errors}


def recover_blocked_task(task_id, reason=None, kanban_db=None):
    """Recover exactly ONE blocked task back to ``ready``.

    Hermes' ``reclaim_task`` cannot do this (see module docstring) and we must
    not patch Hermes, so recovery uses the SAME direct DB update the operator
    used to un-stick tonight's cards, via Hermes' public ``connect_closing()``.

    Returns ``(label, True)`` on success (``label`` = board slug or 'default').
    Returns ``(None, False)`` when the id is not found on any board, or is not
    currently ``blocked`` (never mutate a non-blocked card). A ``reason`` (on
    the CLI: ``--reason``) is recorded as a durable comment + event so there is
    ALWAYS an audit trail for why the card was re-run.
    """
    if kanban_db is None:
        kanban_db = autodown._load_kanban_db_or_default()
    if kanban_db is None:
        return None, False

    boards, _multi = _board_names(kanban_db)

    for board in boards:
        try:
            with kanban_db.connect_closing(board=board) as conn:
                cur = conn.execute(
                    "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, "
                    "consecutive_failures = 0 "
                    "WHERE id = ? AND status = 'blocked'",
                    (task_id,),
                )
                if cur.rowcount != 1:
                    continue
                # Commit the status change NOW — Hermes' connect_closing does
                # not auto-commit, and we must not lose the recovery on close.
                conn.commit()
                _record_recovery(conn, task_id, reason, board)
            return (board if board else "default"), True
        except Exception:
            # A write error on one board must not abort the scan.
            continue
    return None, False


def _record_recovery(conn, task_id, reason, board):
    """Append a durable 'recovered' event + a comment, best-effort.

    Uses the same ``task_events`` / ``task_comments`` tables Hermes'
    ``_append_event`` / ``add_comment`` write to, so the audit trail is
    visible via ``hermes kanban tail`` and ``build_worker_context``. Never
    raises.
    """
    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    try:
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'recovered', ?, ?)",
            (task_id, json.dumps({"reason": reason, "board": board,
                                  "via": "hscc kanban blocked --recover"}),
             now_ts),
        )
    except Exception:
        pass
    try:
        body = (
            "Recovered from blocked to ready by operator (hscc kanban blocked "
            "--recover)."
            + (f" Reason: {reason}" if reason else "")
        )
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, 'hscc-recover', ?, ?)",
            (task_id, body, now_ts),
        )
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def _error(msg, json_mode):
    """Emit a clear error and return a non-zero exit code."""
    if json_mode:
        print(json.dumps({"error": msg, "exit": 1}))
    else:
        print(f"Error: {msg}", file=sys.stderr)
    return 1


def cmd_blocked(rest, json_mode):
    """``hscc kanban blocked`` — list blocked cards or recover one.

    With ``--recover <id>`` recovers exactly ONE blocked card to ready (never
    automatic, never bulk; unknown/non-blocked id ⇒ clear error, non-zero
    exit). Otherwise lists every blocked task across all boards with why it is
    blocked, oldest first.
    """
    if "--recover" in rest:
        if rest.count("--recover") > 1:
            return _error("--recover takes exactly ONE task id", json_mode)
        i = rest.index("--recover")
        if i + 1 >= len(rest) or rest[i + 1].startswith("-"):
            return _error("--recover requires a task id", json_mode)
        task_id = rest[i + 1]
        reason = None
        if "--reason" in rest:
            j = rest.index("--reason")
            if j + 1 < len(rest):
                reason = rest[j + 1]
        # Exactly one target — reject stray positionals that aren't the
        # --recover id or a --reason value.
        positionals = [
            a for a in rest
            if not a.startswith("-")
            and a != task_id
            and a != reason
        ]
        if positionals:
            return _error("--recover recovers exactly ONE task", json_mode)
        try:
            label, ok = recover_blocked_task(task_id, reason=reason)
        except RuntimeError as e:
            return _error(str(e), json_mode)
        if not ok:
            return _error(
                f"cannot recover {task_id!r}: not found as a blocked task "
                "on any board", json_mode)
        if json_mode:
            print(json.dumps({"recovered": task_id, "board": label,
                              "reason": reason, "exit": 0}))
        else:
            msg = f"recovered {task_id} (board '{label}') to ready"
            if reason:
                msg += f" — reason: {reason}"
            print(msg)
        return 0

    # Otherwise: list blocked tasks.
    result = list_blocked_tasks()
    tasks = result["tasks"]

    if json_mode:
        print(json.dumps({
            "boards": result["boards"],
            "tasks": tasks,
            "errors": result["errors"],
        }))
    else:
        if not tasks and not result["errors"]:
            print("no blocked cards on any board")
        for t in tasks:
            assignee = t["assignee"] or "-"
            age = f"{t['age_days']}d" if t["age_days"] is not None else "?d"
            kind = t["block_kind"] or "-"
            title = (t["title"] or "").strip()
            print(
                f"{t['board']:<16} {t['id']:<14} kind={kind:<12} "
                f"{age:>4}  {title}"
            )
            print(f"{'':<16} {'':<14} why: {t['why']}")
            for c in t["comments"]:
                first = c.strip().replace("\n", " ")[:160]
                print(f"{'':<16} {'':<14} comment: {first}")
        if result["errors"]:
            print("\nWarnings (boards not fully scanned):", file=sys.stderr)
            for e in result["errors"]:
                print(f"  - {e}", file=sys.stderr)
    return 0


def cmd_kanban(argv):
    """Dispatch ``hscc kanban <subcommand>``; returns an exit code, never raises.

    Owns the ``blocked`` subcommand (this module). Also owns ``stale`` when
    card t_e751e652's ``kanban_cli`` module is present on disk — the ``hscc
    kanban`` verb has exactly ONE dispatcher, so ``hscc kanban stale`` keeps
    working regardless of merge order. ``--json`` is honored by every
    subcommand. With no subcommand (or ``--help``) prints this group's help and
    exits 0, matching the other group verbs.
    """
    if not argv or argv[0] in ("--help", "-h"):
        print(HELP_TEXT)
        return 0

    json_mode = "--json" in argv
    argv = [a for a in argv if a != "--json"]

    sub = argv[0]
    rest = argv[1:]

    if sub == "blocked":
        return cmd_blocked(rest, json_mode)

    # Delegate `stale` (t_e751e652) when its module is present.
    if sub == "stale":
        try:
            from hscc_daemon import kanban_cli as _kcli
            return _kcli.cmd_kanban(["stale", *rest])
        except ImportError:
            return _error(
                "stale is not available yet (card t_e751e652 not merged)",
                json_mode)

    print(f"Error: unknown kanban subcommand: {sub}", file=sys.stderr)
    print(f"Valid subcommands: blocked, stale", file=sys.stderr)
    return 1
