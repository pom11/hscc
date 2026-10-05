#!/usr/bin/env python3
"""Execute the t_ad5dd538 worktree/branch prune — guarded, checklist-driven.

Reads classify.json (produced by scripts/worktree_hygiene.py), then for each
candidate RE-VERIFIES everything at deletion time. Any check that fails ->
keep + log. Nothing is ever force-removed and branches are deleted with -d
only, never -D.

Hard scope:
- worktrees: only /Users/desac/dev/hscc/.worktrees/t_* dirs registered in
  `git worktree list` (task constraint).
- branches: only refs matching wt/*.
- origin/main is re-resolved fresh; ancestry re-checked per branch.
"""
import json
import os
import re
import subprocess
import sys

PRIMARY = "/Users/desac/dev/hscc"
WT_ROOT = "/Users/desac/dev/hscc/.worktrees/"
CLASSIFY = "/Users/desac/.hermes/profiles/backend-engineer/cache/scratch/t_ad5dd538/classify.json"
LOG = "/Users/desac/.hermes/profiles/backend-engineer/cache/scratch/t_ad5dd538/prune_log.json"


def git(*args, cwd=PRIMARY):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def merged(tip):
    rc, _, _ = git("merge-base", "--is-ancestor", tip, "origin/main")
    return rc == 0


def main():
    dry = "--apply" not in sys.argv
    data = json.load(open(CLASSIFY))
    origin_main = git("rev-parse", "origin/main")[1]
    actions = []

    def log(path, branch, verdict, detail):
        actions.append({"path": path, "branch": branch, "verdict": verdict,
                        "detail": detail})
        print(f"[{'KEEP' if verdict.startswith('keep') else verdict}] "
              f"{os.path.basename(path) or path} ({branch}): {detail}")

    eligible, skipped = [], []
    for e in data["worktrees"]:
        if e["action"] != "remove":
            continue
        p = e["path"]
        if not p.startswith(WT_ROOT) or not re.match(
                r"^t_[0-9a-f]+$", os.path.basename(p)):
            skipped.append((e, "outside .worktrees/t_* scope"))
            continue
        eligible.append(e)

    for e in eligible:
        path, branch = e["path"], e["branch"]
        # 1. registered at expected path
        rc, out, _ = git("worktree", "list", "--porcelain")
        registered = any(l == f"worktree {path}" for l in out.splitlines())
        if not registered:
            log(path, branch, "keep", "not registered (already gone?)")
            continue
        # 2. branch name shape
        if not branch.startswith("wt/"):
            log(path, branch, "keep", f"branch not wt/*: {branch}")
            continue
        # 3. tip unchanged since classification + merged into fresh origin/main
        rc, tip, _ = git("rev-parse", branch)
        if rc != 0 or tip[:10] != e.get("tip"):
            log(path, branch, "keep", "branch tip missing or moved since audit")
            continue
        if not merged(tip):
            log(path, branch, "keep", "not merged into origin/main (recheck)")
            continue
        # 4. worktree still clean (tracked mods or untracked -> keep)
        if os.path.exists(path):
            rc, st, _ = git("status", "--porcelain", cwd=path)
            if rc != 0:
                log(path, branch, "keep", "status unreadable")
                continue
            if st.strip():
                log(path, branch, "keep", f"dirty at delete time: {st.splitlines()[:2]}")
                continue
        # 5. branch not checked out in another worktree
        rc, out, _ = git("worktree", "list", "--porcelain")
        holders = []
        cur = {}
        for line in out.splitlines():
            if not line.strip():
                if cur.get("branch", "").endswith("/" + branch):
                    holders.append(cur.get("worktree"))
                cur = {}
            elif " " in line:
                k, v = line.split(" ", 1)
                cur[k] = v
        holders = [h for h in holders if h != path]
        if holders:
            log(path, branch, "keep", f"branch also checked out at {holders}")
            continue
        # all gates passed
        if dry:
            log(path, branch, "would-remove", "all gates passed")
            continue
        rc, _, err = git("worktree", "remove", path)
        if rc != 0:
            log(path, branch, "keep", f"worktree remove failed: {err[:120]}")
            continue
        # 6. branch deletion: -d only, and only after ancestry proven above
        rc, _, err = git("branch", "-d", branch)
        if rc != 0:
            log(path, branch, "removed-worktree-kept-branch",
                f"worktree gone; branch -d refused: {err[:120]}")
            continue
        log(path, branch, "removed", "worktree removed + branch -d ok")

    # orphan wt/* branches (no worktree) proven merged
    for e in data["branches_only"]:
        if e["action"] != "delete":
            continue
        branch = e["branch"]
        if not branch.startswith("wt/"):
            log("(none)", branch, "keep", "not wt/*")
            continue
        rc, tip, _ = git("rev-parse", branch)
        if rc != 0:
            log("(none)", branch, "keep", "ref gone")
            continue
        if tip[:10] != e.get("tip") or not merged(tip):
            log("(none)", branch, "keep", "tip moved or not merged (recheck)")
            continue
        if dry:
            log("(none)", branch, "would-delete-branch", "merged orphan")
            continue
        rc, _, err = git("branch", "-d", branch)
        log("(none)", branch,
            "deleted-branch" if rc == 0 else "keep",
            "branch -d ok" if rc == 0 else f"branch -d refused: {err[:120]}")

    json.dump({"dry_run": dry, "origin_main": origin_main[:10],
               "out_of_scope_skipped": [
                   {"path": e["path"], "branch": e["branch"], "reason": r}
                   for e, r in skipped],
               "actions": actions},
              open(LOG, "w"), indent=1)
    n = {}
    for a in actions:
        n[a["verdict"]] = n.get(a["verdict"], 0) + 1
    print("\nsummary:", n)
    print("out-of-scope skipped:", [(os.path.basename(e['path']), r) for e, r in skipped])


if __name__ == "__main__":
    main()
