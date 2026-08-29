"""git_state.py — branch/merge/dirty/staleness facts for one repository.

Answers git questions for a single repo. Every external call (subprocess) goes
through an injectable ``_run`` runner so tests use fixtures and never touch a
real repository or the network.

All functions degrade gracefully: a path that is not a git repository, or a
branch that does not exist, returns a clear sentinel (None / False / 0) and
NEVER raises to the caller. Callers show "unknown"; they do not crash.
"""

import subprocess
import time
from typing import Callable, Optional, Sequence

_CMD = Sequence[str]


def _default_run(cmd, repo):
    """Production subprocess runner. ``_run=None`` falls back to this.

    Returns a process-like object with ``.returncode``, ``.stdout`` and
    ``.stderr`` (all str). If the repo dir is missing, git is missing, or any
    other OSError occurs, returns a synthetic failed process (returncode 128)
    so callers degrade gracefully instead of raising.
    """
    try:
        return subprocess.run(
            cmd,
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        return subprocess.CompletedProcess(
            cmd, returncode=128, stdout="", stderr=str(exc)
        )


def _dispatch(cmd, repo, runner):
    """Resolve the injectable runner, defaulting to the real subprocess."""
    if runner is not None:
        return runner(cmd, repo)
    return _default_run(cmd, repo)


def is_merged(repo, branch, base="main", _run: Optional[Callable] = None):
    """True if ``branch`` is an ancestor of ``base`` — i.e. its work has landed
    in ``base``.

    Uses ``git merge-base --is-ancestor <branch> <base>``. This distinguishes a
    card that genuinely needs review from one whose work already landed, so the
    ancestry direction matters: we ask whether the *branch* is an ancestor of
    the *base*, not the reverse.

    Returns False for a non-repo path or a branch we cannot resolve (we refuse
    to claim work landed when we cannot verify it). Never raises.
    """
    cp = _dispatch(["git", "merge-base", "--is-ancestor", branch, base], repo, _run)
    return cp.returncode == 0


def branch_exists(repo, branch, _run: Optional[Callable] = None):
    """True if the ref ``branch`` resolves to a commit in ``repo``.

    Accepts local and remote branches (``origin/foo``), plus any ref git can
    resolve. Returns False for a nonexistent ref or a non-repo path.
    """
    cp = _dispatch(["git", "rev-parse", "--verify", "--quiet", branch], repo, _run)
    return cp.returncode == 0


def commits_ahead(repo, branch, base="main", _run: Optional[Callable] = None):
    """Number of commits on ``branch`` not reachable from ``base``.

    Zero commits ahead is the stall signal: a claimed/running card whose branch
    has no new commits is stuck. Returns 0 for a non-repo path or an unresolvable
    branch (the no-data case reads as "no progress", which is the safe reading
    for a stall detector).
    """
    cp = _dispatch(
        ["git", "rev-list", "--count", "{}..{}".format(base, branch)], repo, _run
    )
    if cp.returncode != 0:
        return 0
    try:
        return int(cp.stdout.strip())
    except ValueError:
        return 0


def own_commits(repo, branch, base="main", _run: Optional[Callable] = None):
    """Number of commits ``branch`` carries that ``base`` does not.

    Non-zero only while the branch is still unmerged. Kept as a stall signal;
    it CANNOT tell a merged branch from an unstarted one, because both are
    ancestors of ``base`` and both count 0. Use :func:`landed_via_merge` for
    that question. Returns 0 on any error. Never raises.
    """
    cp = _dispatch(
        ["git", "rev-list", "--count", "{}..{}".format(base, branch)], repo, _run
    )
    if cp.returncode != 0:
        return 0
    try:
        return int(cp.stdout.strip())
    except ValueError:
        return 0


def landed_via_merge(repo, branch, base="main", _run: Optional[Callable] = None):
    """True when ``branch`` was actually merged into ``base``.

    Being an ancestor of ``base`` is NOT enough, and assuming otherwise was a
    data-loss bug: a freshly created worktree branch points at the ``base`` tip
    it forked from, so as soon as ``base`` advances that branch is an ancestor
    too. Reconcile consequently proposed closing three cards that were running
    at that moment and had written nothing.

    The discriminator: this project merges with ``--no-ff``, so a branch that
    truly landed appears as the SECOND PARENT of a merge commit on ``base``. A
    branch that was never started never does. Returns False on any error — the
    conservative reading, since a false negative only leaves a card open while a
    false positive throws work away.
    """
    tip = _dispatch(["git", "rev-parse", branch], repo, _run)
    if tip.returncode != 0:
        return False
    tip_sha = tip.stdout.strip()
    if not tip_sha:
        return False
    merges = _dispatch(
        ["git", "log", base, "--merges", "--pretty=%P"], repo, _run
    )
    if merges.returncode != 0:
        return False
    for line in merges.stdout.splitlines():
        parents = line.split()
        # parents[0] is the trunk; anything after is a merged-in branch tip.
        if tip_sha in parents[1:]:
            return True
    return False


def commit_subjects(
    repo,
    branch,
    base="main",
    _run: Optional[Callable] = None,
) -> list[str]:
    """Subject lines of the commits ``branch`` carries that ``base`` does not.

    This is the branch's OWN work — ``git log <base>..<branch> --pretty=%s``,
    newest first, one subject per line. An unstarted branch (no commits of its
    own) yields []. Returns [] for a non-repo path or an unresolvable branch —
    the no-data reading is an empty list, never an exception.
    """
    cp = _dispatch(
        ["git", "log", "{}..{}".format(base, branch), "--pretty=%s"], repo, _run
    )
    if cp.returncode != 0:
        return []
    return [ln.strip() for ln in cp.stdout.splitlines() if ln.strip()]


def uncommitted_files(repo, _run: Optional[Callable] = None) -> list[str]:
    """Paths of changed files (modified/staged/untracked) in the working tree.

    Parses ``git status --porcelain`` — one line per changed file. The porcelain
    format is ``XY PATH`` (renames carry a second path); we return the visible
    path (the second field, which is the real name for a rename). Returns [] for
    a clean tree or a non-repo path.
    """
    cp = _dispatch(["git", "status", "--porcelain"], repo, _run)
    if cp.returncode != 0:
        return []
    files: list[str] = []
    for line in cp.stdout.splitlines():
        if not line.strip():
            continue
        # "XY PATH" — for a rename/copy the format is "XY OLD -> NEW", so the
        # LAST token is the real on-disk path.
        parts = line.split()
        if len(parts) >= 2:
            files.append(parts[-1])
    return files


def is_dirty(repo, _run: Optional[Callable] = None):
    """Count of working-tree changes (modified/staged/untracked files) in repo.

    Parses ``git status --porcelain`` — one line per changed file, including
    untracked. A clean tree yields 0. Returns 0 for a non-repo path.
    """
    cp = _dispatch(["git", "status", "--porcelain"], repo, _run)
    if cp.returncode != 0:
        return 0
    return len([line for line in cp.stdout.splitlines() if line.strip()])


def current_branch(repo, _run: Optional[Callable] = None):
    """Name of the currently checked-out branch, or None if unresolvable.

    Returns None for a non-repo path. A detached HEAD yields "HEAD" (the raw
    git abbreviation) — still a truthful answer, not a crash.
    """
    cp = _dispatch(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo, _run)
    if cp.returncode != 0:
        return None
    name = cp.stdout.strip()
    return name or None


def last_commit_age_seconds(
    repo,
    branch=None,
    _run: Optional[Callable] = None,
    _now: Optional[Callable] = None,
):
    """Age in seconds of the most recent commit on ``branch`` (default HEAD).

    Uses ``git log -1 --format=%ct`` (commit unix time) and the injectable
    ``_now`` clock (defaults to ``time.time``). Never negative — a date in the
    future (clock skew) clamps to 0. Returns None for a non-repo path or an
    unresolvable branch.
    """
    now = _now() if _now is not None else time.time()
    cmd = ["git", "log", "-1", "--format=%ct"]
    if branch is not None:
        cmd.append(branch)
    cp = _dispatch(cmd, repo, _run)
    if cp.returncode != 0:
        return None
    try:
        commit_ts = int(cp.stdout.strip())
    except ValueError:
        return None
    return max(0, int(now) - commit_ts)


def merged_subjects_since(
    repo,
    since_ts,
    base="main",
    _run: Optional[Callable] = None,
):
    """Subject lines of branches merged into ``base`` at/after ``since_ts``.

    Returns the SUBJECT lines of merge commits on ``base`` (the commits that
    pull a branch in — this project merges with ``--no-ff``, so each ``--merges``
    commit is one landed branch). Each subject is a line like ``merge: <title>
    (t_card)``. Only merge commits whose committer unix time is >= ``since_ts``
    are returned, so a caller can render exactly what landed inside one window.

    Returns [] for a non-repo path or an unparseable log — the no-data reading
    is an empty list, never an exception. Never raises.
    """
    cp = _dispatch(
        ["git", "log", base, "--merges", "--pretty=%ct%x09%s"], repo, _run
    )
    if cp.returncode != 0:
        return []
    subjects: list[str] = []
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        try:
            ts = int(parts[0])
        except ValueError:
            continue
        if ts >= int(since_ts):
            subjects.append(parts[1])
    return subjects


def head_sha(repo, _run: Optional[Callable] = None):
    """Full SHA of the current HEAD commit, or None if unresolvable."""
    cp = _dispatch(["git", "rev-parse", "HEAD"], repo, _run)
    if cp.returncode != 0:
        return None
    return cp.stdout.strip() or None


def is_repo(repo, _run: Optional[Callable] = None) -> bool:
    """True if ``repo`` is inside a git working tree.

    Probes ``git rev-parse --is-inside-work-tree``. A non-repo path returns
    False (never raises), so callers can branch on it without catching.
    """
    cp = _dispatch(["git", "rev-parse", "--is-inside-work-tree"], repo, _run)
    return cp.returncode == 0


def default_branch(repo, _run: Optional[Callable] = None) -> Optional[str]:
    """Name of the repo's default branch (e.g. 'main'/'master'), or None.

    Reads ``git symbolic-ref refs/remotes/origin/HEAD`` — the local ref git
    maintains pointing at the remote's default branch — and strips the
    ``refs/remotes/origin/`` prefix. Returns None when there is no remote, no
    origin, or no remote HEAD (a fresh repo or one that has never fetched), so
    callers report \"unknown\" rather than guessing a branch name.
    """
    cp = _dispatch(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], repo, _run)
    if cp.returncode != 0:
        return None
    ref = cp.stdout.strip()
    prefix = "refs/remotes/origin/"
    if not ref.startswith(prefix):
        return None
    return ref[len(prefix):] or None


def upstream_of(repo, branch, _run: Optional[Callable] = None) -> Optional[str]:
    """The configured upstream of ``branch`` (e.g. 'origin/main'), or None.

    Uses ``git rev-parse --abbrev-ref <branch>@{upstream}``. Returns None when
    the branch has no tracking branch configured (or the repo is not a repo),
    so the caller can report \"no upstream\" rather than inventing a guess.
    """
    cp = _dispatch(
        ["git", "rev-parse", "--abbrev-ref", "{}@{{upstream}}".format(branch)],
        repo,
        _run,
    )
    if cp.returncode != 0:
        return None
    return cp.stdout.strip() or None


def ahead_of_upstream(repo, branch, _run: Optional[Callable] = None) -> int:
    """Commits on ``branch`` reachable only from itself, not its upstream.

    The dry-run push signal: how many local commits are not yet on the remote
    tracking branch. 0 means nothing to push (or no upstream / non-repo — the
    safe no-data reading is \"nothing to push\").
    """
    upstream = upstream_of(repo, branch, _run)
    if not upstream:
        return 0
    return commits_ahead(repo, branch, base=upstream, _run=_run)


def behind_of_upstream(repo, branch, _run: Optional[Callable] = None) -> int:
    """Commits on ``branch``'s upstream that are not yet on ``branch``.

    The sync signal: how many commits the remote tracking branch has that the
    local branch is missing (i.e. how far local is BEHIND the remote). 0 means
    up to date (or no upstream / non-repo — the safe no-data reading is
    \"nothing to pull\"). Computed as ``git rev-list --count <branch>..<upstream>``
    (the mirror of ``commits_ahead``, which counts the other direction).
    """
    upstream = upstream_of(repo, branch, _run)
    if not upstream:
        return 0
    cp = _dispatch(
        ["git", "rev-list", "--count", "{}..{}".format(branch, upstream)], repo, _run
    )
    if cp.returncode != 0:
        return 0
    try:
        return int(cp.stdout.strip())
    except ValueError:
        return 0


def fetch(repo, _run: Optional[Callable] = None):
    """Run ``git fetch --prune origin``; return ``(ok: bool, detail: str)``.

    ``detail`` is a human line on failure (stderr or a generic message), empty
    on success.
    """
    cp = _dispatch(["git", "fetch", "--prune", "origin"], repo, _run)
    if cp.returncode != 0:
        return False, (cp.stderr.strip() or "fetch failed")
    return True, ""


def remote_url(repo, name="origin", _run: Optional[Callable] = None) -> Optional[str]:
    """Fetch URL of the named remote (e.g. 'origin'), or None.

    Runs ``git remote get-url <name>``. Returns None for a non-repo path or a
    remote that does not exist — the caller reports \"no remote\" rather than
    guessing. Used by the self-update command to learn the installed tool's own
    upstream without assuming a hardcoded URL.
    """
    cp = _dispatch(["git", "remote", "get-url", name], repo, _run)
    if cp.returncode != 0:
        return None
    return cp.stdout.strip() or None


def upstream_head_sha(url, _run: Optional[Callable] = None) -> Optional[str]:
    """The remote's default-branch HEAD commit sha, read WITHOUT cloning.

    Runs ``git ls-remote <url> HEAD`` — a pure network read that writes nothing
    to the local repo (no fetch into any worktree). Parses the first ``<sha>\tHEAD``
    line. Returns None when the remote is unreachable, is not a git repo at that
    URL, or has no HEAD. Used by the self-update command so a dry-run \"what
    would install\" comparison never mutates the installed checkout.
    """
    cp = _dispatch(["git", "ls-remote", url, "HEAD"], ".", _run)
    if cp.returncode != 0:
        return None
    for line in cp.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "HEAD":
            return parts[0]
    return None


def file_at_ref(repo, ref, path, _run: Optional[Callable] = None) -> Optional[str]:
    """Contents of ``path`` at ``ref`` in ``repo``, or None.

    Runs ``git show <ref>:<path>`` — reads a blob from an existing ref without
    checking it out. Returns None for a non-repo path, an unresolvable ref, or
    a path that does not exist at that ref. Used to read e.g. a project's
    ``VERSION`` at its upstream default branch to form a self-update plan.
    """
    cp = _dispatch(["git", "show", "{}:{}".format(ref, path)], repo, _run)
    if cp.returncode != 0:
        return None
    return cp.stdout


def pull_project(repo, _run: Optional[Callable] = None) -> dict:
    """Safely fast-forward ``repo``'s default branch; NEVER disturbs work.

    The pull counterpart of the session's \"don't disturb what the user is
    actively working on\" discipline. Decision order — each non-go is a SKIP
    with a clear reason, never a mutation:

      1. not a git repo          -> SKIP \"not a git repository\"
      2. no default branch       -> SKIP \"no default branch detected\"
      3. on a non-default branch -> SKIP \"on branch <x>, not <default>, leaving untouched\"
      4. dirty working tree      -> SKIP \"N uncommitted change(s), not touching\"
      5. ``git fetch --prune``   (failure -> SKIP \"fetch failed: <detail>\")
      6. ``git pull --ff-only``:
           - moved forward      -> PULLED \"pulled N commit(s)\"
           - unchanged          -> UP_TO_DATE \"already up to date\"
           - non-zero exit      -> SKIP \"diverged history; resolve by hand\"
                                    (a real merge is never auto-made)

    Returns a dict ``{\"status\", \"detail\", \"n\"}`` where ``status`` is one of
    ``pulled`` / ``up_to_date`` / ``skipped`` and ``n`` is the pulled commit
    count (0 otherwise). Never force-pulls, never stashes, never auto-commits,
    never raises.
    """
    if not is_repo(repo, _run):
        return {"status": "skipped", "detail": "not a git repository", "n": 0}

    default = default_branch(repo, _run)
    if not default:
        return {
            "status": "skipped",
            "detail": "no default branch detected (no origin/HEAD); nothing to pull",
            "n": 0,
        }

    branch = current_branch(repo, _run)
    if branch != default:
        return {
            "status": "skipped",
            "detail": f"on branch {branch}, not {default}, leaving untouched",
            "n": 0,
        }

    dirty = is_dirty(repo, _run)
    if dirty:
        return {
            "status": "skipped",
            "detail": f"{dirty} uncommitted change(s), not touching",
            "n": 0,
        }

    before = head_sha(repo, _run)
    ok, err = fetch(repo, _run)
    if not ok:
        return {"status": "skipped", "detail": f"fetch failed: {err}", "n": 0}

    cp = _dispatch(["git", "pull", "--ff-only"], repo, _run)
    if cp.returncode != 0:
        # Non-fast-forward means history diverged. Refuse to merge silently.
        return {
            "status": "skipped",
            "detail": "diverged history; pull --ff-only refused, resolve by hand",
            "n": 0,
        }

    after = head_sha(repo, _run)
    n = 0
    if before and after and before != after:
        cnt = _dispatch(["git", "rev-list", "--count", "{}..{}".format(before, after)], repo, _run)
        if cnt.returncode == 0:
            try:
                n = int(cnt.stdout.strip())
            except ValueError:
                n = 0
    if n:
        return {"status": "pulled", "detail": f"pulled {n} commit(s)", "n": n}
    return {"status": "up_to_date", "detail": "already up to date", "n": 0}


def push_project(repo, _run: Optional[Callable] = None) -> dict:
    """Safely push the CURRENT branch to its own upstream; NEVER force.

    The push counterpart to :func:`pull_project`'s discipline. It pushes only
    the branch actually checked out, to its own configured tracking branch,
    and never force-pushes, never guesses a branch name, and never pushes a
    different branch than what is checked out.

    1. not a git repo    -> SKIP \"not a git repository\"
    2. detached HEAD     -> SKIP \"detached HEAD; nothing to push\"
    3. no upstream       -> SKIP \"no upstream tracking branch for <x>\"
    4. nothing ahead     -> UP_TO_DATE \"nothing to push\"
    5. ``git push``:
         - ok            -> PUSHED
         - non-zero      -> SKIP \"push rejected (remote has diverged), resolve
                            by hand\" — never retried with --force

    Returns ``{\"status\", \"detail\", \"n\", \"branch\", \"upstream\"}`` where
    ``status`` is one of ``pushed`` / ``up_to_date`` / ``skipped``. The
    ``--apply`` gate is a CALLER concern (the command decides whether to
    invoke this); this function only performs the actual push. Use
    :func:`ahead_of_upstream` for the dry-run \"what WOULD be pushed\" report.
    """
    if not is_repo(repo, _run):
        return {
            "status": "skipped", "detail": "not a git repository",
            "n": 0, "branch": None, "upstream": None,
        }

    branch = current_branch(repo, _run)
    if not branch or branch == "HEAD":
        return {
            "status": "skipped", "detail": "detached HEAD; nothing to push",
            "n": 0, "branch": branch, "upstream": None,
        }

    upstream = upstream_of(repo, branch, _run)
    if not upstream:
        return {
            "status": "skipped",
            "detail": f"no upstream tracking branch for {branch}; nothing to push",
            "n": 0, "branch": branch, "upstream": None,
        }

    ahead = ahead_of_upstream(repo, branch, _run)
    if ahead == 0:
        return {
            "status": "up_to_date", "detail": "nothing to push",
            "n": 0, "branch": branch, "upstream": upstream,
        }

    cp = _dispatch(["git", "push"], repo, _run)
    if cp.returncode != 0:
        return {
            "status": "skipped",
            "detail": "push rejected (remote has diverged), resolve by hand",
            "n": 0, "branch": branch, "upstream": upstream,
        }
    return {
        "status": "pushed",
        "detail": f"pushed {ahead} commit(s) to {upstream}",
        "n": ahead, "branch": branch, "upstream": upstream,
    }
