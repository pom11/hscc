"""Tests for flightdeck.commands.decompose.

``decompose`` asks the cluster orchestrator to break a goal into atomic cards,
then GATES the proposal on card quality BEFORE anything is created. These tests
drive it against STUBS: the ``ask`` seam (``(prompt, topic_id) -> proposal``)
stands in for the whole Telegram round-trip, the render deps (``run`` /
``list_cards`` / ``templates_home``) stand in for git and the board, and
``_load_kanban_db`` is stubbed so ``kanban.create_task`` never touches the real
DB. No test touches real Telegram, git, the live kanban board, the network or
the cluster.

The gate contract under test: a well-formed proposal passes; EACH rejection
reason fires independently (multi-concern, missing VERIFY, missing references,
missing acceptance, no dependency position); ``--apply`` creates exactly the
passing cards; without ``--apply`` nothing is created; a proposal where EVERY
card fails creates nothing and exits non-zero.
"""

import argparse
import json
import os

import pytest

from flightdeck.commands import decompose as dec
from flightdeck.core import kanban, registry, templates
from flightdeck.core.templates import UnfilledSlotError
from flightdeck.core.telegram import TelegramError, TopicLockedError
from conftest import TEST_GROUP_ID


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #

class FakeTG:
    """Stands in for the MCP daemon client: callable (tool, args) -> str.

    ``telegram_send`` records the call and mirrors the daemon's reply string;
    ``locked=True`` makes every call raise the single-writer SQLite error, which
    the transport normalises to :class:`TopicLockedError`.
    """

    def __init__(self, locked=False):
        self.locked = locked
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if self.locked:
            raise ConnectionError("sqlite3.OperationalError: database is locked")
        if tool_name == "telegram_send":
            return f"Sent to {TEST_GROUP_ID} topic {arguments['topic_id']}."
        raise AssertionError(f"FakeTG does not know tool {tool_name!r}")


class FakeGit:
    """Stands in for the subprocess runner: callable (cmd, repo) -> CompletedProcess."""

    def __init__(self, branch="main", head="a" * 40):
        self.branch = branch
        self.head = head

    def __call__(self, cmd, repo):
        import subprocess

        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(cmd, 0, self.branch, "")
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1:] == ["HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, self.head, "")
        return subprocess.CompletedProcess(cmd, 128, "", "")


class FakeKB:
    """Stubs hermes_cli.kanban_db for create_task: connect + create_task."""

    def __init__(self, fail=False, new_id="card-1", current="default"):
        self.fail = fail
        self.new_id = new_id
        self.current = current
        self.created: list[dict] = []

    def connect(self, board=None):
        return argparse.Namespace(board=board, close=lambda: None)

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
    """Build an argparse.Namespace with defaults a decompose cmd needs."""
    defaults = dict(
        client=None,
        registry=None,
        ask=None,
        locate_refs=None,
        run=None,
        list_cards=None,
        templates_home=None,
        repo_root=None,
        read_milestone=None,
        project="",
        goal=None,
        milestone=None,
        apply=False,
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
    def _list_cards(board=None, include_archived=False):
        return cards
    return _list_cards


def _seed(home):
    """Copy the shipped template set into a tmp templates home."""
    templates.ensure_seeded(home)
    return home


# --------------------------------------------------------------------------- #
# Proposal fixtures (the orchestrator's JSON reply)
# --------------------------------------------------------------------------- #

def _card(
    id,
    title="atomic card",
    concern="single concern",
    verify="pytest tests/test_x.py",
    acceptance="a test asserting X fails when X is gone",
    body=None,
    refs=None,
    depends_on=None,
    assignee="coder",
):
    """A well-formed card dict. Each field defaults to a PASSING value."""
    if body is None:
        body = (
            f"{title}. Single concern, one module. "
            f"VERIFY: {verify}\nACCEPTANCE: {acceptance}\n"
        )
    return {
        "id": id,
        "title": title,
        "body": body,
        "concern": concern,
        "verify": verify,
        "acceptance": acceptance,
        "references": refs or [],
        "assignee": assignee,
        "depends_on": list(depends_on or []),
    }


def _proposal(*cards):
    return json.dumps({"goal": "g", "cards": list(cards)})


def _ask_stub(proposal_text):
    """Return an ``args.ask`` stub that returns ``proposal_text`` verbatim."""
    def _ask(prompt, topic_id, client=None):
        return proposal_text
    return _ask


# --------------------------------------------------------------------------- #
# Render via the G4 template machinery (not a duplicated inline prompt)
# --------------------------------------------------------------------------- #

def test_render_prompt_uses_the_shipped_decompose_template(tmp_path):
    """The prompt is rendered from the template store, goal injected."""
    home = _seed(str(tmp_path / "tpl"))
    out = dec._render_prompt(
        _project(verify="pytest"),
        "break the goal",
        templates_home=home,
        run=FakeGit(branch="feat/x"),
        list_cards=_list_cards_factory([]),
    )
    assert "GOAL: break the goal" in out
    # Auto-filled project context appears without being retyped.
    assert f"Project: hscc" in out
    assert "feat/x" in out
    # No literal {{slot}} survives rendering — every slot was filled.
    assert "{{" not in out


def test_render_prompt_unfilled_goal_is_an_error(tmp_path):
    """A goal-less render is an error and sends NOTHING (no literal slot)."""
    home = _seed(str(tmp_path / "tpl"))
    text = templates.show_template("decompose", home=home)
    with pytest.raises(UnfilledSlotError):
        templates.render_template(text, {"project": "hscc"}, overrides={})


# --------------------------------------------------------------------------- #
# P4 — the built prompt forbids acting (live 2026-08-11 incident)
# --------------------------------------------------------------------------- #
#
# Observed live: `flightdeck decompose flightdeck --milestone release-flow`
# (WITHOUT --apply) resulted in the orchestrator CREATING SEVEN CARDS on the
# board itself — it has native kanban tools, read the prompt as a work request,
# and acted. The prompt must now state, near the top, that it is a request for
# a PROPOSAL only: reply with a single JSON object, do NOT create/modify/claim/
# dispatch any card, do NOT run any tool, and acting has no payoff because any
# card created directly is ignored and archived.

def test_sent_prompt_forbids_creating_cards_for_the_live_incident(tmp_path):
    """The SENT prompt (rendered) carries the do-not-create-cards instruction.

    Named for the live incident: a `decompose` WITHOUT --apply must not lead
    the orchestrator to create cards. These are the exact lines the orchestrator
    reads, so the forbid must be asserted on the rendered text that is actually
    sent, not just on the raw template.
    """
    home = _seed(str(tmp_path / "tpl"))
    out = dec._render_prompt(
        _project(verify="pytest"),
        "break the goal",
        templates_home=home,
        run=FakeGit(branch="feat/x"),
        list_cards=_list_cards_factory([]),
    )
    # Normalise whitespace so assertions are robust to template line-wrapping:
    # the actual phrases may split across rendered lines.
    flat = " ".join(out.split())
    assert "REPLY WITH A SINGLE JSON OBJECT ONLY" in flat
    assert "Do NOT create, modify, claim or dispatch any kanban card" in flat
    assert "Do NOT run any tool" in flat
    assert "This is a request for a PROPOSAL" in flat
    assert "will be ignored and archived" in flat
    assert "acting has no payoff" in flat
    # It sits near the top: it precedes the project context block.
    assert out.index("REPLY WITH A SINGLE JSON OBJECT ONLY") < out.index(
        "Project context"
    )


def test_sent_prompt_keeps_json_schema_section_unchanged(tmp_path):
    """The card-shape rules that define the required JSON schema are intact.

    The template now adds the do-not-create-cards block but must not disturb
    the rules that establish the required card shape (the JSON schema the gate
    relies on). Every rule line and the closing instruction survive.
    """
    home = _seed(str(tmp_path / "tpl"))
    out = dec._render_prompt(
        _project(verify="pytest"),
        "break the goal",
        templates_home=home,
        run=FakeGit(),
        list_cards=_list_cards_factory([]),
    )
    for rule in (
        "EXACTLY ONE CONCERN per card",
        "A VERIFY: line on every card",
        "CONCRETE file/function references",
        "ACCEPTANCE CRITERIA phrased so a test would FAIL",
        "DEPENDENCY ORDER between cards",
        "One card per output entry",
        "Produce the complete, ordered card set now.",
    ):
        assert rule in out


def test_shipped_decompose_template_carries_the_instruction():
    """The shipped templates/decompose.md (the `flightdeck ask` framing) carries
    the same do-not-create-cards instruction, at the top near the goal."""
    shipped = templates.SHIPPED_DIR / "decompose.md"
    text = shipped.read_text(encoding="utf-8")
    flat = " ".join(text.split())
    assert "REPLY WITH A SINGLE JSON OBJECT ONLY" in flat
    assert "Do NOT create, modify, claim or dispatch any kanban card" in flat
    assert "will be ignored and archived" in flat
    # Near the top: before the project context block.
    assert text.index("REPLY WITH A SINGLE JSON OBJECT ONLY") < text.index(
        "Project context"
    )



# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def test_parse_proposal_well_formed():
    cards = dec.parse_proposal(
        _proposal(_card(1), _card(2, depends_on=[1]))
    )
    assert [c.id for c in cards] == [1, 2]
    assert cards[1].depends_on == [1]
    assert cards[0].assignee == "coder"


def test_parse_proposal_no_json_raises():
    with pytest.raises(dec.ProposalParseError):
        dec.parse_proposal("the orchestrator said nothing machine-readable")


def test_parse_proposal_missing_id_title_raises():
    with pytest.raises(dec.ProposalParseError):
        dec.parse_proposal(json.dumps({"cards": [{"title": "no id"}]}))


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def _gate(card, all_cards=None, repo_root="/nonexistent", locator=None):
    return dec.gate_card(
        card,
        all_cards=all_cards or [card],
        repo_root=repo_root,
        locator=locator,
    )


def test_gate_well_formed_card_passes(tmp_path):
    # A module the body references must EXIST under repo_root for the card to be
    # counted as "touching an existing module"; we give the body a concrete ref
    # so it passes regardless.
    body = (
        "Add flagging to lint. Refer to flightdeck/core/lint.py:61:referenced_modules. "
        "VERIFY: pytest tests/test_lint.py\nACCEPTANCE: a test fails without it\n"
    )
    (tmp_path / "flightdeck").mkdir(parents=True)
    card = dec.ProposedCard(
        id=1,
        title="t",
        body=body,
        concern="single concern",
        verify="pytest tests/test_lint.py",
        acceptance="a test fails without it",
        references=["flightdeck/core/lint.py:61:referenced_modules"],
    )
    reasons = _gate(card, repo_root=str(tmp_path), locator=lambda b, r: [])
    assert reasons == []


def test_gate_multi_concern_fires():
    card = dec.ProposedCard(
        id=1, title="t", body="b", concern="add flag AND a report",
        verify="x", acceptance="y", references=["a.py:1:f"],
    )
    assert "covers more than one concern" in _gate(card)


def test_gate_missing_verify_fires():
    card = dec.ProposedCard(
        id=1, title="t", body="b", concern="single", verify="",
        acceptance="y", references=["a.py:1:f"],
    )
    assert "lacks a VERIFY: line" in _gate(card)


def test_gate_missing_acceptance_fires():
    card = dec.ProposedCard(
        id=1, title="t", body="b", concern="single", verify="pytest",
        acceptance="", references=["a.py:1:f"],
    )
    assert "lacks acceptance criteria" in _gate(card)


def test_gate_missing_references_fires_when_touching_existing_module(tmp_path):
    """A card touching a real module with no concrete refs — even after the
    locator runs — is rejected for missing references."""
    (tmp_path / "flightdeck" / "core").mkdir(parents=True)
    (tmp_path / "flightdeck" / "core" / "kanban.py").write_text(
        "def create_task():\n    pass\n", encoding="utf-8"
    )
    body = "touches flightdeck/core/kanban.py but names no seam VERIFY: x\nACCEPTANCE: y\n"
    card = dec.ProposedCard(
        id=1, title="t", body=body, concern="single", verify="x",
        acceptance="y", references=[],
    )
    # locator returns nothing -> still no concrete refs -> reject.
    reasons = _gate(card, repo_root=str(tmp_path), locator=lambda b, r: [])
    assert "lacks concrete file/function references" in reasons


def test_gate_locator_injects_refs_and_passes(tmp_path):
    """When the locator FINDS refs for a card touching a real module, they are
    injected into card.references and the reference gate PASSES."""
    (tmp_path / "flightdeck" / "core").mkdir(parents=True)
    (tmp_path / "flightdeck" / "core" / "kanban.py").write_text(
        "def create_task():\n    pass\n", encoding="utf-8"
    )
    body = "touches flightdeck/core/kanban.py VERIFY: x\nACCEPTANCE: y\n"
    card = dec.ProposedCard(
        id=1, title="t", body=body, concern="single", verify="x",
        acceptance="y", references=[],
    )
    reasons = _gate(
        card,
        repo_root=str(tmp_path),
        locator=lambda b, r: ["flightdeck/core/kanban.py:234:create_task"],
    )
    assert reasons == []
    assert "flightdeck/core/kanban.py:234:create_task" in card.references


def test_gate_no_dependency_position_fires_on_dangling_dep():
    c1 = dec.ProposedCard(id=1, title="a", body="b", concern="single",
                          verify="x", acceptance="y", references=["a.py:1:f"],
                          depends_on=[99])
    assert "no place in the dependency order" in _gate(c1, all_cards=[c1])


def test_gate_no_dependency_position_fires_on_self_dep():
    c1 = dec.ProposedCard(id=1, title="a", body="b", concern="single",
                          verify="x", acceptance="y", references=["a.py:1:f"],
                          depends_on=[1])
    assert "no place in the dependency order" in _gate(c1, all_cards=[c1])


def test_gate_no_dependency_position_fires_on_cycle():
    c1 = dec.ProposedCard(id=1, title="a", body="b", concern="single",
                          verify="x", acceptance="y", references=["a.py:1:f"],
                          depends_on=[2])
    c2 = dec.ProposedCard(id=2, title="b", body="b", concern="single2",
                          verify="x", acceptance="y", references=["b.py:1:f"],
                          depends_on=[1])
    assert "no place in the dependency order" in _gate(c1, all_cards=[c1, c2])


def test_each_rejection_fires_independently(tmp_path):
    """A card that fails on MANY grounds reports all of them, not one."""
    card = dec.ProposedCard(
        id=1, title="t", body="touches flightdeck/core/kanban.py",
        concern="a and b", verify="", acceptance="", references=[],
        depends_on=[99],
    )
    reasons = _gate(card, repo_root=str(tmp_path), locator=lambda b, r: [])
    for expected in (
        "covers more than one concern",
        "lacks a VERIFY: line",
        "lacks acceptance criteria",
        "no place in the dependency order",
    ):
        assert expected in reasons


# --------------------------------------------------------------------------- #
# The full command — apply / no-apply / all-rejected
# --------------------------------------------------------------------------- #

def _run_decompose(args, projects, monkeypatch, kb=None):
    kb = kb or FakeKB()
    _stub_kb(kb, monkeypatch)
    return dec.cmd_decompose(args, projects), kb


def _base_args(proposal_text, *, home, repo_root, apply=False, project="hscc", goal="g",
               locate_refs=None, milestone=None, read_milestone=None):
    return _ns(
        ask=_ask_stub(proposal_text),
        templates_home=home,
        run=FakeGit(),
        list_cards=_list_cards_factory([]),
        repo_root=repo_root,
        project=project,
        goal=goal,
        milestone=milestone,
        read_milestone=read_milestone,
        apply=apply,
        locate_refs=locate_refs if locate_refs is not None else (lambda b, r: []),
    )


def test_well_formed_proposal_creates_nothing_without_apply(tmp_path, monkeypatch, capsys):
    home = _seed(str(tmp_path / "tpl"))
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)
    proposal = _proposal(
        _card(1, refs=["foo.py:1:f"], depends_on=[]),
        _card(2, refs=["bar.py:1:f"], depends_on=[1]),
    )
    args = _base_args(proposal, home=home, repo_root=repo_root, apply=False)
    rc, kb = _run_decompose(args, [_project()], monkeypatch)

    assert rc == 0
    assert kb.created == []          # nothing created
    out = capsys.readouterr().out
    assert "ACCEPTED CARDS" in out
    assert "dry-run" in out
    assert "DEPENDENCY EDGES" in out


def test_apply_creates_exactly_the_passing_cards(tmp_path, monkeypatch, capsys):
    home = _seed(str(tmp_path / "tpl"))
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)
    project = _project()
    card1 = _card(1, refs=["foo.py:1:f"], depends_on=[])
    card2 = _card(2, refs=["bar.py:1:f"], depends_on=[1])
    card3 = _card(3, concern="bundle this and that", refs=["baz.py:1:f"])
    proposal = _proposal(card1, card2, card3)
    args = _base_args(proposal, home=home, repo_root=repo_root, apply=True)
    rc, kb = _run_decompose(args, [project], monkeypatch)

    assert rc == 0
    # Only the two passing cards were created.
    assert [c["title"] for c in kb.created] == ["atomic card", "atomic card"]
    assert all(c["conn_board"] == "hscc" for c in kb.created)
    out = capsys.readouterr().out
    assert "REJECTED" in out          # card 3 was reported
    assert "created 2 card(s)" in out


def test_apply_anchors_cards_to_project_repo_worktree(tmp_path, monkeypatch, capsys):
    """Sibling of the dispatch anchoring fix: a decomposed card must be created
    as a ``worktree`` workspace anchored to the project repo (the resolved
    ``repo_root``, which is ``proj.repo`` unless overridden), never a scratch
    dir. Fails if the worktree anchoring is removed and cards fall back to the
    store's ``scratch`` default.

    (decompose.py half of card t_6839559e; the message.py half already shipped
    as flightdeck v0.4.1.)
    """
    home = _seed(str(tmp_path / "tpl"))
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)
    project = _project(repo=repo_root)  # proj.repo == repo_root in this test
    card = _card(1, refs=["foo.py:1:f"], depends_on=[])
    proposal = _proposal(card)
    args = _base_args(proposal, home=home, repo_root=repo_root, apply=True)
    rc, kb = _run_decompose(args, [project], monkeypatch)

    assert rc == 0
    assert len(kb.created) == 1
    # Anchored to the project repo as a worktree, not scratch.
    assert kb.created[0]["workspace_kind"] == "worktree"
    assert kb.created[0]["workspace_path"] == repo_root == project.repo


def test_apply_project_with_board_ignores_global_current_board_regression(tmp_path, monkeypatch, capsys):
    """R1-R7 wrong-board incident (decompose): a project WITH a board lands on
    THAT board, never on Hermes' global current board, even when the current
    board points elsewhere."""
    home = _seed(str(tmp_path / "tpl"))
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)
    card = _card(1, refs=["foo.py:1:f"])
    args = _base_args(
        _proposal(card),
        home=home,
        repo_root=repo_root,
        apply=True,
    )
    # GLOBAL current board is 'default' but the project's OWN board is 'hscc'.
    kb = FakeKB(current="default")
    rc, kb = _run_decompose(args, [_project(board="hscc")], monkeypatch, kb=kb)
    assert rc == 0
    assert kb.created[0]["conn_board"] == "hscc"
    captured = capsys.readouterr()
    assert "created 1 card(s) on board 'hscc'" in captured.out
    # The project HAS a board, so no fallback say-so.
    assert "no board for hscc" not in captured.err


def test_decompose_does_not_mutate_global_current_board_even_on_failure(tmp_path, monkeypatch, capsys):
    """The GLOBAL current board is never changed by decompose --apply — not
    even when a create raises. A card-creating command must read the current
    board as a fallback, never write it, so a failure can't leave the global
    board redirected for unrelated work."""
    home = _seed(str(tmp_path / "tpl"))
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)
    card = _card(1, refs=["foo.py:1:f"])
    args = _base_args(
        _proposal(card),
        home=home,
        repo_root=repo_root,
        apply=True,
    )
    kb = FakeKB(fail=True, current="flightdeck")  # create_task raises
    rc, kb = _run_decompose(args, [_project(board=None)], monkeypatch, kb=kb)
    assert rc == 3                       # nothing was created
    captured = capsys.readouterr()
    assert "could not create card" in captured.err
    # The current board was read for the fallback AND left exactly as it was.
    assert kb.current == "flightdeck"


def test_apply_injects_located_refs_into_created_body(tmp_path, monkeypatch, capsys):
    """A card the locator fixes is created with the REFS section in its body."""
    home = _seed(str(tmp_path / "tpl"))
    repo_root = str(tmp_path / "repo")
    os.makedirs(os.path.join(repo_root, "flightdeck", "core"), exist_ok=True)
    with open(os.path.join(repo_root, "flightdeck", "core", "kanban.py"), "w", encoding="utf-8") as f:
        f.write("def create_task():\n    pass\n")
    project = _project()
    card = _card(1, body="touches flightdeck/core/kanban.py VERIFY: x\nACCEPTANCE: y\n",
                 refs=[])
    args = _base_args(
        _proposal(card),
        home=home,
        repo_root=repo_root,
        apply=True,
        locate_refs=lambda b, r: ["flightdeck/core/kanban.py:234:create_task"],
    )
    rc, kb = _run_decompose(args, [project], monkeypatch)
    assert rc == 0
    assert len(kb.created) == 1
    assert "flightdeck/core/kanban.py:234:create_task" in kb.created[0]["body"]
    assert "REFERENCES:" in kb.created[0]["body"]


def test_every_card_fails_creates_nothing_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    home = _seed(str(tmp_path / "tpl"))
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)
    proposal = _proposal(
        _card(1, concern="bundle this and that", verify="", acceptance=""),
        _card(2, concern="also bad and bundled", verify=""),
    )
    args = _base_args(proposal, home=home, repo_root=repo_root, apply=True)
    rc, kb = _run_decompose(args, [_project()], monkeypatch)

    assert rc == 3                       # non-zero: nothing happened
    assert kb.created == []              # nothing created
    err = capsys.readouterr().err
    assert "nothing" in err and "--apply created nothing" in err


def test_every_card_fails_reports_each_reason(tmp_path, monkeypatch, capsys):
    home = _seed(str(tmp_path / "tpl"))
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)
    proposal = _proposal(_card(1, concern="a and b", verify=""))
    args = _base_args(proposal, home=home, repo_root=repo_root, apply=True)
    rc, _ = _run_decompose(args, [_project()], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 3
    assert "covers more than one concern" in out
    assert "lacks a VERIFY: line" in out


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

def test_unknown_project_errors(tmp_path, capsys):
    home = _seed(str(tmp_path / "tpl"))
    args = _base_args("{}", home=home, repo_root=str(tmp_path / "repo"),
                      project="nope")
    rc = dec.cmd_decompose(args, [_project()])
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown project" in err


def test_project_without_topic_errors(tmp_path, capsys):
    home = _seed(str(tmp_path / "tpl"))
    args = _base_args("{}", home=home, repo_root=str(tmp_path / "repo"),
                      project="hscc")
    rc = dec.cmd_decompose(args, [_project(topic=None)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "has no topic" in err
    assert "project repair" in err


def test_project_without_board_falls_back_to_current_and_says_so(tmp_path, monkeypatch, capsys):
    """A project with no board still decomposes: --apply creates cards on the
    CURRENT board and the command SAYS SO (\"no board for <project>; created on
    '<current>'\") — a silent fallback is how cards end up on the wrong board."""
    home = _seed(str(tmp_path / "tpl"))
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)
    card = _card(1, refs=["foo.py:1:f"])
    args = _base_args(
        _proposal(card),
        home=home,
        repo_root=repo_root,
        apply=True,
    )
    kb = FakeKB(current="flightdeck")  # current board is 'flightdeck'
    rc, _ = _run_decompose(args, [_project(board=None)], monkeypatch, kb=kb)
    assert rc == 0
    # Cards were created on the CURRENT board, not hardcoded 'default'.
    assert all(c["conn_board"] == "flightdeck" for c in kb.created)
    captured = capsys.readouterr()
    assert "no board for hscc; created on 'flightdeck'" in captured.err
    assert "created 1 card(s) on board 'flightdeck'" in captured.out


def test_ask_failure_reports_and_returns_2(tmp_path, monkeypatch, capsys):
    home = _seed(str(tmp_path / "tpl"))
    def _raising_ask(prompt, topic_id, client=None):
        raise TopicLockedError("database is locked")
    args = _base_args("{}", home=home, repo_root=str(tmp_path / "repo"))
    args.ask = _raising_ask
    rc, kb = _run_decompose(args, [_project()], monkeypatch)
    err = capsys.readouterr().err
    assert rc == 2
    assert "locked" in err
    assert kb.created == []


def test_unparseable_proposal_returns_2(tmp_path, monkeypatch, capsys):
    home = _seed(str(tmp_path / "tpl"))
    args = _base_args("the orchestrator did not return JSON",
                      home=home, repo_root=str(tmp_path / "repo"))
    rc, kb = _run_decompose(args, [_project()], monkeypatch)
    err = capsys.readouterr().err
    assert rc == 2
    assert "no JSON object" in err
    assert kb.created == []


# --------------------------------------------------------------------------- #
# --milestone: read the goal from ROADMAP.md and stamp every created card
# --------------------------------------------------------------------------- #

def test_milestone_stamps_every_created_card(tmp_path, monkeypatch, capsys):
    """Every card created under --milestone carries `MILESTONE: <id>`.

    This is what makes milestone progress automatic instead of a discipline
    the operator has to remember on every card.
    """
    home = _seed(str(tmp_path / "tpl"))
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)
    proposal = _proposal(
        _card(1, refs=["foo.py:1:f"], depends_on=[]),
        _card(2, refs=["bar.py:1:f"], depends_on=[1]),
    )
    args = _base_args(
        proposal, home=home, repo_root=repo_root, apply=True, goal=None,
        milestone="auth-hardening",
        read_milestone=lambda proj, mid: ("harden the auth path", None),
    )
    rc, kb = _run_decompose(args, [_project()], monkeypatch)

    assert rc == 0
    assert len(kb.created) == 2
    for card in kb.created:
        body = card.get("body") if isinstance(card, dict) else str(card)
        assert "MILESTONE: auth-hardening" in body


def test_milestone_and_goal_together_is_a_usage_error(tmp_path, monkeypatch, capsys):
    """Passing both is ambiguous, so it is refused before anything is sent."""
    home = _seed(str(tmp_path / "tpl"))
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)
    args = _base_args(
        _proposal(_card(1, refs=["foo.py:1:f"], depends_on=[])),
        home=home, repo_root=repo_root, goal="free text", milestone="m1",
    )
    rc, kb = _run_decompose(args, [_project()], monkeypatch)
    assert rc == 2
    assert kb.created == []
    assert "mutually exclusive" in capsys.readouterr().err


def test_neither_goal_nor_milestone_is_a_usage_error(tmp_path, monkeypatch, capsys):
    """One of them is required."""
    home = _seed(str(tmp_path / "tpl"))
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)
    args = _base_args(
        _proposal(_card(1, refs=["foo.py:1:f"], depends_on=[])),
        home=home, repo_root=repo_root, goal=None, milestone=None,
    )
    rc, kb = _run_decompose(args, [_project()], monkeypatch)
    assert rc == 2
    assert kb.created == []


def test_unknown_milestone_errors_and_creates_nothing(tmp_path, monkeypatch, capsys):
    """An unknown id is an error, never a silent empty decomposition."""
    home = _seed(str(tmp_path / "tpl"))
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)
    args = _base_args(
        _proposal(_card(1, refs=["foo.py:1:f"], depends_on=[])),
        home=home, repo_root=repo_root, apply=True, goal=None, milestone="nope",
        read_milestone=lambda proj, mid: (None, "no milestone 'nope'; known ids: auth-hardening"),
    )
    rc, kb = _run_decompose(args, [_project()], monkeypatch)
    assert rc == 2
    assert kb.created == []
    err = capsys.readouterr().err
    assert "nope" in err and "auth-hardening" in err


def test_milestone_without_apply_creates_nothing(tmp_path, monkeypatch, capsys):
    """--milestone still proposes; creation stays behind --apply."""
    home = _seed(str(tmp_path / "tpl"))
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)
    args = _base_args(
        _proposal(_card(1, refs=["foo.py:1:f"], depends_on=[])),
        home=home, repo_root=repo_root, apply=False, goal=None,
        milestone="auth-hardening",
        read_milestone=lambda proj, mid: ("harden the auth path", None),
    )
    rc, kb = _run_decompose(args, [_project()], monkeypatch)
    assert rc == 0
    assert kb.created == []


# --------------------------------------------------------------------------- #
# N15 — decompose waits for the JSON proposal, not the prose preamble
# --------------------------------------------------------------------------- #
#
# The live 2026-08-11 failure: `flightdeck decompose flightdeck
# --milestone release-flow` exited 2 with "no JSON object found in the
# orchestrator's reply". decompose's default ask returned the FIRST genuine
# message — the orchestrator's prose preamble — which contains no JSON, then
# parse_proposal failed. N15 wires the ask seam with `accept=_proposal_accept`
# (which REUSES parse_proposal) so it keeps polling past prose until a message
# actually carries a proposal, threads `--timeout` into the wait, and surfaces
# the raw reply under `RAW REPLY (no JSON proposal)` on timeout. These tests
# exercise the REAL default-ask path through cmd_decompose (args.ask=None) with
# a stubbed Telegram client and an injected clock — no real network/time.

class _FeedClient:
    """Stub Telegram MCP client: k-th telegram_read serves snapshots[k]."""

    def __init__(self, snapshots):
        self.snapshots = [list(s) for s in snapshots]
        self._reads = 0
        self.calls = []

    def __call__(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if tool_name == "telegram_send":
            return "Sent."
        if tool_name == "telegram_read":
            idx = min(self._reads, len(self.snapshots) - 1)
            self._reads += 1
            return "\n".join(self.snapshots[idx])
        raise AssertionError(f"unknown tool {tool_name!r}")


class _Clock:
    """Injected clock: now()/sleep() advance self.t in step."""

    def __init__(self, start=100.0):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def _prose_proposal_line(text):
    return f"[2026-08-11 11:00] Hermes: {text}"


_N15_PREAMBLE = "Here is my proposal. I'll break this goal into cards."
_N15_ACK_LINE = _prose_proposal_line(_N15_PREAMBLE)


def _n15_default_args(home, repo_root, *, timeout=None, extra_attrs=None):
    """args that use the REAL default ask (args.ask=None) + injected now/sleep."""
    kw = dict(
        ask=None,
        client=None,
        now=None,
        sleep=None,
        templates_home=home,
        run=FakeGit(),
        list_cards=_list_cards_factory([]),
        repo_root=repo_root,
        project="hscc",
        goal="g",
        milestone=None,
        apply=True,
        read_milestone=None,
    )
    if timeout is not None:
        kw["timeout"] = timeout
    if extra_attrs:
        kw.update(extra_attrs)
    return _ns(**kw)


def test_decompose_wires_proposal_accept_and_skips_prose_for_the_live_failure(
    tmp_path, monkeypatch, capsys
):
    """The real default ask, wired with `accept=_proposal_accept`, skips the
    orchestrator's prose preamble and waits for the JSON proposal.

    This is the exact live-failure shape: a preamble (no JSON) is posted, then
    the real proposal. With the accept predicate the seam rejects the preamble
    and returns once the message carrying the proposal lands — a passing card is
    created. Without the fix, the preamble would be returned and decompose would
    exit 2 with "no JSON object found".
    """
    home = _seed(str(tmp_path / "tpl"))
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)
    proposal = _proposal(_card(1, title="do it", refs=["foo.py:1:f"], depends_on=[]))
    feed = _FeedClient(
        [
            [],                                     # topic before send (watermark)
            [_N15_ACK_LINE],                        # poll 1: prose preamble, rejected
            [_N15_ACK_LINE, _prose_proposal_line(proposal)],  # poll 2: JSON lands
        ]
    )
    clock = _Clock()
    args = _n15_default_args(home, repo_root, timeout=10)
    args.client = feed
    args.now = clock.now
    args.sleep = clock.sleep

    rc, kb = _run_decompose(args, [_project(topic=140)], monkeypatch)
    assert rc == 0
    assert [c.get("title") for c in kb.created] == ["do it"]
    out = capsys.readouterr().out
    assert "PROPOSAL" in out


def test_decompose_timeout_no_json_reports_count_and_raw_reply(
    tmp_path, monkeypatch, capsys
):
    """On timeout with no JSON, decompose reports how many messages were seen
    AND prints the raw reply under `RAW REPLY (no JSON proposal)`.

    Without a parseable proposal the seam rejects every fragment; the error
    names the count (here 2) and surfaces the most recent raw reply as a
    diagnostic, so a wrong-shaped answer is distinguishable from silence.
    """
    home = _seed(str(tmp_path / "tpl"))
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)
    more_prose = "still not JSON here, just more reasoning about the cards."
    feed = _FeedClient(
        [
            [],
            [_N15_ACK_LINE],                     # fragment 1 seen, rejected
            [_N15_ACK_LINE, _prose_proposal_line(more_prose)],  # fragment 2 rejected
        ]
    )
    clock = _Clock()
    args = _n15_default_args(home, repo_root, timeout=5)
    args.client = feed
    args.now = clock.now
    args.sleep = clock.sleep

    rc, kb = _run_decompose(args, [_project(topic=140)], monkeypatch)
    assert rc == 2
    assert kb.created == []
    err = capsys.readouterr().err
    assert "no accepted reply within 5s" in err
    assert "2 messages seen" in err
    assert "RAW REPLY (no JSON proposal):" in err
    assert _N15_PREAMBLE in err
    assert more_prose in err


def test_decompose_timeout_flag_reaches_the_ask_seam(tmp_path, monkeypatch, capsys):
    """`--timeout N` reaches the default ask seam's `timeout`, quoting N.

    A spy in place of _default_ask records the timeout value the seam is
    invoked with; a single flag must govern the whole wait, so the quoted value
    is the N from `--timeout`, and the accept predicate is the wired
    _proposal_accept.
    """
    home = _seed(str(tmp_path / "tpl"))
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)

    seen = {}

    def _spy_default_ask(prompt, topic_id, _client=None, *, timeout=300,
                         now=None, sleep=None, accept=None):
        seen["timeout"] = timeout
        seen["accept"] = accept
        # Return a well-formed fenced proposal so the pipeline completes.
        proposal = _proposal(_card(1, refs=["foo.py:1:f"], depends_on=[]))
        return "```json\n" + proposal + "\n```"

    monkeypatch.setattr(dec, "_default_ask", _spy_default_ask)

    args = _n15_default_args(home, repo_root, timeout=42)
    rc, kb = _run_decompose(args, [_project(topic=140)], monkeypatch)
    assert rc == 0
    assert seen["timeout"] == 42  # the seam was invoked with the flag's value
    assert seen["accept"] is dec._proposal_accept  # the predicate is wired
    assert [c.get("title") for c in kb.created] == ["atomic card"]
