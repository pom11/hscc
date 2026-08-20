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
"""

from __future__ import annotations

import argparse
import os
import sys

from ..core import git_state, project_lifecycle, registry


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
    print("  it does NOT delete the Telegram topic.")
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

    Attaches the injectable hooks (git runner, telegram client, kanban
    provider) to args so core calls are stubbable in tests without any of
    them touching a real system.
    """
    args.registry = registry_path
    args.run = getattr(args, "run", None)
    args.client = getattr(args, "client", None)
    args.kanban = getattr(args, "kanban", None)

    func = getattr(args, "func", None)
    if func is None:
        return _not_implemented()
    return func(args)
