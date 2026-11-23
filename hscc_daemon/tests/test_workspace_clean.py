"""Tests for :mod:`hscc_daemon.workspace_clean` (t_274530ae).

Everything here runs against a tree built under ``tmp_path``: a fake board DB,
fake workspaces and real ``git`` fixtures. The live gateway is never in scope —
no test points ``execute(apply=True)`` at a real home, a real board or a real
archive, and the injected ``kanban_db`` is the only reason the engine knows
where anything lives (which is what makes an apply here safe).

Two postures, on purpose:

* the injected-lib family (``FakeLib``) reproduces hermes' measured resolution
  rules — ``default`` at ``<root>/kanban.db`` + ``<root>/kanban/workspaces``,
  a named board under ``boards_root()`` — and implements the ``list_tasks``
  WHERE-shape the engine depends on, so the whole engine is exercised on any
  interpreter;
* one fidelity test drives the REAL ``hermes_cli.kanban_db`` through
  ``tests/real_hermes`` (plan only, never apply) so the assumptions the fake
  encodes are checked against the library they model.

``connect_closing`` — the write-capable door that runs schema init and a
writability preflight — is recorded and refused by ``FakeLib``, and the
read-only test asserts it stayed empty: the engine reads boards through the
``mode=ro`` door only, and writes nothing at all until ``apply=True``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

from hscc_daemon import workspace_clean as wc
# t_701e22fc: this file's --apply tests now drive an engine that also runs cache
# commands. Its autouse sandbox gives every test a fake HOME/TMPDIR and shimmed
# binaries, so nothing here can prune the operator's real caches.
from hscc_daemon.tests.test_workspace_clean_deep import cache_sandbox  # noqa: F401,E402
from hscc_daemon.tests import real_hermes
from hscc_daemon.tests.hermes_cli_plant import (evict_planted, is_planted_path,
                                               restore_planted)

NOW = 1_800_000_000
DAY = 86400
OLD = NOW - 30 * DAY          # comfortably past any sane --days
YOUNG = NOW - 2 * DAY         # inside the default 7-day window
FUTURE = NOW + 600

TASK_COLUMNS = ("id", "title", "status", "created_at", "completed_at",
                "claim_lock", "claim_expires", "workspace_path")

GIT = ["git", "-c", "user.name=hscc-clean-test",
       "-c", "user.email=clean-test@invalid", "-c", "commit.gpgsign=false"]


# --------------------------------------------------------------------------- #
# the fake board authority
# --------------------------------------------------------------------------- #

class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


class FakeLib:
    """A ``hermes_cli.kanban_db``-shaped lib over one temp home.

    Only the surface D1 names is implemented (``list_boards``,
    ``kanban_db_path``, ``boards_root``, ``workspaces_root``, ``list_tasks``),
    because only that surface is what the engine may ask hermes. Resolution
    copies hermes' measured rules rather than simplifying them, and data
    follows the FILE: a board's rows live in the DB its slug resolves to, so a
    reader that resolved the wrong board would read the wrong rows.
    """

    def __init__(self, home):
        self.home = Path(home)
        self.boards = []
        self.connects = []          # connect_closing() calls — must stay empty

    # --- paths ------------------------------------------------------------- #
    def boards_root(self):
        return self.home / "kanban" / "boards"

    def kanban_db_path(self, board=None):
        slug = str(board) if board else "default"
        if slug == "default":
            return self.home / "kanban.db"
        return self.boards_root() / slug / "kanban.db"

    def workspaces_root(self, board=None):
        slug = str(board) if board else "default"
        if slug == "default":
            return self.home / "kanban" / "workspaces"
        return self.boards_root() / slug / "workspaces"

    def attachments_root(self, board=None):
        slug = str(board) if board else "default"
        if slug == "default":
            return self.home / "kanban" / "attachments"
        return self.boards_root() / slug / "attachments"

    # --- enumeration ------------------------------------------------------- #
    def list_boards(self):
        return [{"slug": s} for s in self.boards]

    # --- the write door: recorded and refused ------------------------------ #
    def connect_closing(self, board=None):
        self.connects.append(board)
        raise AssertionError(
            "connect_closing() is the write-capable door; the clean engine "
            "must read a board with mode=ro (see kanban_live._open_board_ro)")

    # --- the read door ----------------------------------------------------- #
    def list_tasks(self, conn, *, status=None, include_archived=False,
                   **_ignored):
        """hermes' WHERE shape: archived rows are excluded unless asked for,
        and asking for archived rows IS the ask."""
        query = "SELECT * FROM tasks WHERE 1=1"
        params = []
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        if not include_archived and status != "archived":
            query += " AND status != 'archived'"
        rows = conn.execute(query, params).fetchall()
        return [types.SimpleNamespace(**{k: r[k] for k in TASK_COLUMNS})
                for r in rows]

    # --- fixture-side setup ------------------------------------------------ #
    def add_board(self, slug):
        """Create the board's DB (and its hermes-style ``board.json``)."""
        self.boards.append(slug)
        db = self.kanban_db_path(None if slug == "default" else slug)
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db)
        con.execute(f"CREATE TABLE IF NOT EXISTS tasks ("
                    f"{', '.join(c + ' TEXT' for c in TASK_COLUMNS)})")
        # A second table the engine must never touch: the proof that a run
        # leaves the board's own history alone.
        con.execute("CREATE TABLE IF NOT EXISTS task_comments "
                    "(task_id TEXT, body TEXT)")
        con.commit()
        con.close()
        if slug != "default":
            (db.parent / "board.json").write_text(json.dumps(
                {"slug": slug, "kind": "board", "archived": False}))
        return db

    def add_task(self, slug, task_id, *, status="done", created_at=OLD,
                 completed_at=OLD, claim_lock=None, claim_expires=None,
                 workspace_path=None):
        db = self.kanban_db_path(None if slug == "default" else slug)
        con = sqlite3.connect(db)
        con.execute(
            f"INSERT INTO tasks ({', '.join(TASK_COLUMNS)}) VALUES ("
            f"{', '.join('?' for _ in TASK_COLUMNS)})",
            (task_id, f"card {task_id}", status, created_at, completed_at,
             claim_lock, claim_expires, workspace_path))
        con.execute("INSERT INTO task_comments (task_id, body) VALUES (?, ?)",
                    (task_id, "the handoff comment"))
        con.commit()
        con.close()

    def db_path(self, slug):
        return self.kanban_db_path(None if slug == "default" else slug)

    def workspace(self, slug, task_id):
        return self.workspaces_root(None if slug == "default" else slug) / task_id


@pytest.fixture
def lib(tmp_path):
    """Two boards (legacy ``default`` + a named one) under one temp home."""
    home = tmp_path / "hermes-home"
    fake = FakeLib(home)
    fake.add_board("default")
    fake.add_board("hscc")
    return fake


# --------------------------------------------------------------------------- #
# git fixtures (real repos — the skip rules are only real against real git)
# --------------------------------------------------------------------------- #

def _git(*args, cwd):
    return subprocess.run([*GIT, *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True)


def _bare_origin(tmp_path) -> Path:
    """A bare repo with one commit on ``main`` — the remote that makes a
    clone's ``git log --branches --not --remotes`` empty (i.e. pushable = clean).
    """
    origin = tmp_path / "origin.git"
    origin.mkdir(parents=True, exist_ok=True)
    _git("-c", "init.defaultBranch=main", "init", "--bare", str(origin),
         cwd=tmp_path)
    seed = tmp_path / "seed"
    _git("clone", str(origin), str(seed), cwd=tmp_path)
    (seed / "README.md").write_text("seed\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "seed", cwd=seed)
    _git("push", "origin", "HEAD:refs/heads/main", cwd=seed)
    return origin


def _clone(origin: Path, ws: Path) -> Path:
    ws.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", str(origin), str(ws), cwd=ws.parent)
    return ws


def make_ws(lib, slug, task_id, *, mode="clean", tmp_path=None,
            doc="DECISION.md", extra=None) -> Path:
    """Materialise one workspace.

    ``mode`` selects the git shape: ``plain`` (no repo at all), ``clean`` /
    ``dirty`` / ``unpushed`` / ``stashed`` (a clone with a working tree in that
    state) and ``broken`` (a ``.git`` directory that is not a repository, so
    every probe errors). The decision doc is committed when there is a repo, so
    a clean workspace really is clean.
    """
    ws = lib.workspace(slug, task_id)
    ws.mkdir(parents=True, exist_ok=True)
    cloned = mode in ("clean", "dirty", "unpushed", "stashed")
    origin = _bare_origin(tmp_path) if (tmp_path and cloned) else None
    if origin is not None:
        ws.rmdir()
        _clone(origin, ws)
    (ws / doc).write_text("# decision\narchive me, I am the reason\n")
    if origin is not None:
        _git("add", "-A", cwd=ws)
        _git("commit", "-m", f"{task_id}: decision", cwd=ws)
        if mode in ("clean", "stashed"):
            _git("push", "origin", "HEAD:refs/heads/main", cwd=ws)
        if mode == "dirty":
            (ws / "README.md").write_text("uncommitted local work\n")
        elif mode == "stashed":
            (ws / "scratch.txt").write_text("work in progress\n")
            _git("add", "-A", cwd=ws)
            _git("stash", cwd=ws)
        # ``unpushed`` deliberately keeps its commit unpushed.
    elif mode == "broken":
        (ws / ".git").mkdir()
        (ws / ".git" / "junk").write_text("not a repository\n")
    for name, body in (extra or {}).items():
        target = ws / name
        target.parent.mkdir(parents=True, exist_ok=True)
        (target if body is not None else target / "payload").write_text(body or "")
    return ws


def tar_listing(path: Path) -> list:
    return subprocess.run(["tar", "-tzf", str(path)], capture_output=True,
                          text=True, check=True).stdout.splitlines()


def snapshot(root: Path) -> list:
    """(relpath, kind, size, mtime_ns) for every entry — the read-only proof."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            p = Path(dirpath) / name
            st = os.lstat(p)
            kind = ("link" if os.path.islink(p)
                    else "dir" if p.is_dir() else "file")
            out.append((str(p.relative_to(root)), kind, st.st_size,
                        st.st_mtime_ns))
    return sorted(out)


# --------------------------------------------------------------------------- #
# the plan
# --------------------------------------------------------------------------- #

def test_old_clean_workspace_is_planned(lib):
    lib.add_task("hscc", "t_old")
    ws = make_ws(lib, "hscc", "t_old", mode="plain")
    plan = wc.plan_clean(now=NOW, kanban_db=lib)
    assert plan["errors"] == [] and plan["skipped"] == []
    assert plan["days"] == 7 and plan["cutoff"] == NOW - 7 * DAY
    (action,) = plan["actions"]
    assert action["kind"] == wc.KIND
    assert action["board"] == "hscc" and action["task_id"] == "t_old"
    assert action["path"] == str(ws)
    assert action["size_bytes"] > 0 and action["worktrees"] == []
    assert "30.0d ago" in action["note"]


def test_a_card_younger_than_days_is_not_a_candidate(lib):
    """No entry at all: too young is not a decision to keep something."""
    lib.add_task("hscc", "t_new", created_at=YOUNG, completed_at=YOUNG)
    make_ws(lib, "hscc", "t_new", mode="plain")
    plan = wc.plan_clean(now=NOW, kanban_db=lib)
    assert plan == {"days": 7, "cutoff": NOW - 7 * DAY, "actions": [],
                    "skipped": [], "errors": []}


def test_archived_status_cards_are_planned_too(lib):
    """D1's second ``list_tasks`` call — archived rows are a different status."""
    lib.add_task("hscc", "t_arch", status="archived")
    make_ws(lib, "hscc", "t_arch", mode="plain")
    plan = wc.plan_clean(now=NOW, kanban_db=lib)
    assert [a["task_id"] for a in plan["actions"]] == ["t_arch"]


def test_boards_argument_narrows_the_pass(lib):
    lib.add_task("default", "t_d")
    lib.add_task("hscc", "t_h")
    make_ws(lib, "default", "t_d", mode="plain")
    make_ws(lib, "hscc", "t_h", mode="plain")
    plan = wc.plan_clean(now=NOW, boards=["hscc"], kanban_db=lib)
    assert [(a["board"], a["task_id"]) for a in plan["actions"]] == [("hscc", "t_h")]


def test_a_board_that_will_not_open_is_reported_and_the_pass_continues(lib):
    lib.boards.append("ghost")          # enumerated, but no DB behind it
    lib.add_task("hscc", "t_old")
    make_ws(lib, "hscc", "t_old", mode="plain")
    plan = wc.plan_clean(now=NOW, kanban_db=lib)
    assert [a["task_id"] for a in plan["actions"]] == ["t_old"]
    assert len(plan["errors"]) == 1 and "ghost" in plan["errors"][0]


def test_an_unreachable_lib_is_reported_not_raised(monkeypatch, lib):
    from hscc_daemon import autodown
    monkeypatch.setattr(autodown, "_KANBAN_LOAD", {"reason": "no hermes here"})
    monkeypatch.setattr(autodown, "_load_kanban_db_or_default", lambda: None)
    plan = wc.plan_clean(now=NOW)
    assert plan["actions"] == [] and plan["errors"] == ["no hermes here"]
    res = wc.execute({"actions": [{"kind": wc.KIND, "board": "hscc",
                                  "task_id": "t_x",
                                  "path": str(lib.workspace("hscc", "t_x"))}]},
                     apply=True)
    assert res["errors"] == ["no hermes here"] and res["removed"] == 0


def test_the_plan_is_json_safe(lib):
    lib.add_task("hscc", "t_old")
    make_ws(lib, "hscc", "t_old", mode="dirty", tmp_path=lib.home.parent / "g")
    plan = wc.plan_clean(now=NOW, kanban_db=lib)
    assert json.loads(json.dumps(plan)) == plan
    (skipped,) = plan["skipped"]
    assert set(skipped) == {"kind", "board", "task_id", "path", "size_bytes",
                            "worktrees", "note", "reason"}
    assert skipped["reason"] in wc.SKIP_REASONS


# --------------------------------------------------------------------------- #
# the skip rules (D3) — each one keeps the workspace
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mode,reason", [
    ("dirty", "dirty"),
    ("unpushed", "unpushed"),
    ("stashed", "stashed"),
    ("broken", "unreadable"),
])
def test_an_unsafe_repo_is_skipped_and_kept(lib, tmp_path, mode, reason):
    lib.add_task("hscc", "t_" + mode)
    ws = make_ws(lib, "hscc", "t_" + mode, mode=mode, tmp_path=tmp_path)
    plan = wc.plan_clean(now=NOW, kanban_db=lib)
    assert plan["actions"] == []
    (skipped,) = plan["skipped"]
    assert skipped["reason"] == reason and skipped["path"] == str(ws)
    assert ws.is_dir(), "a skip must keep the workspace"
    assert wc.execute(plan, apply=True, kanban_db=lib)["removed"] == 0
    assert ws.is_dir(), "execute must not remove what the plan kept"


def test_a_live_claim_is_skipped(lib):
    lib.add_task("hscc", "t_claim", claim_lock="host:1234", claim_expires=FUTURE)
    make_ws(lib, "hscc", "t_claim", mode="plain")
    plan = wc.plan_clean(now=NOW, kanban_db=lib)
    assert plan["actions"] == []
    (skipped,) = plan["skipped"]
    assert skipped["reason"] == "claim"


def test_an_expired_claim_is_not_live(lib):
    lib.add_task("hscc", "t_stale_claim", claim_lock="host:1234",
                 claim_expires=NOW - DAY)
    make_ws(lib, "hscc", "t_stale_claim", mode="plain")
    assert [a["task_id"] for a in
            wc.plan_clean(now=NOW, kanban_db=lib)["actions"]] == ["t_stale_claim"]


def test_a_nested_repo_within_maxdepth_is_probed(lib, tmp_path):
    """``find -maxdepth 3`` semantics: a repo at depth 2 is still in scope."""
    lib.add_task("hscc", "t_nested")
    ws = make_ws(lib, "hscc", "t_nested", mode="plain")
    origin = _bare_origin(tmp_path)
    _clone(origin, ws / "a" / "repo")
    (ws / "a" / "repo" / "README.md").write_text("uncommitted\n")
    (ws / "DECISION.md").write_text("decision\n")
    assert wc.plan_clean(now=NOW, kanban_db=lib)["actions"] == []
    assert wc.plan_clean(now=NOW, kanban_db=lib)["skipped"][0]["reason"] == "dirty"


def test_a_repo_deeper_than_maxdepth_is_out_of_scope(lib, tmp_path):
    """The boundary of the documented probe depth, pinned as the reference
    script's contract — not a claim that a deeper repo is safe."""
    lib.add_task("hscc", "t_deep")
    ws = make_ws(lib, "hscc", "t_deep", mode="plain")
    origin = _bare_origin(tmp_path)
    _clone(origin, ws / "a" / "b" / "repo")
    (ws / "a" / "b" / "repo" / "README.md").write_text("uncommitted\n")
    assert [a["task_id"] for a in
            wc.plan_clean(now=NOW, kanban_db=lib)["actions"]] == ["t_deep"]


# --------------------------------------------------------------------------- #
# the derived path (D2) — a row is never trusted with a delete
# --------------------------------------------------------------------------- #

def test_a_workspace_path_row_is_never_the_removal_target(lib, tmp_path):
    """A ``tasks.workspace_path`` pointing at a real checkout must not turn a
    cleanup into a repo delete: the engine derives the path and ignores the row.
    """
    precious = lib.home / "precious-checkout"
    precious.mkdir(parents=True)
    (precious / "keep-me.txt").write_text("the operator's real work\n")
    lib.add_task("hscc", "t_row", workspace_path=str(precious))
    ws = make_ws(lib, "hscc", "t_row", mode="plain")

    plan = wc.plan_clean(now=NOW, kanban_db=lib)
    assert [a["path"] for a in plan["actions"]] == [str(ws)]
    assert wc.execute(plan, apply=True, kanban_db=lib)["removed"] == 1
    assert not ws.exists()
    assert (precious / "keep-me.txt").is_file(), "the row's path was touched"


def test_a_task_id_that_walks_out_of_the_root_is_skipped(lib):
    """Containment is checked after resolving symlinks, on the DERIVED path."""
    precious = lib.home / "precious"
    precious.mkdir(parents=True)
    (precious / "keep-me.txt").write_text("not a workspace\n")
    root = lib.workspaces_root("hscc")
    root.mkdir(parents=True, exist_ok=True)   # `..` only walks from a live dir
    # An id deep enough to walk from the board's workspaces root back up to the
    # home — computed, not counted by hand, so it cannot quietly stop escaping.
    escape = "/".join([".."] * len(root.relative_to(lib.home).parts)) + "/precious"
    # The kernel (not Path.resolve, which resolves a missing tree lexically)
    # must really be able to walk out: that is the state the engine sees.
    assert os.path.isdir(str(root / escape)), "fixture must escape to a live dir"
    lib.add_task("hscc", escape)

    plan = wc.plan_clean(now=NOW, kanban_db=lib)
    assert plan["actions"] == []
    (skipped,) = plan["skipped"]
    assert skipped["reason"] == "unreadable"
    assert wc.execute(plan, apply=True, kanban_db=lib)["removed"] == 0
    assert (precious / "keep-me.txt").is_file()


def test_a_workspace_symlinked_out_of_the_root_is_skipped(lib):
    precious = lib.home / "precious"
    precious.mkdir(parents=True)
    (precious / "keep-me.txt").write_text("not a workspace\n")
    lib.add_task("hscc", "t_link")
    ws = lib.workspace("hscc", "t_link")
    ws.parent.mkdir(parents=True, exist_ok=True)
    ws.symlink_to(precious, target_is_directory=True)

    plan = wc.plan_clean(now=NOW, kanban_db=lib)
    assert plan["actions"] == [] and plan["skipped"][0]["reason"] == "unreadable"
    assert wc.execute(plan, apply=True, kanban_db=lib)["removed"] == 0
    assert (precious / "keep-me.txt").is_file()


def test_execute_rechecks_containment_on_a_forged_plan(lib):
    """The plan may arrive through ``--json``: a path outside the board's own
    workspaces root is skipped there too, not just at plan time.
    """
    precious = lib.home / "precious"
    precious.mkdir(parents=True)
    (precious / "keep-me.txt").write_text("not a workspace\n")
    forged = {"actions": [{"kind": wc.KIND, "board": "hscc", "task_id": "t_x",
                           "path": str(precious), "size_bytes": 1,
                           "worktrees": [], "note": "forged"}]}
    res = wc.execute(forged, apply=True, kanban_db=lib)
    assert res["removed"] == 0
    assert res["skipped"][0]["reason"] == "unreadable"
    assert (precious / "keep-me.txt").is_file()


# --------------------------------------------------------------------------- #
# execute: archive first, remove after (D4 + D5)
# --------------------------------------------------------------------------- #

def test_apply_archives_then_removes_and_the_archive_holds_the_doc(lib, tmp_path):
    """Ordering proof: after the run the workspace is GONE and the tarball that
    replaces it still lists the decision doc — so the archive happened while the
    dir existed. The archive-failure tests below close the other half (a failed
    archive never removes anything).
    """
    lib.add_task("hscc", "t_old")
    ws = make_ws(lib, "hscc", "t_old", mode="plain",
                 extra={"reports/probe.json": '{"ok": true}',
                        "node_modules/junk.js": "buildable junk",
                        "__pycache__/x.pyc": "buildable junk"})
    plan = wc.plan_clean(now=NOW, kanban_db=lib)
    archive = (lib.home / "kanban" / "workspace-archive" / "hscc"
               / "t_old.tar.gz")
    assert not archive.parent.exists(), "a dry plan must not create the archive dir"

    res = wc.execute(plan, apply=True, kanban_db=lib)
    assert res["apply"] is True and res["removed"] == 1 and res["errors"] == []
    assert res["freed_bytes"] == plan["actions"][0]["size_bytes"] > 0
    assert archive.is_file() and not ws.exists()
    assert not list(archive.parent.glob("*.part")), "a .part survived"
    listing = tar_listing(archive)
    assert "./DECISION.md" in listing, listing
    assert "./reports/probe.json" in listing, listing
    assert not [e for e in listing if "node_modules" in e or "__pycache__" in e], listing
    assert res["archived"] == [{"path": str(ws), "archive": str(archive),
                                "size_bytes": res["freed_bytes"]}]


def test_dry_run_reports_without_touching_anything(lib, tmp_path):
    lib.add_task("hscc", "t_old")
    ws = make_ws(lib, "hscc", "t_old", mode="clean", tmp_path=tmp_path)
    plan = wc.plan_clean(now=NOW, kanban_db=lib)
    before = snapshot(lib.home)

    res = wc.execute(plan, apply=False, kanban_db=lib)
    assert snapshot(lib.home) == before, "a dry run mutated the tree"
    assert res["apply"] is False and res["removed"] == 0 and res["freed_bytes"] == 0
    assert res["archived"] == [{"path": str(ws), "size_bytes": res["archived"][0]["size_bytes"],
                                "archive": str(lib.home / "kanban" / "workspace-archive"
                                               / "hscc" / "t_old.tar.gz")}]
    assert not (lib.home / "kanban" / "workspace-archive").exists()


def test_plan_writes_nothing_at_all(lib, tmp_path):
    """The read-only pin: paths, sizes and mtimes are identical before and
    after the plan — including inside the fixture's ``.git`` directories, which
    is exactly where a plain ``git status`` would have written.
    """
    lib.add_task("hscc", "t_clean")
    lib.add_task("hscc", "t_dirty")
    clean = make_ws(lib, "hscc", "t_clean", mode="clean", tmp_path=tmp_path / "a")
    make_ws(lib, "hscc", "t_dirty", mode="dirty", tmp_path=tmp_path / "b")
    # Make the index genuinely stale: that is the one state in which a plain
    # `git status` rewrites .git/index, so this is what pins the flag.
    os.utime(clean / "README.md", (NOW, NOW))
    before = snapshot(lib.home)
    plan = wc.plan_clean(now=NOW, kanban_db=lib)
    assert snapshot(lib.home) == before, "plan_clean is not read-only"
    assert lib.connects == [], "the write-capable door was opened"
    assert plan["actions"] and plan["skipped"]


def test_a_stale_plan_cannot_remove_a_workspace_that_went_dirty(lib, tmp_path):
    """execute re-probes: the plan is a snapshot of a moment, not a warrant."""
    lib.add_task("hscc", "t_old")
    ws = make_ws(lib, "hscc", "t_old", mode="clean", tmp_path=tmp_path)
    plan = wc.plan_clean(now=NOW, kanban_db=lib)
    assert plan["actions"]
    (ws / "work-in-progress.txt").write_text("someone is still working here\n")
    res = wc.execute(plan, apply=True, kanban_db=lib)
    assert res["removed"] == 0 and res["skipped"][0]["reason"] == "dirty"
    assert ws.is_dir()


def test_a_failed_archive_keeps_the_workspace_and_leaves_no_part(lib, monkeypatch):
    """The verify step fails on a part that really was written: the part goes,
    the workspace stays, and nothing is counted as removed."""
    lib.add_task("hscc", "t_old")
    ws = make_ws(lib, "hscc", "t_old", mode="plain")
    real_run = wc._run

    def truncated(argv, timeout):
        if argv[:2] == ["tar", "-tzf"]:
            return 1, "", "truncated archive"
        return real_run(argv, timeout)

    monkeypatch.setattr(wc, "_run", truncated)
    res = wc.execute(wc.plan_clean(now=NOW, kanban_db=lib), apply=True,
                     kanban_db=lib)
    assert res["removed"] == 0 and res["freed_bytes"] == 0
    assert res["skipped"][0]["reason"] == "archive-failed"
    assert "truncated archive" in res["skipped"][0]["note"]
    assert ws.is_dir(), "a failed archive must keep the workspace"
    archive_dir = lib.home / "kanban" / "workspace-archive" / "hscc"
    assert not list(archive_dir.glob("*")), f"leftovers: {list(archive_dir.iterdir())}"


def test_an_unwritable_archive_target_keeps_the_workspace(lib):
    lib.add_task("hscc", "t_old")
    ws = make_ws(lib, "hscc", "t_old", mode="plain")
    archive_root = lib.home / "kanban" / "workspace-archive"
    archive_root.mkdir(parents=True)
    os.chmod(archive_root, 0o500)
    try:
        res = wc.execute(wc.plan_clean(now=NOW, kanban_db=lib), apply=True,
                         kanban_db=lib)
    finally:
        os.chmod(archive_root, 0o755)
    assert res["removed"] == 0
    assert res["skipped"][0]["reason"] == "archive-failed"
    assert ws.is_dir()


def test_archive_first_means_the_archive_survives_a_failed_removal(lib, monkeypatch):
    """When the workspace cannot be deleted the archive is already safe, so the
    failure is an error naming it — not a lost archive and not a lost workspace.
    """
    lib.add_task("hscc", "t_old")
    ws = make_ws(lib, "hscc", "t_old", mode="plain")
    boom = OSError("busy")
    monkeypatch.setattr(wc.shutil, "rmtree", lambda *a, **k: (_ for _ in ()).throw(boom))
    res = wc.execute(wc.plan_clean(now=NOW, kanban_db=lib), apply=True,
                     kanban_db=lib)
    assert res["removed"] == 0 and res["archived"] == []
    assert len(res["errors"]) == 1 and "could not remove" in res["errors"][0]
    assert ws.is_dir()
    assert (lib.home / "kanban" / "workspace-archive" / "hscc"
            / "t_old.tar.gz").is_file()


# --------------------------------------------------------------------------- #
# worktrees leave through git (D5)
# --------------------------------------------------------------------------- #

def test_a_worktree_leaves_through_git_and_the_main_repo_forgets_it(lib, tmp_path):
    origin = _bare_origin(tmp_path)
    main = tmp_path / "main-checkout"
    _git("clone", str(origin), str(main), cwd=tmp_path)
    lib.add_task("hscc", "t_wt")
    ws = lib.workspace("hscc", "t_wt")
    _git("worktree", "add", "--detach", str(ws), "HEAD", cwd=main)
    (ws / "DECISION.md").write_text("decision made here\n")
    _git("add", "-A", cwd=ws)
    _git("commit", "-m", "t_wt: decision", cwd=ws)   # a clean worktree, detached

    plan = wc.plan_clean(now=NOW, kanban_db=lib)
    (action,) = plan["actions"]
    assert action["worktrees"] == [str(ws)], "a .git FILE is a worktree"

    res = wc.execute(plan, apply=True, kanban_db=lib)
    assert res["removed"] == 1 and res["errors"] == []
    assert not ws.exists()
    listed = _git("worktree", "list", "--porcelain", cwd=main).stdout
    assert str(ws) not in listed, "the parent repo kept a dead registration"
    assert (lib.home / "kanban" / "workspace-archive" / "hscc"
            / "t_wt.tar.gz").is_file()


def test_an_unresolvable_worktree_keeps_the_workspace(lib, tmp_path, monkeypatch):
    origin = _bare_origin(tmp_path)
    main = tmp_path / "main-checkout"
    _git("clone", str(origin), str(main), cwd=tmp_path)
    lib.add_task("hscc", "t_wt")
    ws = lib.workspace("hscc", "t_wt")
    _git("worktree", "add", "--detach", str(ws), "HEAD", cwd=main)
    monkeypatch.setattr(wc, "_git_common_dir", lambda _wt: None)

    res = wc.execute(wc.plan_clean(now=NOW, kanban_db=lib), apply=True,
                     kanban_db=lib)
    assert res["removed"] == 0
    assert len(res["errors"]) == 1 and "worktree not removed" in res["errors"][0]
    assert ws.is_dir(), "an unresolved worktree must never be rmtree'd"
    assert str(ws) in _git("worktree", "list", "--porcelain", cwd=main).stdout


def test_a_plain_clone_is_not_treated_as_a_worktree(lib, tmp_path):
    lib.add_task("hscc", "t_clone")
    make_ws(lib, "hscc", "t_clone", mode="clean", tmp_path=tmp_path)
    plan = wc.plan_clean(now=NOW, kanban_db=lib)
    assert plan["actions"][0]["worktrees"] == []


# --------------------------------------------------------------------------- #
# history survives (D7) + the read-only door
# --------------------------------------------------------------------------- #

def test_history_outlives_the_workspace(lib):
    """Comments, attachments and the board DB live outside ``workspaces/`` — so
    an apply must leave all three untouched, byte for byte on the DB.
    """
    lib.add_task("hscc", "t_old")
    ws = make_ws(lib, "hscc", "t_old", mode="plain")
    attachment = lib.attachments_root("hscc") / "t_old" / "report.pdf"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"%PDF-1.4 planted\n")
    db = lib.db_path("hscc")
    db_bytes = hashlib.sha256(db.read_bytes()).hexdigest()

    res = wc.execute(wc.plan_clean(now=NOW, kanban_db=lib), apply=True,
                     kanban_db=lib)
    assert res["removed"] == 1 and not ws.exists()
    assert attachment.read_bytes().startswith(b"%PDF"), "an attachment was lost"
    assert hashlib.sha256(db.read_bytes()).hexdigest() == db_bytes, \
        "the board DB changed — the engine writes to a board"
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT count(*) FROM tasks WHERE id = 't_old'").fetchone()[0] == 1
        assert con.execute("SELECT body FROM task_comments WHERE task_id = 't_old'").fetchone()[0]
    finally:
        con.close()


def test_the_archive_root_follows_the_injected_home(lib):
    """No literal home path in the engine: the archive lands under the home the
    injected lib resolves, two levels above its ``boards_root()``.
    """
    lib.add_task("default", "t_old")
    make_ws(lib, "default", "t_old", mode="plain")
    res = wc.execute(wc.plan_clean(now=NOW, kanban_db=lib), apply=True,
                     kanban_db=lib)
    assert res["removed"] == 1
    assert res["archived"][0]["archive"] == str(
        lib.home / "kanban" / "workspace-archive" / "default" / "t_old.tar.gz")
    assert str(lib.home) in res["archived"][0]["archive"]


def test_execute_tolerates_an_empty_or_foreign_plan(lib):
    assert wc.execute({}, apply=True, kanban_db=lib) == {
        "apply": True, "archived": [], "removed": 0, "skipped": [],
        "errors": [], "freed_bytes": 0}
    assert wc.execute({"actions": [{"kind": "delete_everything"}]},
                      apply=True, kanban_db=lib)["removed"] == 0


def test_a_vanished_workspace_is_reported_and_not_counted(lib):
    lib.add_task("hscc", "t_old")
    make_ws(lib, "hscc", "t_old", mode="plain")
    plan = wc.plan_clean(now=NOW, kanban_db=lib)
    shutil.rmtree(lib.workspace("hscc", "t_old"))
    res = wc.execute(plan, apply=True, kanban_db=lib)
    assert res["removed"] == 0 and len(res["errors"]) == 1
    assert "is gone" in res["errors"][0]


# --------------------------------------------------------------------------- #
# the injection is the authority (D1) — the plant hazard, shared predicate
# --------------------------------------------------------------------------- #

def test_the_injected_lib_is_the_only_board_authority(monkeypatch, lib):
    """A resident planted fake ``hermes_cli.kanban_db`` must not decide what a
    clean pass reads. The fake is recognised by the ONE predicate
    (``hermes_cli_plant``) and borrowed back with its own helpers, so this file
    adds no third definition of that hazard.
    """
    lib.add_task("hscc", "t_old")
    ws = make_ws(lib, "hscc", "t_old", mode="plain")

    def _poison(*_a, **_k):
        raise AssertionError("the clean engine consulted a name, not the injection")

    planted = types.ModuleType("hermes_cli.kanban_db")
    planted.__file__ = os.path.join(tempfile.gettempdir(), "hscc-planted-clean",
                                    "hermes_cli", "kanban_db.py")
    assert is_planted_path(planted.__file__), (
        "the stub does not look planted to the one predicate: "
        + planted.__file__)
    for name in ("list_boards", "list_tasks", "kanban_db_path", "boards_root",
                 "workspaces_root", "connect_closing"):
        setattr(planted, name, _poison)

    borrowed = evict_planted()
    try:
        monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", planted)
        plan = wc.plan_clean(now=NOW, kanban_db=lib)
        assert [a["task_id"] for a in plan["actions"]] == ["t_old"]
        assert wc.execute(plan, apply=True, kanban_db=lib)["removed"] == 1
    finally:
        restore_planted(borrowed)
    assert not ws.exists()


# --------------------------------------------------------------------------- #
# fidelity: the real hermes library, plan only (never apply)
# --------------------------------------------------------------------------- #

def test_real_hermes_roots_and_rows_drive_the_derivation(hermes_kanban_root,
                                                         monkeypatch):
    """The fake above encodes assumptions about hermes; this checks them against
    the library itself — real ``list_boards`` discovery, real ``Task.from_row``,
    real ``workspaces_root`` / ``boards_root``.

    Plan only. ``execute(apply=True)`` is never pointed at the real library
    from a test: every removal in this file happens under ``FakeLib``'s root.
    """
    kb = real_hermes.require_real_hermes()
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
                "HERMES_KANBAN_ATTACHMENTS_ROOT"):
        monkeypatch.delenv(var, raising=False)
    root, _boards = hermes_kanban_root

    slug = "cleanlane"
    db = kb.kanban_db_path(board=slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    kb.init_db(db)                       # hermes' own schema, explicit path
    (db.parent / "board.json").write_text(json.dumps(
        {"slug": slug, "kind": "board", "created_at": NOW, "archived": False}))
    con = sqlite3.connect(db)
    con.execute("INSERT INTO tasks (id, title, status, created_at, completed_at)"
                " VALUES ('t_real', 'a finished card', 'done', ?, ?)",
                (OLD, OLD))
    con.commit()
    con.close()

    ws = kb.workspaces_root(board=slug) / "t_real"
    ws.mkdir(parents=True)
    (ws / "DECISION.md").write_text("real schema, real roots\n")

    plan = wc.plan_clean(days=7, now=NOW, kanban_db=kb)
    assert [e for e in plan["errors"] if slug in e] == [], plan["errors"]
    actions = [a for a in plan["actions"] if a["board"] == slug]
    assert [a["task_id"] for a in actions] == ["t_real"], plan
    # The path is hermes' own workspaces_root joined with the row's id — the
    # engine re-derived nothing — and it stays inside the sandboxed home.
    assert actions[0]["path"] == str(ws)
    derived = Path(os.path.realpath(actions[0]["path"]))
    assert derived == Path(os.path.realpath(str(ws)))
    assert derived.is_relative_to(Path(os.path.realpath(str(root)))), \
        "derived outside the sandboxed home"
