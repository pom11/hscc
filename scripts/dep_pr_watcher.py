#!/usr/bin/env python3
"""Turn dependency-bump PRs into kanban verification cards for the cluster.

The ``check-runtime-deps`` GitHub Action opens a PR (label
``needs-cluster-check``, the repo owner as reviewer) whenever hermes-agent or
sparkrun ships a new release. This watcher — run daily on the gateway — finds
those PRs and creates one kanban card per PR so a worker verifies the bump
end-to-end. Idempotent: a stable ``idempotency_key`` per PR means re-runs never
duplicate a card.

Run under the Hermes venv python (it imports ``hermes_cli.kanban_db``):

    ~/.hermes/hermes-agent/venv/bin/python scripts/dep_pr_watcher.py

Config via env (sensible defaults):
  HSCC_REPO            GitHub repo to watch          (default: pom11/hscc)
  HSCC_PR_LABEL        label the Action sets         (default: needs-cluster-check)
  HSCC_PR_REVIEWER     required review request       (default: repo owner)
  HSCC_DEP_ASSIGNEE    profile to run the card       (default: backend-engineer)
  HSCC_KANBAN_BOARD    board to create verification  (default: hscc)
  HSCC_WORKDIR         repo worktree for the card    (default: ~/dev/hscc)
"""

import json
import os
import subprocess
import sys

# Make ``hermes_cli`` importable however this is launched (Hermes cron, launchd,
# or a bare python3) — the card creation needs the runtime's kanban_db.
_HERMES = os.path.expanduser("~/.hermes/hermes-agent")
if os.path.isdir(_HERMES) and _HERMES not in sys.path:
    sys.path.insert(0, _HERMES)

REPO = os.environ.get("HSCC_REPO", "pom11/hscc")
LABEL = os.environ.get("HSCC_PR_LABEL", "needs-cluster-check")
REVIEWER = os.environ.get("HSCC_PR_REVIEWER", REPO.split("/")[0])
ASSIGNEE = os.environ.get("HSCC_DEP_ASSIGNEE", "backend-engineer")
BOARD = os.environ.get("HSCC_KANBAN_BOARD", "hscc")
WORKDIR = os.path.expanduser(os.environ.get("HSCC_WORKDIR", "~/dev/hscc"))


def list_review_prs():
    """Return open PRs with the label AND the reviewer requested.

    Best-effort: any gh/parse failure yields [] (never raises), so a flaky
    network never wedges the daily run.
    """
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "--repo", REPO, "--state", "open",
             "--label", LABEL, "--json", "number,title,url,reviewRequests"],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            print(f"gh pr list failed: {out.stderr.strip()}", file=sys.stderr)
            return []
        prs = json.loads(out.stdout or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
        print(f"gh pr list error: {e}", file=sys.stderr)
        return []

    wanted = []
    for pr in prs:
        reviewers = {
            (r.get("login") or r.get("slug") or "").lower()
            for r in (pr.get("reviewRequests") or [])
        }
        if REVIEWER.lower() in reviewers:
            wanted.append(pr)
    return wanted


def _card_body(pr):
    return (
        f"Verify the runtime dependency bump in PR {pr['url']} "
        f"(\"{pr.get('title', '')}\").\n\n"
        "Steps:\n"
        "1. Read the PR — which dependency moved and to what version.\n"
        "2. Upgrade the runtime with a recovery point "
        "(`hermes update` / rebase-bump sparkrun in ~/sparkrun).\n"
        "3. Re-bootstrap HSCC: `~/dev/hscc/hscc-bootstrap/bootstrap.sh`.\n"
        "4. Run every component test suite (bootstrap, cluster, commands, "
        "roles, daemon, sparkrun-hermes).\n"
        "5. Verify live: daemon streams, multiplex served profiles, a slash "
        "command, and the :4000 proxy.\n"
        "6. Report what worked and any incompatibilities fixed. Do NOT merge "
        "the PR — leave that to the human reviewer."
    )


def ensure_cards(prs, *, _kb=None):
    """Create one idempotent verification card per PR. Returns list of task ids."""
    if _kb is None:
        from hermes_cli import kanban_db as _kb
    created = []
    with _kb.connect_closing(board=BOARD) as conn:
        for pr in prs:
            num = pr["number"]
            key = f"dep-check-pr-{num}"
            tid = _kb.create_task(
                conn,
                title=f"verify runtime dep bump — PR #{num}",
                body=_card_body(pr),
                assignee=ASSIGNEE,
                created_by="hscc-dep-watcher",
                session_id="hscc_dep_watcher",
                idempotency_key=key,
                board=BOARD,
            )
            conn.execute(
                "UPDATE tasks SET workspace_kind='worktree', workspace_path=? "
                "WHERE id=?",
                (WORKDIR, tid),
            )
            created.append((num, tid))
        conn.commit()
    return created


def main():
    # Silent when idle: under Hermes cron --no-agent mode, empty stdout means no
    # delivery, so a routine "nothing to do" run stays quiet. Only speak up when
    # a card is (or already was) created for a pending dep-bump PR.
    prs = list_review_prs()
    if not prs:
        return 0
    created = ensure_cards(prs)
    lines = [f"🔁 HSCC dep-check: PR #{num} → kanban card {tid}"
             for num, tid in created]
    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
