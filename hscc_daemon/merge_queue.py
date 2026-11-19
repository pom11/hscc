"""Local merge queue for project PRs (MQ1).

The daemon owns a FIFO of green PRs per project. One PR is processed per
project per tick, in a dedicated git worktree under
``~/.hscc/merge-queue/<project>/`` — never the operator's checkout:

    fetch -> rebase PR head onto origin/main -> project gates ->
    push --force-with-lease -> gh pr merge --squash --delete-branch

A conflict or a red gate aborts cleanly, comments on the PR, hands the card
back to its worker as a resolve step, and drops the PR from the queue.
Everything the queue needs to survive a daemon restart is persisted in
``~/.hscc/merge-queue-state.json`` (atomic replace + fsync); a run that dies
mid-flight is recovered to the head of its project's queue on the next tick.

Opt-in is per project through the flightdeck registry::

    merge_queue:
      enabled: false
      gates: ["python3 scripts/address_guard.py --tracked", "bash scripts/run_tests.sh"]
      timeout_s: 1800

The ``hscc`` project gets its own defaults (address guard + pytest legs, plus
``build_check.sh`` for whichever app tree the PR touched) when the registry
entry does not declare gates.

This module is deliberately a plain git + gh implementation: no GitHub org,
no GitHub merge queue. Tests inject the ``git``/``gh`` runners and use a
throwaway bare origin + clone, so no test touches a real repository.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import re
import subprocess
import tempfile
import threading
import time

STREAM_NAME = "merge_queue"
STATE_VERSION = 1

DEFAULT_GATE_TIMEOUT_S = 1800
DEFAULT_PROCESS_TIMEOUT_S = 120
MAX_GATE_TAIL_LINES = 40
MAX_ERROR_ATTEMPTS = 3

#: The PR URL shape workers leave in a card comment (same shape Hermes'
#: respawn guard recognises).
PR_URL_RE = re.compile(
    r"https?://github\.com/[^/\s]+/[^/\s]+/pull/([1-9][0-9]*)")

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
    "url,statusCheckRollup,mergeCommit"
)

_lock = threading.RLock()


# ── paths ───────────────────────────────────────────────────────────────────

def state_file():
    """Queue ledger path (atomic writer; survives daemon restarts)."""
    return os.path.expanduser("~/.hscc/merge-queue-state.json")


def queue_root():
    """Root of the per-project queue worktrees."""
    return os.path.expanduser("~/.hscc/merge-queue")


def worktree_path(project):
    """The queue worktree for ``project`` (never the operator's checkout)."""
    safe = str(project).strip()
    if not safe or safe in (".", "..") or os.sep in safe:
        raise ValueError(f"invalid project name for a worktree: {project!r}")
    return os.path.join(queue_root(), safe)


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
    _atomic_write_json(path or state_file(), doc)


@contextlib.contextmanager
def _file_lock(path=None):
    """Cross-process lock so CLI writes cannot race the daemon tick."""
    path = path or os.path.expanduser("~/.hscc/merge-queue.lock")
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
    ps.setdefault("enabled", None)       # None = follow the registry opt-in
    ps.setdefault("paused", False)
    ps.setdefault("queue", [])
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
        q = _queue(doc, project)
        existing = _find_entry(q, pr)
        if existing is None:
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
    """The effective queue config from one ``load_projects`` row."""
    raw = row.get("merge_queue")
    cfg = dict(raw) if isinstance(raw, dict) else {}
    cfg["name"] = row["name"]
    cfg["repo"] = row["repo"]
    cfg["board"] = row.get("board")
    cfg["enabled"] = bool(cfg.get("enabled", False))
    return cfg


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
    "queued", "rebasing", "gating", "merging", "resolve", "merged", "failed",
)

_OUTCOME_STATES = {
    "merged": "merged",
    "already-merged": "merged",
    "conflict": "resolve",
    "red-gate": "resolve",
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
        enabled = (bool((cfg or {}).get("enabled", False))
                   if ps.get("enabled") is None else bool(ps.get("enabled")))
        last = ps.get("last") if isinstance(ps.get("last"), dict) else None
        rows = []
        shown = set()
        queue = list(ps.get("queue") or [])
        for position, entry in enumerate(queue, 1):
            rows.append(_row(name, "queued", entry=entry, position=position,
                             now_s=now_s))
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
            "paused": bool(ps.get("paused")),
            "repo": (cfg or {}).get("repo"),
            "queue_len": len(queue),
            "rows": rows,
            "last": last,
        }
        all_rows.extend(rows)
    return {
        "ok": True,
        "generated_at": _now_iso(),
        "projects": out,
        "rows": all_rows,
        "count": len(all_rows),
        "queued": sum(1 for row in all_rows if row["state"] == "queued"),
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


def _resolve_gates(cfg, worktree, git_runner):
    """Gate command list for this PR, including hscc's conditional app builds."""
    declared = cfg.get("gates")
    if declared is not None:
        return [str(g) for g in declared]
    if cfg.get("name") != "hscc":
        return []
    gates = list(DEFAULT_HSCC_GATES)
    changed = _changed_files(worktree, git_runner)
    for prefix, cmd in APP_BUILD_GATES.items():
        if any(f.startswith(prefix) for f in changed):
            gates.append(cmd)
    return gates


def _changed_files(worktree, git_runner):
    proc = _git(["diff", "--name-only", "origin/main..HEAD"], worktree,
                git_runner, timeout=DEFAULT_PROCESS_TIMEOUT_S)
    if proc is None or proc.returncode != 0:
        return []
    return [line.strip() for line in (proc.stdout or "").splitlines()
            if line.strip()]


# ── process helpers ─────────────────────────────────────────────────────────

def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _default_runner(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


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


def _checks_green(rollup):
    """True when every reported check is green (an empty rollup counts green)."""
    if not rollup:
        return True
    if not isinstance(rollup, list):
        return False
    for entry in rollup:
        verdict = _truthy_check(entry)
        if verdict is not True:
            return False
    return True


def _pr_ready(info):
    """Open, non-draft, mergeable, checks green."""
    if not isinstance(info, dict):
        return False
    return (
        str(info.get("state", "")).upper() == "OPEN"
        and not bool(info.get("isDraft"))
        and str(info.get("mergeable", "")).upper() == "MERGEABLE"
        and _checks_green(info.get("statusCheckRollup"))
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

def ensure_worktree(cfg, root=None, git_runner=None):
    """Ensure the project's queue worktree exists at ``queue_root()/<name>``."""
    git_runner = git_runner or _default_runner
    repo = cfg["repo"]
    path = (os.path.join(root, cfg["name"]) if root
            else worktree_path(cfg["name"]))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(
            os.path.join(path, ".git")):
        return path

    _git(["fetch", "origin", "--prune"], repo, git_runner)
    proc = _git(["worktree", "add", "--detach", path, "origin/main"],
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


def _rebase_onto_main(worktree, git_runner):
    """Real ``git rebase origin/main``; returns (ok, conflicted files, output)."""
    proc = _git(["rebase", "origin/main"], worktree, git_runner,
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


def resolve_instructions(branch):
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
        "origin/main`.\n"
        "2. Fix the conflicted files / the failing gate, then re-run the "
        "gate until it is green.\n"
        f"3. Push back to YOUR card branch only: `git push --force-with-lease "
        f"origin {target}`.\n"
        "   Never force-push main or any other branch — the pre-push guard "
        "refuses it.\n"
        "4. Comment the result on the card and hand it back for review.\n"
    )


# ── auto-enqueue ────────────────────────────────────────────────────────────

def _discover_ready_cards(cfg, gh_runner=None, kanban_db=None):
    """Cards on the project board whose latest PR URL is ready to merge.

    Read-only: enumerates the board through the SAME seam autodown uses and
    reads comments through ``board_pin.open_board_ro``. Returns
    ``[{pr, card, assignee}]``.
    """
    board = cfg.get("board")
    if not board:
        return []
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
            rows = conn.execute(
                "SELECT id, assignee, body FROM tasks "
                "WHERE status NOT IN ('done', 'archived')").fetchall()
            found = []
            for row in rows:
                comments = conn.execute(
                    "SELECT body FROM task_comments WHERE task_id = ? "
                    "ORDER BY id DESC LIMIT 20", (row["id"],)).fetchall()
                pr = None
                for text in [row["body"] or ""] + [
                        c["body"] or "" for c in comments]:
                    match = PR_URL_RE.search(text)
                    if match:
                        pr = int(match.group(1))
                        break
                if pr is not None:
                    found.append((row["id"], row["assignee"], pr))
    except Exception:
        return []

    ready = []
    seen = set()
    for card, assignee, pr in found:
        if pr in seen:
            continue
        info = pr_view(cfg["repo"], pr, runner=gh_runner)
        if _pr_ready(info):
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
    if not _pr_ready(info):
        # Not green yet: leave it at the head of the queue, no handback.
        _requeue_waiting(state_path, project, entry)
        return {"pr": entry["pr"], "result": "waiting"}

    _update_flight(state_path, project, entry["pr"], stage="rebasing",
                   branch=entry.get("branch"))
    try:
        worktree = ensure_worktree(cfg, root=root, git_runner=git_runner)
    except Exception as exc:
        return _retry_or_drop(doc, state_path, project, entry, str(exc))

    err = _prepare_head(worktree, info, git_runner)
    if err:
        return _retry_or_drop(doc, state_path, project, entry, err)

    ok, conflicted, output = _rebase_onto_main(worktree, git_runner)
    if not ok:
        names = ", ".join(conflicted) if conflicted else "(files not reported)"
        body = (f"Merge queue: rebase onto origin/main conflicted.\n\n"
                f"Conflicted files: {names}\n\n"
                f"{resolve_instructions(info.get('headRefName'))}\n"
                f"{output}")
        _finish(doc, project, state_path, ps, "conflict", entry,
                conflicted=conflicted)
        _handback(entry, cfg, body, handback, gh_runner)
        log(f"merge-queue {project}: PR #{entry['pr']} conflict in {names}",
            "WARN")
        return {"pr": entry["pr"], "result": "conflict",
                "conflicted": conflicted}

    gates = _resolve_gates(cfg, worktree, git_runner)
    _update_flight(state_path, project, entry["pr"],
                   stage="gating" if gates else "merging")
    timeout_s = int(cfg.get("timeout_s") or DEFAULT_GATE_TIMEOUT_S)
    for command in gates:
        passed, output = _run_gate(command, worktree, timeout_s)
        if not passed:
            body = (f"Merge queue: gate failed after rebasing onto origin/main.\n\n"
                    f"Gate: {command}\n\n"
                    f"{resolve_instructions(info.get('headRefName'))}\n"
                    f"{output}")
            _finish(doc, project, state_path, ps, "red-gate", entry,
                    gate=command)
            _handback(entry, cfg, body, handback, gh_runner)
            log(f"merge-queue {project}: PR #{entry['pr']} gate failed: "
                f"{command}", "WARN")
            return {"pr": entry["pr"], "result": "red-gate",
                    "gate": command}

    _update_flight(state_path, project, entry["pr"], stage="merging")
    err = _push_branch(worktree, info, git_runner)
    if err:
        return _retry_or_drop(doc, state_path, project, entry, err)

    sha = _git(["rev-parse", "HEAD"], worktree, git_runner)
    head_sha = (sha.stdout or "").strip() if sha is not None else ""
    merge = _gh(["pr", "merge", str(entry["pr"]), "--squash",
                 "--delete-branch"], worktree, gh_runner)
    if merge is None or merge.returncode != 0:
        return _retry_or_drop(
            doc, state_path, project, entry,
            f"gh pr merge failed: {_tail(merge.stderr if merge else '', 10)}")

    merged_sha = head_sha
    after = pr_view(cfg["repo"], entry["pr"], runner=gh_runner)
    if isinstance(after, dict) and isinstance(after.get("mergeCommit"), dict):
        merged_sha = after["mergeCommit"].get("oid") or merged_sha
    _finish(doc, project, state_path, ps, "merged", entry, sha=merged_sha,
            pushed_sha=head_sha)
    log(f"merge-queue {project}: merged PR #{entry['pr']} as {merged_sha}")
    return {"pr": entry["pr"], "result": "merged", "sha": merged_sha}


def _requeue_waiting(state_path, project, entry):
    """Put a not-yet-green entry back at the head, on a fresh ledger read."""
    with _file_lock():
        doc = load_state(state_path)
        ps = _project_state(doc, project)
        flight = ps.get("in_flight")
        if flight and int(flight.get("pr", 0)) == int(entry.get("pr", 0)):
            ps["in_flight"] = None
            if _find_entry(ps["queue"], entry.get("pr")) is None:
                ps["queue"].insert(0, entry)
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

def _enabled(cfg, ps):
    override = ps.get("enabled")
    return bool(cfg.get("enabled", False) if override is None else override)


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

        for name in names:
            ps = _project_state(doc, name)
            cfg = config_loader(name, registry_path)
            if cfg is None:
                # No registry entry: nothing to run (state is just history).
                result["projects"][name] = _project_view(ps, None)
                continue
            enabled = _enabled(cfg, ps)
            result["projects"][name] = _project_view(ps, cfg, enabled)
            if not enabled or ps.get("paused"):
                continue

            if not ps["queue"]:
                try:
                    candidates = auto_source(cfg, gh_runner=gh_runner) or []
                except Exception as exc:
                    result["errors"].append(f"{name}: auto-enqueue: {exc}")
                    candidates = []
                for cand in candidates:
                    if _find_entry(ps["queue"], cand["pr"]) is None:
                        ps["queue"].append({
                            "pr": int(cand["pr"]),
                            "card": cand.get("card"),
                            "assignee": cand.get("assignee"),
                            "branch": cand.get("branch"),
                            "enqueued_at": _now_iso(),
                            "attempts": 0,
                        })
                if candidates:
                    save_state(doc, state_path)

            if not ps["queue"]:
                continue

            # Claim under the cross-process lock on a FRESH read, so an
            # operator's `add` between our earlier save and this claim is not
            # lost when we write the in-flight entry.
            with _file_lock():
                doc = load_state(state_path)
                ps = _project_state(doc, name)
                if not ps["queue"]:
                    continue
                entry = ps["queue"].pop(0)
                ps["in_flight"] = entry
                save_state(doc, state_path)
            try:
                outcome = process_entry(
                    name, cfg, entry, doc=doc, state_path=state_path,
                    root=root, git_runner=git_runner, gh_runner=gh_runner,
                    handback=handback, log=log)
            except Exception as exc:
                outcome = _retry_or_drop(doc, state_path, name, entry, str(exc))
            result["processed"].append({"project": name, **outcome})
            doc = load_state(state_path)
            result["projects"][name] = _project_view(
                _project_state(doc, name), cfg, enabled)

        result["recovered"] = recovered
        result["generated_at"] = _now_iso()
    return result


def _project_view(ps, cfg, enabled=None):
    return {
        "enabled": bool(cfg.get("enabled", False)) if enabled is None else enabled,
        "paused": bool(ps.get("paused")),
        "queue": list(ps.get("queue") or []),
        "in_flight": ps.get("in_flight"),
        "last": ps.get("last"),
        "repo": (cfg or {}).get("repo"),
        "gates": (cfg or {}).get("gates"),
    }


def check_merge_queue():
    """Daemon stream entry: run one tick and stamp ``state/merge_queue.json``.

    Never raises. With no project opted in this is a cheap no-op that still
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
