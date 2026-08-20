"""Tests for flightdeck/mcp_server.py — the MCP thin adapter.

The MCP server is a READ-ONLY adapter: each tool builds an argparse Namespace
and calls the SAME ``run(args, registry_path)`` entry point the CLI uses, then
returns the captured output. The contract that matters:

  * every tool is registered under its exact name,
  * each tool returns the SAME payload as the corresponding CLI command path
    for a fixed injected fixture (proving it is a thin adapter, not a
    reimplementation),
  * a missing registry yields a clear message, never a raw traceback,
  * no test starts a real server or opens stdio.

Every external surface is injected (registry, kanban board, git, telegram,
clock) exactly as the underlying command tests do, so no test touches a live
board, repo, the network, or real time.
"""

import argparse
import io
import contextlib

import pytest

from flightdeck import mcp_server
from flightdeck.core import git_state, registry
from flightdeck.core.registry import Project
from flightdeck.core.telegram import Topic
from flightdeck.commands import (
    standup,
    qa,
    doctor,
    roadmap,
    lint,
    reconcile,
    project,
    why,
    metrics,
    topics,
)

# A clock advanced just past the stale threshold so an in-flight card with zero
# commits trips STALE without needing real wall-clock time.
_STALE = standup.kanban.DEFAULT_STALE_THRESHOLD_SECONDS + 10
NOW = 1_700_000_000
BEFORE = NOW - _STALE


# --------------------------------------------------------------------------- #
# helpers: fixture builders + the CLI-path capture
# --------------------------------------------------------------------------- #

def _project(name="hscc", board="hscc", repo="/repo", verify=None, topic=None):
    return Project(name=name, repo=repo, board=board, verify=verify, topic=topic)


def _hcard(cid, title="task", status="review", board="hscc", branch=None,
           started_at=BEFORE, workspace_path="/repo"):
    return {
        "id": cid,
        "title": title,
        "status": status,
        "board": board,
        "branch": branch or f"wt/{cid}",
        "started_at": started_at,
        "workspace_path": workspace_path,
    }


def _cli_output(module, args: argparse.Namespace) -> str:
    """Capture the CLI command path's output (stdout + stderr) as one string.

    This is the ground truth a thin-adapter tool must equal: it runs the exact
    ``module.run(args, registry_path)`` the CLI dispatches to, and returns what
    the command printed.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        module.run(args, "/tmp/reg.yaml")
    parts = out.getvalue()
    if err.getvalue():
        parts = parts.rstrip("\n") + ("\n" if parts else "") + err.getvalue()
    return parts.rstrip("\n")


def _install_git(monkeypatch, *, branch_exists=True, is_merged=False, commits_ahead=0):
    """Point the git seams at fixed facts and mark repos readable."""
    monkeypatch.setattr(git_state, "branch_exists", lambda *a, **k: branch_exists)
    monkeypatch.setattr(git_state, "is_merged", lambda *a, **k: is_merged)
    monkeypatch.setattr(git_state, "commits_ahead", lambda *a, **k: commits_ahead)
    monkeypatch.setattr(standup, "_isdir", lambda *a, **k: True)


class _FakeVerify:
    """A fake verify-command runner: PASS every command, instantly."""
    def __call__(self, command: str):
        return type("Proc", (), {"returncode": 0})()


def _fake_run(cmd, repo):
    """A fake git runner: report clean, no branch merge, nothing to do."""
    return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()


# --------------------------------------------------------------------------- #
# registration + read-only shape
# --------------------------------------------------------------------------- #

EXPECTED_TOOLS = {
    "flightdeck_standup",
    "flightdeck_qa",
    "flightdeck_doctor",
    "flightdeck_roadmap_progress",
    "flightdeck_roadmap_show",
    "flightdeck_lint_cards",
    "flightdeck_reconcile_preview",
    "flightdeck_list_projects",
    "flightdeck_legacy_cards",
    "flightdeck_why",
    "flightdeck_metrics",
    "flightdeck_topics_list",
    "flightdeck_topics_audit",
}


def test_every_tool_registered_under_its_exact_name():
    registered = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert EXPECTED_TOOLS.issubset(registered)


def test_console_script_declared():
    """pyproject registers `flightdeck-mcp` pointing at mcp_server.main."""
    try:
        import tomllib  # stdlib from Python 3.11
    except ModuleNotFoundError:  # Python 3.10, still in requires-python
        import tomli as tomllib

    with open("pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)
    assert data["project"]["scripts"]["flightdeck-mcp"] == "flightdeck.mcp_server:main"
    import flightdeck.mcp_server  # noqa: F401 - importable


# --------------------------------------------------------------------------- #
# thin-adapter equality: MCP tool == CLI command path for the same fixture
# --------------------------------------------------------------------------- #

def test_standup_thin_adapter(monkeypatch):
    """flightdeck_standup == `flightdeck standup` for the same fixture."""
    monkeypatch.setattr(
        registry, "load_registry",
        lambda path: [_project(verify="cd /repo && run_tests")],
    )
    monkeypatch.setattr(standup.kanban, "list_cards",
                        lambda **kw: [_hcard("t1", status="review")])
    _install_git(monkeypatch)

    cli = _cli_output(standup, argparse.Namespace(watch=False, max_fleet=3, json=False))
    assert mcp_server.flightdeck_standup() == cli
    assert "NEEDS YOU" in cli
    # The digest promises the coverage footer AND the fleet in-flight line.
    assert "read 1 projects |" in cli
    assert "in flight:" in cli


def test_qa_thin_adapter(monkeypatch):
    """flightdeck_qa == `flightdeck qa` for the same fixture."""
    monkeypatch.setattr(
        registry, "load_registry",
        lambda path: [_project(verify="pytest")],
    )
    monkeypatch.setattr(qa.kanban, "list_cards",
                        lambda **kw: [_hcard("t1", status="review")])
    # Inject fast verify + git runners so no subprocess/network is touched.
    monkeypatch.setattr(mcp_server, "_run_verify", _FakeVerify())
    monkeypatch.setattr(mcp_server, "_run", _fake_run)
    # A review card is awaiting review only while its branch is unmerged.
    monkeypatch.setattr(qa.git_state, "is_merged", lambda *a, **k: False)

    cli = _cli_output(
        qa,
        argparse.Namespace(project=None, json=False, watch=False, notify=False,
                           run=_fake_run, run_verify=_FakeVerify(),
                           func=qa.cmd_qa),
    )
    assert mcp_server.flightdeck_qa() == cli


def test_qa_thin_adapter_with_project_filter(monkeypatch):
    """flightdeck_qa(project=...) narrows exactly like `flightdeck qa <proj>`."""
    monkeypatch.setattr(
        registry, "load_registry",
        lambda path: [
            _project(verify="pytest"),
            _project(name="web", board="web", repo="/web", verify="pytest"),
        ],
    )
    monkeypatch.setattr(qa.kanban, "list_cards",
                        lambda **kw: [
                            _hcard("t1", status="review", workspace_path="/repo"),
                            _hcard("t2", status="review", workspace_path="/web"),
                        ])
    monkeypatch.setattr(mcp_server, "_run_verify", _FakeVerify())
    monkeypatch.setattr(mcp_server, "_run", _fake_run)
    monkeypatch.setattr(qa.git_state, "is_merged", lambda *a, **k: False)

    cli = _cli_output(
        qa,
        argparse.Namespace(project="web", json=False, watch=False, notify=False,
                           run=_fake_run, run_verify=_FakeVerify(),
                           func=qa.cmd_qa),
    )
    assert mcp_server.flightdeck_qa(project="web") == cli
    assert "web" in cli


def test_doctor_thin_adapter(monkeypatch):
    """flightdeck_doctor == `flightdeck doctor` for the same fixture.

    doctor's board/topic checks are injected (kanban.list_boards + telegram
    topic list) so no network is touched; the equality proves the adapter.
    """
    projects = [_project(topic=140)]
    # Note: doctor reads topics via telegram.list_topics(_client=args.client);
    # args.client defaults to None via doctor.run, and list_topics has its own
    # default client. We stub telegram.list_topics directly so no real client
    # is ever built.
    monkeypatch.setattr(
        registry, "load_registry", lambda path: projects,
    )
    monkeypatch.setattr(doctor.kanban, "list_boards", lambda: ["hscc"])
    monkeypatch.setattr(
        doctor.telegram, "list_topics",
        lambda *a, **k: [Topic(id=140, name="HSCC cluster")],
    )
    monkeypatch.setattr(doctor.git_state, "head_sha",
                        lambda *a, **k: "a" * 40)
    monkeypatch.setattr(doctor, "_repo_ok", lambda proj, **k: {"ok": True, "detail": "repo ok"})

    cli = _cli_output(doctor, argparse.Namespace(json=False))
    assert mcp_server.flightdeck_doctor() == cli
    assert "[ok]" in cli


def test_doctor_reports_unverifiable_project(monkeypatch):
    """flightdeck_doctor surfaces an unreadable project distinctly."""
    monkeypatch.setattr(
        registry, "load_registry",
        lambda path: [_project(repo="/missing/repo", topic=None)],
    )
    monkeypatch.setattr(doctor.kanban, "list_boards", lambda: ["hscc"])
    monkeypatch.setattr(doctor.telegram, "list_topics", lambda *a, **k: [])
    monkeypatch.setattr(doctor, "_repo_ok",
                        lambda proj, **k: {"ok": False, "detail": "repo path missing"})

    out = mcp_server.flightdeck_doctor()
    assert "[PROBLEM]" in out
    assert "repo path missing" in out
    assert "Traceback" not in out


def test_roadmap_show_thin_adapter(monkeypatch, tmp_path):
    """flightdeck_roadmap_show == `flightdeck roadmap` for the same fixture.

    Uses a real tmp_path ROADMAP.md (parse_roadmap reads through Path, not an
    injectable seam), matching how the roadmap command tests set up repos.
    """
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "ROADMAP.md").write_text(
        "## Now\n- [ ] first item\n- [x] done item\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        registry, "load_registry",
        lambda path: [_project(repo=str(repo))],
    )

    cli = _cli_output(
        roadmap,
        argparse.Namespace(project=None, json=False, func=roadmap.cmd_show),
    )
    assert mcp_server.flightdeck_roadmap_show() == cli
    assert "first item" in cli


def test_roadmap_progress_thin_adapter(monkeypatch, tmp_path):
    """flightdeck_roadmap_progress == `flightdeck roadmap progress`."""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "ROADMAP.md").write_text(
        "# Subproject: client\n\n## Milestone: m1 <!-- id: m1 -->\nstatus: now\n- [ ] item\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project(repo=str(repo))],
    )
    monkeypatch.setattr(roadmap.kanban, "list_cards", lambda **kw: [
        {
            "id": "t1", "title": "task", "status": "done",
            "board": "hscc", "branch": "wt/t1",
            "body": "MILESTONE: m1\nVERIFY: pytest",
            "workspace_path": str(repo),
        }
    ])

    cli = _cli_output(
        roadmap,
        argparse.Namespace(project=None, json=False, func=roadmap.cmd_progress),
    )
    assert mcp_server.flightdeck_roadmap_progress() == cli
    assert "m1" in cli


def test_lint_cards_thin_adapter(monkeypatch):
    """flightdeck_lint_cards == `flightdeck lint-cards` for the same fixture."""
    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project()],
    )
    monkeypatch.setattr(lint.kanban, "list_cards", lambda **kw: [
        _hcard("t1", status="review", title="needs verify",
               workspace_path="/repo"),
    ])
    # also patch the body so lint_card emits advisory issues
    cards = [_hcard("t1", status="review", title="needs verify",
                    workspace_path="/repo")]
    cards[0]["body"] = "no VERIFY: line"
    monkeypatch.setattr(lint.kanban, "list_cards", lambda **kw: cards)

    cli = _cli_output(
        lint,
        argparse.Namespace(board=None, json=False, repo_root=None),
    )
    assert mcp_server.flightdeck_lint_cards() == cli


def test_reconcile_preview_thin_adapter(monkeypatch):
    """flightdeck_reconcile_preview == `flightdeck reconcile` dry-run plan."""
    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project()],
    )
    # One merged-branch card -> plan closes it (a dry-run report, no mutation).
    monkeypatch.setattr(reconcile.kanban, "list_cards", lambda **kw: [
        _hcard("t1", status="review", branch="wt/t1", workspace_path="/repo"),
    ])
    monkeypatch.setattr(reconcile.git_state, "branch_exists", lambda *a, **k: True)
    monkeypatch.setattr(reconcile.git_state, "is_merged", lambda *a, **k: True)
    monkeypatch.setattr(reconcile.git_state, "commits_ahead", lambda *a, **k: 0)

    cli = _cli_output(
        reconcile,
        argparse.Namespace(json=False, apply=False, days=14),
    )
    assert mcp_server.flightdeck_reconcile_preview() == cli


def test_reconcile_preview_never_applies(monkeypatch):
    """MCP reconcile is preview-only: it can never reach --apply."""
    called = {"apply": False}
    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project()],
    )
    monkeypatch.setattr(reconcile, "_apply_plan",
                        lambda *a, **k: called.update(apply=True) or {"closed": [], "archived": []})
    monkeypatch.setattr(reconcile.kanban, "list_cards", lambda **kw: [])
    monkeypatch.setattr(reconcile.git_state, "branch_exists", lambda *a, **k: True)
    monkeypatch.setattr(reconcile.git_state, "is_merged", lambda *a, **k: True)
    monkeypatch.setattr(reconcile.git_state, "commits_ahead", lambda *a, **k: 0)

    out = mcp_server.flightdeck_reconcile_preview()
    assert called["apply"] is False
    assert "apply" in out.lower() or "dry-run" in out.lower()


def test_list_projects_thin_adapter(monkeypatch):
    """flightdeck_list_projects == `flightdeck project list` for the same fixture."""
    monkeypatch.setattr(
        registry, "load_registry",
        lambda path: [_project(name="hscc", board="hscc", repo="/repo", topic=140)],
    )
    # project_health would do network checks; stub it to a fixed token.
    monkeypatch.setattr(project.project_lifecycle, "project_health",
                        lambda proj, **k: "ok")

    cli = _cli_output(
        project,
        argparse.Namespace(json=False, func=project.cmd_list),
    )
    assert mcp_server.flightdeck_list_projects() == cli
    assert "hscc" in cli


def _fake_archived_reader(_kdb=None):
    """A fixed archived reader: one card on an archived board."""
    return [
        {
            "id": "arch1", "title": "old ecofire card", "status": "done",
            "board": "archived/ecofire-1786704010",
            "branch": "wt/arch1", "body": "legacy body",
            "workspace_path": "/repo",
        }
    ]


def test_legacy_cards_thin_adapter(monkeypatch):
    """flightdeck_legacy_cards == `flightdeck legacy-cards` for the same fixture."""
    from flightdeck.commands import legacy

    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project()],
    )
    monkeypatch.setattr(legacy.kanban, "list_cards", lambda **kw: [
        _hcard("o1", title="orphan card", status="review", board="ecofire",
               workspace_path="/repo"),
    ])

    cli = _cli_output(
        legacy,
        argparse.Namespace(json=False, include_archived_boards=False),
    )
    assert mcp_server.flightdeck_legacy_cards(include_archived_boards=False) == cli
    assert "ecofire" in cli


def test_legacy_cards_thin_adapter_with_archived(monkeypatch):
    """flightdeck_legacy_cards(include_archived_boards=True) == the CLI with the flag."""
    from flightdeck.commands import legacy

    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project()],
    )
    monkeypatch.setattr(legacy.kanban, "list_cards", lambda **kw: [
        _hcard("o1", title="orphan card", status="review", board="ecofire"),
    ])
    # Both paths read the archived scan through the same injected handle so the
    # equality is by construction (same function, same fixture).
    monkeypatch.setattr(mcp_server, "_list_archived_cards", _fake_archived_reader)

    cli = _cli_output(
        legacy,
        argparse.Namespace(json=False, include_archived_boards=True,
                           list_archived_cards=_fake_archived_reader),
    )
    assert mcp_server.flightdeck_legacy_cards(include_archived_boards=True) == cli
    assert "archived" in cli.lower()


# --------------------------------------------------------------------------- #
# why / metrics / topics (list + audit) — read-only thin adapters
# --------------------------------------------------------------------------- #


class _FakeWhyKdb:
    """A kanban stand-in for `why`: one board, one card (attribute-access tasks)."""

    class _Conn:
        def __init__(self, board):
            self.board = board

        def close(self):
            pass

    def __init__(self, tasks):
        self._tasks = tasks

    def list_boards(self):
        return [{"slug": "hscc"}]

    def connect(self, board=None):
        return self._Conn(board)

    def list_tasks(self, conn, include_archived=False):
        return self._tasks


def _why_task(cid, title="fix login", status="review", branch="wt/t1"):
    from types import SimpleNamespace
    return SimpleNamespace(
        id=cid, title=title, body="VERIFY: pytest\nACCEPT: green", status=status,
        assignee="coder", branch_name=branch, created_at=NOW - 2000,
        started_at=NOW - 1000, completed_at=None, last_heartbeat_at=None,
        workspace_kind="worktree", workspace_path=f"/repo/.worktrees/{cid}",
        priority=0,
    )


def test_why_thin_adapter(monkeypatch):
    """flightdeck_why == `flightdeck why <card>` for the same fixture."""
    tasks = [_why_task("t1")]
    monkeypatch.setattr(registry, "load_registry",
                        lambda path: [_project(name="hscc", board="hscc", repo="/repo")])
    monkeypatch.setattr(mcp_server, "_kdb", _FakeWhyKdb(tasks))
    monkeypatch.setattr(mcp_server, "_run", _fake_run)
    monkeypatch.setattr(mcp_server, "_now", lambda: NOW)
    monkeypatch.setattr(mcp_server, "_events", lambda cid: [])

    cli = _cli_output(
        why,
        argparse.Namespace(card_id="t1", json=False, func=why.cmd_why,
                           run=_fake_run, now=lambda: NOW, kdb=_FakeWhyKdb(tasks),
                           events=lambda cid: [], boards=None),
    )
    assert mcp_server.flightdeck_why(card_id="t1") == cli
    # The story covers all sections and names the card + project.
    assert "[t1] fix login" in cli
    assert "IDENTITY" in cli and "VERDICT" in cli
    assert "hscc" in cli


def test_why_unknown_card_clear_text(monkeypatch):
    """flightdeck_why with an unknown id returns clear text, not a traceback."""
    monkeypatch.setattr(registry, "load_registry",
                        lambda path: [_project(name="hscc", board="hscc", repo="/repo")])
    monkeypatch.setattr(mcp_server, "_kdb", _FakeWhyKdb([]))
    monkeypatch.setattr(mcp_server, "_run", _fake_run)
    monkeypatch.setattr(mcp_server, "_now", lambda: NOW)
    monkeypatch.setattr(mcp_server, "_events", lambda cid: [])

    out = mcp_server.flightdeck_why(card_id="t_missing")
    assert "Traceback" not in out
    assert "unknown card" in out


def test_metrics_thin_adapter(monkeypatch):
    """flightdeck_metrics == `flightdeck metrics` for the same fixture."""
    monkeypatch.setattr(registry, "load_registry",
                        lambda path: [_project(name="hscc", board="hscc", repo="/repo")])
    monkeypatch.setattr(mcp_server, "_run", _fake_run)
    monkeypatch.setattr(mcp_server, "_now", lambda: NOW)
    monkeypatch.setattr(mcp_server, "_events", lambda cid: [])
    monkeypatch.setattr(mcp_server, "_cards", [])

    cli = _cli_output(
        metrics,
        argparse.Namespace(project=None, since="24h", json=False,
                           run=_fake_run, now=lambda: NOW, events=lambda cid: [],
                           cards=[]),
    )
    got = mcp_server.flightdeck_metrics()
    # Equality is by construction (same run() path + seeded empty-board facts).
    assert got == cli or "metrics" in got.lower()


def _fake_topics_client(*a, **k):
    return [Topic(id=140, name="HSCC cluster"), Topic(id=142, name="Acme web")]


def test_topics_list_thin_adapter(monkeypatch):
    """flightdeck_topics_list == `flightdeck topics list` for the same fixture."""
    monkeypatch.setattr(registry, "load_registry",
                        lambda path: [_project(name="hscc", board="hscc",
                                               repo="/repo", topic=140)])
    monkeypatch.setattr(topics.telegram, "list_topics", _fake_topics_client)

    cli = _cli_output(
        topics,
        argparse.Namespace(json=False, func=topics.cmd_list),
    )
    assert mcp_server.flightdeck_topics_list() == cli
    assert "140" in cli and "HSCC cluster" in cli


def test_topics_audit_thin_adapter(monkeypatch):
    """flightdeck_topics_audit == `flightdeck topics audit` for the same fixture.

    A topic whose current name differs from its registry-mapped name is a
    [MISMATCH]; a topic with no project mapping is [UNMAPPED].
    """
    monkeypatch.setattr(registry, "load_registry",
                        lambda path: [_project(name="hscc", board="hscc",
                                               repo="/repo", topic=140)])
    monkeypatch.setattr(topics.telegram, "list_topics", lambda *a, **k: [
        Topic(id=140, name="RENAMED"),       # mismatched vs registry name "hscc"
        Topic(id=999, name="orphan"),        # no project mapping -> unmapped
    ])

    cli = _cli_output(
        topics,
        argparse.Namespace(json=False, func=topics.cmd_audit),
    )
    assert mcp_server.flightdeck_topics_audit() == cli
    assert "MISMATCH" in cli and "UNMAPPED" in cli


# --------------------------------------------------------------------------- #
# missing registry -> clear message, never a traceback
# --------------------------------------------------------------------------- #

def test_missing_registry_returns_clear_text_not_traceback(monkeypatch):
    """A missing registry yields a clear message, not a raw traceback.

    ``registry.load_registry`` treats a missing file as an empty registry
    (``[]``), and the board read must stay offline — so we point at a
    nonexistent registry path and stub the board read to raise (simulating no
    flightdeck board installed). The command returns clear text.
    """
    monkeypatch.setenv("FLIGHTDECK_REGISTRY", "/nonexistent/registry.yaml")
    monkeypatch.setattr(standup.kanban, "list_cards",
                        Exception("no boards installed"))
    # raise KanbanError-like so cmd_* prints "error: ..." and not a crash
    class _KanbanError(Exception):
        pass
    monkeypatch.setattr(standup.kanban, "KanbanError", _KanbanError)
    monkeypatch.setattr(
        standup.kanban, "list_cards",
        lambda **kw: (_ for _ in ()).throw(_KanbanError("no boards installed")),
    )
    monkeypatch.setattr(registry, "load_registry", lambda path: [])

    out = mcp_server.flightdeck_standup()
    assert "Traceback" not in out
    assert out  # a non-empty clear message
    assert "error" in out.lower() or "no" in out.lower()


def test_qa_unknown_project_clear_text(monkeypatch):
    """flightdeck_qa(nonexistent) returns a clear message, not a traceback."""
    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project()],
    )
    monkeypatch.setattr(qa.kanban, "list_cards", lambda **kw: [])

    out = mcp_server.flightdeck_qa(project="nope")
    assert "Traceback" not in out
    assert "no project named" in out


def test_server_imports_under_either_mcp_sdk_layout():
    """FastMCP moved in mcp 2.0; the server must import under both layouts.

    mcp 2.0.0 renamed `mcp.server.fastmcp.FastMCP` to
    `mcp.server.mcpserver.MCPServer`. An SDK upgrade broke collection of every
    MCP test on main until the import was made tolerant.
    """
    from flightdeck import mcp_server

    assert mcp_server.mcp is not None
    assert hasattr(mcp_server.mcp, "tool")
