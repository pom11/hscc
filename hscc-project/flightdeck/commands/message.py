"""message.py — send / read / dispatch / broadcast, addressed by PROJECT.

Mirrors topics.py (``flightdeck topics ...``) in the house convention: this one
file owns ONE top-level subcommand, ``message``, whose operations are its
sub-subcommands, so it integrates with cli.py's auto-discovery seam without
touching cli.py:

    flightdeck message send <project> "msg"
    flightdeck message read <project> [-n N]
    flightdeck message dispatch <project> "task" [--assignee X]
    flightdeck message broadcast "msg" [--to a,b,c]

This is the DESIGN "Messaging" addendum rendered through the mechanism cli.py
gives us (module name -> top-level subcommand). Every operation resolves its
Telegram topic through the project registry — an operator never handles a raw
topic id.

Presentation only: all transport logic lives in core modules, both reused
rather than reimplemented — :mod:`flightdeck.core.telegram` (send / read) and
:mod:`flightdeck.core.kanban` (card creation in dispatch).

Behavior contract:
- ``send`` / ``read`` are the interactive path and act immediately (no --apply).
- ``dispatch`` is mutating: it creates a card on the project's board AND
  announces it in the topic, so chat and board cannot diverge. Dry-run by
  default — ``--apply`` actually creates the card and sends the announcement.
  If the card is created but the announcement fails, it says so explicitly and
  prints the card id — never reports success for a half-done dispatch.
- ``broadcast`` reports per-project success/failure individually, never a
  single aggregate "done".
- A project with no ``topic`` in the registry gives the actionable error
  "project <X> has no topic; run: flightdeck project repair <X>", never a crash
  or a silent no-op. Unknown projects error clearly too.
- The single-writer Telegram session ("database is locked") surfaces as a clear
  message with a retry hint, never a traceback.
"""

from __future__ import annotations

import argparse
import sys

from ..core import kanban, registry, telegram
from ..core.telegram import TelegramError, TopicLockedError


def _resolve_topic(projects: list[registry.Project], project_name: str):
    """Return ``(topic_id, None)`` or ``(None, error_string)`` for a project.

    Resolves the topic through the registry so the operator never handles a raw
    topic id. Unknown projects and topic-less projects produce a clear,
    actionable string rather than raising — the caller decides the exit code.
    """
    for proj in projects:
        if proj.name == project_name:
            if proj.topic is None:
                return (
                    None,
                    f"project {project_name} has no topic; "
                    f"run: flightdeck project repair {project_name}",
                )
            return proj.topic, None
    return None, f"unknown project: {project_name!r} (check `flightdeck projects list`)"


def _get_project(projects: list[registry.Project], project_name: str):
    """Return the Project row, or None if absent."""
    for proj in projects:
        if proj.name == project_name:
            return proj
    return None


def _locked_message(exc: TopicLockedError) -> str:
    return (
        f"error: {exc}\n"
        "hint: another process is probably holding the ~/.hermes-tg session; "
        "wait a moment and retry."
    )


# --------------------------------------------------------------------------- #
# send
# --------------------------------------------------------------------------- #

def cmd_send(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    if not args.message:
        print(
            "error: message text is required. "
            "usage: flightdeck message send [project] \"message\"",
            file=sys.stderr,
        )
        return 2
    topic_id, err = _resolve_topic(projects, args.project)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    try:
        telegram.send_message(topic_id, args.message, _client=args.client)
    except TopicLockedError as exc:
        print(_locked_message(exc), file=sys.stderr)
        return 3
    except TelegramError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"sent to {args.project} (topic {topic_id}).")
    return 0


# --------------------------------------------------------------------------- #
# read — interactive path, acts immediately
# --------------------------------------------------------------------------- #

def cmd_read(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    topic_id, err = _resolve_topic(projects, args.project)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    try:
        msgs = telegram.read_messages(topic_id, n=args.n, _client=args.client)
    except TopicLockedError as exc:
        print(_locked_message(exc), file=sys.stderr)
        return 3
    except TelegramError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not msgs:
        print(f"{args.project}: no messages in topic {topic_id}.")
        return 0
    if args.json:
        import json

        print(
            json.dumps(
                [
                    {"timestamp": m.timestamp, "sender": m.sender, "text": m.text}
                    for m in msgs
                ]
            )
        )
        return 0
    for m in msgs:  # already newest last from the core
        prefix = f"[{m.timestamp}] " if m.timestamp else ""
        print(f"{prefix}{m.sender}: {m.text}")
    return 0


# --------------------------------------------------------------------------- #
# dispatch — create a card AND announce, so chat and board cannot diverge
# --------------------------------------------------------------------------- #

def _read_body(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """Resolve the card body source for dispatch.

    Returns ``(body, error)``. When ``--body-file`` is set, the body is read
    from that file (``PATH == '-'`` reads full body text from stdin so a
    caller can pipe a multi-line spec without a temp file). Returns a clear
    error string for an unreadable file — the caller decides the exit code.
    When ``--body-file`` is NOT given returns ``(None, None)`` so the caller's
    existing behaviour (body = announcement) is unchanged.
    """
    body_file = getattr(args, "body_file", None)
    if body_file is None:
        return None, None
    if body_file == "-":
        return sys.stdin.read(), None
    try:
        with open(body_file, "r", encoding="utf-8") as fh:
            return fh.read(), None
    except OSError as exc:
        return None, f"could not read body file {body_file!r}: {exc}"


def cmd_dispatch(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    if not args.task:
        print(
            "error: a task title is required. "
            "usage: flightdeck message dispatch [project] \"task\" [--apply]",
            file=sys.stderr,
        )
        return 2
    proj = _get_project(projects, args.project)
    if proj is None:
        print(
            f"error: unknown project: {args.project!r} "
            "(check `flightdeck projects list`)",
            file=sys.stderr,
        )
        return 2

    # Resolve BOTH surfaces up front so a half-config project fails before any
    # mutation — never a half-done dispatch. No topic -> cannot announce. The
    # board is resolved from the project's OWN board (registry ``board``); a
    # project with NO board falls back to Hermes' current board and we SAY SO —
    # a silent fallback is how cards end up on the wrong board.
    if proj.topic is None:
        print(
            f"error: project {args.project} has no topic; "
            f"run: flightdeck project repair {args.project}",
            file=sys.stderr,
        )
        return 2
    # Anchor the card in the project's OWN repo as a fresh worktree — a card
    # with no repo anchor is exactly the bug this fixes (silently degrades to
    # an unanchored scratch dir with no access to the project's git files).
    # Refuse up front (matching cmd_migrate_card's no-repo guard and the
    # no-topic guard just above) rather than creating an unanchored card.
    if not proj.repo:
        print(
            f"error: project {args.project} has no repo; cannot anchor a worktree. "
            f"run: flightdeck project repair {args.project}",
            file=sys.stderr,
        )
        return 2
    board, used_fallback = kanban.resolve_project_board(proj, _kdb=getattr(args, "_kdb", None))
    if used_fallback:
        print(
            f"no board for {args.project}; created on '{board}'",
            file=sys.stderr,
        )

    # The card body comes from --body-file when given (so a real spec can be
    # attached); otherwise it defaults to the announcement text, exactly as
    # before. The MCP adapter injects the body string directly via ``args.body``
    # (it has no local-file ergonomic), which takes precedence over both.
    # --body-file and --message are allowed to differ on purpose: a short chat
    # ping vs. a long spec.
    body = getattr(args, "body", None)
    if body is None:
        body, body_err = _read_body(args)
        if body_err:
            print(f"error: {body_err}", file=sys.stderr)
            return 2

    # The announcement text defaults to the task title so a bare
    # `dispatch <project> "task"` always posts something meaningful.
    announcement = args.message if args.message is not None else args.task

    # Cross-project dependents: a nudge in the card body itself, so whoever
    # picks up the card sees that other projects depend on this one — never
    # blocking, matches the registry's advisory-only design (see
    # registry.dependent_notice). None when the project has no dependents.
    dependents_notice = registry.dependent_notice(proj.name, projects)

    # Dry-run by default: preview exactly what would be written, then require
    # --apply to actually create the card AND send the announcement — matching
    # every other mutating flightdeck command (migrate-card, roadmp adopt,
    # release, ...). dispatch is a stronger action than a plain `send` (it also
    # creates a durable card and anchors a worktree), so it earns the gate.
    if not getattr(args, "apply", False):
        print(f"dispatch (dry-run) project={args.project} board={board!r}:")
        print(f"  card title: {args.task}")
        if assignee := getattr(args, "assignee", None):
            print(f"  assignee  : {assignee}")
        if body is not None:
            print(f"  card body : {body}")
        elif announcement != args.task:
            print(f"  card body : {announcement}")
        print(f"  announce  : {announcement}")
        if dependents_notice:
            print(f"  dependents: {dependents_notice}")
        print("dry-run: pass --apply to create the card and send the announcement.", file=sys.stderr)
        return 0

    # 1. Create the card on the resolved board. A failure here means nothing
    #    was dispatched. When --body-file was NOT given, the body defaults to
    #    the announcement text (--message or the task title), exactly as before
    #    — fully backward compatible.
    try:
        card_body = body if body is not None else announcement
        if dependents_notice:
            card_body = f"{card_body}\n\n[flightdeck] {dependents_notice}"
        card_id = kanban.create_task(
            board,
            args.task,
            assignee=args.assignee,
            body=card_body,
            workspace_kind="worktree",
            workspace_path=proj.repo,
            _kdb=getattr(args, "_kdb", None),
        )
    except kanban.KanbanError as exc:
        print(f"error: could not create card on board {board!r}: {exc}", file=sys.stderr)
        return 2

    # 2. Announce it in the topic.
    try:
        telegram.send_message(proj.topic, announcement, _client=args.client)
    except (TopicLockedError, TelegramError) as exc:
        print(
            f"error: card {card_id} was created on board {board!r} but the "
            f"announcement FAILED: {exc}",
            file=sys.stderr,
        )
        print(f"card id: {card_id}", file=sys.stderr)
        print("dispatch is PARTIAL — the card exists but was not announced.", file=sys.stderr)
        return 1

    print(f"card {card_id} created on board {board!r} and announced to topic {proj.topic}.")
    return 0


# --------------------------------------------------------------------------- #
# broadcast — per-target results, never a single aggregate "done"
# --------------------------------------------------------------------------- #

def cmd_broadcast(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    if args.to:
        targets = [t.strip() for t in args.to.split(",") if t.strip()]
    else:
        targets = [p.name for p in projects]

    results: list[dict] = []
    for name in targets:
        topic_id, err = _resolve_topic(projects, name)
        entry = {"project": name}
        if err:
            entry["ok"] = False
            entry["error"] = err
        else:
            try:
                telegram.send_message(topic_id, args.message, _client=args.client)
                entry["ok"] = True
                entry["topic"] = topic_id
            except (TopicLockedError, TelegramError) as exc:
                entry["ok"] = False
                entry["error"] = str(exc)
        results.append(entry)

    if args.json:
        import json

        print(json.dumps(results))
    else:
        for r in results:
            if r["ok"]:
                print(f"OK   {r['project']} (topic {r['topic']})")
            else:
                print(f"FAIL {r['project']}: {r['error']}")

    return 0 if all(r["ok"] for r in results) else 1


# --------------------------------------------------------------------------- #
# subparser + entry point
# --------------------------------------------------------------------------- #

def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("message", help="send / read / dispatch / broadcast by project",
                       epilog="example: flightdeck message dispatch flightdeck \"ship 0.6.0\" --apply")
    msub = p.add_subparsers(dest="message_cmd", metavar="MESSAGE_CMD")

    sp = msub.add_parser("send", help="post a message to a project's topic",
                         epilog='example: flightdeck message send flightdeck "standing up now"')
    sp.add_argument("project", nargs="?", default=None,
                    help="project name in the registry (default: detected from cwd)")
    sp.add_argument("message", nargs="?", default=None, help="message text to post")
    sp.set_defaults(func=cmd_send)

    sp = msub.add_parser("read", help="recent messages from a project's topic",
                         epilog="example: flightdeck message read flightdeck -n 20")
    sp.add_argument("project", nargs="?", default=None,
                    help="project name in the registry (default: detected from cwd)")
    sp.add_argument("-n", type=int, default=10, help="number of messages (default: 10)")
    sp.set_defaults(func=cmd_read)

    sp = msub.add_parser("dispatch", help="create a card on a project's board AND announce it",
                         epilog='example: flightdeck message dispatch flightdeck "ship 0.6.0" --apply')
    sp.add_argument("project", nargs="?", default=None,
                    help="project name in the registry (default: detected from cwd)")
    sp.add_argument("task", nargs="?", default=None, help="card title on the project's board")
    sp.add_argument("--assignee", metavar="X", help="profile to assign the card to")
    sp.add_argument("--message", default=None, help="announcement text (default: the task title)")
    sp.add_argument(
        "--body-file",
        metavar="PATH",
        default=None,
        help=(
            "read the CARD BODY from PATH ('-' reads the full body from stdin). "
            "When given, --message becomes ONLY the announcement text and may "
            "differ from the card body (a short chat ping vs. a long spec). "
            "Default: the card body is the announcement text."
        ),
    )
    sp.add_argument(
        "--apply",
        action="store_true",
        help="actually create the card and send the announcement (dry-run by "
        "default; nothing is created or posted without this)",
    )
    sp.set_defaults(func=cmd_dispatch)

    sp = msub.add_parser("broadcast", help="one message to several projects' topics",
                         epilog='example: flightdeck message broadcast "outage over" --to flightdeck,hscc')
    sp.add_argument("message", help="message text to broadcast")
    sp.add_argument("--to", metavar="a,b,c", default=None, help="comma-separated project names (default: all)")
    sp.set_defaults(func=cmd_broadcast)


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: run a message subcommand with shared client + registry.

    Attaches the injectable client so core calls are stubbable in tests, and
    loads projects once for all subcommands.

    For ``send``/``read``/``dispatch`` the project is now OPTIONAL: with no
    positional project given (or, for the two-positional ``send``/``dispatch``,
    a single token that is not a registered project name) it is auto-detected
    from the cwd and a visible note is printed. An explicit project always wins
    and is never silently overridden. See :func:`_resolve_message_project`.
    """
    args.registry = registry_path
    args.client = getattr(args, "client", None)
    args._kdb = getattr(args, "kdb", getattr(args, "_kdb", None))
    args.cwd = getattr(args, "cwd", None)
    projects = registry.load_registry(registry_path)

    func = getattr(args, "func", None)
    if func is None:
        print("message: command not recognised. Try `flightdeck message --help`.", file=sys.stderr)
        return 2

    _resolve_message_project(args, projects, func)
    return func(args, projects)


def _resolve_message_project(
    args: argparse.Namespace,
    projects: list[registry.Project],
    func,
) -> None:
    """Resolve the project positional for send/read/dispatch (else detect).

    Explicit always wins: when a genuine explicit project is present it is
    used untouched and no detection runs. Otherwise the project is
    auto-detected from the cwd (the project whose repo contains the current
    directory), and the one-line note is printed to stderr so the inference is
    never silent.

    The two-positional ``send``/``dispatch`` need one disambiguation rule for
    the single-positional case (argparse binds one token to the ``project``
    slot). If that single token names a registered project, it is the project
    (and the message/task is missing -> the cmd reports it). If it does NOT
    name any project, it is really the message/task and the project is detected
    from the cwd. This keeps an explicit ``project`` that happens to be the
    BARE message from being silently reinterpreted, and keeps ``send hello
    world`` (two tokens) honest: with both slots filled, (project, message) is
    honoured exactly and an unknown first token still errors as today.

    ``read`` has only one positional; a missing project detects directly.
    """
    project = getattr(args, "project", None)
    project_str = str(project) if project is not None else ""
    second_name = "message" if func is cmd_send else ("task" if func is cmd_dispatch else None)
    second = getattr(args, second_name, None) if second_name else None
    second_str = str(second) if second is not None else ""
    proj_names = {p.name for p in projects}

    def _detect():
        resolved, _detected = registry.resolve_project_arg(
            projects, None,
            cwd=getattr(args, "cwd", None),
            _print=lambda line: print(line, file=sys.stderr),
        )
        args.project = resolved
        return resolved

    if func in (cmd_send, cmd_dispatch):
        # Both slots filled -> explicit (project, message/task). Honour exactly.
        if project_str and second_str:
            return

        # Exactly one token: if it names a registered project it IS the project;
        # else it is the message/task and the project is detected from cwd.
        if project_str and not second_str:
            if project_str in proj_names:
                args.project = project_str  # project; message/task missing
                return
            setattr(args, second_name, project_str)
            args.project = None
            _detect()
            return

        # No tokens: detect the project (message/task still missing -> the cmd
        # reports it if no project is detectable either).
        setattr(args, second_name, second if second_str else None)
        _detect()
        return

    # read: single positional; a missing project detects directly.
    _detect()
