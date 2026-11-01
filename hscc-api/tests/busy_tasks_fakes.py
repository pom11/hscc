"""Fixture helper — a fake hermes kanban lib for the busy-signal tests.

GAP-C-5 (t_518183b3): the orchestrator busy signal no longer hand-rolls its own
per-board sqlite query; it delegates to ``hscc_daemon.kanban_live.collect()``.
These helpers let ``test_routes_orchestrator.py`` drive that delegation offline
over throwaway boards, so no test touches the operator's ~/.hermes.

FAITHFUL ON PURPOSE. hermes' own ``connect_closing`` opens a NEW connection for
every call and closes it on exit, and the busy signal runs its pass on its own
short-lived thread. A fake that handed out one cached connection (the obvious
shortcut) would fail with sqlite's thread-affinity error, every board would land
in ``errors``, and the fail-safe assertions would all "pass" while proving
nothing about the real path — which is exactly what happened when this fixture
was first written with ``:memory:`` connections. So each ``connect_closing``
opens its own read-only connection against a per-board file in a private temp
dir and closes it on exit, like any real read-only consumer.
"""

import contextlib
import os
import shutil
import sqlite3
import tempfile

KANBAN_SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
  priority INTEGER DEFAULT 0, created_at INTEGER, completed_at INTEGER,
  last_heartbeat_at INTEGER, current_run_id INTEGER);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT,
  status TEXT, started_at INTEGER, last_heartbeat_at INTEGER);
CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
  run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
"""

NOW = 1_800_000_000


class FakeKanbanDb:
    """A fake ``hermes_cli.kanban_db`` over throwaway on-disk boards.

    ``boards`` maps slug -> callable(conn) that inserts rows. ``broken`` lists
    slugs whose ``connect_closing`` raises — the unreadable-board case the busy
    signal must fail SAFE on.
    """

    def __init__(self, boards=None, broken=()):
        self.dir = tempfile.mkdtemp(prefix="gapc5-boards-")
        self.paths = {}
        self.broken = set(broken)
        for slug, build in (boards or {}).items():
            path = os.path.join(self.dir, f"{slug}.db")
            conn = sqlite3.connect(path)
            try:
                conn.executescript(KANBAN_SCHEMA)
                build(conn)
                conn.commit()
            finally:
                conn.close()
            self.paths[slug] = path

    # -- the shape kanban_live.collect() uses -------------------------------- #
    def list_boards(self):
        return [{"slug": s} for s in list(self.paths) + list(self.broken)]

    @contextlib.contextmanager
    def connect_closing(self, board=None, db_path=None):
        if board in self.broken:
            raise RuntimeError("database is locked")
        conn = sqlite3.connect(f"file:{self.paths[board]}?mode=ro", uri=True,
                               timeout=1.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # -- hygiene ------------------------------------------------------------- #
    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def running_card(conn, tid, assignee, profile=None, run_id=None, now=NOW):
    """One ``running`` task, optionally with a ``task_runs`` row for ``profile``.

    ``kanban_live`` reports a running row's ``worker`` as
    ``task_runs.profile or tasks.assignee`` — so ``profile=None`` is the
    "no run row" shape where the worker falls back to the assignee column.
    """
    if profile is not None:
        if run_id is None:
            run_id = int("".join(c for c in tid if c.isdigit()) or 1)
        conn.execute(
            "INSERT INTO task_runs VALUES (?,?,?,?,?,?)",
            (run_id, tid, profile, "running", now - 60, now - 10))
    conn.execute(
        "INSERT INTO tasks (id,title,assignee,status,created_at,current_run_id) "
        "VALUES (?,?,?,?,?,?)",
        (tid, f"card {tid}", assignee, "running", now - 120, run_id))


def done_card(conn, tid, assignee, now=NOW):
    conn.execute(
        "INSERT INTO tasks (id,title,assignee,status,created_at,completed_at) "
        "VALUES (?,?,?,?,?,?)",
        (tid, f"card {tid}", assignee, "done", now - 900, now - 60))
