"""Tests for `flightdeck legacy-cards` — surfacing cards on orphan/archived boards.

Covers the `flightdeck/commands/legacy.py` command layer: which cards are
listed (orphan board OR unattributed workspace), the suggestion rule (only when
workspace_path resolves to a project; never guessed from title), the
``--include-archived-boards`` toggle, the stable --json shape, and the MCP
adapter equality. Nothing touches a live board or repo: board reads come from
``kanban.list_cards`` / ``args.cards`` (monkeypatched), the archived scan from
an injected reader, and attribution from ``kanban.project_for_card``.
"""

import argparse
import json
import os

import pytest

from flightdeck.commands import legacy as cmd
from flightdeck.core import kanban, registry
from flightdeck.core.registry import Project


def _args(**overrides):
    base = {
        "registry": "/tmp/reg.yaml",
        "json": False,
        "include_archived_boards": False,
        "cards": None,
        "list_archived_cards": None,
        "kdb": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _project(name="sphoin_engine", board="sphoin", repo="/repo"):
    return Project(name=name, repo=repo, board=board)


def _hcard(cid, title="task", status="todo", board="ecofire", body=None,
           workspace_path: str | None = "/repo"):
    return {
        "id": cid,
        "title": title,
        "status": status,
        "board": board,
        "branch": f"wt/{cid}",
        "body": body,
        "workspace_path": workspace_path,
    }


# --------------------------------------------------------------------------- #
# A card on an unmapped (orphan) board is listed.
# --------------------------------------------------------------------------- #


def test_card_on_unmapped_board_is_listed(monkeypatch, capsys):
    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project()],
    )
    monkeypatch.setattr(cmd.kanban, "list_cards", lambda **kw: [
        _hcard("c1", title="orphan work", status="review", board="ecofire"),
    ])
    rc = cmd.cmd_legacy_cards(_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "BOARD: ecofire" in out
    assert "c1" in out


def test_card_on_registered_board_with_attributed_workspace_is_not_listed(monkeypatch, capsys):
    """Cleanly attributed card (registered board + resolves to a project) → absent."""
    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project()],
    )
    monkeypatch.setattr(cmd.kanban, "list_cards", lambda **kw: [
        _hcard("c1", title="clean", status="running", board="sphoin",
               workspace_path="/repo"),
    ])
    rc = cmd.cmd_legacy_cards(_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "no unmanaged cards" in out


# --------------------------------------------------------------------------- #
# A card whose workspace_path attributes to a real project is listed WITH that
# suggestion (even though it sits on the wrong/orphan board).
# --------------------------------------------------------------------------- #


def test_orphan_board_card_with_resolvable_workspace_suggests_target(monkeypatch, capsys):
    """orphan board, but workspace resolves to a registered project → suggested target."""
    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project()],
    )
    monkeypatch.setattr(cmd.kanban, "list_cards", lambda **kw: [
        _hcard("c1", title="old sphoin import", board="ecofire",
               workspace_path="/repo"),
    ])
    rc = cmd.cmd_legacy_cards(_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "BOARD: ecofire" in out
    assert "SUGGESTED TARGET PROJECT: sphoin_engine" in out


# --------------------------------------------------------------------------- #
# A card with no resolvable hint has NO suggestion (never guessed from title).
# --------------------------------------------------------------------------- #


def test_unattributed_card_has_no_suggestion(monkeypatch, capsys):
    """workspace_path resolves to no project → no suggestion, never guessed from title."""
    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project()],
    )
    monkeypatch.setattr(cmd.kanban, "list_cards", lambda **kw: [
        _hcard("c1", title="obviously about sphoin stuff", board="ecofire",
               workspace_path=None),
    ])
    rc = cmd.cmd_legacy_cards(_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "BOARD: ecofire" in out
    # No mechanical hint exists → no suggestion line, and the project name is
    # never guessed from the title text (which mentions "sphoin").
    assert "SUGGESTED TARGET PROJECT" not in out
    assert "sphoin_engine" not in out


def test_registered_board_unattributed_card_is_listed_without_suggestion(monkeypatch, capsys):
    """Card on a REGISTERED board but unattributed workspace → still listed, no suggestion."""
    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project()],
    )
    monkeypatch.setattr(cmd.kanban, "list_cards", lambda **kw: [
        _hcard("c1", title="lost card", board="sphoin", workspace_path=None),
    ])
    rc = cmd.cmd_legacy_cards(_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "BOARD: sphoin" in out
    assert "SUGGESTED TARGET PROJECT" not in out


# --------------------------------------------------------------------------- #
# --include-archived-boards toggles the extra scan.
# --------------------------------------------------------------------------- #


def test_archived_scan_is_off_by_default(monkeypatch):
    """Without the flag, the archived reader is never invoked."""
    called = {"n": 0}

    def fake_reader(_kdb=None):
        called["n"] += 1
        return [_hcard("arch", title="archived", board="archived/ecofire-1")]

    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(cmd.kanban, "list_cards", lambda **kw: [_hcard("c1")])
    rc = cmd.cmd_legacy_cards(_args(list_archived_cards=fake_reader))
    assert rc == 0
    assert called["n"] == 0


def test_archived_scan_runs_when_flag_set(monkeypatch, capsys):
    """With the flag, the archived reader is invoked and its cards are listed."""
    called = {"n": 0}

    def fake_reader(_kdb=None):
        called["n"] += 1
        return [_hcard("arch", title="archived work", board="archived-ecofire")]

    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(cmd.kanban, "list_cards", lambda **kw: [_hcard("c1")])
    rc = cmd.cmd_legacy_cards(_args(include_archived_boards=True,
                                    list_archived_cards=fake_reader))
    assert rc == 0
    assert called["n"] == 1
    out = capsys.readouterr().out
    assert "archived work" in out


# --------------------------------------------------------------------------- #
# kanban.list_archived_board_cards — the separate read path itself
# --------------------------------------------------------------------------- #


class _FakeKdb:
    """Kanban-library stand-in exposing boards_root + connect/list_tasks."""

    def __init__(self, db_paths, tasks_by_path):
        self.db_paths = db_paths  # dir names -> path
        self.tasks_by_path = tasks_by_path  # db path -> list of task objects
        self.connected = []

    def boards_root(self):
        return self.db_paths["root"]

    def connect(self, db_path=None, *, board=None):
        assert db_path is not None, "archived read must pass db_path explicitly"
        self.connected.append(str(db_path))
        return self

    def close(self):
        pass

    def list_tasks(self, conn, include_archived=False):
        p = conn  # we used self as the conn; recover the path from the last connect
        return self.tasks_by_path.get(self.connected[-1], [])


class _Task:
    def __init__(self, tid, title, status="todo", body=None, workspace_path=None,
                 branch_name=None, created_at=100, started_at=None, completed_at=None,
                 workspace_kind=None):
        self.id = tid
        self.title = title
        self.status = status
        self.body = body
        self.branch_name = branch_name
        self.created_at = created_at
        self.started_at = started_at
        self.completed_at = completed_at
        self.workspace_kind = workspace_kind
        self.workspace_path = workspace_path
        self.assignee = None
        self.last_heartbeat_at = None


def test_list_archived_board_cards_reads_each_archived_db(tmp_path):
    """The separate read path labels and reads each archived board's kanban.db."""
    root = tmp_path / "boards" / "_archived"
    (root / "ecofire-123").mkdir(parents=True)
    (root / "sphoin-456").mkdir()
    eco_db = root / "ecofire-123" / "kanban.db"
    (root / "ecofire-123" / "kanban.db").touch()

    tasks = {
        str(eco_db): [_Task("a1", "old card", workspace_path="/repo")],
    }
    kdb = _FakeKdb({"root": tmp_path / "boards"}, tasks)

    cards = kanban.list_archived_board_cards(_kdb=kdb, _archived_root=str(root))
    assert len(cards) == 1
    assert cards[0]["id"] == "a1"
    assert cards[0]["board"] == "ecofire-123"
    assert kdb.connected == [str(eco_db)]


def test_list_archived_board_cards_sources_override(tmp_path):
    """An explicit _sources list replaces the filesystem scan (test seam)."""
    tasks = {
        "/x/ecofire-a/kanban.db": [_Task("a1", "archived task", status="done")],
    }
    kdb = _FakeKdb({"root": tmp_path}, tasks)
    sources = [("ecofire-a", "/x/ecofire-a/kanban.db")]
    cards = kanban.list_archived_board_cards(_kdb=kdb, _sources=sources)
    assert len(cards) == 1
    assert cards[0]["id"] == "a1"
    assert cards[0]["board"] == "ecofire-a"


def test_list_archived_board_cards_empty_root(tmp_path):
    """An absent/empty _archived/ root is an empty list, never an error."""
    root = tmp_path / "boards" / "_archived"  # does not exist
    kdb = _FakeKdb({"root": tmp_path / "boards"}, {})
    assert kanban.list_archived_board_cards(_kdb=kdb, _archived_root=str(root)) == []


# --------------------------------------------------------------------------- #
# --json shape is stable
# --------------------------------------------------------------------------- #


def test_json_shape_is_stable(monkeypatch, capsys):
    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project()],
    )
    monkeypatch.setattr(cmd.kanban, "list_cards", lambda **kw: [
        _hcard("c1", title="orphan", board="ecofire", workspace_path="/repo",
               status="review", body="some body here"),
    ])
    rc = cmd.cmd_legacy_cards(_args(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert len(payload) == 1
    group = payload[0]
    assert set(group) == {"board", "cards"}
    assert group["board"] == "ecofire"
    card = group["cards"][0]
    assert set(card) == {
        "id", "status", "title", "body_excerpt", "workspace_path", "suggestion",
    }
    assert card["id"] == "c1"
    assert card["suggestion"] == "sphoin_engine"
    assert card["body_excerpt"] == "some body here"


def test_json_unattributed_suggestion_is_none(monkeypatch, capsys):
    """No resolvable hint → suggestion is None in JSON, never a guessed string."""
    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project()],
    )
    monkeypatch.setattr(cmd.kanban, "list_cards", lambda **kw: [
        _hcard("c1", title="old sphoin-ish", board="ecofire",
               workspace_path=None),
    ])
    rc = cmd.cmd_legacy_cards(_args(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    card = payload[0]["cards"][0]
    assert card["suggestion"] is None


def test_body_excerpt_is_capped_at_200_chars(monkeypatch, capsys):
    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project()],
    )
    long_body = "x" * 500
    monkeypatch.setattr(cmd.kanban, "list_cards", lambda **kw: [
        _hcard("c1", title="orphan", board="ecofire", body=long_body),
    ])
    rc = cmd.cmd_legacy_cards(_args(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    excerpt = payload[0]["cards"][0]["body_excerpt"]
    assert len(excerpt) <= 200 + 1  # allow the trailing ellipsis char
    assert excerpt.endswith("…")


# --------------------------------------------------------------------------- #
# grouping by board
# --------------------------------------------------------------------------- #


def test_grouped_by_board(monkeypatch, capsys):
    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project()],
    )
    monkeypatch.setattr(cmd.kanban, "list_cards", lambda **kw: [
        _hcard("c1", title="orphan A", board="ecofire"),
        _hcard("c2", title="orphan B", board="ecofire"),
        _hcard("c3", title="other board", board="hermes-prfix"),
    ])
    rc = cmd.cmd_legacy_cards(_args())
    assert rc == 0
    out = capsys.readouterr().out
    # Each board appears once, with its own header.
    assert out.count("BOARD: ecofire") == 1
    assert out.count("BOARD: hermes-prfix") == 1
    assert "c1" in out and "c2" in out and "c3" in out


# --------------------------------------------------------------------------- #
# command is discovered + run wiring
# --------------------------------------------------------------------------- #


def test_command_is_discovered():
    from flightdeck.cli import build_parser

    args = build_parser().parse_args(["legacy-cards"])
    assert args.command == "legacy-cards"
    assert args.func is not None


def test_run_attaches_seams(monkeypatch):
    """run() defaults the injectable seams and dispatches to cmd_legacy_cards.

    include_archived_boards=True is kept (not weakened to False) because the
    interesting case is seam-attachment happening BEFORE the archived branch
    reads it. list_archived_board_cards is monkeypatched so this stays
    hermetic — the real one imports Hermes' hermes_cli.kanban_db, which
    raised KanbanError on any machine without a real ~/.hermes/hermes-agent
    checkout (e.g. every CI runner), even though list_cards was already
    mocked.
    """
    ns = argparse.Namespace(include_archived_boards=True)
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(cmd.kanban, "list_cards", lambda **kw: [])
    monkeypatch.setattr(cmd.kanban, "list_archived_board_cards", lambda **kw: [])
    rc = cmd.run(ns, "/tmp/reg.yaml")
    assert rc == 0
    assert ns.registry == "/tmp/reg.yaml"
    assert ns.include_archived_boards is True
    assert ns.list_archived_cards is None


# --------------------------------------------------------------------------- #
# find_card — falls back to archived boards when the live scan misses
# --------------------------------------------------------------------------- #


class _FindKdb:
    """kanban stand-in with both live boards and a real on-disk archived DB.

    Models the real host: live boards resolved by ``connect(board=slug)`` and
    an archived board as a real ``_archived/<label>/kanban.db`` found through
    ``boards_root()``. ``_Task`` (above) is the card task. Tracks how many
    times the archived path was entered so tests can assert the fast path
    skips it.
    """

    def __init__(self, root, live_tasks, archived_tasks_by_path):
        self.root = root                     # boards root Path
        self.live_tasks = live_tasks         # {live_slug: [Task, ...]}
        self.archived_tasks_by_path = archived_tasks_by_path  # {db_path: [Task,...]}
        self.connected = []                  # last connect target (slug or path)
        self.archived_scans = 0

    def list_boards(self):
        return [{"slug": s} for s in self.live_tasks]

    def boards_root(self):
        return self.root

    def connect(self, db_path=None, *, board=None):
        if db_path is not None:
            self.connected.append(str(db_path))
            self.archived_scans += 1
        else:
            self.connected.append(str(board))
        return self

    def close(self):
        pass

    def list_tasks(self, conn, include_archived=False):
        last = self.connected[-1]
        if last in self.live_tasks:
            return self.live_tasks[last]
        return self.archived_tasks_by_path.get(last, [])


def _archived_fixture(tmp_path):
    """A boards root with one live board and one archived board on disk."""
    arch_root = tmp_path / "boards" / "_archived"
    (arch_root / "ecofire-1786704010").mkdir(parents=True)
    eco_db = arch_root / "ecofire-1786704010" / "kanban.db"
    eco_db.touch()
    root = tmp_path / "boards"
    return root, str(eco_db)


def test_find_card_live_hit_skips_archived_scan(tmp_path):
    """A card on a live board is found via the fast path; archives never scanned."""
    root, eco_db = _archived_fixture(tmp_path)
    kdb = _FindKdb(
        root=root,
        live_tasks={"hscc": [_Task("t_live", "live card")]},
        archived_tasks_by_path={eco_db: [_Task("t_arch", "arch card")]},
    )
    card = kanban.find_card("t_live", _kdb=kdb)
    assert card is not None and card["id"] == "t_live"
    assert card["board"] == "hscc"
    # Live board hit: no archived path needed, so no _board_path leak.
    assert "_board_path" not in card
    assert kdb.archived_scans == 0


def test_find_card_falls_back_to_archived_when_live_misses(tmp_path):
    """A card only on an archived board is found via the archived fallback."""
    root, eco_db = _archived_fixture(tmp_path)
    kdb = _FindKdb(
        root=root,
        live_tasks={"hscc": [_Task("t_live", "live card")]},
        archived_tasks_by_path={eco_db: [_Task("t_arch", "arch card")]},
    )
    card = kanban.find_card("t_arch", _kdb=kdb)
    assert card is not None and card["id"] == "t_arch"
    # 'board' is the archived board's actual slug so the provenance/plan reads
    # naturally; '_board_path' lets a caller OPEN it via db_path.
    assert card["board"] == "ecofire-1786704010"
    assert card["_board_path"] == eco_db
    assert kdb.archived_scans == 1  # fallback ran only after the live scan missed


def test_find_card_nonexistent_returns_none(tmp_path):
    """A card on no board (live or archived) still returns None."""
    root, eco_db = _archived_fixture(tmp_path)
    kdb = _FindKdb(
        root=root,
        live_tasks={"hscc": [_Task("t_live", "live card")]},
        archived_tasks_by_path={eco_db: [_Task("t_arch", "arch card")]},
    )
    assert kanban.find_card("t_nope", _kdb=kdb) is None
