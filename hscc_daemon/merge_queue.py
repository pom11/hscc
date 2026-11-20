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

The queue is **on by default** for a project that has all three of: a git
checkout at its registry ``repo``, a ``github.com`` ``origin`` remote, and
declared gates (``merge_queue.gates``) or hscc's built-in gates::

    merge_queue:
      # Optional explicit override; omit to follow the default-on rule.
      # enabled: false
      gates: ["python3 scripts/address_guard.py --tracked", "bash scripts/run_tests.sh"]
      timeout_s: 1800

A project missing one of the three stays off and ``hscc merge-queue status``
prints why (``no repo`` / ``no GitHub remote`` / ``no gates``). The ``hscc``
project gets its own defaults (address guard + pytest legs, plus
``build_check.sh`` for whichever app tree the PR touched) when the registry
entry does not declare gates. An explicit ``enable``/``disable`` from the CLI
is stored in the ledger and always wins over the default.

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
import subprocess
import tempfile
import threading
import time
import urllib.parse

STREAM_NAME = "merge_queue"
STATE_VERSION = 1

DEFAULT_GATE_TIMEOUT_S = 1800
DEFAULT_PROCESS_TIMEOUT_S = 120
MAX_GATE_TAIL_LINES = 40
MAX_ERROR_ATTEMPTS = 3

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

#: hscc's own default gates. The app build legs are appended only when the PR
#: actually touched that tree (see :func:`_resolve_gates`).
DEFAULT_HSCC_GATES = (
    "python3 scripts/address_guard.py --tracked",
    "bash scripts/run_tests.sh",
)
APP_BUILD_GATES = {
    "ios-app/": "bash ios-app/scripts/build_check.sh",
    "macos-app/": "bash macos-app/scripts/build_check.sh",
}

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
    """Remove a PR from ``project``'s queue (and any in-flight claim)."""
    with _lock, _file_lock():
        doc = state if state is not None else load_state(path)
        ps = _project_state(doc, project)
        ps["queue"] = [e for e in ps["queue"]
                       if int(e.get("pr", 0)) != int(pr)]
        ps["waiting"] = [e for e in ps["waiting"]
                         if int(e.get("pr", 0)) != int(pr)]
        if ps.get("in_flight") and int(ps["in_flight"].get("pr", 0)) == int(pr):
            ps["in_flight"] = None
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
#: ledger outcomes; ``rebasing``/``gating``/``merging`` are the live stages the
#: daemon stamps on its in-flight claim as it moves through the queue.
MERGE_QUEUE_STATES = (
    "queued", "waiting", "rebasing", "gating", "merging", "resolve", "merged",
    "failed",
)

_OUTCOME_STATES = {
    "merged": "merged",
    "already-merged": "merged",
    "conflict": "resolve",
    "red-gate": "resolve",
    "refused": "failed",
    "error": "failed",
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


def _resolve_gates(cfg, worktree, git_runner, base="main"):
    """Gate command list for this PR, including hscc's conditional app builds."""
    declared = cfg.get("gates")
    if declared is not None:
        return [str(g) for g in declared]
    if cfg.get("name") != "hscc":
        return []
    gates = list(DEFAULT_HSCC_GATES)
    changed = _changed_files(worktree, git_runner, base=base)
    for prefix, cmd in APP_BUILD_GATES.items():
        if any(f.startswith(prefix) for f in changed):
            gates.append(cmd)
    return gates


def _changed_files(worktree, git_runner, base="main"):
    proc = _git(["diff", "--name-only", f"origin/{base}..HEAD"], worktree,
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


def _run_gate(command, worktree, timeout_s):
    """Run one shell gate; returns (ok, output)."""
    try:
        proc = subprocess.run(command, shell=True, cwd=worktree,
                              capture_output=True, text=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        out = ((exc.stdout or "") if isinstance(exc.stdout, str) else "")
        err = ((exc.stderr or "") if isinstance(exc.stderr, str) else "")
        return False, _tail(f"{out}\n{err}\ntimeout after {timeout_s}s")
    output = ((proc.stdout or "") + (proc.stderr or ""))
    return proc.returncode == 0, _tail(output)


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


def _comment_pr(repo, pr, body, gh_runner):
    try:
        _gh(["pr", "comment", str(pr), "--body", body], repo, gh_runner)
    except Exception:
        pass


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
    """
    with _file_lock():
        doc = load_state(state_path)
        flight = _project_state(doc, project).get("in_flight")
        if flight and int(flight.get("pr", 0)) == int(pr):
            flight.update(fields)
            save_state(doc, state_path)


def _handback(entry, cfg, body, handback, gh_runner):
    _comment_pr(cfg["repo"], entry["pr"], body, gh_runner)
    try:
        handback(entry.get("card"), entry.get("assignee"), body)
    except Exception:
        pass


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
        ps["last"] = {
            "pr": entry.get("pr"),
            "card": entry.get("card"),
            "branch": entry.get("branch"),
            "result": result,
            "at": _now_iso(),
            **fields,
        }
        save_state(doc, state_path)


def _refuse_entry(doc, state_path, project, entry, reason, cfg, gh_runner):
    """Record a terminal refusal (base/fork) and say why on the PR."""
    body = ("Merge queue: refusing this PR.\n\n"
            f"{reason}\n\n"
            "It stays out of the queue until it is retargeted (base branch) "
            "or re-opened from a branch in this repo.\n")
    _comment_pr(cfg["repo"], entry["pr"], body, gh_runner)
    _finish(doc, project, state_path, None, "refused", entry, error=reason)
    return {"pr": entry["pr"], "result": "refused", "error": reason}


def _cancel_reason(state_path, project, entry, cfg):
    """Why this claim must stop now, or None when it may proceed.

    A ``remove`` clears the claim; ``pause``/``disable`` leave it but change the
    project. All three can land while a rebase or a gate is running, so this is
    a FRESH read under the file lock — never the tick's older snapshot.
    """
    with _file_lock():
        doc = load_state(state_path)
        ps = _project_state(doc, project)
        flight = ps.get("in_flight")
        if not (flight
                and int(flight.get("pr", 0)) == int(entry.get("pr", 0))):
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


def _post_push_ready(cfg, base, after, head_sha, git_runner, gh_runner):
    """True only when the pushed head itself is green and still the PR head."""
    if not isinstance(after, dict):
        return False
    if str(after.get("headRefOid") or "") != head_sha:
        return False
    required = _required_for_info(cfg, base, after, git_runner, gh_runner)
    return _pr_ready(after, required_checks=required)


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
    info = pr_view(cfg["repo"], entry["pr"], runner=gh_runner)
    if info is None:
        return _retry_or_drop(doc, state_path, project, entry, "gh pr view failed")
    if info.get("headRefName"):
        entry["branch"] = info["headRefName"]
    state = str(info.get("state", "")).upper()
    if state in ("MERGED", "CLOSED"):
        _finish(doc, project, state_path, ps, "already-" + state.lower(),
                entry, sha=info.get("headRefOid"))
        return {"pr": entry["pr"], "result": "already-merged"}
    reason = refusal_reason(info, base)
    if reason:
        # Terminal: a fork or wrong-base PR is not transient, so retrying (and
        # auto-enqueue re-adding it) can never make it mergeable here.
        log(f"merge-queue {project}: PR #{entry['pr']} refused: {reason}",
            "WARN")
        return _refuse_entry(doc, state_path, project, entry, reason, cfg,
                             gh_runner)
    required = _required_for_info(cfg, base, info, git_runner, gh_runner)
    if not _pr_ready(info, required_checks=required):
        # Not green yet: leave it at the head of the queue, no handback.
        _requeue_waiting(state_path, project, entry)
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
        _handback(entry, cfg, body, handback, gh_runner)
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
        passed, output = _run_gate(command, worktree, timeout_s)
        if not passed:
            body = (f"Merge queue: gate failed after rebasing onto "
                    f"origin/{base}.\n\n"
                    f"Gate: {command}\n\n"
                    f"{resolve_instructions(info.get('headRefName'), base)}\n"
                    f"{output}")
            _finish(doc, project, state_path, ps, "red-gate", entry,
                    gate=command, head_sha=info.get("headRefOid"))
            _handback(entry, cfg, body, handback, gh_runner)
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
    after = pr_view(cfg["repo"], entry["pr"], runner=gh_runner)
    cancel = _cancel_reason(state_path, project, entry, cfg)
    if cancel:
        return _stop_cancelled(state_path, project, entry, cancel)
    if not _post_push_ready(cfg, base, after, head_sha, git_runner, gh_runner):
        _requeue_waiting(state_path, project, entry)
        log(f"merge-queue {project}: PR #{entry['pr']} not green on the "
            "pushed head; will re-check next tick", "WARN")
        return {"pr": entry["pr"], "result": "waiting"}

    # Last check under the lock: never merge a PR the operator removed while
    # the post-push probe was in flight.
    cancel = _cancel_reason(state_path, project, entry, cfg)
    if cancel:
        return _stop_cancelled(state_path, project, entry, cancel)

    merge = _gh(["pr", "merge", str(entry["pr"]), "--squash",
                 "--delete-branch", "--match-head-commit", head_sha],
                worktree, gh_runner)
    if merge is None or merge.returncode != 0:
        return _retry_or_drop(
            doc, state_path, project, entry,
            f"gh pr merge failed: {_tail(merge.stderr if merge else '', 10)}")

    merged_sha = head_sha
    merged = pr_view(cfg["repo"], entry["pr"], runner=gh_runner)
    if isinstance(merged, dict) and isinstance(merged.get("mergeCommit"), dict):
        merged_sha = merged["mergeCommit"].get("oid") or merged_sha
    _finish(doc, project, state_path, ps, "merged", entry, sha=merged_sha,
            pushed_sha=head_sha)
    log(f"merge-queue {project}: merged PR #{entry['pr']} as {merged_sha}")
    return {"pr": entry["pr"], "result": "merged", "sha": merged_sha}


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


def _retry_or_drop(doc, state_path, project, entry, error):
    entry["attempts"] = int(entry.get("attempts") or 0) + 1
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
        for name in names:
            ps = _project_state(doc, name)
            flight = ps.get("in_flight")
            if flight:
                ps["in_flight"] = None
                if not any(int(e.get("pr", 0)) == int(flight.get("pr", 0))
                           for e in ps["queue"]):
                    ps["queue"].insert(0, flight)
                recovered.append(name)
        if recovered:
            save_state(doc, state_path)

        cfgs = {}
        enabled = {}
        for name in names:
            cfg = config_loader(name, registry_path)
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
                continue

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

            # Claim the oldest entry across every project on this repo, under
            # the lock and on a FRESH read, so an operator's `add` between our
            # earlier save and this claim is not lost. Re-reading enabled and
            # paused also makes a pause/disable raced in during discovery win.
            with _file_lock():
                doc = load_state(state_path)
                best = None
                source = "queue"
                for idx, name in enumerate(members):
                    cfg = cfgs.get(name)
                    ps = _project_state(doc, name)
                    if (cfg is None or not effective_enabled(cfg, ps)
                            or ps.get("paused")):
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
                    outcome = _retry_or_drop(
                        doc, state_path, claim_name, entry, str(exc))
            result["processed"].append({"project": claim_name, **outcome})
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


def check_merge_queue():
    """Daemon stream entry: run one tick and stamp ``state/merge_queue.json``.

    Never raises. With no project enabled this is a cheap no-op that still
    stamps, so ``hscc verify`` never reports the stream dark.
    """
    from .state import write_state
    try:
        result = tick()
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        write_state(STREAM_NAME, result)
    except Exception:
        pass
    return result
