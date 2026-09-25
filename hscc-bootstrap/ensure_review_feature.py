"""Re-apply the kanban review feature to the Hermes runtime after an update.

The ``kanban_submit_review`` tool + policy-gated ``auto_review`` pairing lives in
an upstream PR (NousResearch/hermes-agent#43425) that HSCC depends on. Until it
merges upstream it is carried as a local commit on the runtime checkout, which a
``hermes update`` can drop. This step restores it idempotently:

  * If the runtime already exposes ``kanban_submit_review`` — no-op.
  * Otherwise cherry-pick the feature commit from the fork branch.

Best-effort and SAFE: it refuses to touch a dirty or mid-operation repo, and on
any cherry-pick failure it aborts cleanly (never leaves the runtime in a
conflicted state) and reports the failure so bootstrap can warn, not die. The
permanent fix is the PR merging upstream, after which the feature arrives via a
plain ``hermes update`` and this step becomes a no-op.
"""

import json
import os
import re
import subprocess


HERMES_DIR = os.path.expanduser("~/.hermes/hermes-agent")
FORK_REMOTE = os.environ.get("HSCC_HERMES_FORK_REMOTE", "fork")
FEATURE_BRANCH = os.environ.get(
    "HSCC_REVIEW_FEATURE_BRANCH", "feat/kanban-submit-review")
# The tool name whose presence means the feature is already installed.
_MARKER = "kanban_submit_review"

# A safe git ref token: alnum start, no leading '-' (blocks option smuggling).
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def _git(args, cwd, timeout=60):
    """Run a git command; return (ok, stdout, stderr)."""
    try:
        r = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return False, "", str(e)


def _feature_state(hermes_dir):
    """Return 'present', 'absent', or 'unreadable' for the review marker.

    'present' — kanban_tools.py read and contains the marker.
    'absent'  — kanban_tools.py read and does NOT contain the marker.
    'unreadable' — the marker file could not be read (missing/renamed/
        permission fault). Returning 'present' here would claim the feature is
        installed when we never actually looked — a failure to look is not a
        data fact — so it is surfaced as a distinct non-ok status instead of
        silently skipping the install.
    """
    tool_file = os.path.join(hermes_dir, "tools", "kanban_tools.py")
    try:
        with open(tool_file, "r") as f:
            return "present" if _MARKER in f.read() else "absent"
    except OSError:
        return "unreadable"


def ensure_review_feature(hermes_dir=None, remote=None, branch=None):
    """Ensure the kanban review feature is present in the Hermes runtime.

    Returns a dict: {status, ok, [detail]}. status is one of:
      already_present | applied | unreadable | not_git | dirty | fetch_failed |
      conflict | missing_ref | skipped_missing | invalid_config
    """
    hermes_dir = hermes_dir or HERMES_DIR
    remote = remote or FORK_REMOTE
    branch = branch or FEATURE_BRANCH

    # ``remote``/``branch`` are env-overridable and flow into git argv. Reject
    # anything that isn't a plain ref token (in particular a leading ``-``) so a
    # value can't smuggle a git option (e.g. ``--upload-pack=…``).
    if not (_REF_RE.fullmatch(remote) and _REF_RE.fullmatch(branch)):
        return {"status": "invalid_config", "ok": False,
                "detail": "remote/branch must match a plain ref token"}

    if not os.path.isdir(hermes_dir):
        return {"status": "skipped_missing", "ok": True}

    state = _feature_state(hermes_dir)
    if state == "present":
        return {"status": "already_present", "ok": True}
    if state == "unreadable":
        # We could not even look for the marker — an environment fault (file
        # missing/renamed/permission), NOT a fact that the feature is present.
        # Report it so bootstrap warns instead of believing the feature is
        # installed (previously this silently returned already_present: ok).
        return {"status": "unreadable", "ok": False,
                "detail": "cannot read tools/kanban_tools.py in the runtime; "
                          "cannot verify the review feature — refusing to "
                          "claim it is present"}

    if not os.path.isdir(os.path.join(hermes_dir, ".git")):
        return {"status": "not_git", "ok": False,
                "detail": "runtime is not a git checkout — cannot cherry-pick"}

    # Refuse to operate on a dirty or mid-operation repo — never risk corrupting
    # the live runtime.
    ok, out, _ = _git(["status", "--porcelain"], hermes_dir)
    if not ok or out:
        return {"status": "dirty", "ok": False,
                "detail": "runtime has uncommitted changes — skipping cherry-pick"}
    if os.path.exists(os.path.join(hermes_dir, ".git", "CHERRY_PICK_HEAD")) or \
       os.path.exists(os.path.join(hermes_dir, ".git", "MERGE_HEAD")):
        return {"status": "dirty", "ok": False,
                "detail": "runtime is mid git operation — skipping cherry-pick"}

    # Fetch the feature branch (best-effort).
    ok, _, err = _git(["fetch", remote, branch], hermes_dir, timeout=120)
    if not ok:
        return {"status": "fetch_failed", "ok": False, "detail": err[:200]}

    ref = f"{remote}/{branch}"
    ok, _, _ = _git(["rev-parse", "--verify", ref], hermes_dir)
    if not ok:
        return {"status": "missing_ref", "ok": False,
                "detail": f"{ref} not found after fetch"}

    ok, _, err = _git(["cherry-pick", ref], hermes_dir, timeout=120)
    if not ok:
        # Abort cleanly — never leave the runtime in a conflicted state.
        _git(["cherry-pick", "--abort"], hermes_dir)
        return {"status": "conflict", "ok": False, "detail": err[:200]}

    return {"status": "applied", "ok": True}


if __name__ == "__main__":
    print(json.dumps(ensure_review_feature()))
