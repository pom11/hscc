#!/usr/bin/env python3
"""HSCC orchestrator reconcile tick (cron --script detector).

Emits a human-readable block of HSCC kanban cards that JUST reached a terminal
state (done / review / blocked) since the previous tick, plus an AUTONOMY flag
line. Stdout is injected into the cron agent's prompt; empty stdout -> agent
stays silent.

Source of truth: the live HSCC kanban boards (every `hscc-*` board), so cards
created ad-hoc (outside `dispatch-task`) are caught too. The orchestrator's own
dispatch ledger ~/.hscc/bridge.json is used only to ENRICH a card with worker
host / worktree / hscc task id when it happens to have come through dispatch.
Dedup via ~/.hscc/.orch_tick_ack.json (kanban_id -> last-reported status). First
run seeds the baseline and emits nothing, so we never blast the whole backlog.
"""
import json
import os
import re
import shutil
import subprocess
import sys

HSCC_HOME = os.environ.get("HSCC_HOME", os.path.expanduser("~/.hscc"))
BRIDGE = os.path.join(HSCC_HOME, "bridge.json")
PROJECTS = os.path.join(HSCC_HOME, "projects.json")
ACK = os.path.join(HSCC_HOME, ".orch_tick_ack.json")
AUTONOMY = os.path.join(HSCC_HOME, "autonomy")

# Terminal kanban states worth surfacing to the orchestrator.
TERMINAL = {"done", "review", "blocked"}
# Once a card is reported at one of these, never re-query it.
FINAL = {"done", "archived"}
# HSCC boards always carry this slug prefix (hscc-<hex>).
BOARD_RE = re.compile(r"\bhscc-[0-9a-f]{6,}\b")


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


def _run(hbin, args, timeout=25):
    try:
        r = subprocess.run([hbin] + args, capture_output=True, text=True,
                           timeout=timeout)
        if r.returncode != 0:
            return None
        return r.stdout
    except (subprocess.SubprocessError, OSError):
        return None


def _project_board_slugs():
    """Board slugs declared in ~/.hscc/projects.json (boardSlug per project)."""
    data = _load_json(PROJECTS, {})
    slugs = set()
    for p in (data.get("projects") or []):
        slug = p.get("boardSlug")
        if slug:
            slugs.add(slug)
    return slugs


def _discover_boards(hbin, bridge):
    """Union of: live `hscc-*` boards, bridge boards, projects.json boards."""
    boards = set()
    out = _run(hbin, ["kanban", "boards"])
    if out:
        boards.update(BOARD_RE.findall(out))
    boards.update(_project_board_slugs())
    for e in bridge.values():
        b = e.get("board")
        if b:
            boards.add(b)
    return sorted(boards)


def _list_cards(hbin, board):
    out = _run(hbin, ["kanban", "--board", board, "list", "--json"])
    if out is None:
        return None  # transient failure -> caller skips this board this tick
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else (data.get("tasks") or [])


def _card_summary(hbin, board, kid):
    """Fetch (status, title, summary, assignee) for one card via `show`."""
    out = _run(hbin, ["kanban", "--board", board, "show", kid, "--json"])
    if out is None:
        return None, "", "", ""
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None, "", "", ""
    task = data.get("task") or {}
    summary = (data.get("latest_summary") or task.get("result") or "").strip()
    return (task.get("status"), task.get("title") or kid,
            summary, task.get("assignee") or "")


def _bridge_index(bridge):
    """Map kanban_id -> bridge entry for enrichment."""
    idx = {}
    for hscc_id, e in bridge.items():
        kid = e.get("kanban_id")
        if kid:
            idx[kid] = dict(e, hscc_task_id=hscc_id)
    return idx


def main():
    bridge = _load_json(BRIDGE, {}).get("tasks", {})
    bidx = _bridge_index(bridge)
    ack = _load_json(ACK, None)
    first_run = ack is None
    if ack is None:
        ack = {}

    hbin = _hermes_bin()
    new_items = []

    for board in _discover_boards(hbin, bridge):
        cards = _list_cards(hbin, board)
        if cards is None:
            continue  # transient board read failure; retry next tick
        for card in cards:
            kid = card.get("id")
            status = card.get("status")
            if not kid or status not in TERMINAL:
                continue
            if ack.get(kid) in FINAL:
                continue  # already reported at a final state
            if ack.get(kid) == status:
                continue  # already reported at this status

            # Enrich with summary via `show` only on a genuine transition.
            st2, title, summary, assignee = _card_summary(hbin, board, kid)
            status = st2 or status
            if status not in TERMINAL:
                continue
            ack[kid] = status
            if first_run:
                continue
            e = bidx.get(kid, {})
            new_items.append({
                "hscc_task_id": e.get("hscc_task_id", ""),
                "project_id": e.get("project_id", ""),
                "kanban_id": kid,
                "board": board,
                "status": status,
                "title": title or card.get("title") or kid,
                "assignee": assignee or card.get("assignee", ""),
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
        if it["hscc_task_id"]:
            print(f"    hscc_task_id: {it['hscc_task_id']}")
        if it["project_id"]:
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
