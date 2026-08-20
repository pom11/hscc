"""Tests for flightdeck.commands.message + the core send/read/create helpers.

Every test drives the command against STUBS: an injectable telegram client
(``(tool, args) -> str``) and a stub Hermes kanban library (patching
``kanban._load_kanban_db``). No test touches real Telegram, git, the live
cluster, or the real kanban DB. Registry files are written to a pytest
tmp_path, never ~/.flightdeck.
"""

from types import SimpleNamespace

import pytest

from flightdeck.commands import message as msg_cmd
from flightdeck.core import kanban, registry, telegram
from flightdeck.core.telegram import Message, Topic, TopicLockedError
from conftest import TEST_GROUP_ID

# The group the resolver injects for every test (see conftest). Needs to
# match `TEST_GROUP_ID` so assertions on the resolved `group` arg hold.
GROUP = TEST_GROUP_ID


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #

class FakeTG:
    """Stands in for the MCP daemon client: callable (tool, args) -> str.

    - ``telegram_topic_status`` answers from ``topics`` (id -> name).
    - ``telegram_send`` records the send, mirrors the daemon's reply string.
    - ``telegram_read`` answers from ``feed`` (a list of ``[ts] sender: text``
      lines) — the topic's message history.
    - ``locked=True`` makes every call raise the single-writer SQLite error.
    """

    def __init__(self, topics=None, feed=None, locked=False):
        self.topics = dict(topics or {})
        self.feed = list(feed or [])
        self.locked = locked
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if self.locked:
            raise ConnectionError("sqlite3.OperationalError: database is locked")
        if tool_name == "telegram_topic_status":
            return "\n".join(
                f"topic_id={tid} title={name}" for tid, name in sorted(self.topics.items())
            )
        if tool_name == "telegram_send":
            return f"Sent to {GROUP} topic {arguments['topic_id']}."
        if tool_name == "telegram_read":
            return "\n".join(self.feed[-arguments["limit"]:])
        raise AssertionError(f"FakeTG does not know tool {tool_name!r}")


class FakeKB:
    """Stubs hermes_cli.kanban_db for create_task: connect + create_task."""

    def __init__(self, fail=False, new_id="card-1", current="default"):
        self.fail = fail
        self.new_id = new_id
        self.current = current
        self.created: list[dict] = []

    def connect(self, board=None):
        return SimpleNamespace(board=board, close=lambda: None)

    def get_current_board(self):
        return self.current

    def create_task(self, conn, **kwargs):
        self.created.append({"conn_board": conn.board, **kwargs})
        if self.fail:
            raise kanban.KanbanError("could not read board (stub)")
        return self.new_id


def _stub_kb(kb, monkeypatch):
    monkeypatch.setattr(kanban, "_load_kanban_db", lambda: kb, raising=False)


def _ns(**kw):
    """Build an argparse.Namespace with sensible defaults for a message cmd."""
    import argparse

    defaults = dict(
        client=None, registry=None, json=False,
        project="", message="", task="", assignee=None, n=10, to=None,
        message_cmd=None, apply=False, body_file=None, cwd=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _write_registry(tmp_path, rows):
    import yaml

    p = tmp_path / "registry.yaml"
    p.write_text(yaml.safe_dump({"projects": rows}, sort_keys=False), encoding="utf-8")
    return str(p)


def _row(name="hscc", topic=140, board="hscc"):
    row = {"name": name, "repo": f"~/dev/{name}", "topic": topic, "board": board}
    return row


# --------------------------------------------------------------------------- #
# core.telegram — send_message / read_messages
# --------------------------------------------------------------------------- #

def test_send_message_posts_to_topic_and_returns_raw():
    fake = FakeTG(topics={140: "hscc"})
    raw = telegram.send_message(140, "hello", _client=fake)
    assert ("telegram_send", {"group": GROUP, "message": "hello", "topic_id": 140}) in fake.calls
    assert "topic 140" in raw


def test_read_messages_parses_newest_last():
    fake = FakeTG(feed=["[08:00] Alice(@a): first", "[08:05] Bob(@b): second"])
    msgs = telegram.read_messages(140, n=10, _client=fake)
    assert msgs == [
        Message(timestamp="08:00", sender="Alice(@a)", text="first"),
        Message(timestamp="08:05", sender="Bob(@b)", text="second"),
    ]
    assert ("telegram_read", {"group": GROUP, "limit": 10, "topic_id": 140}) in fake.calls


def test_read_messages_drops_blank_lines_keeps_unparsed():
    fake = FakeTG(feed=["[08:00] Alice(@a): first", "", "  ", "some odd line"])
    msgs = telegram.read_messages(140, n=10, _client=fake)
    assert len(msgs) == 2
    assert msgs[0].text == "first"
    assert msgs[1].sender == "(unparsed)"  # never silently dropped


def test_send_locked_surfaces_topiclocked():
    fake = FakeTG(topics={140: "hscc"}, locked=True)
    with pytest.raises(TopicLockedError):
        telegram.send_message(140, "hello", _client=fake)


# --------------------------------------------------------------------------- #
# core.kanban — create_task
# --------------------------------------------------------------------------- #

def test_kanban_create_task_calls_board_and_returns_id(monkeypatch):
    kb = FakeKB(new_id="card-9")
    _stub_kb(kb, monkeypatch)
    card_id = kanban.create_task("hscc", "fix things", assignee="coder", body="do it")
    assert card_id == "card-9"
    assert kb.created == [
        {
            "conn_board": "hscc", "title": "fix things", "body": "do it",
            "assignee": "coder", "board": "hscc",
        }
    ]


# --------------------------------------------------------------------------- #
# send — resolves project -> topic
# --------------------------------------------------------------------------- #

def test_send_resolves_project_to_topic(capsys):
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140)]
    fake = FakeTG(topics={140: "hscc"})
    rc = msg_cmd.cmd_send(_ns(client=fake, project="hscc", message="hello"), projects)
    out = capsys.readouterr().out
    assert rc == 0
    assert "sent to hscc" in out
    # The operator gave a PROJECT; the topic id 140 was resolved, never typed.
    assert ("telegram_send", {"group": GROUP, "message": "hello", "topic_id": 140}) in fake.calls


def test_send_unknown_project_errors_clearly(tmp_path, capsys):
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140)]
    rc = msg_cmd.cmd_send(_ns(client=FakeTG(), project="nope", message="x"), projects)
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown project" in err
    assert "nope" in err


def test_send_project_without_topic_gives_actionable_error(tmp_path, capsys):
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=None)]
    rc = msg_cmd.cmd_send(_ns(client=FakeTG(), project="hscc", message="x"), projects)
    err = capsys.readouterr().err
    assert rc == 2
    assert "has no topic" in err
    assert "flightdeck project repair hscc" in err


def test_send_locked_surfaces_clear_message(tmp_path, capsys):
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140)]
    fake = FakeTG(topics={140: "hscc"}, locked=True)
    rc = msg_cmd.cmd_send(_ns(client=fake, project="hscc", message="x"), projects)
    err = capsys.readouterr().err
    assert rc == 3
    assert "locked" in err
    assert "retry" in err
    assert "Traceback" not in err


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #

def test_read_shows_messages_newest_last(tmp_path, capsys):
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140)]
    fake = FakeTG(feed=["[08:00] Alice(@a): first", "[08:05] Bob(@b): second"])
    rc = msg_cmd.cmd_read(_ns(client=fake, project="hscc", n=10), projects)
    out = capsys.readouterr().out
    assert rc == 0
    # Ordered newest last (the feed is already oldest->newest; core keeps order).
    assert out.index("Alice") < out.index("Bob")


def test_read_json_shape(tmp_path, capsys):
    import json

    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140)]
    fake = FakeTG(feed=["[08:00] Alice(@a): first"])
    rc = msg_cmd.cmd_read(_ns(client=fake, project="hscc", n=10, json=True), projects)
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data == [{"timestamp": "08:00", "sender": "Alice(@a)", "text": "first"}]


def test_read_unknown_project_errors(tmp_path, capsys):
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140)]
    rc = msg_cmd.cmd_read(_ns(client=FakeTG(), project="nope", n=10), projects)
    assert rc == 2
    assert "unknown project" in capsys.readouterr().err


def test_read_project_without_topic_actionable(tmp_path, capsys):
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=None)]
    rc = msg_cmd.cmd_read(_ns(client=FakeTG(), project="hscc", n=10), projects)
    err = capsys.readouterr().err
    assert rc == 2
    assert "flightdeck project repair hscc" in err


def test_read_locked_surfaces_clear_message(tmp_path, capsys):
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140)]
    fake = FakeTG(feed=[], locked=True)
    rc = msg_cmd.cmd_read(_ns(client=fake, project="hscc", n=10), projects)
    err = capsys.readouterr().err
    assert rc == 3
    assert "locked" in err
    assert "retry" in err


# --------------------------------------------------------------------------- #
# dispatch — creates the card AND announces
# --------------------------------------------------------------------------- #

def test_dispatch_creates_card_and_announces(monkeypatch, tmp_path, capsys):
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140, board="hscc")]
    kb = FakeKB(new_id="card-7")
    _stub_kb(kb, monkeypatch)
    fake = FakeTG(topics={140: "hscc"})

    rc = msg_cmd.cmd_dispatch(
        _ns(client=fake, project="hscc", task="build the widget", assignee="coder", message="do the widget", apply=True),
        projects,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "card-7" in out
    # Card created on the project's board.
    assert kb.created[0]["conn_board"] == "hscc"
    assert kb.created[0]["title"] == "build the widget"
    assert kb.created[0]["assignee"] == "coder"
    # No --body-file given, so the card body defaults to the announcement.
    assert kb.created[0]["body"] == "do the widget"
    # Announcement posted to the project's topic.
    assert ("telegram_send", {"group": GROUP, "message": "do the widget", "topic_id": 140}) in fake.calls


def test_dispatch_anchors_card_in_projects_repo_worktree(monkeypatch, tmp_path, capsys):
    """A dispatched card must be anchored in the project's OWN repo as a
    worktree — never silently degraded to an unanchored scratch dir. This is
    the regression for the reported bug where ``message dispatch`` created
    every card with workspace_kind='scratch' and workspace_path=NULL."""
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140, board="hscc")]
    kb = FakeKB(new_id="card-7")
    _stub_kb(kb, monkeypatch)
    fake = FakeTG(topics={140: "hscc"})

    rc = msg_cmd.cmd_dispatch(
        _ns(client=fake, project="hscc", task="build the widget", assignee="coder", message="do the widget", apply=True),
        projects,
    )
    assert rc == 0
    # The card is anchored in the project's repo as a worktree.
    assert kb.created[0]["workspace_kind"] == "worktree"
    assert kb.created[0]["workspace_path"] == "~/dev/hscc"
    # The original scratch-degradation shape is gone.
    assert kb.created[0]["workspace_kind"] != "scratch"
    assert kb.created[0]["workspace_path"] is not None


def test_dispatch_project_with_board_lands_on_its_own_board_regression(monkeypatch, tmp_path, capsys):
    """R1-R7 wrong-board incident: a project WITH a board must land on THAT
    board, never on Hermes' global current board. Here the global current board
    is 'default' while the project's board is 'hscc' — the exact shape of the
    2026-08-11 incident where seven cards landed on 'default' instead of the
    project board."""

    # The fake's GLOBAL current board points elsewhere ('default'), but the
    # project's OWN board is 'hscc'. dispatch must use 'hscc' and never consult
    # the current board for a project that has one.
    kb = FakeKB(new_id="card-7", current="default")
    _stub_kb(kb, monkeypatch)
    fake = FakeTG(topics={140: "hscc"})
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140, board="hscc")]

    rc = msg_cmd.cmd_dispatch(
        _ns(client=fake, project="hscc", task="build the widget", assignee=None, message="do the widget", apply=True),
        projects,
    )
    out = capsys.readouterr().out
    assert rc == 0
    # Landed on the PROJECT'S board, not the global current board.
    assert kb.created[0]["conn_board"] == "hscc"
    assert "created on board 'hscc'" in out
    # No fallback say-so: the project HAS a board, so nothing to report.
    assert "no board for hscc" not in capsys.readouterr().err


def test_dispatch_project_without_board_falls_back_to_current_and_says_so(monkeypatch, tmp_path, capsys):
    """A project with NO board still dispatches: the card lands on Hermes'
    CURRENT board and the command SAYS SO (\"no board for <project>; created on
    '<current>'\") — a silent fallback is how cards end up on the wrong board."""
    kb = FakeKB(new_id="card-9", current="flightdeck")
    _stub_kb(kb, monkeypatch)
    fake = FakeTG(topics={140: "hscc"})
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140, board=None)]

    rc = msg_cmd.cmd_dispatch(
        _ns(client=fake, project="hscc", task="build the widget", assignee=None, message="do the widget", apply=True),
        projects,
    )
    captured = capsys.readouterr()
    assert rc == 0
    # Landed on the CURRENT board, not hardcoded 'default' and not an error.
    assert kb.created[0]["conn_board"] == "flightdeck"
    assert "no board for hscc; created on 'flightdeck'" in captured.err
    assert "created on board 'flightdeck'" in captured.out


def test_dispatch_does_not_mutate_global_current_board_even_on_failure(monkeypatch, tmp_path, capsys):
    """The GLOBAL current board is never changed by dispatch — not even when
    the create raises. A card-creating command must read the current board as a
    fallback, never write it; the Hermes dispatcher relies on it staying put for
    every other topic, so a leftover mutation would redirect unrelated work."""
    kb = FakeKB(fail=True, current="flightdeck")  # create_task raises
    _stub_kb(kb, monkeypatch)
    fake = FakeTG(topics={140: "hscc"})
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140, board=None)]

    rc = msg_cmd.cmd_dispatch(
        _ns(client=fake, project="hscc", task="t", assignee=None, message="m", apply=True),
        projects,
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "could not create card" in err
    # The current board was read for the fallback AND left exactly as it was.
    assert kb.current == "flightdeck"


def test_dispatch_defaults_announcement_to_task_title(monkeypatch, capsys):
    """A bare dispatch <project> \"task\" (no --message) announces the task title."""
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140, board="hscc")]
    kb = FakeKB(new_id="card-8")
    _stub_kb(kb, monkeypatch)
    fake = FakeTG(topics={140: "hscc"})

    rc = msg_cmd.cmd_dispatch(
        _ns(client=fake, project="hscc", task="do the thing", assignee=None, message=None, apply=True),
        projects,
    )
    out = capsys.readouterr().out
    assert rc == 0
    # The announcement sent to the topic defaults to the task title.
    assert ("telegram_send", {"group": GROUP, "message": "do the thing", "topic_id": 140}) in fake.calls


def test_dispatch_no_topic_actionable_without_mutation(monkeypatch, capsys):
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=None, board="hscc")]
    kb = FakeKB()
    _stub_kb(kb, monkeypatch)
    fake = FakeTG(topics={})

    rc = msg_cmd.cmd_dispatch(
        _ns(client=fake, project="hscc", task="t", assignee=None, message="m"),
        projects,
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "has no topic" in err
    assert "flightdeck project repair hscc" in err
    # Nothing was created or posted — no half-done dispatch.
    assert kb.created == []
    assert fake.calls == []


def test_dispatch_no_repo_refuses_without_mutation(monkeypatch, capsys):
    """A project with NO repo cannot anchor a worktree — refuse with a clear,
    actionable error and create/post nothing. A card with no repo anchor is
    exactly the scratch-degradation bug this task fixes, so we never silently
    create an unanchored card (matching the no-topic guard just above and
    cmd_migrate_card's no-repo guard)."""
    projects = [registry.Project(name="hscc", repo=None, topic=140, board="hscc")]
    kb = FakeKB(new_id="card-70")
    _stub_kb(kb, monkeypatch)
    fake = FakeTG(topics={140: "hscc"})

    rc = msg_cmd.cmd_dispatch(
        _ns(client=fake, project="hscc", task="t", assignee=None, message="m", apply=True),
        projects,
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "has no repo" in err
    assert "cannot anchor a worktree" in err
    # Nothing was created or posted — no half-done dispatch, no unanchored card.
    assert kb.created == []
    assert fake.calls == []


def test_dispatch_unknown_project_errors(tmp_path, capsys):
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140, board="hscc")]
    rc = msg_cmd.cmd_dispatch(
        _ns(client=FakeTG(), project="nope", task="t", assignee=None, message="m"),
        projects,
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown project" in err
def test_dispatch_failing_announcement_reports_card_id_and_partial(monkeypatch, tmp_path, capsys):
    """Card created but announcement fails: say so, print the card id, never a
    plain success."""
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140, board="hscc")]
    kb = FakeKB(new_id="card-42")
    _stub_kb(kb, monkeypatch)
    fake = FakeTG(topics={140: "hscc"}, locked=True)  # announcement will fail

    rc = msg_cmd.cmd_dispatch(
        _ns(client=fake, project="hscc", task="t", assignee=None, message="m", apply=True),
        projects,
    )
    out_err = capsys.readouterr()
    combined = out_err.out + out_err.err
    assert rc == 1                       # partial failure, not success
    assert "card-42" in combined         # created card id still reported
    assert "PARTIAL" in combined
    assert "FAILED" in combined
    assert "announced to topic" not in out_err.out  # NOT a success line


def test_dispatch_dry_run_by_default_mutates_nothing(monkeypatch, capsys):
    """dispatch now defaults to a dry-run plan (matching every other mutating
    command): no card is created and nothing is announced without --apply."""
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140, board="hscc")]
    kb = FakeKB(new_id="card-99")
    _stub_kb(kb, monkeypatch)
    fake = FakeTG(topics={140: "hscc"})

    rc = msg_cmd.cmd_dispatch(
        _ns(client=fake, project="hscc", task="build the widget", assignee="coder", message="ping", apply=False),
        projects,
    )
    out_err = capsys.readouterr()
    assert rc == 0
    # The plan previews the resolved board and the card title.
    assert "dry-run" in out_err.out
    assert "board='hscc'" in out_err.out
    assert "card title: build the widget" in out_err.out
    assert "announce  : ping" in out_err.out
    assert "pass --apply" in out_err.err
    # Nothing was created or posted.
    assert kb.created == []
    assert fake.calls == []


def test_dispatch_body_file_sets_card_body(monkeypatch, tmp_path, capsys):
    """--body-file PATH makes the CARD BODY come from the file, while --message
    stays the (short) announcement text — they may differ on purpose."""
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140, board="hscc")]
    kb = FakeKB(new_id="card-11")
    _stub_kb(kb, monkeypatch)
    fake = FakeTG(topics={140: "hscc"})

    body_file = tmp_path / "spec.md"
    body_file.write_text("VERIFY: pytest\nACCEPT: it passes\nfix the login bug at auth.py:10", encoding="utf-8")

    rc = msg_cmd.cmd_dispatch(
        _ns(client=fake, project="hscc", task="fix login", assignee="coder",
            message="fix the login bug", body_file=str(body_file), apply=True),
        projects,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "card-11" in out
    # The card body comes verbatim from the file, multi-line.
    assert kb.created[0]["body"] == "VERIFY: pytest\nACCEPT: it passes\nfix the login bug at auth.py:10"
    # The announcement is the short --message text, not the full spec.
    assert ("telegram_send", {"group": GROUP, "message": "fix the login bug", "topic_id": 140}) in fake.calls


def test_dispatch_body_file_dash_reads_stdin(monkeypatch, capsys, tmp_path):
    """--body-file - reads the full body text from stdin, so a caller can pipe a
    multi-line spec without a temp file."""
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140, board="hscc")]
    kb = FakeKB(new_id="card-12")
    _stub_kb(kb, monkeypatch)
    fake = FakeTG(topics={140: "hscc"})

    spec = "VERIFY: pytest\nACCEPT: green\nline three"
    import sys

    monkeypatch.setattr(sys, "stdin", type("S", (), {"read": lambda self: spec})())

    rc = msg_cmd.cmd_dispatch(
        _ns(client=fake, project="hscc", task="pipe it", assignee=None,
            message=None, body_file="-", apply=True),
        projects,
    )
    assert rc == 0
    assert kb.created[0]["body"] == spec


def test_dispatch_body_file_unreadable_errors_before_mutation(monkeypatch, tmp_path, capsys):
    """An unreadable --body-file fails cleanly (rc=2) and creates/posts nothing."""
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140, board="hscc")]
    kb = FakeKB(new_id="card-13")
    _stub_kb(kb, monkeypatch)
    fake = FakeTG(topics={140: "hscc"})

    rc = msg_cmd.cmd_dispatch(
        _ns(client=fake, project="hscc", task="t", assignee=None,
            message="m", body_file=str(tmp_path / "missing.md"), apply=True),
        projects,
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "could not read body file" in err
    assert kb.created == []
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# dispatch — cross-project dependents notice (registry.dependent_notice)
# --------------------------------------------------------------------------- #


def test_dispatch_appends_dependents_notice_to_card_body(monkeypatch, tmp_path, capsys):
    """A dispatch to a project WITH dependents appends the advisory note."""
    projects = [
        registry.Project(name="bc", repo="~/dev/bc", topic=140, board="bc"),
        registry.Project(name="app", repo="~/dev/app", topic=141, board="app", depends_on=["bc"]),
    ]
    kb = FakeKB(new_id="card-9")
    _stub_kb(kb, monkeypatch)
    fake = FakeTG(topics={140: "bc"})

    rc = msg_cmd.cmd_dispatch(
        _ns(client=fake, project="bc", task="edit the API", assignee=None,
            message="do the edit", apply=True),
        projects,
    )
    assert rc == 0
    assert kb.created[0]["body"] == (
        "do the edit\n\n[flightdeck] 1 dependent project(s): app — "
        "consider verifying they still work"
    )


def test_dispatch_no_dependents_notice_when_none(monkeypatch, tmp_path, capsys):
    """A dispatch to a project with NO dependents leaves the body unchanged —
    fully backward compatible with every dispatch test above this one."""
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140, board="hscc")]
    kb = FakeKB(new_id="card-9")
    _stub_kb(kb, monkeypatch)
    fake = FakeTG(topics={140: "hscc"})

    rc = msg_cmd.cmd_dispatch(
        _ns(client=fake, project="hscc", task="t", assignee=None,
            message="do the thing", apply=True),
        projects,
    )
    assert rc == 0
    assert kb.created[0]["body"] == "do the thing"


def test_dispatch_dry_run_shows_dependents_notice(monkeypatch, capsys):
    """The dry-run preview shows the same dependents note before --apply."""
    projects = [
        registry.Project(name="bc", repo="~/dev/bc", topic=140, board="bc"),
        registry.Project(name="app", repo="~/dev/app", topic=141, board="app", depends_on=["bc"]),
    ]
    kb = FakeKB(new_id="card-9")
    _stub_kb(kb, monkeypatch)
    fake = FakeTG(topics={140: "bc"})

    rc = msg_cmd.cmd_dispatch(
        _ns(client=fake, project="bc", task="edit the API", assignee=None,
            message="do the edit", apply=False),
        projects,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "dependents: 1 dependent project(s): app — consider verifying they still work" in out
    # Dry-run: nothing created or sent.
    assert kb.created == []
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# broadcast — per-target results, never a single aggregate
# --------------------------------------------------------------------------- #

def test_broadcast_reports_each_target_individually(capsys):
    projects = [
        registry.Project(name="hscc", repo="~/dev/hscc", topic=140, board="hscc"),
        registry.Project(name="ecofire", repo="~/dev/ecofire", topic=2257, board="ecofire"),
    ]
    fake = FakeTG(topics={140: "hscc", 2257: "ecofire"})
    rc = msg_cmd.cmd_broadcast(_ns(client=fake, message="hi", to=None), projects)
    out = capsys.readouterr().out
    assert rc == 0
    # Per-target lines for BOTH projects.
    assert "OK   hscc" in out
    assert "OK   ecofire" in out
    assert ("telegram_send", {"group": GROUP, "message": "hi", "topic_id": 140}) in fake.calls
    assert ("telegram_send", {"group": GROUP, "message": "hi", "topic_id": 2257}) in fake.calls


def test_broadcast_to_limited_subset(capsys):
    projects = [
        registry.Project(name="hscc", repo="~/dev/hscc", topic=140, board="hscc"),
        registry.Project(name="ecofire", repo="~/dev/ecofire", topic=2257, board="ecofire"),
    ]
    fake = FakeTG(topics={140: "hscc", 2257: "ecofire"})
    rc = msg_cmd.cmd_broadcast(_ns(client=fake, message="hi", to="hscc"), projects)
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK   hscc" in out
    assert "ecofire" not in out  # only the --to target was hit
    assert all(a["topic_id"] == 140 for (t, a) in fake.calls if t == "telegram_send")


def test_broadcast_mixed_success_failure_reports_each(capsys):
    projects = [
        registry.Project(name="hscc", repo="~/dev/hscc", topic=140, board="hscc"),
        registry.Project(name="nos", repo="~/dev/nos", topic=None, board="nos"),  # no topic
        registry.Project(name="ecofire", repo="~/dev/ecofire", topic=2257, board="ecofire"),
    ]
    fake = FakeTG(topics={140: "hscc", 2257: "ecofire"})
    rc = msg_cmd.cmd_broadcast(_ns(client=fake, message="hi", to=None), projects)
    out = capsys.readouterr().out
    assert rc == 1                       # not "all done" — a target failed
    assert "OK   hscc" in out            # per-target success
    assert "OK   ecofire" in out
    assert "FAIL nos" in out             # per-target failure with the actionable error
    assert "has no topic" in out
    assert "flightdeck project repair nos" in out


def test_broadcast_unknown_target_fails_individually(capsys):
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140, board="hscc")]
    fake = FakeTG(topics={140: "hscc"})
    rc = msg_cmd.cmd_broadcast(_ns(client=fake, message="hi", to="hscc,nope"), projects)
    out = capsys.readouterr().out
    assert rc == 1
    assert "OK   hscc" in out
    assert "FAIL nope" in out
    assert "unknown project" in out


def test_broadcast_locked_surfaces_clear_message(capsys):
    projects = [registry.Project(name="hscc", repo="~/dev/hscc", topic=140, board="hscc")]
    fake = FakeTG(topics={140: "hscc"}, locked=True)
    rc = msg_cmd.cmd_broadcast(_ns(client=fake, message="hi", to="hscc"), projects)
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL hscc" in out
    assert "locked" in out
    assert "retry" in out        # the single-writer retry hint surfaces per-target
    assert "Traceback" not in out


# --------------------------------------------------------------------------- #
# AUTO-DETECT project from cwd (through the run() entry)
# --------------------------------------------------------------------------- #


def _run_args(tmp_path, name="hscc", **overrides):
    """A namespace primed for msg_cmd.run() with a project named ``name``."""
    reg = _write_registry(tmp_path, [_row(name)])
    base = _ns(client=FakeTG(), registry=reg, project="", cwd=None)
    base.cwd = registry._expand(f"~/dev/{name}") + "/sub"  # cwd inside the repo
    if overrides:
        for k, v in overrides.items():
            setattr(base, k, v)
    return base, reg


def test_run_send_detects_project_from_cwd(tmp_path, capsys, monkeypatch):
    """`message send` with no project + cwd inside a repo -> detected project.

    Here the namespace carries the MESSAGE text (what a real single-token
    parse would bind to the message slot), no project, and a cwd under the hscc
    repo; run() detects hscc and posts there.
    """
    args, reg = _run_args(tmp_path, func=msg_cmd.cmd_send, message="hello")
    fake = args.client
    rc = msg_cmd.run(args, reg)
    captured = capsys.readouterr()
    assert rc == 0
    assert "used project 'hscc' (detected from cwd)" in captured.err or \
        "detected from cwd" in captured.err
    assert ("telegram_send", {"group": GROUP, "message": "hello", "topic_id": 140}) in fake.calls


def test_run_read_detects_project_from_cwd(tmp_path, capsys):
    args, reg = _run_args(tmp_path, func=msg_cmd.cmd_read, n=10)
    fake = args.client
    rc = msg_cmd.run(args, reg)
    captured = capsys.readouterr()
    assert rc == 0
    assert "detected from cwd" in captured.err
    assert ("telegram_read", {"group": GROUP, "limit": 10, "topic_id": 140}) in fake.calls


def test_run_dispatch_detects_project_from_cwd(tmp_path, capsys, monkeypatch):
    """`message dispatch` with no project + cwd inside a repo -> detected."""
    kb = FakeKB(new_id="card-77")
    _stub_kb(kb, monkeypatch)
    args, reg = _run_args(tmp_path, func=msg_cmd.cmd_dispatch, task="build the widget",
                          message=None, apply=True)
    fake = args.client
    rc = msg_cmd.run(args, reg)
    captured = capsys.readouterr()
    assert rc == 0
    assert "detected from cwd" in captured.err
    # Created on the detected hscc project's board, announced to hscc's topic.
    assert kb.created[0]["conn_board"] == "hscc"
    assert ("telegram_send", {"group": GROUP, "message": "build the widget", "topic_id": 140}) in fake.calls


def test_run_detection_note_uses_detected_project_name(tmp_path, capsys):
    """The note names the DETECTED project, and appears on stderr once."""
    args, reg = _run_args(tmp_path, func=msg_cmd.cmd_read, n=5)
    msg_cmd.run(args, reg)
    err = capsys.readouterr().err
    assert "using project 'hscc' (detected from cwd)" in err


def test_run_explicit_project_wins_over_cwd(tmp_path, capsys):
    """An explicit project positional beats cwd detection (no note)."""
    # Registry has hscc AND other; cwd is inside hscc but user typed other.
    reg = _write_registry(
        tmp_path,
        [_row("hscc", topic=140), _row("other", topic=150, board="other")],
    )
    fake = FakeTG(topics={140: "hscc", 150: "other"})
    args = _ns(client=fake, registry=reg, project="other", message="hi",
               cwd=registry._expand("~/dev/hscc") + "/sub", func=msg_cmd.cmd_send)
    rc = msg_cmd.run(args, reg)
    captured = capsys.readouterr()
    assert rc == 0
    assert "detected from cwd" not in captured.err
    # Posted to OTHER's topic (150), not the hscc one the cwd matched.
    assert ("telegram_send", {"group": GROUP, "message": "hi", "topic_id": 150}) in fake.calls


def test_single_token_disambiguation(tmp_path, capsys, monkeypatch):
    """A single positional on send is message-if-not-a-project, else project.

    ``message send \"hello\"`` (one token that is not a project name) binds\
    'hello' to the project slot; with a cwd inside the hscc repo it is treated
    as the MESSAGE and the project is detected. ``message send hscc`` (one
    token that IS a project name) is treated as the project with a missing
    message -> error.
    """
    reg = _write_registry(tmp_path, [_row("hscc", topic=140)])
    cwd = registry._expand("~/dev/hscc") + "/sub"

    # Case A: single token, not a project name -> it is the message; project
    # detected from cwd. Simulate the argparse bind by putting it in project.
    fake = FakeTG(topics={140: "hscc"})
    args = _ns(client=fake, registry=reg, project="hello", message="",
               cwd=cwd, func=msg_cmd.cmd_send)
    rc = msg_cmd.run(args, reg)
    captured = capsys.readouterr()
    assert rc == 0
    assert "detected from cwd" in captured.err
    assert ("telegram_send", {"group": GROUP, "message": "hello", "topic_id": 140}) in fake.calls

    # Case B: single token that IS a project name -> it is the project; the
    # message is missing, so cmd_send reports it (no detection, no send).
    fake2 = FakeTG(topics={140: "hscc"})
    args2 = _ns(client=fake2, registry=reg, project="hscc", message="",
                cwd=cwd, func=msg_cmd.cmd_send)
    rc2 = msg_cmd.run(args2, reg)
    captured2 = capsys.readouterr()
    assert rc2 == 2
    assert "detected from cwd" not in captured2.err
    assert "message text is required" in captured2.err
    assert fake2.calls == []  # nothing was sent


