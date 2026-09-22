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
from ..core import bindings
from ..core import digest as digest_core

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


def _seed_empty_orchestrator(
    name: str, session: str, profile: str, args, discovery: dict, ensured: dict | None,
) -> int:
    """Seed the project's EMPTY orchestrator session with the digest; return an exit code.

    Idempotency is the whole point: seed ONLY when the orchestrator session is
    CONFIRMED empty — either freshly created in this command (``ensured`` is
    ``created``) or a persisted row with ``message_count == 0`` (the provisioning
    placeholder). After one seed the session has the digest message, so the next
    chat sees a non-empty session and does NOT re-seed. Never guessed at: an
    unconﬁrmable session (no row, not just-created) fails closed to no-seed.

    ENV FAULT VS DATA (mandatory): an unreadable Hermes runtime renders as
    ``runtime_error`` — never as "empty session" and never as an uninformed
    seed. When the runtime is unreadable we return 3 (matching ``sessions`` /
    ``digest``) and do NOT seed, because seeding would claim to know a history
    we could not read.

    The digest is a SYNTHESIS, and it lands in the project's OWN orchestrator
    thread only — the Telegram sessions stay untouched on the DEFAULT profile.
    This is never a merge (no tool_call adjacency, no compaction-header hijack).

    Returns 0 to continue the attach, or a non-zero exit code to abort (3 on a
    runtime fault). All status goes to stdout (operator TTY); only a genuine
    failure goes to stderr.
    """
    # An unreadable runtime is NOT an empty session — report it and stop rather
    # than seeding an uninformed digest. Discovery already probed the runtime
    # (skip its own probe; it may be unreadable, so reuse discovery's verdict).
    if discovery.get("runtime_error"):
        print(
            f"project chat: cannot read session history: {discovery['runtime_error']}",
            file=sys.stderr,
        )
        print(
            "    - retry under the Hermes venv, e.g. "
            "~/.hermes/hermes-agent/venv/bin/hscc project chat " + name,
            file=sys.stderr,
        )
        return 3

    just_created = bool(ensured and ensured.get("status") == "created")
    orch_row = discovery.get("orchestrator")
    orch_empty = just_created or bool(
        orch_row is not None and (orch_row.get("message_count") or 0) == 0
    )

    # Build the digest with the archive/mapping seams (hidden argv for tests).
    # `_runtime_error_fn` returns discovery's verdict so the digest and the
    # discovery agree about the runtime — one source of truth, no double probe.
    digest = digest_core.build_digest(
        name,
        archive_dir=getattr(args, "archive_dir", None),
        mapping_path=getattr(args, "mapping_path", None),
        _runtime_error_fn=lambda: discovery.get("runtime_error"),
    )
    digest_sessions = digest.get("sessions") or []
    digest_msgs = digest.get("total_messages", 0)

    if not orch_empty:
        # Non-empty orchestrator session: never re-seed. Honest about what we're
        # NOT doing, and only when a digest would have been seeded (so an active
        # plain session with no archive history isn't spammed on every chat).
        if digest_sessions:
            print(
                f"session '{session}' already has history — not re-seeding "
                f"the {len(digest_sessions)}-thread / {digest_msgs}-message digest"
            )
        return 0

    # Orchestrator session IS empty — seed the digest if there is one.
    if not digest_sessions:
        # Nothing to seed. Keep the honest empty-start messaging (a genuinely
        # new project with no archived history, or an empty project with none).
        if just_created:
            print(f"created session '{session}' on {profile!r} (first use)")
        else:
            print(f"session '{session}' has no archived history — starting fresh")
        return 0

    # Seed: append the digest verbatim as the opening user message. This is the
    # ONE sanctioned read-write to a session DB in the chat flow, and it targets
    # ONLY this project's own empty orchestrator thread.
    # The target id: for a freshly-created session (no persisted id yet) we must
    # seed the REAL created id (``ensured["session"]``), not the name that
    # ``--continue`` happens to resolve by title — otherwise the message would
    # land on an id hermes never continues. For an existing placeholder the
    # resolved ``session`` IS the id.
    seed_target = session
    if just_created and ensured and ensured.get("session"):
        seed_target = ensured["session"]
    seeded = project_lifecycle.seed_session_with_digest(
        seed_target, profile, digest_core.format_digest(digest),
        _session_db=args.session_db,
    )
    if seeded:
        plural = "" if len(digest_sessions) == 1 else "s"
        print(
            f"seeded session '{seed_target}' with the project digest "
            f"({len(digest_sessions)} thread{plural}, {digest_msgs} messages)"
        )
    else:
        print(
            f"could not seed session '{seed_target}' with the project digest "
            f"(write failed) — continuing with a blank session",
            file=sys.stderr,
        )
    return 0


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

    # Read-only discovery (see session_discovery): surfaces any Telegram
    # history that belongs to the project AND the orchestrator session's own
    # row (title / message count) — the input for the empty-seed decision.
    discovery = session_discovery.list_project_sessions(
        name, path=args.registry,
        _session_db=args.session_db, _default_db=args.default_db,
    )

    # A brand-new project has no session yet; CREATE it on first use so the
    # subsequent `--continue <session>` resolves. Idempotent, never clobbers.
    # Fail-soft: an ensure that can't verify must not block the attach — the
    # exec'd hermes then fails honestly if the session truly doesn't exist.
    try:
        ensured = project_lifecycle.ensure_session(session, profile, _session_db=args.session_db)
    except project_lifecycle.LifecycleError:
        ensured = None

    # Seed the EMPTY orchestrator session with the project digest so the
    # operator lands in a session that already knows the history instead of a
    # blank slate. Idempotent: seed ONLY when the session is confirmed empty —
    # after one seed the session is non-empty, so a second chat never re-injects.
    # This is a SYNTHESIS seeded into the project's OWN orchestrator thread;
    # the telegram sessions stay untouched in the DEFAULT profile (no merge).
    seed_rc = _seed_empty_orchestrator(
        name, session, profile, args, discovery, ensured,
    )
    if seed_rc != 0:
        return seed_rc

    _print_history_note(discovery)

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
    # An unreadable runtime is NOT an empty history — say which one it is.
    if discovery.get("runtime_error"):
        print(f"  cannot read session history: {discovery['runtime_error']}")
        print(f"    - retry under the Hermes venv, e.g.")
        print(f"      ~/.hermes/hermes-agent/venv/bin/hscc project sessions {name}")
        return 3

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


def cmd_digest(args: argparse.Namespace) -> int:
    """`hscc project digest <name>` — a bounded, readable digest of a project's history.

    Synthesises the project's archive markdown (``~/.hermes/archive/telegram/
    <name>/``) into a short human extract: per session, title, date range,
    message count, a bounded ``decided/outcome`` slice, and the resume line.
    Bounded to a few thousand tokens — NEVER the raw transcripts, and never a
    physical merge of sessions (synthesis only, read-only).
    """
    name = args.name
    digest = digest_core.build_digest(
        name,
        archive_dir=getattr(args, "archive_dir", None),
        mapping_path=getattr(args, "mapping_path", None),
        _runtime_error_fn=getattr(args, "runtime_error_fn", None),
    )

    if getattr(args, "json", False):
        import json
        out = {
            "project": digest["project"],
            "archive_dir": digest["archive_dir"],
            "total_messages": digest["total_messages"],
            "runtime_error": digest["runtime_error"],
            "sessions": digest["sessions"],
        }
        print(json.dumps(out, indent=2))
        # An env fault is not an empty history — even in JSON, say which one.
        return 3 if digest["runtime_error"] else 0

    print(digest_core.format_digest(digest))
    return 3 if digest["runtime_error"] else 0


# --------------------------------------------------------------------------- #
# link / unlink / link --list — metadata on the canonical binding store
# --------------------------------------------------------------------------- #

def cmd_link(args: argparse.Namespace) -> int:
    """`hscc project link <project> <session-id>` — bind a session to a project.

    ``link --list [<project>]`` prints the current bindings instead of writing.
    A real link writes ONE file — the canonical ``~/.hermes/archive/telegram/
    proposals/mapping.json`` — inserting or replacing ``session-id``'s entry
    with ``project = <project>``, ``method = \"manual\"``, ``confidence = 1.0``
    (numeric), and an evidence string naming the operator and date. IDEMPOTENT:
    re-linking the same session to the same project is a no-op; linking to a
    different project overwrites (last-write-wins). It NEVER touches state.db,
    the registry, or a kanban board — fully reversible by editing the file.
    Linking is metadata only; it never physically merges sessions.
    """
    mapping_path = getattr(args, "mapping_path", None)

    if getattr(args, "list", False):
        project_filter = getattr(args, "project", None)
        store = bindings.list(mapping_path, project=project_filter)
        if not store:
            if project_filter:
                print(f"project link: no bindings for {project_filter!r}.")
            else:
                print("project link: no bindings.")
            return 0
        if not project_filter:
            print("project session bindings (project / session / method / confidence):")
            for sid in sorted(store):
                meta = store[sid]
                print(f"  {meta.get('project','?'):<20} {sid}  "
                      f"[{meta.get('method','?')}, {meta.get('confidence','?')}]")
        else:
            for sid in sorted(store):
                meta = store[sid]
                print(f"  {sid}  [{meta.get('method','?')}, {meta.get('confidence','?')}]")
        return 0

    project = getattr(args, "project", None)
    session_id = getattr(args, "session_id", None)
    if not project or not session_id:
        print(
            "project link: need <project> <session-id> (or --list). "
            "Try `hscc project link --help`.",
            file=sys.stderr,
        )
        return 2

    entry = bindings.bind(
        session_id,
        project,
        evidence=getattr(args, "evidence", None),
        mapping_path=mapping_path,
    )
    print(f"linked session {session_id} -> project {project!r} "
          f"[method {entry['method']}, confidence {entry['confidence']}]")
    print(f"  evidence: {entry['evidence']}")
    print(f"  store: {mapping_path or bindings.MAPPING_FILE}")
    return 0


def cmd_unlink(args: argparse.Namespace) -> int:
    """`hscc project unlink <project> <session-id>` — remove a binding.

    Removes ``session-id``'s entry from the canonical binding store entirely.
    If there was no such binding, says so and returns 0 (not an error). Writes
    ONLY ``mapping.json`` — never state.db, the registry, or a kanban board.
    Linking is metadata, never a physical merge of sessions.
    """
    session_id = getattr(args, "session_id", None)
    mapping_path = getattr(args, "mapping_path", None)
    if not session_id:
        print(
            "project unlink: need <project> <session-id>. "
            "Try `hscc project unlink --help`.",
            file=sys.stderr,
        )
        return 2
    removed = bindings.unbind(session_id, mapping_path=mapping_path)
    project = getattr(args, "project", None)
    if removed:
        print(f"unlinked session {session_id} "
              + (f"from project {project!r} " if project else "")
              + "from the binding store.")
    else:
        print(f"unlink: session {session_id} had no binding — nothing removed.")
    print(f"  store: {mapping_path or bindings.MAPPING_FILE}")
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
    sp.add_argument("--archive-dir", dest="archive_dir", default=None,
                    help=argparse.SUPPRESS)  # hidden seam for seed tests
    sp.add_argument("--mapping-path", dest="mapping_path", default=None,
                    help=argparse.SUPPRESS)  # hidden seam for seed tests
    sp.add_argument("extra", nargs=argparse.REMAINDER,
                    help="trailing args passed through to hermes (after --), e.g. '-- --resume <id>'")
    sp.set_defaults(func=cmd_chat)

    sp = subsub.add_parser("sessions", help="list every session belonging to a project (orchestrator + telegram), newest first",
                           epilog="example: flightdeck project sessions flightdeck   (resolves registry topic -> sessions.thread_id on the DEFAULT profile; read-only)")
    sp.add_argument("name", help="project name in the registry")
    sp.set_defaults(func=cmd_sessions)

    # digest — bounded, readable synthesis of the project's archive history.
    sp = subsub.add_parser("digest", help="bounded digest of a project's archive history (title/date-range/count/decision/resume per session)",
                           epilog="example: flightdeck project digest flightdeck   (reads ~/.hermes/archive/telegram/flightdeck/; bounded to a few thousand tokens, never raw transcripts)")
    sp.add_argument("name", help="project name in the registry")
    sp.add_argument("--json", action="store_true", help="emit machine-readable JSON (clean, not byte-exact to anything)")
    sp.add_argument("--archive-dir", dest="archive_dir", default=None,
                    help=argparse.SUPPRESS)  # hidden seam for tests
    sp.add_argument("--mapping-path", dest="mapping_path", default=None,
                    help=argparse.SUPPRESS)  # hidden seam for tests
    sp.set_defaults(func=cmd_digest)

    # link — bind a session to a project (metadata only; never merges).
    # `link --list [<project>]` prints; `link <project> <session-id>` writes.
    sp = subsub.add_parser("link", help="bind a session to a project (or --list bindings)",
                           epilog="example: flightdeck project link flightdeck <session-id>\n"
                                  "         flightdeck project link --list\n"
                                  "         flightdeck project link --list flightdeck  (just one project)")
    sp.add_argument("project", nargs="?", default=None,
                    help="project name (with --list: filter to this project)")
    sp.add_argument("session_id", nargs="?", default=None,
                    help="session id to bind to the project")
    sp.add_argument("--list", action="store_true",
                    help="print bindings instead of writing (optionally filtered to <project>)")
    sp.add_argument("--mapping-path", dest="mapping_path", default=None,
                    help=argparse.SUPPRESS)  # hidden seam for tests
    sp.add_argument("--evidence", dest="evidence", default=None,
                    help=argparse.SUPPRESS)  # hidden seam for deterministic tests
    sp.set_defaults(func=cmd_link)

    # unlink — remove a session's binding entry entirely.
    sp = subsub.add_parser("unlink", help="remove a session's binding from the binding store",
                           epilog="example: flightdeck project unlink flightdeck <session-id>")
    sp.add_argument("project", nargs="?", default=None,
                    help="project name the session was linked to (informational)")
    sp.add_argument("session_id", nargs="?", default=None,
                    help="session id to unbind (removed from mapping.json entirely)")
    sp.add_argument("--mapping-path", dest="mapping_path", default=None,
                    help=argparse.SUPPRESS)  # hidden seam for tests
    sp.set_defaults(func=cmd_unlink)


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
    args.mapping_path = getattr(args, "mapping_path", None)

    func = getattr(args, "func", None)
    if func is None:
        return _not_implemented()
    return func(args)
