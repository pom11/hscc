"""Tests for flightdeck.commands.ask + flightdeck.core.templates.

The ``ask`` command renders a stored prompt template, auto-fills it with context
flightdeck already knows about the project, applies ``--set`` overrides, and
sends it to that project's topic. These tests drive it against STUBS: an
injectable telegram client, an injectable git runner (``_run``), and an
injectable board reader (``_list_cards``). No test touches real Telegram, git,
the live kanban board, or the cluster. Template stores live in a pytest
tmp_path, never ~/.flightdeck.

The hard contract under test: an unfilled slot is an ERROR that lists what the
template expects and sends NOTHING — a literal ``{{slot}}`` is never posted;
``--dry-run`` prints and sends nothing; unknown templates list the available
ones; a project with no topic gives the actionable error.
"""

import argparse

import pytest

from flightdeck.commands import ask as ask_cmd
from flightdeck.core import registry, templates
from flightdeck.core.templates import UnfilledSlotError
from flightdeck.cli import build_parser
from conftest import TEST_GROUP_ID

# The Telegram group the resolver injects for every test (see conftest). Used
# only in the stubbed daemon reply; kept in sync with the injected value.
GROUP = TEST_GROUP_ID


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #

class FakeTG:
    """Stands in for the MCP daemon client: callable (tool, args) -> str.

    ``telegram_send`` records the call and mirrors the daemon's reply string.
    ``locked=True`` makes every call raise the single-writer SQLite error, as
    the real transport normalises to :class:`TopicLockedError`.
    """

    def __init__(self, locked=False):
        self.locked = locked
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if self.locked:
            raise ConnectionError("sqlite3.OperationalError: database is locked")
        if tool_name == "telegram_send":
            return f"Sent to {GROUP} topic {arguments['topic_id']}."
        raise AssertionError(f"FakeTG does not know tool {tool_name!r}")


class FakeGit:
    """Stands in for the subprocess runner: callable (cmd, repo) -> CompletedProcess.

    Maps git commands to canned results so ``git_state`` derives stable branch /
    HEAD facts without touching a real repository. Any command not recognised
    fails (returncode 128), matching git_state's graceful degradation.
    """

    def __init__(self, branch="main", head="a" * 40):
        self.branch = branch
        self.head = head
        self.calls: list[list] = []

    def __call__(self, cmd, repo):
        self.calls.append(list(cmd))
        # Match the more specific --abbrev-ref BEFORE the bare rev-parse HEAD,
        # otherwise both git_state calls resolve to the HEAD sha.
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return _proc(cmd, self.branch)
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1:] == ["HEAD"]:
            return _proc(cmd, self.head)
        return _proc(cmd, "", code=128)


def _proc(cmd, stdout, code=0):
    import subprocess

    return subprocess.CompletedProcess(cmd, code, stdout, "")


def _ns(**kw):
    """Build an argparse.Namespace with the defaults an ask cmd needs."""
    defaults = dict(
        client=None,
        registry=None,
        json=False,
        run=None,
        list_cards=None,
        templates_home=None,
        project="",
        template="",
        set=None,
        dry_run=False,
        name="",
        editor=None,
        ask_cmd=None,
        template_cmd=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _project(name="hscc", topic=140, board="hscc", repo="~/dev/hscc", verify=None):
    if topic is not None:
        topic = int(topic)
    return registry.Project(
        name=name, repo=repo, topic=topic, board=board, verify=verify
    )


def _list_cards_factory(cards):
    """Return a ``_list_cards`` stub returning ``cards``.

    The default board reader is ``kanban.list_cards``; this stub stands in for
    it so the test never touches the live board. Signature matches
    ``kanban.list_cards(board=None, ...)``.
    """

    def _list_cards(board=None, include_archived=False):
        return cards

    return _list_cards


def _seed(home):
    """Copy the shipped template set into a tmp templates home."""
    templates.ensure_seeded(home)
    return home


# --------------------------------------------------------------------------- #
# core.templates — rendering + auto-fill context
# --------------------------------------------------------------------------- #

def test_render_fills_slots_from_context():
    out = templates.render_template(
        "P: {{project}} on branch {{branch}}",
        {"project": "hscc", "branch": "main"},
    )
    assert out == "P: hscc on branch main"


def test_render_set_override_wins_over_context():
    # The operator's explicit --set always beats the auto-fill.
    out = templates.render_template(
        "branch={{branch}}",
        {"branch": "main"},
        overrides={"branch": "hacked"},
    )
    assert out == "branch=hacked"


def test_render_missing_slot_raises_listing_what_is_expected():
    with pytest.raises(UnfilledSlotError) as exc:
        templates.render_template(
            "goal={{goal}} context={{project}}",
            {"project": "hscc"},  # goal is missing
        )
    assert "goal" in str(exc.value)
    assert "project" not in str(exc.value)  # only the missing one is listed


def test_render_never_emits_literal_slot():
    # A fully-satisfied template has no {{ }} left in the output.
    out = templates.render_template("{{a}} {{b}}", {"a": "x", "b": "y"})
    assert "{{" not in out


def test_gather_context_derives_facts_without_passing_them():
    """Auto-filled context appears without the caller passing it."""
    proj = _project(verify="pytest")
    fake = FakeGit(branch="feat/x", head="c" * 40)
    ctx = templates.gather_context(proj, _run=fake, _list_cards=_list_cards_factory([]))
    assert ctx["project"] == "hscc"
    assert ctx["repo"] == "~/dev/hscc"
    assert ctx["branch"] == "feat/x"
    assert ctx["head_sha"] == "ccccccc… (cccccccccccccccccccccccccccccccccccccccc)"
    assert ctx["verify"] == "pytest"


def test_gather_context_surfaces_open_and_awaiting_review():
    cards = [
        {"id": "c1", "title": "open one", "status": "running"},
        {"id": "c2", "title": "await review", "status": "review"},
        {"id": "c3", "title": "blocked one", "status": "blocked"},
    ]
    ctx = templates.gather_context(
        _project(), _run=FakeGit(), _list_cards=_list_cards_factory(cards)
    )
    assert "c1 (running) open one" in ctx["open_cards"]
    assert "c2 (review) await review" in ctx["awaiting_review"]
    assert "c3 (blocked) blocked one" in ctx["awaiting_review"]
    # awaiting_review is a SUBSET of open_cards
    assert "c1 (running) open one" not in ctx["awaiting_review"]


def test_gather_context_degrades_empty_board_and_no_verify():
    ctx = templates.gather_context(
        _project(verify=None), _run=FakeGit(), _list_cards=_list_cards_factory([])
    )
    assert ctx["open_cards"] == "(none)"
    assert ctx["awaiting_review"] == "(none)"
    assert ctx["verify"] == "(none configured)"


def test_gather_context_roadmap_now_unchecked_items(tmp_path):
    proj = _project(repo=str(tmp_path))
    (tmp_path / "ROADMAP.md").write_text(
        "## Now\n- [ ] do the thing\n- [x] done thing\n## Next\n- [ ] later\n",
        encoding="utf-8",
    )
    ctx = templates.gather_context(
        proj, _run=FakeGit(), _list_cards=_list_cards_factory([])
    )
    assert "- do the thing" in ctx["roadmap_now"]
    assert "done thing" not in ctx["roadmap_now"]  # checked items dropped
    assert "later" not in ctx["roadmap_now"]


def test_gather_context_missing_roadmap_degrades():
    ctx = templates.gather_context(
        _project(), _run=FakeGit(), _list_cards=_list_cards_factory([])
    )
    assert ctx["roadmap_now"] == "(no roadmap)"


# --------------------------------------------------------------------------- #
# ask — the interactive path
# --------------------------------------------------------------------------- #

def _write_ask_template(home, body):
    """Write a template (must exist before cmd_ask)."""
    _seed(home)
    templates.save_template("myask", body, home=home)


def test_ask_auto_fill_appears_without_being_passed(tmp_path, capsys):
    home = _seed(str(tmp_path / "tpl"))
    fake_git = FakeGit(branch="feat/auto", head="b" * 40)
    fake = FakeTG()
    proj = _project(verify="pytest")
    body = "PROJECT {{project}} on {{branch}} verify {{verify}} head {{head_sha}}"
    _write_ask_template(home, body)

    rc = ask_cmd.cmd_ask(
        _ns(
            client=fake,
            run=fake_git,
            list_cards=_list_cards_factory([]),
            templates_home=home,
            project="hscc",
            template="myask",
            dry_run=False,
        ),
        [proj],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "sent 'myask' template to hscc" in out
    # The rendered text actually went to the topic.
    sent = fake.calls[0][1]["message"]
    assert "PROJECT hscc on feat/auto verify pytest" in sent
    assert "head bbbbbbb…" in sent


def test_ask_set_overrides_auto_fill(tmp_path, capsys):
    home = _seed(str(tmp_path / "tpl"))
    fake = FakeTG()
    proj = _project()
    _write_ask_template(home, "branch={{branch}} project={{project}}")

    rc = ask_cmd.cmd_ask(
        _ns(
            client=fake,
            run=FakeGit(branch="main"),
            list_cards=_list_cards_factory([]),
            templates_home=home,
            project="hscc",
            template="myask",
            set=["branch=hacked", "project=over"],
            dry_run=False,
        ),
        [proj],
    )
    capsys.readouterr()
    assert rc == 0
    sent = fake.calls[0][1]["message"]
    assert "branch=hacked project=over" in sent


def test_ask_missing_slot_errors_and_sends_nothing(tmp_path, capsys):
    home = _seed(str(tmp_path / "tpl"))
    fake = FakeTG()
    proj = _project()
    # {{target}} is an operator slot with no --set and no auto-fill -> unfilled.
    _write_ask_template(home, "GOAL: {{goal}}")

    rc = ask_cmd.cmd_ask(
        _ns(
            client=fake,
            run=FakeGit(),
            list_cards=_list_cards_factory([]),
            templates_home=home,
            project="hscc",
            template="myask",
            set=None,
            dry_run=False,
        ),
        [proj],
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "goal" in err
    assert "not filled" in err
    assert fake.calls == []  # NOTHING was sent


def test_ask_dry_run_prints_and_sends_nothing(tmp_path, capsys):
    home = _seed(str(tmp_path / "tpl"))
    fake = FakeTG()
    proj = _project()
    _write_ask_template(home, "{{project}} {{goal}}")

    rc = ask_cmd.cmd_ask(
        _ns(
            client=fake,
            run=FakeGit(),
            list_cards=_list_cards_factory([]),
            templates_home=home,
            project="hscc",
            template="myask",
            set=["goal=ziptie"],
            dry_run=True,
        ),
        [proj],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "hscc ziptie" in out          # rendered text printed
    assert "dry-run" in out
    assert fake.calls == []              # NOTHING sent


def test_ask_unknown_template_lists_available(tmp_path, capsys):
    home = _seed(str(tmp_path / "tpl"))
    proj = _project()
    rc = ask_cmd.cmd_ask(
        _ns(
            client=FakeTG(),
            run=FakeGit(),
            list_cards=_list_cards_factory([]),
            templates_home=home,
            project="hscc",
            template="nope",
            dry_run=False,
        ),
        [proj],
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown template" in err
    assert "decompose" in err  # lists the available ones (a shipped name)


def test_ask_project_without_topic_actionable(tmp_path, capsys):
    home = _seed(str(tmp_path / "tpl"))
    proj = _project(topic=None)
    rc = ask_cmd.cmd_ask(
        _ns(
            client=FakeTG(),
            run=FakeGit(),
            list_cards=_list_cards_factory([]),
            templates_home=home,
            project="hscc",
            template="decompose",
            dry_run=False,
        ),
        [proj],
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "has no topic" in err
    assert "flightdeck project repair hscc" in err


def test_ask_unknown_project_errors(tmp_path, capsys):
    home = _seed(str(tmp_path / "tpl"))
    proj = _project()
    rc = ask_cmd.cmd_ask(
        _ns(
            client=FakeTG(),
            run=FakeGit(),
            list_cards=_list_cards_factory([]),
            templates_home=home,
            project="nope",
            template="decompose",
            dry_run=False,
        ),
        [proj],
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown project" in err
    assert "nope" in err


def test_ask_unknown_template_checked_before_git(tmp_path, capsys):
    """Unknown template fails even when the project would otherwise render."""
    home = _seed(str(tmp_path / "tpl"))
    proj = _project()
    rc = ask_cmd.cmd_ask(
        _ns(
            client=FakeTG(),
            run=FakeGit(),
            list_cards=_list_cards_factory([]),
            templates_home=home,
            project="hscc",
            template="missing",
            dry_run=False,
        ),
        [proj],
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown template" in err
    assert "Available" in err


def test_ask_locked_surfaces_retry_hint(tmp_path, capsys):
    home = _seed(str(tmp_path / "tpl"))
    fake = FakeTG(locked=True)
    proj = _project()
    _write_ask_template(home, "{{project}} {{goal}}")

    rc = ask_cmd.cmd_ask(
        _ns(
            client=fake,
            run=FakeGit(),
            list_cards=_list_cards_factory([]),
            templates_home=home,
            project="hscc",
            template="myask",
            set=["goal=g"],
            dry_run=False,
        ),
        [proj],
    )
    err = capsys.readouterr().err
    assert rc == 3
    assert "locked" in err
    assert "retry" in err
    assert "Traceback" not in err


# --------------------------------------------------------------------------- #
# template — manage the store
# --------------------------------------------------------------------------- #

def test_template_list_lists_names(tmp_path, capsys):
    home = _seed(str(tmp_path / "tpl"))
    rc = ask_cmd.cmd_template_list(_ns(templates_home=home), [])
    out = capsys.readouterr().out
    assert rc == 0
    for expected in ("decompose", "brief", "review", "status", "bugfix", "spike"):
        assert expected in out.split()


def test_template_list_json(tmp_path, capsys):
    import json

    home = _seed(str(tmp_path / "tpl"))
    rc = ask_cmd.cmd_template_list(_ns(templates_home=home, json=True), [])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "decompose" in data
    assert "spike" in data


def test_template_show_returns_text(tmp_path, capsys):
    home = _seed(str(tmp_path / "tpl"))
    rc = ask_cmd.cmd_template_show(
        _ns(templates_home=home, name="decompose"), []
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "GOAL" in out


def test_template_show_unknown_errors(tmp_path, capsys):
    home = _seed(str(tmp_path / "tpl"))
    rc = ask_cmd.cmd_template_show(_ns(templates_home=home, name="nope"), [])
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown template" in err


def test_seed_is_idempotent_and_preserves_user_edits(tmp_path):
    home = _seed(str(tmp_path / "tpl"))
    # User edits decompose; re-seeding must NOT clobber it.
    templates.save_template("decompose", "CUSTOM", home=home)
    templates.ensure_seeded(home)
    assert templates.show_template("decompose", home=home) == "CUSTOM"


# --------------------------------------------------------------------------- #
# shipped templates — all render fully with a populated fixture
# --------------------------------------------------------------------------- #

_OPERATOR_SLOTS = {
    "decompose": {"goal": "ship the widget"},
    "brief": {"target": "reach zero bugs in message flow"},
    "review": {"work": "the ask command (branch wt/ask)"},
    "status": {"work": "the ask command"},
    "bugfix": {"symptom": "crash on empty input", "repro": "run it", "expected": "no crash"},
    "spike": {"topic": "migrate the registry to a SQLite store"},
}


def _populated_fixture(tmp_path):
    """A Project + stubs such that every shipped template can fully render.

    The repo is a real tmp dir so ROADMAP.md can be written; the FakeGit
    supplies branch/HEAD; the board stub supplies open + awaiting-review cards.
    """
    proj = _project(repo=str(tmp_path), verify="pytest")
    (tmp_path / "ROADMAP.md").write_text(
        "## Now\n- [ ] land the ask command\n- [x] ship message loop\n",
        encoding="utf-8",
    )
    cards = [
        {"id": "c1", "title": "ask command", "status": "running"},
        {"id": "c2", "title": "review queue", "status": "review"},
    ]
    fake = FakeGit(branch="feat/ask", head="d" * 40)
    ctx = templates.gather_context(
        proj, _run=fake, _list_cards=_list_cards_factory(cards)
    )
    return ctx


@pytest.mark.parametrize("name", sorted(_OPERATOR_SLOTS))
def test_shipped_template_renders_fully(tmp_path, name):
    """Every shipped template renders with no literal {{ }} left behind."""
    home = _seed(str(tmp_path / "tpl"))
    body = templates.show_template(name, home=home)
    ctx = _populated_fixture(tmp_path)
    overrides = dict(_OPERATOR_SLOTS[name])
    rendered = templates.render_template(body, ctx, overrides=overrides)
    assert "{{" not in rendered, f"{name} left a literal slot unfilled"
    assert "}}" not in rendered


# --------------------------------------------------------------------------- #
# CLI wiring — run() dispatch from parsed args
# --------------------------------------------------------------------------- #
# argparse cannot host sibling positionals and a subparser at the same level
# (the positionals swallow ``template list``), so ask.py spells the grammar as
# one variadic ``parts`` positional and disambiguates in run(). These tests
# drive the full parse -> run() path so the render form and the template-manager
# form both reach the right handler.

def _parsed(argv):
    """Parse CLI argv and attach the injectables run() needs."""
    args = build_parser().parse_args(argv)
    args.run = FakeGit(branch="feat/x", head="c" * 40)
    args.list_cards = _list_cards_factory([])
    args.client = FakeTG()
    return args


def _write_registry(tmp_path, rows):
    import yaml

    p = tmp_path / "registry.yaml"
    p.write_text(yaml.safe_dump({"projects": rows}, sort_keys=False), encoding="utf-8")
    return str(p)


def test_cli_render_dispatch_sends(monkeypatch, tmp_path, capsys):
    """`ask <project> <template>` renders with auto-fill and sends to the topic."""
    reg = _write_registry(tmp_path, [{"name": "hscc", "repo": "~/dev/hscc", "topic": 140, "board": "hscc", "verify": "pytest"}])
    home = _seed(str(tmp_path / "tpl"))
    args = _parsed(["ask", "hscc", "decompose", "goal=ship it"])
    args.templates_home = str(home)
    rc = ask_cmd.run(args, reg)
    out = capsys.readouterr().out
    assert rc == 0
    assert "sent 'decompose' template to hscc" in out
    # The rendered auto-filled text reached the topic.
    sent = args.client.calls[0][1]["message"]
    assert "GOAL: ship it" in sent
    assert "Project: hscc" in sent
    assert "feat/x" in sent


def test_cli_template_list_dispatch(monkeypatch, tmp_path, capsys):
    """`ask template list` reaches the manager, not the render path."""
    reg = _write_registry(tmp_path, [{"name": "hscc", "repo": "~/dev/hscc", "topic": 140, "board": "hscc"}])
    home = _seed(str(tmp_path / "tpl"))
    args = _parsed(["ask", "template", "list"])
    args.templates_home = str(home)
    rc = ask_cmd.run(args, reg)
    out = capsys.readouterr().out
    assert rc == 0
    assert "decompose" in out.split()
    assert "spike" in out.split()
    # A list must NOT have tried to render or send anything.
    assert args.client.calls == []


def test_cli_template_show_dispatch(tmp_path, capsys):
    """`ask template show <name>` prints the raw template body."""
    reg = _write_registry(tmp_path, [{"name": "hscc", "repo": "~/dev/hscc", "topic": 140}])
    home = _seed(str(tmp_path / "tpl"))
    args = _parsed(["ask", "template", "show", "decompose"])
    args.templates_home = str(home)
    rc = ask_cmd.run(args, reg)
    out = capsys.readouterr().out
    assert rc == 0
    assert "GOAL" in out


def test_cli_bare_keyvalue_tokens_absorbed_into_set(monkeypatch, tmp_path, capsys):
    """Trailing bare `key=value` tokens are treated as --set overrides.

    This is what lets `ask <project> <template> goal=x verify=cmd` work without
    the operator repeating --set for each slot.
    """
    reg = _write_registry(tmp_path, [{"name": "hscc", "repo": "~/dev/hscc", "topic": 140, "board": "hscc"}])
    home = _seed(str(tmp_path / "tpl"))
    args = _parsed(["ask", "hscc", "bugfix", "symptom=boom", "repro=run it", "expected=no crash"])
    args.templates_home = str(home)
    rc = ask_cmd.run(args, reg)
    capsys.readouterr()
    assert rc == 0
    sent = args.client.calls[0][1]["message"]
    assert "SYMPTOM: boom" in sent
    assert "REPRO: run it" in sent
    assert "EXPECTED: no crash" in sent


def test_cli_render_unknown_project_errors(tmp_path, capsys):
    reg = _write_registry(tmp_path, [{"name": "hscc", "repo": "~/dev/hscc", "topic": 140}])
    home = _seed(str(tmp_path / "tpl"))
    args = _parsed(["ask", "nope", "decompose", "goal=x"])
    args.templates_home = str(home)
    rc = ask_cmd.run(args, reg)
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown project" in err
    assert args.client.calls == []


def test_cli_bad_arg_shape_errors(tmp_path, capsys):
    """A render call with the wrong number of positional tokens is rejected."""
    reg = _write_registry(tmp_path, [{"name": "hscc", "repo": "~/dev/hscc", "topic": 140}])
    home = _seed(str(tmp_path / "tpl"))
    args = _parsed(["ask", "hscc"])  # only one token — not a render, not a manager
    args.templates_home = str(home)
    rc = ask_cmd.run(args, reg)
    err = capsys.readouterr().err
    assert rc == 2
    assert "expected" in err


def test_cli_template_unknown_verb_errors(tmp_path, capsys):
    reg = _write_registry(tmp_path, [{"name": "hscc", "repo": "~/dev/hscc", "topic": 140}])
    home = _seed(str(tmp_path / "tpl"))
    args = _parsed(["ask", "template", "frobnicate"])
    args.templates_home = str(home)
    rc = ask_cmd.run(args, reg)
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown 'template' verb" in err
