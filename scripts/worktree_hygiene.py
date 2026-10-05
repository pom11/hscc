#!/usr/bin/env python3
"""Worktree/branch hygiene audit for the hscc primary checkout (t_ad5dd538).

Classification only -- this script NEVER deletes anything. It enumerates the
registered worktrees and wt/* branches, decides merge status and dirtiness,
and prints JSON verdicts for human/agent review.

Safety rules encoded here:
- Only branches matching refs/heads/wt/* are candidates.
- A branch is "merged" only if its tip is an ancestor of origin/main.
- A worktree is removable only if the dir is gone, empty, or has no
  tracked modifications AND no untracked files.
- Anything uncertain -> KEEP.
"""
import json
import os
import subprocess
import sys

PRIMARY = "/Users/desac/dev/hscc"
PREFIX = "wt/"


def git(*args, check=True):
    r = subprocess.run(
        ["git", "-C", PRIMARY, *args],
        capture_output=True, text=True,
    )
    if check and r.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def classify():
    origin_main = git("rev-parse", "origin/main").strip()

    # worktrees -> {path: (branch, admin_dir_exists)}
    admin = os.path.join(PRIMARY, ".git", "worktrees")
    admins = set(os.listdir(admin)) if os.path.isdir(admin) else set()

    worktrees = {}
    cur = {}
    for line in git("worktree", "list", "--porcelain").splitlines():
        if not line.strip():
            if cur.get("worktree"):
                worktrees[cur["worktree"]] = (cur.get("branch", ""), cur)
            cur = {}
        elif " " in line:
            k, v = line.split(" ", 1)
            cur[k] = v
    if cur.get("worktree"):
        worktrees[cur["worktree"]] = (cur.get("branch", ""), cur)

    results: dict = {"worktrees": [], "branches_only": []}

    wt_branches = [
        l.strip()[2:] if l.startswith("* ") else l.strip()
        for l in git("branch", "--list", PREFIX + "*").splitlines()
    ]
    wt_branches = [b for b in wt_branches if b]

    # branch -> worktree path
    by_branch = {b: p for p, (b, _) in worktrees.items() if b}

    for path, (branch, attrs) in sorted(worktrees.items()):
        entry = {
            "path": path,
            "branch": branch,
            "primary": path == PRIMARY,
            "locked": "locked" in attrs,
            "action": "keep",
            "reason": "",
        }
        if entry["primary"]:
            entry["reason"] = "primary checkout"
            results["worktrees"].append(entry)
            continue
        if entry["locked"]:
            entry["reason"] = "worktree locked"
            results["worktrees"].append(entry)
            continue
        if not branch.startswith(PREFIX):
            entry["reason"] = f"branch not under {PREFIX}*: {branch or '(detached)'}"
            results["worktrees"].append(entry)
            continue

        tip = git("rev-parse", f"{branch}", check=False).strip()
        if not tip:
            entry["reason"] = "branch ref missing"
            results["worktrees"].append(entry)
            continue
        merged = subprocess.run(
            ["git", "-C", PRIMARY, "merge-base", "--is-ancestor", tip, origin_main],
            capture_output=True,
        ).returncode == 0
        entry["tip"] = tip[:10]
        entry["merged"] = merged

        if not os.path.exists(path):
            entry["dir_state"] = "gone"
        else:
            # registered but missing dir also shows as gone
            porcelain = git("-C", path, "status", "--porcelain", check=False)
            lines = [l for l in porcelain.splitlines() if l.strip()]
            entry["dir_state"] = "exists"
            entry["dirty_lines"] = len(lines)
            entry["untracked"] = any(l.startswith("??") for l in lines)
            entry["tracked_mods"] = any(not l.startswith("??") for l in lines)
            entry["files"] = os.listdir(path) if os.path.isdir(path) else None
            if entry["files"] is not None and len(entry["files"]) == 0:
                entry["dir_state"] = "empty"

        dirty = entry["dir_state"] == "exists" and (
            entry.get("tracked_mods") or entry.get("untracked")
        )
        if entry["dir_state"] == "exists" and entry.get("files") is None:
            dirty = True  # cannot inspect -> keep

        if merged and not dirty:
            entry["action"] = "remove"
            entry["reason"] = f"merged into origin/main; dir={entry['dir_state']}"
        else:
            reasons = []
            if not merged:
                reasons.append("not merged into origin/main")
            if dirty:
                reasons.append(
                    f"dirty worktree (tracked_mods={entry.get('tracked_mods')}, "
                    f"untracked={entry.get('untracked')})"
                )
            entry["reason"] = "; ".join(reasons)
        results["worktrees"].append(entry)

    # wt/* branches with no registered worktree
    ancestor_cache = {}
    for b in sorted(wt_branches):
        if b in by_branch:
            continue
        tip = git("rev-parse", b).strip()
        if tip not in ancestor_cache:
            ancestor_cache[tip] = subprocess.run(
                ["git", "-C", PRIMARY, "merge-base", "--is-ancestor", tip, origin_main],
                capture_output=True,
            ).returncode == 0
        merged = ancestor_cache[tip]
        results["branches_only"].append({
            "branch": b,
            "tip": tip[:10],
            "merged": merged,
            "action": "delete" if merged else "keep",
            "reason": "merged into origin/main, no worktree" if merged
                      else "not merged into origin/main",
        })

    results["summary"] = {
        "origin_main": origin_main[:10],
        "worktrees_total": len(results["worktrees"]),
        "worktrees_removable": sum(
            1 for e in results["worktrees"] if e["action"] == "remove"),
        "wt_branches_total": len(wt_branches),
        "branches_only_total": len(results["branches_only"]),
        "branches_only_deletable": sum(
            1 for e in results["branches_only"] if e["action"] == "delete"),
        "worktrees_kept": sum(1 for e in results["worktrees"] if e["action"] == "keep"),
        "branches_only_kept": sum(
            1 for e in results["branches_only"] if e["action"] == "keep"),
    }
    return results


if __name__ == "__main__":
    out = classify()
    print(json.dumps(out, indent=1))
