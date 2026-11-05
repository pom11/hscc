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

The runtime checkout is resolved at CALL time through the shared homes ladder
(``hscc_daemon.hermes_python.hermes_agent_dir``; see :func:`resolve_hermes_dir`).
It must never be an import-time ``$HERMES_HOME/hermes-agent`` join: under a
profile session that path cannot exist, and this step would report a green
``skipped_missing`` while the feature stayed dropped (t_919ac971).
"""

import json
import os
import re
import subprocess
import sys


FORK_REMOTE = os.environ.get("HSCC_HERMES_FORK_REMOTE", "fork")
FEATURE_BRANCH = os.environ.get(
    "HSCC_REVIEW_FEATURE_BRANCH", "feat/kanban-submit-review")
# The tool name whose presence means the feature is already installed.
_MARKER = "kanban_submit_review"

# A safe git ref token: alnum start, no leading '-' (blocks option smuggling).
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def _load_hermes_python():
    """Import ``hscc_daemon/hermes_python.py`` for the runtime-checkout ladder.

    Deferred + guarded, same pattern as doctor.py's ``_load_orch_waker``: the
    resolution rule lives with the shared ladder (t_314eb13c) so this step
    cannot drift from how the rest of HSCC finds the Hermes install. Returns
    the module, or None when hscc_daemon is not deployed next to this plugin
    (a bootstrap-only install), in which case :func:`_fallback_homes` covers us.
    """
    try:
        from hscc_daemon import hermes_python
        return hermes_python
    except Exception:
        pass
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from hscc_daemon import hermes_python
        return hermes_python
    except Exception:
        return None


def _fallback_homes(env=None, *, home=None):
    """Candidate Hermes ROOTS when ``hscc_daemon`` is unavailable.

    Deliberately the SAME three steps as ``hermes_python.hermes_homes`` —
    exported ``$HERMES_HOME``, its ``profiles/`` grandparent, then
    ``~/.hermes`` — because a shorter list here is exactly the bug being fixed:
    any resolver that looks only at the exported home reports "no runtime" from
    every profile session. ``test_ladder_drift`` pins the two against each
    other so this cannot drift silently.
    """
    env = os.environ if env is None else env
    home = home or os.path.expanduser("~")
    homes: list[str] = []

    def _push(path):
        if not path:
            return
        path = os.path.expanduser(str(path))
        if path not in homes:
            homes.append(path)

    _push(env.get("HERMES_HOME", "").strip())
    if homes:
        parent = os.path.dirname(homes[0])
        if parent and os.path.basename(parent) == "profiles":
            _push(os.path.dirname(parent))
    _push(os.path.join(os.path.expanduser(str(home)), ".hermes"))
    return [os.path.join(h, "hermes-agent") for h in homes]


def agent_dir_candidates(env=None, *, home=None) -> list[str]:
    """Candidate ``hermes-agent`` checkout paths, in the order they are tried.

    Shared-module ladder when available, :func:`_fallback_homes` otherwise.
    Non-existent candidates are kept so :func:`resolve_hermes_dir` can report
    every path it ruled out.
    """
    hp = _load_hermes_python()
    if hp is not None:
        return hp.agent_dir_candidates(env, home=home)
    return _fallback_homes(env, home=home)


def resolve_hermes_dir(env=None, *, home=None):
    """Resolve the runtime checkout: ``(hermes_dir_or_None, searched_detail)``.

    Resolved **at call time**, never at import. The old module-level
    ``HERMES_DIR = $HERMES_HOME/hermes-agent`` (L24-25 before this fix) bound
    the profile dir + ``hermes-agent`` — a path that cannot exist under a
    profile session, which is Hermes' normal run mode. ``os.path.isdir`` was
    then false, this step returned ``skipped_missing``/ok, bootstrap printed
    "kanban review feature: skipped_missing" as a green row, and the PR #43425
    feature a ``hermes update`` had dropped stayed dropped with nobody told
    (t_919ac971).

    ``searched_detail`` is non-None only on the no-runtime path, and names every
    candidate that was ruled out — so a genuine "no Hermes install here" is
    auditable rather than a bare word.
    """
    for cand in agent_dir_candidates(env, home=home):
        try:
            if os.path.isdir(cand):
                return cand, None
        except OSError:  # pragma: no cover - unreadable path is not a hit
            continue
    return None, ", ".join(agent_dir_candidates(env, home=home)) or "no candidate paths"



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


def ensure_review_feature(hermes_dir=None, remote=None, branch=None,
                          env=None, home=None):
    """Ensure the kanban review feature is present in the Hermes runtime.

    Returns a dict: {status, ok, [detail]}. status is one of:
      already_present | applied | unreadable | not_git | dirty | fetch_failed |
      conflict | missing_ref | skipped_missing | invalid_config

    ``hermes_dir=None`` resolves the checkout through the shared ladder at call
    time (see :func:`resolve_hermes_dir`); an explicit ``hermes_dir`` is used as
    given. ``env``/``home`` are injectable for tests so the ladder can be
    exercised against fabricated roots — this step must never read or write the
    operator's real ``~/.hermes`` from a test.
    """
    if not hermes_dir:
        hermes_dir, searched = resolve_hermes_dir(env=env, home=home)
        if not hermes_dir:
            # Genuinely no Hermes runtime anywhere we look — the only case that
            # stays fail-open ok=True (there is nothing here to install into).
            # It is NOT silent any more: the detail names every candidate that
            # was ruled out, so a resolver regression can no longer masquerade
            # as "there is no runtime on this box".
            return {"status": "skipped_missing", "ok": True,
                    "detail": f"no hermes-agent checkout found in: {searched}"}
    remote = remote or FORK_REMOTE
    branch = branch or FEATURE_BRANCH

    # ``remote``/``branch`` are env-overridable and flow into git argv. Reject
    # anything that isn't a plain ref token (in particular a leading ``-``) so a
    # value can't smuggle a git option (e.g. ``--upload-pack=…``).
    if not (_REF_RE.fullmatch(remote) and _REF_RE.fullmatch(branch)):
        return {"status": "invalid_config", "ok": False,
                "detail": "remote/branch must match a plain ref token"}

    if not os.path.isdir(hermes_dir):
        return {"status": "skipped_missing", "ok": True,
                "detail": f"hermes-agent checkout missing at {hermes_dir}"}

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
