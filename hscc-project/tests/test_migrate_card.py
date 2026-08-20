"""Tests for `flightdeck migrate-card` (MIG2) — safe card re-homing.

The command reads one source card, builds a ``[migrated]`` copy on the target
project's board, and marks the original as migrated (archive + comment pointer),
never deleting it. Dry-run by default; ``--apply`` performs the migration.
Running/claimed source cards are refused; unknown target projects are refused
with the known ones listed.

No test touches a real board: the source card comes from a stubbed
``kanban.find_card``, board resolution from a stubbed
``kanban.resolve_project_board``, creation through ``kanban.create_task``, and
archive + comment through a fake ``_kdb`` injected on ``args``.
"""

import argparse

import pytest

from flightdeck.commands import legacy
from flightdeck.core import kanban
from flightdeck.core.registry import Project


def _args(**overrides):
    base = {
        "registry": "/tmp/reg.yaml",
        "card_id": "t_old",
        "to": "hscc",
        "apply": False,
        "kdb": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _project(name="hscc", board="hscc", repo="/repo/hscc"):
    return Project(name=name, repo=repo, board=board)


def _card(cid="t_old", title="Old task", body="the body", status="review",
          board="ecofire", assignee="coder", priority=3, _board_path=None):
    return {
        "id": cid,
        "title": title,
        "body": body,
        "status": status,
        "board": board,
        "assignee": assignee,
        "priority": priority,
        "workspace_path": "/repo/ecofire",
        "_board_path": _board_path,
    }


class FakeKdb:
    """A hermes kanban_db stand-in: connect + create_task + add_comment + archive_task.

    Records what would be created/archived/commented and where, so tests can
    assert the migration actually happened on the TARGET board and the ORIGINAL
    was archived (never deleted) with a comment pointer to the new card.
    """

    def __init__(self, new_id="t_new"):
        self.new_id = new_id
        self.created = []      # dicts of create_task kwargs
        self.archived = []     # (board, card_id)
        self.commented = []    # (board, card_id, body)

    class _Conn:
        def __init__(self, board=None):
            self.board = board

        def close(self):
            pass

    def connect(self, board=None):
        return self._Conn(board)

    def create_task(self, conn, **kwargs):
        self.created.append({"board": conn.board, **kwargs})
        return self.new_id

    def add_comment(self, conn, task_id, author, body):
        self.commented.append((conn.board, task_id, body))
        return 1

    def archive_task(self, conn, task_id):
        self.archived.append((conn.board, task_id))
        return True


def _install(monkeypatch, *, card, kdb, projects=None, target_board=None,
             used_fallback=False):
    """Stub the kanban seams so no test touches a real board."""
    if projects is None:
        projects = [_project()]
    monkeypatch.setattr(legacy.kanban, "find_card",
                        lambda cid, _kdb=None: card)
    monkeypatch.setattr(legacy.kanban, "resolve_project_board",
                        lambda proj, _kdb=None: (target_board or proj.board, used_fallback))
    monkeypatch.setattr(legacy.kanban, "create_task",
                        lambda board, title, assignee=None, body=None,
                               workspace_kind=None, workspace_path=None, _kdb=None:
                        kdb.create_task(kdb.connect(board=board), title=title,
                                        body=body, assignee=assignee,
                                        workspace_kind=workspace_kind,
                                        workspace_path=workspace_path))


# --------------------------------------------------------------------------- #
# --apply creates the migrated card on the TARGET board, never the source's.
# --------------------------------------------------------------------------- #

def test_apply_creates_migrated_card_on_target_board(monkeypatch):
    kdb = FakeKdb()
    _install(monkeypatch, card=_card(), kdb=kdb)

    rc = legacy.cmd_migrate_card(_args(apply=True, kdb=kdb), [_project()])

    assert rc == 0
    assert len(kdb.created) == 1
    created = kdb.created[0]
    # Landed on the TARGET project's board, not the source card's board.
    assert created["board"] == "hscc"
    assert created["title"].startswith("[migrated] ")


def test_apply_carries_title_body_prefix_provenance_and_fields(monkeypatch):
    kdb = FakeKdb()
    _install(monkeypatch, card=_card(title="Old task", body="the body",
                                     assignee="coder", priority=3), kdb=kdb)

    rc = legacy.cmd_migrate_card(_args(apply=True, kdb=kdb), [_project()])

    assert rc == 0
    created = kdb.created[0]
    assert created["title"] == "[migrated] Old task"
    assert created["assignee"] == "coder"
    # Provenance line prepended, original body preserved below it.
    assert created["body"].startswith("Migrated from board 'ecofire' card t_old on ")
    assert "the body" in created["body"]
    # Workspace anchored to the TARGET repo as a fresh worktree, not the
    # source board's stale one.
    assert created["workspace_path"] == "/repo/hscc"
    assert created["workspace_kind"] == "worktree"


def test_apply_provenance_line_names_source_board_and_card(monkeypatch):
    kdb = FakeKdb()
    _install(monkeypatch, card=_card(board="legacy/ecofire"), kdb=kdb)

    legacy.cmd_migrate_card(_args(apply=True, kdb=kdb), [_project()])

    first_line = kdb.created[0]["body"].splitlines()[0]
    assert first_line == "Migrated from board 'legacy/ecofire' card t_old on " \
        f"{__import__('datetime').date.today().isoformat()}."


def test_apply_archives_original_with_comment_pointer_never_deletes(monkeypatch):
    kdb = FakeKdb(new_id="t_new99")
    _install(monkeypatch, card=_card(), kdb=kdb)

    rc = legacy.cmd_migrate_card(_args(apply=True, kdb=kdb), [_project()])

    assert rc == 0
    # Original ARCHIVED on its SOURCE board (ecofire), not the target.
    assert kdb.archived == [("ecofire", "t_old")]
    # A comment points from old -> new, so the paper trail survives archiving.
    assert kdb.commented == [("ecofire", "t_old",
                              f"Migrated to card t_new99 on "
                              f"{__import__('datetime').date.today().isoformat()}.")]
    # Never deleted: only archived + commented (no task deletion in the seam).
    assert kdb.created and kdb.archived


# --------------------------------------------------------------------------- #
# Dry-run — prints the plan, touches nothing.
# --------------------------------------------------------------------------- #

def test_dry_run_prints_plan_and_mutates_nothing(monkeypatch, capsys):
    kdb = FakeKdb()
    _install(monkeypatch, card=_card(), kdb=kdb)

    rc = legacy.cmd_migrate_card(_args(apply=False, kdb=kdb), [_project()])

    captured = capsys.readouterr()
    assert rc == 0
    # The plan is shown, including the migrated title and the archive step.
    assert "CREATE" in captured.out
    assert "[migrated] Old task" in captured.out
    assert "ARCHIVE original card t_old" in captured.out
    assert "dry-run" in captured.err
    # Nothing was created or archived.
    assert kdb.created == [] and kdb.archived == [] and kdb.commented == []


# --------------------------------------------------------------------------- #
# Unknown target project — refuse, listing the known ones.
# --------------------------------------------------------------------------- #

def test_unknown_target_project_refused_listing_known(capsys):
    projects = [_project(name="hscc"), _project(name="ecofire", repo="/repo/ecofire")]
    rc = legacy.cmd_migrate_card(_args(apply=True, card_id="t_old", to="nope"), projects)

    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown target project 'nope'" in err
    assert "ecofire" in err and "hscc" in err


def test_unknown_target_no_projects_registered(capsys):
    rc = legacy.cmd_migrate_card(_args(apply=True, to="nope"), [])
    err = capsys.readouterr().err
    assert rc == 2
    assert "(none registered)" in err


# --------------------------------------------------------------------------- #
# Active source card — refused (the safety rule).
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("status", ["running", "claimed"])
def test_active_source_card_is_refused(monkeypatch, capsys, status):
    """run: MIG2 SAFETY — never migrate a card a worker may be mid-way through.

    Reuses reconcile's close-safety active-card guard (kanban.ACTIVE_STATUSES).
    """
    kdb = FakeKdb()
    _install(monkeypatch, card=_card(status=status), kdb=kdb)

    rc = legacy.cmd_migrate_card(_args(apply=True, kdb=kdb), [_project()])

    err = capsys.readouterr().err
    assert rc == 2
    assert "active" in err
    assert "refusing to migrate" in err
    # Nothing was created or archived — the refusal happens before any write.
    assert kdb.created == [] and kdb.archived == [] and kdb.commented == []


# --------------------------------------------------------------------------- #
# Card not found / partial failure
# --------------------------------------------------------------------------- #

def test_command_is_discovered():
    """cli.py auto-discovers the migrate-card subcommand via build_subparser."""
    from flightdeck.cli import build_parser

    args = build_parser().parse_args(["migrate-card", "t_old", "--to", "hscc"])
    assert args.command == "migrate-card"
    assert args.func is not None


def test_kanban_create_task_forwards_workspace_anchors(monkeypatch):
    """The real create_task seam forwards workspace_kind/path to kdb.create_task,
    and omits them when unset (existing callers see no change)."""
    kdb = FakeKdb(new_id="t_new")

    class _RealKdb:
        def connect(self, board=None):
            return FakeKdb._Conn(board)

        def create_task(self, conn, **kwargs):
            kdb.created.append({"board": conn.board, **kwargs})
            return "t_new"

    monkeypatch.setattr(kanban, "_load_kanban_db", lambda: _RealKdb())

    new_id = kanban.create_task("hscc", "t", workspace_kind="worktree",
                                workspace_path="/repo/hscc")
    assert new_id == "t_new"
    assert kdb.created[0]["workspace_kind"] == "worktree"
    assert kdb.created[0]["workspace_path"] == "/repo/hscc"

    # Unset anchors are omitted entirely — existing callers unchanged.
    kdb.created.clear()
    kanban.create_task("hscc", "plain")
    assert "workspace_kind" not in kdb.created[0]
    assert "workspace_path" not in kdb.created[0]


def test_card_not_found_errors_clearly(monkeypatch, capsys):
    """A card that resolves on no board is refused with a clear message."""
    _install(monkeypatch, card=None, kdb=FakeKdb())
    rc = legacy.cmd_migrate_card(_args(apply=True), [_project()])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not found on any board" in err


def test_create_failure_reports_partial_with_new_card(monkeypatch, capsys):
    """If the original cannot be archived, the copy already exists — report it."""
    class _Fail:
        def connect(self, board=None):
            return FakeKdb._Conn(board)

        def add_comment(self, conn, task_id, author, body):
            raise kanban.KanbanError("boom")

        def archive_task(self, conn, task_id):
            return True

    kdb = _Fail()
    _install(monkeypatch, card=_card(), kdb=kdb)
    # create_task reaches the real _install lambda -> FakeKdb.create_task; but
    # here the injected kdb must ALSO serve creation. Redirect create to a fake.
    fake = FakeKdb(new_id="t_new")
    monkeypatch.setattr(legacy.kanban, "create_task",
                        lambda board, title, assignee=None, body=None,
                               workspace_kind=None, workspace_path=None, _kdb=None:
                        fake.create_task(fake.connect(board=board), title=title,
                                         body=body, assignee=assignee,
                                         workspace_kind=workspace_kind,
                                         workspace_path=workspace_path))

    rc = legacy.cmd_migrate_card(_args(apply=True, kdb=kdb), [_project()])

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert rc == 1
    assert "PARTIAL" in combined
    assert "t_new" in combined  # the created card id is still reported


def test_archive_false_reports_partial_not_success(monkeypatch, capsys):
    """If archive_task returns False (card already archived / vanished), the
    migration is PARTIAL — the copy exists but the original is untouched. The
    CLI must NOT print 'archived' + exit 0 (the historical bug)."""
    class _FalseKdb:
        def connect(self, board=None):
            return FakeKdb._Conn(board)

        def add_comment(self, conn, task_id, author, body):
            return 1

        def archive_task(self, conn, task_id):
            return False  # no-match / already archived — the failure being fixed

    kdb = _FalseKdb()
    fake = FakeKdb(new_id="t_new")
    _install(monkeypatch, card=_card(), kdb=kdb)
    # Route creation to a working fake; only ARCHIVE fails here.
    monkeypatch.setattr(legacy.kanban, "create_task",
                        lambda board, title, assignee=None, body=None,
                               workspace_kind=None, workspace_path=None, _kdb=None:
                        fake.create_task(fake.connect(board=board), title=title,
                                         body=body, assignee=assignee,
                                         workspace_kind=workspace_kind,
                                         workspace_path=workspace_path))

    rc = legacy.cmd_migrate_card(_args(apply=True, kdb=kdb), [_project()])

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert rc == 1
    assert "PARTIAL" in combined          # reaches the migration-is-partial path
    assert "t_new" in combined            # the new card id is still reported
    assert "archived with a pointer" not in combined  # NO false success claim


# --------------------------------------------------------------------------- #
# Archived source board — found via find_card's archived fallback, carrying
# _board_path so _archive_source can open the archived board by db_path
# (connect(board=slug) cannot resolve an archived slug).
# --------------------------------------------------------------------------- #


def test_dry_run_plans_archived_board_card(monkeypatch, capsys):
    """A card on an archived board (found via find_card's fallback) dry-runs
    into a plan, naming the archived board in provenance, mutating nothing."""
    kdb = FakeKdb()
    _install(monkeypatch, card=_card(
        board="ecofire-1786704010",
        _board_path="/x/boards/_archived/ecofire-1786704010/kanban.db",
    ), kdb=kdb)

    rc = legacy.cmd_migrate_card(_args(apply=False, kdb=kdb), [_project()])

    captured = capsys.readouterr()
    assert rc == 0
    assert "CREATE" in captured.out
    # Provenance names the ARCHIVED board's slug, not a bogus resolved one.
    assert "Migrated from board 'ecofire-1786704010' card t_old" in captured.out
    assert "dry-run" in captured.err
    # Nothing was created or archived.
    assert kdb.created == [] and kdb.archived == [] and kdb.commented == []


def test_archive_source_connects_via_db_path_for_archived_card():
    """_archive_source opens an archived source board through _board_path,
    because connect(board=slug) cannot resolve an archived slug."""
    recorded = {}

    class _ArchKdb:
        def connect(self, db_path=None, *, board=None):
            assert db_path is not None, "archived source must connect by db_path"
            recorded["db_path"] = db_path
            return FakeKdb._Conn(board)

        def add_comment(self, conn, task_id, author, body):
            recorded["commented"] = (conn.board, task_id, body)
            return 1

        def archive_task(self, conn, task_id):
            recorded["archived"] = (conn.board, task_id)
            return True

    card = _card(board="ecofire-1786704010",
                 _board_path="/x/boards/_archived/ecofire-1786704010/kanban.db")
    legacy._archive_source(card, "t_new", _kdb=_ArchKdb())

    # Connected by the archived board's full db path, not the unresolved slug.
    assert str(recorded["db_path"]) == \
        "/x/boards/_archived/ecofire-1786704010/kanban.db"
    assert recorded["archived"] == (None, "t_old")
    assert recorded["commented"][1] == "t_old"
