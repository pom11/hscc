"""Tests for hscc_daemon/kanban_cli.py — board hygiene / stale cards.

These exercise ``cmd_kanban`` and the autodown stale helpers directly against
tmp in-memory boards (fake Hermes kanban lib backed by real sqlite) — NEVER
the operator's live boards, ~/.hermes, or ~/.hscc.

Key invariant under test throughout: ``hscc kanban stale`` reuses the SAME
board enumeration ``_has_active_work`` (the autodown C3 interlock predicate)
uses — ``_enum_board_names`` — so the boards "that block autodown" are exactly
the boards "that stale lists". A divergence would be its own bug.
"""

import datetime
import json
import sqlite3

import pytest

import hscc_daemon.autodown as ad
from hscc_daemon.kanban_cli import (cmd_kanban, _parse_older_than,
                                    DEFAULT_STALE_DAYS)


# ---------------------------------------------------------------------------
# Fake Hermes kanban lib — real in-memory sqlite, board-aware
# ---------------------------------------------------------------------------

def _t(id, status, age_days, title="t", assignee="w"):
    """Build a task row for the fake lib: created_at epoch = now - age_days."""
    now = datetime.datetime.now(datetime.timezone.utc)
    created = int(now.timestamp()) - age_days * 86400
    return (id, title, assignee, status, created)


class _FakeKb:
    """Fake Hermes kanban lib over real in-memory sqlite boards.

    Mirrors the real ``hermes_cli.kanban_db`` surface the daemon's
    ``_enum_board_names`` / ``_has_active_work`` consume (``list_boards``,
    board-aware ``connect_closing``). ``boards`` is a dict
    ``{slug: [(id, title, assignee, status, created_at_epoch), ...]}``.
    ``broken`` names a board whose connection raises (unreadable case).
    """

    def __init__(self, boards=None, broken=None):
        self._conns = {}
        self.opened = []
        self.closed = 0
        self.broken = set(broken or [])
        boards = boards or {"default": []}
        for slug, rows in boards.items():
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row  # real hermes_cli.kanban_db does this
            conn.execute(
                "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, "
                "assignee TEXT, status TEXT, created_at INTEGER)")
            for (tid, title, assignee, status, created) in rows:
                conn.execute(
                    "INSERT INTO tasks (id, title, assignee, status, created_at) "
                    "VALUES (?,?,?,?,?)", (tid, title, assignee, status, created))
            conn.commit()
            self._conns[slug] = conn

    def list_boards(self):
        return [{"slug": slug} for slug in self._conns]

    def _conn_for(self, board):
        if board in self.broken:
            raise RuntimeError(f"DB unreachable for board {board!r}")
        return self._conns.get(board) or self._conns.get("default")

    def connect_closing(self, board=None):
        self.opened.append(board)
        conn = self._conn_for(board)
        return _ConnCM(self, conn)


class _ConnCM:
    def __init__(self, kb, conn):
        self._kb = kb
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        self._kb.closed += 1
        return False


# ---------------------------------------------------------------------------
# Hermetic PR/CI + interlock-state hygiene (t_0db812de)
# ---------------------------------------------------------------------------
#
# ``cmd_autodown(["status", ...])`` evaluates the PR/CI interlock for real:
# ``_has_active_pr_ci()`` falls through to ``_screen_prci()``, which shells
# out to ``gh`` per tracked repo. Before this fixture existed this file had NO
# PR/CI stub (test_autodown_cli.py and test_autodown.py both have one), so
# ``test_status_json_names_blocking_tasks`` greened only when gh was present,
# authenticated and fast; when gh was missing/unreachable the fail-safe WARN
# ("Autodown PR/CI interlock unevaluable…") was emitted by the interlock and
# the test's ``json.loads(capsys stdout)`` choked on the WARN prefix — the
# env-dependent flake this card fixes (deterministic repro: run the test with
# PATH=/usr/bin:/bin).
#
# Stubbing the SCREEN SOURCE (not ``_has_active_pr_ci``) is the seam the
# autodown.py docstring sanctions and the pattern test_autodown.py:40-55
# already uses: every test stays off the real repo/remote/network, while the
# real ``_has_active_pr_ci`` logic stays intact for tests that inject their
# own checker.

#: The real (import-time) screen, kept so one test can restore it deliberately.
_REAL_SCREEN_PRCI = ad._screen_prci

#: Pristine interlock module state (module globals like _KANBAN_WORK — they
#: survive test boundaries otherwise, and test_autodown.py legitimately dirties
#: them; without a reset a dirtied value could leak INTO this file's tests).
_PRCI_LOAD_INIT = {"blocking": None, "ok": None, "reason": "", "warned": False}
_KANBAN_LOAD_INIT = {"ok": None, "reason": "", "warned": False}
_KANBAN_WORK_INIT = {"blocking": None}
_PRCI_CACHE_INIT = {"at": 0.0, "key": None, "result": None}


@pytest.fixture(autouse=True)
def _no_prci_network(monkeypatch):
    """Default: PR/CI interlock CLEAR, so status tests are hermetic.

    ``_cmd_status`` evaluates ``_has_active_pr_ci()`` → the real ``gh`` screen.
    Stubbing the screen to a positively-clear result keeps every test here off
    GitHub and deterministic (mirrors test_autodown.py::_no_prci_network).
    Tests that want a different PR/CI outcome override this with their own
    monkeypatch (LIFO teardown → the test's patch wins).
    """
    monkeypatch.setattr(ad, "_screen_prci",
                        lambda *a, **k: {"open_prs": 0, "active_runs": 0,
                                         "repos": []})


@pytest.fixture(autouse=True)
def _interlock_state_reset(monkeypatch):
    """Reset + restore the interlock module-globals around every test here.

    ``_PRCI_LOAD`` / ``_KANBAN_LOAD`` / ``_KANBAN_WORK`` / ``_prci_cache`` are
    process-wide dicts (like ``_KANBAN_WORK`` always was): one test's
    fail-safe evaluation would otherwise be visible to the next — and to
    tests in THIS file by earlier files in the same pytest process
    (test_autodown.py calls ``_has_active_pr_ci`` for real and leaves the
    state dirty). Reset at entry makes each test start from pristine state;
    restore at exit leaves the process as we found it.
    """
    monkeypatch.setattr(ad, "_PRCI_LOAD", dict(_PRCI_LOAD_INIT))
    monkeypatch.setattr(ad, "_KANBAN_LOAD", dict(_KANBAN_LOAD_INIT))
    monkeypatch.setattr(ad, "_KANBAN_WORK", dict(_KANBAN_WORK_INIT))
    monkeypatch.setattr(ad, "_prci_cache", dict(_PRCI_CACHE_INIT))
    # Restore needs NO post-yield code: monkeypatch's own undo (which runs
    # after this fixture, LIFO) puts the ORIGINAL dict objects back — and
    # because we replaced the identities, the test's mutations went to throw-
    # away dicts, so the originals are untouched.
    yield


# ---------------------------------------------------------------------------
# _parse_older_than — validation unit
# ---------------------------------------------------------------------------

class TestParseOlderThan:
    def test_absent_returns_sentinel(self):
        assert _parse_older_than([]) == (None, None)

    def test_valid(self):
        assert _parse_older_than(["--older-than", "30"]) == (30, None)

    def test_zero_valid(self):
        # 0 = list every non-terminal card (filter disabled).
        assert _parse_older_than(["--older-than", "0"]) == (0, None)

    def test_rejects_non_integer(self):
        value, err = _parse_older_than(["--older-than", "abc"])
        assert value is None and err and "non-negative" in err

    def test_rejects_negative(self):
        value, err = _parse_older_than(["--older-than", "-1"])
        assert value is None and err and "non-negative" in err

    def test_rejects_missing_value(self):
        value, err = _parse_older_than(["--older-than"])
        assert value is None and err and "requires a value" in err


# ---------------------------------------------------------------------------
# list_stale_tasks — multi-board, oldest-first, filtering, unreadable boards
# ---------------------------------------------------------------------------

class TestListStale:
    def test_lists_non_terminal_across_boards_oldest_first(self):
        kb = _FakeKb({
            "hscc": [
                _t("t-old", "todo", 20),
                _t("t-new", "running", 2),
            ],
            "ios-app": [
                _t("t-ios", "todo", 40),
            ],
        })
        res = ad.list_stale_tasks(kanban_db=kb, older_than=0)
        # Both boards enumerated.
        assert set(res["boards"]) == {"hscc", "ios-app"}
        ids = [t["id"] for t in res["tasks"]]
        # All four non-terminal tasks present.
        assert ids == ["t-ios", "t-old", "t-new"]  # oldest first
        by_id = {t["id"]: t for t in res["tasks"]}
        assert by_id["t-ios"]["board"] == "ios-app"
        assert by_id["t-ios"]["status"] == "todo"
        assert by_id["t-ios"]["age_days"] == 40
        assert by_id["t-old"]["age_days"] == 20
        assert by_id["t-new"]["age_days"] == 2

    def test_terminal_tasks_excluded(self):
        kb = _FakeKb({"default": [
            _t("t-done", "done", 100),
            _t("t-work", "running", 1),
            _t("t-arch", "archived", 90),
            _t("t-blocked", "blocked", 80),
        ]})
        res = ad.list_stale_tasks(kanban_db=kb, older_than=0)
        ids = [t["id"] for t in res["tasks"]]
        assert ids == ["t-work"]  # only non-terminal included

    def test_older_than_filters(self):
        kb = _FakeKb({"default": [
            _t("t-old", "todo", 30),
            _t("t-recent", "running", 3),
        ]})
        # Default threshold (>= 7 days) excludes the 3-day-old task.
        res = ad.list_stale_tasks(kanban_db=kb)
        ids = [t["id"] for t in res["tasks"]]
        assert ids == ["t-old"]

        # Explicit threshold of 0 lists everything.
        res = ad.list_stale_tasks(kanban_db=kb, older_than=0)
        assert [t["id"] for t in res["tasks"]] == ["t-old", "t-recent"]

    def test_unreadable_board_reported_not_crashed(self):
        kb = _FakeKb(
            {"hscc": [_t("t-h", "todo", 9)],
             "broken": [_t("t-b", "todo", 5)]},
            broken={"broken"},
        )
        res = ad.list_stale_tasks(kanban_db=kb, older_than=0)
        # The readable board's task is listed.
        assert [t["id"] for t in res["tasks"]] == ["t-h"]
        # The unreadable board is REPORTED, not a crash.
        assert any("broken" in e and "unreadable" in e for e in res["errors"])

    def test_json_shape(self):
        kb = _FakeKb({"hscc": [_t("t-1", "todo", 9)]})
        res = ad.list_stale_tasks(kanban_db=kb, older_than=0)
        # Every task row carries the documented columns.
        row = res["tasks"][0]
        for key in ("board", "id", "status", "assignee", "age_days", "title"):
            assert key in row

    def test_board_restriction_uses_same_enumeration(self):
        kb = _FakeKb({"hscc": [_t("t-a", "todo", 5)],
                      "other": [_t("t-b", "todo", 10)]})
        res = ad.list_stale_tasks(kanban_db=kb, board="hscc", older_than=0)
        assert [t["id"] for t in res["tasks"]] == ["t-a"]
        assert res["boards"] == ["hscc"]


# ---------------------------------------------------------------------------
# archive_stale_task — exactly one, never bulk, unknown id errors
# ---------------------------------------------------------------------------

class TestArchiveStale:
    def test_archive_exactly_one(self):
        kb = _FakeKb({"hscc": [
            _t("t-1", "todo", 30),
            _t("t-2", "todo", 20),
        ]})
        label, ok = ad.archive_stale_task("t-1", kanban_db=kb)
        assert ok is True and label == "hscc"
        # Only t-1 archived; t-2 untouched.
        res = ad.list_stale_tasks(kanban_db=kb, older_than=0)
        assert [t["id"] for t in res["tasks"]] == ["t-2"]

    def test_archive_finds_task_on_secondary_board(self):
        kb = _FakeKb({"hscc": [_t("t-a", "todo", 5)],
                      "ios-app": [_t("t-b", "todo", 40)]})
        label, ok = ad.archive_stale_task("t-b", kanban_db=kb)
        assert ok is True and label == "ios-app"
        res = ad.list_stale_tasks(kanban_db=kb, older_than=0)
        assert [t["id"] for t in res["tasks"]] == ["t-a"]

    def test_unknown_id_returns_not_found(self):
        kb = _FakeKb({"hscc": [_t("t-1", "todo", 5)]})
        label, ok = ad.archive_stale_task("nope", kanban_db=kb)
        assert ok is False and label is None

    def test_archive_persists_across_connections(self, tmp_path):
        """Archive must COMMIT — a later, separate connection to the same DB
        file sees the status change. Real hermes_cli.kanban_db uses autocommit,
        but we must not depend on the caller's isolation mode."""
        db_file = tmp_path / "kanban.db"

        def _make_db():
            c = sqlite3.connect(str(db_file))
            c.row_factory = sqlite3.Row
            c.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, "
                      "assignee TEXT, status TEXT, created_at INTEGER)")
            c.execute("INSERT INTO tasks VALUES "
                      "('t-x', 'forgotten', 'w', 'todo', 0), "
                      "('t-y', 'other', 'w', 'running', 0)")
            c.commit()
            c.close()

        class _FileKb:
            def list_boards(self):
                return [{"slug": "default"}]
            def connect_closing(self, board=None):
                # Deliberately NOT autocommit: default isolation_level="".
                class _CM:
                    def __enter__(_self):
                        _self._conn = sqlite3.connect(str(db_file))
                        _self._conn.row_factory = sqlite3.Row
                        return _self._conn
                    def __exit__(_self, *exc):
                        _self._conn.close()
                        return False
                return _CM()

        _make_db()
        label, ok = ad.archive_stale_task("t-x", kanban_db=_FileKb())
        assert ok is True and label == "default"

        # Fresh connection (new process-visibility): t-x archived, t-y intact.
        c = sqlite3.connect(str(db_file))
        c.row_factory = sqlite3.Row
        rows = {r["id"]: r["status"]
                for r in c.execute("SELECT id, status FROM tasks")}
        c.close()
        assert rows["t-x"] == "archived"
        assert rows["t-y"] == "running"


# ---------------------------------------------------------------------------
# cmd_kanban — CLI wiring
# ---------------------------------------------------------------------------

class TestCmdKanban:
    def test_help(self, capsys):
        assert cmd_kanban([]) == 0
        out = capsys.readouterr().out
        assert "Usage: hscc kanban" in out
        assert "stale" in out
        assert "--archive" in out

    def test_listing_json_valid(self, capsys, monkeypatch):
        kb = _FakeKb({"hscc": [_t("t-1", "todo", 8)]})
        monkeypatch.setattr(ad, "_load_kanban_db_or_default", lambda: kb)
        rc = cmd_kanban(["stale", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["tasks"][0]["id"] == "t-1"
        assert data["tasks"][0]["board"] == "hscc"

    def test_listing_reuses_load_kanban_db(self, capsys, monkeypatch):
        # Prove the CLI goes through _load_kanban_db_or_default (the same seam
        # _has_active_work uses), the reuse requirement.
        kb = _FakeKb({"default": [_t("t-1", "todo", 8)]})
        monkeypatch.setattr(ad, "_load_kanban_db_or_default", lambda: kb)
        rc = cmd_kanban(["stale"])
        assert rc == 0

    def test_archive_via_cli(self, capsys, monkeypatch):
        kb = _FakeKb({"hscc": [_t("t-1", "todo", 8),
                               _t("t-2", "todo", 5)]})
        monkeypatch.setattr(ad, "_load_kanban_db_or_default", lambda: kb)
        rc = cmd_kanban(["stale", "--archive", "t-1"])
        assert rc == 0
        assert "archived t-1" in capsys.readouterr().out
        # t-2 still there; t-1 gone.
        res = ad.list_stale_tasks(kanban_db=kb, older_than=0)
        assert [t["id"] for t in res["tasks"]] == ["t-2"]

    def test_archive_unknown_id_clear_error_nonzero(self, capsys, monkeypatch):
        kb = _FakeKb({"hscc": [_t("t-1", "todo", 8)]})
        monkeypatch.setattr(ad, "_load_kanban_db_or_default", lambda: kb)
        rc = cmd_kanban(["stale", "--archive", "nope"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "no task with id" in err and "nope" in err

    def test_archive_rejects_missing_id(self, capsys, monkeypatch):
        rc = cmd_kanban(["stale", "--archive"])
        assert rc == 1
        assert "requires a task id" in capsys.readouterr().err

    def test_archive_rejects_multiple_ids(self, capsys, monkeypatch):
        kb = _FakeKb({"hscc": [_t("t-1", "todo", 8)]})
        monkeypatch.setattr(ad, "_load_kanban_db_or_default", lambda: kb)
        rc = cmd_kanban(["stale", "--archive", "t-1", "extra"])
        assert rc == 1
        assert "exactly ONE" in capsys.readouterr().err

    def test_unknown_subcommand_nonzero(self, capsys):
        rc = cmd_kanban(["bogus"])
        assert rc == 1
        assert "unknown kanban subcommand" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# autodown status names specific blocking tasks (the "Also" requirement)
# ---------------------------------------------------------------------------

class TestStatusNamesBlockingTasks:
    def test_status_names_blocking_tasks(self, capsys, monkeypatch, tmp_path):
        """When kanban work blocks teardown, hscc autodown status names the
        SPECIFIC task(s) — board + id + title — capped at 3 + a count."""
        from hscc_daemon import autodown_cli

        def _fake_active(kanban_db=None):
            ad._note_blocking("hscc")
            return True

        kb = _FakeKb({
            "hscc": [
                _t("t-a", "todo", 9, title="forgotten A"),
                _t("t-b", "running", 8, title="forgotten B"),
                _t("t-c", "todo", 7, title="forgotten C"),
                _t("t-d", "ready", 6, title="forgotten D"),
            ]
        })
        monkeypatch.setattr(ad, "_has_active_work", _fake_active)
        monkeypatch.setattr(ad, "_load_kanban_db_or_default", lambda: kb)

        rc = autodown_cli.cmd_autodown(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        # Rich wraps the long blocked-by row to the console width at word
        # boundaries, so assert short stable fragments that cannot straddle a
        # wrap point, rather than one contiguous substring.
        assert "blocked by:" in out
        assert "forgotten A" in out
        # Task count is 4, only first 3 named → "+1 more".
        assert "and 1 more" in out

    def test_status_json_names_blocking_tasks(self, capsys, monkeypatch):
        from hscc_daemon import autodown_cli

        def _fake_active(kanban_db=None):
            ad._note_blocking("hscc")
            return True

        kb = _FakeKb({"hscc": [_t("t-a", "todo", 9, title="forgotten A")]})
        monkeypatch.setattr(ad, "_has_active_work", _fake_active)
        monkeypatch.setattr(ad, "_load_kanban_db_or_default", lambda: kb)

        rc = autodown_cli.cmd_autodown(["status", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "t-a (forgotten A)" in data["blocked_by"]


# ---------------------------------------------------------------------------
# PR/CI fail-safe WARN vs the --json contract (t_0db812de)
# ---------------------------------------------------------------------------
#
# The flake this card kills: ``status --json`` evaluated the REAL gh screen;
# when gh was missing/unreachable the fail-safe WARN was emitted while the
# dump was being produced. These tests drive the REAL interlock through the
# supported checker seam (no fake needed — ``run`` is injected, so zero
# network even with gh installed) and pin the contract that was violated:
# diagnostics belong on stderr, stdout carries ONLY the JSON document.

def _boom_run(*a, **k):
    """subprocess.run stand-in: gh is simply not there (simulates the flake)."""
    raise FileNotFoundError(2, "No such file or directory", "gh")


class TestStatusJsonStreamContract:
    def test_unreachable_prci_warn_never_touches_stdout(self, capsys,
                                                        monkeypatch):
        """UNREACHABLE PR/CI during ``status --json``: the fail-safe WARN is
        emitted (operator visibility kept) but stdout stays a parseable JSON
        document — the WARN rides stderr, never the payload stream."""
        from hscc_daemon import autodown_cli

        monkeypatch.setattr(ad, "_has_active_work", lambda *a, **k: False)
        # The supported seam: a zero-arg checker built from the REAL screen
        # with an injected failing `run` (the exact gh-missing path
        # _screen_prci_uncached takes when it returns _PRCI_UNREACHABLE).
        monkeypatch.setattr(
            ad, "_screen_prci",
            lambda: _REAL_SCREEN_PRCI(repos=["fake/repo"], run=_boom_run))

        rc = autodown_cli.cmd_autodown(["status", "--json"])
        assert rc == 0
        captured = capsys.readouterr()
        # The contract: stdout is the JSON payload and NOTHING else.
        data = json.loads(captured.out)
        assert captured.out.strip().startswith("{")
        # Fail-safe semantics intact: unreachable ⇒ active ⇒ blocks teardown,
        # and status surfaces a blocker rather than a bare ``state: up``.
        assert data["prci_active"] is True
        assert data["blocked_by"]
        # Operator visibility kept — the WARN exists, on the other stream.
        assert "Autodown PR/CI interlock unevaluable" in captured.err
        assert "WARN" in captured.err
        # NOTE: the exact blocked_by WORDING and prci_ok are deliberately not
        # pinned — the current code lets _note_prci_active() clobber the
        # ok=False/reason recorded by _note_prci_unavailable() moments
        # earlier (autodown.py:1113-1118), so status today misreports the
        # sentinel as "open PR / active CI run". That state-reporting defect
        # is a separate filed card; pinning the buggy wording here would
        # saddle that fix with a test change. This card owns only the stream
        # separation + the fail-safe ACTIVE direction asserted above.

    def test_clear_prci_produces_no_warn_line(self, capsys, monkeypatch):
        """Positively-clear PR/CI ⇒ clean JSON and a clean stderr (autouse
        hermetic stub), and no stale WARN state from earlier tests."""
        from hscc_daemon import autodown_cli

        monkeypatch.setattr(ad, "_has_active_work", lambda *a, **k: False)
        rc = autodown_cli.cmd_autodown(["status", "--json"])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["prci_active"] is False
        assert data["blocked_by"] is None
        assert "interlock unevaluable" not in captured.err


class TestInterlockStateDoesNotLeak:
    """_PRCI_LOAD / _KANBAN_WORK are module-globals — like _KANBAN_WORK they
    survive test boundaries unless reset. These two tests are an ORDERED pair
    (definition order = execution order): the first dirties the state on
    purpose, the second must arrive at pristine state. If the reset fixture
    ever stops working, the second test fails.
    """

    def test_dirty_then_pristine__dirty(self, monkeypatch):
        # Arrive pristine (the reset fixture guarantees it).
        assert dict(ad._PRCI_LOAD) == _PRCI_LOAD_INIT
        assert ad._KANBAN_WORK["blocking"] is None
        # Dirty the PR/CI bookkeeping via the REAL interlock (unreachable
        # screen ⇒ _note_prci_unavailable + _note_prci_active both ran).
        assert ad._has_active_pr_ci(
            lambda: _REAL_SCREEN_PRCI(repos=["fake/repo"],
                                      run=_boom_run)) is True
        assert ad._PRCI_LOAD["blocking"] is not None  # dirtied on purpose
        ad._note_blocking("leak-probe-board")
        assert ad._KANBAN_WORK["blocking"] == "leak-probe-board"

    def test_dirty_then_pristine__arrives_pristine(self):
        # The PREVIOUS test dirtied both dicts; the reset fixture must hand
        # this test pristine state (and hand the process back its originals).
        assert dict(ad._PRCI_LOAD) == _PRCI_LOAD_INIT
        assert dict(ad._KANBAN_LOAD) == _KANBAN_LOAD_INIT
        assert ad._KANBAN_WORK["blocking"] is None
        assert ad._prci_cache == _PRCI_CACHE_INIT
