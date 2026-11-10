"""Unit tests for the hscc kill switch (hscc_daemon/kill_switch.py).

Hermetic: a fake kanban lib backed by real in-memory sqlite boards replaces
``hermes_cli.kanban_db`` (injected, never imported from the live ~/.hermes).
The kill path is exercised with a ``signal_fn`` seam — a no-op killer — so a
test can never signal a real process. We COVER the full contract the card
demands:

  * list_running_tasks enumerates ONLY running tasks, across boards, and
    reports each task's pid + host_local ("names what will be stopped").
  * kill_running_task finds exactly ONE running task by id and delegates to
    ``_terminate_reclaimed_worker`` via the injectable kanban lib, returning
    its result dict verbatim ("reports what actually died").
  * kill on a non-running / missing task reports found=False (404 upstream).
  * host-locality is surfaced and enforced.

The second family (t_147201fa) arms a worker ``HERMES_KANBAN_DB`` + fence
marker against a fake lib that reproduces hermes' MEASURED precedence rule, and
asserts the decision recorded in the module docstring — guarded read, refusing
verb, both driven by one scan. Every conflict case is paired with a non-vacuity
assertion that the same armed env reads (or kills) the WRONG card without the
guard, and with companion cases proving the un-armed and legacy shapes behave
exactly as before.
"""

import os
import pathlib
import sqlite3
from contextlib import contextmanager

import pytest

from hscc_daemon import board_pin
from hscc_daemon import kill_switch as ks


def _make_conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE tasks ("
        " id TEXT PRIMARY KEY,"
        " title TEXT,"
        " assignee TEXT,"
        " status TEXT,"
        " claim_lock TEXT,"
        " worker_pid INTEGER,"
        " started_at INTEGER,"
        " claim_expires INTEGER)"
    )
    for r in rows:
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, claim_lock,"
            " worker_pid, started_at, claim_expires) VALUES (?,?,?,?,?,?,?,?)",
            (r.get("id"), r.get("title"), r.get("assignee"), r.get("status"),
             r.get("claim_lock"), r.get("worker_pid"), r.get("started_at"),
             r.get("claim_expires")),
        )
    conn.commit()
    return conn


class _FakeKb:
    """Fake Hermes kanban lib: list_boards + board-aware connect_closing."""

    def __init__(self, boards):
        # boards: dict {slug: [row-dicts]} OR list of row-dicts (default board)
        if isinstance(boards, (list, tuple)):
            boards = {"default": boards}
        self._conns = {slug: _make_conn(rows) for slug, rows in boards.items()}
        self._claims = {}

    def list_boards(self):
        return [{"slug": slug} for slug in self._conns]

    @contextmanager
    def connect_closing(self, board=None):
        conn = self._conns.get(board) or self._conns.get("default")
        yield conn

    # --- the two Hermes-core functions kill_switch delegates to ----------
    def _claimer_id(self):
        return "node-1:9999"

    def _terminate_reclaimed_worker(self, pid, claim_lock, *, signal_fn=None):
        # Record the delegate call so tests can assert WHO was signalled.
        self._claims[(pid, claim_lock)] = signal_fn
        # Simulate a successful SIGTERM-then-gone termination by default.
        return {
            "prev_pid": int(pid) if pid else None,
            "host_local": True,
            "termination_attempted": True,
            "terminated": True,
            "sigkill": False,
        }


# --------------------------------------------------------------------------- #
# list_running_tasks — "names what will be stopped"
# --------------------------------------------------------------------------- #

def _running_row(task_id="t_abc", pid=4242, claim="node-1:4242", wp=None,
                 assignee="worker", title="Runaway training", started=1700000000):
    return {"id": task_id, "title": title, "assignee": assignee,
            "status": "running", "claim_lock": claim, "worker_pid": wp,
            "started_at": started}


def test_list_only_running_across_boards():
    kb = _FakeKb({
        "default": [_running_row(task_id="t_abc"), {"id": "t_done",
                   "status": "done", "title": "x", "assignee": None,
                   "claim_lock": None, "worker_pid": None, "started_at": None}],
        "hscc": [_running_row(task_id="t_xyz", pid=7777, claim="node-1:7777")],
    })
    res = ks.list_running_tasks(kanban_db=kb)
    assert res["errors"] == []
    assert res["count"] == 2
    ids = {t["id"] for t in res["tasks"]}
    assert ids == {"t_abc", "t_xyz"}
    # only running tasks are listed
    assert all(t["status"] == "running" for t in res["tasks"])
    # board labels present
    assert {t["board"] for t in res["tasks"]} == {"default", "hscc"}


def test_list_reports_pid_and_host_local():
    kb = _FakeKb({"default": [
        _running_row(pid=4242, claim="node-1:4242"),          # host-local
        _running_row(task_id="t_remote", pid=999, claim="node-9:999"),  # remote
    ]})
    res = ks.list_running_tasks(kanban_db=kb)
    by_id = {t["id"]: t for t in res["tasks"]}
    assert by_id["t_abc"]["pid"] == 4242
    assert by_id["t_abc"]["host_local"] is True
    assert by_id["t_remote"]["pid"] == 999
    assert by_id["t_remote"]["host_local"] is False


def test_list_pid_falls_back_to_claim_lock():
    # No worker_pid column value -> pid parsed from claim_lock "node-1:4242"
    kb = _FakeKb({"default": [_running_row(pid=None, wp=None)]})
    res = ks.list_running_tasks(kanban_db=kb)
    assert res["tasks"][0]["pid"] == 4242


def test_list_none_unreachable_reports_error():
    class _None:
        def __init__(self):
            self.nodes = None
    # simulate _load returning None by passing an unreachable enum:
    # a lib whose connect raises
    class _Broken:
        @contextmanager
        def connect_closing(self, board=None):
            raise RuntimeError("DB unreachable")
        def list_boards(self):
            return [{"slug": "default"}]
    res = ks.list_running_tasks(kanban_db=_Broken())
    assert res["errors"]  # board unreadable reported, not crashed
    assert res["tasks"] == []
    assert res["count"] == 0


def test_list_unreachable_lib_reports_error(monkeypatch):
    monkeypatch.setattr(ks.autodown, "_load_kanban_db_or_default", lambda: None)
    res = ks.list_running_tasks(kanban_db=None)
    assert res["errors"]
    assert res["tasks"] == []
    assert res["count"] == 0


# --------------------------------------------------------------------------- #
# kill_running_task — "reports what actually died"
# --------------------------------------------------------------------------- #

def test_kill_found_and_delegates():
    kb = _FakeKb({"default": [_running_row()]})
    res = ks.kill_running_task("t_abc", kanban_db=kb)
    assert res["found"] is True
    assert res["task"]["id"] == "t_abc"
    assert res["task"]["board"] == "default"
    assert res["task"]["pid"] == 4242
    # delegate was called with (pid, claim_lock)
    assert (4242, "node-1:4242") in kb._claims
    # termination reported verbatim
    assert res["termination"]["terminated"] is True
    assert res["termination"]["termination_attempted"] is True


def test_kill_not_running_reports_not_found():
    kb = _FakeKb({"default": [{"id": "t_done", "status": "done", "title": "x",
                               "assignee": None, "claim_lock": None,
                               "worker_pid": None, "started_at": None}]})
    res = ks.kill_running_task("t_done", kanban_db=kb)
    assert res["found"] is False


def test_kill_missing_id_reports_not_found():
    kb = _FakeKb({"default": [_running_row()]})
    res = ks.kill_running_task("t_ghost", kanban_db=kb)
    assert res["found"] is False
    assert res["task"] is None


def test_kill_remote_task_surfaces_host_local_false():
    kb = _FakeKb({"default": [_running_row(pid=999, claim="node-9:999")]})
    res = ks.kill_running_task("t_abc", kanban_db=kb)
    assert res["found"] is True
    assert res["task"]["host_local"] is False


def test_kill_signal_fn_seam_is_forwarded():
    kb = _FakeKb({"default": [_running_row()]})
    sent = []
    def _killer(pid, sig):
        sent.append((pid, sig))
    res = ks.kill_running_task("t_abc", kanban_db=kb, signal_fn=_killer)
    # the no-op killer was passed through to _terminate_reclaimed_worker
    assert kb._claims[(4242, "node-1:4242")] is _killer


def test_kill_surfaces_sigkill_escalation():
    kb = _FakeKb({"default": [_running_row()]})
    kb._terminate_reclaimed_worker = lambda pid, claim, *, signal_fn=None: {
        "prev_pid": pid, "host_local": True, "termination_attempted": True,
        "terminated": True, "sigkill": True,
    }
    res = ks.kill_running_task("t_abc", kanban_db=kb)
    assert res["termination"]["sigkill"] is True


# --------------------------------------------------------------------------- #
# the board-pin family (t_147201fa) — a worker's HERMES_KANBAN_DB vs the verb
# --------------------------------------------------------------------------- #

#: The two markers hermes checks (``agent/delegation_context.py:76-93``). Tests
#: arm them explicitly rather than inheriting them, so each case reads the same
#: inside a real worker shell and outside one.
FENCE_VARS = ("HERMES_KANBAN_TASK", "HERMES_DELEGATED_CHILD_CONTEXT")


class _PinKb:
    """Fake lib reproducing hermes' MEASURED precedence rule (audit §1).

    ``kanban_db_path`` returns the ``HERMES_KANBAN_DB`` pin whenever a fence
    marker is set, discarding the ``board=`` it was handed — the behaviour
    measured in ``docs/audits/board-pin-t_b014c2b8.md`` — and rows follow the
    FILE, so resolving to the pinned path really does return the pinned board's
    cards (that is what makes the id-collision hazard real rather than
    theoretical). ``_terminate_reclaimed_worker`` RECORDS the (pid, lock) it was
    asked to kill instead of signalling anything, so "did the verb touch a
    process?" is an observable rather than an assumption.
    """

    def __init__(self, root, boards):
        self.root = str(root)
        self.slugs = list(boards)
        self.connects = []           # (board arg, resolved path, pin seen)
        self.killed = []             # (pid, claim_lock) handed to the killer
        self._by_path = {str(self._canonical(s)): _make_conn(rows)
                         for s, rows in boards.items()}

    def _canonical(self, slug):
        if slug in (None, "default"):
            return pathlib.Path(self.root) / "kanban.db"
        return (pathlib.Path(self.root) / "kanban" / "boards" / str(slug)
                / "kanban.db")

    def _fenced(self):
        return any((os.environ.get(v) or "").strip() for v in FENCE_VARS)

    def list_boards(self):
        return [{"slug": s} for s in self.slugs]

    def kanban_db_path(self, board=None):
        pin = (os.environ.get(board_pin.PIN_ENV) or "").strip()
        if pin and (board is None or self._fenced()):
            return pathlib.Path(pin)
        return self._canonical(board)

    @contextmanager
    def connect_closing(self, board=None):
        path = str(self.kanban_db_path(board=board))
        self.connects.append((board, path, os.environ.get(board_pin.PIN_ENV)))
        if path not in self._by_path:
            raise RuntimeError(f"no board file at {path}")
        yield self._by_path[path]

    def _claimer_id(self):
        return "node-1:9999"

    def _terminate_reclaimed_worker(self, pid, claim_lock, *, signal_fn=None):
        self.killed.append((pid, claim_lock))
        return {"prev_pid": int(pid) if pid else None, "host_local": True,
                "termination_attempted": True, "terminated": True,
                "sigkill": False}


def _two_board_fleet(root):
    """Two boards, one running card each, distinct ids and pids."""
    return _PinKb(root, {
        "alpha": [_running_row(task_id="t_alpha", pid=1111, claim="node-1:1111")],
        "beta": [_running_row(task_id="t_beta", pid=2222, claim="node-1:2222")],
    })


@pytest.fixture
def pin_alpha(tmp_path, monkeypatch):
    """Worker-shell state: ``HERMES_KANBAN_DB`` pins board ``alpha``, fence
    armed. Returns ``(kb, pin_path)``."""
    kb = _two_board_fleet(tmp_path / "kanban-home")
    pin = str(kb._canonical("alpha"))
    monkeypatch.setenv(board_pin.PIN_ENV, pin)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    return kb, pin


@pytest.fixture
def warnings(monkeypatch):
    """Capture what the guard says. Emission goes through ``daemon_ops.log``
    (daemon file + stderr, never stdout — stdout is the machine channel for
    every HSCC CLI), so patching THAT keeps the real ``_emit`` path under test.
    A fresh ``_WARNED`` set makes the once-per-process dedupe observable."""
    seen = []
    import hscc_daemon.daemon_ops as daemon_ops
    monkeypatch.setattr(daemon_ops, "log",
                        lambda msg, level="INFO", **kw: seen.append((level, msg)))
    monkeypatch.setattr(board_pin, "_WARNED", set())
    return seen


def _warn_text(seen):
    return " || ".join(m for _lvl, m in seen)


def test_pinned_listing_reads_the_real_fleet_and_says_so(pin_alpha, warnings):
    """The consequence the card names: ``kill status`` from a worker shell must
    stop reporting ONE pinned board as the fleet."""
    kb, pin = pin_alpha
    res = ks.list_running_tasks(kanban_db=kb)
    assert res["errors"] == []
    assert res["count"] == 2
    assert {t["id"] for t in res["tasks"]} == {"t_alpha", "t_beta"}, (
        f"beta was re-read as alpha: {res['tasks']}")
    text = _warn_text(warnings)
    assert text, "the override must be loud, not merely correct"
    assert pin in text, f"the warning must name the pin it overrode: {text}"
    assert "beta" in text, f"the warning must name the board asked for: {text}"
    assert "HERMES_KANBAN_TASK" in text, f"it must name the trigger: {text}"


def test_the_same_armed_env_reads_alpha_twice_without_the_guard(pin_alpha):
    """Non-vacuity: under the same env, a naive per-board open hands back
    alpha's rows for EVERY slug — the panel the phone would have been shown.

    If this ever fails, the fixture stopped reproducing the bug and every
    guarded test here proves nothing.
    """
    kb, _pin = pin_alpha
    naive = {}
    for slug in kb.slugs:
        with kb.connect_closing(board=slug) as conn:
            naive[slug] = [r["id"] for r in ks._running_rows(conn)]
    assert naive == {"alpha": ["t_alpha"], "beta": ["t_alpha"]}, naive


def test_pinned_verb_refuses_a_foreign_board_before_touching_a_process(pin_alpha):
    """The verb half of the decision: refuse, name both values, and do it
    BEFORE a pid is read or a signal is sent."""
    kb, pin = pin_alpha
    with pytest.raises(board_pin.BoardPinConflict) as exc:
        ks.kill_running_task("t_beta", kanban_db=kb)
    msg = str(exc.value)
    assert pin in msg, f"must name the pin: {msg}"
    assert "beta" in msg, f"must name the board asked for: {msg}"
    assert "kanban.db" in msg, f"both resolved paths belong in it: {msg}"
    assert kb.killed == [], "the refusal must precede any signal"


def test_pinned_verb_still_kills_on_the_board_the_pin_points_at(pin_alpha):
    """Companion: the fence keeps its mutation half without taking the worker's
    OWN board away from it — that one card it is allowed to stop."""
    kb, _pin = pin_alpha
    res = ks.kill_running_task("t_alpha", kanban_db=kb)
    assert res["found"] is True
    assert res["termination"]["terminated"] is True
    assert kb.killed == [(1111, "node-1:1111")]


def test_listing_and_verb_agree_on_every_row(pin_alpha):
    """The card's own clause: a listing that shows cards the verb will not
    touch is a trap. Each row's ``killable`` must be exactly what the verb does
    with that id — and the row's stated reason must be the verb's sentence."""
    kb, _pin = pin_alpha
    res = ks.list_running_tasks(kanban_db=kb)
    assert len(res["tasks"]) == 2, res["tasks"]
    for t in res["tasks"]:
        if t["killable"]:
            assert t["pin_conflict"] is None, t
            got = ks.kill_running_task(t["id"], kanban_db=kb)
            assert got["found"] is True, t
        else:
            assert t["pin_conflict"], f"an un-killable row must explain itself: {t}"
            with pytest.raises(board_pin.BoardPinConflict) as exc:
                ks.kill_running_task(t["id"], kanban_db=kb)
            assert str(exc.value) == t["pin_conflict"], (
                "the row's reason and the verb's refusal must be one sentence")
    assert res["pin_conflicts"] and len(res["pin_conflicts"]) == 1
    assert kb.killed == [(1111, "node-1:1111")], "only the owned board touched"


def test_unpinned_listing_and_verb_are_unchanged(tmp_path, monkeypatch, warnings):
    """Companion for the daemon/operator environment (no pin, no fence): every
    row killable, nothing warned, ``board=`` passed through exactly as before.
    """
    monkeypatch.delenv(board_pin.PIN_ENV, raising=False)
    for v in FENCE_VARS:
        monkeypatch.delenv(v, raising=False)
    kb = _two_board_fleet(tmp_path / "kanban-home")
    res = ks.list_running_tasks(kanban_db=kb)
    assert res["count"] == 2 and res["errors"] == []
    assert res["pin_conflicts"] == []
    assert all(t["killable"] and t["pin_conflict"] is None for t in res["tasks"])
    assert not warnings, _warn_text(warnings)
    assert [c[0] for c in kb.connects] == ["alpha", "beta"], kb.connects
    assert all(c[2] is None for c in kb.connects)
    assert ks.kill_running_task("t_beta", kanban_db=kb)["found"] is True
    assert kb.killed == [(2222, "node-1:2222")]


def test_legacy_single_board_shape_still_honours_the_pin(warnings, monkeypatch):
    """Companion: a lib WITHOUT ``list_boards`` (``multi=False``) keeps the
    no-board ``connect_closing()`` verbatim — there the pin IS the intended
    answer, so nothing is warned and the verb must not refuse what it can
    legitimately see."""
    calls = []
    rows = [_running_row(task_id="t_own", pid=3333, claim="node-1:3333")]

    class _Legacy:
        def __init__(self):
            self.conn = _make_conn(rows)
            self.killed = []

        @contextmanager
        def connect_closing(self, board=None):
            calls.append(board)
            yield self.conn

        def _claimer_id(self):
            return "node-1:9999"

        def _terminate_reclaimed_worker(self, pid, claim_lock, *, signal_fn=None):
            self.killed.append((pid, claim_lock))
            return {"prev_pid": pid, "host_local": True,
                    "termination_attempted": True, "terminated": True,
                    "sigkill": False}

    kb = _Legacy()
    monkeypatch.setenv(board_pin.PIN_ENV, "/elsewhere/pinned/kanban.db")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    res = ks.list_running_tasks(kanban_db=kb)
    assert calls == [None], "the legacy shape must keep the no-board call"
    assert res["count"] == 1 and res["tasks"][0]["board"] == "default"
    assert res["tasks"][0]["killable"] is True
    assert res["pin_conflicts"] == [] and not warnings, _warn_text(warnings)
    assert ks.kill_running_task("t_own", kanban_db=kb)["found"] is True
    assert kb.killed == [(3333, "node-1:3333")]


def test_a_refused_board_is_never_reported_as_an_unreadable_board(pin_alpha):
    """Both loops end in an ``except Exception`` handler. A ``BoardPinConflict``
    must be re-raised there, never folded into ``errors`` (or into a silent
    not-found) — same posture as ``autodown.archive_stale_task`` and
    ``kanban_blocked.recover_blocked_task``."""
    kb, _pin = pin_alpha

    @contextmanager
    def refusing(board=None):
        raise board_pin.BoardPinConflict("refused: the pin points elsewhere")
        yield  # pragma: no cover — unreachable, keeps this a context manager

    kb.connect_closing = refusing
    with pytest.raises(board_pin.BoardPinConflict) as exc:
        ks.list_running_tasks(kanban_db=kb)
    assert "the pin points elsewhere" in str(exc.value)
    with pytest.raises(board_pin.BoardPinConflict):
        ks.kill_running_task("t_beta", kanban_db=kb)
    assert kb.killed == []


def test_repeated_renders_warn_once_per_pin_and_board(pin_alpha, warnings):
    """The API panel polls (``routes_activity`` wraps this same listing), so the
    override must be said once per (pin, board), not once per render."""
    kb, _pin = pin_alpha
    for _ in range(4):
        assert ks.list_running_tasks(kanban_db=kb)["count"] == 2
    assert len(warnings) == 1, _warn_text(warnings)
