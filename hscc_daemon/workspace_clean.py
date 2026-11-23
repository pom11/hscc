"""`hscc clean`: archive the workspace of every finished card, then remove it.

Engine only (t_274530ae). The CLI verb is card 2 (t_fc9296a7) and the
rebuildable-cache tiers are card 3 (t_a4d6b78d); there is no `--deep`, no
report-only section and no output formatting here (D8), and this module must
not be imported by ``hscc.py`` for those cards to wire it up.

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
import time
from pathlib import Path

from hscc_daemon import autodown
from hscc_daemon import kanban_live

__all__ = ["KIND", "SKIP_REASONS", "plan_clean", "execute"]

#: The one action kind this engine produces (card 2 renders / extends it).
KIND = "archive_workspace"

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


# --------------------------------------------------------------------------- #
# The public pair
# --------------------------------------------------------------------------- #

def plan_clean(*, days: int = DEFAULT_DAYS, boards=None, now=None,
               kanban_db=None) -> dict:
    """Read-only plan: every finished-card workspace older than ``days`` that
    is safe to archive.

    ``boards`` narrows the pass to those slugs (``None`` = every board hermes
    enumerates). ``now`` (epoch seconds) and ``kanban_db`` (a
    ``hermes_cli.kanban_db``-shaped module) are injectable so the whole engine
    tests offline against a temp root; with ``kanban_db=None`` the production
    loader resolves it exactly like ``kanban_live.collect()`` does, and an
    unloadable lib is reported in ``errors`` rather than raised.

    Returns the JSON-safe dict card 2 dumps straight into ``hscc clean --json``::

        {"days", "cutoff", "actions": [...], "skipped": [...], "errors": [...]}

    A card too young, or one whose workspace does not exist, produces no entry
    at all — it is not a candidate, and silence about it keeps ``skipped``
    meaning "we looked and decided to keep this".
    """
    now_s = int(now if now is not None else time.time())
    days_i = max(0, int(days))
    cutoff = now_s - days_i * SECONDS_PER_DAY
    plan = {"days": days_i, "cutoff": cutoff, "actions": [], "skipped": [],
            "errors": []}

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
         "removed", "skipped", "errors", "freed_bytes"}

    ``size_bytes`` always means bytes of *workspace* data (what is freed), in
    the plan, in the skip entries and here; the archive's own size is one
    ``stat`` away and stays out of the payload.
    """
    result = {"apply": bool(apply), "archived": [], "removed": 0,
              "skipped": [], "errors": [], "freed_bytes": 0}
    actions = [a for a in (plan.get("actions") or [])
               if isinstance(a, dict) and a.get("kind") == KIND]
    if not actions:
        return result

    if kanban_db is None:
        kanban_db = autodown._load_kanban_db_or_default()
    if kanban_db is None:
        result["errors"].append(autodown._KANBAN_LOAD.get("reason")
                                or "kanban lib unreachable")
        return result

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
    return result


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
# Git + tar subprocess helpers (the only two executables this module names)
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
