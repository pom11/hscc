"""``hscc merge-queue`` — inspect and steer the local merge queue (MQ1).

The daemon does the work (one PR per repo per tick); this group is the
operator's read/steer surface:

    hscc merge-queue status [--json]
    hscc merge-queue list [<project>] [--json]
    hscc merge-queue add <project> <pr> [--card <id>] [--assignee <name>]
    hscc merge-queue remove <project> <pr> [--json]
    hscc merge-queue pause <project> [--json]
    hscc merge-queue resume <project> [--json]
    hscc merge-queue enable <project> [--json]
    hscc merge-queue disable <project> [--json]

Enabled is opt-in: the registry's ``merge_queue.enabled`` is the default and
``enable``/``disable`` write an explicit per-project override to the queue
ledger under ``~/.hscc``. Nothing here ever touches a repository: only the
daemon tick runs git/gh, in the queue's own worktree.
"""

from __future__ import annotations

import json
import sys

from . import merge_queue as mq
from . import cli_theme as theme

HELP_TEXT = """\
HSCC local merge queue — FIFO of green PRs, one at a time per repo.

The daemon fetches origin, rebases each PR onto origin/<base> in its own
worktree (~/.hscc/merge-queue/<project>/), runs the project's gates, pushes
--force-with-lease and runs `gh pr merge --squash --delete-branch`.
<base> is the repo's default branch (main or master) unless the registry
declares merge_queue.base_branch. A conflict or red gate comments on the PR
and hands the card back as a resolve step; a fork PR or a PR targeting
another base is refused once with the reason.

Usage: hscc merge-queue <subcommand> [args]

  hscc merge-queue status [--json]
                              Every known project: enabled, paused, queue
                              depth, in-flight PR and last outcome.
  hscc merge-queue list [<project>] [--json]
                              Queue entries (optionally just one project).
  hscc merge-queue add <project> <pr> [--card <id>] [--assignee <name>]
                              Enqueue a PR (idempotent by PR number). --card
                              gives the resolve handback its target.
  hscc merge-queue remove <project> <pr> [--json]
                              Drop a PR from the queue / in-flight claim.
  hscc merge-queue pause <project> [--json]
  hscc merge-queue resume <project> [--json]
                              Hold/resume processing for one project. Queued
                              entries survive a pause.
  hscc merge-queue enable <project> [--json]
  hscc merge-queue disable <project> [--json]
                              Explicit per-project override of the registry
                              opt-in; disable leaves the queue intact.
  hscc merge-queue --help     This help
"""

SUBCOMMAND_HELP = {
    "status": "hscc merge-queue status [--json]\n\n"
              "Every known project: enabled, paused, queue depth, in-flight\n"
              "PR and last outcome.",
    "list": "hscc merge-queue list [<project>] [--json]\n\n"
            "Queue entries, oldest first (FIFO).",
    "add": "hscc merge-queue add <project> <pr> [--card <id>] "
           "[--assignee <name>] [--json]\n\n"
           "Enqueue a PR. Idempotent by PR number.",
    "remove": "hscc merge-queue remove <project> <pr> [--json]\n\n"
              "Drop a PR from the queue (and any in-flight claim).",
    "pause": "hscc merge-queue pause <project> [--json]\n\n"
             "Stop processing this project; the queue is kept.",
    "resume": "hscc merge-queue resume <project> [--json]\n\n"
              "Resume processing this project's queue.",
    "enable": "hscc merge-queue enable <project> [--json]\n\n"
              "Opt this project in (overrides the registry default).",
    "disable": "hscc merge-queue disable <project> [--json]\n\n"
               "Opt this project out; queued entries are kept but not run.",
}


def _console(**kwargs):
    return theme.make_console(**kwargs)


def _error(message, json_mode):
    if json_mode:
        print(json.dumps({"error": message, "exit": 1}))
    else:
        print(f"hscc merge-queue: {message}", file=sys.stderr)
    return 1


def _strip_json(args):
    return [a for a in args if a != "--json"], "--json" in args


def _status_doc(registry_path=None, state_path=None):
    doc = mq.load_state(state_path)
    projects = [p["name"] for p in mq.load_projects(registry_path)]
    projects.extend(n for n in doc.get("projects", {}) if n not in projects)
    out = {"ok": True, "projects": {}}
    for name in dict.fromkeys(projects):
        ps = mq._project_state(doc, name)
        cfg = mq.project_config(name, registry_path)
        enabled = (bool(cfg.get("enabled", False))
                   if ps.get("enabled") is None and cfg else
                   bool(ps.get("enabled")) if ps.get("enabled") is not None
                   else False)
        out["projects"][name] = {
            "enabled": enabled,
            "paused": bool(ps.get("paused")),
            "queue_len": len(ps.get("queue") or []),
            "queue": list(ps.get("queue") or []),
            "waiting_len": len(ps.get("waiting") or []),
            "waiting": list(ps.get("waiting") or []),
            "in_flight": ps.get("in_flight"),
            "last": ps.get("last"),
            "repo": (cfg or {}).get("repo"),
        }
    return out


def cmd_merge_queue(argv):
    if not argv or argv[0] in ("--help", "-h", "help"):
        print(HELP_TEXT)
        return 0

    sub = argv[0]
    rest, json_mode = _strip_json(argv[1:])
    if sub not in SUBCOMMAND_HELP:
        print(f"Unknown merge-queue subcommand: {sub}", file=sys.stderr)
        print("Valid subcommands: " + ", ".join(SUBCOMMAND_HELP), file=sys.stderr)
        return 1
    if any(a in ("-h", "--help") for a in rest):
        print(SUBCOMMAND_HELP[sub])
        return 0

    if sub == "status":
        data = _status_doc()
        if json_mode:
            print(json.dumps(data))
            return 0
        console = _console()
        if not data["projects"]:
            console.print("merge queue: no projects configured")
            return 0
        for name, row in data["projects"].items():
            state = "paused" if row["paused"] else (
                "enabled" if row["enabled"] else "disabled")
            console.print(
                f"{name}: {state}, queued={row['queue_len']}, "
                f"waiting={row['waiting_len']}, "
                f"in-flight={row['in_flight']['pr'] if row['in_flight'] else '-'}, "
                f"last={(row['last'] or {}).get('result', '-')}")
        return 0

    if sub == "list":
        data = _status_doc()
        project = rest[0] if rest else None
        rows = data["projects"]
        if project is not None:
            if project not in rows:
                return _error(f"unknown project {project!r}", json_mode)
            rows = {project: rows[project]}
        if json_mode:
            print(json.dumps({"ok": True, "projects": rows}))
            return 0
        console = _console()
        empty = True
        for name, row in rows.items():
            for entry in row["queue"]:
                empty = False
                console.print(
                    f"{name}  PR #{entry.get('pr')}  "
                    f"card={entry.get('card') or '-'}  "
                    f"assignee={entry.get('assignee') or '-'}")
            for entry in row["waiting"]:
                empty = False
                console.print(
                    f"{name}  PR #{entry.get('pr')}  waiting  "
                    f"card={entry.get('card') or '-'}  "
                    f"assignee={entry.get('assignee') or '-'}")
        if empty:
            console.print("merge queue is empty")
        return 0

    if sub == "add":
        if len(rest) < 2:
            return _error("add needs <project> <pr>", json_mode)
        project, pr = rest[0], rest[1]
        try:
            pr = int(pr)
        except (TypeError, ValueError):
            return _error(f"pr must be an integer, got {pr!r}", json_mode)
        card = _flag(rest, "--card")
        assignee = _flag(rest, "--assignee")
        mq.add_to_queue(project, pr, card=card, assignee=assignee)
        if json_mode:
            print(json.dumps({"ok": True, "project": project, "pr": pr}))
        else:
            print(f"queued PR #{pr} for {project}")
        return 0

    if sub == "remove":
        if len(rest) < 2:
            return _error("remove needs <project> <pr>", json_mode)
        project, pr = rest[0], rest[1]
        try:
            pr = int(pr)
        except (TypeError, ValueError):
            return _error(f"pr must be an integer, got {pr!r}", json_mode)
        mq.remove_from_queue(project, pr)
        if json_mode:
            print(json.dumps({"ok": True, "project": project, "pr": pr}))
        else:
            print(f"removed PR #{pr} from {project}")
        return 0

    # pause / resume / enable / disable
    if not rest:
        return _error(f"{sub} needs <project>", json_mode)
    project = rest[0]
    if sub in ("pause", "resume"):
        mq.set_paused(project, sub == "pause")
        value = sub == "pause"
        key = "paused"
    else:
        mq.set_enabled(project, sub == "enable")
        value = sub == "enable"
        key = "enabled"
    if json_mode:
        print(json.dumps({"ok": True, "project": project, key: value}))
    else:
        print(f"{project}: {key}={value}")
    return 0


def _flag(args, name):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


if __name__ == "__main__":
    sys.exit(cmd_merge_queue(sys.argv[1:]))
