"""Local merge queue for project PRs (MQ1).

The daemon owns a FIFO of green PRs per repo. One PR is processed per repo
per tick, in a dedicated git worktree under
``~/.hscc/merge-queue/<project>/`` — never the operator's checkout:

    fetch origin -> rebase PR head onto origin/<base> -> project gates ->
    push --force-with-lease -> gh pr merge --squash --delete-branch

``<base>`` is the repo's default branch (detected from ``origin/HEAD``, so a
``master`` repo works too) unless the registry declares
``merge_queue.base_branch``. A PR whose ``baseRefName`` is not that branch is
refused once with a clear reason, never retried. Cross-repository (fork) PRs
are refused the same way: the queue only pushes branches in the project's own
repo. Projects that share one repo path share one FIFO and one per-repo lock,
so two registry names cannot run two PRs against the same checkout at once.

A conflict or a red gate aborts cleanly, comments on the PR, hands the card
back to its worker as a resolve step, and drops the PR from the queue.
Everything the queue needs to survive a daemon restart is persisted in
``~/.hscc/merge-queue-state.json`` (atomic replace + fsync); a run that dies
mid-flight is recovered to the head of its project's queue on the next tick.

A PR that is not green yet sits in ``waiting``; EVERY tick re-checks that list
(one cheap ``gh pr view`` per entry) and returns the green ones to the FIFO,
whether or not the active queue is empty.  A wait-listed PR whose checks are
``red`` (or ``cancelled``) gets its branch updated onto the current base with
``gh pr update-branch``, which re-runs CI on a base that may since have been
fixed; the entry records the base sha it was refreshed onto, so this happens
at most once per base commit per PR and cannot loop.  After a push the new
head's CI is usually pending, so the entry keeps its in-flight claim at
``ci-pending`` and
is re-read each tick until green (merge that exact SHA), red (hand back) or the
CI budget expires (release to the rechecked wait list) — never re-gated on the
same pushed head. GitHub answers ``mergeable: UNKNOWN`` for a few seconds after
the base moves; that is re-polled, and a still-undecided entry keeps the head
slot instead of being parked in ``waiting``.

The queue is **on by default** for a project that has all three of: a git
checkout at its registry ``repo``, a ``github.com`` ``origin`` remote, and
declared gates (``merge_queue.gates``) or hscc's built-in gates::

    merge_queue:
      # Optional explicit override; omit to follow the default-on rule.
      # enabled: false
      gates: ["python3 scripts/address_guard.py --tracked", "bash scripts/run_tests.sh"]
      timeout_s: 3600

A project missing one of the three stays off and ``hscc merge-queue status``
prints why (``no repo`` / ``no GitHub remote`` / ``no gates``). The ``hscc``
project gets its own defaults (address guard + pytest legs, plus
``build_check.sh`` for whichever app tree the PR touched) when the registry
entry does not declare gates. An explicit ``enable``/``disable`` from the CLI
is stored in the ledger and always wins over the default.

The local pytest gate is deliberately narrow: for hscc's built-in defaults it
runs only the test legs whose directories the rebased diff touches, through
``HSCC_TEST_LEGS``. A diff that touches a shared harness file (the runner,
conftest, CI config, changelog tooling, requirements, the install payload) or
more than :data:`MAX_LOCAL_TEST_LEGS` legs falls back to the whole roster.
Nothing is lost by that: GitHub CI runs the full suite on the pushed head, and
:func:`_post_push_ready` refuses to merge unless the PR's checks are green on
that exact SHA.

This module is deliberately a plain git + gh implementation: no GitHub org,
no GitHub merge queue. Tests inject the ``git``/``gh`` runners and use a
throwaway bare origin + clone, so no test touches a real repository.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse

STREAM_NAME = "merge_queue"
STATE_VERSION = 1

DEFAULT_GATE_TIMEOUT_S = 3600
DEFAULT_PROCESS_TIMEOUT_S = 120
MAX_GATE_TAIL_LINES = 40
MAX_ERROR_ATTEMPTS = 3

#: Cap on the retry error kept in ``last_error``. The first line stays the
#: exception type + message (what a surface renders), followed by the tail of
#: the traceback; the cap keeps a pathological traceback out of the ledger.
MAX_LAST_ERROR_CHARS = 2000

#: How many verbatim failure lines a red handback carries ahead of the tail.
#:
#: The tail alone cannot name a failing test: `scripts/run_tests.sh` runs 12
#: legs, so a leg that goes red in the middle has ~60 lines of later legs and
#: trailing stderr printed after its own `FAILED path::test` line, and the
#: 40-line window is already full of them (measured on PR #673, 2026-11-23).
#: Reading the FULL output is therefore the fix, and this cap is what keeps
#: that safe: a branch whose fixture blew up can print hundreds of `FAILED`
#: rows, and a card comment is a triage surface, not a log file.
MAX_FAILURE_LINES = 25

#: Per-line cap. A pytest short-summary line ends with the failure reason, and
#: the reason is unbounded (`assert` can carry a whole dict). The part triage
#: reads — `FAILED path::test` — is at the front, so a long line is shortened
#: rather than dropped.
MAX_FAILURE_LINE_CHARS = 300

#: GitHub answers ``mergeable: "UNKNOWN"`` for every open PR for a few seconds
#: after the base branch moves (measured: all 13 open PRs, then MERGEABLE /
#: CONFLICTING on a second ``gh pr view``).  That is "not decided yet", not
#: "not ready": re-poll a bounded number of times before believing it.
#: Read at CALL time so a test can retune/zero the delay.
MERGEABLE_POLL_ATTEMPTS = 3
MERGEABLE_POLL_DELAY_S = 5.0

#: The in-flight stage of a PR whose head the queue pushed and whose CI has not
#: answered yet.  The claim is HELD here (no re-rebase, no re-gate) and
#: re-read on every tick until the checks are green or red.
CI_PENDING_STAGE = "ci-pending"

#: The in-flight stage of a claim the operator removed while its gate was
#: still running.  The claim is HELD here on purpose (see
#: :func:`remove_from_queue`): the work is live, so the ledger must not report
#: an idle repo.  The running processing settles it at its next checkpoint,
#: and the dead-run recovery drops it instead of requeuing it.
CANCELLING_STAGE = "cancelling"

#: Set on an in-flight claim that a ``remove`` deferred. Its presence — not the
#: stage — is what ``_cancel_reason`` and the recovery read.
CANCEL_KEY = "cancel_requested"

#: How long a pushed head may keep the claim while CI stays silent.  Past this
#: the entry goes back to the wait list (which every tick rechecks), never
#: terminal: a CI that never reported is not a verdict about the code.
CI_PENDING_TIMEOUT_S = 1800

#: How long an entry may stay in the wait list before the queue drops it.  The
#: wait list re-reads every waiting PR on every tick and, since cards 1 and 2,
#: even refreshes and hands one back — but until now it never gave up, so a PR
#: nobody was pushing any more waited on nobody forever.  A project overrides it
#: with ``waiting_max_age_s`` in its registry ``merge_queue`` block (seconds).
WAITING_MAX_AGE_S = 24 * 3600
WAITING_MAX_AGE_KEY = "waiting_max_age_s"

#: Task-policy class the gate asks for on macOS, and where that tool lives.
#:
#: The gate is a child of the `com.hermes.hscc_daemon` launchd job, whose plist
#: sets `ProcessType => Background`; every process under it inherits background
#: QoS and is scheduled behind ordinary interactive work.  Measured A/B on ONE
#: pytest leg in the SAME worktree: warm at default QoS 144.9 s vs warm at
#: background QoS 484.7 s = 3.34x (3.80x with the queue's exact no-bytecode +
#: background combination).  The whole 12-leg suite needs 1092.6 s unopposed —
#: 30% of DEFAULT_GATE_TIMEOUT_S — so it was the scheduling class exhausting the
#: budget, not the suite.  `utility` is the smallest elevation that gets a real
#: scheduler share without claiming foreground priority from the operator.
GATE_QOS_POLICY_CLASS = "utility"
TASKPOLICY_PATH = "/usr/sbin/taskpolicy"

#: Cache for :func:`_gate_qos_prefix`; the answer cannot change during a run.
_GATE_QOS_PREFIX_CACHE = None

#: How often a running gate re-stamps the health stream (see
#: :class:`_GateLiveness`). 30 s is 1/20 of verify's 600 s staleness budget for
#: this stream, so one lost heartbeat costs a stamp, never the verdict.
#: Read at CALL time (not bound into a default argument), so a test or an
#: operator can retune it by rebinding the module attribute.
LIVENESS_STAMP_S = 30

#: Every heartbeat thread is named with this prefix — the "no heartbeat
#: outlives the gate" contract is asserted by looking for live threads by name.
_LIVENESS_THREAD_PREFIX = "mq-gate-liveness"

#: The PR URL shape workers leave in a card comment (same shape Hermes'
#: respawn guard recognises). Owner/repo are captured too: auto-enqueue must
#: only accept a URL from the project's OWN origin remote.
PR_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/"
    r"(?P<number>[1-9][0-9]*)")

#: A project's ``origin`` remote, as github.com/<owner>/<repo> (https or ssh).
_REMOTE_SLUG_RE = re.compile(
    r"github\.com[/:](?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$")

#: A github.com remote URL: https(s), ssh, git, or scp-like (``git@github.com:``).
_GITHUB_REMOTE_RE = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/\s]+@)?github\.com[/:]",
    re.IGNORECASE)

#: The gate name in :data:`DEFAULT_HSCC_GATES` that leg selection rewrites.
RUN_TESTS_GATE = "bash scripts/run_tests.sh"

#: hscc's own default gates. The app build legs are appended only when the PR
#: actually touched that tree (see :func:`_resolve_gates`).
DEFAULT_HSCC_GATES = (
    "python3 scripts/address_guard.py --tracked",
    RUN_TESTS_GATE,
)
APP_BUILD_GATES = {
    "ios-app/": "bash ios-app/scripts/build_check.sh",
    "macos-app/": "bash macos-app/scripts/build_check.sh",
}

#: Changed paths that can alter how EVERY leg runs, so a local gate may not
#: narrow to the leg that owns the directory. A prefix without a trailing slash
#: is also a prefix match (``requirements`` covers ``requirements-ci.txt``).
SHARED_GATE_PATHS = (
    "scripts/run_tests.sh",
    "scripts/leg_heartbeat.sh",
    "conftest.py",
    "pytest.ini",
    "requirements",
    "install/",
    ".github/",
    ".githooks/",
    "scripts/changelog_fragments.py",
    "scripts/migrate_changelog_fragments.py",
)

#: Above this many legs a narrowed local gate stops being a saving, so the
#: queue runs the whole roster instead.
MAX_LOCAL_TEST_LEGS = 3

#: Changed paths whose only Python reader is scripts/tests. Mirrors the prose and
#: Apple-client arms of .github/scripts/leg_select.py::_RULES — the two tables
#: must stay in step, pinned by test_local_selection_agrees_with_leg_select.
NARROW_TO_SCRIPTS_LEG = ("changelog.d/", "docs/", "site/", "README",
                         "CLAUDE.md", "AGENTS.md", "macos-app/", "ios-app/")

#: The leg that parses and lints them (scripts/tests), per that same map.
SCRIPTS_LEG = "scripts"

#: A leg name that is safe to put in the gate's shell string verbatim
#: (``.github/scripts`` is a real roster entry, hence the leading-dot arm).
_LEG_NAME_RE = re.compile(r"^[A-Za-z0-9.][A-Za-z0-9._/-]*$")

_PR_JSON_FIELDS = (
    "number,state,isDraft,mergeable,headRefName,headRefOid,baseRefName,"
    "isCrossRepository,url,statusCheckRollup,mergeCommit,reviewDecision"
)

_lock = threading.RLock()


# ── paths ───────────────────────────────────────────────────────────────────

def _hscc_root():
    """The hscc state root: ``$HSCC_HOME``, then ``$HSCC_DIR``, then ~/.hscc.

    Resolved at CALL time (like :func:`hscc_daemon.alerts.hscc_root`), so a
    temp/sandboxed instance never reads or writes the live ledger or worktrees.
    """
    env = (os.environ.get("HSCC_HOME")
           or os.environ.get("HSCC_DIR") or "").strip()
    return os.path.expanduser(env or "~/.hscc")


def state_file():
    """Queue ledger path (atomic writer; survives daemon restarts)."""
    return os.path.join(_hscc_root(), "merge-queue-state.json")


def queue_root():
    """Root of the per-project queue worktrees."""
    return os.path.join(_hscc_root(), "merge-queue")


def worktree_path(project):
    """The queue worktree for ``project`` (never the operator's checkout)."""
    safe = str(project).strip()
    if not safe or safe in (".", "..") or os.sep in safe:
        raise ValueError(f"invalid project name for a worktree: {project!r}")
    return os.path.join(queue_root(), safe)


def repo_identity(repo):
    """Canonical identity of a repo path (symlinks and ``..`` resolved)."""
    return os.path.realpath(os.path.expanduser(str(repo or "")))


def repo_lock_path(repo, root=None):
    """Lock file shared by every project whose ``repo`` is the same path."""
    canonical = repo_identity(repo)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return os.path.join(root or _hscc_root(), "merge-queue-locks",
                        f"{digest}.lock")


@contextlib.contextmanager
def _repo_lock(repo, root=None):
    """Serialize processing for one repo across projects and processes."""
    path = repo_lock_path(repo, root=root)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    f = open(path, "a+")
    try:
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        f.close()


# ── state ───────────────────────────────────────────────────────────────────

def _empty_state():
    return {"version": STATE_VERSION, "projects": {}}


def _atomic_write_json(path, doc):
    """Write ``doc`` as JSON with fsync + atomic replace (mode 0600)."""
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".merge-queue-", suffix=".tmp",
                               dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        try:
            dir_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_state(path=None):
    """Load the persisted queue, returning a well-formed (possibly empty) doc."""
    path = path or state_file()
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return _empty_state()
    except (OSError, ValueError):
        return _empty_state()
    if not isinstance(doc, dict):
        return _empty_state()
    doc.setdefault("version", STATE_VERSION)
    projects = doc.get("projects")
    if not isinstance(projects, dict):
        doc["projects"] = {}
    return doc


def save_state(doc, path=None):
    """Persist the ledger, stamping the write time the surfaces read as freshness."""
    doc["generated_at"] = _now_iso()
    _atomic_write_json(path or state_file(), doc)


@contextlib.contextmanager
def _file_lock(path=None):
    """Cross-process lock so CLI writes cannot race the daemon tick."""
    path = path or os.path.join(_hscc_root(), "merge-queue.lock")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    f = open(path, "a+")
    try:
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        f.close()


def _project_state(doc, project):
    ps = doc.setdefault("projects", {}).setdefault(str(project), {})
    if not isinstance(ps, dict):
        # A hand-edited or half-written ledger can hold anything under a project
        # name, and every reader below calls `.get` on this value. Garbage is an
        # empty state, not a crash: one bad value must not wedge
        # `hscc merge-queue list`, `GET /v1/merge-queue` or a monitor.
        ps = doc["projects"][str(project)] = {}
    ps.setdefault("enabled", None)       # None = registry/default-on decides
    ps.setdefault("paused", False)
    ps.setdefault("queue", [])
    # A non-green head moves here so it cannot block the FIFO behind it. It is
    # retried only when the active queue is empty (or when auto-enqueue sees
    # the PR become green), never in the head slot.
    ps.setdefault("waiting", [])
    ps.setdefault("in_flight", None)
    ps.setdefault("last", None)
    return ps


def _queue(doc, project):
    return _project_state(doc, project)["queue"]


def _find_entry(queue, pr):
    for entry in queue:
        if int(entry.get("pr", 0)) == int(pr):
            return entry
    return None


def _entry_pr(entry):
    """The PR number of a queue/wait-list entry as an int, or None.

    Entries come back out of JSON, so a ``pr`` can be a string, a float or junk
    from an older ledger. Anything that compares against it goes through here
    instead of assuming ``int``.
    """
    if not isinstance(entry, dict):
        return None
    value = entry.get("pr")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def add_to_queue(project, pr, card=None, assignee=None, branch=None,
                 state=None, path=None):
    """Append a PR to ``project``'s FIFO (idempotent by PR number)."""
    with _lock, _file_lock():
        doc = state if state is not None else load_state(path)
        ps = _project_state(doc, project)
        q = ps["queue"]
        existing = _find_entry(q, pr)
        if existing is None:
            # A manual re-add of a waiting PR is an explicit retry: move the
            # entry back into the active FIFO instead of duplicating it.
            waiting = _find_entry(ps["waiting"], pr)
            if waiting is not None:
                ps["waiting"].remove(waiting)
                if branch and not waiting.get("branch"):
                    waiting["branch"] = branch
                q.append(waiting)
            else:
                q.append({
                    "pr": int(pr),
                    "card": card,
                    "assignee": assignee,
                    "branch": branch,
                    "enqueued_at": _now_iso(),
                    "attempts": 0,
                })
        elif branch and not existing.get("branch"):
            existing["branch"] = branch
        save_state(doc, path)
        return doc


def remove_from_queue(project, pr, state=None, path=None):
    """Remove a PR from ``project``'s queue, deferring an in-flight removal.

    The FIFO and the wait list lose the entry immediately. A claim this PR
    still holds is NOT nulled: its gate is live, and nulling the claim (as
    this function used to) makes the repo look idle while a gate still holds
    the worktree and the per-repo lock, leaves the post-gate path writing a
    ``last`` row for a PR that no longer exists anywhere in the ledger, and
    lets the next tick claim a second PR against the same checkout.

    Instead the claim is marked (:data:`CANCEL_KEY`,
    :data:`CANCELLING_STAGE`) and kept — a *deferred* removal, the same shape
    ``pause``/``disable`` already leave behind. The running processing sees
    the mark at its next :func:`_cancel_reason` checkpoint and settles it
    through :func:`_stop_cancelled`, which drops the entry; a daemon that
    dies first has the mark dropped, not requeued, by the tick's dead-run
    recovery. Either way the operator's removal is final; only its bookkeeping
    waits for the gate.
    """
    with _lock, _file_lock():
        doc = state if state is not None else load_state(path)
        ps = _project_state(doc, project)
        ps["queue"] = [e for e in ps["queue"]
                       if int(e.get("pr", 0)) != int(pr)]
        ps["waiting"] = [e for e in ps["waiting"]
                         if int(e.get("pr", 0)) != int(pr)]
        flight = ps.get("in_flight")
        if isinstance(flight, dict) and int(flight.get("pr", 0)) == int(pr):
            if not flight.get(CANCEL_KEY):
                flight[CANCEL_KEY] = _now_iso()
                flight["stage"] = CANCELLING_STAGE
        save_state(doc, path)
        return doc


def set_paused(project, paused, state=None, path=None):
    with _lock, _file_lock():
        doc = state if state is not None else load_state(path)
        _project_state(doc, project)["paused"] = bool(paused)
        save_state(doc, path)
        return doc


def set_enabled(project, enabled, state=None, path=None):
    with _lock, _file_lock():
        doc = state if state is not None else load_state(path)
        _project_state(doc, project)["enabled"] = bool(enabled)
        save_state(doc, path)
        return doc


# ── registry config ─────────────────────────────────────────────────────────

def _registry_module():
    from . import autodown
    return autodown._registry_module()


def load_projects(registry_path=None):
    """Registry projects as dicts, or ``[]`` when the registry is unavailable."""
    mod = _registry_module()
    if mod is None:
        return []
    path = registry_path or os.environ.get("FLIGHTDECK_REGISTRY")
    try:
        return [{
            "name": p.name,
            "repo": p.repo,
            "board": getattr(p, "board", None),
            "merge_queue": getattr(p, "merge_queue", None),
        } for p in mod.load_registry(path)]
    except Exception:
        return []


def _config_from_row(row):
    """The effective queue config from one ``load_projects`` row.

    ``enabled_declared`` records whether the registry itself set ``enabled``;
    only then is that an explicit override. Without it the project follows the
    default-on eligibility rule (:func:`default_reason`).
    """
    raw = row.get("merge_queue")
    cfg = dict(raw) if isinstance(raw, dict) else {}
    cfg["name"] = row["name"]
    cfg["repo"] = row["repo"]
    cfg["board"] = row.get("board")
    cfg["enabled_declared"] = "enabled" in cfg
    cfg["enabled"] = bool(cfg.get("enabled", False))
    cfg["default_reason"] = default_reason(cfg)
    cfg["default_enabled"] = cfg["default_reason"] is None
    return cfg


def _git_checkout(repo):
    """The expanded ``repo`` path when it is a git work-tree root, else None.

    Pure filesystem check (``.git`` is a directory in a clone and a file in a
    linked worktree/submodule), so a non-repo project costs no subprocess.
    """
    path = os.path.expanduser(str(repo or "").strip())
    if not path or not os.path.isdir(path):
        return None
    if not os.path.exists(os.path.join(path, ".git")):
        return None
    return path


def _repo_origin(repo, git_runner=None):
    """``(is_git_root, origin_url)`` for ``repo``; reads local config only.

    ``is_git_root`` is false when the path is missing or is not the root of a
    git work tree (a subdirectory of some other checkout is not this project's
    repo). The origin URL is the raw configured value, so a ``url.*.insteadOf``
    mirror does not hide a GitHub origin.
    """
    path = _git_checkout(repo)
    if path is None:
        return False, None
    runner = git_runner or _default_runner
    try:
        top = _git(["rev-parse", "--show-toplevel"], path, runner)
        if top is None or top.returncode != 0:
            return False, None
        root = (top.stdout or "").strip()
        if not root or os.path.realpath(root) != os.path.realpath(path):
            return False, None
        proc = _git(["config", "--get", "remote.origin.url"], path, runner)
    except Exception:
        return False, None
    if proc is None or proc.returncode != 0:
        return True, None
    return True, ((proc.stdout or "").strip() or None)


def _github_remote(url):
    """True when ``url`` is a github.com remote (https, ssh, git, git@)."""
    return bool(_GITHUB_REMOTE_RE.match(str(url or "").strip()))


def _project_has_gates(cfg):
    """Declared gates, or hscc's built-in gates, make a project runnable.

    An explicit ``gates: []`` means no gates for every project (including
    hscc): the built-in list applies only when the key is absent, matching
    :func:`_resolve_gates`.
    """
    if not isinstance(cfg, dict):
        return False
    gates = cfg.get("gates")
    if gates is not None:
        return isinstance(gates, (list, tuple)) and any(
            str(g).strip() for g in gates)
    return str(cfg.get("name") or "") == "hscc"


def default_reason(cfg, *, git_runner=None):
    """Why default-on does not apply, or None when the project is eligible.

    Default-on requires a git checkout at the registry ``repo``, a
    ``github.com`` ``origin`` remote, and gates (``merge_queue.gates`` or
    hscc's built-in gates). The three reason strings are printed by
    ``hscc merge-queue status``.
    """
    if not isinstance(cfg, dict):
        return "no repo"
    repo = str(cfg.get("repo") or "").strip()
    if _git_checkout(repo) is None:
        return "no repo"
    if not _project_has_gates(cfg):
        return "no gates"
    is_repo, origin = _repo_origin(repo, git_runner)
    if not is_repo:
        return "no repo"
    if not _github_remote(origin):
        return "no GitHub remote"
    return None


def default_enabled(cfg, *, git_runner=None):
    """True when the project meets the default-on conditions."""
    return default_reason(cfg, git_runner=git_runner) is None


def enabled_source(cfg, ps):
    """Where the on/off decision came from: ``cli``, ``registry`` or ``default``."""
    if (ps or {}).get("enabled") is not None:
        return "cli"
    if (cfg or {}).get("enabled_declared"):
        return "registry"
    return "default"


def effective_enabled(cfg, ps):
    """The project's on/off state.

    An explicit CLI override in the ledger wins over everything (including a
    ``disable`` that must survive restarts); a registry ``enabled`` is the next
    explicit answer; otherwise the default-on eligibility rule decides.
    """
    if not cfg:
        return False
    override = (ps or {}).get("enabled")
    if override is not None:
        return bool(override)
    if cfg.get("enabled_declared"):
        return bool(cfg.get("enabled"))
    if "default_enabled" in cfg:
        return bool(cfg["default_enabled"])
    return default_enabled(cfg)


def project_config(project, registry_path=None):
    """The effective queue config for ``project`` (registry + defaults)."""
    for row in load_projects(registry_path):
        if row["name"] == project:
            return _config_from_row(row)
    return None


# ── read-only rows (CLI monitor + GET /v1/merge-queue) ─────────────────────

#: States a row can carry. ``queued``/``merged``/``resolve``/``failed`` are
#: ledger outcomes; ``rebasing``/``gating``/``merging``/``ci-pending``/
#: ``timeout`` are the live stages the daemon stamps on its in-flight claim as
#: it moves through the queue. ``timeout`` is a stage, never an outcome state: a
#: gate that ran out of budget re-enters the wait list, so its row renders
#: ``waiting`` (see ``_OUTCOME_STATES``), and nothing that timed out may render
#: ``resolve``.
MERGE_QUEUE_STATES = (
    "queued", "waiting", "rebasing", "gating", "merging", "ci-pending",
    "resolve", "merged", "failed", "timeout",
)

_OUTCOME_STATES = {
    "merged": "merged",
    "already-merged": "merged",
    "conflict": "resolve",
    "red-gate": "resolve",
    # CI failed on the head the queue itself pushed: the local gates passed,
    # so this is a real verdict about the code and needs the worker.
    "red-ci": "resolve",
    "refused": "failed",
    "error": "failed",
    # Budget exhaustion is not a verdict about the code, so it must not render
    # as a failure the author is sent to fix: the entry is back in the wait
    # list, and the read model says so.
    "gate-timeout": "waiting",
    # Same for a pushed head whose CI never answered: release to waiting (the
    # wait list is rechecked every tick), not a resolve the worker must chase.
    "ci-timeout": "waiting",
    # ── the wait-list sweep (card 3) ──
    # It landed, just not through us: the same row an in-flight PR reports when
    # it is found already merged.
    "merged-elsewhere": "merged",
    # The queue gave up on it. A budget, not a verdict on the code, so it must
    # not render `resolve` (nothing is being asked of the author but a re-add) —
    # and not `timeout` either, which this module reserves for the live stage
    # stamped on an in-flight claim (see MERGE_QUEUE_STATES).
    "expired": "failed",
    # The author closed it: nothing landed and nothing is left to resolve.
    "closed": "failed",
}


def outcome_state(result):
    """Map a recorded ledger result to a row state in ``MERGE_QUEUE_STATES``."""
    return _OUTCOME_STATES.get(str(result or ""), "failed")


def _epoch(iso_value):
    if not iso_value:
        return None
    try:
        stamp = datetime.datetime.fromisoformat(str(iso_value))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp.timestamp()


def _age_seconds(iso_value, now_s):
    stamp = _epoch(iso_value)
    if stamp is None:
        return None
    return max(0, int(now_s - stamp))


def _row(project, state, *, entry=None, last=None, position=None, now_s):
    """One merge-queue row: the table's columns, plus the fields JSON needs."""
    entry = entry if isinstance(entry, dict) else {}
    last = last if isinstance(last, dict) else None
    if last is not None:
        stamp = last.get("at")
        last_result = last.get("result")
        detail = last.get("error") or last.get("gate")
        conflicted = last.get("conflicted")
        if conflicted:
            detail = ", ".join(str(c) for c in conflicted)
    else:
        stamp = entry.get("enqueued_at")
        last_result = "retry" if entry.get("last_error") else None
        detail = entry.get("last_error")
    return {
        "project": project,
        "position": position,
        "pr": entry.get("pr") if entry.get("pr") is not None
              else (last or {}).get("pr"),
        "card": entry.get("card") or (last or {}).get("card"),
        "branch": entry.get("branch") or (last or {}).get("branch"),
        "state": state,
        "age_s": _age_seconds(stamp, now_s),
        "enqueued_at": stamp,
        "last_result": last_result,
        "detail": detail,
        "assignee": entry.get("assignee"),
        "attempts": int(entry.get("attempts") or 0),
        "is_last": last is not None,
    }


def view(*, registry_path=None, state_path=None, board=None, now=None):
    """The merge queue's rows — ONE read for the CLI monitor and the API route.

    Returns every known project in registry order (then state-only projects)
    with the queue entries at their FIFO positions, the in-flight claim in its
    recorded stage, and the last terminal outcome when it is not already one of
    those rows. ``board`` narrows to the project whose registry ``board``
    matches, mirroring ``hscc kanban monitor --board``.
    """
    doc = load_state(state_path)
    project_rows = load_projects(registry_path)
    configs = {row["name"]: _config_from_row(row) for row in project_rows}
    names = [row["name"] for row in project_rows]
    names.extend(name for name in doc.get("projects", {})
                 if name not in names)
    names = list(dict.fromkeys(names))
    now_s = float(time.time() if now is None else now)

    out = {}
    all_rows = []
    for name in names:
        ps = _project_state(doc, name)
        cfg = configs.get(name)
        if board is not None and (cfg or {}).get("board") != board:
            continue
        enabled = effective_enabled(cfg, ps)
        last = ps.get("last") if isinstance(ps.get("last"), dict) else None
        rows = []
        shown = set()
        queue = list(ps.get("queue") or [])
        for position, entry in enumerate(queue, 1):
            rows.append(_row(name, "queued", entry=entry, position=position,
                             now_s=now_s))
            if entry.get("pr") is not None:
                shown.add(int(entry["pr"]))
        waiting = list(ps.get("waiting") or [])
        for entry in waiting:
            rows.append(_row(name, "waiting", entry=entry, now_s=now_s))
            if entry.get("pr") is not None:
                shown.add(int(entry["pr"]))
        flight = ps.get("in_flight")
        if isinstance(flight, dict) and flight:
            rows.append(_row(name, flight.get("stage") or "rebasing",
                             entry=flight, now_s=now_s))
            if flight.get("pr") is not None:
                shown.add(int(flight["pr"]))
        if last and last.get("pr") is not None:
            pr = int(last["pr"])
            if pr in shown:
                for row in rows:
                    if row.get("pr") is not None and int(row["pr"]) == pr:
                        row["last_result"] = last.get("result")
                        row["detail"] = (last.get("error") or last.get("gate")
                                         or row.get("detail"))
            else:
                rows.append(_row(
                    name, outcome_state(last.get("result")),
                    entry={"pr": pr, "card": last.get("card"),
                           "branch": last.get("branch")},
                    last=last, now_s=now_s))
        out[name] = {
            "enabled": enabled,
            "enabled_source": enabled_source(cfg, ps),
            "default_enabled": bool((cfg or {}).get("default_enabled")),
            "default_reason": (cfg or {}).get("default_reason"),
            "paused": bool(ps.get("paused")),
            "repo": (cfg or {}).get("repo"),
            "queue_len": len(queue),
            "waiting_len": len(waiting),
            "rows": rows,
            "last": last,
        }
        all_rows.extend(rows)
    return {
        "ok": True,
        # The ledger's write time, NOT this read: two view() calls with no tick
        # or state write between them must report the same timestamp, so a
        # widget cannot present stale rows as freshly generated.
        "generated_at": doc.get("generated_at"),
        "projects": out,
        "rows": all_rows,
        "count": len(all_rows),
        "queued": sum(1 for row in all_rows if row["state"] == "queued"),
        "waiting": sum(1 for row in all_rows if row["state"] == "waiting"),
    }


def card_states(*, registry_path=None, state_path=None, now=None):
    """``{card_id: row}`` for the cards currently visible in the queue view."""
    states = {}
    for row in view(registry_path=registry_path, state_path=state_path,
                    now=now)["rows"]:
        card = row.get("card")
        if card:
            states[str(card)] = row
    return states


def default_branch(repo, git_runner=None, *, probe_remote=True):
    """The repo's default branch, or None when it cannot be determined.

    ``origin/HEAD`` is authoritative and free; when the clone does not have it,
    an existing ``origin/main``/``origin/master`` is accepted. Only then (and
    only when ``probe_remote`` is true) does this ask the remote, so a sandboxed
    test repo with no refs never touches the network.
    """
    runner = git_runner or _default_runner
    try:
        proc = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                    repo, runner)
    except Exception:
        proc = None
    if proc is not None and proc.returncode == 0:
        ref = (proc.stdout or "").strip()
        if ref.startswith("origin/") and len(ref) > len("origin/"):
            return ref.split("/", 1)[1]
    for name in ("main", "master"):
        try:
            proc = _git(["rev-parse", "--verify", "--quiet",
                         f"refs/remotes/origin/{name}"], repo, runner)
        except Exception:
            proc = None
        if proc is not None and proc.returncode == 0:
            return name
    if not probe_remote:
        return None
    try:
        proc = _git(["ls-remote", "--symref", "origin", "HEAD"], repo, runner)
    except Exception:
        return None
    if proc is None or proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("ref:"):
            ref = line.split()[1] if len(line.split()) > 1 else ""
            if ref.startswith("refs/heads/"):
                return ref[len("refs/heads/"):]
    return None


def expected_base(cfg, git_runner=None, *, probe_remote=True):
    """The branch this queue rebases onto: configured, else repo default."""
    declared = str((cfg or {}).get("base_branch") or "").strip()
    if declared:
        return declared
    return default_branch((cfg or {}).get("repo"), git_runner,
                          probe_remote=probe_remote)


def refusal_reason(info, base):
    """Why this PR must not be processed here, or None when it may be.

    Base is checked against the queue's branch (repo default unless
    ``merge_queue.base_branch``). A cross-repository PR is unsupported: its
    head branch does not exist in the project's own ``origin``.
    """
    if not isinstance(info, dict):
        return None
    if info.get("isCrossRepository"):
        return ("cross-repository (fork) PR: the merge queue only pushes "
                "branches in the project's own repo; open the PR from a "
                "branch pushed to this repo instead")
    pr_base = str(info.get("baseRefName") or "").strip()
    if base and pr_base and pr_base != base:
        return (f"PR targets {pr_base!r}, not this queue's base {base!r}; "
                "retarget it (or declare merge_queue.base_branch)")
    return None


def _test_leg_roster(worktree):
    """The runner's ``DIRS`` roster from the queue worktree, or None.

    Read from the worktree, not this module's install tree: the roster that
    matters is the one the PR's head would land. An unreadable or malformed
    roster means the caller must fall back to the full suite.
    """
    path = os.path.join(worktree, "scripts", "run_tests.sh")
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    match = re.search(r"^DIRS=\((?P<body>[^)]*)\)", text, re.MULTILINE)
    if match is None:
        return None
    legs = match.group("body").split()
    return legs or None


def _local_test_legs(changed, roster):
    """Legs a narrowed local gate may run, or ``None`` for the full suite.

    A path under a leg's own directory selects that leg. Repo prose and the
    Apple client trees select :data:`SCRIPTS_LEG`, mirroring
    ``.github/scripts/leg_select.py`` — ``scripts/tests`` is their only Python
    reader (it parses ``changelog.d/`` and ``CHANGELOG.md``, lints the README
    and docs index, and reads/executes the app publishers). A shared harness
    path, an unreadable/odd roster, a roster without :data:`SCRIPTS_LEG` for
    such a path, an empty diff, a path no rule claims, or more than
    :data:`MAX_LOCAL_TEST_LEGS` distinct legs widen to ``None`` (full suite):
    under-selection is the failure mode this gate must not have.
    """
    if not roster or any(not _LEG_NAME_RE.match(leg) for leg in roster):
        return None
    paths = [p.strip().replace("\\", "/") for p in changed if p.strip()]
    if not paths:
        return None
    for path in paths:
        if any(path == shared or path.startswith(shared)
               for shared in SHARED_GATE_PATHS):
            return None
    chosen = set()
    for path in paths:
        claimed = {leg for leg in roster
                   if path.startswith(leg.rstrip("/") + "/")}
        # Root-anchored like leg_select: hscc-api/docs/x.md is hscc-api's.
        if any(path.startswith(prefix) for prefix in NARROW_TO_SCRIPTS_LEG):
            if SCRIPTS_LEG not in roster:
                return None
            claimed.add(SCRIPTS_LEG)
        if not claimed:
            return None
        chosen |= claimed
    if not chosen or len(chosen) > MAX_LOCAL_TEST_LEGS:
        return None
    return [leg for leg in roster if leg in chosen]


def _selected_leg_gate(legs):
    """The runner command with ``HSCC_TEST_LEGS`` narrowed to ``legs``."""
    return 'HSCC_TEST_LEGS="%s" %s' % (" ".join(legs), RUN_TESTS_GATE)


def _resolve_gates(cfg, worktree, git_runner, base="main"):
    """Gate command list for this PR, including hscc's conditional app builds.

    Declared gates are used verbatim. For hscc's built-in defaults the pytest
    gate is narrowed to the legs the rebased diff touches (see
    :func:`_local_test_legs`); app build gates still come from that same diff.
    The full suite is not lost with the narrowed local gate: GitHub CI runs it
    on the pushed head and :func:`_post_push_ready` refuses to merge until that
    exact SHA is green.
    """
    declared = cfg.get("gates")
    if declared is not None:
        return [str(g) for g in declared]
    if cfg.get("name") != "hscc":
        return []
    gates = list(DEFAULT_HSCC_GATES)
    changed = _changed_files(worktree, git_runner, base=base)
    legs = _local_test_legs(changed, _test_leg_roster(worktree))
    if legs:
        gates[gates.index(RUN_TESTS_GATE)] = _selected_leg_gate(legs)
    for prefix, cmd in APP_BUILD_GATES.items():
        if any(f.startswith(prefix) for f in changed):
            gates.append(cmd)
    return gates


def _changed_files(worktree, git_runner, base="main"):
    proc = _git(["diff", "--name-only", f"origin/{base}...HEAD"], worktree,
                git_runner, timeout=DEFAULT_PROCESS_TIMEOUT_S)
    if proc is None or proc.returncode != 0:
        return []
    return [line.strip() for line in (proc.stdout or "").splitlines()
            if line.strip()]


# ── process helpers ─────────────────────────────────────────────────────────

def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _default_runner(argv, **kwargs):
    # _git/_gh already pass capture_output/text/timeout; passing them again
    # here duplicated the keywords and raised TypeError on every production
    # call (the tests all injected a runner, which hid it).
    return subprocess.run(argv, **kwargs)


def _git(args, cwd, runner, timeout=DEFAULT_PROCESS_TIMEOUT_S):
    return runner(["git", *args], cwd=cwd, capture_output=True, text=True,
                  timeout=timeout)


def _gh(args, cwd, runner, timeout=DEFAULT_PROCESS_TIMEOUT_S):
    return runner(["gh", *args], cwd=cwd, capture_output=True, text=True,
                  timeout=timeout)


def _tail(text, lines=MAX_GATE_TAIL_LINES):
    rows = (text or "").strip().splitlines()
    return "\n".join(rows[-lines:])


#: A gate's own words for "this went red", matched at the START of a line so a
#: progress line, a dot line or prose that merely mentions a failure cannot join
#: the digest.  Two buckets, because they answer two different questions:
#:
#: * `_TEST_FAILURE_RE` — `FAILED <path>::<test> - <reason>` / `ERROR ...`:
#:   pytest's short test summary, printed by `-q` (`run_tests.sh` adds `-rs` for
#:   the skips). This is the name of the thing that broke.
#: * `_LEG_FAILURE_RES` — `  ✗ <leg> (pytest exit N)` and `N failed, M passed in
#:   Xs`: `scripts/run_tests.sh`'s own summary row and pytest's per-leg count
#:   line. Together they say WHICH leg went red and HOW MANY it lost.
_TEST_FAILURE_RE = re.compile(r"^(?:FAILED|ERROR)\s")
_LEG_FAILURE_RES = (
    re.compile(r"^✗\s"),
    re.compile(r"^\d+ failed\b"),
)


def _failure_lines(text, *, cap=MAX_FAILURE_LINES):
    """The failure lines of a gate's output, VERBATIM, capped.

    Read from the FULL text, never from the tail.  That is the whole point:
    `_tail` keeps the last 40 lines of one 12-leg run, and a leg failing in the
    middle has the legs after it — plus the stderr the API appends — sitting on
    top of its `FAILED` line.  PR #673's handback measured as a run whose only
    red evidence was `✗ hscc-api (pytest exit 1)`: enough to know the suite was
    red, not enough to say whether the test or the branch broke.

    Verbatim is also the point.  A paraphrase ("hscc-api failed") is what the
    summary row already said and cannot be trusted for a flake call; the exact
    `path::test - reason` line is what a re-queue-vs-fix decision is made from,
    and it is what the author can grep their own tree for.

    Ordering is by kind, not by position, and that is deliberate: the leg rows
    lead, then the failing tests in the order pytest printed them.  A branch
    with 40 failures is capped, and the rows printed last (the summary block)
    would otherwise be the cap's first casualty — losing exactly the line that
    names the leg.  Leading indentation is dropped: this is a list of failure
    lines, not a transcript.

    De-duplicated, capped at ``cap`` distinct lines, each line capped at
    :data:`MAX_FAILURE_LINE_CHARS`; when anything is hidden the last entry says
    how many, so a short digest never looks like a complete one.
    """
    legs, tests, seen = [], [], set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if _TEST_FAILURE_RE.match(line):
            bucket = tests
        elif any(p.match(line) for p in _LEG_FAILURE_RES):
            bucket = legs
        else:
            continue
        if len(line) > MAX_FAILURE_LINE_CHARS:
            line = line[:MAX_FAILURE_LINE_CHARS - 1].rstrip() + "…"
        if line in seen:
            continue
        seen.add(line)
        bucket.append(line)
    legs = legs[:cap]
    room = cap - len(legs)
    kept = legs + tests[:room]
    if len(tests) > room:
        kept.append(f"… {len(tests) - room} more failure line(s) not shown …")
    return kept


def _gate_report(text, *, note=None):
    """What a gate that did not pass hands back: the failure lines, then the tail.

    The digest LEADS because the reader of a card comment reads from the top,
    and in a 12-leg run the tail is the least informative part of the output: it
    is mostly the legs that passed after the failure.  The tail itself is still
    the plain :func:`_tail` of the full text — the digest is added to it, never
    substituted for it — so nothing that used to be visible went away.

    ``note`` is appended after the tail, which is where the timeout path's
    `timeout after {N}s` line has always been read.
    """
    tail = _tail(text)
    if note:
        tail = f"{tail}\n{note}" if tail else note
    lines = _failure_lines(text)
    if not lines:
        return tail
    header = "━━━ Failed lines from the full gate output (verbatim) ━━━"
    return "\n".join([header, *lines, "", tail])


def _truthy_check(entry):
    """True/False/None for one statusCheckRollup entry (None = still pending)."""
    if not isinstance(entry, dict):
        return None
    # CheckRun shape
    if "conclusion" in entry or entry.get("__typename") == "CheckRun":
        conclusion = (entry.get("conclusion") or "").upper()
        if not conclusion:
            status = (entry.get("status") or "").upper()
            return None if status and status != "COMPLETED" else None
        return conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED")
    # StatusContext shape
    state = (entry.get("state") or "").upper()
    if state:
        return state in ("SUCCESS", "NEUTRAL", "SKIPPED")
    return None


def _checks_green(rollup, *, required_checks=False):
    """True when every reported check is green.

    An empty rollup is only green when the repo has NO required checks
    configured (``required_checks is False``). ``None`` means "could not
    determine", which is not positive evidence, so an empty rollup stays not
    green — GitHub may still be waiting for checks the queue never saw.
    """
    if not rollup:
        return required_checks is False
    if not isinstance(rollup, list):
        return False
    for entry in rollup:
        verdict = _truthy_check(entry)
        if verdict is not True:
            return False
    return True


def _checks_verdict(info, *, required_checks=False):
    """``"green"``, ``"pending"`` or ``"red"`` for a PR's reported checks.

    The tri-state the queue needs AFTER it pushed: "not green yet" conflates a
    check still running (hold the claim, look again) with a check that failed
    (hand back to the worker). An empty rollup is only green when GitHub
    positively said the base requires no checks, mirroring :func:`_checks_green`.
    """
    rollup = (info or {}).get("statusCheckRollup")
    if not rollup:
        return "green" if required_checks is False else "pending"
    if not isinstance(rollup, list):
        return "pending"
    pending = False
    for entry in rollup:
        verdict = _truthy_check(entry)
        if verdict is False:
            return "red"
        if verdict is not True:
            pending = True
    return "pending" if pending else "green"


def _mergeable_unknown(info):
    """True when GitHub has not decided ``mergeable`` yet (absent or UNKNOWN)."""
    return str((info or {}).get("mergeable") or "").upper() in ("", "UNKNOWN")


def _pr_ready(info, *, required_checks=False):
    """Open, non-draft, mergeable, not changes-requested, checks green."""
    if not isinstance(info, dict):
        return False
    return (
        str(info.get("state", "")).upper() == "OPEN"
        and not bool(info.get("isDraft"))
        and str(info.get("mergeable", "")).upper() == "MERGEABLE"
        and str(info.get("reviewDecision") or "").upper()
        != "CHANGES_REQUESTED"
        and _checks_green(info.get("statusCheckRollup"),
                          required_checks=required_checks)
    )


def pr_view(repo, pr, runner=None):
    """``gh pr view`` JSON for one PR, or None when it cannot be read."""
    runner = runner or _default_runner
    try:
        proc = _gh(["pr", "view", str(pr), "--json", _PR_JSON_FIELDS],
                   repo, runner)
    except Exception:
        return None
    if proc is None or proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except (TypeError, ValueError):
        return None


def pr_view_settled(repo, pr, runner=None):
    """``gh pr view`` that gives GitHub a bounded moment to compute mergeability.

    Right after the base moves, GitHub reports ``mergeable: UNKNOWN`` for every
    open PR until it recomputes. Treating that as "not ready" parked the PR in
    the wait list, which only drained when the queue was empty — a livelock.
    Re-poll a few times with a short backoff; a still-UNKNOWN answer is returned
    as-is and the caller leaves the entry at the head of the queue.
    """
    attempts = max(1, int(MERGEABLE_POLL_ATTEMPTS))
    info = pr_view(repo, pr, runner=runner)
    for attempt in range(1, attempts):
        if info is None or not _mergeable_unknown(info):
            break
        delay = max(0.0, float(MERGEABLE_POLL_DELAY_S))
        if delay:
            time.sleep(delay)
        info = pr_view(repo, pr, runner=runner)
    return info


# ── git worktree / rebase ───────────────────────────────────────────────────

def ensure_worktree(cfg, root=None, git_runner=None, base="main"):
    """Ensure the project's queue worktree exists at ``queue_root()/<name>``.

    ``origin`` is fetched on EVERY call, not just when the worktree is first
    created: a merged PR advances the base branch on GitHub, and the next entry
    must rebase and gate on that new base (the worktree's remote-tracking ref
    would otherwise stay at the commit it was created from).
    """
    git_runner = git_runner or _default_runner
    repo = cfg["repo"]
    path = (os.path.join(root, cfg["name"]) if root
            else worktree_path(cfg["name"]))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cwd = path if (os.path.isdir(os.path.join(path, ".git"))
                   or os.path.isfile(os.path.join(path, ".git"))) else repo
    fetch = _git(["fetch", "origin", "--prune"], cwd, git_runner)
    if fetch is None or fetch.returncode != 0:
        raise RuntimeError(
            f"could not fetch origin in {cwd}: "
            f"{_tail(fetch.stderr if fetch else 'git not runnable', 10)}")
    if cwd == path:
        return path

    proc = _git(["worktree", "add", "--detach", path, f"origin/{base}"],
                repo, git_runner)
    if proc is None or proc.returncode != 0:
        raise RuntimeError(
            f"could not create queue worktree {path}: "
            f"{_tail(proc.stderr if proc else 'git not runnable', 10)}")
    return path


def _prepare_head(worktree, info, git_runner):
    """Fetch + detach onto the PR head. Returns an error string or None."""
    branch = info.get("headRefName")
    if not branch:
        return "gh pr view returned no headRefName"
    fetch = _git(["fetch", "origin", branch], worktree, git_runner)
    if fetch is None or fetch.returncode != 0:
        return f"git fetch origin {branch} failed: {_tail(fetch.stderr if fetch else '', 10)}"
    checkout = _git(["checkout", "--detach", "--force", "FETCH_HEAD"],
                    worktree, git_runner)
    if checkout is None or checkout.returncode != 0:
        return f"git checkout FETCH_HEAD failed: {_tail(checkout.stderr if checkout else '', 10)}"
    return None


def _conflicted_files(worktree, git_runner):
    proc = _git(["diff", "--name-only", "--diff-filter=U"], worktree,
                git_runner)
    if proc is None or proc.returncode != 0:
        return []
    return [line.strip() for line in (proc.stdout or "").splitlines()
            if line.strip()]


def _rebase_onto(worktree, base, git_runner):
    """Real ``git rebase origin/<base>``; returns (ok, conflicted, output)."""
    proc = _git(["rebase", f"origin/{base}"], worktree, git_runner,
                timeout=DEFAULT_GATE_TIMEOUT_S)
    if proc is not None and proc.returncode == 0:
        return True, [], ""
    conflicted = _conflicted_files(worktree, git_runner)
    _git(["rebase", "--abort"], worktree, git_runner)
    output = (proc.stdout or "") + (proc.stderr or "") if proc else ""
    return False, conflicted, _tail(output, 20)


# ── health-stream liveness ────────────────────────────────────────────────
# ``check_merge_queue`` is a 60 s daemon stream (daemon_ops.PERIODIC_INTERVALS)
# and ``hscc verify`` calls ``state/merge_queue.json`` stale after
# ``max(600, 2*60+90)`` = 600 s.  A tick that holds a claim can spend the WHOLE
# gate budget inside a single leg (:data:`DEFAULT_GATE_TIMEOUT_S` is 3600 s), so
# a stamp written only once ``tick()`` returns goes RED on a runner that is
# demonstrably alive — the "cries wolf on a healthy thing" anti-pattern
# ``verify.py`` documents.  These writers stamp from INSIDE the work instead:
# ``_stream_stamp`` at every stage transition, :class:`_GateLiveness` around
# each gate leg.
#
# The fix is deliberately on the STAMPING side, never the judging side: verify's
# budget is untouched and there is no verify-side in-flight exemption, so a
# heartbeat can only ever tell the truth.  It proves "the daemon process is
# alive AND a gate subprocess is running".  It cannot mask a dead process (a
# daemon thread dies with its process) and cannot mask a tick deadlocked
# OUTSIDE a gate (nothing heartbeats there), so both still go RED within the
# same 600 s.

def _stream_stamp(*, project, pr, stage, gate=None, elapsed_s=None):
    """Mirror one in-flight queue moment onto the health stream. Never raises.

    Two different files, two different jobs: the LEDGER
    (``~/.hscc/merge-queue-state.json``) is the queue's durable truth across
    restarts, the STREAM (``~/.hscc/state/merge_queue.json``) is what
    ``hscc verify`` reads for daemon liveness.  This touches only the stream —
    never queue state — and carries the same ``in_flight`` row the ledger
    holds, so the health stamp and the queue can never disagree about what the
    in-flight PR is doing.

    ``ok=True`` is honest here: reaching a stage transition *is* a healthy
    queue.  ``check_merge_queue`` still overwrites the file with the tick's
    real verdict (ok=False included) the moment the tick returns, and a stamp
    whose writer died stops advancing and goes stale like any other.
    """
    from .state import write_state
    try:
        return write_state(STREAM_NAME, {
            "ok": True,
            "in_flight": {
                "project": project,
                "pr": pr,
                "stage": stage,
                "gate": gate,
                "elapsed_s": elapsed_s,
            },
        })
    except Exception:
        # check_merge_queue has always swallowed a failed stamp; a mirror
        # written from inside the run must be at least as forgiving — a
        # health-reporting write must never fail the work it reports on.
        return None


class _GateLiveness:
    """Context manager: re-stamp the stream every ``LIVENESS_STAMP_S`` while
    one gate leg runs.

    Use it as ``with _GateLiveness(project, pr, command): _run_gate(...)`` —
    started immediately before the leg and stopped in a ``finally`` immediately
    after it, so a heartbeat can only ever be running while a gate subprocess
    is live (see the rationale above :func:`_stream_stamp`).  The thread is a
    daemon thread and the stop is a joined ``Event``, so nothing here can
    outlive the tick or keep the interpreter alive.
    """

    def __init__(self, project, pr, gate, *, stage="gating", interval=None):
        self.project = project
        self.pr = pr
        self.gate = gate
        self.stage = stage
        # Resolved here (call time), not in the signature, so rebinding the
        # module constant still takes effect.
        self.interval = LIVENESS_STAMP_S if interval is None else interval
        self._stop = threading.Event()
        self._thread = None
        self._started = time.monotonic()

    def _stamp(self):
        _stream_stamp(project=self.project, pr=self.pr, stage=self.stage,
                      gate=self.gate,
                      elapsed_s=round(time.monotonic() - self._started, 1))

    def _run(self):
        # One stamp immediately: the span from the leg starting to the first
        # interval boundary is exactly the dark window this class exists to
        # close, and the caller's stage stamp may already be seconds old.
        self._stamp()
        # Event.wait, not sleep: stop() has to release the loop at once rather
        # than after another whole interval.
        while not self._stop.wait(self.interval):
            self._stamp()

    def __enter__(self):
        self._thread = threading.Thread(
            target=self._run,
            name=f"{_LIVENESS_THREAD_PREFIX}-{self.pr}", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False

    def stop(self):
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            # The loop waits on the event, so set() releases it immediately and
            # the join is cheap; it is also what makes "no heartbeat outlives
            # the gate" a fact instead of a hope.  Bounded so a wedged stream
            # write can never hang the tick that is only trying to finish.
            thread.join(timeout=max(5.0, self.interval + 5.0))


def _gate_qos_prefix():
    """Argv prefix that pulls the gate out of inherited background QoS.

    ``()`` means "ask for nothing" — which is deliberately the answer on every
    non-macOS host AND on a macOS host where /usr/sbin/taskpolicy is missing.
    The prefix is a *program* exec'd before the shell, so an unavailable program
    would make the gate exit 127 and record `red-gate` on a PR whose code was
    never even compiled.  A QoS speed-up is never worth a false red, so this
    fails toward the pre-fix behaviour rather than toward the fix.
    """
    global _GATE_QOS_PREFIX_CACHE
    if _GATE_QOS_PREFIX_CACHE is None:
        _GATE_QOS_PREFIX_CACHE = (
            (TASKPOLICY_PATH, "-c", GATE_QOS_POLICY_CLASS)
            if sys.platform == "darwin" and os.path.exists(TASKPOLICY_PATH)
            else ()
        )
    return _GATE_QOS_PREFIX_CACHE


#: The interpreter a gate's shell string runs under.  ``shell=True`` resolves to
#: this same ``/bin/sh`` (``subprocess.unix_shell`` on POSIX) and deliberately
#: NOT to ``$SHELL``, so naming it by hand keeps an operator's gate scripts
#: behaving exactly as they did before the QoS prefix below was added.
GATE_SHELL = "/bin/sh"


def _gate_shell_argv(command):
    """Argv that runs the operator's shell string at the requested QoS class.

    The shape is ``[taskpolicy, -c, utility, /bin/sh, -c, <command>]``, spawned
    with ``shell=False``.  Two things about it are load-bearing:

    * `taskpolicy` must be the *program* that gets exec'd, so the shell and every
      pytest leg under it inherit the class — that is the entire point of item 1.
    * the command must reach the shell as ONE argument.

    The tempting one-line diff — a list argv with ``shell=True`` still set — does
    the opposite of both.  On POSIX ``shell=True`` runs
    ``/bin/sh -c <argv[0]> <argv[1]> ...``, so only ``argv[0]`` is the command and
    ``-c utility /bin/sh -c <gate>`` silently become the shell's own ``$0 $1 $2``.
    Measured on this host: the gate then ran a bare `taskpolicy`, printed its
    usage and exited **64** — which the queue would have recorded as `red-gate`
    on every PR on the Mac, against code that was never even compiled.  An
    injected fake ``Popen`` cannot see that, because it never execs anything,
    which is why :func:`test_the_gate_argv_actually_runs_the_operators_command`
    executes the real argv instead of only reading its shape.
    """
    return [*_gate_qos_prefix(), GATE_SHELL, "-c", command]


def _stream_text(stream):
    """``TimeoutExpired.stdout``/``.stderr`` as text, whatever Python handed us.

    These are `bytes` when the child ran without text mode and `str` with it —
    and on 3.14 `TimeoutExpired.stdout` came back as empty `bytes` even for a
    `text=True` request.  The old `isinstance(x, str)` test silently discarded
    the bytes case, which is the whole reason a timed-out gate's handback
    measured as `['', '', 'timeout after 1800s']`: real output was there and
    was thrown away.  `errors="replace"` keeps one truncated multi-byte
    sequence at the kill point from losing the whole tail.
    """
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream or ""


def _kill_gate_group(proc):
    """Best-effort: kill the timed-out gate's WHOLE process group, then reap it.

    The gate runs `start_new_session=True`, so its pid is also its process-group
    id and `killpg` reaches the `sh` *and* the pytest it spawned.  Killing only
    the shell — what an ungrouped kill does — left pytest running: measured, the
    queue re-gated 68 s later and attempt 2 competed with attempt 1's orphan.

    `os.killpg` is looked up on the module at call time, which is also what lets
    a test record which group the kill was aimed at without any real process
    being spawned.

    Best-effort by design, for two reasons.  A process can die between the kill
    and the reap (the `ESRCH` case), and the group can already be gone; and
    `proc` may be an injected test fake whose pid belongs to no OS process at
    all.  None of those may turn a timeout into an exception escaping the
    daemon's tick — the caller's `timeout` verdict and its requeue are what
    protect the PR, never this cleanup.
    """
    try:
        pid = proc.pid
        if pid:
            os.killpg(pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        # Reaps the shell so a killed gate leaves no zombie behind for the
        # daemon's own lifetime.
        proc.communicate()
    except Exception:
        pass


def _run_gate(command, worktree, timeout_s):
    """Run one shell gate; returns ``(verdict, output)``.

    ``verdict`` is one of ``"pass"``, ``"fail"`` or ``"timeout"``.  The third
    one is the whole point: running out of the budget is NOT a verdict about
    the code — the gate never got to answer.  Flattening it into ``fail`` made
    a slow queue look like a broken PR, dropped the PR from the queue and told
    the author to go fix code that was never shown to be broken.

    Three things this leg must get right, all learned from a 1800 s budget that
    a 1092.6 s suite kept failing (see :func:`_gate_qos_prefix`,
    :func:`_kill_gate_group` and :func:`_stream_text`):

    1. do not run at the launchd job's inherited **background QoS**, which
       measured 3.34x slower and is what actually exhausted the budget;
    2. do not leave the gate's pytest **alive after the timeout**, or the next
       attempt competes with the previous attempt's orphan;
    3. carry the **real last lines** into the handback, so a timeout is
       distinguishable from a test failure by reading the PR comment.

    And a fourth, learned from the other side of the same comment: the last
    lines are not enough to name a failure.  A 12-leg run buries its
    `FAILED path::test` line under everything printed after it, so a non-passing
    leg goes through :func:`_gate_report`, which puts the verbatim failure lines
    ahead of the tail.

    Internal contract only (``process_entry`` is the sole caller plus injected
    test runners); nothing outside this module parses this tuple.
    """
    argv = _gate_shell_argv(command)
    # Bound before the try: `TimeoutExpired` is raised from *inside* Popen on
    # some paths, and reading an unbound name there would replace the timeout
    # verdict with a NameError.
    stdout = stderr = ""
    try:
        proc = subprocess.Popen(argv, shell=False, cwd=worktree,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            # communicate() rather than run(timeout=): the timeout must be able
            # to reach the process group, and `subprocess.run` throws its Popen
            # handle away before it raises TimeoutExpired.
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            _kill_gate_group(proc)
            # Prefer what communicate() salvaged, fall back to whatever the
            # exception carries: they differ by Python version and by how far
            # the kill raced the pipes, and the whole bug was picking one.
            out = _stream_text(stdout) or _stream_text(exc.stdout)
            err = _stream_text(stderr) or _stream_text(exc.stderr)
            return "timeout", _gate_report(f"{out}\n{err}",
                                           note=f"timeout after {timeout_s}s")
        verdict = "pass" if proc.returncode == 0 else "fail"
        full = _stream_text(stdout) + _stream_text(stderr)
        # A passing leg keeps the plain tail it always kept — the digest is
        # triage evidence for a red run, and scanning 12 green legs for markers
        # would only risk reformatting output nobody reads.
        return verdict, (_tail(full) if verdict == "pass"
                         else _gate_report(full))
    except OSError as exc:
        # The gate could not be started at all.  That is a real failure with a
        # readable reason, and it must NOT be reported as a timeout: a timeout
        # requeues and retries forever, which is how a broken gate becomes an
        # invisible one.
        return "fail", _tail(f"could not run gate: {exc}")


def _push_branch(worktree, info, git_runner):
    branch = info.get("headRefName")
    expected = info.get("headRefOid")
    lease = f"refs/heads/{branch}:{expected}" if expected else f"refs/heads/{branch}"
    proc = _git(["push", f"--force-with-lease={lease}", "origin",
                 f"HEAD:refs/heads/{branch}"], worktree, git_runner,
                timeout=DEFAULT_GATE_TIMEOUT_S)
    if proc is None or proc.returncode != 0:
        return f"git push --force-with-lease failed: {_tail(proc.stderr if proc else '', 10)}"
    return None


# ── card handback ───────────────────────────────────────────────────────────

def default_handback(card, assignee, message):
    """Comment + reassign (+ unblock) through the sanctioned hermes CLI.

    Mirrors ``escalate_watcher._default_reassign``: the daemon never writes a
    board directly for a handback. Returns True when the comment landed.
    """
    if not card:
        return False
    try:
        from .lifecycle import find_hermes_bin
        hermes = find_hermes_bin()
    except Exception:
        hermes = None
    if not hermes:
        return False

    def run(args):
        try:
            return subprocess.run([hermes, "kanban", *args],
                                  capture_output=True, text=True,
                                  timeout=60)
        except Exception:
            return None

    result = run(["comment", str(card), message])
    if result is None or result.returncode != 0:
        return False
    if assignee:
        run(["reassign", str(card), str(assignee), "--reclaim"])
    # A card parked in blocked/scheduled by the failure resumes with the
    # worker; a ready/running card just gets an error from the CLI, harmless.
    run(["unblock", str(card)])
    return True


def _comment_pr(repo, pr, body, gh_runner, log=None):
    log = log or (lambda *a, **k: None)
    try:
        _gh(["pr", "comment", str(pr), "--body", body], repo, gh_runner)
    except Exception as exc:
        log(f"merge-queue: PR #{pr} comment failed: "
            f"{type(exc).__name__}: {exc}", "WARN")


def resolve_instructions(branch, base="main"):
    """The resolve step a handed-back card's worker must follow (MQ3).

    The PR's head branch IS the card branch (``wt/<card>`` or the branch the
    card recorded), so it is the only ref the worker may force-push. The
    pre-push guard (``.githooks/pre-push``) refuses main and every other
    branch, so this text and the fence agree.
    """
    target = branch or "the branch recorded on your card"
    return (
        "Resolve it as this card's worker:\n"
        "1. In your own card worktree: `git fetch origin && git rebase "
        f"origin/{base}`.\n"
        "2. Fix the conflicted files / the failing gate, then re-run the "
        "gate until it is green.\n"
        f"3. Push back to YOUR card branch only: `git push --force-with-lease "
        f"origin {target}`.\n"
        "   Never force-push main or any other branch — the pre-push guard "
        "refuses it.\n"
        "4. Comment the result on the card and hand it back for review.\n"
    )


# ── auto-enqueue ────────────────────────────────────────────────────────────

def _pr_url(text):
    """``(owner, repo, pr)`` for the first GitHub PR URL in ``text``, or None."""
    match = PR_URL_RE.search(text or "")
    if not match:
        return None
    return (match.group("owner"), match.group("repo"),
            int(match.group("number")))


def _origin_slug(repo, git_runner=None):
    """The project repo's ``origin`` as ``owner/repo`` (lower-case), or None.

    Auto-enqueue compares this against the owner/repo in a card's PR URL so a
    cross-repo link can never enqueue an unrelated same-numbered PR.
    """
    runner = git_runner or _default_runner
    try:
        proc = _git(["remote", "get-url", "origin"], repo, runner)
    except Exception:
        return None
    if proc is None or proc.returncode != 0:
        return None
    match = _REMOTE_SLUG_RE.search((proc.stdout or "").strip())
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}".lower()


def _required_checks(cfg, base, git_runner=None, gh_runner=None, info=None):
    """Does the PR's base branch require status checks?

    ``True``/``False`` when GitHub answers, ``None`` when it cannot be
    determined. Only a positive ``False`` (the required-status-checks endpoint
    is absent, HTTP 404) makes an empty rollup green; a permission error or an
    unparseable answer stays unknown and therefore not green.
    """
    link = _pr_url((info or {}).get("url") or "")
    slug = (f"{link[0]}/{link[1]}".lower() if link
            else _origin_slug((cfg or {}).get("repo"), git_runner))
    if not slug or not base:
        return None
    gh_runner = gh_runner or _default_runner
    quoted = urllib.parse.quote(str(base), safe="")
    try:
        proc = _gh([
            "api", f"repos/{slug}/branches/{quoted}/protection/"
                   "required_status_checks"], cfg["repo"], gh_runner)
    except Exception:
        return None
    if proc is None:
        return None
    if proc.returncode != 0:
        err = getattr(proc, "stderr", "") or ""
        if "404" in err or "Not Found" in err:
            return False
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return bool(data.get("contexts") or data.get("checks"))


def _required_for_info(cfg, base, info, git_runner=None, gh_runner=None):
    """Required-check answer, but only asked when the rollup is empty."""
    if isinstance(info, dict) and info.get("statusCheckRollup"):
        return False
    return _required_checks(cfg, base, git_runner, gh_runner, info=info)


def _auto_enqueue_suppressed(doc, project, cand, cfg, gh_runner=None):
    """True when a red-gated PR is still sitting on the same head SHA.

    Dropping a red-gate PR from the queue must not make the next empty tick
    pick it straight back up: that reruns the gate, comments and reassigns the
    card every minute. Auto-enqueue is suppressed until the PR head changes
    (a worker push) or the operator adds it again explicitly.
    """
    last = _project_state(doc, project).get("last")
    if not isinstance(last, dict) or last.get("result") != "red-gate":
        return False
    if int(last.get("pr") or 0) != int(cand.get("pr") or 0):
        return False
    gated_sha = str(last.get("head_sha") or "")
    if not gated_sha:
        return False
    info = pr_view(cfg["repo"], cand["pr"], runner=gh_runner)
    if not isinstance(info, dict):
        # Cannot prove the head moved; keep it out rather than re-run a gate.
        return True
    return str(info.get("headRefOid") or "") == gated_sha


def _discover_ready_cards(cfg, gh_runner=None, kanban_db=None, git_runner=None):
    """Cards on the project board whose latest PR URL is ready to merge.

    Read-only: enumerates the board through the SAME seam autodown uses and
    reads comments through ``board_pin.open_board_ro``. A card URL whose
    owner/repo is not the project's own ``origin`` is ignored, so a foreign
    repo's PR can never be enqueued under this project. Fork PRs and PRs whose
    base is not the repo default (or configured base) are skipped here as well
    as in :func:`process_entry`, so a refusal is never auto-re-added every
    tick. Returns ``[{pr, card, assignee, branch}]``.
    """
    board = cfg.get("board")
    if not board:
        return []
    want_repo = _origin_slug(cfg["repo"], git_runner)
    if want_repo is None:
        return []
    # Local refs only: discovery must stay free of extra network probes.
    base = expected_base(cfg, git_runner, probe_remote=False)
    try:
        from . import autodown, board_pin
        kb = kanban_db or autodown._load_kanban_db_or_default()
        if kb is None:
            return []
        boards, multi = autodown._enum_board_names(kb, registered_only=True)
        label = str(board)
        if label not in [b if b else "default" for b in boards]:
            return []
        with board_pin.open_board_ro(kb, label, multi=multi) as conn:
            # Only cards that are actively in flight: a blocked/scheduled/todo
            # card (or one already done/archived) is not mergeable just because
            # its PR looks green.
            rows = conn.execute(
                "SELECT id, assignee, body FROM tasks "
                "WHERE status IN ('running', 'ready', 'review')").fetchall()
            found = []
            for row in rows:
                comments = conn.execute(
                    "SELECT body FROM task_comments WHERE task_id = ? "
                    "ORDER BY id DESC LIMIT 20", (row["id"],)).fetchall()
                link = None
                for text in [row["body"] or ""] + [
                        c["body"] or "" for c in comments]:
                    link = _pr_url(text)
                    if link:
                        break
                if link is None:
                    continue
                owner, repo_name, pr = link
                if f"{owner}/{repo_name}".lower() != want_repo:
                    continue
                found.append((row["id"], row["assignee"], pr))
    except Exception:
        return []

    ready = []
    seen = set()
    for card, assignee, pr in found:
        if pr in seen:
            continue
        info = pr_view(cfg["repo"], pr, runner=gh_runner)
        required = _required_for_info(cfg, base, info, git_runner, gh_runner)
        if (_pr_ready(info, required_checks=required)
                and refusal_reason(info, base) is None):
            seen.add(pr)
            ready.append({"pr": pr, "card": card, "assignee": assignee,
                          "branch": info.get("headRefName")})
    return ready


# ── one PR ──────────────────────────────────────────────────────────────────

def _update_flight(state_path, project, pr, **fields):
    """Stamp progress on the in-flight claim (fresh read, under the lock).

    The monitor reads ``stage`` so "rebasing" is not reported as "merging";
    the branch is recorded here too, since a manually enqueued PR has no
    branch until ``gh pr view`` answers.

    A ``stage`` change is mirrored onto the HEALTH STREAM at the same moment it
    reaches the LEDGER, from the same still-holds-the-claim condition — one
    writer, one truth.  Without it the stream only advances when ``tick()``
    returns, which is minutes (up to the whole gate budget) after the stage the
    queue is actually in.
    """
    stage = None
    with _file_lock():
        doc = load_state(state_path)
        flight = _project_state(doc, project).get("in_flight")
        if flight and int(flight.get("pr", 0)) == int(pr):
            flight.update(fields)
            save_state(doc, state_path)
            if "stage" in fields:
                stage = fields["stage"]
    if stage is not None:
        # Outside the file lock: the stream is a separate file and a health
        # stamp must never extend how long the CLI waits on this lock.
        _stream_stamp(project=project, pr=pr, stage=stage)


def _handback(entry, cfg, body, handback, gh_runner, log=None):
    _comment_pr(cfg["repo"], entry["pr"], body, gh_runner, log=log)
    try:
        handback(entry.get("card"), entry.get("assignee"), body)
    except Exception:
        pass


def _last_record(result, entry, **fields):
    """The one shape of a ``ps["last"]`` row, shared by every terminal outcome."""
    return {
        "pr": entry.get("pr"),
        "card": entry.get("card"),
        "branch": entry.get("branch"),
        "result": result,
        "at": _now_iso(),
        **fields,
    }


def _finish(doc, project, state_path, ps, result, entry, **fields):
    """Record a terminal outcome on a FRESH ledger read.

    The tick may spend minutes in gates; a CLI ``add``/``remove`` can land in
    that window. Re-reading before writing keeps those edits instead of
    clobbering them with the tick's older copy.
    """
    with _file_lock():
        doc = load_state(state_path)
        ps = _project_state(doc, project)
        flight = ps.get("in_flight")
        if flight and int(flight.get("pr", 0)) == int(entry.get("pr", 0)):
            ps["in_flight"] = None
        ps["last"] = _last_record(result, entry, **fields)
        save_state(doc, state_path)


def _refuse_entry(doc, state_path, project, entry, reason, cfg, gh_runner,
                  log=None):
    """Record a terminal refusal (base/fork) and say why on the PR."""
    body = ("Merge queue: refusing this PR.\n\n"
            f"{reason}\n\n"
            "It stays out of the queue until it is retargeted (base branch) "
            "or re-opened from a branch in this repo.\n")
    _comment_pr(cfg["repo"], entry["pr"], body, gh_runner, log=log)
    _finish(doc, project, state_path, None, "refused", entry, error=reason)
    return {"pr": entry["pr"], "result": "refused", "error": reason}


def _cancel_reason(state_path, project, entry, cfg):
    """Why this claim must stop now, or None when it may proceed.

    A ``remove`` clears the claim; ``pause``/``disable`` leave it but change the
    project. All three can land while a rebase or a gate is running, so this is
    a FRESH read under the file lock — never the tick's older snapshot.

    A removal that landed while this PR held the claim does not clear it (see
    :func:`remove_from_queue`): the mark is the cancellation here, so the
    still-running processing stops at the next checkpoint and settles the claim
    instead of carrying on as if nothing had happened.
    """
    with _file_lock():
        doc = load_state(state_path)
        ps = _project_state(doc, project)
        flight = ps.get("in_flight")
        if not (flight
                and int(flight.get("pr", 0)) == int(entry.get("pr", 0))):
            return "removed"
        if flight.get(CANCEL_KEY):
            return "removed"
        if ps.get("paused"):
            return "paused"
        if cfg is not None and not effective_enabled(cfg, ps):
            return "disabled"
        return None


def _stop_cancelled(state_path, project, entry, reason):
    """Stop a cancelled claim: drop on remove, keep the entry on pause/disable."""
    with _file_lock():
        doc = load_state(state_path)
        ps = _project_state(doc, project)
        flight = ps.get("in_flight")
        if (flight
                and int(flight.get("pr", 0)) == int(entry.get("pr", 0))):
            ps["in_flight"] = None
            if (reason != "removed"
                    and _find_entry(ps["queue"], entry.get("pr")) is None):
                ps["queue"].insert(0, entry)
        save_state(doc, state_path)
    return {"pr": entry.get("pr"), "result": reason}


def process_entry(project, cfg, entry, *, doc=None, state_path=None,
                  root=None, git_runner=None, gh_runner=None,
                  handback=None, log=None):
    """Process one claimed PR entry. Returns a small result dict."""
    doc = doc if doc is not None else load_state(state_path)
    ps = _project_state(doc, project)
    git_runner = git_runner or _default_runner
    gh_runner = gh_runner or _default_runner
    handback = handback or default_handback
    log = log or (lambda *a, **k: None)

    base = expected_base(cfg, git_runner) or "main"
    info = pr_view_settled(cfg["repo"], entry["pr"], runner=gh_runner)
    if info is None:
        return _retry_or_drop(doc, state_path, project, entry, "gh pr view failed")
    if info.get("headRefName"):
        entry["branch"] = info["headRefName"]
    state = str(info.get("state", "")).upper()
    if state in ("MERGED", "CLOSED"):
        # Terminal BEFORE any work is attempted: a merged PR's branch is deleted
        # on merge, so anything past here rebases a ref that no longer exists and
        # can spend the whole gate budget on a commit already on main.  The tell
        # is the PR's state, not that git error.
        result = "already-" + state.lower()
        _finish(doc, project, state_path, ps, result, entry,
                sha=info.get("headRefOid"))
        return {"pr": entry["pr"], "result": result}
    reason = refusal_reason(info, base)
    if reason:
        # Terminal: a fork or wrong-base PR is not transient, so retrying (and
        # auto-enqueue re-adding it) can never make it mergeable here.
        log(f"merge-queue {project}: PR #{entry['pr']} refused: {reason}",
            "WARN")
        return _refuse_entry(doc, state_path, project, entry, reason, cfg,
                             gh_runner, log=log)
    required = _required_for_info(cfg, base, info, git_runner, gh_runner)
    if not _pr_ready(info, required_checks=required):
        if _mergeable_unknown(info):
            # Not decided yet is NOT not-ready: keep the head slot so the next
            # tick looks again instead of parking it in a wait list.
            _release_to_head(state_path, project, entry)
            log(f"merge-queue {project}: PR #{entry['pr']} mergeable UNKNOWN "
                f"after {MERGEABLE_POLL_ATTEMPTS} polls; left at the head of "
                "the queue", "WARN")
            return {"pr": entry["pr"], "result": "mergeable-unknown"}
        # Not green yet: leave it at the head of the queue, no handback.
        _requeue_waiting(state_path, project, entry)
        log(f"merge-queue {project}: PR #{entry['pr']} checks not green; "
            "moved to the wait list", "INFO")
        return {"pr": entry["pr"], "result": "waiting"}

    cancel = _cancel_reason(state_path, project, entry, cfg)
    if cancel:
        return _stop_cancelled(state_path, project, entry, cancel)

    _update_flight(state_path, project, entry["pr"], stage="rebasing",
                   branch=entry.get("branch"))
    try:
        worktree = ensure_worktree(cfg, root=root, git_runner=git_runner,
                                   base=base)
    except Exception as exc:
        return _retry_or_drop(doc, state_path, project, entry, str(exc))

    err = _prepare_head(worktree, info, git_runner)
    if err:
        return _retry_or_drop(doc, state_path, project, entry, err)

    ok, conflicted, output = _rebase_onto(worktree, base, git_runner)
    if not ok:
        names = ", ".join(conflicted) if conflicted else "(files not reported)"
        body = (f"Merge queue: rebase onto origin/{base} conflicted.\n\n"
                f"Conflicted files: {names}\n\n"
                f"{resolve_instructions(info.get('headRefName'), base)}\n"
                f"{output}")
        _finish(doc, project, state_path, ps, "conflict", entry,
                conflicted=conflicted)
        _handback(entry, cfg, body, handback, gh_runner, log=log)
        log(f"merge-queue {project}: PR #{entry['pr']} conflict in {names}",
            "WARN")
        return {"pr": entry["pr"], "result": "conflict",
                "conflicted": conflicted}

    cancel = _cancel_reason(state_path, project, entry, cfg)
    if cancel:
        return _stop_cancelled(state_path, project, entry, cancel)

    gates = _resolve_gates(cfg, worktree, git_runner, base=base)
    _update_flight(state_path, project, entry["pr"],
                   stage="gating" if gates else "merging")
    timeout_s = int(cfg.get("timeout_s") or DEFAULT_GATE_TIMEOUT_S)
    for command in gates:
        # One gate leg can hold the entire DEFAULT_GATE_TIMEOUT_S budget, so the
        # stream is heartbeated from around the call, not after it: the wrapper
        # starts the heartbeat immediately before the leg and stops it in a
        # finally immediately after (see _GateLiveness).  The `with` closes
        # BEFORE the verdict branch below, so a heartbeat can never outlive the
        # leg it reports on — including the timeout path that requeues.
        with _GateLiveness(project, entry["pr"], command):
            verdict, output = _run_gate(command, worktree, timeout_s)
        if verdict == "timeout":
            # Out of budget is NOT out of green.  The gate never answered, so
            # nothing here is a verdict about the PR's code: the entry goes
            # back to the wait list (non-terminal) instead of being dropped,
            # and the handback must not send the author off to fix code.
            #
            # ORDER MATTERS: `_requeue_waiting` only re-appends while this PR
            # still holds the claim, and `_finish` clears `in_flight` — running
            # `_finish` first is precisely how a timeout used to drop the PR.
            _update_flight(state_path, project, entry["pr"], stage="timeout")
            _requeue_waiting(state_path, project, entry)
            # `last` is written after the requeue, so the entry is safely back
            # in the wait list before anything records an outcome.  The row
            # renders `waiting` (see _OUTCOME_STATES), never `resolve`.
            _finish(doc, project, state_path, ps, "gate-timeout", entry,
                    gate=command, timeout_s=timeout_s)
            body = (
                f"Merge queue: the gate ran out of its time budget.\n\n"
                f"Gate: {command}\n"
                f"Budget: {timeout_s}s (exceeded)\n\n"
                "This is NOT a test failure. Nothing in this PR was shown to "
                "be broken — the queue simply did not get an answer from the "
                "gate before its budget was spent, and a gate that never "
                "finished cannot say whether the code is good.\n\n"
                f"The PR stays in the merge queue (back in the wait list) and "
                "the queue will look again on a later tick. No action is "
                "needed from you; do not rebase or 'fix the gate' because of "
                "this comment.\n"
                f"{output}")
            _handback(entry, cfg, body, handback, gh_runner, log=log)
            log(f"merge-queue {project}: PR #{entry['pr']} gate timed out "
                f"after {timeout_s}s: {command} (not a test failure)", "WARN")
            return {"pr": entry["pr"], "result": "gate-timeout",
                    "gate": command, "timeout_s": timeout_s}
        if verdict != "pass":
            # `attempts` counts retries ALREADY SPENT (0 on a first pass), so the
            # handback reports the 1-based attempt the reader expects to see.
            attempt = int(entry.get("attempts") or 0) + 1
            # Evidence first, boilerplate last: `output` leads with the verbatim
            # failure lines, so the comment names the broken test above the
            # resolve script instead of below six lines of it.
            body = (f"Merge queue: gate failed after rebasing onto "
                    f"origin/{base}.\n\n"
                    f"Gate: {command}\n"
                    f"Attempt: {attempt}\n\n"
                    f"{output}\n\n"
                    f"{resolve_instructions(info.get('headRefName'), base)}\n")
            _finish(doc, project, state_path, ps, "red-gate", entry,
                    gate=command, head_sha=info.get("headRefOid"))
            _handback(entry, cfg, body, handback, gh_runner, log=log)
            log(f"merge-queue {project}: PR #{entry['pr']} gate failed: "
                f"{command}", "WARN")
            return {"pr": entry["pr"], "result": "red-gate",
                    "gate": command}

    # A remove/pause/disable that landed while the gates ran wins before the
    # force-push. The claim is re-read under the file lock, never from our
    # tick-start snapshot.
    cancel = _cancel_reason(state_path, project, entry, cfg)
    if cancel:
        return _stop_cancelled(state_path, project, entry, cancel)

    _update_flight(state_path, project, entry["pr"], stage="merging")
    err = _push_branch(worktree, info, git_runner)
    if err:
        return _retry_or_drop(doc, state_path, project, entry, err)

    sha = _git(["rev-parse", "HEAD"], worktree, git_runner)
    head_sha = (sha.stdout or "").strip() if sha is not None else ""
    if not head_sha:
        return _retry_or_drop(doc, state_path, project, entry,
                              "could not read the pushed head SHA")

    # The force-push moved the head, so the pre-push readiness answer is stale:
    # re-read the PR itself and merge only this exact SHA while it is green.
    after = pr_view_settled(cfg["repo"], entry["pr"], runner=gh_runner)
    cancel = _cancel_reason(state_path, project, entry, cfg)
    if cancel:
        return _stop_cancelled(state_path, project, entry, cancel)
    return _settle_pushed_head(
        project, cfg, entry, after, head_sha, base=base,
        state_path=state_path, cwd=worktree, git_runner=git_runner,
        gh_runner=gh_runner, handback=handback, log=log)


def _requeue_waiting(state_path, project, entry):
    """Move a not-yet-green entry to the wait list, on a fresh ledger read.

    It leaves the head slot instead of being reinserted at position 0, so the
    later FIFO entries can proceed while this PR waits for its checks.
    """
    with _file_lock():
        doc = load_state(state_path)
        ps = _project_state(doc, project)
        flight = ps.get("in_flight")
        if flight and int(flight.get("pr", 0)) == int(entry.get("pr", 0)):
            ps["in_flight"] = None
            if (_find_entry(ps["queue"], entry.get("pr")) is None
                    and _find_entry(ps["waiting"], entry.get("pr")) is None):
                ps["waiting"].append(entry)
        save_state(doc, state_path)


def _release_to_head(state_path, project, entry):
    """Give a claim back at the HEAD of the FIFO, not in the wait list.

    Used when the PR's readiness is simply not decided yet (GitHub answered
    ``mergeable: UNKNOWN`` even after the bounded re-poll): parking it in
    ``waiting`` is what used to strand it, so it must keep its head slot.
    """
    with _file_lock():
        doc = load_state(state_path)
        ps = _project_state(doc, project)
        flight = ps.get("in_flight")
        if flight and int(flight.get("pr", 0)) == int(entry.get("pr", 0)):
            ps["in_flight"] = None
            if (_find_entry(ps["queue"], entry.get("pr")) is None
                    and _find_entry(ps["waiting"], entry.get("pr")) is None):
                ps["queue"].insert(0, entry)
        save_state(doc, state_path)


# ── stale-base auto-refresh ───────────────────────────────────────────────
#
# A red PR in the wait list was red AT THE MOMENT ITS CI RAN, and nothing ever
# looked again: PRs sat 9-18 h red because main itself was broken, or because
# one flaky leg happened to fail on their run. Neither is the PR's fault, and
# the wait list was strictly passive about it. ``gh pr update-branch`` moves the
# PR's branch onto the current base, which re-triggers CI on a base that may
# since have been fixed.
#
# The bound IS the design: at most ONE update per (PR, base sha). The sha is
# recorded on the entry the moment gh ANSWERS — updated, already current,
# conflicted, or a gh error — so the next tick reads the same sha and does
# nothing. Without that record a red PR would re-update every tick and keep CI
# busy forever. A gh that could not run at all (no runner installed, the
# process would not start) answered nothing, so it records the error and keeps
# the sha: a local fault must not write off the base for the PR.

def _remote_base_sha(repo, base, git_runner=None):
    """The CURRENT sha of ``refs/heads/<base>`` on origin, or None.

    ``ls-remote`` rather than ``git rev-parse origin/<base>`` on purpose: this
    drain does not fetch, and the case it exists for is a queue that is IDLE —
    nothing fetches then, so the local remote-tracking ref never moves and a
    stale base would never be noticed. One bounded network read, taken only
    when a red waiting entry actually needs the answer.
    """
    runner = git_runner or _default_runner
    try:
        proc = _git(["ls-remote", "--", "origin", f"refs/heads/{base}"],
                    repo, runner)
    except Exception:
        return None
    if proc is None or proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        fields = line.split()
        if fields and fields[0].strip():
            return fields[0].strip().lower()
    return None


def _red_on_stale_base(entry, info):
    """True when a waiting entry's checks are red/cancelled AND a refresh is
    still allowed for it.

    A ``CANCELLED`` check lands here too: GitHub cancels a run when a newer
    commit pushes it aside, ``_truthy_check`` reads that as not-green, and the
    PR is then waiting on a verdict that can never arrive.  No ``required``
    answer is needed: an empty rollup reads as green or pending, never red, so
    this cannot be flipped by branch protection.  A PR that is not open, a
    draft, or a refresh that already conflicted (card 2 hands that one back) is
    left strictly alone.
    """
    if bool(entry.get("update_conflict")):
        return False
    if str(info.get("state", "")).upper() != "OPEN" or info.get("isDraft"):
        return False
    return _checks_verdict(info) == "red"


def _update_branch(repo, pr, gh_runner):
    """ONE ``gh pr update-branch <pr>``.

    Returns ``(ok, conflict, output, answered)``. ``answered`` is False only
    when ``gh`` never got to reply — no runner installed, or the process could
    not be started at all. That is a LOCAL fault rather than a verdict on this
    base, and the caller must not count it as an attempt; a non-zero exit with
    a message from GitHub IS an answer.

    Never retries — the caller decides what a conflict means.
    """
    try:
        proc = _gh(["pr", "update-branch", str(pr)], repo, gh_runner)
    except Exception as exc:
        return False, False, f"gh pr update-branch raised {exc!r}", False
    if proc is None:
        return False, False, "gh pr update-branch: gh not runnable", False
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        return False, "conflict" in output.lower(), output, True
    return True, False, output, True


def _stamp_waiting(state_path, project, patches):
    """Write ``patches`` (pr -> fields) onto the entries still in the wait list.

    Shared by the refresh and the hand-back, which both stamp bookkeeping after
    a pass of network calls. It reads the ledger FRESH under the file lock and
    skips any PR that has since left ``waiting`` — a CLI ``add`` during the
    ``gh`` call moves the entry, and its history belongs wherever it landed.
    """
    if not patches:
        return
    with _file_lock():
        doc = load_state(state_path)
        ps = _project_state(doc, project)
        for entry in ps.get("waiting") or []:
            if not isinstance(entry, dict):
                continue
            pr = entry.get("pr")
            if pr is None:
                continue
            try:
                pr = int(pr)
            except (TypeError, ValueError):
                continue
            if pr in patches:
                entry.update(patches[pr])
        save_state(doc, state_path)


def _refresh_stale_red(project, cfg, base, entries, *, state_path,
                       git_runner=None, gh_runner=None, log=None,
                       base_sha=None):
    """Update red wait-listed PRs onto the current base, once per base sha.

    Returns the PR numbers this pass wrote a patch for. Only entries that are
    still in this project's wait list are written back — a CLI ``add`` during
    the ``gh`` call moves the entry out, and a moved entry's bookkeeping
    belongs to wherever it landed.

    ``base_sha`` lets the caller hand down the sha it already probed (the
    wait-list drain needs the same answer to decide the hand-back, and two
    ``ls-remote`` calls per tick for one wait list would be silly). Left None it
    probes for itself, so every other caller keeps its own behaviour.

    An entry whose ``gh`` never got to answer (no runner installed, the process
    could not start) gets ONLY ``last_update_error``: no base sha, so the next
    tick asks again. A local fault is not an attempt at this base.
    """
    log = log or (lambda *a, **k: None)
    # The daemon calls this with NO runners (``check_merge_queue`` injects
    # none), exactly like ``process_entry`` and ``_resume_ci_pending``: default
    # them here, at the seam that uses them, never inside ``_gh``/``_git``.
    # Every gh/git call below — ``pr_view``, the base probe, ``update-branch``,
    # the hand-back comment — inherits from these two lines.
    git_runner = git_runner or _default_runner
    gh_runner = gh_runner or _default_runner
    candidates = [entry for entry in entries
                  if isinstance(entry, dict) and entry.get("pr") is not None]
    if not candidates:
        return []
    sha = base_sha or _remote_base_sha(cfg["repo"], base, git_runner)
    if not sha:
        # Base unknown: doing nothing is the read-only answer, and the next
        # tick asks again — no attempt was made, so nothing is recorded.
        return []
    patches = {}
    for entry in candidates:
        if str(entry.get("refreshed_main_sha") or "") == sha:
            # Already refreshed onto exactly this base: the bound.
            continue
        pr = int(str(entry["pr"]))
        ok, conflict, output, answered = _update_branch(cfg["repo"], pr,
                                                        gh_runner)
        head = sha[:12]
        if not answered:
            # gh never got to answer — not installed, not runnable, or the call
            # raised (the None-runner crash the daemon hit every tick). That is
            # a LOCAL fault, not a verdict on this base, so the sha stays
            # unrecorded and the next tick tries again. Recording it here would
            # burn the whole base on one transient local error and the PR would
            # sit red forever, which is the trap this refresh exists to open.
            patch = {"last_update_error": _tail(output, 3) or "gh not runnable"}
            log(f"merge-queue {project}: PR #{pr} update-branch could not run "
                f"on main {head}: {patch['last_update_error']}", "WARNING")
            patches[pr] = patch
            continue
        patch = {"refreshed_main_sha": sha, "refreshed_at": _now_iso()}
        if ok:
            verb = ("branch already onto main"
                    if "already up to date" in output.lower()
                    else "updated branch onto main")
            log(f"merge-queue {project}: PR #{pr} red on stale base; "
                f"{verb} {head}", "INFO")
        elif conflict:
            patch["update_conflict"] = True
            log(f"merge-queue {project}: PR #{pr} update-branch conflicted "
                f"onto main {head}; left in the wait list for handback: "
                f"{_tail(output, 3)}", "WARNING")
        else:
            patch["last_update_error"] = _tail(output, 3) or "gh failed"
            log(f"merge-queue {project}: PR #{pr} update-branch failed on "
                f"main {head}: {patch['last_update_error']}", "WARNING")
        patches[pr] = patch
    _stamp_waiting(state_path, project, patches)
    return sorted(patches)


# ── wait-list hand-back: red for its OWN reasons ────────────────────────────
#
# The refresh above gives a red wait-listed PR one free re-run on the current
# base. When that is spent there is nothing left the queue can do by itself:
# either `update-branch` refused to move the branch at all (a conflict, which is
# never retried) or CI came back red AGAIN on the current base, which is a
# verdict on this PR's code. The wait list used to sit on those silently as
# well — the author only found out by opening the PR, which is how a card sat
# untouched for a day while its PR waited on nobody.
#
# The hand-back reuses the gating path's `_handback` (a PR comment naming what
# is red + a board handback to the card and its assignee) and the entry STAYS in
# the wait list: it is not dropped, and once its checks go green the drain
# returns it to the FIFO exactly as before.
#
# The bound is structural again — at most ONE hand-back per (PR, base sha),
# recorded on the entry as `handed_back_main_sha` — so a PR that stays red can
# not re-comment every tick. A newer base sha re-arms it, which is the intent:
# the earlier hand-back asked the author to fix a red that was partly about the
# base it was tried against.

#: How many failing check names the hand-back lists before it counts the rest.
MAX_HANDBACK_CHECK_NAMES = 8


def _waiting_own_red(entry, info):
    """True when a wait-listed PR is red in a way only its author can clear.

    Two shapes: the checks read red/cancelled, or the base update conflicted
    (a hard wall whatever CI says — the PR cannot land however green it is). A
    closed, merged or draft PR is left strictly alone: the queue has no claim on
    it and a comment there is noise.
    """
    if str(info.get("state", "")).upper() != "OPEN" or info.get("isDraft"):
        return False
    return bool(entry.get("update_conflict")) or _checks_verdict(info) == "red"


def _own_red_reason(entry, base_sha):
    """``"conflict"``, ``"red-after-refresh"`` or None — why to hand it back.

    ``base_sha`` is the CURRENT sha of the base branch. Only an entry already
    refreshed onto THAT sha is handed back: if the base moved since, the red
    verdict is stale and card 1 still has an update to make, so this answers
    None and the caller routes the entry to the refresh instead. That exclusion
    is what stops the queue from blaming an author for a base that has since
    been fixed, and it is why the sha is probed once and shared by both passes.
    """
    if not base_sha:
        return None
    if str(entry.get("handed_back_main_sha") or "") == base_sha:
        return None                             # the bound: once per base sha
    if bool(entry.get("update_conflict")):
        return "conflict"
    if str(entry.get("refreshed_main_sha") or "") == base_sha:
        return "red-after-refresh"
    return None


def _failing_check_names(info, cap=MAX_HANDBACK_CHECK_NAMES):
    """The names of the checks the rollup says failed, in rollup order.

    ``name`` for a CheckRun, ``context`` for a StatusContext; a failing entry
    that reports neither still counts as ``?`` so the list can never understate
    how many checks are red. An empty answer means the rollup named nothing —
    callers must not turn that into "nothing failed".
    """
    names = []
    for item in (info or {}).get("statusCheckRollup") or []:
        if _truthy_check(item) is not False:
            continue
        if isinstance(item, dict):
            names.append(str(item.get("name") or item.get("context") or "?"))
        else:
            names.append("?")
    if len(names) <= cap:
        return names
    return names[:cap] + [f"(+{len(names) - cap} more)"]


def _card_from_branch(branch):
    """The card id a PR branch name names, or None.

    The queue's own convention is that a PR branch IS the card branch
    ``wt/<card>`` (see :func:`resolve_instructions`, which tells the worker to
    push back to exactly that ref), so ``wt/t_1a2b3c`` hands back to
    ``t_1a2b3c``. A manually enqueued PR has no card on its entry and this is
    the only way to find whose work it is.
    """
    name = str(branch or "").strip()
    if not name.startswith("wt/"):
        return None
    return name[len("wt/"):] or None


def _board_card(cfg, card, kanban_db=None):
    """``(card, assignee)`` for ``card`` proven on the project's board.

    The same read-only board seam :func:`_discover_ready_cards` uses — the
    daemon never opens a board any other way. No board configured, no hermes, an
    unknown board, no such card or any error at all answers ``(None, None)``,
    which leaves the caller with a comment-only hand-back rather than a guess.
    """
    board = (cfg or {}).get("board")
    if not board or not card:
        return None, None
    try:
        from . import autodown, board_pin
        kb = kanban_db or autodown._load_kanban_db_or_default()
        if kb is None:
            return None, None
        boards, multi = autodown._enum_board_names(kb, registered_only=True)
        label = str(board)
        if label not in [b if b else "default" for b in boards]:
            return None, None
        with board_pin.open_board_ro(kb, label, multi=multi) as conn:
            row = conn.execute(
                "SELECT id, assignee FROM tasks WHERE id = ?",
                (str(card),)).fetchone()
    except Exception:
        return None, None
    if row is None:
        return None, None
    return str(row["id"]), (row["assignee"] or None)


def _handback_target(entry, cfg, branch=None):
    """``(card, assignee)`` this wait-list hand-back should reach.

    The entry's own card when it has one — that is how the entry was enqueued.
    Without one, the branch name is the card and the board fills in the
    assignee, so the reassign still has a target. A card that cannot be proved
    on the board is still passed through: the PR comment lands either way, and
    a board comment for a card that turns out not to exist fails harmlessly.
    """
    if entry.get("card"):
        return entry["card"], entry.get("assignee")
    derived = _card_from_branch(branch or entry.get("branch"))
    if not derived:
        return None, entry.get("assignee")
    found, assignee = _board_card(cfg, derived)
    return (found or derived), (assignee or entry.get("assignee"))


def _own_red_body(base, base_sha, reason, branch, checks, entry, card):
    """The hand-back text: what is red, what was already tried, how to fix it.

    Evidence first like the other handbacks: the failing checks lead, the
    resolve step for the PR's own branch follows, and the boilerplate about the
    wait list is last so a reader sees the verdict before the queue's housekeeping.
    """
    if reason == "conflict":
        lead = "Merge queue: this branch cannot be moved onto the current base."
        why = (f"`gh pr update-branch` refused to rebase it onto `{base}` "
               f"{base_sha[:12]} because of a conflict, and the queue does not "
               f"retry a conflict. The branch has to be rebased by hand.")
    else:
        lead = "Merge queue: the checks are red on the current base."
        why = (f"The queue already moved this PR onto `{base}` {base_sha[:12]} "
               f"and its checks are red again on that base, so this is a "
               f"verdict on the PR — not on a base that was broken at the time "
               f"and has since been fixed.")
    failed = ", ".join(checks) if checks else "(gh reported no failing check names)"
    body = (f"{lead}\n\n"
            f"Failing check(s): {failed}\n\n"
            f"{why}\n")
    if entry.get("last_update_error"):
        body += f"\nThe queue's own update attempt also failed: {entry['last_update_error']}\n"
    body += ("\n"
             f"{resolve_instructions(branch, base)}\n"
             f"It stays in the merge queue's wait list{' (card ' + card + ')' if card else ''}: "
             "the queue keeps looking every tick and returns it to the FIFO "
             "when its checks go green.\n")
    return body


def _hand_back_own_red(project, cfg, base, candidates, *, state_path, base_sha,
                       gh_runner=None, handback=None, log=None):
    """Comment on the PR and hand the card back; keep the entry waiting.

    ``candidates`` is ``[(entry, info, reason)]`` from :func:`_drain_waiting`.
    Returns the PR numbers handed back. Like :func:`_refresh_stale_red` the
    bookkeeping is written on a FRESH read under the file lock and only onto
    entries still in this project's wait list: a CLI ``add`` during the ``gh``
    call moves the entry, and its history belongs wherever it landed.
    """
    log = log or (lambda *a, **k: None)
    handback = handback or default_handback
    if not candidates:
        return []
    patches = {}
    for entry, info, reason in candidates:
        try:
            pr = int(str(entry["pr"]))
        except (TypeError, ValueError, KeyError):
            continue
        branch = entry.get("branch") or info.get("headRefName")
        card, assignee = _handback_target(entry, cfg, branch=branch)
        body = _own_red_body(base, base_sha, reason, branch,
                             _failing_check_names(info), entry, card)
        target = dict(entry)
        target["card"], target["assignee"] = card, assignee
        _handback(target, cfg, body, handback, gh_runner, log=log)
        patches[pr] = {"handed_back_at": _now_iso(),
                       "handed_back_main_sha": base_sha}
        log(f"merge-queue {project}: PR #{pr} handed back to card "
            f"{card or '(none — branch names no card)'}: {reason} on main "
            f"{base_sha[:12]}", "WARN")
    _stamp_waiting(state_path, project, patches)
    return sorted(patches)


# ── wait-list expiry: the queue eventually gives up ─────────────────────────
#
# Everything above KEEPS the entry: a red PR is refreshed once per base commit,
# handed back once per base commit, and goes on waiting.  Nothing decided when
# the waiting was OVER, so a PR whose author had walked away waited on nobody
# indefinitely — the same passivity that let a broken main (#716) or one flaky
# leg (#738) hold a PR for 9-18 h while the operator guessed which of the two it
# was.
#
# Two exits, both keyed on the PR's STATE and never on whether its branch still
# resolves: a merged PR's branch is deleted on merge, so the ``fatal: bad
# revision`` it provokes reads like a broken ref and is actually "this landed".
# Keying on the branch would misread the good case as a fault.
#
#   merged or closed     out of the queue immediately and SILENTLY: the queue has
#                        no claim on it and a comment there is noise.
#   still open, too old  out with one comment — how long it waited, what was
#                        still red, and the exact command to re-add it — plus a
#                        WARN line for the daemon log.
#
# A GREEN entry is exempt whatever its age: it goes back to the FIFO, because
# releasing a PR that can land now beats dropping one the operator asked for.

def waiting_budget_s(cfg):
    """Seconds an entry may stay in ``project``'s wait list (default 24 h).

    ``waiting_max_age_s`` in the registry's ``merge_queue`` block overrides it,
    so a project with slow CI can wait it out. A value that is not a positive
    number — a typo, ``0``, a ``"24h"`` string, a YAML ``true`` — falls back to
    the default rather than emptying the wait list on one bad registry line.
    """
    raw = (cfg or {}).get(WAITING_MAX_AGE_KEY)
    if raw is None or isinstance(raw, bool):
        return WAITING_MAX_AGE_S
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return WAITING_MAX_AGE_S
    return value if value > 0 else WAITING_MAX_AGE_S


def _age_span(seconds):
    """A wait-list span as one word for a log line or a PR comment."""
    seconds = int(max(0, seconds or 0))
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _pr_gone(info):
    """``"merged-elsewhere"`` / ``"closed"`` for a PR the queue no longer owns.

    A squash commit counts as merged even when ``state`` still reads OPEN:
    GitHub has answered OPEN for a PR whose merge commit already exists, and
    landing is landing. Everything else — OPEN, a draft, a view that could not be
    read — answers None: the queue keeps its claim, and a read that failed is
    simply asked again on the next tick.
    """
    state = str((info or {}).get("state") or "").upper()
    commit = (info or {}).get("mergeCommit")
    if state == "MERGED" or (isinstance(commit, dict) and commit.get("oid")):
        return "merged-elsewhere"
    if state == "CLOSED":
        return "closed"
    return None


def _waiting_expired(age_s, budget_s):
    """True when a wait-list entry has outlived ``budget_s``.

    A None age — no ``enqueued_at``, or one this ledger cannot parse — is NEVER
    expired: dropping a PR because the bookkeeping lost a timestamp turns a
    bookkeeping bug into lost work.
    """
    return age_s is not None and age_s > budget_s


def _expiry_body(project, pr, base, info, entry, age_s, budget_s):
    """What the queue says on a PR it gave up on: the wait, and the way back in.

    Same shape as the hand-back body — the fact first, the queue's own effort
    next, the action last — but the action here is one command, not a rebase.
    """
    checks = _failing_check_names(info)
    failed = f"\nStill red: {', '.join(checks)}\n" if checks else ""
    if entry.get("handed_back_main_sha"):
        tried = ("The queue already moved this branch onto the current base and "
                 "handed the card back once.")
    elif entry.get("refreshed_main_sha"):
        tried = (f"The queue already moved this branch onto `{base}` "
                 f"{str(entry['refreshed_main_sha'])[:12]} once.")
    elif entry.get("update_conflict"):
        tried = (f"The branch could not be moved onto `{base}` — `gh pr "
                 f"update-branch` refused it with a conflict.")
    else:
        tried = (f"The queue never got a green verdict on it, and never managed "
                 f"to move it onto `{base}`.")
    return (
        f"Merge queue: dropped from the merge queue after {_age_span(age_s)} "
        f"waiting.\n\n"
        f"It entered the queue at {entry.get('enqueued_at')} and its checks "
        f"never went green on `{base}` — longer than this project's "
        f"{_age_span(budget_s)} wait-list budget. It is OUT of the queue now: "
        f"the queue will not re-add it and will not gate it.\n"
        f"{failed}\n"
        f"{tried}\n"
        "Nothing here says the code is wrong. The wait list is where a PR goes "
        "while it waits for its checks, and this one stopped being waited on.\n\n"
        f"Re-add it once green:\n\n    hscc merge-queue add {project} {pr}\n")


def _drop_waiting(state_path, project, drops):
    """Remove wait-list entries and record why.  ``drops``: pr -> (result, note).

    A FRESH read under the file lock, like every other write in this module: an
    operator's ``add``/``remove`` during the ``gh`` calls above must survive, and
    an entry that has already left the wait list is not dropped a second time —
    its history belongs wherever it landed.  ``last`` holds ONE outcome by
    design, so it carries the last drop of this pass; every dropped PR gets its
    own log line and, for an expiry, its own PR comment.
    """
    if not drops:
        return []
    with _file_lock():
        doc = load_state(state_path)
        ps = _project_state(doc, project)
        remaining, gone = [], []
        for entry in ps.get("waiting") or []:
            pr = _entry_pr(entry)
            if pr is not None and pr in drops:
                gone.append((pr, entry))
            else:
                remaining.append(entry)
        if gone:
            pr, entry = gone[-1]
            result, note = drops[pr]
            ps["last"] = _last_record(result, entry, error=note)
        ps["waiting"] = remaining
        save_state(doc, state_path)
    return [pr for pr, _ in gone]


def _drain_waiting(project, cfg, *, state_path, git_runner=None,
                   gh_runner=None, log=None, handback=None):
    """Return every wait-listed PR whose checks are green to the FIFO.

    Only a green PR LEAVES the wait list here — except for the two exits of the
    sweep below, which are also the only reasons an entry is ever REMOVED without
    going somewhere: a PR GitHub says is merged or closed, and a PR still open
    past :data:`WAITING_MAX_AGE_S`.  While looking it also drives the two red
    passes above (the stale-base refresh, then the hand-back for a PR that is red
    on its own), either of which comments and stamps the entry but leaves it
    waiting.

    Runs on EVERY tick, with a cheap ``gh pr view`` per waiting entry, and does
    not require the active queue to be empty.  Before this the wait list was
    only revisited by auto-enqueue (which runs only when every FIFO is empty),
    so one non-empty queue kept green PRs waiting forever.  Entries move in
    their original wait-list order; ``enqueued_at`` still ranks them against
    the queue's existing entries.

    The daemon calls this with NO runners (``check_merge_queue`` injects none),
    so both are defaulted at the top: every ``gh``/``git`` call below — the
    view, the base probe, ``update-branch``, the hand-back comment — inherits
    from these two lines. Without them a ``None`` runner reaches ``_gh`` and
    the ``TypeError`` escapes the drain and kills the whole tick.
    """
    log = log or (lambda *a, **k: None)
    git_runner = git_runner or _default_runner
    gh_runner = gh_runner or _default_runner
    with _file_lock():
        ps = _project_state(load_state(state_path), project)
        waiting = [dict(entry) for entry in (ps.get("waiting") or [])
                   if isinstance(entry, dict)]
    if not waiting:
        return []
    base = expected_base(cfg, git_runner) or "main"
    budget_s = waiting_budget_s(cfg)
    now_s = _epoch(_now_iso())
    ready = []
    red = []
    drops = {}
    for entry in waiting:
        info = pr_view(cfg["repo"], entry.get("pr"), runner=gh_runner)
        if not isinstance(info, dict):
            continue
        pr = _entry_pr(entry)
        required = _required_for_info(cfg, base, info, git_runner, gh_runner)
        # Green FIRST, whatever its age: a PR that can land now goes back to the
        # FIFO rather than being dropped for having waited a day to get here.
        if _pr_ready(info, required_checks=required):
            ready.append(entry)
            continue
        # Then the two exits, both BEFORE the red routing below: a PR that has
        # landed, been closed, or outlived its budget must not be refreshed onto
        # a fresh base or handed back a second time on the queue's own account.
        gone = _pr_gone(info)
        if gone is not None and pr is not None:
            drops[pr] = (gone, (f"PR #{pr} merged outside the queue"
                                if gone == "merged-elsewhere"
                                else f"PR #{pr} closed without merging"))
            log(f"merge-queue {project}: PR #{pr} {gone}; dropped from the "
                "wait list (no comment — the queue has no claim on it)", "INFO")
            continue
        age_s = _age_seconds(entry.get("enqueued_at"), now_s)
        if pr is not None and _waiting_expired(age_s, budget_s):
            # The comment goes out BEFORE the ledger write: it is a gh call, and
            # every gh call in this pass happens outside the file lock so a slow
            # GitHub cannot block the CLI. A comment that fails is logged by
            # `_comment_pr` and does not hold up the drop — the drop is the point.
            _comment_pr(cfg["repo"], pr,
                        _expiry_body(project, pr, base, info, entry,
                                     age_s, budget_s),
                        gh_runner, log=log)
            drops[pr] = ("expired", f"waited {_age_span(age_s)}, "
                                    f"budget {_age_span(budget_s)}")
            log(f"merge-queue {project}: PR #{pr} dropped from the wait list "
                f"after {_age_span(age_s)} waiting (budget "
                f"{_age_span(budget_s)}); re-add with `hscc merge-queue add "
                f"{project} {pr}` once green", "WARN")
            continue
        if _waiting_own_red(entry, info):
            red.append((entry, info))
    if drops:
        _drop_waiting(state_path, project, drops)
    # Not green because its own checks are red/cancelled: the wait list used to
    # be passive about that forever. Two answers, and the SAME base sha decides
    # which, so it is probed once here and handed to both passes:
    #   red on a base it has not been tried against yet -> ONE update-branch
    #     (card 1: a red may be main's fault, not the PR's).
    #   red on THIS base, or unmovable onto it -> hand it back to the author
    #     (card 2: nothing the queue can do will change that verdict).
    # An unknown base sha answers neither: doing nothing is the read-only answer
    # and the next tick asks again.
    if red:
        sha = _remote_base_sha(cfg["repo"], base, git_runner)
        if sha:
            stale_red = []
            own_red = []
            for entry, info in red:
                reason = _own_red_reason(entry, sha)
                if reason:
                    own_red.append((entry, info, reason))
                elif _red_on_stale_base(entry, info):
                    stale_red.append(entry)
            if stale_red:
                # Bounded to one update per (PR, base sha).
                _refresh_stale_red(project, cfg, base, stale_red,
                                   state_path=state_path, git_runner=git_runner,
                                   gh_runner=gh_runner, log=log, base_sha=sha)
            if own_red:
                _hand_back_own_red(project, cfg, base, own_red,
                                   state_path=state_path, base_sha=sha,
                                   gh_runner=gh_runner, handback=handback,
                                   log=log)
    if not ready:
        return []
    ready_prs = {int(entry["pr"]) for entry in ready
                 if entry.get("pr") is not None}
    moved = []
    with _file_lock():
        doc = load_state(state_path)
        ps = _project_state(doc, project)
        remaining = []
        for entry in ps.get("waiting") or []:
            try:
                pr = int(entry.get("pr"))
            except (TypeError, ValueError):
                remaining.append(entry)
                continue
            if pr not in ready_prs:
                remaining.append(entry)
                continue
            # A CLI `add` may already have returned it to the queue; either way
            # it is no longer waiting, and must not be duplicated.
            if _find_entry(ps["queue"], pr) is None:
                ps["queue"].append(entry)
            moved.append(pr)
        ps["waiting"] = remaining
        save_state(doc, state_path)
    if moved:
        log(f"merge-queue {project}: returned green waiting PR(s) "
            f"{', '.join('#' + str(pr) for pr in moved)} to the FIFO", "INFO")
    return moved


def _capped_error(error):
    """Cap a retry error while keeping its first line and traceback tail."""
    error = str(error)
    if len(error) <= MAX_LAST_ERROR_CHARS:
        return error
    head, _, rest = error.partition("\n")
    room = MAX_LAST_ERROR_CHARS - len(head) - 1
    if room <= 0:
        return error[:MAX_LAST_ERROR_CHARS]
    return head + "\n" + rest[-room:]


def _retry_or_drop(doc, state_path, project, entry, error):
    entry["attempts"] = int(entry.get("attempts") or 0) + 1
    error = _capped_error(error)
    entry["last_error"] = error
    with _file_lock():
        fresh = load_state(state_path)
        ps = _project_state(fresh, project)
        # The entry is ours only while it is still the in-flight claim; a CLI
        # remove during processing clears that claim and must win.
        flight = ps.get("in_flight")
        mine = bool(flight and int(flight.get("pr", 0))
                    == int(entry.get("pr", 0)))
        if mine:
            ps["in_flight"] = None
            if entry["attempts"] < MAX_ERROR_ATTEMPTS:
                if _find_entry(ps["queue"], entry.get("pr")) is None:
                    ps["queue"].insert(0, entry)
                result = "retry"
            else:
                ps["last"] = {
                    "pr": entry.get("pr"),
                    "card": entry.get("card"),
                    "result": "error",
                    "at": _now_iso(),
                    "error": error,
                }
                result = "error"
        else:
            result = "removed"
        save_state(fresh, state_path)
    return {"pr": entry.get("pr"), "result": result, "error": error}


def _ci_pending_expired(since_iso):
    """True when a held pushed head has outlived the CI-pending budget."""
    since = _epoch(since_iso)
    return (since is not None
            and time.time() - since >= max(1, int(CI_PENDING_TIMEOUT_S)))


def _hold_ci_pending(state_path, project, entry, *, head_sha, base, since,
                     log):
    """Keep the claim at ``ci-pending`` and record what it is waiting on."""
    _update_flight(state_path, project, entry["pr"],
                   stage=CI_PENDING_STAGE,
                   pushed_sha=head_sha, ci_base=base, ci_since=since)
    log(f"merge-queue {project}: PR #{entry['pr']} pushed "
        f"{(head_sha or '?')[:12]}; CI pending, holding the claim", "INFO")


def _settle_pushed_head(project, cfg, entry, info, head_sha, *, base,
                        state_path, cwd, git_runner, gh_runner, handback,
                        log, ci_since=None):
    """Decide one look at a head the queue pushed.

    Green (and still the PR head) -> merge that exact SHA.  Pending -> hold the
    claim at ``ci-pending`` and look again next tick.  Red -> comment and hand
    the card back, because the local gates passed and CI is a real verdict.
    Called both right after the force-push and on every later tick while the
    claim is parked, so a pushed PR is never re-gated from scratch for CI.
    """
    pr = entry["pr"]
    since = ci_since or _now_iso()
    if not isinstance(info, dict):
        if _ci_pending_expired(since):
            _requeue_waiting(state_path, project, entry)
            log(f"merge-queue {project}: PR #{pr} CI still unknown after "
                f"{CI_PENDING_TIMEOUT_S}s and gh pr view failed; released to "
                "the wait list", "WARN")
            return {"pr": pr, "result": "ci-timeout",
                    "error": "gh pr view failed"}
        _hold_ci_pending(state_path, project, entry, head_sha=head_sha,
                         base=base, since=since, log=log)
        return {"pr": pr, "result": CI_PENDING_STAGE,
                "error": "gh pr view failed"}

    state = str(info.get("state", "")).upper()
    if state in ("MERGED", "CLOSED"):
        _finish(None, project, state_path, None, "already-" + state.lower(),
                entry, sha=info.get("headRefOid"))
        log(f"merge-queue {project}: PR #{pr} is already {state.lower()}",
            "INFO")
        return {"pr": pr, "result": "already-merged"}

    if str(info.get("headRefOid") or "") != head_sha:
        # A worker (or anyone) pushed over the queue's head while it waited:
        # the queue can no longer merge the SHA it validated.
        _requeue_waiting(state_path, project, entry)
        log(f"merge-queue {project}: PR #{pr} head moved from the pushed "
            f"{(head_sha or '?')[:12]}; released to the wait list", "WARN")
        return {"pr": pr, "result": "waiting"}

    required = _required_for_info(cfg, base, info, git_runner, gh_runner)
    verdict = _checks_verdict(info, required_checks=required)
    if verdict == "red":
        body = (
            f"Merge queue: CI is red on the pushed head {head_sha[:12]}.\n\n"
            "The project gates passed locally and the queue pushed this head, "
            "but the repository's own checks failed on the result.\n\n"
            f"{resolve_instructions(entry.get('branch'), base)}\n")
        _finish(None, project, state_path, None, "red-ci", entry,
                head_sha=head_sha)
        _handback(entry, cfg, body, handback, gh_runner, log=log)
        log(f"merge-queue {project}: PR #{pr} CI red on pushed head "
            f"{head_sha[:12]}", "WARN")
        return {"pr": pr, "result": "red-ci", "head_sha": head_sha}

    if verdict == "pending" or not _pr_ready(info, required_checks=required):
        if verdict == "pending":
            if _ci_pending_expired(since):
                _requeue_waiting(state_path, project, entry)
                log(f"merge-queue {project}: PR #{pr} CI pending for "
                    f"{CI_PENDING_TIMEOUT_S}s; released to the wait list",
                    "WARN")
                return {"pr": pr, "result": "ci-timeout",
                        "timeout_s": CI_PENDING_TIMEOUT_S}
            _hold_ci_pending(state_path, project, entry, head_sha=head_sha,
                             base=base, since=since, log=log)
            return {"pr": pr, "result": CI_PENDING_STAGE}
        if _mergeable_unknown(info):
            # Checks are green but GitHub has not recomputed mergeability yet:
            # "not decided" is still not "not ready", so keep the claim rather
            # than parking a validated, pushed head in the wait list.
            if _ci_pending_expired(since):
                _requeue_waiting(state_path, project, entry)
                log(f"merge-queue {project}: PR #{pr} mergeable still UNKNOWN "
                    f"after {CI_PENDING_TIMEOUT_S}s; released to the wait list",
                    "WARN")
                return {"pr": pr, "result": "ci-timeout",
                        "timeout_s": CI_PENDING_TIMEOUT_S}
            _hold_ci_pending(state_path, project, entry, head_sha=head_sha,
                             base=base, since=since, log=log)
            return {"pr": pr, "result": CI_PENDING_STAGE}
        # Checks are green but the PR itself is not mergeable now (draft,
        # review requested changes, ...): not a CI verdict, park it.
        _requeue_waiting(state_path, project, entry)
        log(f"merge-queue {project}: PR #{pr} not mergeable after push; "
            "released to the wait list", "WARN")
        return {"pr": pr, "result": "waiting"}

    # Last check under the lock: never merge a PR the operator removed while
    # the post-push probe (or a later tick's re-check) was in flight.
    cancel = _cancel_reason(state_path, project, entry, cfg)
    if cancel:
        return _stop_cancelled(state_path, project, entry, cancel)

    merge = _gh(["pr", "merge", str(pr), "--squash",
                 "--delete-branch", "--match-head-commit", head_sha],
                cwd, gh_runner)
    if merge is None or merge.returncode != 0:
        return _retry_or_drop(
            None, state_path, project, entry,
            f"gh pr merge failed: {_tail(merge.stderr if merge else '', 10)}")

    merged_sha = head_sha
    merged = pr_view(cfg["repo"], pr, runner=gh_runner)
    if isinstance(merged, dict) and isinstance(merged.get("mergeCommit"), dict):
        merged_sha = merged["mergeCommit"].get("oid") or merged_sha
    _finish(None, project, state_path, None, "merged", entry, sha=merged_sha,
            pushed_sha=head_sha)
    log(f"merge-queue {project}: merged PR #{pr} as {merged_sha}", "INFO")
    return {"pr": pr, "result": "merged", "sha": merged_sha}


def _resume_ci_pending(project, cfg, entry, *, state_path, git_runner=None,
                       gh_runner=None, handback=None, log=None):
    """One tick's look at a claim parked on a pending pushed-head CI."""
    log = log or (lambda *a, **k: None)
    handback = handback or default_handback
    git_runner = git_runner or _default_runner
    gh_runner = gh_runner or _default_runner
    cancel = _cancel_reason(state_path, project, entry, cfg)
    if cancel:
        return _stop_cancelled(state_path, project, entry, cancel)
    head_sha = str(entry.get("pushed_sha") or "")
    if not head_sha:
        # A claim from a version that did not record the pushed SHA cannot be
        # trusted to merge anything: release it instead of guessing.
        _requeue_waiting(state_path, project, entry)
        log(f"merge-queue {project}: PR #{entry['pr']} ci-pending claim has no "
            "pushed SHA; released to the wait list", "WARN")
        return {"pr": entry["pr"], "result": "waiting"}
    base = entry.get("ci_base") or expected_base(cfg, git_runner) or "main"
    info = pr_view_settled(cfg["repo"], entry["pr"], runner=gh_runner)
    return _settle_pushed_head(
        project, cfg, entry, info, head_sha, base=base,
        state_path=state_path, cwd=cfg["repo"], git_runner=git_runner,
        gh_runner=gh_runner, handback=handback, log=log,
        ci_since=entry.get("ci_since"))


# ── tick ────────────────────────────────────────────────────────────────────

def _repo_groups(projects, names):
    """Group project names by canonical repo path (registry order first).

    Two registry entries with the same ``repo`` (``ecofire-bc`` and
    ``ecofire-pontaje``) are ONE queue for scheduling purposes: their entries
    are ordered together and only one PR per repo is claimed per tick.
    """
    groups = {}
    order = []
    for row in projects:
        key = repo_identity(row["repo"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        if row["name"] not in groups[key]:
            groups[key].append(row["name"])
    grouped = {name for members in groups.values() for name in members}
    for name in names:
        if name not in grouped:
            key = f"project:{name}"
            groups[key] = [name]
            order.append(key)
    return order, groups


def _tick_failure(log, result, project, where, exc):
    """Announce and record a raise inside the tick.  Never raises itself.

    The tick has always been able to die on a transient — a ``gh`` probe that
    answers oddly, a kanban read that fails, a project whose registry row
    cannot be resolved — and the daemon entry point has always swallowed that
    exception into the stream file without ever writing a log line.  That is
    the reason the 2026-11-23 wedge could not be diagnosed from the daemon
    log: a raised tick and a tick that legitimately had nothing to do looked
    identical from the outside.  A raise is now named, with its traceback, at
    the moment it happens.
    """
    detail = f"{type(exc).__name__}: {exc}"
    result["errors"].append(f"{project}: {where}: {detail}")
    with contextlib.suppress(Exception):
        log(f"merge-queue {project}: {where} raised {detail}\n"
            f"{traceback.format_exc()}", "ERROR")


def _busy_reason(project, flight):
    """Why this project's FIFO head stays put: another PR owns the repo."""
    return (f"{project} is busy with PR #{flight.get('pr')} "
            f"(stage={flight.get('stage') or 'running'})")


def _inactive_reason(cfg, ps):
    """Why this project is not being processed right now, or ``None``.

    The three conditions the tick already tested (no registry entry, paused,
    disabled) were tested and then thrown away, so a queue that sat untouched
    for an hour gave the operator nothing to read.  Same test, kept this time.
    """
    if cfg is None:
        return "no registry entry — nothing to run"
    if ps.get("paused"):
        return "the project is paused"
    if not effective_enabled(cfg, ps):
        return "the queue is disabled for this project"
    return None


def _declined(log, doc, project, reason):
    """Say which queued PR was NOT claimed this tick, and why.

    A tick that passes over an eligible PR used to write nothing at all: on
    the 2026-11-23 wedge the stream ticked 30+ times with one eligible PR and
    produced zero merge-queue lines, which made a refused claim
    indistinguishable from a daemon that had stopped.  Only a project that
    actually has an entry is announced — an idle queue is the normal state and
    a 60s stream must not fill up with ``nothing to do``.
    """
    ps = _project_state(doc, project)
    head = None
    for bucket in ("queue", "waiting"):
        for cand in (ps.get(bucket) or []):
            if isinstance(cand, dict):
                head = cand
                break
        if head is not None:
            break
    if head is None:
        return
    log(f"merge-queue {project}: PR #{head.get('pr')} not claimed: {reason}",
        "DEBUG")


def tick(*, state_path=None, root=None, registry_path=None,
         git_runner=None, gh_runner=None, handback=None, auto_source=None,
         config_loader=None, log=None, now=None):
    """One daemon tick: recover, auto-enqueue, process at most one PR per repo."""
    state_path = state_path or state_file()
    log = log or (lambda *a, **k: None)
    auto_source = auto_source or _discover_ready_cards
    config_loader = config_loader or project_config
    result = {"ok": True, "projects": {}, "processed": [], "errors": []}

    with _lock:
        doc = load_state(state_path)
        projects = load_projects(registry_path)
        names = [p["name"] for p in projects]
        # A project can still be paused/disabled in state after leaving the
        # registry; keep its queue visible but do not process it.
        names.extend(n for n in doc.get("projects", {}) if n not in names)
        names = list(dict.fromkeys(names))

        # Recover a run that died mid-flight: put it back at the head.
        recovered = []
        settled = []
        for name in names:
            ps = _project_state(doc, name)
            flight = ps.get("in_flight")
            if (isinstance(flight, dict)
                    and str(flight.get("stage") or "") == CI_PENDING_STAGE):
                # A pushed head waiting on CI is NOT a dead run: its claim is
                # live and this tick re-checks it in place. Recovering it would
                # re-rebase and re-gate work that is already pushed.
                continue
            if flight:
                ps["in_flight"] = None
                if flight.get(CANCEL_KEY):
                    # The operator removed this PR while its gate was live and
                    # the run died before the gate settled the deferred removal.
                    # Requeuing it here would silently undo the removal and put
                    # the PR back on a repo that was told to stop touching it,
                    # so the mark wins over the recovery (t_7cf67395).
                    settled.append(name)
                    log(f"merge-queue {name}: PR #{flight.get('pr')} was "
                        "removed while in flight; the claim is dropped, not "
                        "requeued", "INFO")
                else:
                    if not any(int(e.get("pr", 0))
                               == int(flight.get("pr", 0))
                               for e in ps["queue"]):
                        ps["queue"].insert(0, flight)
                    recovered.append(name)
        if recovered or settled:
            save_state(doc, state_path)

        cfgs = {}
        enabled = {}
        for name in names:
            try:
                cfg = config_loader(name, registry_path)
            except Exception as exc:
                # One unreadable project config is that project's problem, not
                # the whole tick's: with no config it is simply not eligible,
                # and the reason is said out loud instead of ending the tick.
                _tick_failure(log, result, name, "project config", exc)
                cfg = None
            cfgs[name] = cfg
            ps = _project_state(doc, name)
            enabled[name] = effective_enabled(cfg, ps) if cfg is not None else False
            # No registry entry: nothing to run (state is just history).
            result["projects"][name] = _project_view(
                ps, cfg, enabled[name])

        order, groups = _repo_groups(projects, names)
        for key in order:
            members = groups[key]
            active = [n for n in members
                      if cfgs.get(n) is not None and enabled.get(n)
                      and not _project_state(doc, n).get("paused")]
            if not active:
                # Eligibility was tested and, until now, silently: a queue that
                # sat here for an hour produced nothing to read.
                for name in members:
                    _declined(
                        log, doc, name,
                        _inactive_reason(cfgs.get(name),
                                         _project_state(doc, name))
                        or "not eligible for this repo")
                continue

            # Every tick, whether or not the FIFO is empty: a PR whose checks
            # went green while it sat in `waiting` must be able to re-enter the
            # queue. Auto-enqueue alone only runs on an EMPTY FIFO, which is
            # exactly the livelock this drain removes.
            #
            # The drain is network+git work, and a raise in it used to take the
            # whole tick — including every other repo — down with it, silently.
            # One repo's drain failing now costs that repo this tick only.
            drained = True
            for name in active:
                try:
                    _drain_waiting(name, cfgs[name], state_path=state_path,
                                   git_runner=git_runner, gh_runner=gh_runner,
                                   log=log, handback=handback)
                except Exception as exc:
                    _tick_failure(log, result, name, "wait-list drain", exc)
                    drained = False
                    break
            if not drained:
                continue
            doc = load_state(state_path)

            # Auto-enqueue only when this repo's whole FIFO is empty, so a
            # shared repo never stacks new work in front of queued entries.
            if not any(_queue(doc, n) for n in active):
                candidates = {}
                for name in active:
                    try:
                        found = auto_source(cfgs[name],
                                            gh_runner=gh_runner) or []
                    except Exception as exc:
                        result["errors"].append(f"{name}: auto-enqueue: {exc}")
                        found = []
                    if found:
                        candidates[name] = found
                if candidates:
                    # Suppression probes are network reads, so do them BEFORE
                    # taking the file lock: a slow `gh` must not block the CLI.
                    snapshot = load_state(state_path)
                    allowed = {}
                    for name, found in candidates.items():
                        keep = [
                            cand for cand in found
                            if not _auto_enqueue_suppressed(
                                snapshot, name, cand, cfgs[name], gh_runner)
                        ]
                        if keep:
                            allowed[name] = keep
                    candidates = allowed
                if candidates:
                    # Fresh read under the lock: a CLI `add` that landed while
                    # auto-enqueue probed the network must survive this write.
                    with _file_lock():
                        doc = load_state(state_path)
                        for name, found in candidates.items():
                            ps = _project_state(doc, name)
                            q = ps["queue"]
                            for cand in found:
                                pr = int(cand["pr"])
                                if _find_entry(q, pr) is not None:
                                    continue
                                waiting = _find_entry(ps["waiting"], pr)
                                if waiting is not None:
                                    # It just became green: return it to the
                                    # active FIFO rather than duplicating it.
                                    ps["waiting"].remove(waiting)
                                    q.append(waiting)
                                    continue
                                q.append({
                                    "pr": pr,
                                    "card": cand.get("card"),
                                    "assignee": cand.get("assignee"),
                                    "branch": cand.get("branch"),
                                    "enqueued_at": _now_iso(),
                                    "attempts": 0,
                                })
                        save_state(doc, state_path)

            # A pushed head whose CI is still pending KEEPS its in-flight claim
            # (stage `ci-pending`) and is re-checked here, in place, on every
            # tick. It is not released to `waiting` and re-gated later, and it
            # is not "recovered" as a dead run.
            ci_claim = None
            with _file_lock():
                doc = load_state(state_path)
                for idx, name in enumerate(members):
                    cfg = cfgs.get(name)
                    ps = _project_state(doc, name)
                    if (cfg is None or not effective_enabled(cfg, ps)
                            or ps.get("paused")):
                        continue
                    flight = ps.get("in_flight")
                    if (isinstance(flight, dict)
                            and str(flight.get("stage") or "")
                            == CI_PENDING_STAGE):
                        ci_claim = (idx, name, dict(flight))
                        break
            if ci_claim is not None:
                _, claim_name, entry = ci_claim
                cfg = cfgs[claim_name]
                _stream_stamp(project=claim_name, pr=entry["pr"],
                              stage=CI_PENDING_STAGE)
                lock_root = os.path.dirname(os.path.abspath(state_path))
                with _repo_lock(cfg["repo"], root=lock_root):
                    try:
                        outcome = _resume_ci_pending(
                            claim_name, cfg, entry, state_path=state_path,
                            git_runner=git_runner, gh_runner=gh_runner,
                            handback=handback, log=log)
                    except Exception as exc:
                        error = (f"{type(exc).__name__}: {exc}\n"
                                 f"{traceback.format_exc()}")
                        log(f"merge-queue {claim_name}: PR #{entry['pr']} "
                            f"raised:\n{error}", "ERROR")
                        outcome = _retry_or_drop(
                            None, state_path, claim_name, entry, error)
                result["processed"].append({"project": claim_name, **outcome})
                log(f"merge-queue {claim_name}: PR #{entry['pr']} -> "
                    f"{outcome.get('result')}")
                doc = load_state(state_path)
                for name in members:
                    ps = _project_state(doc, name)
                    member_cfg = cfgs.get(name)
                    result["projects"][name] = _project_view(
                        ps, member_cfg,
                        effective_enabled(member_cfg, ps)
                        if member_cfg else False)
                    # One PR per repo per tick: whatever is queued behind the
                    # pushed head waits, and says so.
                    _declined(log, doc, name,
                              f"{claim_name} holds the claim "
                              f"(PR #{entry['pr']}, "
                              f"stage={CI_PENDING_STAGE})")
                continue

            # Claim the oldest entry across every project on this repo, under
            # the lock and on a FRESH read, so an operator's `add` between our
            # earlier save and this claim is not lost. Re-reading enabled and
            # paused also makes a pause/disable raced in during discovery win.
            with _file_lock():
                doc = load_state(state_path)
                best = None
                source = "queue"
                # A claim can also be live in ANOTHER process (a CLI or API
                # tick against the same ledger), which the recovery above
                # cannot see. One PR per repo per tick holds here too — and the
                # entry that has to wait is named instead of passed over.
                busy = {}
                for name in members:
                    flight = _project_state(doc, name).get("in_flight")
                    if (isinstance(flight, dict)
                            and flight.get("pr") is not None):
                        busy[name] = flight
                for idx, name in enumerate(members):
                    cfg = cfgs.get(name)
                    ps = _project_state(doc, name)
                    if (cfg is None or not effective_enabled(cfg, ps)
                            or ps.get("paused")):
                        continue
                    if name in busy:
                        _declined(log, doc, name,
                                  _busy_reason(name, busy[name]))
                        continue
                    for pos, entry in enumerate(ps.get("queue") or []):
                        rank = (str(entry.get("enqueued_at") or ""), idx, pos)
                        if best is None or rank < best[0]:
                            best = (rank, name, pos, entry)
                if best is None:
                    # The active FIFO is empty: retry one wait-listed PR. They
                    # are appended on the way in, so this round-robins instead
                    # of letting one permanently non-green PR starve the rest.
                    source = "waiting"
                    for idx, name in enumerate(members):
                        cfg = cfgs.get(name)
                        ps = _project_state(doc, name)
                        if (cfg is None or not effective_enabled(cfg, ps)
                                or ps.get("paused")):
                            continue
                        if name in busy:
                            _declined(log, doc, name,
                                      _busy_reason(name, busy[name]))
                            continue
                        for pos, entry in enumerate(ps.get("waiting") or []):
                            rank = (str(entry.get("enqueued_at") or ""), idx,
                                    pos)
                            if best is None or rank < best[0]:
                                best = (rank, name, pos, entry)
                if best is None:
                    continue
                _, claim_name, pos, entry = best
                ps = _project_state(doc, claim_name)
                entry = ps[source].pop(pos)
                ps["in_flight"] = entry
                save_state(doc, state_path)

            # The claim itself is a stage worth stamping: from this instant the
            # tick may spend the whole gate budget before it returns, and the
            # stream must not sit dark across it.  "claimed" until process_entry
            # reports a real stage of its own.
            _stream_stamp(project=claim_name, pr=entry["pr"], stage="claimed")

            cfg = cfgs[claim_name]
            # One lock per repo path: two project names on the same checkout
            # can never run a rebase/gate/push concurrently.
            lock_root = os.path.dirname(os.path.abspath(state_path))
            with _repo_lock(cfg["repo"], root=lock_root):
                try:
                    outcome = process_entry(
                        claim_name, cfg, entry, doc=doc,
                        state_path=state_path, root=root,
                        git_runner=git_runner, gh_runner=gh_runner,
                        handback=handback, log=log)
                except Exception as exc:
                    error = (f"{type(exc).__name__}: {exc}\n"
                             f"{traceback.format_exc()}")
                    log(f"merge-queue {claim_name}: PR #{entry['pr']} "
                        f"raised:\n{error}", "ERROR")
                    outcome = _retry_or_drop(
                        doc, state_path, claim_name, entry, error)
            result["processed"].append({"project": claim_name, **outcome})
            log(f"merge-queue {claim_name}: PR #{entry['pr']} -> "
                f"{outcome.get('result')}")
            doc = load_state(state_path)
            for name in members:
                ps = _project_state(doc, name)
                member_cfg = cfgs.get(name)
                result["projects"][name] = _project_view(
                    ps, member_cfg,
                    effective_enabled(member_cfg, ps) if member_cfg else False)

        result["recovered"] = recovered
        # A tick that does no work is still a tick: stamp the ledger so the
        # surfaces can tell a live-but-idle daemon from a dead one.
        with _file_lock():
            doc = load_state(state_path)
            save_state(doc, state_path)
            result["generated_at"] = doc.get("generated_at")
    return result


def _project_view(ps, cfg, enabled=None):
    return {
        "enabled": (effective_enabled(cfg, ps) if enabled is None
                    else enabled),
        "enabled_source": enabled_source(cfg, ps),
        "default_enabled": bool((cfg or {}).get("default_enabled")),
        "default_reason": (cfg or {}).get("default_reason"),
        "paused": bool(ps.get("paused")),
        "queue": list(ps.get("queue") or []),
        "waiting": list(ps.get("waiting") or []),
        "in_flight": ps.get("in_flight"),
        "last": ps.get("last"),
        "repo": (cfg or {}).get("repo"),
        "gates": (cfg or {}).get("gates"),
    }


def _daemon_log(msg, level="INFO"):
    """Write one merge-queue line to the daemon log. Never raises.

    ``tick()`` defaults ``log`` to a no-op so an injected runner/test tick never
    writes the operator's log; the daemon's own entry point
    (:func:`check_merge_queue`) passes this instead, which is why the log used
    to have zero merge-queue lines. The path follows ``HSCC_HOME`` at call time,
    so a temp instance logs to its own root.
    """
    try:
        from . import daemon_ops
        root = _hscc_root()
        os.makedirs(root, exist_ok=True)
        daemon_ops.log(msg, level,
                       log_file=os.path.join(root, "daemon.log"),
                       pid_file=os.path.join(root, "daemon.pid"))
    except Exception:
        pass


def check_merge_queue():
    """Daemon stream entry: run one tick and stamp ``state/merge_queue.json``.

    Never raises. With no project enabled this is a cheap no-op that still
    stamps, so ``hscc verify`` never reports the stream dark.

    The stamp is NOT only this final write: ``tick()`` mirrors its claim and
    every stage transition onto the same file, and :class:`_GateLiveness`
    heartbeats it around each gate leg.  That is the whole point — a tick
    holding a claim can run for the full gate budget, and a stamp written only
    here would be hours late on a runner that is alive.
    """
    from .state import write_state
    try:
        result = tick(log=_daemon_log)
    except Exception as exc:
        # The stream is still stamped and the daemon goes on, but the raise
        # itself must reach the log.  Until now a tick that died here looked
        # exactly like a tick that had nothing to do: a fresh stream file and
        # zero merge-queue lines (t_7cf67395).  The verdict is unchanged —
        # ``ok`` stays False for the surfaces that read it.
        _daemon_log(f"merge-queue: tick raised {type(exc).__name__}: {exc}\n"
                    f"{traceback.format_exc()}", "ERROR")
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        write_state(STREAM_NAME, result)
    except Exception:
        pass
    return result
