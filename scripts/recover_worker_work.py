#!/usr/bin/env python3
"""Salvage a blocked/crashed kanban worker's uncommitted work before it is lost.

Background
----------
Kanban workers have lost completed work because they could not commit it
(hscc task t_74e1ff6f, incidents t_e8a61c8e / t_61f27161 / t_9e66d919). The
dispatcher already *preserves* dirty worktrees (it will not `git worktree
remove` a tree with uncommitted edits or unpushed commits), so on-disk edits
survive until a human or a later run fetches them. But that only helps if
someone knows to look. This helper makes the recovery explicit and durable:
it copies a target's uncommitted delta into a recovery directory outside the
worktree, so nothing is ever silently reclaimed away.

It is strictly READ-ONLY on the source worktree: it never deletes, never
force-removes, never resets. It only reads `git status` / diffs / logs and
copies files into --out.

Usage
-----
    # Recover work for specific blocked/crashed cards (default board hscc)
    python3 scripts/recover_worker_work.py --task t_ab12cd34 t_ef56abcd

    # Scan every blocked card on another board and recover whatever it has
    python3 scripts/recover_worker_work.py --board projects --blocked

    # Target a JSON file of recovered paths (what an operator kept after a prior run)
    python3 scripts/recover_worker_work.py --task-list /tmp/recovered.json

Flags
-----
    --board <slug>   board slug (default "hscc"); board DB resolved from HERMES_HOME.
    --db <path>      explicit board DB path (overrides --board + HERMES_HOME).
    --task <id> ...  specific task ids to recover (positional-agnostic; repeatable).
    --task-list <f>  JSON file: {"tasks": [...]}.
    --blocked        scan all tasks in status `blocked`.
    --crashed        scan all tasks in status `crashed`.
    --all            scan every task.
    --out <dir>      recovery root (default <HERMES_HOME>/kanban/recovered-worker-work/<board>/).
    --json           print a machine-readable summary.
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


def resolve_board_db(board: str) -> str:
    # The board DBs live under the *real* hermes home (~/.hermes), never the
    # per-profile HERMES_HOME — even in a dispatched worker whose HERMES_HOME
    # points at the profile dir. The dispatcher also exports HERMES_KANBAN_DB
    # to its workers, which wins when present.
    env_db = os.environ.get("HERMES_KANBAN_DB")
    if env_db and os.path.exists(env_db):
        return env_db
    home = str(Path.home() / ".hermes")
    return str(Path(home) / "kanban" / "boards" / board / "kanban.db")


def git(*args: str, worktree: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", worktree, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )


def is_worktree(path: str) -> bool:
    """True when `path` is a real, existing git worktree (linked or main)."""
    p = Path(path).expanduser()
    if not p.is_dir():
        return False
    r = git("rev-parse", "--is-inside-work-tree", worktree=str(p))
    return r.returncode == 0 and r.stdout.strip() == "true"


def snapshot_task(conn: sqlite3.Connection, task_id: str, out_root: Path) -> dict:
    cur = conn.cursor()
    cur.execute(
        "SELECT status, workspace_path, branch_name, title FROM tasks WHERE id=?",
        (task_id,),
    )
    row = cur.fetchone()
    if not row:
        return {"task": task_id, "error": "not found"}
    status, workspace_path, branch_name, title = row

    result = {
        "task": task_id,
        "status": status,
        "branch": branch_name or None,
        "title": (title or "")[:80],
        "workspace": workspace_path or None,
        "preserved": None,
    }

    if not workspace_path or not is_worktree(workspace_path):
        result["note"] = "no existing worktree to recover"
        return result

    # Detect uncommitted track-modified files, untracked files, unpushed commits.
    status_p = git("status", "--porcelain", worktree=workspace_path)
    dirty_lines = [l for l in status_p.stdout.splitlines() if l.strip()]
    unpushed_p = git(
        "log", "--oneline", "@{u}..HEAD", worktree=workspace_path,
    )
    unpushed = [l for l in unpushed_p.stdout.splitlines() if l.strip()] if unpushed_p.returncode == 0 else None

    if not dirty_lines and not unpushed:
        result["note"] = "clean worktree — nothing to recover"
        return result

    # Build the recovery directory.
    tag = task_id.replace("/", "_")
    dest = out_root / tag
    dest.mkdir(parents=True, exist_ok=True)
    patches = dest / "tracked-changes.diff"
    logpath = dest / "recovery-notes.txt"

    # Snapshot tracked modifications / deletions / staged changes as a unified diff.
    diff_p = git("diff", "HEAD", worktree=workspace_path)
    patches.write_text(diff_p.stdout, encoding="utf-8", errors="replace")

    # Copy untracked files in place (preserving relative paths) so binary or
    # large artifacts a diff can't carry are not lost.
    untracked_srcs = [
        l[3:].strip().strip('"') for l in dirty_lines
        if l.startswith("??") and len(l) > 3
    ]
    copied = []
    for rel in untracked_srcs:
        src = Path(workspace_path) / rel
        if not src.is_file() and not src.is_dir():
            continue
        if src.is_dir():
            # git status shows one "dir/" line; walk it for real files.
            for f in sorted(src.rglob("*")):
                if f.is_file():
                    _copy_one(f, workspace_path, dest, copied)
        elif src.is_file():
            _copy_one(src, workspace_path, dest, copied)

    # Note any unpushed commits so a human knows a branch may already carry work.
    notes = []
    notes.append(f"task: {task_id}")
    notes.append(f"status: {status}")
    notes.append(f"branch: {branch_name}")
    notes.append(f"workspace: {workspace_path}")
    if unpushed:
        notes.append("UNPUSHED COMMITS (branch may already carry finished work):")
        notes.extend(f"  {c}" for c in unpushed)
    notes.append("tracked changes written to: tracked-changes.diff")
    notes.append("untracked files copied to: untracked/")
    logpath.write_text("\n".join(notes) + "\n", encoding="utf-8")

    result["preserved"] = str(dest)
    result["tracked_diff_bytes"] = len(diff_p.stdout.encode("utf-8", errors="replace"))
    result["untracked_files"] = len(copied)
    result["dirty_lines"] = len(dirty_lines)
    result["unpushed_commits"] = len(unpushed) if unpushed else 0
    return result


def _copy_one(src: Path, workspace_path: str, dest: Path, copied: list) -> None:
    rel = src.relative_to(Path(workspace_path))
    target = dest / "untracked" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, target)
        copied.append(str(rel))
    except OSError as exc:
        print(f"  warning: could not copy {src}: {exc}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--board", default=os.environ.get("HERMES_KANBAN_BOARD") or "hscc")
    ap.add_argument("--db", default=None)
    ap.add_argument("--task", action="append", default=[], help="task id to recover; repeatable")
    ap.add_argument("--task-list", default=None, help="JSON file with {\"tasks\": [...]}")
    ap.add_argument("--blocked", action="store_true")
    ap.add_argument("--crashed", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    db = args.db or resolve_board_db(args.board)
    if not os.path.exists(db):
        print(f"board DB not found: {db}", file=sys.stderr)
        return 2

    # Resolve default recovery root under the real hermes home (see
    # resolve_board_db for why HERMES_HOME is not used here).
    home = str(Path.home() / ".hermes")
    out_root = Path(args.out) if args.out else Path(home) / "kanban" / "recovered-worker-work" / args.board

    task_ids = list(args.task)
    if args.task_list:
        with open(args.task_list) as f:
            task_ids += json.load(f).get("tasks", [])

    conn = sqlite3.connect(db)
    try:
        if args.all or args.blocked or args.crashed:
            statuses = []
            if args.all:
                statuses = None  # every row
            else:
                if args.blocked:
                    statuses.append("blocked")
                if args.crashed:
                    statuses.append("crashed")
            cur = conn.cursor()
            if statuses:
                cur.execute(
                    f"SELECT id FROM tasks WHERE status IN ({','.join('?'*len(statuses))})",
                    statuses,
                )
            else:
                cur.execute("SELECT id FROM tasks")
            task_ids += [r[0] for r in cur.fetchall()]

        task_ids = list(dict.fromkeys(task_ids))  # dedupe, keep order
        if not task_ids:
            print("no tasks selected; nothing to recover", file=sys.stderr)
            return 2

        out_root.mkdir(parents=True, exist_ok=True)
        results = [snapshot_task(conn, tid, out_root) for tid in task_ids]
    finally:
        conn.close()

    if args.json:
        print(json.dumps({"out": str(out_root), "results": results}, indent=2))
    else:
        print(f"recovery root: {out_root}")
        for r in results:
            line = f"  {r['task']:<14} [{r['status']}]"
            if r.get("error"):
                line += f" ERROR {r['error']}"
            elif r.get("note"):
                line += f" {r['note']}"
            else:
                line += (f" -> {r['preserved']} "
                         f"(diff {r['tracked_diff_bytes']}B, "
                         f"untracked {r['untracked_files']} file(s), "
                         f"unpushed {r['unpushed_commits']})")
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
