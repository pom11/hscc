"""Tests for flightdeck.commands.review — fixture-only, no real git.

Every git call is routed through an injectable ``_run`` runner backed by a
:class:`FakeGit` fixture; card reads come from a passed-in ``cards`` list (or a
stubbed core.kanban); card closing is an injectable ``close_card`` callable.
None of these tests executes a real ``git`` binary, touches a real repo, the
network, or a live board, so the suite stays fast and deterministic.
"""

import argparse
import subprocess
import time

import pytest

from flightdeck.commands import review
from flightdeck.core import registry

# --------------------------------------------------------------------------- #
# A scripted git runner that understands review's commands
# --------------------------------------------------------------------------- #


class FakeGit:
    """A subprocess runner that answers review's git commands from canned state.

    Dispatches on ``cmd[1]`` (the git subcommand). Records every call so tests
    can assert the exact sequence issued (especially under --apply). Raises on
    a command it does not understand so a surprising call is caught loudly.
    """

    def __init__(
        self,
        *,
        exists=True,
        landed=False,
        subject="implement the thing",
        numstat="",
        merge_tree_conflicts=0,
        checkout_ok=True,
        merge_ok=True,
        push_ok=True,
    ):
        self.exists = exists
        self.landed = landed
        self.subject = subject
        self.numstat = numstat or "3\t1\tflightdeck/commands/review.py"
        self.merge_tree_conflicts = merge_tree_conflicts
        self.checkout_ok = checkout_ok
        self.merge_ok = merge_ok
        self.push_ok = push_ok
        self.calls: list[list[str]] = []

    def _proc(self, cmd, rc, stdout=""):
        return subprocess.CompletedProcess(cmd, rc, stdout, "")

    def __call__(self, cmd, repo):
        self.calls.append(cmd)
        sub = cmd[1]

        if sub == "rev-parse":
            # git rev-parse --verify --quiet <branch>  (branch_exists)
            assert cmd[2] == "--verify"
            return self._proc(cmd, 0 if self.exists else 1, self.subject if self.exists else "")

        if sub == "merge-base":
            # git merge-base --is-ancestor <branch> <base>  (landed?)
            assert cmd[2] == "--is-ancestor"
            return self._proc(cmd, 0 if self.landed else 1)

        if sub == "log":
            # git log -1 --format=%s <branch>
            assert cmd[2] == "-1" and cmd[3] == "--format=%s"
            return self._proc(cmd, 0, self.subject)

        if sub == "diff":
            # git diff --numstat <base>...<branch>
            assert cmd[2] == "--numstat"
            return self._proc(cmd, 0, self.numstat)

        if sub == "merge-tree":
            # git merge-tree --write-tree <base> <branch>
            assert cmd[2] == "--write-tree"
            if self.merge_tree_conflicts == 0:
                return self._proc(cmd, 0, "abc123tree\n")
            markers = "\n<<<<<<< .merge_file_aaaa\n" * self.merge_tree_conflicts
            return self._proc(cmd, 1, "abc123tree\n<Conflicted>\n" + markers)

        if sub == "checkout":
            return self._proc(cmd, 0 if self.checkout_ok else 128)

        if sub == "merge":
            return self._proc(cmd, 0 if self.merge_ok else 128)

        if sub == "push":
            return self._proc(cmd, 0 if self.push_ok else 128)

        raise AssertionError("FakeGit does not know this command: %r" % (cmd,))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _ns(**kw):
    """An argparse.Namespace with review defaults."""
    defaults = dict(
        card="t_abc", registry=None, json=False, apply=False,
        base="main", run=None, close_card=None, cards=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _card(cid="t_abc", board="hscc", body="VERIFY: pytest", workspace=None):
    """A card whose workspace_path resolves to ``~/dev/<board>`` by default.

    Attribution (``kanban.project_for_card``) matches the card's
    ``workspace_path`` against a project's ``repo``. The default points the
    workspace under ``~/dev/<board>`` so it resolves to the project whose repo
    is ``~/dev/<board>`` (the common fixture shape in this suite).
    """
    if workspace is None:
        workspace = f"~/dev/{board}/.worktrees/{cid}"
    return {
        "id": cid,
        "title": "Implement the thing",
        "status": "review",
        "board": board,
        "branch": f"wt/{cid}",
        "body": body,
        "workspace_path": workspace,
    }


def _project(name="hscc", board="hscc", repo="~/dev/hscc"):
    return registry.Project(name=name, board=board, repo=repo)


def _write_registry(tmp_path, projects):
    import yaml

    p = tmp_path / "registry.yaml"
    rows = [
        {"name": proj.name, "repo": proj.repo, "board": proj.board}
        for proj in projects
    ]
    p.write_text(yaml.safe_dump({"projects": rows}, sort_keys=False), encoding="utf-8")
    return str(p)


def _projects(tmp_path, *projects):
    reg = _write_registry(tmp_path, list(projects))
    return reg, registry.load_registry(reg)


# --------------------------------------------------------------------------- #
# Resolution: card -> branch -> project -> repo
# --------------------------------------------------------------------------- #


def test_resolves_card_to_branch_project_repo(tmp_path, capsys):
    """Review resolves the card, finds wt/<id>, and maps board -> repo."""
    reg = _write_registry(tmp_path, [_project(board="hscc", repo="~/dev/hscc")])
    fake = FakeGit()
    rc = review.cmd_review(_ns(card="t_abc", registry=reg, run=fake, cards=[_card()]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "wt/t_abc" in out            # branch: wt/<card_id> convention
    assert "project hscc" in out        # project
    assert "/dev/hscc" in out           # repo (registry expands ~)
    assert "MERGE: clean" in out
    # Dry-run: no mutating git call and no card close.
    assert not any(c[0:2] == ["git", "checkout"] for c in fake.calls)
    assert not any(c[0:2] == ["git", "merge"] for c in fake.calls)
    assert not any(c[0:2] == ["git", "push"] for c in fake.calls)


def test_resolution_uses_cards_supplied_to_args(tmp_path, capsys):
    """A card passed via args.cards is used instead of re-reading the board."""
    reg = _write_registry(tmp_path, [_project(board="other", repo="~/dev/other")])
    fake = FakeGit(subject="add feature")
    rc = review.cmd_review(
        _ns(card="c9", registry=reg, run=fake, cards=[_card(cid="c9", board="other")])
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "add feature" in out
    assert "/dev/other" in out


def test_unknown_card_is_rejected(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_project()])
    rc = review.cmd_review(_ns(card="nope", registry=reg, run=FakeGit(), cards=[]))
    err = capsys.readouterr().err
    assert rc == 2
    assert "no card" in err


def test_card_with_unowned_workspace_is_rejected(tmp_path, capsys):
    """A card whose workspace_path resolves to no registry repo is rejected.

    Attribution is by workspace_path (not board slug): a card whose workspace
    does not fall under any project's repo is unresolvable against git — never
    guessed into a project based on its board.
    """
    reg = _write_registry(tmp_path, [_project(board="hscc")])
    card = _card(cid="t_xyz", board="hscc", workspace="~/elsewhere/nt_xyz")
    rc = review.cmd_review(
        _ns(card="t_xyz", registry=reg, run=FakeGit(), cards=[card])
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "cannot resolve its git repo" in err


def test_divergence_resolves_by_workspace_not_board(tmp_path, capsys):
    """reconcile and review agree: attribution is workspace_path, not board.

    The exact divergence this unifies: a card on project A's board whose
    workspace_path points at project B's worktree must review against B (the
    repo where the branch physically lives), exactly as reconcile would — not
    against A just because the card is displayed on A's board.
    """
    # Project A owns board "hscc"; project B owns the repo the card's work is in.
    proj_a = _project(name="projA", board="hscc", repo="~/dev/projA")
    proj_b = registry.Project(name="projB", repo="~/dev/projB", board="bprojB")
    reg = _write_registry(tmp_path, [proj_a, proj_b])
    # Card displayed on A's board but its work lives under B's repo.
    card = _card(board="hscc", workspace="~/dev/projB/.worktrees/t_abc")
    fake = FakeGit()
    rc = review.cmd_review(_ns(card="t_abc", registry=reg, run=fake, cards=[card]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "project projB" in out          # attributed to B by workspace_path
    assert "/dev/projB" in out             # reviewed against B's repo
    assert "projA" not in out


def test_missing_branch_is_rejected(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_project()])
    broken = {**_card(), "branch": None}
    rc = review.cmd_review(_ns(card="t_abc", registry=reg, run=FakeGit(), cards=[broken]))
    err = capsys.readouterr().err
    assert rc == 2
    assert "no resolvable branch" in err


# --------------------------------------------------------------------------- #
# Diff summary: subject, files, insertions, deletions
# --------------------------------------------------------------------------- #


def test_shows_commit_subject_and_diff_stats(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit(
        subject="fix the parser",
        numstat="5\t2\tflightdeck/core/kanban.py\n1\t0\tflightdeck/commands/review.py",
    )
    rc = review.cmd_review(_ns(card="t_abc", registry=reg, run=fake, cards=[_card()]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "fix the parser" in out
    assert "2 file(s) changed" in out
    assert "6 insertion(s)(+)" in out
    assert "2 deletion(s)(-)" in out


def test_diff_stats_ignore_binary_rows(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit(numstat="-\t-\tassets/logo.png\n3\t1\tflightdeck/core/x.py")
    rc = review.cmd_review(_ns(card="t_abc", registry=reg, run=fake, cards=[_card()]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 file(s) changed" in out
    assert "3 insertion(s)(+)" in out
    assert "1 deletion(s)(-)" in out


# --------------------------------------------------------------------------- #
# Conflict count
# --------------------------------------------------------------------------- #


def test_clean_merge_shows_zero_conflicts(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit(merge_tree_conflicts=0)
    rc = review.cmd_review(_ns(card="t_abc", registry=reg, run=fake, cards=[_card()]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "MERGE: clean" in out


def test_conflicting_merge_reports_count(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit(merge_tree_conflicts=2)
    rc = review.cmd_review(_ns(card="t_abc", registry=reg, run=fake, cards=[_card()]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 conflict(s)" in out
    assert "will need resolution" in out


# --------------------------------------------------------------------------- #
# VERIFY line
# --------------------------------------------------------------------------- #


def test_missing_verify_flagged_loudly(tmp_path, capsys):
    """A card with no VERIFY line must say so loudly, not silently pass."""
    reg = _write_registry(tmp_path, [_project()])
    card = _card(body="do the thing")  # no VERIFY:
    rc = review.cmd_review(_ns(card="t_abc", registry=reg, run=FakeGit(), cards=[card]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "NO VERIFY LINE" in out
    assert "Reverse-engineer" in out


def test_verify_line_shown_when_present(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_project()])
    card = _card(body="TESTS: run suite\nVERIFY: python -m flightdeck.cli doctor")
    rc = review.cmd_review(_ns(card="t_abc", registry=reg, run=FakeGit(), cards=[card]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "VERIFY: python -m flightdeck.cli doctor" in out
    assert "NO VERIFY LINE" not in out


def test_verify_present_but_empty_is_surfaced(tmp_path, capsys):
    """VERIFY: with nothing after it is reported, not silently treated as ok."""
    reg = _write_registry(tmp_path, [_project()])
    card = _card(body="VERIFY:")
    rc = review.cmd_review(_ns(card="t_abc", registry=reg, run=FakeGit(), cards=[card]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "present but empty" in out
    assert "NO VERIFY LINE" not in out


# --------------------------------------------------------------------------- #
# Dry-run changes nothing
# --------------------------------------------------------------------------- #


def test_dry_run_performs_no_merge_and_no_close(tmp_path, capsys):
    """Default: show everything, issue no mutating git command, no card close."""
    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit()
    closed = []
    rc = review.cmd_review(
        _ns(card="t_abc", registry=reg, run=fake, close_card=lambda c, b: closed.append(c),
            cards=[_card()])
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry-run" in out
    assert closed == []                                  # card not closed
    assert not any(c[0:2] == ["git", "checkout"] for c in fake.calls)
    assert not any(c[0:2] == ["git", "merge"] for c in fake.calls)
    assert not any(c[0:2] == ["git", "push"] for c in fake.calls)


# --------------------------------------------------------------------------- #
# --apply merges AND closes as one action
# --------------------------------------------------------------------------- #


def test_apply_merges_and_closes(tmp_path, capsys):
    """--apply runs checkout+merge+push and then closes the card."""
    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit()
    closed = []
    rc = review.cmd_review(
        _ns(card="t_abc", registry=reg, run=fake, apply=True,
            close_card=lambda c, b: closed.append(c), cards=[_card()])
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "merged wt/t_abc into main and pushed" in out
    assert closed == ["t_abc"]          # card closed as part of the SAME action
    # The exact git sequence for a merge: checkout main, merge branch, push.
    git_calls = [c for c in fake.calls if c[0] == "git"]
    assert ["git", "checkout", "main"] in git_calls
    assert ["git", "merge", "wt/t_abc"] in git_calls
    assert ["git", "push", "origin", "main"] in git_calls


def test_apply_failed_merge_does_not_close(tmp_path, capsys):
    """A merge conflict under --apply leaves the card open for resolution."""
    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit(merge_ok=False)
    closed = []
    rc = review.cmd_review(
        _ns(card="t_abc", registry=reg, run=fake, apply=True,
            close_card=lambda c, b: closed.append(c), cards=[_card()])
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "merge failed" in out
    assert closed == []                 # nothing closed on a failed merge


def test_apply_checks_out_and_stops_if_failed(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit(checkout_ok=False)
    closed = []
    rc = review.cmd_review(
        _ns(card="t_abc", registry=reg, run=fake, apply=True,
            close_card=lambda c, b: closed.append(c), cards=[_card()])
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "could not checkout" in out
    assert closed == []


# --------------------------------------------------------------------------- #
# Already-landed refusal
# --------------------------------------------------------------------------- #


def test_already_landed_is_refused(tmp_path, capsys):
    """A branch that is already an ancestor of main is refused up front."""
    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit(landed=True)
    rc = review.cmd_review(_ns(card="t_abc", registry=reg, run=fake, cards=[_card()]))
    out = capsys.readouterr().out
    assert rc == 1
    assert "already landed, nothing to review" in out
    # Refusal means we never even look at the diff or merge.
    assert not any(c[0:2] == ["git", "diff"] for c in fake.calls)
    assert not any(c[0:2] == ["git", "merge"] for c in fake.calls)


def test_missing_branch_reports_not_exists(tmp_path, capsys):
    """A branch that does not resolve is reported, never a fabricated diff."""
    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit(exists=False)
    rc = review.cmd_review(_ns(card="t_abc", registry=reg, run=fake, cards=[_card()]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "does not exist" in out


# --------------------------------------------------------------------------- #
# Cross-project dependents surfacing (registry.dependent_notice)
# --------------------------------------------------------------------------- #


def _write_registry_with_deps(tmp_path, rows):
    """Like _write_registry, but rows can carry a raw ``depends_on`` list."""
    import yaml

    p = tmp_path / "registry.yaml"
    p.write_text(yaml.safe_dump({"projects": rows}, sort_keys=False), encoding="utf-8")
    return str(p)


def test_dependents_line_shown_for_project_with_dependents(tmp_path, capsys):
    """A card on a project OTHER projects depend on shows the exact notice."""
    reg = _write_registry_with_deps(tmp_path, [
        {"name": "bc", "repo": "~/dev/bc", "board": "hscc"},
        {"name": "app", "repo": "~/dev/app", "board": "app", "depends_on": ["bc"]},
        {"name": "driver", "repo": "~/dev/driver", "board": "driver", "depends_on": ["bc"]},
    ])
    fake = FakeGit()
    rc = review.cmd_review(_ns(card="t_abc", registry=reg, run=fake, cards=[_card(board="hscc", workspace="~/dev/bc/.worktrees/t_abc")]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "dependents: 2 dependent project(s): app, driver — consider verifying they still work" in out


def test_no_dependents_line_for_project_with_no_dependents(tmp_path, capsys):
    """A project nothing depends on shows no dependents line — no noise."""
    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit()
    rc = review.cmd_review(_ns(card="t_abc", registry=reg, run=fake, cards=[_card()]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "dependents:" not in out


def test_dependents_in_json_output(tmp_path, capsys):
    """--json carries the same dependents notice under a `dependents` key."""
    import json as jsonlib

    reg = _write_registry_with_deps(tmp_path, [
        {"name": "bc", "repo": "~/dev/bc", "board": "hscc"},
        {"name": "app", "repo": "~/dev/app", "board": "app", "depends_on": ["bc"]},
    ])
    fake = FakeGit()
    rc = review.cmd_review(_ns(card="t_abc", registry=reg, run=fake, cards=[_card(board="hscc", workspace="~/dev/bc/.worktrees/t_abc")], json=True))
    out = capsys.readouterr().out
    assert rc == 0
    parsed = jsonlib.loads(out)
    assert parsed["dependents"] == "1 dependent project(s): app — consider verifying they still work"


def test_dependents_none_in_json_when_no_dependents(tmp_path, capsys):
    import json as jsonlib

    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit()
    rc = review.cmd_review(_ns(card="t_abc", registry=reg, run=fake, cards=[_card()], json=True))
    out = capsys.readouterr().out
    assert rc == 0
    parsed = jsonlib.loads(out)
    assert parsed["dependents"] is None


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #


def test_parse_numstat_handles_empty():
    assert review._parse_numstat("") == (0, 0, 0)


def test_parse_numstat_sums_rows():
    out = "3\t1\ta/b.py\n5\t0\tc.py\n-\t-\tbinary.png"
    assert review._parse_numstat(out) == (3, 8, 1)


def test_conflict_count_counts_markers():
    assert review._conflict_count("") == 0
    assert review._conflict_count("<<<<<<< a\n<<<<<<< b") == 2


def test_verify_line_absent_for_no_marker():
    assert review._verify_line("do the thing") == (False, "")


def test_verify_line_present():
    assert review._verify_line("TESTS: x\nVERIFY: pytest") == (True, "pytest")


def test_verify_line_bulleted_and_case_insensitive():
    assert review._verify_line("- verify: ./run_tests.sh") == (True, "./run_tests.sh")
    assert review._verify_line("verify:") == (True, "")


# --------------------------------------------------------------------------- #
# run() wiring: reads cards via stubbed core.kanban
# --------------------------------------------------------------------------- #


class _FakeKB:
    """minimal stand-in for hermes_cli.kanban_db producing one task per card."""

    def __init__(self, cards):
        from types import SimpleNamespace

        self._by_board = {}
        self.archived: set[tuple[str, str]] = set()  # (board, id) archived
        for card in cards:
            self._by_board.setdefault(card["board"], []).append(
                SimpleNamespace(
                    id=card["id"],
                    title=card["title"],
                    body=card.get("body"),
                    status=card["status"],
                    assignee="coder",
                    branch_name=card["branch"],
                    created_at=1,
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

    def archive_task(self, conn, task_id) -> bool:
        """Mirror ``kanban_db.archive_task``: archive once, then report False.

        Records the ``(board, id)`` in ``self.archived`` so a test can assert
        the card's status actually became archived (the exact gap this suite
        guards). Returns True the first time for a given card, False if it is
        already archived — same idempotent contract as the real library.
        """
        key = (conn.board, str(task_id))
        if key in self.archived:
            return False
        self.archived.add(key)
        return True


class _FakeConn:
    def __init__(self, board):
        self.board = board

    def close(self):
        pass


def test_run_wires_kanban_read_and_dispatch(monkeypatch, tmp_path, capsys):
    """run() reads cards through core/kanban then dispatches to cmd_review."""
    from flightdeck.core import kanban as kanban_mod

    monkeypatch.setattr(
        kanban_mod,
        "_load_kanban_db",
        lambda: _FakeKB([_card()]),
        raising=False,
    )
    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit()
    args = _ns(card="t_abc", registry=None, run=fake, func=review.cmd_review)
    rc = review.run(args, reg)
    out = capsys.readouterr().out
    assert rc == 0
    assert "wt/t_abc" in out
    assert "MERGE: clean" in out


def test_run_apply_actually_archives_card(monkeypatch, tmp_path, capsys):
    """A REAL ``review --apply`` via ``run()`` must archive the card.

    This is the exact gap that let the bug ship: nothing in production ever
    wired ``close_card``, so ``--apply`` claimed "card closed" without
    archiving anything. Here we go through ``run()`` with NO pre-injected fake
    close seam, so ``run()`` wires the real ``_real_close_card`` — and we then
    assert the card was genuinely archived through the kanban library (its
    ``archive_task`` was reached with the right board card), not just that the
    CLI printed success.
    """
    from flightdeck.core import kanban as kanban_mod

    kb = _FakeKB([_card()])  # default status "review"
    monkeypatch.setattr(kanban_mod, "_load_kanban_db", lambda: kb, raising=False)
    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit()
    # No close_card injected — run() must wire _real_close_card itself.
    args = _ns(card="t_abc", registry=None, run=fake, apply=True,
               func=review.cmd_review)
    rc = review.run(args, reg)
    out = capsys.readouterr().out
    assert rc == 0
    assert "merged wt/t_abc into main and pushed" in out
    assert "card t_abc closed" in out          # printed once, on real success
    assert "warning:" not in out               # no spurious close warning
    # THE POINT: the card's status became archived through the kanban library.
    assert ("hscc", "t_abc") in kb.archived


def test_run_apply_warns_when_archive_fails(monkeypatch, tmp_path, capsys):
    """When archiving genuinely fails, ``--apply`` warns instead of claiming success.

    ``archive_task`` returns False when the row no longer matches (e.g. the card
    is already archived). The command must NOT print "card closed" then — it
    reports a clear warning so the operator knows the card still needs archiving.
    This is the honest half of the fix: no more unconditional success claims.
    """
    from flightdeck.core import kanban as kanban_mod

    kb = _FakeKB([_card()])
    # Pre-mark the card as already archived so _real_close_card's archive_task
    # returns False (the genuine-failure case we must surface, not hide).
    kb.archived.add(("hscc", "t_abc"))
    monkeypatch.setattr(kanban_mod, "_load_kanban_db", lambda: kb, raising=False)
    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit()
    args = _ns(card="t_abc", registry=None, run=fake, apply=True,
               func=review.cmd_review)
    rc = review.run(args, reg)
    captured = capsys.readouterr()
    assert rc == 0                                  # merge succeeded
    assert "merged wt/t_abc into main and pushed" in captured.out
    assert "card t_abc closed" not in captured.out   # NOT claimed on failed archive
    assert "could not be archived" in captured.err   # clear warning on stderr

# F9b additions -- awaiting-review queue + test-quality gate
"""Tests for F9b — the awaiting-review queue and the test-quality gate.

Fixture-driven and injection-only: git facts come from injected git_state
runners (FactsGit-style), verify runs through an injected ``_run``, the
baseline is a tmp_path file, and cards come from a stubbed kanban provider.
No test touches git, the network, a live board, or a real project.

The five mandated behaviours are pinned:

1. the queue lists only unmerged review cards and excludes merged ones
2. ordering is newest-first
3. a slow test is flagged
4. a network-touching test is flagged
5. a clean fast suite produces no flags
"""

from flightdeck.commands import review  # the commands module (dispatch/entry)
import flightdeck.core.review as core_review
from flightdeck.core.registry import Project

SLOW = core_review.SLOW_TEST_SECONDS  # 1.0


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

class FakeRun:
    """A list-command runner for the verify + gate path.

    ``_run(["sh", "-c", <verify>], cwd) -> proc``. Returns canned
    stdout/stderr/returncode.
    """

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.calls = []

    def __call__(self, cmd, cwd):
        self.calls.append((cmd, cwd))
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


class FactsGit:
    """Enriched-card git runner returning branch facts from canned state.

    ``_run([...], repo) -> proc`` — answers the two git_state questions the
    queue needs: branch_exists (rev-parse --verify) and is_merged
    (merge-base --is-ancestor).
    """

    def __init__(self, branch_exists=True, merged=False):
        self.branch_exists = branch_exists
        self.merged = merged
        self.calls = []

    def __call__(self, cmd, repo):
        self.calls.append((cmd, repo))
        sub = cmd[1]
        if sub == "rev-parse":
            return subprocess.CompletedProcess(
                cmd, 0 if self.branch_exists else 1,
                "wt/abc" if self.branch_exists else "", "",
            )
        if sub == "merge-base":
            return subprocess.CompletedProcess(cmd, 0 if self.merged else 1, "", "")
        raise AssertionError("FactsGit does not know this command: %r" % (cmd,))


def _qcard(cid="t_1", status="review", branch="wt/t_1", created_at=None, title="task"):
    return {
        "id": cid, "title": title, "status": status, "assignee": "coder",
        "board": "hscc", "branch": branch, "created_at": created_at,
    }


def _qprojects(*names):
    return [Project(name=n, repo=f"/repo/{n}", board=f"b{n}") for n in names]


NOW = 5000


# --------------------------------------------------------------------------- #
# review_queue — the mandated tests
# --------------------------------------------------------------------------- #

def test_queue_lists_only_unmerged_review_cards():
    """A merged review card is excluded; an unmerged one is listed."""
    cards = [
        dict(_qcard("merged", status="blocked", branch="wt/merged"),
             project="hscc", branch_exists=True, is_merged=True),
        dict(_qcard("open", status="review", branch="wt/open"),
             project="hscc", branch_exists=True, is_merged=False),
    ]
    rows = core_review.review_queue(cards, now=NOW)
    assert [r["card_id"] for r in rows] == ["open"]


def test_queue_excludes_merged_blocked_card():
    """A blocked card whose branch is an ancestor of main is NOT awaiting review."""
    cards = [
        dict(_qcard("m", status="blocked", branch="wt/m"),
             project="hscc", branch_exists=True, is_merged=True),
        dict(_qcard("u", status="review", branch="wt/u"),
             project="hscc", branch_exists=True, is_merged=False),
    ]
    rows = core_review.review_queue(cards, now=NOW)
    assert "m" not in [r["card_id"] for r in rows]
    assert [r["card_id"] for r in rows] == ["u"]


def test_queue_orders_newest_first():
    created = {  # c_old created first (smallest ts), c_new last
        "c_old": 3000, "c_mid": 4000, "c_new": 4500,
    }
    cards = [
        dict(_qcard(cid, status="review", branch=f"wt/{cid}", created_at=ts),
             project="hscc", branch_exists=True, is_merged=False)
        for cid, ts in created.items()
    ]
    rows = core_review.review_queue(cards, now=NOW)
    assert [r["card_id"] for r in rows] == ["c_new", "c_mid", "c_old"]
    # Newest also has the smallest age.
    assert rows[0]["age_seconds"] == NOW - 4500


def test_queue_requires_status_in_review_statuses():
    cards = [
        dict(_qcard("run", status="running", branch="wt/run"),
             project="hscc", branch_exists=True, is_merged=False),
    ]
    assert core_review.review_queue(cards, now=NOW) == []


def test_queue_requires_existing_branch():
    """No branch facts -> conservative: not awaiting review (can't verify)."""
    cards = [
        dict(_qcard("nb", status="review", branch="wt/nb"),
             project="hscc", branch_exists=False, is_merged=False),
    ]
    assert core_review.review_queue(cards, now=NOW) == []


def test_queue_rows_carry_project_card_branch_age():
    cards = [
        dict(_qcard("abc", status="review", branch="wt/abc", created_at=4500),
             project="hscc", branch_exists=True, is_merged=False),
    ]
    rows = core_review.review_queue(cards, now=NOW)
    assert rows[0]["project"] == "hscc"
    assert rows[0]["card_id"] == "abc"
    assert rows[0]["branch"] == "wt/abc"
    assert rows[0]["age_seconds"] == 500


def test_queue_never_drops_card_with_unknown_age():
    """A card lacking created_at is kept (sorted last), never dropped."""
    cards = [
        dict(_qcard("has_ts", status="review", branch="wt/has_ts", created_at=4000),
             project="hscc", branch_exists=True, is_merged=False),
        dict(_qcard("no_ts", status="review", branch="wt/no_ts", created_at=None),
             project="hscc", branch_exists=True, is_merged=False),
    ]
    rows = core_review.review_queue(cards, now=NOW)
    ids = [r["card_id"] for r in rows]
    assert set(ids) == {"has_ts", "no_ts"}
    assert ids[-1] == "no_ts"  # unknown age sorts last, still present


def test_queue_never_drops_any_row_even_empty_git_fields():
    """Every qualifying card row is present in the output list."""
    cards = [
        dict(_qcard(f"c{i}", status="review", branch=f"wt/c{i}", created_at=1000 + i),
             project="hscc", branch_exists=True, is_merged=False)
        for i in range(5)
    ]
    assert len(core_review.review_queue(cards, now=NOW)) == 5


# --------------------------------------------------------------------------- #
# parse_durations / parse_total_seconds
# --------------------------------------------------------------------------- #

def test_parse_durations_extracts_timing_rows():
    out = (
        "2.34s call     tests/test_x.py::test_slow\n"
        "0.05s call     tests/test_x.py::test_fast\n"
        "=========== 10 passed in 3.40s ===========\n"
    )
    timings = core_review.parse_durations(out)
    assert [(t.name, t.duration) for t in timings] == [
        ("tests/test_x.py::test_slow", 2.34),
        ("tests/test_x.py::test_fast", 0.05),
    ]


def test_parse_total_seconds_from_summary():
    assert core_review.parse_total_seconds("6 passed, 2 warnings in 2.34s\n") == 2.34
    assert core_review.parse_total_seconds("=========== 10 passed in 3.40s ===========") == 3.40


def test_parse_total_seconds_none_when_no_summary():
    assert core_review.parse_total_seconds("some random output\n") is None


# --------------------------------------------------------------------------- #
# The gate — the mandated tests
# --------------------------------------------------------------------------- #

def test_a_slow_test_is_flagged():
    flags = core_review.check_test_quality(
        "2.34s call     tests/test_x.py::test_slow\n", total_seconds=3.4
    )
    assert any(f.startswith("SLOW TEST tests/test_x.py::test_slow") for f in flags)


def test_a_network_touching_test_is_flagged():
    out = (
        "tests/test_y.py::test_conn FAILED\n"
        "socket.gaierror: [Errno -2] Name or service not known\n"
    )
    flags = core_review.check_test_quality(out, total_seconds=0.4)
    assert any(f.startswith("NETWORK:") for f in flags)


def test_a_clean_fast_suite_produces_no_flags():
    out = (
        "0.02s call     tests/test_a.py::test_one\n"
        "0.03s call     tests/test_b.py::test_two\n"
        "=========== 2 passed in 0.05s ===========\n"
    )
    flags = core_review.check_test_quality(out, total_seconds=0.05, baseline_seconds=0.10)
    assert flags == []


def test_suite_slower_than_baseline_is_flagged():
    out = "=========== 10 passed in 4.00s ===========\n"
    flags = core_review.check_test_quality(out, total_seconds=4.0, baseline_seconds=2.2)
    assert any(f.startswith("SUITE SLOW") for f in flags)


def test_no_baseline_no_suite_flag():
    """No recorded baseline -> cannot compare -> no SUITE SLOW flag."""
    out = "=========== 10 passed in 4.00s ===========\n"
    flags = core_review.check_test_quality(out, total_seconds=4.0, baseline_seconds=None)
    assert all(not f.startswith("SUITE SLOW") for f in flags)


def test_slow_flag_fires_on_green_suite():
    """The evidence case: a GREEN suite with a slow test is still flagged."""
    out = (
        "10.02s call    tests/test_net.py::test_call\n"
        "=========== 10 passed in 72.4s ===========\n"
    )
    flags = core_review.check_test_quality(out, total_seconds=72.4)
    assert any(f.startswith("SLOW TEST tests/test_net.py::test_call") for f in flags)


# --------------------------------------------------------------------------- #
# run_verify_with_gate — end-to-end gate over an injected runner
# --------------------------------------------------------------------------- #

def _qproject(tmp_path, verify=None, name="svc"):
    return Project(name=name, repo=str(tmp_path), verify=verify)


def test_gate_records_baseline_on_clean_run(tmp_path):
    clean = (
        "0.02s call     tests/test_a.py::test_one\n"
        "=========== 1 passed in 0.02s ===========\n"
    )
    store = core_review.BaselineStore(str(tmp_path / "baseline.yaml"))
    result = core_review.run_verify_with_gate(
        _qproject(tmp_path, verify="pytest"), store, _run=FakeRun(stdout=clean)
    )

    assert result.ok is True
    assert result.returncode == 0
    assert result.total_seconds == 0.02
    assert store.get("svc") == 0.02  # clean run recorded the baseline


def test_gate_uses_recorded_baseline_to_flag_slowness(tmp_path):
    store = core_review.BaselineStore(str(tmp_path / "baseline.yaml"))
    store.set("svc", 2.2)

    slow_out = (
        "3.0s call      tests/test_a.py::test_one\n"
        "=========== 1 passed in 4.00s ===========\n"
    )
    result = core_review.run_verify_with_gate(
        _qproject(tmp_path, verify="pytest"), store, _run=FakeRun(stdout=slow_out)
    )

    assert result.ok is False
    assert any(f.startswith("SLOW TEST") for f in result.flags)
    assert any(f.startswith("SUITE SLOW") for f in result.flags)


def test_gate_does_not_ratchet_baseline_on_flagged_run(tmp_path):
    store = core_review.BaselineStore(str(tmp_path / "baseline.yaml"))
    store.set("svc", 2.2)

    slow_out = "=========== 1 passed in 8.00s ===========\n"
    result = core_review.run_verify_with_gate(
        _qproject(tmp_path, verify="pytest"), store, _run=FakeRun(stdout=slow_out)
    )

    # Flagged run -> baseline untouched (must NOT move up to 8.0).
    assert result.ok is False
    assert store.get("svc") == 2.2


def test_gate_no_verify_command_returns_127(tmp_path):
    store = core_review.BaselineStore(str(tmp_path / "baseline.yaml"))
    result = core_review.run_verify_with_gate(
        _qproject(tmp_path, verify=None), store, _run=FakeRun()
    )
    assert result.returncode == 127


def test_gate_invokes_verify_via_sh_c_in_repo_dir(tmp_path):
    run = FakeRun(stdout="=========== 1 passed in 0.02s ===========\n")
    store = core_review.BaselineStore(str(tmp_path / "baseline.yaml"))

    project = _qproject(tmp_path, verify="cd ~/svc && ./run_tests.sh")
    core_review.run_verify_with_gate(project, store, _run=run)

    assert run.calls[0][0] == ["sh", "-c", "cd ~/svc && ./run_tests.sh"]
    assert run.calls[0][1] == str(tmp_path)


# --------------------------------------------------------------------------- #
# BaselineStore
# --------------------------------------------------------------------------- #

def test_baseline_store_roundtrip(tmp_path):
    store = core_review.BaselineStore(str(tmp_path / "b.yaml"))
    assert store.get("svc") is None
    store.set("svc", 2.2, _now=lambda: 1000)
    # A fresh store (new instance) reads the same file back.
    assert core_review.BaselineStore(str(tmp_path / "b.yaml")).get("svc") == 2.2


def test_baseline_store_missing_file_is_empty(tmp_path):
    store = core_review.BaselineStore(str(tmp_path / "nope.yaml"))
    assert store.get("svc") is None


def test_baseline_store_per_project_isolation(tmp_path):
    store = core_review.BaselineStore(str(tmp_path / "b.yaml"))
    store.set("a", 1.1)
    store.set("b", 2.2)
    assert store.get("a") == 1.1
    assert store.get("b") == 2.2


def test_format_age_renders_units():
    assert review._format_age(None) == "unknown"
    assert review._format_age(59) == "59s"
    assert review._format_age(120) == "2m"
    assert review._format_age(2 * 3600) == "2h"
    assert review._format_age(2 * 86400) == "2d"
    assert review._format_age(70 * 86400) == "2mo"


# --------------------------------------------------------------------------- #
# Command wiring — review is auto-discovered and dispatches correctly
# --------------------------------------------------------------------------- #

def test_review_command_auto_discovered_by_cli():
    from flightdeck.cli import _discover_commands

    assert "review" in _discover_commands()


def test_review_run_wires_seams(tmp_path, monkeypatch, capsys):
    """run() attaches registry, run, now, now_fn, baseline and dispatches."""
    from flightdeck.commands import review as cmd_review
    from flightdeck.core import kanban as kb_mod

    monkeypatch.setattr(kb_mod, "_load_kanban_db", lambda: _FakeKB([_card()]), raising=False)
    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit()

    # Dispatch: a card id -> card flow (F9a preserved end-to-end via run()).
    args = _ns(card="t_abc", registry=None, run=fake, func=review.cmd_dispatch,
               queue=False, baseline=None, now_fn=None)
    rc = review.run(args, reg)
    out = capsys.readouterr().out
    assert rc == 0
    assert "wt/t_abc" in out
    assert "MERGE: clean" in out
    # run() attached the queue/gate seams.
    assert args.now is not None
    assert getattr(args, "now_fn", None) is None
    assert getattr(args, "baseline", None) is None


def test_review_run_no_args_prints_usage(capsys):
    from flightdeck.commands import review as cmd_review

    args = argparse.Namespace(card=None, queue=False, func=cmd_review.cmd_dispatch,
                              run=None, now_fn=None, cards=[])
    rc = cmd_review.run(args, "/tmp/nonexistent.yaml")
    err = capsys.readouterr().err
    assert rc == 2
    assert "review --queue" in err


def test_review_dispatch_unknown_token_is_error(capsys):
    from flightdeck.commands import review as cmd_review

    args = argparse.Namespace(queue=False, card="not-a-card", registry="/tmp/r.yaml",
                              json=False, run=None, now_fn=None, now=NOW, cards=[])
    rc = cmd_review.cmd_dispatch(args)
    err = capsys.readouterr().err
    assert rc == 2
    assert "neither a known card id nor a registry project" in err


def test_review_dispatch_no_token_detects_project_from_cwd(tmp_path, monkeypatch, capsys):
    """`review` with no positional + cwd inside a repo -> that project's gate.

    The review verify+gate form runs with the detected project and prints the
    detection note. The injected FakeRun stands in for the verify runner.
    """
    from flightdeck.commands import review as cmd_review

    proj = registry.Project(name="hscc", repo="~/dev/hscc", board="hscc", verify="pytest")
    monkeypatch.setattr(cmd_review.registry, "load_registry", lambda path: [proj])
    fake = FakeRun(stdout="1 passed in 0.02s\n", returncode=0)
    cwd = registry._expand("~/dev/hscc") + "/sub"
    args = _ns(card=None, registry="/tmp/r.yaml", queue=False, json=False,
               run=fake, baseline=None, now=NOW, now_fn=lambda: NOW, cwd=cwd,
               cards=[])
    rc = cmd_review.cmd_dispatch(args)
    captured = capsys.readouterr()
    assert rc == 0
    assert "using project 'hscc' (detected from cwd)" in captured.err
    assert "test-quality gate: clean" in captured.out


def test_review_dispatch_no_token_no_cwd_match_prints_usage(capsys):
    """No positional + cwd outside every repo -> unchanged usage error."""
    from flightdeck.commands import review as cmd_review

    args = argparse.Namespace(queue=False, card=None, registry="/tmp/nope.yaml",
                              json=False, run=None, baseline=None,
                              now_fn=None, now=NOW, cards=[])
    rc = cmd_review.cmd_dispatch(args)
    err = capsys.readouterr().err
    assert rc == 2
    assert "review --queue" in err
    assert "detected from cwd" not in err



def test_review_dispatch_card_id_routes_to_card_flow(tmp_path, capsys):
    """A positional that matches a card id follows F9a's card review flow."""
    from flightdeck.commands import review as cmd_review

    reg = _write_registry(tmp_path, [_project()])
    fake = FakeGit()
    args = _ns(card="t_abc", registry=reg, run=fake, cards=[_card()],
               func=cmd_review.cmd_dispatch, queue=False, json=False,
               baseline=None, now=NOW, now_fn=None)
    rc = cmd_review.cmd_dispatch(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "MERGE: clean" in out          # card flow rendered, not verify
    assert "no 'verify' command" not in out


def test_review_queue_end_to_end(capsys, monkeypatch, tmp_path):
    """`review --queue` lists only genuine review cards, newest first."""
    from flightdeck.commands import review as cmd_review
    from flightdeck.core import kanban as kb_mod

    projects = [
        Project(name="hscc", repo="/repo/hscc", board="bhscc"),
        Project(name="soc", repo="/repo/soc", board="bsoc"),
    ]
    monkeypatch.setattr(cmd_review.registry, "load_registry", lambda path: projects)

    def fake_kanban_cards(board=None):
        # Return dicts shaped like core.kanban.list_cards output.
        if board == "bhscc":
            return [
                dict(_qcard("c_open", status="review", branch="wt/c_open", created_at=4500)),
                dict(_qcard("c_merged", status="blocked", branch="wt/c_merged", created_at=4000)),
            ]
        if board == "bsoc":
            return [dict(_qcard("c_soc", status="review", branch="wt/c_soc", created_at=4600))]
        return []

    monkeypatch.setattr(cmd_review.kanban, "list_cards", fake_kanban_cards)
    monkeypatch.setattr(  # freshness: date the contributing boards
        cmd_review.kanban, "list_board_watermarks",
        lambda **kw: {"bhscc": 4500, "bsoc": 4600},
    )

    def fake_git_run(cmd, repo):
        sub = cmd[1]
        if sub == "rev-parse":
            merged_branch = cmd[3] == "wt/c_merged"
            return subprocess.CompletedProcess(cmd, 0, "wt/x" if not merged_branch else "", "")
        if sub == "merge-base":
            merged = cmd[3] == "wt/c_merged"
            return subprocess.CompletedProcess(cmd, 0 if merged else 1, "", "")
        raise AssertionError(cmd)

    import argparse

    args = argparse.Namespace(queue=True, card=None, registry="/tmp/r.yaml",
                              baseline="", json=False, now=NOW,
                              run=fake_git_run, now_fn=lambda: NOW)
    rc = cmd_review.cmd_dispatch(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "c_open" in out and "c_soc" in out
    assert "c_merged" not in out                     # merged -> excluded
    assert out.index("c_soc") < out.index("c_open")  # newest first


def test_review_queue_prints_freshness_watermark(capsys, monkeypatch):
    """`review --queue` ends with a `data as of <time>` line dating the
    oldest contributing board — the freshness floor for a stale queue."""
    from flightdeck.commands import review as cmd_review

    projects = [Project(name="hscc", repo="/repo/hscc", board="bhscc")]
    monkeypatch.setattr(cmd_review.registry, "load_registry", lambda path: projects)
    monkeypatch.setattr(
        cmd_review.kanban, "list_cards",
        lambda **kw: [dict(_qcard("c1", status="review", branch="wt/c1", created_at=4500))],
    )
    monkeypatch.setattr(  # the contributing board is fresh (near wall-clock)
        cmd_review.kanban, "list_board_watermarks",
        lambda **kw: {"hscc": int(time.time())},
    )

    import argparse

    args = argparse.Namespace(queue=True, card=None, registry="/tmp/r.yaml",
                              baseline="", json=False, now=NOW,
                              run=lambda *a, **k: subprocess.CompletedProcess(
                                  ["git"], 0, "", ""),
                              now_fn=lambda: NOW)
    rc = cmd_review.cmd_dispatch(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "data as of" in out
    assert "data as of <unknown>" not in out


def test_review_verify_gate_end_to_end(capsys, monkeypatch, tmp_path):
    """`review <project>` runs verify + gate and blocks merge on flags."""
    from flightdeck.commands import review as cmd_review

    monkeypatch.setattr(
        cmd_review.registry, "get_project",
        lambda name, path: Project(name="svc", repo=str(tmp_path),
                                   verify="pytest --durations=0"),
    )
    monkeypatch.setattr(
        cmd_review.review, "run_verify_with_gate",
        lambda *a, **kw: core_review.VerifyResult(
            project="svc", returncode=0, total_seconds=2.5,
            tests=[core_review.TestTiming("tests/test_a.py::test_slow", 2.1)],
            flags=["SLOW TEST tests/test_a.py::test_slow: 2.10s (> 1s)"],
            baseline_seconds=2.2,
        ),
    )

    import argparse

    args = argparse.Namespace(queue=False, card="svc", registry="/tmp/r.yaml",
                              baseline=str(tmp_path / "b.yaml"), json=False,
                              now=NOW, run=None, now_fn=lambda: NOW, cards=[])
    rc = cmd_review.cmd_dispatch(args)
    out = capsys.readouterr().out
    assert rc == 1                              # flagged -> blocks merge
    assert "SLOW TEST" in out
    assert "BEFORE MERGE" in out


def test_review_verify_clean_returns_zero(capsys, monkeypatch, tmp_path):
    """A clean, fast verify run -> rc 0, no flags."""
    from flightdeck.commands import review as cmd_review

    monkeypatch.setattr(
        cmd_review.registry, "get_project",
        lambda name, path: Project(name="svc", repo=str(tmp_path),
                                   verify="pytest --durations=0"),
    )
    monkeypatch.setattr(
        cmd_review.review, "run_verify_with_gate",
        lambda *a, **kw: core_review.VerifyResult(
            project="svc", returncode=0, total_seconds=0.02,
            tests=[core_review.TestTiming("tests/test_a.py::test_one", 0.02)],
            flags=[], baseline_seconds=None,
        ),
    )

    import argparse

    args = argparse.Namespace(queue=False, card="svc", registry="/tmp/r.yaml",
                              baseline=str(tmp_path / "b.yaml"), json=False,
                              now=NOW, run=None, now_fn=lambda: NOW, cards=[])
    rc = cmd_review.cmd_dispatch(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "test-quality gate: clean" in out
