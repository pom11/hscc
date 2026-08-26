"""HSCC `kanban` CLI verb group — board hygiene for autodown.

Exposes ``hscc kanban stale`` so an operator can SEE and CLEAN the stale
cards that the autodown C3 interlock treats as work-in-progress.

Board hygiene is now load-bearing for autodown: the C3 interlock refuses to
tear down while ANY non-terminal kanban card exists on ANY board, so ONE
forgotten card anywhere blocks power-saving permanently. ``stale`` lists
every non-terminal task across ALL boards so the operator can find those
cards, and ``stale --archive <id>`` archives exactly one by explicit id.

CRITICAL reuse: this module NEVER re-implements board enumeration. It calls
``hscc_daemon.autodown.list_stale_tasks`` / ``archive_stale_task``, which
consume the SAME enumeration ``_has_active_work`` uses
(``_load_kanban_db_or_default`` + ``_enum_board_names``). So the boards "that
block autodown" are exactly the boards "that stale lists" — a divergence
between the two would be its own bug. See autodown.py::_enum_board_names.

Archive is never automatic and never bulk: ``--archive`` takes exactly ONE
task id, and it is a judgement call about whether the work is actually done
— autodown must not be able to unblock itself by deleting evidence of work.

Mirrors the ``api`` verb-group wiring (hscc.py:main() dispatches ``kanban``
→ ``cmd_kanban``; unknown subcommands exit non-zero, same as
api_cli.cmd_api, api_cli.py:275-297).
"""

import json
import sys

from hscc_daemon import autodown

VALID_SUBCOMMANDS = ("stale",)

# The default for ``--older-than`` is decided here (a week) so the common
# case surfaces genuinely-forgotten cards, not active work; the predicate
# itself carries the same constant so ``kanban_cli`` and the daemon agree.
DEFAULT_STALE_DAYS = autodown.DEFAULT_STALE_DAYS

HELP_TEXT = """\
HSCC kanban board hygiene — find and clean the stale cards that block autodown.

Board hygiene is load-bearing for autodown: the C3 interlock treats ANY
non-terminal card on ANY board as work-in-progress and refuses to tear down,
so one forgotten card blocks power-saving forever. This verb surfaces and
cleans those cards.

Usage: hscc kanban <subcommand> [args]

  hscc kanban stale [--older-than <days>] [--json]
                              List every non-terminal task across ALL boards,
                              oldest first (board, id, status, assignee, age,
                              title). Default --older-than is %d days; pass
                              --older-than 0 for ALL non-terminal cards.
  hscc kanban stale --archive <task_id>
                              Archive exactly ONE task by id. Never automatic,
                              never bulk — this is a judgement call about
                              whether the work is actually done.
  hscc kanban --help          This help
""" % DEFAULT_STALE_DAYS


def _parse_older_than(rest):
    """Pull optional ``--older-than N`` out of ``rest``.

    Returns ``(older_than, error)``. ``older_than`` is None if the flag is
    absent. An absent flag means the caller should use the module default. A
    non-integer or negative value is an error (never silently coerced).
    ``0`` is valid and means "list every non-terminal card".
    """
    if "--older-than" not in rest:
        return None, None
    i = rest.index("--older-than")
    if i + 1 >= len(rest):
        return None, "--older-than requires a value (a non-negative integer)"
    raw = rest[i + 1]
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return None, f"--older-than must be a non-negative integer, got {raw!r}"
    if value < 0:
        return None, f"--older-than must be a non-negative integer, got {raw!r}"
    return value, None


def _error(msg, json_mode):
    """Emit a clear error and return a non-zero exit code."""
    if json_mode:
        print(json.dumps({"error": msg, "exit": 1}))
    else:
        print(f"Error: {msg}", file=sys.stderr)
    return 1


def _cmd_stale(rest, json_mode):
    """``hscc kanban stale`` — list or archive stale cards.

    With ``--archive <id>`` archives exactly ONE task and nothing else
    (unknown id ⇒ clear error, non-zero exit — never invented). Otherwise
    lists every non-terminal task across all boards, oldest first, honoring
    ``--older-than``. All board reading/writing is delegated to autodown's
    reused enumeration and archive helpers.
    """
    # --archive <task_id>: archive exactly one task by id.
    if "--archive" in rest:
        if rest.count("--archive") > 1:
            return _error("--archive takes exactly ONE task id", json_mode)
        i = rest.index("--archive")
        if i + 1 >= len(rest) or rest[i + 1].startswith("-"):
            return _error("--archive requires a task id", json_mode)
        task_id = rest[i + 1]
        # Exactly one archive target — reject any stray positional args.
        if len([a for a in rest if not a.startswith("-")]) > 1:
            return _error("--archive archives exactly ONE task", json_mode)
        try:
            label, ok = autodown.archive_stale_task(task_id)
        except RuntimeError as e:
            return _error(str(e), json_mode)
        if not ok:
            return _error(
                f"no task with id {task_id!r} found on any board", json_mode)
        if json_mode:
            print(json.dumps({"archived": task_id, "board": label, "exit": 0}))
        else:
            print(f"archived {task_id} (board '{label}')")
        return 0

    # Otherwise: list stale tasks.
    older_than, err = _parse_older_than(rest)
    if err:
        return _error(err, json_mode)
    if older_than is None:
        older_than = DEFAULT_STALE_DAYS

    result = autodown.list_stale_tasks(older_than=older_than)
    tasks = result["tasks"]

    if json_mode:
        print(json.dumps({
            "boards": result["boards"],
            "tasks": tasks,
            "errors": result["errors"],
            "older_than": older_than,
        }))
    else:
        if not tasks and not result["errors"]:
            print("no stale cards — no non-terminal task is older than "
                  f"{older_than} day{'s' if older_than != 1 else ''}")
        for t in tasks:
            assignee = t["assignee"] or "-"
            print(
                f"{t['board']:<16} {t['id']:<14} {t['status']:<9} "
                f"{assignee:<18} {t['age_days']:>4}d  {t['title']}")
        if result["errors"]:
            print("\nWarnings (boards not fully scanned):", file=sys.stderr)
            for e in result["errors"]:
                print(f"  - {e}", file=sys.stderr)
    return 0


def cmd_kanban(argv):
    """Dispatch ``hscc kanban <subcommand>``; returns an exit code (never raises).

    With no subcommand (or ``--help``/``-h``) prints the group help and exits
    0, matching how the other group verbs handle their no-subcommand/``--help``
    case (api_cli.cmd_api, api_cli.py:275-284). Unknown subcommands exit
    non-zero.

    ``--json`` (for scripting) is honored by every subcommand.
    """
    if not argv or argv[0] in ("--help", "-h"):
        print(HELP_TEXT)
        return 0

    json_mode = "--json" in argv
    # Strip --json so no subcommand has to re-filter it from its own rest.
    argv = [a for a in argv if a != "--json"]

    sub = argv[0]
    rest = argv[1:]

    if sub == "stale":
        return _cmd_stale(rest, json_mode)

    print(f"Error: unknown kanban subcommand: {sub}")
    print(f"Valid subcommands: {', '.join(VALID_SUBCOMMANDS)}")
    return 1
