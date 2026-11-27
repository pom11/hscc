"""``hscc merge-queue`` — inspect and steer the local merge queue (MQ1).

The daemon does the work (one PR per repo per tick); this group is the
operator's read/steer surface:

    hscc merge-queue status [--json]
    hscc merge-queue list [<project>] [--json]
    hscc merge-queue add <project> <pr> [--card <id>] [--assignee <name>]
    hscc merge-queue remove <project> <pr> [--json]
    hscc merge-queue hold <project> <pr> [--reason <text>]
    hscc merge-queue unhold <project> <pr> [--json]
    hscc merge-queue pause <project> [--json]
    hscc merge-queue resume <project> [--json]
    hscc merge-queue enable <project> [--json]
    hscc merge-queue disable <project> [--json]

Enabled follows a default-on rule: a project with a git checkout at its
registry repo, a github.com origin remote, and gates (the registry's
``merge_queue.gates`` or hscc's built-in gates) is enabled without any flag.
A project missing one stays off and ``status`` prints why
(``no repo`` / ``no GitHub remote`` / ``no gates``). ``enable``/``disable``
write an explicit per-project override to the queue ledger under ``~/.hscc``
that always wins over the default (a ``disable`` persists across restarts).
Nothing here ever touches a repository: only the daemon tick runs git/gh, in
the queue's own worktree.
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

The queue is ON BY DEFAULT for a project that has a git checkout at its
registry repo, a github.com origin remote, and gates (merge_queue.gates, or
hscc's built-in gates). A project missing one stays off and `status` prints
why: "no repo", "no GitHub remote" or "no gates". `enable`/`disable` write an
explicit per-project override that always wins over the default; a `disable`
persists across daemon restarts.

Usage: hscc merge-queue <subcommand> [args]

  hscc merge-queue status [--json]
                              Every known project: enabled (and why not),
                              paused, queue depth, in-flight PR and last
                              outcome.
  hscc merge-queue list [<project>] [--json]
                              Queue entries (optionally just one project).
  hscc merge-queue add <project> <pr> [--card <id>] [--assignee <name>]
                              Enqueue a PR (idempotent by PR number). --card
                              gives the resolve handback its target.
  hscc merge-queue remove <project> <pr> [--json]
                              Drop a PR from the queue / in-flight claim. That
                              is a PULL, not a hold: the next empty tick
                              auto-enqueues the PR again.
  hscc merge-queue hold <project> <pr> [--reason <text>]
                              Park a PR until an explicit `unhold`. A push, a
                              green check or elapsed time never lift a hold —
                              auto-enqueue, the wait-list drain and the claim
                              all skip a held PR. This is the per-PR lever;
                              `pause` below is project-wide.
  hscc merge-queue unhold <project> <pr> [--json]
                              Release a hold. The PR re-enters the queue the
                              ordinary way (auto-enqueue, or an explicit `add`).
  hscc merge-queue pause <project> [--json]
  hscc merge-queue resume <project> [--json]
                              Hold/resume processing for one project. Queued
                              entries survive a pause.
  hscc merge-queue enable <project> [--json]
  hscc merge-queue disable <project> [--json]
                              Explicit per-project override of the default-on
                              rule and the registry setting; disable leaves
                              the queue intact and persists.
  hscc merge-queue --help     This help
"""

SUBCOMMAND_HELP = {
    "status": "hscc merge-queue status [--json]\n\n"
              "Every known project: enabled (or why it is off: no repo /\n"
              "no GitHub remote / no gates), paused, queue depth, in-flight\n"
              "PR and last outcome.",
    "list": "hscc merge-queue list [<project>] [--json]\n\n"
            "Queue entries, oldest first (FIFO).",
    "add": "hscc merge-queue add <project> <pr> [--card <id>] "
           "[--assignee <name>] [--json]\n\n"
           "Enqueue a PR. Idempotent by PR number.",
    "remove": "hscc merge-queue remove <project> <pr> [--json]\n\n"
              "Drop a PR from the queue (and any in-flight claim). A pull, not\n"
              "a hold: auto-enqueue may put the PR straight back. Use `hold`.",
    "hold": "hscc merge-queue hold <project> <pr> [--reason <text>]\n\n"
            "Park a PR until an explicit `unhold`: a push, a green check or\n"
            "elapsed time never lift a hold. Pulls it from the queue, the wait\n"
            "list and any live claim, and shows in status/list/monitors.",
    "unhold": "hscc merge-queue unhold <project> <pr> [--json]\n\n"
              "Release a hold — the only way one ends. The PR re-enters the\n"
              "queue by the next tick's auto-enqueue or an explicit `add`.",
    "pause": "hscc merge-queue pause <project> [--json]\n\n"
             "Stop processing this project; the queue is kept.",
    "resume": "hscc merge-queue resume <project> [--json]\n\n"
              "Resume processing this project's queue.",
    "enable": "hscc merge-queue enable <project> [--json]\n\n"
              "Force this project on (overrides the default-on rule and the\n"
              "registry setting).",
    "disable": "hscc merge-queue disable <project> [--json]\n\n"
               "Force this project off; the override persists and queued\n"
               "entries are kept but not run.",
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
        held = [dict(h) for h in (ps.get("held") or [])
                if isinstance(h, dict)]
        out["projects"][name] = {
            "enabled": mq.effective_enabled(cfg, ps),
            "enabled_source": mq.enabled_source(cfg, ps),
            "default_enabled": bool((cfg or {}).get("default_enabled")),
            "default_reason": (cfg or {}).get("default_reason"),
            "paused": bool(ps.get("paused")),
            "queue_len": len(ps.get("queue") or []),
            "queue": list(ps.get("queue") or []),
            "waiting_len": len(ps.get("waiting") or []),
            # Entries pass through whole, so the `waiting_reason` the engine
            # stamps lands in --json without this projection inventing any.
            "waiting": list(ps.get("waiting") or []),
            "held_len": len(held),
            "held": held,
            "in_flight": ps.get("in_flight"),
            "last": ps.get("last"),
            "repo": (cfg or {}).get("repo"),
        }
    # One engine, one clock — and it belongs on the surface, not in a
    # hand-stat of ~/.hscc/state (t_cab1f418: "in_flight: None, no
    # timestamps" read as a 4-hour livelock on a healthy busy engine).
    out["engine"] = mq.engine_liveness(doc, health=mq.read_health_stamp())
    return out


def _engine_line(engine):
    """The one line the liveness block deserves."""
    secs = engine.get("seconds_since_last_write")
    if secs is None:
        return "engine: no state write on record (never ticked?)"
    line = f"engine: {secs}s since last state write"
    if engine.get("ledger_age_s") is not None \
            and engine.get("health_age_s") is not None:
        line += (f" (ledger {engine['ledger_age_s']}s ago, health stamp "
                 f"{engine['health_age_s']}s ago)")
    flight = engine.get("in_flight") or {}
    if flight.get("pr") is not None:
        line += f" — gating PR #{flight['pr']}"
    return line


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
            if row["paused"]:
                state = "paused"
            elif row["enabled"]:
                state = ("enabled (default)"
                         if row["enabled_source"] == "default" else "enabled")
            elif row["enabled_source"] == "default" and row["default_reason"]:
                # Why the default-on rule did not apply: no repo /
                # no GitHub remote / no gates.
                state = f"disabled ({row['default_reason']})"
            elif row["enabled_source"] == "registry":
                state = "disabled (registry)"
            elif row["enabled_source"] == "cli":
                state = "disabled (override)"
            else:
                state = "disabled"
            console.print(
                f"{name}: {state}, queued={row['queue_len']}, "
                f"waiting={row['waiting_len']}, "
                f"held={row['held_len']}, "
                f"in-flight={row['in_flight']['pr'] if row['in_flight'] else '-'}, "
                f"last={(row['last'] or {}).get('result', '-')}")
            for hold in row["held"]:
                # The reason on the line is the whole point: a parked PR whose
                # reason lives only in a PR comment is folklore (t_662753d6).
                console.print(f"  held #{hold.get('pr')}  "
                              f"{hold.get('reason') or '(no reason given)'}")
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
            # The engine clock rides the JSON too — whoever consumes this
            # must be able to tell a healthy idle from a stalled engine
            # without stat-ing state files (t_cab1f418).
            print(json.dumps({"ok": True, "engine": data["engine"],
                              "projects": rows}))
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
                # The WHY rides the entry — a wait list of bare numbers is
                # how a queue refusing dirty heads read as a dead queue
                # (t_cab1f418).
                reason = entry.get("waiting_reason")
                why = (f"  why={reason}"
                       f" ({entry['waiting_detail']})"
                       if reason and entry.get("waiting_detail")
                       else f"  why={reason}" if reason else "")
                console.print(
                    f"{name}  PR #{entry.get('pr')}  waiting  "
                    f"card={entry.get('card') or '-'}  "
                    f"assignee={entry.get('assignee') or '-'}" + why)
            flight = row.get("in_flight")
            if isinstance(flight, dict) and flight:
                # A held claim is queue content, not noise: `list` used to
                # print "merge queue is empty" while an engine gated a PR.
                empty = False
                console.print(
                    f"{name}  PR #{flight.get('pr')}  in-flight "
                    f"(stage={flight.get('stage') or 'running'})")
            for entry in row["held"]:
                empty = False
                console.print(
                    f"{name}  PR #{entry.get('pr')}  held  "
                    f"{entry.get('reason') or '(no reason given)'}")
        if empty:
            console.print("merge queue is empty")
        console.print(_engine_line(data["engine"]))
        return 0

    if sub == "add":
        if len(rest) < 2:
            return _error("add needs <project> <pr>", json_mode)
        project, pr = rest[0], rest[1]
        try:
            pr = int(pr)
        except (TypeError, ValueError):
            return _error(f"pr must be an integer, got {pr!r}", json_mode)
        if mq.is_held(project, pr):
            # `add` is not a way to defeat a hold; `unhold` is (t_662753d6).
            return _error(
                f"PR #{pr} is held for {project}; run "
                f"`hscc merge-queue unhold {project} {pr}` first", json_mode)
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

    if sub == "hold":
        if len(rest) < 2:
            return _error("hold needs <project> <pr>", json_mode)
        project, pr = rest[0], rest[1]
        try:
            pr = int(pr)
        except (TypeError, ValueError):
            return _error(f"pr must be an integer, got {pr!r}", json_mode)
        reason = _flag(rest, "--reason")
        mq.hold_pr(project, pr, reason=reason)
        if json_mode:
            print(json.dumps({"ok": True, "project": project, "pr": pr,
                              "held": True, "reason": reason}))
        else:
            print(f"held PR #{pr} for {project}"
                  + (f": {reason}" if reason else "")
                  + " (only `merge-queue unhold` releases it)")
        return 0

    if sub == "unhold":
        if len(rest) < 2:
            return _error("unhold needs <project> <pr>", json_mode)
        project, pr = rest[0], rest[1]
        try:
            pr = int(pr)
        except (TypeError, ValueError):
            return _error(f"pr must be an integer, got {pr!r}", json_mode)
        mq.unhold_pr(project, pr)
        if json_mode:
            print(json.dumps({"ok": True, "project": project, "pr": pr,
                              "held": False}))
        else:
            print(f"released PR #{pr} for {project}; it re-enters the queue on "
                  "the next tick")
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
