"""Tests for flightdeck.core.lint + flightdeck.commands.lint.

The lint rules are pure decision logic exercised directly against card dicts
(never the live board). The ``cmd_lint`` presentation layer is driven with a
stubbed card list and an injected line-count resolver, so no test touches the
filesystem, git, the network, or a real kanban DB — fast and deterministic.
"""

import argparse

import pytest

from flightdeck.commands import lint as lint_cmd
from flightdeck.core import kanban, lint


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

GOOD_BODY = """\
Build the lint-cards command.

One command: `flightdeck lint-cards [board]` in flightdeck/commands/lint.py.

- VERIFY: run `pytest -q` against the suite in core/lint.py.
- ACCEPTANCE: lint-card flags each missing element independently; a good
  card passes; it never mutates a card.
"""


def card(cid="t_abc", body=GOOD_BODY):
    """A minimal flightdeck card dict with a body (defaults to a GOOD card)."""
    return {
        "id": cid,
        "title": "some task",
        "status": "todo",
        "assignee": "coder",
        "board": "default",
        "branch": None,
        "body": body,
    }


def _ns(**kw):
    """An argparse.Namespace with sensible defaults for the lint command."""
    defaults = dict(
        board=None, repo_root="/repo", module_line_counts=None,
        module_line_threshold=lint.DEFAULT_MODULE_LINE_THRESHOLD,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# --------------------------------------------------------------------------- #
# core.lint — each missing element flagged INDEPENDENTLY
# --------------------------------------------------------------------------- #


def test_good_card_passes():
    assert lint.lint_card(card()) == []


def test_missing_verify_flagged_alone():
    body = """\
    Do the thing.

    - ACCEPTANCE: it works.
    """
    issues = lint.lint_card(card(body=body))
    assert issues == ["missing VERIFY: line"]


def test_missing_acceptance_flagged_alone():
    body = """\
    Do the thing.

    - VERIFY: python -m pytest -q.
    """
    issues = lint.lint_card(card(body=body))
    assert issues == ["missing acceptance criterion"]


def test_missing_both_flagged_together():
    body = "Just go and do it."
    issues = lint.lint_card(card(body=body))
    assert issues == ["missing VERIFY: line", "missing acceptance criterion"]


# --------------------------------------------------------------------------- #
# acceptance criterion: ACCEPTANCE: marker or TESTS: section
# --------------------------------------------------------------------------- #


def test_acceptance_accept_marker_counts():
    body = "- VERIFY: run suite.\n- ACCEPT: behaviour is correct."
    assert lint.lint_card(card(body=body)) == []


def test_acceptance_tests_section_counts():
    body = "- VERIFY: run suite.\n- TESTS: a good card passes; report-only."
    assert lint.lint_card(card(body=body)) == []


# --------------------------------------------------------------------------- #
# large-module concrete-reference check
# --------------------------------------------------------------------------- #


def test_concrete_refs_required_for_large_referenced_module():
    body = "- VERIFY: pytest.\n- ACCEPTANCE: it works.\n" \
           "Touches flightdeck/core/kanban.py heavily."
    counts = {"flightdeck/core/kanban.py": 600}
    issues = lint.lint_card(card(body=body), module_line_counts=counts)
    assert any("concrete" in i for i in issues)


def test_concrete_refs_satisfy_large_module_requirement():
    body = ("- VERIFY: pytest.\n- ACCEPTANCE: it works.\n"
            "Touches flightdeck/core/kanban.py:205 and kanban.py:classify.")
    counts = {"flightdeck/core/kanban.py": 600}
    assert lint.lint_card(card(body=body), module_line_counts=counts) == []


def test_small_module_does_not_require_concrete_refs():
    body = "- VERIFY: pytest.\n- ACCEPTANCE: it works.\n" \
           "Touches a small module flightdeck/commands/topics.py."
    counts = {"flightdeck/commands/topics.py": 10}
    assert lint.lint_card(card(body=body), module_line_counts=counts) == []


def test_unverified_module_size_not_flagged_large():
    """A referenced module with no known line count is NOT flagged — we never
    call a module large without verifying its size."""
    body = "- VERIFY: pytest.\n- ACCEPTANCE: it works.\n" \
           "Touches flightdeck/core/mystery.py."
    issues = lint.lint_card(card(body=body), module_line_counts={})
    assert issues == []
    assert lint.lint_card(card(body=body), module_line_counts=None) == []


def test_threshold_is_injectable():
    body = ("- VERIFY: pytest.\n- ACCEPTANCE: it works.\n"
            "Touches flightdeck/core/small.py.")
    counts = {"flightdeck/core/small.py": 200}
    # Under the default 500 threshold -> no requirement.
    assert lint.lint_card(card(body=body), module_line_counts=counts) == []
    # With a tighter threshold it becomes large and demands concrete refs.
    issues = lint.lint_card(
        card(body=body), module_line_counts=counts, module_line_threshold=100
    )
    assert any("concrete" in i for i in issues)


def test_referenced_modules_normalises_and_dedupes():
    body = "see ./flightdeck/core/kanban.py and flightdeck/core/kanban.py"
    assert lint.referenced_modules(body) == ["flightdeck/core/kanban.py"]


# --------------------------------------------------------------------------- #
# report-only — never mutates the card
# --------------------------------------------------------------------------- #


def test_lint_card_does_not_mutate(capsys):
    c = card()
    before = dict(c)
    lint.lint_card(c, module_line_counts={"flightdeck/core/kanban.py": 600})
    assert c == before


def test_cmd_lint_does_not_edit_cards(capsys):
    """The command layer reports issues but never writes to any card — even a
    CRITICAL main-tree card is only reported, never moved or re-pointed."""
    bad = _mcard("t_bad", REPO)  # workspace at the repo root -> CRITICAL
    original = dict(bad)
    rc = lint_cmd.cmd_lint(_ns(projects=[_project()]), [bad])
    assert rc == 1
    assert bad == original  # untouched
    assert capsys.readouterr().out  # something was reported


# --------------------------------------------------------------------------- #
# cmd_lint — exit codes and presentation
# --------------------------------------------------------------------------- #


def test_cmd_lint_clean_exits_zero(capsys):
    rc = lint_cmd.cmd_lint(_ns(), [card(cid="c1"), card(cid="c2")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "lint clean" in out


def test_cmd_lint_failure_exits_zero(capsys):
    """Advisory findings (missing VERIFY / acceptance) are reported but are
    NON-FATAL: with no CRITICAL main-tree card, lint-cards exits zero so a
    script gate is not blocked by ordinary quality nits."""
    bad = card(cid="t_x", body="nothing here")
    rc = lint_cmd.cmd_lint(_ns(), [bad])
    out = capsys.readouterr().out
    assert rc == 0
    assert "t_x" in out
    assert "VERIFY" in out
    assert "acceptance" in out


def test_cmd_lint_uses_injected_line_counts(capsys):
    """A card referencing a large module flags without any filesystem access."""
    body = ("- VERIFY: pytest.\n- ACCEPTANCE: works.\n"
            "Touches flightdeck/core/kanban.py.")
    counts = {"flightdeck/core/kanban.py": 600}
    rc = lint_cmd.cmd_lint(
        _ns(module_line_counts=lambda b: counts), [card(cid="t_big", body=body)]
    )
    out = capsys.readouterr().out
    assert rc == 0  # advisory-only: concrete-ref finding is non-fatal
    assert "kanban.py" in out
    assert "concrete" in out


# --------------------------------------------------------------------------- #
# cli wiring — lint-cards is discovered and reachable
# --------------------------------------------------------------------------- #


def test_run_reads_cards_via_kanban_and_lints(monkeypatch, capsys):
    """End-to-end through run(): cards come from a stubbed kanban read."""
    monkeypatch.setattr(kanban, "list_cards", lambda board=None: [card(cid="c1")])
    args = _ns()
    rc = lint_cmd.run(args, "/tmp/reg.yaml")
    out = capsys.readouterr().out
    assert rc == 0
    assert "lint clean" in out


def test_run_surfaces_clean_error_when_board_unreadable(monkeypatch, capsys):
    """A kanban read failure is a clean message + exit 2, never a traceback."""
    def _boom(board=None):
        raise kanban.KanbanError("Hermes agent source not found at ...")

    monkeypatch.setattr(kanban, "list_cards", _boom)
    rc = lint_cmd.run(_ns(), "/tmp/reg.yaml")
    err = capsys.readouterr().err
    assert rc == 2
    assert "error:" in err
    assert "Traceback" not in err


def test_lint_cards_is_discovered_by_cli():
    from flightdeck.cli import build_parser

    args = build_parser().parse_args(["lint-cards", "hscc"])
    assert args.command == "lint-cards"
    assert args.board == "hscc"


# --------------------------------------------------------------------------- #
# CRITICAL main-tree check — a card that would run in a repo's MAIN TREE
# --------------------------------------------------------------------------- #

REPO = "/Users/desac/dev/flightdeck"


def _project(repo=REPO):
    from flightdeck.core import registry

    return registry.Project(name="flightdeck", repo=repo)


def _mcard(cid, workspace, kind="scratch", status="running", title="mt"):
    """A card whose workspace lands under a registered repo."""
    return {
        "id": cid,
        "title": title,
        "status": status,
        "body": GOOD_BODY,  # quality-ok, so any finding is purely the workspace
        "workspace_path": workspace,
        "workspace_kind": kind,
    }


def _lint(cards, projects=None):
    """Run cmd_lint over cards against a project list and return (rc, out)."""
    import io
    import contextlib

    ns = _ns(projects=projects if projects is not None else [_project()])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = lint_cmd.cmd_lint(ns, cards)
    return rc, buf.getvalue()


def test_near_miss_card_at_repo_root_is_critical():
    """The live near-miss: a card whose workspace_path is the repo ROOT is
    flagged CRITICAL, so it can never be dispatched into the main tree."""
    rc, out = _lint([_mcard("t_r1", REPO)])
    assert rc == 1
    assert "CRITICAL" in out
    assert "t_r1" in out
    assert REPO in out


def test_unclaimed_worktree_card_at_repo_root_is_normal():
    """N17 false-positive regression: an UNCLAIMED worktree-kind card at the
    repo root is NORMAL (Hermes rewrites workspace_path to .worktrees/<id> only
    on claim), so it must produce NO finding and no CRITICAL — regardless of
    the pending status (ready/todo/blocked)."""
    for status in ("ready", "todo", "blocked"):
        rc, out = _lint(
            [_mcard(f"t_un_{status}", REPO, kind="worktree", status=status)]
        )
        assert rc == 0, f"{status} at root wrongly CRITICAL"
        assert "CRITICAL" not in out, f"{status} at root produced a finding"


def test_running_worktree_card_at_repo_root_is_critical():
    """A worktree-kind card at the repo root WITH a worker attached
    (status running) really would run in the main tree — CRITICAL."""
    rc, out = _lint([_mcard("t_run", REPO, kind="worktree", status="running")])
    assert rc == 1
    assert "CRITICAL" in out
    assert "t_run" in out


def test_running_worktree_card_under_worktrees_is_fine():
    """A worktree-kind card UNDER <repo>/.worktrees/<id> passes cleanly even
    when running — the normal claimed state."""
    rc, out = _lint(
        [_mcard("t_wok2", f"{REPO}/.worktrees/t_wok2", kind="worktree", status="running")]
    )
    assert rc == 0
    assert "CRITICAL" not in out


def test_repo_kind_at_repo_root_is_critical_even_when_unclaimed():
    """workspace_kind 'repo' at the repo root really would run in place, so it
    stays CRITICAL at ANY status — including an unclaimed (ready) card."""
    rc, out = _lint([_mcard("t_repo", REPO, kind="repo", status="ready")])
    assert rc == 1
    assert "CRITICAL" in out
    assert "t_repo" in out


def test_lint_cards_exits_zero_when_only_unclaimed_at_root():
    """When EVERY card is a normal unclaimed worktree-kind card at the repo
    root, lint-cards exits ZERO and prints no CRITICAL — the guard stays silent
    in the normal case."""
    cards = [
        _mcard("t_a", REPO, kind="worktree", status="ready"),
        _mcard("t_b", REPO, kind="worktree", status="todo"),
        _mcard("t_c", REPO, kind="worktree", status="blocked"),
    ]
    rc, out = _lint(cards)
    assert rc == 0
    assert "CRITICAL" not in out


def test_card_under_worktrees_is_fine():
    """A normal worktree card under <repo>/.worktrees/<id> passes cleanly."""
    rc, out = _lint([_mcard("t_ok", f"{REPO}/.worktrees/t_ok", kind="worktree")])
    assert rc == 0
    assert "CRITICAL" not in out


def test_tilde_and_trailing_slash_variants_of_repo_root_caught(monkeypatch, tmp_path):
    """The repo root reached via ~ expansion, trailing slash, and symlink all
    normalise to the same canonical path and are all flagged CRITICAL."""
    import os as _os
    # Pin ~ expansion to a tmp home so the tilde variant resolves to the same
    # canonical repo root regardless of the host's real $HOME (a fake HOME in
    # CI must not shift where ~ points). The repo root is derived from that
    # same pinned home so every variant collapses onto one canonical path.
    home = tmp_path / "home"
    monkeypatch.setattr(
        _os.path, "expanduser",
        lambda p: str(home) + p[1:] if p.startswith("~") else p,
    )
    repo = f"{home}/dev/flightdeck"
    variants = ["~/dev/flightdeck", f"{repo}/", f"{repo}//", "~/dev/flightdeck/"]
    for i, workspace in enumerate(variants):
        rc, out = _lint(
            [_mcard(f"t_v{i}", workspace)],
            projects=[_project(repo=repo)],
        )
        assert rc == 1, f"variant {workspace!r} not flagged"
        assert "CRITICAL" in out


def test_symlink_variant_of_repo_root_caught(monkeypatch, tmp_path):
    """A symlinked checkout collapsing onto the repo root is flagged CRITICAL.

    ``_resolve_path`` calls os.path.realpath, which follows the symlink to the
    canonical repo root — so even a symlink whose name is not the repo root is
    caught. A symlink to the repo ROOT collapses to the root itself.
    """
    link = tmp_path / "link" / "to" / "repo"  # symlink dir collapsed by realpath

    import os as _os

    # Monkeypatch os.path.realpath to collapse the symlink onto the repo root,
    # exactly as realpath collapses a symlinked checkout onto its target.
    real = _os.path.realpath

    def fake_realpath(p):
        if p.endswith("/link/to/repo"):
            return REPO
        return real(p)

    monkeypatch.setattr(_os.path, "realpath", fake_realpath)
    rc, out = _lint([_mcard("t_sym", str(link))])
    assert rc == 1
    assert "CRITICAL" in out


def test_worktree_kind_with_non_worktree_path_caught():
    """workspace_kind=worktree but the path is NOT under <repo>/.worktrees/ is
    the same defect and is flagged CRITICAL."""
    # A worktree-kind card pointing at a subdirectory of the repo (not .worktrees)
    bad = _mcard("t_w2", f"{REPO}/subdir", kind="worktree")
    rc, out = _lint([bad])
    assert rc == 1
    assert "CRITICAL" in out
    assert f"{REPO}/subdir" in out
    # ... and pointing at the repo root itself, again flagged.
    rc2, out2 = _lint([_mcard("t_w1", REPO, kind="worktree")])
    assert rc2 == 1
    assert "CRITICAL" in out2


def test_worktree_kind_at_worktree_path_is_fine():
    """A proper worktree-kind card under <repo>/.worktrees/<id> is not flagged."""
    rc, out = _lint([_mcard("t_wok", f"{REPO}/.worktrees/t_wok", kind="worktree")])
    assert rc == 0
    assert "CRITICAL" not in out


def test_scratch_kind_under_repo_subdir_is_not_main_tree():
    """A scratch-kind card pointing under the repo is unusual but not the
    main-tree defect; only workspace==repo or worktree-kind-not-under-worktrees
    is flagged."""
    rc, out = _lint([_mcard("t_s", f"{REPO}/somewhere", kind="scratch")])
    assert rc == 0
    assert "CRITICAL" not in out


def test_archived_card_at_repo_root_is_not_critical():
    """Archived cards are settled — the repo-root defect only blocks a pending
    or running card that might actually be dispatched."""
    rc, out = _lint([_mcard("t_arc", REPO, status="archived")])
    assert rc == 0
    assert "CRITICAL" not in out


def test_card_unattached_to_any_project_not_critical():
    """A workspace_path that matches no registered repo is not the main-tree
    defect."""
    rc, out = _lint(
        [_mcard("t_other", "/elsewhere/not-a-repo")],
        projects=[_project(repo=REPO)],
    )
    assert rc == 0
    assert "CRITICAL" not in out


def test_critical_and_advisory_together_exit_nonzero(capsys):
    """A card that is both quality-poor AND at the repo root exits non-zero —
    the CRITICAL finding drives the gate."""
    import io
    import contextlib

    bad = {
        "id": "t_crit",
        "title": "mt",
        "status": "running",
        "body": "no markers",
        "workspace_path": REPO,
        "workspace_kind": "scratch",
    }
    ns = _ns(projects=[_project()])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = lint_cmd.cmd_lint(ns, [bad])
    out = buf.getvalue()
    assert rc == 1
    assert "CRITICAL" in out
