"""topics.py — Telegram topic CRUD and the overwrite audit.

Presents `flightdeck topics ...` against :mod:`flightdeck.core.telegram`
(the transport) and :mod:`flightdeck.core.registry` (the topic<->project
mapping). Presentation only — all logic lives in the core modules, which are
independently testable against a stubbed client.

Read-only commands (``list``, ``audit``) act immediately. Every mutating
command (``rename``/``create``/``bind``/``unbind``) prints exactly what it will
do and requires ``--apply`` before touching Telegram or the registry.
"""

from __future__ import annotations

import argparse
import sys

from ..core import registry, telegram
from ..core.telegram import TelegramError, TopicLockedError
from .sync import DEFAULT_IGNORED_TOPICS


def _registry_topic_map(projects: list[registry.Project]) -> dict[int, str]:
    """topic id -> project name, for topics bound in the registry."""
    out: dict[int, str] = {}
    for proj in projects:
        if proj.topic is not None:
            out[proj.topic] = proj.name
    return out


def _not_implemented() -> int:
    print("topics: command not recognised. Try `flightdeck topics --help`.", file=sys.stderr)
    return 2


# --------------------------------------------------------------------------- #
# list — read-only
# --------------------------------------------------------------------------- #

def cmd_list(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    topic_map = _registry_topic_map(projects)
    try:
        top = telegram.list_topics(_client=args.client)
    except TopicLockedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except TelegramError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not top:
        print("No topics found.")
        return 0
    if args.json:
        import json

        print(
            json.dumps(
                [{"id": t.id, "name": t.name, "project": topic_map.get(t.id)} for t in top]
            )
        )
        return 0
    for t in sorted(top, key=lambda x: x.id):
        project = topic_map.get(t.id) or "unknown"
        print(f"{t.id:<8} {t.name:<40} {project}")
    return 0


# --------------------------------------------------------------------------- #
# audit — read-only, the overwrite detector
# --------------------------------------------------------------------------- #

def cmd_audit(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    """Flag topics whose CURRENT name differs from their registry name, and
    topics with no project mapping. Read-only; never mutates anything."""
    topic_map = _registry_topic_map(projects)
    # Known-permanent topics (the operator's persisted ignored_topics list,
    # always plus Telegram's built-in General topic) are excluded from the
    # [UNMAPPED] section entirely — matching how sync reports orphans. The
    # MISMATCH path below is unaffected: an ignored topic that maps to a real
    # project elsewhere still surfaces a genuine name mismatch.
    ignored = DEFAULT_IGNORED_TOPICS | set(registry.load_ignored_topics(args.registry))
    try:
        top = telegram.list_topics(_client=args.client)
    except TopicLockedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except TelegramError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    issues = 0
    mismatched: list[tuple[telegram.Topic, str, str]] = []
    unmapped: list[telegram.Topic] = []

    for t in sorted(top, key=lambda x: x.id):
        project = topic_map.get(t.id)
        if project is None:
            if t.id in ignored:
                continue
            unmapped.append(t)
            continue
        proj = registry.get_project(project, path=args.registry)
        # The registry's expected topic name; falls back to the project key.
        expected = proj.topic_name or proj.name
        if expected and t.name != expected:
            mismatched.append((t, expected, project))

    if args.json:
        import json

        print(
            json.dumps(
                {
                    "mismatched": [
                        {"id": t.id, "current": t.name, "expected": reg, "project": proj}
                        for (t, reg, proj) in mismatched
                    ],
                    "unmapped": [{"id": t.id, "name": t.name} for t in unmapped],
                }
            )
        )
        return 0

    for (t, reg, proj) in mismatched:
        issues += 1
        print(f"[MISMATCH] topic {t.id}: current name {t.name!r} != registry {reg!r} ({proj})")
    for t in unmapped:
        issues += 1
        print(f"[UNMAPPED] topic {t.id}: {t.name!r} (no project mapping)")

    if issues == 0:
        print("audit clean: every topic matches its registry name and is mapped.")
        return 0
    print(f"\n{issues} issue(s) found.")
    return 1


# --------------------------------------------------------------------------- #
# rename — mutating
# --------------------------------------------------------------------------- #

def cmd_rename(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    topic_id = args.id
    name = args.name
    topic_map = _registry_topic_map(projects)
    project = topic_map.get(topic_id)

    # The current name is display-only. A locked session at this stage must not
    # abort the whole command — the real lock error surfaces from the mutation
    # below; here we just degrade the preview to "(unknown)".
    current = None
    try:
        for t in telegram.list_topics(_client=args.client):
            if t.id == topic_id:
                current = t
                break
    except (TopicLockedError, TelegramError):
        current = None
    cur_name = current.name if current else "(unknown)"
    mapping = project or "unknown"

    print(f"will rename topic {topic_id} from {cur_name!r} to {name!r} (mapped to {mapping})")
    if not args.apply:
        print("dry-run: pass --apply to perform this rename.")
        return 0

    try:
        updated = telegram.rename_topic(topic_id, name, _client=args.client)
    except TopicLockedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except TelegramError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"renamed topic {updated.id} to {updated.name!r}")
    return 0


# --------------------------------------------------------------------------- #
# create — mutating
# --------------------------------------------------------------------------- #

def cmd_create(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    name = args.name
    print(f"will create topic {name!r}")
    if args.bind:
        print(f"  will bind topic to project {args.bind!r}")
    if not args.apply:
        print("dry-run: pass --apply to perform this create.")
        return 0

    try:
        created = telegram.create_topic(name, _client=args.client)
    except TopicLockedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except TelegramError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.bind:
        try:
            registry.bind_topic(args.bind, created.id, path=args.registry)
        except registry.ProjectNotFoundError:
            print(
                f"created topic {created.id} ({name!r}) but could not bind: "
                f"no project named {args.bind!r} in the registry",
                file=sys.stderr,
            )
            return 2
    print(f"created topic {created.id} {name!r}")
    return 0


# --------------------------------------------------------------------------- #
# bind / unbind — mutating (registry only)
# --------------------------------------------------------------------------- #

def cmd_bind(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    topic_id = args.id
    project = args.project
    topic_map = _registry_topic_map(projects)
    current_owner = topic_map.get(topic_id)

    if current_owner is not None and current_owner != project:
        print(
            f"will rebind topic {topic_id} from project {current_owner!r} to {project!r}"
        )
    else:
        print(f"will bind topic {topic_id} to project {project!r}")
    if not args.apply:
        print("dry-run: pass --apply to perform this bind.")
        return 0

    try:
        updated = registry.bind_topic(project, topic_id, path=args.registry)
    except registry.ProjectNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"bound topic {topic_id} to project {updated.name!r}")
    return 0


def cmd_unbind(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    topic_id = args.id
    topic_map = _registry_topic_map(projects)
    project = topic_map.get(topic_id)
    if project is None:
        print(f"topic {topic_id} has no project mapping; nothing to unbind.")
        return 0
    print(f"will unbind topic {topic_id} from project {project!r}")
    if not args.apply:
        print("dry-run: pass --apply to perform this unbind.")
        return 0
    registry.unbind_topic(project, path=args.registry)
    print(f"unbound topic {topic_id} from project {project!r}")
    return 0


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #

def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("topics", help="Telegram topic CRUD + overwrite audit",
                       epilog="example: flightdeck topics list")
    subsub = p.add_subparsers(dest="topic_cmd", metavar="TOPIC_CMD")

    sp = subsub.add_parser("list", help="every topic: id, name, mapped project",
                           epilog="example: flightdeck topics list")
    sp.set_defaults(func=cmd_list)

    sp = subsub.add_parser("audit", help="flag mismatched + unmapped topics (read-only)",
                           epilog="example: flightdeck topics audit")
    sp.set_defaults(func=cmd_audit)

    sp = subsub.add_parser("rename", help="restore a topic name",
                           epilog='example: flightdeck topics rename 3 "updates" --apply')
    sp.add_argument("id", type=int, help="topic id to rename")
    sp.add_argument("name", help="new topic name")
    _add_apply(sp)
    sp.set_defaults(func=cmd_rename)

    sp = subsub.add_parser("create", help="create a forum topic",
                           epilog="example: flightdeck topics create updates --bind flightdeck --apply")
    sp.add_argument("name", help="topic name")
    sp.add_argument("--bind", metavar="PROJECT", help="bind the new topic to a project")
    _add_apply(sp)
    sp.set_defaults(func=cmd_create)

    sp = subsub.add_parser("bind", help="map a topic id to a project",
                           epilog="example: flightdeck topics bind 3 flightdeck --apply")
    sp.add_argument("id", type=int, help="topic id")
    sp.add_argument("project", help="project name in the registry")
    _add_apply(sp)
    sp.set_defaults(func=cmd_bind)

    sp = subsub.add_parser("unbind", help="clear a topic's project mapping",
                           epilog="example: flightdeck topics unbind 3 --apply")
    sp.add_argument("id", type=int, help="topic id")
    _add_apply(sp)
    sp.set_defaults(func=cmd_unbind)


def _add_apply(sp: argparse.ArgumentParser) -> None:
    sp.add_argument(
        "--apply",
        action="store_true",
        help="perform the change (mutating commands are dry-run by default)",
    )


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: run a topics subcommand with a shared client.

    Attaches the injectable client to args so core calls are stubbable in tests.
    """
    args.registry = registry_path
    args.client = getattr(args, "client", None)
    projects = registry.load_registry(registry_path)

    func = getattr(args, "func", None)
    if func is None:
        return _not_implemented()
    return func(args, projects)
