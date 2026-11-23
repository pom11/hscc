"""`hscc clean`: archive finished card workspaces, then clean rebuildable caches.

Engine only. The workspace tier is t_274530ae, the CLI verb is t_fc9296a7 and
the rebuildable-cache tiers + the report-only section are t_701e22fc; output
formatting stays out of here (D8) and this module must not be imported by
``hscc.py`` at module scope for those cards to wire it up.

Why this module exists
----------------------
The gateway kept its disk hygiene in a hand-maintained shell script
(``~/bin/clean-mac.sh`` § "kanban workspaces of done/archived cards"), invisible
to bootstrap and to review. This is that behaviour productised. The script is
the behavioural spec — same age filter, same "keep it if anything looks live"
probes, same archive-then-delete order — with three deliberate changes:

* Hermes is the only board authority. The script shells out to ``sqlite3`` and
  writes its own ``select id from tasks where ...``; here the board list, the
  task rows and every root come from hermes' own accessors, i.e. the same ones
  ``kanban_live.collect()`` uses (D1).
* A workspace path is never read from a DB row. ``tasks.workspace_path`` is
  untrusted input: a row pointing at a real repo would turn a cleanup into a
  repo delete. The path is DERIVED as ``workspaces_root(board) / task_id`` and
  re-checked for containment against that derived root at every step, in the
  plan and again in the executor (D2).
* ``~/dev`` is never touched (D6). The script ends its section with
  ``git -C ~/dev/<repo> worktree prune`` for every repo in ``~/dev``; a
  productised cleanup must not sweep the developer's checkouts. Worktrees this
  engine removes leave through ``git worktree remove`` (D5), which keeps the
  parent registry consistent for exactly what we removed — which is all the
  pruning the engine is entitled to do.

Safety posture (the whole point of the module)
----------------------------------------------
``plan_clean`` is read-only: no create, no move, no delete, no write-open. The
git probes run under ``--no-optional-locks`` precisely because a plain
``git status`` refreshes ``.git/index`` — a write that a "read-only" pass must
not perform. ``execute(apply=False)`` mutates nothing either, and
``execute(apply=True)`` re-runs the same probes before it removes anything, so
a plan that went stale (a card re-claimed, a tree dirtied) cannot delete work
it was never allowed to delete.

Nothing is deleted that has not first been archived: the archive is written to
``<target>.part``, verified with ``tar -tzf``, then renamed into place, and any
failure anywhere in that sequence deletes the ``.part`` and KEEPS the workspace
(D4). Card/board history survives by construction — comments, attachments,
events and the board DB all live outside ``workspaces/`` (D7).

No absolute home path appears in this module: every root is what the injected
(or lazily loaded) ``kanban_db`` resolves, so a sandboxed ``HERMES_KANBAN_HOME``
redirects the archive root and ``shutil.rmtree`` can only ever receive a path
derived from a caller-supplied root.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from hscc_daemon import autodown
from hscc_daemon import kanban_live

__all__ = ["KIND", "KIND_COMMAND", "KIND_PATH", "KIND_REPORT", "TIER_SAFE",
           "TIER_DEEP", "SKIP_REASONS", "CACHE_REASONS", "CACHE_STATUSES",
           "plan_clean", "execute"]

#: The workspace action kind (t_274530ae).
KIND = "archive_workspace"
#: A cache-tier action that runs one external command (``uv cache prune``).
KIND_COMMAND = "cache_command"
#: A cache-tier action that removes one directory the engine named itself.
KIND_PATH = "cache_path"
#: A path this verb measures and prints but never touches (script section 6).
KIND_REPORT = "report_only"

#: The two cache tiers. ``safe`` runs with ``--apply`` alone; ``deep`` runs only
#: with ``--apply --deep --confirm`` (an explicit operator gesture) and is the
#: tier whose removals cost the operator a rebuild.
TIER_SAFE = "safe"
TIER_DEEP = "deep"
TIERS = (TIER_SAFE, TIER_DEEP)

#: Closed vocabulary for ``skipped[].reason`` (D3 + D4). Anything outside this
#: set is a bug, not a new category — a skip must always say *why* in a word.
SKIP_REASONS = ("dirty", "unpushed", "stashed", "unreadable", "claim",
                "archive-failed")

#: A finished card's workspace becomes a candidate once it has been untouched
#: for this long (the script's ``--days`` default).
DEFAULT_DAYS = 7

SECONDS_PER_DAY = 86400

#: ``<hermes-home>/kanban/workspace-archive/<board>/<id>.tar.gz`` — the same
#: location the reference script uses, spelled relative to hermes' own root.
ARCHIVE_SUBPATH = ("kanban", "workspace-archive")

#: ``find "$ws" -maxdepth 3 -name .git``: a workspace may hold a repo, or a
#: directory of repos, but we do not go hunting deeper than that.
GIT_MAXDEPTH = 3

#: Rebuildable junk that does not belong in an archive. Everything else in the
#: workspace is kept — decision docs, reports, probe output, git-ignored results.
TAR_EXCLUDES = (".git", "__pycache__", "node_modules", ".venv", "venv",
                ".dart_tool", ".pytest_cache", "DerivedData", ".build")

_PROBE_TIMEOUT = 30       # a single `git status` on a healthy repo
_TAR_TIMEOUT = 900        # a whole workspace, compressed

# ── the cache tiers (t_701e22fc) ─────────────────────────────────────────── #

#: Every external command the cache tiers name runs under this cap, killed when
#: it expires and reported in ``errors``. The verb is callable from a cron, so
#: a wedged ``brew cleanup`` must never be able to wedge the verb with it —
#: this is the whole reason the cap is per command rather than per run.
CACHE_TIMEOUT = 300

#: A pytest temp dir is disposable once it has sat untouched this long (the
#: reference script's ``-mtime +1``).
PYTEST_TEMP_AGE_SECONDS = 86400

#: ``${TMPDIR}/pytest-of-$USER`` — the tree the script sweeps.
PYTEST_TEMP_PREFIX = "pytest-of-"

#: ``$HOME/.cache/hscc-uitest*`` — the UI-test caches.
UITEST_CACHE_PREFIX = "hscc-uitest"

#: The names the engine puts on its own cache actions. They are part of the
#: removal contract below (an action's ``name`` says which directory it may
#: remove), so they are constants, not report strings.
UV_NAME = "uv cache"
NPM_NAME = "npm cache"
CONDA_NAME = "conda pkgs"
BREW_NAME = "homebrew"
PIP_NAME = "pip cache"
SIMCTL_NAME = "simulators"
PYTEST_TEMP_NAME = "pytest temp dir"
UITEST_NAME = "hscc UI-test cache"
DERIVED_DATA_NAME = "Xcode DerivedData"
SIMULATOR_CACHE_NAME = "simulator caches"
TRASH_NAME = "Trash"

#: How the Trash is emptied: the reference script's AppleScript, verbatim. This
#: engine never removes ``~/.Trash`` itself — Finder is the only thing allowed
#: to empty it, which is also why a missing ``osascript`` is a skip, not an
#: ``rm -rf`` fallback.
TRASH_APPLESCRIPT = 'tell application "Finder" to empty trash'

#: ``<name>: (<allowed parent>, <leaf rule>)`` — the ONLY directories a
#: ``cache_path`` action may remove, and the only names the executor accepts.
#: A plan is untrusted input (it can arrive through ``--json``), so the executor
#: re-DERIVES the parent from HOME/TMPDIR at removal time and requires the leaf
#: rule under it: a name it does not know, a parent it does not derive for that
#: name, or a leaf it did not name is refused. The same D2 posture the workspace
#: tier applies to ``workspaces_root`` — a path in a dict is not permission.
#: ``"*"`` means any child; ``prefix:<x>`` means any child whose name starts
#: with ``<x>``.
CACHE_PATH_RULES = {
    DERIVED_DATA_NAME: ("xcode", "DerivedData"),
    SIMULATOR_CACHE_NAME: ("coresimulator", "Caches"),
    PYTEST_TEMP_NAME: ("pytest", "*"),
    UITEST_NAME: ("cache", f"prefix:{UITEST_CACHE_PREFIX}"),
}

#: Closed vocabulary for a cache entry's ``status``.
CACHE_STATUSES = ("planned", "skipped", "ran", "failed", "timed-out",
                  "report-only")

#: Closed vocabulary for a cache entry's ``reason``, deliberately SEPARATE from
#: :data:`SKIP_REASONS`: that one is the workspace tier's vocabulary (a card is
#: kept because it is dirty, unpushed, claimed…) and t_fc9296a7 pins every
#: rendered workspace skip to it. A cache entry is skipped for machinery reasons
#: instead, and merging the two vocabularies would let a card disappear behind
#: an ``absent``.
CACHE_REASONS = ("not-installed", "absent", "outside-root", "protected",
                 "not-a-directory")


# --------------------------------------------------------------------------- #
# The public pair
# --------------------------------------------------------------------------- #

def plan_clean(*, days: int = DEFAULT_DAYS, boards=None, now=None,
               kanban_db=None, deep: bool = False) -> dict:
    """Read-only plan: every finished-card workspace older than ``days`` that
    is safe to archive, plus the rebuildable-cache tiers.

    ``boards`` narrows the pass to those slugs (``None`` = every board hermes
    enumerates). ``now`` (epoch seconds) and ``kanban_db`` (a
    ``hermes_cli.kanban_db``-shaped module) are injectable so the whole engine
    tests offline against a temp root; with ``kanban_db=None`` the production
    loader resolves it exactly like ``kanban_live.collect()`` does, and an
    unloadable lib is reported in ``errors`` rather than raised.

    ``deep`` adds the deep tier — the whole uv cache instead of a prune, Xcode's
    ``DerivedData``, the simulator caches and the Trash — to the plan. It changes
    only WHAT MAY BE REMOVED, never what is measured: a ``deep`` plan is still
    just a plan, and the executor refuses it unless the caller asked for it
    (card t_fc9296a7 turns that refusal into the ``--confirm`` gate).

    Returns the JSON-safe dict card 2 dumps straight into ``hscc clean --json``::

        {"days", "cutoff", "deep", "actions", "skipped", "errors",
         "cache_actions", "report_only"}

    The five t_274530ae keys keep their exact meaning: ``actions``/``skipped``
    stay the WORKSPACE tier (a cache entry never joins them) and ``errors`` stays
    the pass-level failure list. The three new keys are the cache tiers, and
    every size in them is measured HERE — once, read-only — so ``execute`` never
    re-walks a tree it has already cost the operator time on.

    A card too young, or one whose workspace does not exist, produces no entry
    at all — it is not a candidate, and silence about it keeps ``skipped``
    meaning "we looked and decided to keep this". The cache tiers follow the
    same rule: a cache directory that is not there produces no entry.
    """
    now_s = int(now if now is not None else time.time())
    days_i = max(0, int(days))
    cutoff = now_s - days_i * SECONDS_PER_DAY
    plan = {"days": days_i, "cutoff": cutoff, "deep": bool(deep),
            "actions": [], "skipped": [], "errors": [],
            "cache_actions": [], "report_only": []}

    # The cache tiers first, and with no board authority in hand: a host whose
    # kanban lib is unreachable still gets its cache report, and the workspace
    # half still reports its own error below. Home and TMPDIR are resolved here
    # and nowhere else in the plan, so one redirect moves the whole tier.
    home, tmp_root = _host_roots()
    cache, report = _plan_cache(home, tmp_root, now_s, bool(deep))
    plan["cache_actions"], plan["report_only"] = cache, report

    if kanban_db is None:
        kanban_db = autodown._load_kanban_db_or_default()
    if kanban_db is None:
        # Same message as kanban_live.collect(): the loader's own reason, never
        # a re-derived guess about where hermes should have been.
        plan["errors"].append(autodown._KANBAN_LOAD.get("reason")
                              or "kanban lib unreachable")
        return plan

    try:
        enumerated, multi = autodown._enum_board_names(kanban_db)
    except Exception as e:  # noqa: BLE001 — enumeration failure is a reported error
        plan["errors"].append(f"could not enumerate kanban boards: {e}")
        return plan

    wanted = None if boards is None else {str(b) for b in boards}
    for slug in enumerated:
        label = slug if slug else "default"
        if wanted is not None and label not in wanted:
            continue
        try:
            # The read-only door (kanban_live._open_board_ro -> sqlite mode=ro).
            # Never connect_closing(): that is the write-capable door, which
            # runs schema init and a writability preflight.
            with kanban_live._open_board_ro(kanban_db, slug, multi=multi) as conn:
                finished = _finished_tasks(kanban_db, conn)
        except Exception as e:  # noqa: BLE001 — one bad board must not stop the pass
            plan["errors"].append(f"board {label!r} unreadable: {e}")
            continue
        try:
            root = Path(kanban_db.workspaces_root(slug))
        except Exception as e:  # noqa: BLE001
            plan["errors"].append(f"board {label!r} workspaces root unreadable: {e}")
            continue
        for task in finished:
            kind, entry = _classify(task, board=label, root=root,
                                    cutoff=cutoff, now_s=now_s)
            if kind == "action":
                plan["actions"].append(entry)
            elif kind == "skip":
                plan["skipped"].append(entry)
    return plan


def execute(plan, *, apply: bool, kanban_db=None) -> dict:
    """Carry out (or report) a plan.

    ``apply=False`` mutates nothing — no archive dir is created, no workspace
    is opened for anything but reading — and returns the would-do report.
    ``apply=True`` archives each workspace first and only then removes it.

    ``kanban_db`` is the root authority: every removal target is re-checked
    against ``workspaces_root(board)`` HERE, because a plan may have arrived
    through ``--json`` and a path in a dict is untrusted input, exactly like a
    path in a DB row. Each action is also re-probed (D3) so a plan that went
    stale between the two calls cannot delete work that became live meanwhile.

    Returns::

        {"apply", "archived": [{"path", "archive", "size_bytes"}],
         "removed", "skipped", "errors", "freed_bytes",
         "cache": [...], "cache_freed_bytes", "report_only": [...]}

    ``size_bytes`` always means bytes of *workspace* data (what is freed), in
    the plan, in the skip entries and here; the archive's own size is one
    ``stat`` away and stays out of the payload.

    The three cache keys are t_701e22fc. ``cache`` is one entry per cache
    action with what happened to it (``status`` says ``ran``/``failed``/
    ``timed-out``/``skipped``/``planned``), ``cache_freed_bytes`` is the sum of
    the PLAN-TIME sizes of the actions that ran — an estimate, never a re-walk
    (a ``prune`` that reclaims half a cache reports high, which is why the
    workspace total and this one stay separate numbers) — and ``report_only``
    is carried through unchanged so ``--json --apply`` is self-contained.
    """
    result = {"apply": bool(apply), "archived": [], "removed": 0,
              "skipped": [], "errors": [], "freed_bytes": 0,
              "cache": [], "cache_freed_bytes": 0, "report_only": []}
    home, tmp_root = _host_roots()
    actions = [a for a in (plan.get("actions") or [])
               if isinstance(a, dict) and a.get("kind") == KIND]
    if not actions:
        # Nothing to archive is not nothing to do: a host with no finished card
        # still has caches to prune, and the cache tiers do not read the board.
        return _finish_cache(result, plan, home=home, tmp_root=tmp_root)

    if kanban_db is None:
        kanban_db = autodown._load_kanban_db_or_default()
    if kanban_db is None:
        result["errors"].append(autodown._KANBAN_LOAD.get("reason")
                                or "kanban lib unreachable")
        return _finish_cache(result, plan, home=home, tmp_root=tmp_root)

    for action in actions:
        entry = _action_entry(action)
        board, task_id, ws = entry["board"], entry["task_id"], Path(entry["path"])
        if not task_id or not str(ws):
            result["errors"].append(
                f"{board}/{task_id or '?'}: malformed action (no board, task id "
                f"or path) — not touched")
            continue
        if not ws.is_dir():
            result["errors"].append(
                f"{board}/{task_id}: workspace {ws} is gone — nothing removed")
            continue
        try:
            root = Path(kanban_db.workspaces_root(board))
        except Exception as e:  # noqa: BLE001 — no root authority => no removal
            result["errors"].append(
                f"{board}/{task_id}: workspaces root unreadable ({e}) — kept")
            continue
        if not _strictly_inside(root, ws):
            # D2, second gate: a path that is not under this board's derived
            # workspaces root is never archived and never removed.
            result["skipped"].append({**entry, "reason": "unreadable",
                                      "note": f"not under {root}"})
            continue

        size = _dir_bytes(ws)
        entry["size_bytes"] = size

        reason, worktrees = _scan_git(ws)
        if reason:
            # Re-probed, not trusted: the plan is a snapshot of a moment.
            result["skipped"].append({**entry, "reason": reason, "worktrees": worktrees,
                                      "note": "re-probed before removal"})
            continue
        entry["worktrees"] = worktrees

        target = _archive_target(kanban_db, board, task_id)
        if not apply:
            result["archived"].append({"path": str(ws), "archive": str(target),
                                       "size_bytes": size})
            continue

        ok, why = _archive_workspace(ws, target)
        if not ok:
            # Archive first, remove after — and only after the archive is real.
            result["skipped"].append({**entry, "reason": "archive-failed",
                                      "note": why})
            continue

        # D5: a worktree leaves through git, so the parent repo never keeps a
        # dead registration. `git worktree remove` deletes the directory too,
        # which is why the removal below tolerates an already-gone workspace.
        failed = _remove_worktrees(entry["worktrees"])
        if failed:
            result["errors"].append(
                f"{board}/{task_id}: worktree not removed ({failed}) — "
                f"workspace kept, archive at {target}")
            continue

        if ws.is_dir():
            try:
                shutil.rmtree(ws)
            except OSError as e:
                result["errors"].append(
                    f"{board}/{task_id}: could not remove {ws} ({e}) — "
                    f"archive kept at {target}")
                continue
        result["archived"].append({"path": str(ws), "archive": str(target),
                                   "size_bytes": size})
        result["removed"] += 1
        result["freed_bytes"] += size
    return _finish_cache(result, plan, home=home, tmp_root=tmp_root)


# --------------------------------------------------------------------------- #
# Reading the board (D1)
# --------------------------------------------------------------------------- #

def _finished_tasks(kanban_db, conn) -> list:
    """Every done + archived task on one board, through hermes' own accessor.

    Two calls, not one query, because that is what the library's signature
    allows: ``list_tasks`` adds ``AND status != 'archived'`` only when
    ``include_archived`` is false AND ``status`` is not itself ``"archived"`` —
    so ``status="done", include_archived=True`` returns the done rows and
    ``status="archived"`` returns the archived ones. Age is filtered in Python,
    on the Task fields, not in SQL.
    """
    return (list(kanban_db.list_tasks(conn, status="done", include_archived=True))
            + list(kanban_db.list_tasks(conn, status="archived")))


def _finished_epoch(task) -> int | None:
    """``max(completed_at, created_at)`` — the card's age stamp, or None.

    None means neither stamp is usable, which makes the card un-ageable. The
    reference script's SQL could not match such a row either
    (``coalesce(completed_at, created_at) < cutoff`` is NULL for it), so it was
    never a candidate; "not a candidate" is reported as no entry, not as a skip.
    """
    stamps = []
    for name in ("completed_at", "created_at"):
        raw = getattr(task, name, None)
        try:
            if raw:
                stamps.append(int(raw))
        except (TypeError, ValueError):
            continue
    return max(stamps) if stamps else None


def _claim_live(task, now_s: int) -> bool:
    """True while a dispatcher holds an unexpired claim on the card (D3)."""
    lock = getattr(task, "claim_lock", None)
    try:
        expires = int(getattr(task, "claim_expires", None) or 0)
    except (TypeError, ValueError):
        return True  # an unreadable claim stamp is a live claim: keep it
    return bool(lock) and expires > now_s


# --------------------------------------------------------------------------- #
# Classifying one workspace (D2 + D3)
# --------------------------------------------------------------------------- #

def _classify(task, *, board: str, root: Path, cutoff: int,
              now_s: int) -> tuple:
    """``("action", entry)`` / ``("skip", entry)`` / ``(None, None)``."""
    task_id = str(getattr(task, "id", "") or "")
    stamp = _finished_epoch(task)
    if stamp is None or stamp >= cutoff or not task_id:
        return None, None
    ws = root / task_id
    if not ws.is_dir():
        return None, None

    base = {"kind": KIND, "board": board, "task_id": task_id, "path": str(ws),
            "size_bytes": 0, "worktrees": [],
            "note": ""}

    if not _strictly_inside(root, ws):
        # D2: the path is derived, so this only fires when the derivation or a
        # symlink resolves out of the board's own workspaces root. Keep it.
        return "skip", {**base, "reason": "unreadable",
                       "note": f"derived path escapes {root}"}
    if _claim_live(task, now_s):
        return "skip", {**base, "size_bytes": _dir_bytes(ws), "reason": "claim",
                       "note": "a worker still holds an unexpired claim"}

    reason, worktrees = _scan_git(ws)
    size = _dir_bytes(ws)
    note = (f"finished {_age_days(stamp, now_s)} ago; {_human_size(size)}; "
            f"{len(worktrees)} worktree(s)")
    if reason:
        return "skip", {**base, "size_bytes": size, "worktrees": worktrees,
                        "reason": reason, "note": note}
    return "action", {**base, "size_bytes": size, "worktrees": worktrees,
                      "note": note}


def _strictly_inside(root: Path, ws: Path) -> bool:
    """True iff ``ws`` resolves to a path strictly below ``root`` (D2).

    Resolving both sides is the whole check: a task id that walks out with
    ``..``, or a workspace that is a symlink into someone's repo, fails here and
    is therefore never archived and never removed.
    """
    try:
        base = Path(os.path.realpath(str(root)))
        target = Path(os.path.realpath(str(ws)))
    except OSError:  # pragma: no cover — realpath only fails on a syscall error
        return False
    return base in target.parents


def _scan_git(ws: Path) -> tuple:
    """``(skip_reason_or_None, [worktree dirs])`` for one workspace (D3).

    Every ``.git`` within :data:`GIT_MAXDEPTH` is probed — a plain clone (a
    ``.git`` directory) and a worktree (a ``.git`` file) alike. A non-empty
    answer from any probe keeps the whole workspace; so does a probe that
    errors, because unreadable counts as dirty and we never guess that files
    are disposable. That is the reference script's rule, kept verbatim.
    """
    worktrees: list[str] = []
    for gitpath in _git_entries(ws):
        repo = gitpath.parent
        if gitpath.is_file():
            worktrees.append(str(repo))
        for argv, reason in ( (("status", "--porcelain"), "dirty"),
                              (("log", "--branches", "--not", "--remotes",
                                "--oneline"), "unpushed"),
                              (("stash", "list"), "stashed") ):
            ok, out = _git(repo, *argv)
            if not ok:
                return "unreadable", worktrees
            if out.strip():
                return reason, worktrees
    return None, worktrees


def _git_entries(ws: Path) -> list:
    """Every ``.git`` entry at depth <= :data:`GIT_MAXDEPTH` below ``ws``.

    ``os.walk`` with a pruned ``dirnames`` is the equivalent of the script's
    ``find "$ws" -maxdepth 3 -name .git``: ``.git`` never goes back into the
    walk, so nothing inside an object store is inspected, and within three
    levels nothing that ``find`` would report is missed.
    """
    found: list[Path] = []
    base = len(os.path.realpath(str(ws)).rstrip(os.sep).split(os.sep))
    for dirpath, dirnames, filenames in os.walk(ws):
        depth = len(os.path.realpath(dirpath).split(os.sep)) - base
        if ".git" in dirnames or ".git" in filenames:
            found.append(Path(dirpath) / ".git")
        dirnames[:] = [d for d in dirnames if d != ".git"]
        if depth >= GIT_MAXDEPTH - 1:
            # A .git found in a child at this depth sits at GIT_MAXDEPTH, the
            # last one in scope; there is no reason to descend further.
            dirnames[:] = []
    return found


# --------------------------------------------------------------------------- #
# Git + tar subprocess helpers (the workspace tier's only two executables; the
# cache tiers name their own, each one probed and each one bounded)
# --------------------------------------------------------------------------- #

def _run(argv: list, timeout: int) -> tuple:
    """One subprocess. Test seam: patch this, not ``subprocess.run``."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return 124, "", f"{type(e).__name__}: {e}"
    return p.returncode, p.stdout, p.stderr


def _git(repo, *args: str) -> tuple:
    """``(ok, stdout)`` for one read-only git probe.

    ``--no-optional-locks`` is not decoration: a plain ``git status`` refreshes
    ``.git/index``, which is a write, and ``plan_clean`` promises it writes
    nothing. ``ok`` is False for a non-zero exit as well as for a missing git —
    the caller maps that to ``unreadable`` rather than to "clean".
    """
    rc, out, err = _run(["git", "--no-optional-locks", "-C", str(repo), *args],
                        _PROBE_TIMEOUT)
    if rc != 0:
        return False, (err or out or "").strip()
    return True, out


def _git_common_dir(worktree: Path) -> Path | None:
    """The shared git dir of a worktree — the handle on its main repo (D5)."""
    ok, out = _git(worktree, "rev-parse", "--path-format=absolute",
                   "--git-common-dir")
    if not ok or not out.strip():
        return None
    common = Path(out.strip())
    # A linked worktree of a normal repo answers `<main>/.git`; of a bare repo
    # it answers the bare repo itself. Anything else is a shape we do not
    # recognise, and recognising it wrongly would run a removal in the wrong
    # repo — return None and let the caller keep the workspace.
    return common.parent if common.name == ".git" else common


def _remove_worktrees(worktrees: list) -> str:
    """`git worktree remove --force` each worktree. '' when all left, else why.

    Runs against the MAIN repo (``-C <main>``), never from inside the worktree,
    so git's own registry stays consistent — a plain rmtree would leave the
    parent repo holding a dead registration.

    Deliberate divergence from the reference script (D6, t_274530ae): there is
    no `git worktree prune` pass over ``~/dev`` afterwards. The script prunes
    every developer checkout on the machine; this engine removes only what it
    registered a removal for.
    """
    for wt in worktrees:
        wtp = Path(wt)
        if not wtp.exists():
            continue  # already gone — nothing to unregister, nothing to keep
        main = _git_common_dir(wtp)
        if main is None or not main.is_dir():
            return f"could not resolve the main repo for {wtp}"
        rc, out, err = _run(["git", "-C", str(main), "worktree", "remove",
                             "--force", str(wtp)], _PROBE_TIMEOUT)
        if rc != 0 and wtp.exists():
            return f"git worktree remove failed for {wtp}: " \
                   f"{(err or out or '').strip()[:200]}"
    return ""


def _archive_target(kanban_db, board: str, task_id: str) -> Path:
    """``<hermes-home>/kanban/workspace-archive/<board>/<id>.tar.gz`` (D4).

    The home comes from hermes' own ``boards_root()`` — ``<home>/kanban/boards``,
    so two levels up is the home it resolved (``HERMES_KANBAN_HOME`` or the
    default root) — which is why a sandboxed home redirects the archive and why
    no literal ``~/.hermes`` appears in this module. The two name components are
    flattened because a plan may be a round-tripped JSON payload: they choose
    where the ARCHIVE lands, and that is still worth sanitising.
    """
    home = Path(kanban_db.boards_root()).parent.parent
    safe_board = _safe_component(board, "default")
    return (home.joinpath(*ARCHIVE_SUBPATH) / safe_board
            / f"{_safe_component(task_id, 'unknown')}.tar.gz")


def _safe_component(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", str(value or ""))
    return cleaned if cleaned and cleaned not in (".", "..") else fallback


def _archive_workspace(ws: Path, target: Path) -> tuple:
    """Write ``target`` atomically. ``(True, '')`` or ``(False, why)``.

    ``tar`` writes ``<target>.part``, ``tar -tzf`` proves the part is a
    listable archive, and only then is it renamed into place — a truncated
    archive that looks valid is worse than none, so verification is not
    optional and any failure in this sequence leaves the workspace untouched
    and no ``.part`` behind.
    """
    part = target.with_name(target.name + ".part")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        argv = ["tar", "-czf", str(part), "-C", str(ws)]
        for exclude in TAR_EXCLUDES:
            argv += ["--exclude", exclude]
        argv.append(".")
        # exit 0 is the contract; stderr is not a failure signal here — BSD tar
        # on macOS writes extended-attribute chatter while still succeeding, and
        # the reference script redirects it for exactly that reason.
        rc, out, err = _run(argv, _TAR_TIMEOUT)
        if rc != 0:
            return False, f"tar create failed: {(err or out or '').strip()[:200]}"
        rc, out, err = _run(["tar", "-tzf", str(part)], _TAR_TIMEOUT)
        if rc != 0 or not out.strip():
            return False, ("tar verify failed: "
                           f"{(err or out or 'empty listing').strip()[:200]}")
        os.replace(part, target)
        return True, ""
    except OSError as e:
        return False, f"archive step failed: {type(e).__name__}: {e}"
    finally:
        # Reached on every non-published exit. After os.replace the part is
        # already gone, so this is a no-op there.
        with contextlib.suppress(OSError):
            if part.exists():
                part.unlink()


# --------------------------------------------------------------------------- #
# The cache tiers: planning (read-only — probes and sizes, nothing else)
# --------------------------------------------------------------------------- #

#: Where conda lives on the reference host. Probed by REAL path first: the
#: ``conda activate`` shim a shell installs has no executable behind it, while a
#: conda in another prefix still owns a cache worth cleaning.
CONDA_REAL_SUBPATH = ("miniconda3", "bin", "conda")

#: ``(name, home-relative path, note)`` for the section the script prints under
#: "report only — decide yourself". Measured and named, never touched: the whole
#: point is that the operator decides, and cannot decide without the size.
REPORT_ONLY_SPECS = (
    ("Xcode device support",
     ("Library", "Developer", "CoreDevice", "DeviceFS"),
     "Xcode re-downloads it when a device connects"),
    ("hermes profiles", (".hermes", "profiles"),
     "hermes data — this verb never touches it"),
    ("LM Studio models", (".lmstudio",),
     "downloaded models; delete what you do not want yourself"),
    ("Trash", (".Trash",),
     "emptied only by the deep set, through Finder"),
)


def _host_roots() -> tuple:
    """``(home, tmp_root)`` — the only two roots the cache tiers know about.

    Both resolve at CALL time, never at import: pointing ``HOME``/``TMPDIR``
    somewhere else moves the whole tier with it, and no absolute home path is
    ever baked into this module (or into a plan a test can compare against).
    """
    tmp = os.environ.get("TMPDIR") or tempfile.gettempdir()
    return Path(os.path.expanduser("~")), Path(tmp)


def _user_name() -> str:
    """``$USER`` — the name the pytest temp tree is keyed by, with a fallback."""
    name = os.environ.get("USER") or ""
    if name:
        return name
    try:
        import getpass
        return getpass.getuser()
    except Exception:  # pragma: no cover — needs a host with no passwd entry
        return "unknown"


def _probe(name: str) -> str:
    """The absolute path of ``name``, or '' when it is not installed."""
    return shutil.which(name) or ""


def _conda_binary(home: Path) -> str:
    """conda at its real path first, then whatever ``PATH`` offers (or '')."""
    real = home.joinpath(*CONDA_REAL_SUBPATH)
    if real.is_file() and os.access(real, os.X_OK):
        return str(real)
    return _probe("conda")


def _size(path: Path) -> int:
    """The plan-time size of one path, 0 when it is not there.

    This is where a `hscc clean` pass spends its wall-clock time — an
    ``lstat`` per file, read-only, and deliberately done ONCE, here, so an
    ``--apply`` never re-walks a tree the operator already waited for.
    """
    return _dir_bytes(path) if path.is_dir() else 0


def _children(root: Path, *, prefix: str = "", older_than: int | None = None) -> list:
    """Immediate subdirectories of ``root`` matching the age/prefix filter.

    Sorted, so a plan is byte-stable. A symlinked child is never a candidate:
    the script's ``find -type d`` would follow it, and this engine would rather
    leave one stale directory behind than remove a link into real work.
    """
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    found = []
    for name in names:
        if prefix and not name.startswith(prefix):
            continue
        child = root / name
        try:
            if child.is_symlink() or not child.is_dir():
                continue
            if (older_than is not None
                    and os.lstat(child).st_mtime >= older_than):
                continue
        except OSError:
            continue                    # vanished mid-listing: not a candidate
        found.append(child)
    return found


def _command_entry(name, argv, *, tier, installed=True, size=0, note="") -> dict:
    """One cache action that runs a command. ``argv[0]`` is the resolved path."""
    return {"kind": KIND_COMMAND, "name": name, "tier": tier,
            "argv": [str(a) for a in argv], "path": "", "root": "",
            "size_bytes": int(size),
            "status": "planned" if installed else "skipped",
            "reason": "" if installed else "not-installed", "note": note}


def _path_entry(name, path, root, *, tier, size=0, note="") -> dict:
    """One cache action that removes a directory this engine named itself."""
    return {"kind": KIND_PATH, "name": name, "tier": tier, "argv": [],
            "path": str(path), "root": str(root), "size_bytes": int(size),
            "status": "planned", "reason": "", "note": note}


def _cache_roots_for(name: str, home: Path, tmp_root: Path) -> tuple:
    """``(allowed parent, the one target under it or None)`` for a ``name``.

    The single derivation both halves use — and it is read out of
    :data:`CACHE_PATH_RULES`, so the planner and the executor cannot disagree
    about what a name means. ``None`` as the target means "any child allowed by
    the leaf rule" (``*`` or ``prefix:<x>``).
    """
    rule = CACHE_PATH_RULES.get(name)
    if rule is None:
        return None, None
    kind, leaf = rule
    parents = {"xcode": home / "Library" / "Developer" / "Xcode",
               "coresimulator": home / "Library" / "Developer" / "CoreSimulator",
               "pytest": tmp_root / f"{PYTEST_TEMP_PREFIX}{_user_name()}",
               "cache": home / ".cache"}
    parent = parents[kind]
    if leaf == "*" or leaf.startswith("prefix:"):
        return parent, None
    return parent, parent / leaf


def _plan_cache(home: Path, tmp_root: Path, now_s: int, deep: bool) -> tuple:
    """``(cache_actions, report_only)`` — the two tiers, read-only.

    ``shutil.which`` probes and size walks; no create, no move, no remove. A
    binary that is not installed still gets an entry (``status="skipped"``,
    ``reason="not-installed"``) so the report says the pass covered that cache
    and why it did nothing — a Mac without Xcode must not be red.
    """
    actions = []

    # uv is the one cache whose command changes with the tier: `prune` keeps
    # what is still useful, `clean` drops the lot. So a non-deep plan never
    # contains `uv cache clean` and a deep one never contains both.
    uv = _probe("uv")
    actions.append(_command_entry(
        UV_NAME, [uv, "cache", "clean" if deep else "prune"],
        tier=TIER_DEEP if deep else TIER_SAFE, installed=bool(uv),
        size=_size(home / ".cache" / "uv"),
        note="whole-cache clean (--deep) instead of prune" if deep else ""))

    npm = _probe("npm")
    actions.append(_command_entry(NPM_NAME, [npm, "cache", "clean", "--force"],
                                  tier=TIER_SAFE, installed=bool(npm),
                                  size=_size(home / ".npm")))

    conda = _conda_binary(home)
    actions.append(_command_entry(
        CONDA_NAME, [conda, "clean", "--all", "-y"], tier=TIER_SAFE,
        installed=bool(conda), size=_size(home / "miniconda3" / "pkgs"),
        note="probed at its real path, not via PATH"))

    brew = _probe("brew")
    actions.append(_command_entry(
        BREW_NAME, [brew, "cleanup", "--prune=30"], tier=TIER_SAFE,
        installed=bool(brew),
        size=_size(home / "Library" / "Caches" / "Homebrew")))

    python3 = _probe("python3")
    actions.append(_command_entry(
        PIP_NAME, [python3, "-m", "pip", "cache", "purge"], tier=TIER_SAFE,
        installed=bool(python3),
        size=_size(home / "Library" / "Caches" / "pip")))

    xcrun = _probe("xcrun")
    actions.append(_command_entry(
        SIMCTL_NAME, [xcrun, "simctl", "delete", "unavailable"],
        tier=TIER_SAFE, installed=bool(xcrun)))

    pytest_root, _ = _cache_roots_for(PYTEST_TEMP_NAME, home, tmp_root)
    for child in _children(pytest_root,
                           older_than=now_s - PYTEST_TEMP_AGE_SECONDS):
        actions.append(_path_entry(PYTEST_TEMP_NAME, child, pytest_root,
                                   tier=TIER_SAFE, size=_size(child),
                                   note="older than 1 day"))

    uitest_root, _ = _cache_roots_for(UITEST_NAME, home, tmp_root)
    for child in _children(uitest_root, prefix=UITEST_CACHE_PREFIX):
        actions.append(_path_entry(UITEST_NAME, child, uitest_root,
                                   tier=TIER_SAFE, size=_size(child)))

    if deep:
        for name in (DERIVED_DATA_NAME, SIMULATOR_CACHE_NAME):
            parent, target = _cache_roots_for(name, home, tmp_root)
            if target is not None and target.is_dir():
                actions.append(_path_entry(name, target, parent,
                                           tier=TIER_DEEP, size=_size(target)))
        # The Trash goes through Finder, exactly as the script does. There is
        # no rm -rf path for it in this module at all: `osascript` missing is a
        # skip, never a fallback.
        osascript = _probe("osascript")
        actions.append(_command_entry(
            TRASH_NAME, [osascript, "-e", TRASH_APPLESCRIPT], tier=TIER_DEEP,
            installed=bool(osascript), size=_size(home / ".Trash"),
            note="emptied through Finder, never rm -rf"))

    return actions, _plan_report_only(home)


def _plan_report_only(home: Path) -> list:
    """Script section 6 as plan entries: measured, printed, never touched."""
    out = []
    for name, rel, note in REPORT_ONLY_SPECS:
        path = home.joinpath(*rel)
        out.append({"kind": KIND_REPORT, "name": name, "path": str(path),
                    "size_bytes": _size(path), "status": "report-only",
                    "note": note})
    return out


# --------------------------------------------------------------------------- #
# The cache tiers: execution (bounded, guarded, and the only place that mutates)
# --------------------------------------------------------------------------- #

def _finish_cache(result: dict, plan: dict, *, home: Path,
                  tmp_root: Path) -> dict:
    """Fold the cache tiers into an ``execute`` result and return it."""
    entries, freed = _execute_cache(plan, apply=bool(result["apply"]),
                                    home=home, tmp_root=tmp_root,
                                    errors=result["errors"])
    result["cache"] = entries
    result["cache_freed_bytes"] = int(freed)
    result["report_only"] = [dict(e) for e in (plan.get("report_only") or [])
                             if isinstance(e, dict)
                             and e.get("kind") == KIND_REPORT]
    return result


def _execute_cache(plan: dict, *, apply: bool, home: Path, tmp_root: Path,
                   errors: list) -> tuple:
    """``(one entry per cache action, freed estimate)``.

    Nothing runs unless ``apply`` is true, and no command runs without a
    timeout: an expired one is killed, lands in ``errors`` with its name, and
    the pass continues to the next action. A refusal (``protected``,
    ``outside-root``) is reported as a skip AND as an error, because a plan
    naming a directory it was never allowed to name is worth an exit 1.
    """
    entries = []
    freed = 0
    for raw in (plan.get("cache_actions") or []):
        if not isinstance(raw, dict):
            continue
        entry = _cache_entry(raw)
        entries.append(entry)
        if entry["status"] == "skipped":
            continue                       # already explained by the planner
        if not apply:
            continue                       # a report is not a run
        if entry["kind"] == KIND_COMMAND:
            status, detail = _run_cache_command(entry["argv"])
        else:
            status, detail = _remove_cache_path(entry, home=home,
                                                tmp_root=tmp_root)
        if status == "skipped":
            entry["status"], entry["reason"] = "skipped", detail
            if detail in ("outside-root", "protected", "not-a-directory"):
                errors.append(f"cache {entry['name']}: refused ({detail})")
        elif status == "ran":
            entry["status"], entry["reason"] = "ran", ""
            freed += int(entry["size_bytes"] or 0)
        else:
            entry["status"], entry["reason"] = status, detail
            errors.append(f"cache {entry['name']}: {status} ({detail})")
    return entries, freed


def _cache_entry(raw: dict) -> dict:
    """Normalise one planned cache action into what the executor reports."""
    status = str(raw.get("status") or "planned")
    return {"kind": raw.get("kind"), "name": str(raw.get("name") or ""),
            "tier": (raw.get("tier") if raw.get("tier") in TIERS
                     else TIER_SAFE),
            "argv": [str(a) for a in (raw.get("argv") or [])],
            "path": str(raw.get("path") or ""),
            "root": str(raw.get("root") or ""),
            "size_bytes": _as_int(raw.get("size_bytes")),
            "status": status if status in ("planned", "skipped") else "planned",
            "reason": str(raw.get("reason") or ""),
            "note": str(raw.get("note") or "")}


def _run_cache_command(argv: list) -> tuple:
    """``('ran'|'failed'|'timed-out', detail)`` for one cache command.

    :data:`CACHE_TIMEOUT` is the point: ``brew cleanup`` can wedge on a tap
    fetch, and this verb is meant to be runnable from a cron. ``_run`` reports
    every ``SubprocessError`` as exit 124 with the exception name in stderr,
    which is how a kill is told apart from a binary that refused.
    """
    if not argv or not argv[0]:
        return "skipped", "not-installed"
    rc, out, err = _run(argv, CACHE_TIMEOUT)
    detail = (err or out or "").strip()[:200]
    if rc == 124:
        if "TimeoutExpired" in (err or ""):
            return ("timed-out", f"killed after {CACHE_TIMEOUT}s with no "
                                 f"result — the command was left mid-flight")
        return "failed", detail or "could not run"
    if rc != 0:
        return "failed", detail or f"exit {rc}"
    return "ran", ""


def _remove_cache_path(entry: dict, *, home: Path, tmp_root: Path) -> tuple:
    """Remove one cache directory this engine named. Never a shell command."""
    blocker, resolved = _cache_path_blocker(entry, home=home, tmp_root=tmp_root)
    if blocker:
        return "skipped", blocker
    try:
        shutil.rmtree(str(resolved))
    except OSError as e:
        return "failed", f"could not remove {resolved}: {e}"
    return "ran", ""


def _cache_path_blocker(entry: dict, *, home: Path, tmp_root: Path) -> tuple:
    """``('', resolved)`` when removal is allowed, else ``(reason, None)``.

    Re-derived, never trusted — the D2 posture of the workspace tier, applied to
    caches. The allowed parent comes from the CURRENT ``HOME``/``TMPDIR`` and the
    action's own ``name`` (see :data:`CACHE_PATH_RULES`), the target must sit
    strictly inside it, and it must clear :func:`_protected_roots`. So a plan
    that arrived through ``--json`` cannot point this tier at anything the
    engine did not itself name, and cannot name its way out of ``~/.hermes``,
    ``~/.hscc`` or ``~/dev``.
    """
    name = str(entry.get("name") or "")
    rule = CACHE_PATH_RULES.get(name)
    if rule is None:
        return "outside-root", None            # a name this engine never plans
    _parent, leaf = rule
    allowed, _fixed = _cache_roots_for(name, home, tmp_root)
    target = str(entry.get("path") or "")
    if allowed is None or not target:
        return "absent" if allowed is not None else "outside-root", None
    try:
        resolved = Path(os.path.realpath(target))
    except OSError:  # pragma: no cover — realpath only fails on a syscall error
        return "absent", None
    if not _strictly_inside(allowed, resolved):
        return "outside-root", None
    if leaf != "*" and not _leaf_ok(leaf, resolved.name):
        return "outside-root", None
    for protected in _protected_roots(home):
        root = Path(os.path.realpath(str(protected)))
        if resolved == root or root in resolved.parents:
            return "protected", None
    if not resolved.is_dir():
        return "absent", None
    return "", resolved


def _leaf_ok(leaf: str, name: str) -> bool:
    """Apply one :data:`CACHE_PATH_RULES` leaf rule to a directory name."""
    if leaf.startswith("prefix:"):
        return name.startswith(leaf[len("prefix:"):])
    return name == leaf


def _protected_roots(home: Path) -> tuple:
    """The roots neither cache tier may ever remove anything from.

    ``~/.hermes`` (config, sessions, memory, profiles — the archive path is
    written there by the workspace tier and is the only ``~/.hermes`` path this
    module touches, and never as a removal target), ``~/.hscc`` (daemon state)
    and ``~/dev`` (the developer's own checkouts). This is a SECOND gate after
    :func:`_cache_path_blocker`'s containment, not a replacement for it: the
    first says "only what we named", this one says what we never name.
    """
    return (home / ".hermes", home / ".hscc", home / "dev")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _action_entry(action: dict) -> dict:
    """Normalise one plan action into the shape the engine works with."""
    return {"kind": KIND,
            "board": str(action.get("board") or "default"),
            "task_id": str(action.get("task_id") or ""),
            "path": str(action.get("path") or ""),
            "size_bytes": _as_int(action.get("size_bytes")),
            "worktrees": [str(w) for w in (action.get("worktrees") or [])],
            "note": str(action.get("note") or "")}


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _dir_bytes(path: Path) -> int:
    """Bytes of regular files under ``path`` (an estimate, never a write).

    Symlinks are counted as their own size, not their target's, and an entry we
    cannot stat is skipped rather than fatal — this number reports disk freed,
    it is not a safety check.
    """
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda e: None):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def _human_size(n: int) -> str:
    for unit, scale in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} B"


def _age_days(stamp: int, now_s: int) -> str:
    return f"{max(0, now_s - stamp) / SECONDS_PER_DAY:.1f}d"
