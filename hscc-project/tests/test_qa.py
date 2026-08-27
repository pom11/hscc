"""Tests for flightdeck.commands.qa — fixture-only, no real git.

Every git call is routed through an injectable ``_run`` runner backed by a
:class:`FakeGit` fixture; the project verify command goes through an injectable
``_run_verify`` shell runner (a :class:`FakeVerify`); card reads come from a
passed-in ``cards`` list (or a stubbed core.kanban). None of these tests
executes a real ``git`` binary, touches a real repo, the network, a live board,
or a real shell.

The phantom rule is central: a card whose branch IS an ancestor of ``main`` is
merged and must never appear in the queue.
"""

import argparse
import os
import subprocess
import time

import pytest

from flightdeck.commands import qa
from flightdeck.commands import review
from flightdeck.core import registry

# --------------------------------------------------------------------------- #
# Scripted git + verify runners
# --------------------------------------------------------------------------- #


class FakeGit:
    """A subprocess runner that answers qa's git commands from canned state.

    Dispatches on ``cmd[1]`` (the git subcommand). Records every call so tests
    can assert the exact commands issued.
    """

    def __init__(self, *, exists=True, landed=False, subject="implement the thing",
                 numstat=""):
        self.exists = exists
        self.landed = landed
        self.subject = subject
        self.numstat = numstat or "3\t1\tflightdeck/commands/qa.py"
        self.calls: list[list[str]] = []

    def _proc(self, cmd, rc, stdout=""):
        return subprocess.CompletedProcess(cmd, rc, stdout, "")

    def __call__(self, cmd, repo):
        self.calls.append(cmd)
        sub = cmd[1]

        if sub == "rev-parse":
            # git rev-parse --verify --quiet <branch>  (branch_exists)
            return self._proc(cmd, 0 if self.exists else 1, self.subject if self.exists else "")

        if sub == "merge-base":
            # git merge-base --is-ancestor <branch> <base>  (landed?)
            return self._proc(cmd, 0 if self.landed else 1)

        if sub == "log":
            # git log -1 --format=%s <branch>
            return self._proc(cmd, 0, self.subject)

        if sub == "diff":
            # git diff --numstat <base>...<branch>
            return self._proc(cmd, 0, self.numstat)

        if sub == "merge-tree":
            # git merge-tree --write-tree <base> <branch>
            return self._proc(cmd, 0, "abc123tree\n")

        raise AssertionError("FakeGit does not know this command: %r" % (cmd,))


class FakeVerify:
    """A shell runner for the project verify command.

    ``command`` is the shell string passed in (e.g. from the registry ``verify``
    field). ``passed`` controls the reported exit code. Records every invocation
    so tests can assert verify ran exactly once per project.
    """

    def __init__(self, *, passed=True, raise_error=False):
        self.passed = passed
        self.raise_error = raise_error
        self.calls: list[str] = []

    def __call__(self, command):
        self.calls.append(command)
        if self.raise_error:
            raise OSError("boom")
        return subprocess.CompletedProcess(command, 0 if self.passed else 1, "", "")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _ns(**kw):
    """An argparse.Namespace with qa defaults."""
    defaults = dict(
        project=None, registry=None, json=False, run=None, run_verify=None,
        cards=None, cwd=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _card(cid="t_abc", board="hscc", body="VERIFY: pytest",
          status="review", created=1000, workspace_path="~/dev/hscc"):
    return {
        "id": cid,
        "title": "Implement the thing",
        "status": status,
        "board": board,
        "branch": f"wt/{cid}",
        "body": body,
        "created_at": created,
        "workspace_path": workspace_path,
    }


def _project(name="hscc", board="hscc", repo="~/dev/hscc", verify=None, topic=None):
    return registry.Project(
        name=name, board=board, repo=repo, verify=verify, topic=topic
    )


def _write_registry(tmp_path, projects):
    import yaml

    p = tmp_path / "registry.yaml"
    rows = []
    for proj in projects:
        row = {"name": proj.name, "repo": proj.repo, "board": proj.board}
        if proj.verify is not None:
            row["verify"] = proj.verify
        if proj.topic is not None:
            row["topic"] = proj.topic
        rows.append(row)
    p.write_text(yaml.safe_dump({"projects": rows}, sort_keys=False), encoding="utf-8")
    return str(p)


def _registry(tmp_path, projects):
    reg = _write_registry(tmp_path, list(projects))
    return reg, registry.load_registry(reg)


# --------------------------------------------------------------------------- #
# Phantom rule: merged branches never appear
# --------------------------------------------------------------------------- #


def test_merged_branch_card_never_appears(tmp_path, capsys):
    """A card whose branch is an ancestor of main is the phantom — excluded."""
    reg = _write_registry(tmp_path, [_project()])
    card = _card()
    fake = FakeGit(landed=True)
    rc = qa.cmd_qa(_ns(registry=reg, run=fake, run_verify=FakeVerify(),
                       cards=[card]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing awaiting review" in out
    assert "t_abc" not in out


def test_running_and_not_review_status_are_excluded(tmp_path, capsys):
    """Only review-required/blocked cards with unmerged branches qualify."""
    reg = _write_registry(tmp_path, [_project()])
    running = _card(cid="t_a", status="running", created=1)
    closed = _card(cid="t_b", status="done", created=2)
    fake = FakeGit()
    rc = qa.cmd_qa(_ns(registry=reg, run=fake, run_verify=FakeVerify(),
                       cards=[running, closed]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing awaiting review" in out
    assert "t_a" not in out
    assert "t_b" not in out


# --------------------------------------------------------------------------- #
# AUTO-DETECT from cwd (shared helper-driven)
# --------------------------------------------------------------------------- #


def test_detects_project_from_cwd_and_filters(tmp_path, capsys):
    """No project arg + cwd inside a repo -> queue scoped, note printed.

    The card at ~/dev/hscc qualifies and the detection note is surfaced on
    stderr; the whole-fleet rows for other projects are NOT shown.
    """
    repo = registry._expand("~/dev/hscc")
    cwd = repo + "/flightdeck"  # cwd inside the hscc repo
    projects = [
        _project(name="hscc", repo="~/dev/hscc"),
        _project(name="other", repo="~/dev/other"),
    ]
    reg = _write_registry(tmp_path, projects)
    card = _card(cid="t_abc", body="VERIFY: pytest")  # workspace ~/dev/hscc
    other = _card(cid="t_other", body="VERIFY: pytest", workspace_path="~/dev/other")
    fake = FakeGit()
    rc = qa.cmd_qa(_ns(registry=reg, run=fake, run_verify=FakeVerify(),
                       cards=[card, other], cwd=cwd))
    captured = capsys.readouterr()
    assert rc == 0
    assert "using project 'hscc' (detected from cwd)" in captured.err
    assert "t_abc" in captured.out
    assert "t_other" not in captured.out


def test_no_cwd_match_renders_whole_fleet(tmp_path, capsys):
    """No project arg + cwd outside every repo -> unchanged whole-fleet view."""
    projects = [
        _project(name="hscc", repo="~/dev/hscc"),
        _project(name="other", repo="~/dev/other"),
    ]
    reg = _write_registry(tmp_path, projects)
    card = _card(cid="t_abc", body="VERIFY: pytest")
    other = _card(cid="t_other", body="VERIFY: pytest", workspace_path="~/dev/other")
    fake = FakeGit()
    rc = qa.cmd_qa(_ns(registry=reg, run=fake, run_verify=FakeVerify(),
                       cards=[card, other], cwd=str(tmp_path / "elsewhere")))
    captured = capsys.readouterr()
    assert rc == 0
    assert "detected from cwd" not in captured.err
    assert "t_abc" in captured.out
    assert "t_other" in captured.out


def test_explicit_project_wins_over_cwd(tmp_path, capsys):
    """An explicit project arg beats cwd detection, with no detection note."""
    repo = registry._expand("~/dev/hscc")
    cwd = repo + "/sub"
    projects = [
        _project(name="hscc", repo="~/dev/hscc"),
        _project(name="other", repo="~/dev/other"),
    ]
    reg = _write_registry(tmp_path, projects)
    hscc = _card(cid="t_abc", body="VERIFY: pytest")  # ~/dev/hscc
    other = _card(cid="t_other", body="VERIFY: pytest", workspace_path="~/dev/other")
    fake = FakeGit()
    rc = qa.cmd_qa(_ns(registry=reg, run=fake, run_verify=FakeVerify(),
                       cards=[hscc, other], project="other", cwd=cwd))
    captured = capsys.readouterr()
    assert rc == 0
    assert "detected from cwd" not in captured.err
    assert "t_other" in captured.out
    assert "t_abc" not in captured.out


# --------------------------------------------------------------------------- #
# UNVERIFIABLE flag + ordering
# --------------------------------------------------------------------------- #


def test_card_without_verify_flagged_unverifiable_and_first(tmp_path, capsys):
    """No VERIFY line -> flagged UNVERIFIABLE and sorted before verifiable ones."""
    reg = _write_registry(tmp_path, [_project()])
    no_verify = _card(cid="t_noverify", body="do the thing", created=5000)
    has_verify = _card(cid="t_verify", body="VERIFY: pytest", created=9)
    fake = FakeGit()
    rc = qa.cmd_qa(_ns(registry=reg, run=fake, run_verify=FakeVerify(),
                       cards=[no_verify, has_verify]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "UNVERIFIABLE" in out
    assert "NO VERIFY LINE" in out
    # UNVERIFIABLE card comes before the verifiable one despite being newer.
    assert out.index("t_noverify") < out.index("t_verify")


def test_orders_by_age_within_group(tmp_path, capsys):
    """Within a group, oldest first."""
    reg = _write_registry(tmp_path, [_project()])
    old = _card(cid="t_old", created=100)
    young = _card(cid="t_young", created=9000)
    # Both verifiable -> same group, ordered by age.
    rc = qa.cmd_qa(_ns(registry=reg, run=FakeGit(), run_verify=FakeVerify(),
                       cards=[young, old]))
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("t_old") < out.index("t_young")


def test_unverifiable_group_orders_by_age_too(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_project()])
    old = _card(cid="t_uold", body="no verify here", created=100)
    young = _card(cid="t_uyoung", body="no verify here", created=9000)
    rc = qa.cmd_qa(_ns(registry=reg, run=FakeGit(), run_verify=FakeVerify(),
                       cards=[young, old]))
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("t_uold") < out.index("t_uyoung")


# --------------------------------------------------------------------------- #
# Rendered fields
# --------------------------------------------------------------------------- #


def test_shows_project_id_branch_and_files(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit(numstat="2\t1\ta.py\n1\t0\tb.py")
    rc = qa.cmd_qa(_ns(registry=reg, run=fake, run_verify=FakeVerify(),
                       cards=[_card()]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "hscc" in out          # project
    assert "t_abc" in out         # card id
    assert "wt/t_abc" in out      # branch
    assert "files changed: 2" in out


def test_verify_line_shown(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_project()])
    rc = qa.cmd_qa(_ns(registry=reg, run=FakeGit(), run_verify=FakeVerify(),
                       cards=[_card(body="VERIFY: python -m flightdeck.cli doctor")]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "VERIFY: python -m flightdeck.cli doctor" in out
    assert "UNVERIFIABLE" not in out


# --------------------------------------------------------------------------- #
# Project-level automated verify
# --------------------------------------------------------------------------- #


def test_project_verify_passed_shown(tmp_path, capsys):
    reg = _write_registry(
        tmp_path, [_project(verify="cd ~/dev/hscc && ./scripts/run_tests.sh")]
    )
    verifier = FakeVerify(passed=True)
    rc = qa.cmd_qa(_ns(registry=reg, run=FakeGit(), run_verify=verifier,
                       cards=[_card()]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "project verify: PASSED" in out
    assert verifier.calls == ["cd ~/dev/hscc && ./scripts/run_tests.sh"]


def test_project_verify_failed_is_reported(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_project(verify="make test")])
    verifier = FakeVerify(passed=False)
    rc = qa.cmd_qa(_ns(registry=reg, run=FakeGit(), run_verify=verifier,
                       cards=[_card()]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "project verify: FAILED" in out


def test_project_verify_not_configured(tmp_path, capsys):
    """A project with no registry verify command -> nothing to run."""
    reg = _write_registry(tmp_path, [_project()])  # no verify field
    verifier = FakeVerify()
    rc = qa.cmd_qa(_ns(registry=reg, run=FakeGit(), run_verify=verifier,
                       cards=[_card()]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "not configured" in out
    assert verifier.calls == []  # never even attempted


def test_project_verify_runs_once_per_project(tmp_path, capsys):
    """Verify is a project-level fact — run once even for many cards."""
    reg = _write_registry(tmp_path, [_project(verify="pytest")])
    verifier = FakeVerify(passed=True)
    cards = [_card(cid="t_1", created=1), _card(cid="t_2", created=2)]
    rc = qa.cmd_qa(_ns(registry=reg, run=FakeGit(), run_verify=verifier, cards=cards))
    assert rc == 0
    assert len(verifier.calls) == 1


# --------------------------------------------------------------------------- #
# --json shape
# --------------------------------------------------------------------------- #


def test_json_shape_is_stable(tmp_path, capsys):
    import json

    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit(numstat="1\t0\tx.py")
    rc = qa.cmd_qa(_ns(registry=reg, run=fake, run_verify=FakeVerify(passed=True),
                       json=True, cards=[_card(created=42)],
                       state=str(tmp_path / "manual-qa.yaml")))
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    # The overall shape is an object with a queue and the manual-QA section.
    assert set(data.keys()) == {"queue", "manual_qa"}
    assert data["manual_qa"] == []  # nothing added -> empty section
    assert len(data["queue"]) == 1
    row = data["queue"][0]
    assert row == {
        "project": "hscc",
        "card_id": "t_abc",
        "title": "Implement the thing",
        "status": "review",
        "branch": "wt/t_abc",
        "unverifiable": False,
        "verify": "pytest",
        "files_changed": 1,
        "verify_configured": False,
        "verify_run": False,
        "verify_passed": False,
        "created_at": 42,
    }


def test_json_marks_unverifiable(tmp_path, capsys):
    import json

    reg = _write_registry(tmp_path, [_project()])
    rc = qa.cmd_qa(_ns(registry=reg, run=FakeGit(), run_verify=FakeVerify(),
                       json=True, cards=[_card(body="no verify here")]))
    data = json.loads(capsys.readouterr().out)
    assert len(data["queue"]) == 1
    assert data["queue"][0]["unverifiable"] is True
    assert data["queue"][0]["verify"] == ""


def test_json_empty_is_clean(tmp_path, capsys):
    import json

    reg = _write_registry(tmp_path, [_project()])
    rc = qa.cmd_qa(_ns(registry=reg,
                       run=FakeGit(landed=True),
                       run_verify=FakeVerify(),
                       json=True, cards=[_card()],
                       state=str(tmp_path / "manual-qa.yaml")))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["queue"] == []
    assert data["manual_qa"] == []


# --------------------------------------------------------------------------- #
# manual-QA store: qa add / qa done + the NEEDS MANUAL VERIFICATION section
# --------------------------------------------------------------------------- #


def _manual_state(tmp_path):
    """A tmp_path-resident store path + a fixed clock for deterministic tests."""
    return str(tmp_path / "manual-qa.yaml"), lambda: 1700000000


def _manual_entry(**over):
    """A single stored manual-QA entry; fields overridable via ``over``."""
    e = {
        "id": "mqa-1a2b3c4d",
        "project": "hscc",
        "description": "check printer byte output on real hardware",
        "card_id": "t_abc",
        "added_at": "2026-08-15T09:00:00",
        "checked": False,
        "checked_at": None,
    }
    e.update(over)
    return e


def _write_manual(path, entries):
    import yaml

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(entries, f, sort_keys=False)


# -- qa add ---------------------------------------------------------------- #


def test_qa_add_appends_unchecked_entry(tmp_path):
    """`qa add` appends a new unchecked entry with a fresh mqa id."""
    state, now = _manual_state(tmp_path)
    reg = _write_registry(tmp_path, [_project()])
    projects = registry.load_registry(reg)

    rc = qa.cmd_qa_add(
        _ns(add_project="hscc", description="verify printer output", card="t_999",
            state=state, now=now),
        projects,
    )

    assert rc == 0
    entries = qa._load_manual(state)
    assert len(entries) == 1
    e = entries[0]
    assert e["project"] == "hscc"
    assert e["description"] == "verify printer output"
    assert e["card_id"] == "t_999"
    assert e["checked"] is False
    assert e["checked_at"] is None
    import re

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", e["added_at"])
    assert e["id"].startswith("mqa-")
    assert len(e["id"]) == 12  # "mqa-" + 8 hex
    assert re.fullmatch(r"mqa-[0-9a-f]{8}", e["id"])


def test_qa_add_no_card_field(tmp_path):
    """card_id stays None when --card is not given."""
    state, now = _manual_state(tmp_path)
    reg = _write_registry(tmp_path, [_project()])
    projects = registry.load_registry(reg)

    rc = qa.cmd_qa_add(
        _ns(add_project="hscc", description="plain check", state=state, now=now),
        projects,
    )

    assert rc == 0
    assert qa._load_manual(state)[0]["card_id"] is None


def test_qa_add_appends_does_not_clobber_existing(tmp_path):
    """Adding never overwrites prior entries — it appends to the list."""
    state, now = _manual_state(tmp_path)
    _write_manual(state, [_manual_entry()])
    reg = _write_registry(tmp_path, [_project()])
    projects = registry.load_registry(reg)

    qa.cmd_qa_add(
        _ns(add_project="hscc", description="a second check", state=state, now=now),
        projects,
    )

    entries = qa._load_manual(state)
    assert len(entries) == 2
    assert entries[0]["id"] == "mqa-1a2b3c4d"       # prior entry preserved
    assert entries[1]["description"] == "a second check"
    assert entries[1]["id"] != entries[0]["id"]     # fresh non-colliding id


def test_qa_add_unknown_project_refused(tmp_path, capsys):
    """An unknown project is refused cleanly — no dangling reference created."""
    state, now = _manual_state(tmp_path)
    reg = _write_registry(tmp_path, [_project()])
    projects = registry.load_registry(reg)

    rc = qa.cmd_qa_add(
        _ns(add_project="nope", description="ghost", state=state, now=now), projects,
    )

    assert rc == 2
    assert "no project named 'nope'" in capsys.readouterr().err
    assert qa._load_manual(state) == []  # nothing was written


# -- qa done --------------------------------------------------------------- #


def test_qa_done_marks_checked(tmp_path):
    """`qa done` sets checked=True and records checked_at; entry stays in file."""
    import re

    state, now = _manual_state(tmp_path)
    _write_manual(state, [_manual_entry()])

    rc = qa.cmd_qa_done(_ns(id="mqa-1a2b3c4d", state=state, now=now))

    assert rc == 0
    entries = qa._load_manual(state)
    assert len(entries) == 1              # never deleted, kept for history
    assert entries[0]["checked"] is True
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", entries[0]["checked_at"])


def test_qa_done_unknown_id_refused(tmp_path, capsys):
    """An unknown id is refused cleanly and nothing is written."""
    state, now = _manual_state(tmp_path)
    _write_manual(state, [_manual_entry()])

    rc = qa.cmd_qa_done(_ns(id="mqa-feedbeef", state=state, now=now))

    assert rc == 2
    assert "no manual-QA entry with id 'mqa-feedbeef'" in capsys.readouterr().err
    assert qa._load_manual(state)[0]["checked"] is False  # untouched


def test_qa_done_already_checked_refused(tmp_path, capsys):
    """Marking an already-checked entry done again is refused."""
    state, now = _manual_state(tmp_path)
    _write_manual(state, [_manual_entry(checked=True, checked_at="2026-08-15T08:00:00")])

    rc = qa.cmd_qa_done(_ns(id="mqa-1a2b3c4d", state=state, now=now))

    assert rc == 2
    assert "already checked" in capsys.readouterr().err
    # checked_at unchanged
    assert qa._load_manual(state)[0]["checked_at"] == "2026-08-15T08:00:00"


# -- extended qa output ---------------------------------------------------- #


def test_manual_section_shows_when_queue_empty(tmp_path, capsys):
    """NEEDS MANUAL VERIFICATION must appear even with nothing pre-merge."""
    state, now = _manual_state(tmp_path)
    _write_manual(state, [_manual_entry()])
    reg = _write_registry(tmp_path, [_project()])

    rc = qa.cmd_qa(_ns(registry=reg, run=FakeGit(landed=True),
                       run_verify=FakeVerify(), cards=[_card()], state=state))

    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing awaiting review" in out       # pre-merge queue empty message
    assert "NEEDS MANUAL VERIFICATION" in out     # manual section still shown
    assert "check printer byte output on real hardware" in out
    assert "mqa-1a2b3c4d" in out


def test_manual_section_shows_checked_items_dropped(tmp_path, capsys):
    """Checked entries drop out of the default view."""
    state, now = _manual_state(tmp_path)
    _write_manual(state, [
        _manual_entry(id="mqa-aaaa0000", description="open item", checked=False),
        _manual_entry(id="mqa-bbbb0000", description="done item", checked=True),
    ])
    reg = _write_registry(tmp_path, [_project()])

    qa.cmd_qa(_ns(registry=reg, run=FakeGit(landed=True), run_verify=FakeVerify(),
                  cards=[_card()], state=state))

    out = capsys.readouterr().out
    assert "open item" in out
    assert "mqa-aaaa0000" in out
    assert "done item" not in out       # checked entry hidden from default view


def test_manual_section_orders_oldest_first(tmp_path, capsys):
    state, now = _manual_state(tmp_path)
    _write_manual(state, [
        _manual_entry(id="mqa-zzzz0000", description="newer", added_at="2026-08-15T11:00:00"),
        _manual_entry(id="mqa-aaaa0000", description="older", added_at="2026-08-15T08:00:00"),
    ])
    reg = _write_registry(tmp_path, [_project()])

    qa.cmd_qa(_ns(registry=reg, run=FakeGit(landed=True), run_verify=FakeVerify(),
                  cards=[_card()], state=state))

    out = capsys.readouterr().out
    assert out.index("mqa-aaaa0000") < out.index("mqa-zzzz0000")


def test_both_queue_and_manual_section_show(tmp_path, capsys):
    """With both a pre-merge queue and manual items, both sections render."""
    state, now = _manual_state(tmp_path)
    _write_manual(state, [_manual_entry()])
    reg = _write_registry(tmp_path, [_project()])

    rc = qa.cmd_qa(_ns(registry=reg, run=FakeGit(), run_verify=FakeVerify(),
                       cards=[_card()], state=state))

    out = capsys.readouterr().out
    assert rc == 0
    assert "MANUAL-TESTING QUEUE" in out
    assert "t_abc" in out               # pre-merge card
    assert "NEEDS MANUAL VERIFICATION" in out
    assert "mqa-1a2b3c4d" in out


def test_both_empty_prints_nothing_awaiting_review(tmp_path, capsys):
    """Both empty -> just the nothing-awaiting-review line, no manual section."""
    state, now = _manual_state(tmp_path)
    reg = _write_registry(tmp_path, [_project()])

    rc = qa.cmd_qa(_ns(registry=reg, run=FakeGit(landed=True),
                       run_verify=FakeVerify(), cards=[_card()], state=state))

    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing awaiting review" in out
    assert "NEEDS MANUAL VERIFICATION" not in out


def test_manual_section_project_filter(tmp_path, capsys):
    """The [project] filter narrows manual entries like it narrows the queue."""
    state, now = _manual_state(tmp_path)
    _write_manual(state, [
        _manual_entry(id="mqa-aaaa0000", project="alpha", description="alpha item"),
        _manual_entry(id="mqa-bbbb0000", project="beta", description="beta item"),
    ])
    reg = _write_registry(tmp_path, [
        _project(name="alpha", board="alpha_board", repo="~/dev/alpha"),
        _project(name="beta", board="beta_board", repo="~/dev/beta"),
    ])

    qa.cmd_qa(_ns(project="alpha", registry=reg, run=FakeGit(landed=True),
                  run_verify=FakeVerify(), cards=[], state=state))

    out = capsys.readouterr().out
    assert "mqa-aaaa0000" in out
    assert "mqa-bbbb0000" not in out


def test_json_manual_section_shape(tmp_path, capsys):
    """--json carries the manual-QA section with stable keys."""
    import json

    state, now = _manual_state(tmp_path)
    _write_manual(state, [_manual_entry()])
    reg = _write_registry(tmp_path, [_project()])

    qa.cmd_qa(_ns(registry=reg, run=FakeGit(landed=True), run_verify=FakeVerify(),
                  json=True, cards=[_card()], state=state))

    data = json.loads(capsys.readouterr().out)
    assert data["queue"] == []
    assert data["manual_qa"] == [{
        "id": "mqa-1a2b3c4d",
        "project": "hscc",
        "description": "check printer byte output on real hardware",
        "card_id": "t_abc",
        "added_at": "2026-08-15T09:00:00",
        "checked": False,
        "checked_at": None,
    }]


# --------------------------------------------------------------------------- #
# pure store helpers
# --------------------------------------------------------------------------- #


def test_load_manual_missing_file_is_empty(tmp_path):
    assert qa._load_manual(str(tmp_path / "nope.yaml")) == []


def test_load_manual_corrupt_degrades_to_empty(tmp_path):
    path = str(tmp_path / "manual-qa.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(":::: not yaml ::::\n")
    assert qa._load_manual(path) == []


def test_load_manual_not_a_list_degrades_to_empty(tmp_path):
    path = str(tmp_path / "manual-qa.yaml")
    import yaml

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump({"projects": []}, f)
    assert qa._load_manual(path) == []


def test_new_manual_id_avoids_collision():
    existing = [{"id": "mqa-1a2b3c4d"}]
    import re

    for _ in range(50):
        nid = qa._new_manual_id(existing)
        assert nid not in {e["id"] for e in existing}
        assert re.fullmatch(r"mqa-[0-9a-f]{8}", nid)


# --------------------------------------------------------------------------- #
# isolation: defaults resolve under the qa_home sandbox, never ~/.flightdeck
# --------------------------------------------------------------------------- #


def test_default_paths_resolve_under_sandbox(monkeypatch, tmp_path):
    """With the sandbox root set, the DEFAULT paths resolve strictly under it.

    This is the isolation guarantee: a default-constructed ``_manual_path()`` /
    ``_state_path()`` points under the sandbox, not the operator's real
    ``~/.flightdeck``. Fails if the sandbox fallback (``qa.qa_home``) is
    removed -- the defaults would then resolve to the real home and a save could
    silently touch the operator's live store.
    """
    sandbox = tmp_path / "qa-sandbox"
    monkeypatch.setenv("HERMES_HOME", str(sandbox))

    assert qa._manual_path().startswith(str(sandbox))
    assert qa._state_path().startswith(str(sandbox))
    # Explicitly NOT the real home files.
    assert qa._manual_path() != os.path.expanduser("~/.flightdeck/manual-qa.yaml")
    assert qa._state_path() != os.path.expanduser("~/.flightdeck/qa-notified.yaml")


def test_save_default_path_never_touches_real_home(monkeypatch, tmp_path):
    """A direct save with no injected path while the sandbox is active writes
    strictly under the sandbox — the operator's real ~/.flightdeck files are
    never touched by this test call.

    Fails if the sandbox fallback is removed: the save would then resolve to
    (and create) the real home file. We allow the real files to have pre-existed
    from genuine operator usage, so we assert on mtime being unchanged rather
    than absence.
    """
    sandbox = tmp_path / "qa-sandbox"
    monkeypatch.setenv("HERMES_HOME", str(sandbox))

    # Pre-existing real files (if any) must not be touched by the test call.
    real_manual = os.path.join(os.path.expanduser("~/.flightdeck"), "manual-qa.yaml")
    real_notified = os.path.join(os.path.expanduser("~/.flightdeck"), "qa-notified.yaml")
    before = {
        p: os.path.getmtime(p) if os.path.exists(p) else None
        for p in (real_manual, real_notified)
    }

    qa._save_manual([{"id": "mqa-deadbeef", "project": "x", "checked": False}])
    qa._save_notified(["t_1"])

    manual_path = qa._manual_path()
    state_path = qa._state_path()
    assert os.path.exists(manual_path)
    assert os.path.exists(state_path)
    assert manual_path.startswith(str(sandbox))
    assert state_path.startswith(str(sandbox))

    # The real home files are untouched (mtime unchanged when they existed,
    # still absent when they did not).
    for p, prev in before.items():
        if prev is None:
            assert not os.path.exists(p)
        else:
            assert os.path.getmtime(p) == prev


def test_qa_home_unset_falls_back_to_real_home(monkeypatch):
    """Without HERMES_HOME set, qa_home()/defaults are the real ~/.flightdeck
    — unchanged production behaviour (only the suite's conftest redirects it)."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert qa.qa_home() == os.path.expanduser("~/.flightdeck")
    assert qa._manual_path() == os.path.join(qa.qa_home(), qa.MANUAL_QA_DEFAULT)
    assert qa._state_path() == os.path.join(qa.qa_home(), qa.NOTIFY_STATE_DEFAULT)


def test_qa_add_default_store_lands_under_sandbox(monkeypatch, tmp_path):
    """cmd_qa_add with no injected state while the sandbox is active writes to
    the store under the sandbox — never ~/.flightdeck/manual-qa.yaml."""
    sandbox = tmp_path / "qa-sandbox"
    monkeypatch.setenv("HERMES_HOME", str(sandbox))
    reg = _write_registry(tmp_path, [_project()])
    projects = registry.load_registry(reg)

    rc = qa.cmd_qa_add(
        _ns(add_project="hscc", description="verify sandbox isolation",
            state=None, now=lambda: 0),
        projects,
    )

    assert rc == 0
    # Default store resolves under the sandbox and holds the new entry.
    store_path = qa._manual_path()
    assert store_path.startswith(str(sandbox))
    entries = qa._load_manual()
    assert entries[0]["description"] == "verify sandbox isolation"
    assert store_path == qa._manual_path()
    # The real home path is never the default here.
    assert store_path != os.path.expanduser("~/.flightdeck/manual-qa.yaml")




# --------------------------------------------------------------------------- #
# project filter
# --------------------------------------------------------------------------- #


def test_project_filter_restricts_to_one_project(tmp_path, capsys):
    reg = _write_registry(
        tmp_path,
        [
            _project(name="alpha", board="alpha_board", repo="~/dev/alpha"),
            _project(name="beta", board="beta_board", repo="~/dev/beta"),
        ],
    )
    a = _card(cid="t_alpha", board="alpha_board", workspace_path="~/dev/alpha")
    b = _card(cid="t_beta", board="beta_board", workspace_path="~/dev/beta")
    rc = qa.cmd_qa(_ns(project="alpha", registry=reg, run=FakeGit(),
                       run_verify=FakeVerify(), cards=[a, b]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "t_alpha" in out
    assert "t_beta" not in out
    assert "alpha" in out


def test_unknown_project_filter_rejected(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_project()])
    rc = qa.cmd_qa(_ns(project="nope", registry=reg, run=FakeGit(),
                       run_verify=FakeVerify(), cards=[_card()]))
    err = capsys.readouterr().err
    assert rc == 2
    assert "no project named 'nope'" in err


def test_project_with_no_cards_reports_cleanly(tmp_path, capsys):
    """A project with no cards reports cleanly, never prints nothing."""
    reg = _write_registry(tmp_path, [_project()])
    rc = qa.cmd_qa(_ns(project="hscc", registry=reg, run=FakeGit(),
                       run_verify=FakeVerify(), cards=[]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing awaiting review" in out


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #


def test_awaiting_review_false_for_non_review_status():
    proj = _project()
    assert not qa._awaiting_review(
        _card(status="running"), proj, _run=FakeGit()
    )


def test_awaiting_review_true_for_review_status_unmerged():
    assert qa._awaiting_review(_card(status="review"), _project(), _run=FakeGit())
    assert qa._awaiting_review(_card(status="blocked"), _project(), _run=FakeGit())


def test_awaiting_review_false_when_merged():
    assert not qa._awaiting_review(
        _card(status="review"), _project(), _run=FakeGit(landed=True)
    )


def test_project_verify_none_not_configured():
    assert qa._project_verify(_project()) == {
        "configured": False, "run": False, "passed": False
    }


def test_project_verify_pass():
    assert qa._project_verify(
        _project(verify="pytest"), _run_verify=FakeVerify(passed=True)
    ) == {"configured": True, "run": True, "passed": True}


def test_project_verify_fail():
    assert qa._project_verify(
        _project(verify="pytest"), _run_verify=FakeVerify(passed=False)
    ) == {"configured": True, "run": True, "passed": False}


# --------------------------------------------------------------------------- #
# run() wiring
# --------------------------------------------------------------------------- #


class _FakeKB:
    """minimal stand-in for hermes_cli.kanban_db producing one task per card."""

    def __init__(self, cards):
        from types import SimpleNamespace

        self._by_board = {}
        for card in cards:
            self._by_board.setdefault(card["board"], []).append(
                SimpleNamespace(
                    id=card["id"],
                    title=card["title"],
                    body=card.get("body"),
                    status=card["status"],
                    assignee="coder",
                    branch_name=card["branch"],
                    created_at=card.get("created_at"),
                    started_at=None,
                    completed_at=None,
                    workspace_kind="worktree",
                    workspace_path=card.get("workspace_path"),
                )
            )

    def list_boards(self):
        return [{"slug": b} for b in self._by_board]

    def connect(self, board=None):
        return _FakeConn(board)

    def list_tasks(self, conn, include_archived=False):
        return self._by_board.get(conn.board, [])


class _FakeConn:
    def __init__(self, board):
        self.board = board

    def close(self):
        pass


def test_run_wires_kanban_read_and_dispatch(monkeypatch, tmp_path, capsys):
    from flightdeck.core import kanban as kanban_mod

    monkeypatch.setattr(
        kanban_mod, "_load_kanban_db", lambda: _FakeKB([_card()]), raising=False,
    )
    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit()
    verifier = FakeVerify()
    args = _ns(registry=None, run=fake, run_verify=verifier, func=qa.cmd_qa)
    rc = qa.run(args, reg)
    out = capsys.readouterr().out
    assert rc == 0
    assert "wt/t_abc" in out
    assert "project verify: not configured" in out


# --------------------------------------------------------------------------- #
# Telegram client stub
# --------------------------------------------------------------------------- #


class FakeTelegram:
    """MCP client stub: records ``(tool_name, arguments)`` sends."""

    def __init__(self, raise_error=False):
        self.raise_error = raise_error
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool_name, arguments):
        from flightdeck.core.telegram import TelegramError

        if self.raise_error:
            raise TelegramError("database is locked")
        self.calls.append((tool_name, arguments))
        return "ok"


def _needs_rows(tmp_path, n=1, project=None, **card_kw):
    """Rows from _collect for ``n`` identical cards against a topic'd project."""
    proj = project or _project(topic=1001)
    reg = _write_registry(tmp_path, [proj])
    projects = registry.load_registry(reg)
    cards = [_card(cid=f"t_{i}", created=i) for i in range(n)]
    rows = qa._collect(cards, projects, _run=FakeGit(), _run_verify=FakeVerify())
    return rows, projects


# --------------------------------------------------------------------------- #
# --notify: transition tracking
# --------------------------------------------------------------------------- #


def test_notify_fires_once_on_entering(tmp_path):
    """A card entering needs-QA posts one message naming card, branch, VERIFY."""
    rows, projects = _needs_rows(tmp_path)
    client = FakeTelegram()
    state = str(tmp_path / "notified.yaml")

    newly, errors = qa._run_notify(rows, projects, _client=client, _state=state)

    assert newly == ["t_0"]
    assert errors == []
    assert len(client.calls) == 1
    tool, args = client.calls[0]
    assert tool == "telegram_send"
    assert args["topic_id"] == 1001
    assert "Implement the thing" in args["message"]
    assert "t_0" in args["message"]
    assert "wt/t_0" in args["message"]
    assert "pytest" in args["message"]  # the VERIFY line


def test_notify_second_tick_same_card_zero(tmp_path):
    """Once notified, a card that stays in the queue is NOT re-notified."""
    rows, projects = _needs_rows(tmp_path)
    client = FakeTelegram()
    state = str(tmp_path / "notified.yaml")

    qa._run_notify(rows, projects, _client=client, _state=state)  # tick 1
    assert len(client.calls) == 1

    newly, errors = qa._run_notify(rows, projects, _client=client, _state=state)  # tick 2

    assert newly == []
    assert errors == []
    assert len(client.calls) == 1  # still only the first

def test_notify_leaving_and_reentering_notifies_again(tmp_path):
    """A card leaving the queue drops out of the set; re-entry notifies again."""
    rows, projects = _needs_rows(tmp_path)
    client = FakeTelegram()
    state = str(tmp_path / "notified.yaml")

    qa._run_notify(rows, projects, _client=client, _state=state)  # enters -> notify
    assert len(client.calls) == 1

    qa._run_notify([], projects, _client=client, _state=state)  # leaves -> empty set
    newly, errors = qa._run_notify(rows, projects, _client=client, _state=state)  # re-enters

    assert newly == ["t_0"]
    assert errors == []
    assert len(client.calls) == 2  # notified again on the fresh transition


def test_notify_multiple_cards_enter_together(tmp_path):
    rows, projects = _needs_rows(tmp_path, n=2)
    client = FakeTelegram()
    state = str(tmp_path / "notified.yaml")

    newly, errors = qa._run_notify(rows, projects, _client=client, _state=state)

    assert set(newly) == {"t_0", "t_1"}
    assert errors == []
    assert len(client.calls) == 2


def test_notify_telegram_failure_is_reported_but_not_cached(tmp_path, capsys):
    """A failing send is reported and NOT marked notified (retries next tick)."""
    rows, projects = _needs_rows(tmp_path)
    bad = FakeTelegram(raise_error=True)
    state = str(tmp_path / "notified.yaml")

    newly, errors = qa._run_notify(rows, projects, _client=bad, _state=state)

    assert newly == []          # nothing succeeded
    assert len(errors) == 1     # the failure was surfaced
    assert "t_0" in errors[0][0]
    # Not cached -> next tick retries.
    ok_client = FakeTelegram()
    newly, errors = qa._run_notify(rows, projects, _client=ok_client, _state=state)
    assert newly == ["t_0"]
    assert len(ok_client.calls) == 1


def test_notify_project_without_topic_is_reported_not_cached(tmp_path, capsys):
    """A project with no telegram topic is a reported error, never silent."""
    proj = _project(topic=None)
    rows, projects = _needs_rows(tmp_path, project=proj)
    client = FakeTelegram()
    state = str(tmp_path / "notified.yaml")

    newly, errors = qa._run_notify(rows, projects, _client=client, _state=state)

    assert newly == []
    assert len(errors) == 1
    assert "no topic" in errors[0][1]
    assert client.calls == []  # nothing was sent
    # Not cached -> retried next tick (and would still fail until a topic exists)


def test_notify_without_watch_works_single_pass(tmp_path, capsys):
    """``--notify`` alone (no --watch) posts for a single pass and renders."""
    rows, projects = _needs_rows(tmp_path)
    client = FakeTelegram()
    state = str(tmp_path / "notified.yaml")
    args = _ns(
        registry=None,
        run=FakeGit(),
        run_verify=FakeVerify(),
        notify=True,
        client=client,
        state=state,
        cards=[_card()],
        func=qa.cmd_qa,
    )
    rc = qa.run(args, _write_registry(tmp_path, [projects[0]]))
    assert rc == 0
    assert len(client.calls) == 1
    # readouterr() drains BOTH streams, so capture once and use both fields.
    captured = capsys.readouterr()
    assert "notified 1 card(s) entering the QA queue" in captured.err
    assert "wt/t_abc" in captured.out  # one-shot render still shown


# --------------------------------------------------------------------------- #
# --watch
# --------------------------------------------------------------------------- #


def _collect_frames(gather, *, interval, sleep):
    """Collect qa_frames yields until the injected sleep raises KeyboardInterrupt.

    ``list(gen())`` under ``pytest.raises`` loses frames: when the generator
    raises, the partial list is discarded by ``list()``. Iterating manually and
    appending each frame as it arrives preserves them.
    """
    frames = []
    it = qa.qa_frames(gather, interval=interval, _sleep=sleep)
    while True:
        try:
            frames.append(next(it))
        except KeyboardInterrupt:
            return frames


def test_watch_frame_body_matches_one_shot(tmp_path):
    """A watch frame reuses the one-shot renderer: byte-identical body."""
    _, projects = _needs_rows(tmp_path)

    def gather():
        return qa._collect(
            [_card()], projects, _run=FakeGit(), _run_verify=FakeVerify()
        )

    # Compare against the one-shot render of the SAME data, so this asserts
    # renderer reuse rather than accidentally comparing two different cards.
    one_shot = "\n".join(qa._render(gather())) + "\n"

    sleeps: list[int] = []

    def fake_sleep(interval):
        sleeps.append(interval)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt()  # stop after the first frame

    frames = _collect_frames(gather, interval=7, sleep=fake_sleep)

    assert len(frames) == 1
    # qa_frames sleeps then yields, matching standup's watch_frames: one
    # delivered frame therefore costs two sleeps before the interrupt lands.
    assert sleeps == [7, 7]
    frame = frames[0]
    assert frame["error"] is None
    assert "\n".join(frame["lines"]) + "\n" == one_shot


def test_watch_keeps_last_good_frame_on_gather_failure(tmp_path):
    """A failing refresh keeps the last good frame and reports the error."""
    _, projects = _needs_rows(tmp_path)
    counter = [0]

    def gather():
        counter[0] += 1
        if counter[0] == 2:
            raise RuntimeError("flaky board read")
        return qa._collect(
            [_card()], projects, _run=FakeGit(), _run_verify=FakeVerify()
        )

    sleeps = []

    def fake_sleep(interval):
        sleeps.append(interval)
        if len(sleeps) >= 3:
            raise KeyboardInterrupt()

    frames = _collect_frames(gather, interval=5, sleep=fake_sleep)

    # frame1 good, frame2 failed (keeps last good lines); sleep stops after 2 frames
    assert len(frames) == 2
    assert frames[0]["error"] is None
    assert frames[1]["error"] is not None
    assert "refresh failed" in frames[1]["error"]
    assert frames[1]["lines"] == frames[0]["lines"]  # kept the last good body


def test_watch_telegram_failure_does_not_crash_loop(tmp_path, capsys):
    """A telegram send failure inside --watch is reported, loop keeps going."""
    rows, projects = _needs_rows(tmp_path)
    reg = _write_registry(tmp_path, [projects[0]])
    bad = FakeTelegram(raise_error=True)

    args = _ns(
        registry=reg,
        run=FakeGit(),
        run_verify=FakeVerify(),
        cards=[_card()],
        watch=True,
        interval=5,
        now=lambda: 1000,
        sleep=_StopAfter(2),
        client=bad,
        state=str(tmp_path / "notified.yaml"),
    )

    rc = qa._watch(args, projects, reg)
    assert rc == 0

    err = capsys.readouterr().err
    assert "notify failed" in err


def test_watch_manual_section_shows_pending_item(tmp_path):
    """A watch frame renders the pending manual item, like the one-shot view."""
    state, _ = _manual_state(tmp_path)
    _write_manual(state, [_manual_entry()])

    def gather():
        return qa._collect(
            [_card()], [_project()], _run=FakeGit(), _run_verify=FakeVerify()
        )

    def load_manual():
        return qa._unchecked_manual(state, None)

    sleeps: list[int] = []

    def fake_sleep(interval):
        sleeps.append(interval)

    it = qa.qa_frames(gather, interval=7, _sleep=fake_sleep,
                      manual_loader=load_manual)
    frame1 = next(it)

    body = "\n".join(frame1["lines"])
    assert frame1["error"] is None
    assert "NEEDS MANUAL VERIFICATION" in body
    assert "mqa-1a2b3c4d" in body
    assert "check printer byte output on real hardware" in body


def test_watch_manual_checked_off_disappears_next_tick(tmp_path):
    """A manual item checked off between ticks disappears on the next tick.

    The store is re-read on every frame: frame1 shows the unchecked item, the
    entry is marked checked, and frame2 (a fresh read) drops it.
    """
    state, _ = _manual_state(tmp_path)
    _write_manual(state, [_manual_entry()])

    def gather():
        return qa._collect(
            [_card()], [_project()], _run=FakeGit(), _run_verify=FakeVerify()
        )

    def load_manual():
        return qa._unchecked_manual(state, None)

    sleeps: list[int] = []

    def fake_sleep(interval):
        sleeps.append(interval)
        if len(sleeps) >= 3:
            raise KeyboardInterrupt()

    frames: list[dict] = []
    it = qa.qa_frames(gather, interval=7, _sleep=fake_sleep,
                      manual_loader=load_manual)
    frames.append(next(it))          # tick 1: item unchecked -> shown

    # Simulate the operator checking the item off while watching.
    entries = qa._load_manual(state)
    entries[0]["checked"] = True
    _write_manual(state, entries)

    try:
        frames.append(next(it))      # tick 2: fresh read -> hidden
    except KeyboardInterrupt:
        pass

    assert "mqa-1a2b3c4d" in "\n".join(frames[0]["lines"])
    assert "mqa-1a2b3c4d" not in "\n".join(frames[1]["lines"])


def test_watch_manual_project_filter(tmp_path):
    """The [project] filter applies in watch mode exactly as in the default view."""
    state, _ = _manual_state(tmp_path)
    _write_manual(state, [
        _manual_entry(id="mqa-aaaa0000", project="alpha", description="alpha item"),
        _manual_entry(id="mqa-bbbb0000", project="beta", description="beta item"),
    ])

    def gather():
        return qa._collect(
            [_card()], [_project()], _run=FakeGit(), _run_verify=FakeVerify()
        )

    def load_manual():
        return qa._unchecked_manual(state, "alpha")

    sleeps: list[int] = []

    def fake_sleep(interval):
        sleeps.append(interval)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt()

    frames: list[dict] = []
    it = qa.qa_frames(gather, interval=7, _sleep=fake_sleep,
                      manual_loader=load_manual)
    while True:
        try:
            frames.append(next(it))
        except KeyboardInterrupt:
            break

    body = "\n".join(frames[0]["lines"])
    assert "mqa-aaaa0000" in body     # alpha item kept under the alpha filter
    assert "mqa-bbbb0000" not in body  # beta item filtered out


class _StopAfter:
    """A sleep that raises KeyboardInterrupt after ``n`` calls, ending _watch."""

    def __init__(self, n):
        self.n = n
        self.calls = 0

    def __call__(self, interval):
        self.calls += 1
        if self.calls >= self.n:
            raise KeyboardInterrupt()
        time.sleep(0)


def test_help_shows_both_flags():
    """build_subparser exposes --watch, --interval and --notify."""
    import argparse

    parser = argparse.ArgumentParser(prog="flightdeck")
    sub = parser.add_subparsers(dest="command")
    qa.build_subparser(sub)
    # The qa subparser help is only reachable via the sub-action; inspect the
    # options it registered on the "qa" subparser directly.
    qa_parser = None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            qa_parser = action.choices.get("qa")
    assert qa_parser is not None
    option_strings = {opt for a in qa_parser._actions for opt in a.option_strings}
    assert "--watch" in option_strings
    assert "--interval" in option_strings
    assert "--notify" in option_strings


def _build_qa_parser():
    """A real (unmocked) parser with the `qa` subcommand registered."""
    import argparse

    parser = argparse.ArgumentParser(prog="flightdeck")
    sub = parser.add_subparsers(dest="command")
    qa.build_subparser(sub)
    return parser


def test_cli_parses_bare_project_as_render_filter():
    """REAL argparse: `qa <project>` must parse and filter, never an 'invalid
    choice'. This is the regression the nested-subparser approach introduced
    and the case at the heart of this card's scope: the render path is
    untouched — `qa <project>` keeps working exactly as before."""
    parser = _build_qa_parser()
    args = parser.parse_args(["qa", "testproj"])
    assert args.command == "qa"
    assert args.project == ["testproj"]  # leading token is the render filter


def test_cli_parses_qa_with_no_project():
    """REAL argparse: bare `qa` parses with an empty project list (all)."""
    parser = _build_qa_parser()
    args = parser.parse_args(["qa"])
    assert args.command == "qa"
    assert args.project == []


def test_cli_parses_qa_add_form():
    """REAL argparse: `qa add <project> \"desc\" [--card ID]` parses cleanly."""
    parser = _build_qa_parser()
    args = parser.parse_args(["qa", "add", "hscc", "verify printer", "--card", "t_1"])
    assert args.command == "qa"
    assert args.project == ["add", "hscc", "verify printer"]
    assert args.card == "t_1"


def test_cli_parses_qa_done_form():
    """REAL argparse: `qa done <id>` parses cleanly."""
    parser = _build_qa_parser()
    args = parser.parse_args(["qa", "done", "mqa-1a2b3c4d"])
    assert args.command == "qa"
    assert args.project == ["done", "mqa-1a2b3c4d"]


def test_cli_parses_project_filter_with_flags():
    """REAL argparse: `qa <project> --watch` — option AFTER the positional —
    must parse (the greedy nested-subparser approach swallowed options here)."""
    parser = _build_qa_parser()
    args = parser.parse_args(["qa", "testproj", "--watch"])
    assert args.project == ["testproj"]
    assert args.watch is True


def test_qa_add_run_dispatch(monkeypatch, tmp_path, capsys):
    """`qa add` through run() dispatches on the leading token, writes the
    store, and never reads the kanban board."""
    from flightdeck.core import kanban as kanban_mod

    def _boom():
        raise AssertionError("qa add must not read the board")

    monkeypatch.setattr(kanban_mod, "_load_kanban_db", _boom, raising=False)
    state, now = _manual_state(tmp_path)
    reg = _write_registry(tmp_path, [_project()])
    args = argparse.Namespace(
        project=["add", "hscc", "verify printer"], card=None,
        state=state, now=now,
    )
    rc = qa.run(args, reg)
    assert rc == 0
    assert qa._load_manual(state)[0]["description"] == "verify printer"


def test_qa_done_run_dispatch(monkeypatch, tmp_path, capsys):
    """`qa done` through run() dispatches on the leading token, marks the
    entry checked, and never reads the kanban board."""
    from flightdeck.core import kanban as kanban_mod

    def _boom():
        raise AssertionError("qa done must not read the board")

    monkeypatch.setattr(kanban_mod, "_load_kanban_db", _boom, raising=False)
    state, now = _manual_state(tmp_path)
    _write_manual(state, [_manual_entry()])
    reg = _write_registry(tmp_path, [_project()])
    args = argparse.Namespace(
        project=["done", "mqa-1a2b3c4d"], state=state, now=now,
    )
    rc = qa.run(args, reg)
    assert rc == 0
    assert qa._load_manual(state)[0]["checked"] is True
