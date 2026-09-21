"""project.py — `flightdeck project ...` — lifecycle + registry CRUD.

Presents the project lifecycle (``new``), the read-only registry view
(``list``), and the registry-only commands (``remove``, ``repair``) on top of
:mod:`flightdeck.core.project_lifecycle` (the orchestration) and
:mod:`flightdeck.core.registry` (the store). All logic lives in core — this
module is presentation + argparse + the mutating-command ``--apply`` gate per
the DESIGN ("Mutating without --apply is refused with the plan printed").

Commands:
    new     <name>  wire repo + topic + board + roadmap + registry (idempotent)
    list            name, repo, board, topic, health (read-only)
    remove  <name>  registry entry only — NEVER deletes the repo or the topic
    repair  <name>  same as re-running `new` on an existing (possibly partial)
                    project — fills only what is missing
    pull/push       safe fast-forward pull / gated branch push of registered repos
    chat    [name]  open an INTERACTIVE hermes session on the project's
                    orchestrator (the permanent thread the app + WS relay use)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..core import git_state, project_lifecycle, registry
from ..core import session_discovery

# --------------------------------------------------------------------------- #
# Orchestrator resolver (reused from hscc-roles so CLI/REST/WS never disagree)
# --------------------------------------------------------------------------- #
# The project → orchestrator identity resolver lives in hscc-roles/
# orchestrators.py (vendored verbatim; the REST chat handler in
# hscc-api/routes_orchestrator.py loads it the exact same way). We load it under
# the same sys.path pattern so `hscc project chat <name>` maps a project to the
# SAME {profile, session, board, repo} the iOS app and the WS relay use.
_ROLES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "hscc-roles"
if _ROLES_DIR.is_dir() and str(_ROLES_DIR) not in sys.path:
    sys.path.insert(0, str(_ROLES_DIR))

from orchestrators import (                      # noqa: E402
    OrchestratorError,
    UnknownProjectError,
    resolve_orchestrator,
)


def _not_implemented() -> int:
    print("project: command not recognised. Try `flightdeck project --help`.", file=sys.stderr)
    return 2


# --------------------------------------------------------------------------- #
# new — the idempotent lifecycle command
# --------------------------------------------------------------------------- #

def cmd_new(args: argparse.Namespace) -> int:
    name = args.name
    repo = args.repo or os.path.expanduser(f"~/dev/{name}")
    github = bool(args.github)
    private = bool(args.private)
    apply = bool(args.apply)
    dry_run = bool(args.dry_run)

    print(f"flightdeck project new {name}")
    print(f"  repo    {repo}" + ("  (--github --private)" if private else ("  (--github)" if github else "")))
    print("  topic   forum topic named after the project")
    print("  board   kanban board slug = project name")
    print("  roadmap ROADMAP.md seeded with Now/Next/Later")
    print("  registry entry binding repo <-> board <-> topic")

    if dry_run or not apply:
        print("\nplan printed; nothing performed.")
        print("pass --apply to create the project.")
        return 0

    result = project_lifecycle.create_project(
        name,
        repo=repo,
        registry_path=args.registry,
        github=github,
        private=private,
        _run=args.run,
        _client=args.client,
        _kanban=args.kanban,
        _session_db=args.session_db,
    )

    print("\nresult:")
    for step in result["steps"]:
        mark = "ok " if step["status"] == "ok" else ("-- " if step["status"] == "skipped" else "FAIL")
        print(f"  [{mark}] {step['id']:<8} {step['detail']}")
        if step["status"] == "failed":
            print(f"         retry: {step.get('retry', '')}")

    if not result["ok"]:
        retry = result.get("retry")
        print(f"\npartial failure recorded in the registry; what succeeded is kept.")
        if retry:
            print(f"retry command: {retry}")
        return 1

    print(f"\nproject {name!r} created.")
    return 0


# --------------------------------------------------------------------------- #
# list — read-only
# --------------------------------------------------------------------------- #

def cmd_list(args: argparse.Namespace) -> int:
    projects = registry.load_registry(args.registry)
    if not projects:
        print("No projects registered.")
        return 0

    if args.json:
        import json
        out = []
        for p in projects:
            out.append(
                {
                    "name": p.name,
                    "repo": p.repo,
                    "board": p.board or "unknown",
                    "topic": p.topic if p.topic is not None else "unknown",
                    "health": project_lifecycle.project_health(
                        p, _run=args.run, _client=args.client, _kanban=args.kanban
                    ),
                }
            )
        print(json.dumps(out))
        return 0

    print(f"{'NAME':<20} {'REPO':<40} {'BOARD':<12} {'TOPIC':<8} HEALTH")
    for p in projects:
        health = project_lifecycle.project_health(
            p, _run=args.run, _client=args.client, _kanban=args.kanban
        )
        print(
            f"{p.name:<20} {p.repo:<40} "
            f"{(p.board or 'unknown'):<12} "
            f"{(str(p.topic) if p.topic is not None else 'unknown'):<8} {health}"
        )
    return 0


# --------------------------------------------------------------------------- #
# remove — registry entry only, never the repo or the topic
# --------------------------------------------------------------------------- #

def cmd_remove(args: argparse.Namespace) -> int:
    name = args.name

    print(f"flightdeck project remove {name}")
    print("  this removes the REGISTRY ENTRY only.")
    print("  it does NOT delete the git repo.")
    print("  (the registry 'topic' field is cleared too — Telegram is removed.)")
    if not args.apply:
        print("\nplan printed; nothing performed.")
        print("pass --apply to remove the registry entry.")
        return 0

    try:
        registry.remove_project(name, path=args.registry)
    except registry.ProjectNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"\nremoved registry entry for {name!r}. The repo and topic are untouched.")
    return 0


# --------------------------------------------------------------------------- #
# repair — same as re-running `new` on an existing registry project
# --------------------------------------------------------------------------- #

def cmd_repair(args: argparse.Namespace) -> int:
    name = args.name

    try:
        proj = registry.get_project(name, path=args.registry)
    except registry.ProjectNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    repo = args.repo or proj.repo
    github = bool(args.github)
    private = bool(args.private)

    print(f"flightdeck project repair {name}")
    print(f"  repo    {repo}")
    print("  .. ensures repo / topic / board / roadmap / registry are all present")
    if not args.apply:
        print("\nplan printed; nothing performed.")
        print("pass --apply to repair the project.")
        return 0

    try:
        result = project_lifecycle.create_project(
            name,
            repo=repo,
            registry_path=args.registry,
            github=github,
            private=private,
            _run=args.run,
            _client=args.client,
            _kanban=args.kanban,
            _session_db=args.session_db,
        )
    except Exception as exc:  # the exact retry needs args; surface clearly
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print("\nresult:")
    for step in result["steps"]:
        mark = "ok " if step["status"] == "ok" else ("-- " if step["status"] == "skipped" else "FAIL")
        print(f"  [{mark}] {step['id']:<8} {step['detail']}")
        if step["status"] == "failed":
            print(f"         retry: {step.get('retry', '')}")

    if not result["ok"]:
        retry = result.get("retry")
        if retry:
            print(f"\nretry command: {retry}")
        return 1
    print(f"\nproject {name!r} repaired.")
    return 0


# --------------------------------------------------------------------------- #
# pull — safe fetch + fast-forward-only pull across registered project repos
# --------------------------------------------------------------------------- #

def _target_projects(args: argparse.Namespace, cmd: str) -> list | int:
    """Resolve the projects a pull/push operates on, or an exit code.

    With ``args.name`` set, returns the one matching project (erroring with
    exit code 2 and printing to stderr if it is not in the registry). Without
    a name, returns every registered project that has a ``repo`` configured.
    Returns a list on success, an int exit code on error.
    """
    if args.name:
        try:
            return [registry.get_project(args.name, path=args.registry)]
        except registry.ProjectNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    return [p for p in registry.load_registry(args.registry) if p.repo]


def cmd_pull(args: argparse.Namespace) -> int:
    """`flightdeck project pull [name]` — safe fetch + fast-forward pull.

    Pull is deliberately NOT gated behind ``--apply``: it only ever
    fast-forwards or skips (never force, never stashes, never auto-merges,
    never touches a non-default branch or a dirty tree), so it is low-risk
    enough to run by default like ``standup`` -- a no-op experiment it cannot
    harm the user. The genuinely risky operation in this family is ``push``
    (visible to others), which IS gated behind ``--apply``; see ``cmd_push``.
    """
    resolved = _target_projects(args, "pull")
    if isinstance(resolved, int):
        return resolved
    targets = resolved

    heading = f"flightdeck project pull{' ' + args.name if args.name else ''}"
    rows = []
    for proj in targets:
        res = git_state.pull_project(proj.repo, _run=args.run)
        rows.append({
            "name": proj.name,
            "repo": proj.repo,
            "status": res["status"],
            "detail": res["detail"],
            "n": res["n"],
        })

    if getattr(args, "json", False):
        import json
        print(json.dumps(rows))
        return 0

    print(heading)
    print(f"{'NAME':<20} RESULT")
    for row in rows:
        if row["status"] == "pulled":
            line = f"{row['name']:<20} pulled {row['n']} commit(s)"
        elif row["status"] == "up_to_date":
            line = f"{row['name']:<20} already up to date"
        else:
            line = f"{row['name']:<20} SKIPPED: {row['detail']}"
        print(line)
    return 0


# --------------------------------------------------------------------------- #
# push — push only the current branch, gated behind --apply (never --force)
# --------------------------------------------------------------------------- #

def cmd_push(args: argparse.Namespace) -> int:
    """`flightdeck project push [name]` — push the current branch, gated.

    Push is a \"visible to others\" action, so per the project's safety posture
    (the same gate ``review``/``release`` put behind ``--apply``) it is gated
    here too. Without ``--apply`` it only reports what WOULD be pushed (the
    ahead-count per project) and changes nothing. With ``--apply`` it pushes
    only the branch actually checked out, to its own upstream, never with
    ``--force``.
    """
    resolved = _target_projects(args, "push")
    if isinstance(resolved, int):
        return resolved
    targets = resolved

    apply = bool(args.apply)
    heading = f"flightdeck project push{' ' + args.name if args.name else ''}"
    rows = []
    for proj in targets:
        branch = git_state.current_branch(proj.repo, _run=args.run)
        upstream = (
            git_state.upstream_of(proj.repo, branch, _run=args.run)
            if branch
            else None
        )
        ahead = (
            git_state.ahead_of_upstream(proj.repo, branch, _run=args.run)
            if branch
            else 0
        )

        if apply:
            res = git_state.push_project(proj.repo, _run=args.run)
            rows.append({
                "name": proj.name,
                "repo": proj.repo,
                "branch": res["branch"],
                "upstream": res["upstream"],
                "ahead": res["n"],
                "status": res["status"],
                "detail": res["detail"],
            })
        else:
            # Dry-run: report what WOULD be pushed, push nothing.
            if branch is None:
                rows.append({
                    "name": proj.name, "repo": proj.repo, "branch": None,
                    "upstream": None, "ahead": 0,
                    "status": "skipped", "detail": "not a git repository",
                })
            elif branch == "HEAD":
                rows.append({
                    "name": proj.name, "repo": proj.repo, "branch": branch,
                    "upstream": None, "ahead": 0,
                    "status": "skipped", "detail": "detached HEAD; nothing to push",
                })
            elif not upstream:
                rows.append({
                    "name": proj.name, "repo": proj.repo, "branch": branch,
                    "upstream": None, "ahead": 0,
                    "status": "skipped",
                    "detail": f"no upstream tracking branch for {branch}; nothing to push",
                })
            elif ahead == 0:
                rows.append({
                    "name": proj.name, "repo": proj.repo, "branch": branch,
                    "upstream": upstream, "ahead": 0,
                    "status": "up_to_date", "detail": "nothing to push",
                })
            else:
                rows.append({
                    "name": proj.name, "repo": proj.repo, "branch": branch,
                    "upstream": upstream, "ahead": ahead,
                    "status": "would_push",
                    "detail": f"{ahead} commit(s) ahead of {upstream} (pass --apply to push)",
                })

    if getattr(args, "json", False):
        import json
        print(json.dumps(rows))
        return 0

    print(heading + "  (--apply not given: nothing was pushed)" if not apply else heading)
    print(f"{'NAME':<20} RESULT")
    for row in rows:
        if row["status"] == "pushed":
            line = f"{row['name']:<20} pushed {row['ahead']} commit(s) to {row['upstream']}"
        elif row["status"] == "would_push":
            line = f"{row['name']:<20} would push {row['ahead']} commit(s) to {row['upstream']}"
        elif row["status"] == "up_to_date":
            line = f"{row['name']:<20} nothing to push"
        else:
            line = f"{row['name']:<20} SKIPPED: {row['detail']}"
        print(line)
    return 0


# --------------------------------------------------------------------------- #
# chat — drop the operator into an INTERACTIVE hermes session on the
# project's orchestrator (the permanent thread the app + WS relay use)
# --------------------------------------------------------------------------- #

def _detect_project(args, registry_path) -> str | None:
    """Resolve the chat target project: explicit name, else detect from cwd.

    Returns the project name, or ``None`` when neither an explicit ``--name``
    nor a cwd match was available (the caller then fails loudly listing the
    valid names). An explicit choice always wins; detection is never silent.
    The detection note goes to STDOUT (the operator's TTY) alongside the
    identity banner — stderr is reserved for errors that exit non-zero.
    """
    projects = registry.load_registry(registry_path)
    explicit = getattr(args, "name", None)
    name, _detected = registry.resolve_project_arg(
        projects, explicit,
        cwd=getattr(args, "cwd", None),
        _print=print,
    )
    return name


def _valid_names(registry_path) -> list[str]:
    from orchestrators import list_registry_projects
    return list_registry_projects(registry_path)


def _resume_argv(resume_id: str, discovery: dict) -> tuple[str, list] | None:
    """Pick ``(profile, argv)`` for ``--resume <id>`` from a discovery result.

    The id may name the project's orchestrator session (on ``<name>-orch``) or
    a telegram session belonging to the project (on the DEFAULT profile). Both
    are resumable; the difference is ONLY the profile hermes must run under —
    get this wrong and `hermes --resume` looks in the wrong profile's DB and
    reports the session missing. Returns ``None`` when the id is not one of the
    project's sessions (the caller fails loudly rather than guessing).
    """
    orch = discovery.get("orchestrator") or {}
    if orch.get("id") == resume_id:
        return (discovery["orch_profile"], ["hermes", "-p", discovery["orch_profile"], "--resume", resume_id])
    for row in discovery.get("telegram", []):
        if row.get("id") == resume_id:
            return (discovery["telegram_profile"], ["hermes", "-p", discovery["telegram_profile"], "--resume", resume_id])
    return None


def _print_history_note(discovery: dict) -> None:
    """Print a TTY note when a project has Telegram history to discover.

    Does NOT auto-resume: telegram sessions are a different thread and merging
    identities silently is worse than the current behaviour. The note points at
    ``project sessions`` and ``--resume``. Writes to stdout (operator-facing),
    matching the identity banner; stderr stays reserved for fail-non-zero.
    """
    rows = discovery.get("telegram", [])
    if not rows:
        return
    total_msgs = sum(r.get("message_count") or 0 for r in rows)
    plural = "" if len(rows) == 1 else "s"
    print(
        f"note: {len(rows)} earlier telegram session{plural} for this project "
        f"({total_msgs} msgs) — `hscc project sessions {discovery['project']}` "
        f"to list, `--resume <id>` to open"
    )


def cmd_chat(args: argparse.Namespace) -> int:
    """`hscc project chat [name] [--resume <id>] [-- <hermes args>]` — interactive hermes.

    Resolves the project → orchestrator identity exactly as the REST chat
    handler does (``resolve_orchestrator``), ensures the permanent session
    exists (creating it on first use, idempotently), prints a banner naming
    the identity the operator is about to speak as, then EXECs hermes
    interactively — a real TTY, stdout never captured, no -q/-Q.

    ``--resume <session_id>`` opens a SPECIFIC session by id instead of the
    orchestrator thread: a telegram session for the project (lives on the
    DEFAULT profile) or the orchestrator session itself. The id is validated
    against the project's discovered sessions so a typo never silently opens a
    fresh, wrong identity. When the project has Telegram history and no resume
    id is given, a note says so rather than silently starting fresh.

    Unknown project: fail loudly, exit 2, and list the valid names. We never
    fall back to a default project or the general orchestrator — silently
    talking to the wrong orchestrator is worse than an error.
    """
    name = _detect_project(args, args.registry)
    if not name:
        valid = _valid_names(args.registry)
        listed = ", ".join(valid) if valid else "(none registered)"
        print(
            f"project chat: no project given and none detected from the current "
            f"directory.\nvalid projects: {listed}",
            file=sys.stderr,
        )
        return 2

    try:
        resolved = resolve_orchestrator(name, path=args.registry)
    except UnknownProjectError as exc:
        valid = _valid_names(args.registry)
        listed = ", ".join(valid) if valid else "(none registered)"
        print(f"project chat: {exc}\nvalid projects: {listed}", file=sys.stderr)
        return 2
    except OrchestratorError as exc:
        print(f"project chat: {exc}", file=sys.stderr)
        return 2

    profile = resolved["profile"]
    session = resolved["session"]

    resume_id = getattr(args, "resume_id", None)
    if resume_id:
        return _cmd_chat_resume(name, resume_id, args, resolved)

    # Surface any discoverable Telegram history BEFORE attaching, so the
    # operator is never left thinking a project with months of real work is
    # empty. Read-only through hermes' own API (see session_discovery).
    discovery = session_discovery.list_project_sessions(
        name, path=args.registry,
        _session_db=args.session_db, _default_db=args.default_db,
    )
    _print_history_note(discovery)

    # A brand-new project has no session yet; CREATE it on first use so the
    # subsequent `--continue <session>` resolves. Idempotent, never clobbers.
    # Fail-soft: an ensure that can't verify must not block the attach — the
    # exec'd hermes then fails honestly if the session truly doesn't exist.
    try:
        ensured = project_lifecycle.ensure_session(session, profile, _session_db=args.session_db)
    except project_lifecycle.LifecycleError:
        ensured = None
    if ensured and ensured.get("status") == "created":
        # STDOUT like the identity banner: all operator-facing status before
        # exec'ing belongs on the TTY; stderr is reserved for failing non-zero.
        print(
            f"created session '{session}' on {profile!r} (first use)",
        )

    # Banner: make the joined identity unmistakable — this is the project's
    # PERMANENT orchestrator thread, shared with the iOS app and the WS relay.
    # A worker once contaminated hscc-orch's permanent session with
    # worker-scoped context; the operator must never be in doubt who they speak
    # as. Explicit > implicit; print before exec'ing so it is on the terminal.
    print(f"project {name} -> profile {profile}, session '{session}' "
          f"(permanent, shared with the app)")

    argv = ["hermes", "-p", profile, "chat", "--continue", session]
    trailing = getattr(args, "extra", None) or []
    if trailing:
        argv += list(trailing)

    exec_seam = getattr(args, "exec_seam", None) or os.execvp
    # os.execvp replaces this process — the operator gets a real TTY. On a
    # success path this never returns; the return 0 is for the seam-injected
    # tests (and is unreachable in production).
    exec_seam("hermes", argv)
    return 0


def _cmd_chat_resume(name: str, resume_id: str, args, resolved: dict) -> int:
    """Exec hermes resuming ``resume_id`` on the project session's right profile.

    Resolves the resume target against the project's discovered sessions so a
    typo'd or foreign session id fails loudly instead of silently starting a
    fresh, wrong identity. The orchestrator session resumes on ``<name>-orch``;
    a telegram session resumes on the DEFAULT profile (get this wrong and
    ``hermes --resume`` looks in the wrong profile's DB and reports "not found").
    """
    discovery = session_discovery.list_project_sessions(
        name, path=args.registry,
        _session_db=args.session_db, _default_db=args.default_db,
    )
    target = _resume_argv(resume_id, discovery)
    if target is None:
        valid = (
            f"  {discovery['orch_profile']}: {discovery['orchestrator']['id']}"
            if discovery.get("orchestrator") else ""
        )
        telegram = ", ".join(
            r["id"] for r in discovery.get("telegram", [])
        ) or "(none for this project)"
        print(
            f"project chat: no session '{resume_id}' belongs to project "
            f"{name!r}.\n"
            f"  this project's sessions:\n"
            f"  {valid}\n"
            f"  {discovery['telegram_profile']} (telegram): {telegram}",
            file=sys.stderr,
        )
        return 2

    profile, argv = target
    print(f"project {name} -> profile {profile}, resuming session '{resume_id}'")
    exec_seam = getattr(args, "exec_seam", None) or os.execvp
    exec_seam("hermes", argv)
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    """`hscc project sessions <name>` — every session belonging to a project.

    Lists the project's sessions — the permanent CLI orchestrator session AND
    the Telegram ones whose ``thread_id`` equals the project's registry
    ``topic`` — with id, title, message count, first/last activity and source,
    newest first. Read-only: it opens every state.db through Hermes' own
    ``SessionDB`` in read-only mode; nothing is ever written.

    Name & ordering: called ``sessions`` (not ``history`` / ``list``) to match
    ``hermes sessions`` — the CLI the data actually comes from — so the
    subcommand reads as "the project's view of *those* sessions". Actually
    ``sessions`` also shadows nothing in ``project`` and reads naturally:
    ``hscc project sessions <name>``.
    """
    name = args.name
    try:
        discovery = session_discovery.list_project_sessions(
            name, path=args.registry,
            _session_db=args.session_db, _default_db=args.default_db,
        )
    except UnknownProjectError as exc:
        valid = _valid_names(args.registry)
        listed = ", ".join(valid) if valid else "(none registered)"
        print(f"project sessions: {exc}\nvalid projects: {listed}", file=sys.stderr)
        return 2
    except OrchestratorError as exc:
        print(f"project sessions: {exc}", file=sys.stderr)
        return 2

    topic = discovery.get("topic")
    print(f"project {name} sessions (topic "
          f"{topic if topic is not None else '(none)'}, "
          f"telegram profile {discovery['telegram_profile']}):")
    # Orchestrator session first (the primary identity), then telegram newest-first.
    orch = discovery.get("orchestrator")
    if orch:
        print(_format_session_row(orch, discovery["orch_profile"], current=True))
    else:
        print(
            f"  {discovery['orch_profile']}: (no orchestrator session found)\n"
            f"    - `hscc project chat {name}` creates it on first use"
        )
    if discovery["telegram"]:
        for row in discovery["telegram"]:
            print(_format_session_row(row, discovery["telegram_profile"], current=True))
    else:
        reason = (
            "project has no telegram topic"
            if topic is None
            else f"no telegram sessions on thread {topic}"
        )
        print(f"  {discovery['telegram_profile']}: (no telegram history — {reason})")
    return 0


def _format_session_row(row: dict, profile: str, *, current: bool = False) -> str:
    """One human-readable ``sessions`` line: id, title, msgs, activity, source."""
    first = _fmt_time(row.get("first"))
    last = _fmt_time(row.get("last"))
    marker = " *" if current else "  "
    src = f"{row['source']} ({profile})"
    msgs = row.get("message_count", 0)
    return (
        f"{marker} {src:<24} {msgs:>4} msgs  {first} .. {last}\n"
        f"    {row.get('id')}  {row.get('title')}"
    )


def _fmt_time(ts) -> str:
    """Format a hermes timestamp (float epoch) to a compact ISO/Y-M-D, or '—'."""
    if not ts:
        return "—"
    try:
        import datetime as _dt
        return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return str(ts)


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #

def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("project", help="project lifecycle + registry CRUD",
                       epilog="example: flightdeck project list")
    subsub = p.add_subparsers(dest="project_cmd", metavar="PROJECT_CMD")

    sp = subsub.add_parser("new", help="wire repo + topic + board + roadmap + registry",
                           epilog="example: flightdeck project new flightdeck --github")
    sp.add_argument("name", help="project name (also the repo dir basename and board slug)")
    sp.add_argument("--repo", help="repo path (default ~/dev/<name>)")
    sp.add_argument("--github", nargs="?", const=True, default=False,
                    help="create a GitHub remote via `gh` and push")
    sp.add_argument("--private", action="store_true",
                    help="with --github, make the remote private")
    _add_apply(sp)
    sp.set_defaults(func=cmd_new)

    sp = subsub.add_parser("list", help="name, repo, board, topic, health (read-only)",
                           epilog="example: flightdeck project list")
    sp.set_defaults(func=cmd_list)

    sp = subsub.add_parser("remove", help="remove a REGISTRY entry (never the repo/topic)",
                           epilog="example: flightdeck project remove flightdeck --apply")
    sp.add_argument("name", help="project name in the registry")
    _add_apply(sp)
    sp.set_defaults(func=cmd_remove)

    sp = subsub.add_parser("repair", help="re-run `new` on an existing registry project",
                           epilog="example: flightdeck project repair flightdeck")
    sp.add_argument("name", help="project name in the registry")
    sp.add_argument("--repo", help="override the repo path (default: from registry)")
    sp.add_argument("--github", nargs="?", const=True, default=False,
                    help="also ensure a GitHub remote via `gh`")
    sp.add_argument("--private", action="store_true", help="private GitHub remote")
    _add_apply(sp)
    sp.set_defaults(func=cmd_repair)

    sp = subsub.add_parser("pull", help="safe fast-forward pull of a registered project's repo",
                           epilog="example: flightdeck project pull flightdeck   (no --apply: only ever fast-forwards or skips)")
    sp.add_argument("name", nargs="?", default=None,
                    help="project name (default: every registered project with a repo)")
    sp.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    sp.set_defaults(func=cmd_pull)

    sp = subsub.add_parser("push", help="push the current branch (gated behind --apply, never --force)",
                           epilog="example: flightdeck project push flightdeck --apply")
    sp.add_argument("name", nargs="?", default=None,
                    help="project name (default: every registered project with a repo)")
    sp.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    _add_apply(sp)
    sp.set_defaults(func=cmd_push)

    sp = subsub.add_parser("chat", help="open an INTERACTIVE hermes session on the project's orchestrator",
                           epilog="example: flightdeck project chat flightdeck   (attaches to the permanent orchestrator thread the app uses)")
    sp.add_argument("name", nargs="?", default=None,
                    help="project name (default: detect from the current directory)")
    sp.add_argument("--resume", dest="resume_id", metavar="SESSION_ID", default=None,
                    help="resume a SPECIFIC session by id (a telegram session for this project, on the DEFAULT profile, or the orchestrator session) instead of attaching to the orchestrator thread")
    sp.add_argument("extra", nargs=argparse.REMAINDER,
                    help="trailing args passed through to hermes (after --), e.g. '-- --resume <id>'")
    sp.set_defaults(func=cmd_chat)

    sp = subsub.add_parser("sessions", help="list every session belonging to a project (orchestrator + telegram), newest first",
                           epilog="example: flightdeck project sessions flightdeck   (resolves registry topic -> sessions.thread_id on the DEFAULT profile; read-only)")
    sp.add_argument("name", help="project name in the registry")
    sp.set_defaults(func=cmd_sessions)


def _add_apply(sp: argparse.ArgumentParser) -> None:
    sp.add_argument(
        "--apply",
        action="store_true",
        help="perform the change (mutating commands are dry-run by default)",
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and touch nothing (alias of simply omitting --apply)",
    )


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: run a project subcommand.

    Attaches the injectable hooks (git runner, kanban
    provider) to args so core calls are stubbable in tests without any of
    them touching a real system.
    """
    args.registry = registry_path
    args.run = getattr(args, "run", None)
    args.client = getattr(args, "client", None)
    args.kanban = getattr(args, "kanban", None)
    args.session_db = getattr(args, "session_db", None)
    args.default_db = getattr(args, "default_db", None)
    args.cwd = getattr(args, "cwd", None)
    args.exec_seam = getattr(args, "exec_seam", None)

    func = getattr(args, "func", None)
    if func is None:
        return _not_implemented()
    return func(args)
