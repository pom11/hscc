#!/usr/bin/env python3
"""HSCC orchestrator reconcile tick (cron --script detector).

Emits a human-readable block of HSCC subtasks that JUST reached a terminal
kanban state (done / review / blocked / archived) since the previous tick,
plus an AUTONOMY flag line. Stdout is injected into the cron agent's prompt;
empty stdout -> agent stays silent.

Source of truth: ~/.hscc/bridge.json (the orchestrator's own dispatch ledger)
cross-referenced to live kanban status. Dedup via ~/.hscc/.orch_tick_ack.json
(kanban_id -> last-reported status). First run seeds the baseline and emits
nothing, so we never blast the whole backlog on install.
"""
import json
import os
import shutil
import subprocess
import sys

HSCC_HOME = os.environ.get("HSCC_HOME", os.path.expanduser("~/.hscc"))
BRIDGE = os.path.join(HSCC_HOME, "bridge.json")
ACK = os.path.join(HSCC_HOME, ".orch_tick_ack.json")
AUTONOMY = os.path.join(HSCC_HOME, "autonomy")

# Terminal kanban states worth surfacing to the orchestrator.
TERMINAL = {"done", "review", "blocked", "archived"}
# Bridge entries in these HSCC states are never queried (no live card).
SKIP_BRIDGE = {"cancelled"}


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _hermes_bin():
    found = shutil.which("hermes")
    if found:
        return found
    cand = os.path.expanduser("~/.hermes/hermes-agent/venv/bin/hermes")
    return cand if os.path.exists(cand) else "hermes"


def _autonomy_on():
    try:
        with open(AUTONOMY) as f:
            return f.read().strip().lower() in ("on", "1", "true", "yes")
    except (FileNotFoundError, OSError):
        return False


def _card(hbin, board, kid):
    """Return (status, title, summary, assignee) or (None, ...) on failure."""
    try:
        r = subprocess.run(
            [hbin, "kanban", "--board", board, "show", kid, "--json"],
            capture_output=True, text=True, timeout=25,
        )
        if r.returncode != 0:
            return None, "", "", ""
        data = json.loads(r.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None, "", "", ""
    task = data.get("task") or {}
    summary = (data.get("latest_summary") or task.get("result") or "").strip()
    return (task.get("status"), task.get("title") or kid,
            summary, task.get("assignee") or "")


def main():
    bridge = _load_json(BRIDGE, {}).get("tasks", {})
    ack = _load_json(ACK, None)
    first_run = ack is None
    if ack is None:
        ack = {}

    hbin = _hermes_bin()
    new_items = []

    for hscc_id, e in bridge.items():
        board = e.get("board")
        kid = e.get("kanban_id")
        if not board or not kid:
            continue
        if e.get("status") in SKIP_BRIDGE:
            continue
        # Already reported as a final state -> never re-query.
        if ack.get(kid) in ("done", "archived"):
            continue

        status, title, summary, assignee = _card(hbin, board, kid)
        if status is None:
            continue  # transient lookup failure; retry next tick
        if status not in TERMINAL:
            continue
        if ack.get(kid) == status:
            continue  # already reported at this status

        ack[kid] = status
        if not first_run:
            new_items.append({
                "hscc_task_id": hscc_id,
                "project_id": e.get("project_id", ""),
                "kanban_id": kid,
                "board": board,
                "status": status,
                "title": title,
                "assignee": assignee,
                "summary": summary,
                "worktree": e.get("worktree", ""),
                "worker_host": e.get("worker_host", ""),
            })

    # Persist the cursor regardless of mode so we don't replay.
    try:
        os.makedirs(HSCC_HOME, exist_ok=True)
        tmp = ACK + ".tmp"
        with open(tmp, "w") as f:
            json.dump(ack, f, indent=2)
        os.replace(tmp, ACK)
    except OSError as exc:
        print(f"[orch-tick] WARN: could not persist ack: {exc}", file=sys.stderr)

    if first_run or not new_items:
        return  # empty stdout -> agent stays silent

    print(f"AUTONOMY={'on' if _autonomy_on() else 'off'}")
    print(f"NEW_TERMINAL_TASKS={len(new_items)}")
    print()
    for it in new_items:
        print(f"- kanban {it['kanban_id']} [{it['status']}] "
              f"@{it['assignee']} — {it['title']}")
        print(f"    hscc_task_id: {it['hscc_task_id']}")
        print(f"    project_id:   {it['project_id']}")
        print(f"    board:        {it['board']}")
        if it["worker_host"]:
            print(f"    worker_host:  {it['worker_host']}")
        if it["worktree"]:
            print(f"    worktree:     {it['worktree']}")
        if it["summary"]:
            print(f"    summary:      {it['summary'][:500]}")
        print()


if __name__ == "__main__":
    main()
