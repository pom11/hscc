"""Branch-scoped force-push fence for dispatched kanban workers (MQ3).

WHY THIS EXISTS
---------------
Hermes flags every ``git push --force*`` as a dangerous command. An unattended
kanban worker cannot answer an approval prompt, so hscc seeds
``approvals.single_query_mode: approve`` (``hscc-bootstrap/enable_plugins.py``)
to keep workers able to run code. That allowance is fleet-wide and
branch-blind: without this guard a worker could force-push ``main`` or another
card's branch, and the first such push rewrites published history.

This is the branch half of the fence. A worker run is any process carrying
``HERMES_KANBAN_TASK`` (the dispatcher exports it, plus ``HERMES_KANBAN_BRANCH``
— ``hermes_cli/kanban_db_dispatch.py``). Such a run may force-update ONLY its
own card branch: ``HERMES_KANBAN_BRANCH`` when set, else ``wt/<task>``.
Non-fast-forward updates to ``main``, tags, or any other branch are refused.
Non-worker processes — the operator, orchestrators, the merge-queue daemon in
its own worktree — are not touched.

HOW IT IS ARMED
---------------
``.githooks/pre-push`` executes this module. ``core.hooksPath`` lives in the
repo's COMMON git config (``hscc-bootstrap/install_hooks.py``), so every linked
worktree, including the dispatcher's worker worktrees, runs it with no
per-worktree step — the same mechanism as the commit-time address guard.

A pre-push hook cannot see the client's ``--force`` flag directly. Git hands it
``<local ref> <local sha> <remote ref> <remote sha>`` per update, so the guard
classifies an update as a force exactly when the remote tip is not an ancestor
of the local tip. Creates (zero remote sha) and fast-forwards stay allowed.
"""

from __future__ import annotations

import os
import subprocess
import sys

WORKER_TASK_ENV = "HERMES_KANBAN_TASK"
WORKER_BRANCH_ENV = "HERMES_KANBAN_BRANCH"
ZERO_SHA = "0" * 40


def worker_task(env):
    """The dispatched worker's task id, or "" outside a worker run."""
    return (env.get(WORKER_TASK_ENV) or "").strip()


def own_branch(env):
    """The one branch a worker run may force-push.

    ``HERMES_KANBAN_BRANCH`` is the branch recorded on the card; older cards
    that carry no branch use the ``wt/<card>`` convention.
    """
    branch = (env.get(WORKER_BRANCH_ENV) or "").strip()
    if branch:
        return branch
    task = worker_task(env)
    return f"wt/{task}" if task else ""


def normalize_branch(ref):
    """``refs/heads/x`` -> ``x``; every other ref is returned verbatim."""
    ref = (ref or "").strip()
    prefix = "refs/heads/"
    return ref[len(prefix):] if ref.startswith(prefix) else ref


def _is_ancestor(remote_sha, local_sha, cwd=None):
    """True when *remote_sha* is an ancestor of *local_sha* (a fast-forward).

    Any git failure returns False: an update that cannot be proven to be a
    fast-forward is treated as a force push, which is the safe direction.
    """
    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", remote_sha, local_sha],
            cwd=cwd, capture_output=True, text=True)
    except OSError:
        return False
    return proc.returncode == 0


def check_updates(updates, env, *, cwd=None, ancestor=None):
    """Refusals for the updates a worker run is about to push.

    *updates* is an iterable of ``(local_ref, local_sha, remote_ref,
    remote_sha)`` tuples. Returns a list of human-readable refusal strings —
    empty means the push may go ahead. Non-worker runs always return [].
    """
    task = worker_task(env)
    if not task:
        return []
    own = own_branch(env)
    ancestor = ancestor or (lambda remote, local: _is_ancestor(remote, local, cwd))

    refusals = []
    for _local_ref, local_sha, remote_ref, remote_sha in updates:
        local_sha = (local_sha or "").strip()
        remote_sha = (remote_sha or "").strip()
        # Creates are not force pushes, and the branch may not exist yet.
        if not remote_sha or set(remote_sha) == {"0"}:
            continue
        if ancestor(remote_sha, local_sha):
            continue  # fast-forward
        target = normalize_branch(remote_ref)
        if remote_ref == f"refs/heads/{own}" and target == own:
            continue
        refusals.append(
            f"REFUSED by hscc worker push guard: force-push to "
            f"'{target or remote_ref}' is not allowed.\n"
            f"A dispatched worker may force-push only its own card branch "
            f"'{own}'. Push main and other branches normally, or hand the "
            f"card back with the conflict.")
    return refusals


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in ("--pre-push",):
        print(f"usage: {argv[0]} --pre-push <remote> <url>", file=sys.stderr)
        return 2
    cwd = None
    updates = []
    for line in sys.stdin:
        parts = line.strip().split()
        if len(parts) != 4:
            continue
        updates.append(tuple(parts))
    refusals = check_updates(updates, os.environ, cwd=cwd)
    if refusals:
        print("\n".join(refusals), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
