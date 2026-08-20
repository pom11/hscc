"""Tests for the MCP mutating tools (MCP2) — the apply-gate contract.

The read-only MCP server (MCP1) is extended with seven mutating-style tools
that let an MCP client drive flightdeck's state-changing commands. The rules
that ARE the product:

  * ``apply`` DEFAULTS TO FALSE on every tool. A tool called with no arguments
    must never mutate anything.
  * When ``apply=False`` the tool returns the plan and states plainly that
    nothing was changed.
  * Each mutating tool's docstring says, in its first sentence, that it mutates
    only when ``apply=True``.
  * ``apply=True`` reaches the SAME command function the CLI uses (thin
    adapter, not a reimplementation).
  * ``flightdeck_message_send`` is the ONE exception: it posts immediately,
    exactly like the CLI, and has no apply gate.

Every external surface is injected (registry, kanban board, git, telegram,
clock) exactly as the underlying command tests do, so no test touches a live
board, repo, the network, or real time, and no test starts a real server.
"""

import argparse
import inspect
import subprocess

import pytest

from flightdeck import mcp_server
from flightdeck.core import registry
from flightdeck.commands import (
    release,
    review,
    reconcile,
    decompose,
    start,
    ingest,
    roadmap,
    message,
    legacy,
    report,
    incident,
    hygiene,
)

MUTATING_TOOLS = [
    "flightdeck_review",
    "flightdeck_reconcile",
    "flightdeck_decompose",
    "flightdeck_start",
    "flightdeck_ingest",
    "flightdeck_roadmap_adopt",
    "flightdeck_message_send",
    "flightdeck_migrate_card",
    "flightdeck_message_dispatch",
    "flightdeck_report",
    "flightdeck_incident",
    "flightdeck_hygiene",
    "flightdeck_release",
]

# The apply-gated subset (message_send is the one immediate-by-design tool).
APPLY_GATED_TOOLS = set(MUTATING_TOOLS) - {"flightdeck_message_send"}


def _project(name="hscc", board="hscc", repo="/repo", verify=None, topic=None):
    return registry.Project(name=name, repo=repo, board=board, verify=verify, topic=topic)


def _hcard(cid, title="task", status="review", board="hscc", branch=None,
           workspace_path="/repo", body="VERIFY: pytest"):
    return {
        "id": cid,
        "title": title,
        "status": status,
        "board": board,
        "branch": branch or f"wt/{cid}",
        "workspace_path": workspace_path,
        "body": body,
    }


class _RecordingGit:
    """A run seam that records every git command; nothing mutates unless told."""

    def __init__(self, *, exists=True, landed=False, subject="the thing",
                 numstat="1\t1\tfile.py", conflicts=0):
        self.exists = exists
        self.landed = landed
        self.subject = subject
        self.numstat = numstat
        self.conflicts = conflicts
        self.calls: list[list[str]] = []

    def _proc(self, cmd, rc, stdout=""):
        return subprocess.CompletedProcess(cmd, rc, stdout, "")

    def __call__(self, cmd, repo):
        self.calls.append(cmd)
        sub = cmd[1]
        if sub == "rev-parse":
            return self._proc(cmd, 0 if self.exists else 1, self.subject if self.exists else "")
        if sub == "merge-base":
            return self._proc(cmd, 0 if self.landed else 1)
        if sub == "log":
            return self._proc(cmd, 0, self.subject)
        if sub == "diff":
            return self._proc(cmd, 0, self.numstat)
        if sub == "merge-tree":
            return self._proc(cmd, 0, "tree\n")
        if sub in ("checkout", "merge", "push"):
            return self._proc(cmd, 0)
        raise AssertionError(f"_RecordingGit does not know: {cmd}")


class _RecordingKdb:
    """A recording hermes kanban_db stand-in: connect/create/complete/archive/assign/comment."""

    def __init__(self, new_id="t_new"):
        self.new_id = new_id
        self.writes: list[tuple[str, str]] = []  # (board, op)

    class _Conn:
        def __init__(self, board=None):
            self.board = board

        def close(self):
            pass

    def connect(self, board):
        return self._Conn(board)

    def create_task(self, conn, **kwargs):
        self.writes.append(("create", self.new_id))
        return self.new_id

    def add_comment(self, conn, task_id, author, body):
        self.writes.append(("comment", task_id))
        return 1

    def complete_task(self, conn, cid, result=""):
        self.writes.append(("complete", cid))
        return True

    def archive_task(self, conn, cid):
        self.writes.append(("archive", cid))
        return True

    def assign_task(self, conn, cid, assignee):
        self.writes.append(("assign", cid))
        return True


# --------------------------------------------------------------------------- #
# REGISTRATION + console script
# --------------------------------------------------------------------------- #


def test_every_mutating_tool_registered():
    registered = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert set(MUTATING_TOOLS).issubset(registered)


def test_console_script_declared():
    """pyproject registers `flightdeck-mcp` pointing at mcp_server.main."""
    try:
        import tomllib  # stdlib from Python 3.11
    except ModuleNotFoundError:  # Python 3.10, still in requires-python
        import tomli as tomllib

    with open("pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)
    assert data["project"]["scripts"]["flightdeck-mcp"] == "flightdeck.mcp_server:main"


# --------------------------------------------------------------------------- #
# DOCSTRING GATE — the model reads the docstring to decide whether to apply
# --------------------------------------------------------------------------- #


def test_each_apply_gated_tool_docstring_first_sentence_mentions_apply_gate():
    """Every apply-gated tool's docstring first sentence names the apply=True gate.

    The model reads the docstring to decide whether calling with apply=True is
    safe — so the very first sentence must say the tool mutates only then.
    """
    for name in APPLY_GATED_TOOLS:
        func = getattr(mcp_server, name)
        doc = inspect.getdoc(func) or ""
        assert doc, f"{name} has no docstring"
        first_sentence = doc.split(".")[0].lower()
        assert "apply=true" in first_sentence, (
            f"{name} first sentence does not mention the apply=True gate: {doc!r}"
        )


def test_message_send_docstring_says_immediate_no_gate():
    """message_send is the one exception: it says it posts immediately, no gate."""
    doc = (inspect.getdoc(mcp_server.flightdeck_message_send) or "").lower()
    assert "immediately" in doc or "immediate" in doc


# --------------------------------------------------------------------------- #
# review
# --------------------------------------------------------------------------- #


def _install_review_seams(monkeypatch, *, cards, git, close_card=None):
    monkeypatch.setattr(registry, "load_registry",
                        lambda path: [_project(name="hscc", board="hscc", repo="/repo")])
    monkeypatch.setattr(mcp_server, "_cards", cards)
    monkeypatch.setattr(mcp_server, "_run", git)
    if close_card is not None:
        monkeypatch.setattr(mcp_server, "_close_card", close_card)


def test_review_defaults_apply_false_and_mutates_nothing(monkeypatch):
    """flightdeck_review(card=...) with defaults never merges or closes."""
    git = _RecordingGit()
    closed = []
    _install_review_seams(monkeypatch, cards=[_hcard("t1")], git=git,
                          close_card=lambda c, b: closed.append(c))

    out = mcp_server.flightdeck_review(card="t1")

    assert "APPLY GATE: apply=False" in out and "nothing was changed" in out
    # The plan is returned, not an empty string.
    assert "card: t1" in out and "dry-run" in out
    # No mutation reached the git or close seams.
    no_write = [c for c in git.calls if c[1] in ("checkout", "merge", "push")]
    assert no_write == [], f"review dry-run issued write git commands: {no_write}"
    assert closed == []


def test_review_apply_reaches_cli_command(monkeypatch):
    """flightdeck_review(apply=True) merges + closes via the SAME cmd the CLI uses."""
    git = _RecordingGit()
    closed = []
    _install_review_seams(monkeypatch, cards=[_hcard("t1")], git=git,
                          close_card=lambda c, b: closed.append(c))

    out = mcp_server.flightdeck_review(card="t1", apply=True)

    assert closed == ["t1"], "apply=True must close the card"
    mutating = [c for c in git.calls if c[1] in ("checkout", "merge", "push")]
    assert mutating, "apply=True must reach _do_apply (git merge+push) — the CLI path"
    assert "merged" in out


def test_review_failure_is_clear_text(monkeypatch):
    """A bad card id returns clear text, never a traceback."""
    _install_review_seams(monkeypatch, cards=[], git=_RecordingGit())
    out = mcp_server.flightdeck_review(card="t_missing")
    assert "Traceback" not in out
    assert out  # non-empty


# --------------------------------------------------------------------------- #
# reconcile
# --------------------------------------------------------------------------- #


def _install_reconcile_seams(monkeypatch, *, cards, plan, kdb):
    monkeypatch.setattr(registry, "load_registry",
                        lambda path: [_project(name="hscc", board="hscc", repo="/repo")])
    monkeypatch.setattr(reconcile.kanban, "list_cards", lambda **kw: cards)
    monkeypatch.setattr(reconcile.kanban, "reconcile_plan",
                        lambda *a, **k: plan)
    monkeypatch.setattr(mcp_server, "_kdb", kdb)


def test_reconcile_defaults_apply_false_and_mutates_nothing(monkeypatch):
    """flightdeck_reconcile() with defaults performs no closes."""
    kdb = _RecordingKdb()
    _install_reconcile_seams(
        monkeypatch,
        cards=[_hcard("t1", status="review")],
        plan={"close": ["t1"], "archive": [], "stale": []},
        kdb=kdb,
    )

    out = mcp_server.flightdeck_reconcile()

    assert "APPLY GATE: apply=False" in out and "nothing was changed" in out
    assert "dry-run" in out
    assert kdb.writes == [], "reconcile dry-run must not write via kdb"


def test_reconcile_apply_reaches_cli_command(monkeypatch):
    """flightdeck_reconcile(apply=True) closes via the same cmd the CLI uses."""
    kdb = _RecordingKdb()
    _install_reconcile_seams(
        monkeypatch,
        cards=[_hcard("t1")],
        plan={"close": ["t1"], "archive": [], "stale": []},
        kdb=kdb,
    )

    out = mcp_server.flightdeck_reconcile(apply=True)

    assert ("complete", "t1") in kdb.writes
    assert "applied" in out.lower()


# --------------------------------------------------------------------------- #
# release — apply-gated bump→commit→tag→push→gh→install→verify
# --------------------------------------------------------------------------- #


class _ReleaseProc:
    """Minimal subprocess.CompletedProcess-like result for the release _run."""

    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _install_release_seams(monkeypatch, tmp_path, *, runner=None):
    """Wire the release seams: a real tmp repo + a stubbed git/verify runner.

    release.preconditions reads VERSION and CHANGELOG.md off disk (so a real
    tmp_path repo is required) and gates every git read through the injected
    ``_run`` seam (never real git). The runner fails closed: any command the
    test did not wire up returns 128 so a surprise mutation surfaces loudly.
    """
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "VERSION").write_text("1.8.1\n", encoding="utf-8")
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [1.8.1] — previous\n\n### Fixed\n- stuff\n\n"
        "## [1.9.0] — upcoming\n\n### Changed\n- planned\n",
        encoding="utf-8",
    )
    proj = _project(name="hscc", board="hscc", repo=str(repo), verify="true")
    monkeypatch.setattr(registry, "load_registry", lambda path: [proj])
    if runner is None:
        runner = _release_runner()
    monkeypatch.setattr(mcp_server, "_run", runner)
    return runner


def _release_runner(*, branch="main", dirty=False, verify_rc=0):
    """A fail-closed runner answering the git reads release's preconditions issue."""
    return _RecordingReleaseRunner(branch=branch, dirty=dirty, verify_rc=verify_rc)


class _RecordingReleaseRunner:
    """A callable recording every command; fails closed on anything unwired.

    Answers the git reads release.preconditions issues (status, branch) and
    the project's verify command (``true``). Any other command — e.g. a
    surprise mutation — returns 128 so it surfaces as a loud failure.
    """

    def __init__(self, *, branch="main", dirty=False, verify_rc=0):
        self.branch = branch
        self.dirty = dirty
        self.verify_rc = verify_rc
        self.calls: list[tuple[str, str]] = []

    def __call__(self, cmd, cwd):
        self.calls.append((cmd, cwd))
        if cmd == "git status --porcelain":
            return _ReleaseProc(0, "m\n" if self.dirty else "", "")
        if cmd == "git rev-parse --abbrev-ref HEAD":
            return _ReleaseProc(0, self.branch + "\n", "")
        if cmd == "true":  # the verify command for the default project
            return _ReleaseProc(self.verify_rc)
        # the version-bump write + the executor's mutation commands (execute())
        if "VERSION" in cmd and ">" in cmd:
            return _ReleaseProc(0)
        if (cmd.startswith("git add ")
                or cmd.startswith("git tag ")
                or cmd.startswith("git push ")
                or cmd.startswith("gh release ")):
            return _ReleaseProc(0)
        return _ReleaseProc(128, "", f"unexpected command: {cmd!r}")


def test_release_present_in_exposed_tools():
    """flightdeck_release must be exposed as an MCP tool (fails if removed)."""
    registered = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert "flightdeck_release" in registered


def test_release_defaults_apply_false_and_mutates_nothing(monkeypatch, tmp_path):
    """flightdeck_release(...) with defaults prints the dry-run plan, no release."""
    run = _install_release_seams(monkeypatch, tmp_path)

    out = mcp_server.flightdeck_release(project="hscc", version="1.9.0")

    assert "APPLY GATE: apply=False" in out and "nothing was changed" in out
    assert "release plan for hscc 1.9.0" in out
    # No mutating command may have reached the runner (all reads only).
    mutating = [c for c, _ in run.calls
                if c.startswith(("git add", "git tag", "git push", "gh release"))]
    assert mutating == [], f"release dry-run issued mutating commands: {mutating}"


def test_release_apply_reaches_cli_command(monkeypatch, tmp_path):
    """flightdeck_release(apply=True) threads through _run_gated to release.run."""
    run = _install_release_seams(monkeypatch, tmp_path)

    out = mcp_server.flightdeck_release(project="hscc", version="1.9.0",
                                        apply=True)

    # apply=True must NOT append the apply-gate footer (release.run is reached).
    assert "APPLY GATE:" not in out
    # The full executor's mutation commands were issued, ending in the verify.
    mutating = [c for c, _ in run.calls if c.startswith("git add")]
    assert mutating, "apply=True must reach release.execute (the CLI release path)"


# --------------------------------------------------------------------------- #
# decompose
# --------------------------------------------------------------------------- #


def _install_decompose_seams(monkeypatch, *, cards, create_rec):
    """Wire the decompose seams so no network/board/proposal work is real.

    ``parse_proposal`` and ``gate_card`` are patched to accept the injected
    ``cards`` so the dry-run and apply branches are reached deterministically.
    ``create_rec`` records ``kanban.create_task`` calls (a list we append to).
    """
    monkeypatch.setattr(registry, "load_registry",
                        lambda path: [_project(name="hscc", board="hscc",
                                               repo="/repo", topic=140)])
    monkeypatch.setattr(mcp_server, "_ask", lambda *a, **k: '{"raw": true}')
    monkeypatch.setattr(decompose, "parse_proposal", lambda raw: cards)
    monkeypatch.setattr(decompose, "gate_card", lambda *a, **k: [])
    # The decompose prompt renderer auto-fills the project's open cards from
    # the live board (gather_context -> kanban.list_cards). Under a fake HOME
    # that read fails, so stub it to an empty board — the card CREATION path we
    # are asserting on is the seam above, not the prompt's card summary.
    monkeypatch.setattr(decompose.kanban, "list_cards", lambda **kw: [])
    monkeypatch.setattr(decompose.kanban, "create_task",
                        lambda board, title, assignee=None, body=None, **k:
                        create_rec.append((board, title)) or "t_new")


def test_decompose_defaults_apply_false_and_mutates_nothing(monkeypatch):
    """flightdeck_decompose(...) with defaults creates no cards."""
    created = []
    _install_decompose_seams(monkeypatch, cards=[
        decompose.ProposedCard(id=1, title="Card A", body="b", concern="c",
                               verify="pytest", acceptance="passes",
                               references=["flightdeck/commands/review.py:1"]),
    ], create_rec=created)

    out = mcp_server.flightdeck_decompose(project="hscc", goal="build a thing")

    assert "APPLY GATE: apply=False" in out and "nothing was changed" in out
    assert created == [], "decompose dry-run must not create cards"


def test_decompose_apply_reaches_cli_command(monkeypatch):
    """flightdeck_decompose(apply=True) creates cards via the same cmd the CLI uses."""
    created = []
    _install_decompose_seams(monkeypatch, cards=[
        decompose.ProposedCard(id=1, title="Card A", body="b", concern="c",
                               verify="pytest", acceptance="passes",
                               references=["flightdeck/commands/review.py:1"]),
    ], create_rec=created)

    out = mcp_server.flightdeck_decompose(project="hscc", goal="build a thing",
                                          apply=True)

    assert created, "apply=True must reach kanban.create_task (the CLI card path)"
    assert "created" in out.lower()


# --------------------------------------------------------------------------- #
# start
# --------------------------------------------------------------------------- #


def _install_start_seams(monkeypatch, *, cards, kdb):
    monkeypatch.setattr(registry, "load_registry",
                        lambda path: [_project(name="hscc", board="hscc", repo="/repo")])
    monkeypatch.setattr(start.kanban, "list_cards", lambda **kw: cards)
    monkeypatch.setattr(start, "_read_config", lambda path=None: {
        "max_in_progress": 10, "max_in_progress_per_profile": 3,
    })
    monkeypatch.setattr(mcp_server, "_kdb", kdb)
    monkeypatch.setattr(mcp_server, "_run", _RecordingGit(landed=True))


def test_start_defaults_apply_false_and_mutates_nothing(monkeypatch):
    """flightdeck_start(...) with defaults releases no cards."""
    kdb = _RecordingKdb()
    _install_start_seams(monkeypatch, cards=[_hcard("t1", body="MILESTONE: m1\nVERIFY: pytest")], kdb=kdb)

    out = mcp_server.flightdeck_start(project="hscc", milestone="m1")

    assert "APPLY GATE: apply=False" in out and "nothing was changed" in out
    assert "RELEASE PLAN" in out or "dry-run" in out
    assert kdb.writes == [], "start dry-run must not assign any card"


def test_start_apply_reaches_cli_command(monkeypatch):
    """flightdeck_start(apply=True) assigns cards via the same cmd the CLI uses."""
    kdb = _RecordingKdb()
    _install_start_seams(monkeypatch, cards=[_hcard("t1", body="MILESTONE: m1\nVERIFY: pytest")], kdb=kdb)

    out = mcp_server.flightdeck_start(project="hscc", milestone="m1", apply=True)

    assert ("assign", "t1") in kdb.writes
    assert "applied" in out.lower()


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #


def _install_ingest_seams(monkeypatch, tmp_path, *, create_rec):
    """Wire the ingest seams so no network/board/repo work is real.

    The three context gatherers are patched to fixed (content, report) pairs
    (so ``any_content`` is true and nothing reaches Telegram/git), the context
    staging dir is a tmp_path, and ``kanban.create_task`` records calls.
    """
    monkeypatch.setattr(registry, "load_registry",
                        lambda path: [_project(name="hscc", board="hscc",
                                               repo="/repo", topic=140)])
    monkeypatch.setattr(ingest, "_gather_repo", lambda proj, **k: ("repo stuff", "ok"))
    monkeypatch.setattr(ingest, "_gather_topic", lambda proj, limit, client=None: ("topic stuff", "ok"))
    monkeypatch.setattr(mcp_server, "_read_refs",
                        lambda proj: ("ref stuff", "ok"))
    monkeypatch.setattr(mcp_server, "_context_dir", str(tmp_path / "ctx"))
    monkeypatch.setattr(ingest.kanban, "create_task",
                        lambda board, title, body=None, _kdb=None, **k:
                        create_rec.append((board, title)) or "t_ingest")


def test_ingest_defaults_apply_false_and_mutates_nothing(monkeypatch, tmp_path):
    """flightdeck_ingest(...) with defaults stages context but creates no card."""
    created = []
    _install_ingest_seams(monkeypatch, tmp_path, create_rec=created)

    out = mcp_server.flightdeck_ingest(project="hscc")

    assert "APPLY GATE: apply=False" in out and "nothing was changed" in out
    assert created == [], "ingest dry-run must not dispatch a synthesis card"


def test_ingest_apply_reaches_cli_command(monkeypatch, tmp_path):
    """flightdeck_ingest(apply=True) dispatches the card via the same cmd the CLI uses."""
    created = []
    _install_ingest_seams(monkeypatch, tmp_path, create_rec=created)

    out = mcp_server.flightdeck_ingest(project="hscc", apply=True)

    assert created, "apply=True must reach kanban.create_task (the CLI card path)"
    assert "created ingest card" in out.lower()


# --------------------------------------------------------------------------- #
# roadmap_adopt
# --------------------------------------------------------------------------- #


def _install_adopt_seams(monkeypatch, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    draft = repo / "docs" / "ROADMAP.draft.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("## Now\n- [ ] item\n", encoding="utf-8")
    monkeypatch.setattr(registry, "load_registry",
                        lambda path: [_project(name="hscc", board="hscc",
                                               repo=str(repo), topic=140)])
    return repo


def test_roadmap_adopt_defaults_apply_false_and_mutates_nothing(monkeypatch, tmp_path):
    """flightdeck_roadmap_adopt(...) with defaults promotes nothing."""
    repo = _install_adopt_seams(monkeypatch, tmp_path)

    out = mcp_server.flightdeck_roadmap_adopt(project="hscc")

    assert "APPLY GATE: apply=False" in out and "nothing was changed" in out
    assert "dry-run" in out
    # No ROADMAP.md created and no .bak backup produced.
    assert not (repo / "ROADMAP.md").exists()
    assert not (repo / "ROADMAP.md.bak").exists()


def test_roadmap_adopt_apply_reaches_cli_command(monkeypatch, tmp_path):
    """flightdeck_roadmap_adopt(apply=True) promotes via the same cmd the CLI uses."""
    repo = _install_adopt_seams(monkeypatch, tmp_path)

    out = mcp_server.flightdeck_roadmap_adopt(project="hscc", apply=True)

    assert (repo / "ROADMAP.md").exists()
    assert "adopted" in out


# --------------------------------------------------------------------------- #
# message_send — immediate by design, no apply gate
# --------------------------------------------------------------------------- #


def test_message_send_posts_immediately(monkeypatch):
    """flightdeck_message_send posts right away — no apply gate, like the CLI."""
    sent = []
    monkeypatch.setattr(registry, "load_registry",
                        lambda path: [_project(name="hscc", board="hscc",
                                               repo="/repo", topic=140)])
    monkeypatch.setattr(message.telegram, "send_message",
                        lambda tid, text, _client=None: sent.append((tid, text)))

    out = mcp_server.flightdeck_message_send(project="hscc", text="hello")

    assert sent == [(140, "hello")]
    assert "sent to hscc" in out


def test_message_send_unknown_project_clear_text(monkeypatch):
    """flightdeck_message_send to an unknown project returns clear text."""
    monkeypatch.setattr(registry, "load_registry",
                        lambda path: [_project(name="hscc", board="hscc",
                                               repo="/repo", topic=140)])
    out = mcp_server.flightdeck_message_send(project="nope", text="hi")
    assert "Traceback" not in out
    assert out


# --------------------------------------------------------------------------- #
# message_dispatch — apply-gated create-card + announce
# --------------------------------------------------------------------------- #


def _install_dispatch_seams(monkeypatch, *, kdb, sent=None, projects=None):
    """Wire the message-dispatch seams: registry + kdb for create + telegram send.

    ``create_task`` and ``send_message`` go through the injected ``kdb`` fake
    and a recorded ``sent`` list, so neither a real board nor real Telegram is
    touched.
    """
    if projects is None:
        projects = [_project(name="hscc", board="hscc", repo="/repo", topic=140)]
    monkeypatch.setattr(registry, "load_registry", lambda path: projects)
    monkeypatch.setattr(mcp_server, "_kdb", kdb)
    monkeypatch.setattr(message.telegram, "send_message",
                        lambda tid, text, _client=None:
                        sent.append((tid, text)) if sent is not None else None)


def test_message_dispatch_defaults_apply_false_and_mutates_nothing(monkeypatch):
    """flightdeck_message_dispatch(...) with defaults creates/announces nothing."""
    kdb = _RecordingKdb()
    sent = []
    _install_dispatch_seams(monkeypatch, kdb=kdb, sent=sent)

    out = mcp_server.flightdeck_message_dispatch(project="hscc", task="fix login",
                                                 body="VERIFY: pytest\nACCEPT: green")

    assert "APPLY GATE: apply=False" in out and "nothing was changed" in out
    assert "dry-run" in out
    assert "card title: fix login" in out
    assert kdb.writes == [], "dispatch dry-run must not create via kdb"
    assert sent == [], "dispatch dry-run must not announce"


def test_message_dispatch_apply_reaches_cli_command(monkeypatch):
    """flightdeck_message_dispatch(apply=True) creates the card and announces
    via the SAME cmd the CLI uses, with body as the card body and message as the
    announcement (which may differ)."""
    kdb = _RecordingKdb()
    sent = []
    _install_dispatch_seams(monkeypatch, kdb=kdb, sent=sent)

    out = mcp_server.flightdeck_message_dispatch(
        project="hscc", task="fix login", assignee="coder",
        body="VERIFY: pytest\nACCEPT: green", message="fix the login bug",
        apply=True,
    )

    assert ("create", "t_new") in kdb.writes
    assert sent == [(140, "fix the login bug")], "announcement = --message, not the body"
    assert "created on board" in out.lower()


def test_message_dispatch_unknown_project_clear_text(monkeypatch):
    """flightdeck_message_dispatch to an unknown project returns clear text."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [])
    out = mcp_server.flightdeck_message_dispatch(project="nope", task="t")
    assert "Traceback" not in out
    assert out


# --------------------------------------------------------------------------- #
# migrate_card — apply-gated card re-homing
# --------------------------------------------------------------------------- #


def _install_migrate_seams(monkeypatch, *, cards, kdb, projects=None):
    """Wire the migrate-card seams: registry + find_card + board creation via kdb.

    The target project must resolve in the registry; ``find_card`` is stubbed
    to ``cards`` (a list of flightdeck card dicts keyed by id); creation and
    archive go through the injected ``kdb`` fake.
    """
    if projects is None:
        projects = [_project(name="hscc", board="hscc", repo="/repo")]
    monkeypatch.setattr(registry, "load_registry", lambda path: projects)
    monkeypatch.setattr(legacy.kanban, "find_card",
                        lambda cid, _kdb=None: _find(cards, cid))
    monkeypatch.setattr(mcp_server, "_kdb", kdb)


def _find(cards, cid):
    for c in cards:
        if c.get("id") == cid:
            return c
    return None


def test_migrate_card_defaults_apply_false_and_mutates_nothing(monkeypatch):
    """flightdeck_migrate_card(...) with defaults creates/archives nothing."""
    kdb = _RecordingKdb()
    _install_migrate_seams(monkeypatch, cards=[
        _hcard("t1", title="Old task", body="the body", status="review"),
    ], kdb=kdb)

    out = mcp_server.flightdeck_migrate_card(card_id="t1", project="hscc")

    assert "APPLY GATE: apply=False" in out and "nothing was changed" in out
    assert "dry-run" in out
    assert "[migrated] Old task" in out
    assert kdb.writes == [], "migrate-card dry-run must not write via kdb"


def test_migrate_card_apply_reaches_cli_command(monkeypatch):
    """flightdeck_migrate_card(apply=True) creates on the target board + archives the original."""
    kdb = _RecordingKdb()
    _install_migrate_seams(monkeypatch, cards=[
        _hcard("t1", title="Old task", body="the body", status="review"),
    ], kdb=kdb)

    out = mcp_server.flightdeck_migrate_card(card_id="t1", project="hscc", apply=True)

    assert "applied" in out.lower() or "created" in out.lower()
    assert ("archive", "t1") in kdb.writes


def test_migrate_card_unknown_project_clear_text(monkeypatch):
    """flightdeck_migrate_card to an unknown project returns clear text, no traceback."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [])
    out = mcp_server.flightdeck_migrate_card(card_id="t1", project="nope")
    assert "Traceback" not in out
    assert "unknown" in out


# --------------------------------------------------------------------------- #
# report — apply-gated completion summary (posts to topic when apply=True)
# --------------------------------------------------------------------------- #

_NOW = 10_000_000


def _report_hcard(cid, title="task", status="done", completed_at=None):
    """A report-shaped card dict (carries ``completed_at``)."""
    return {
        "id": cid,
        "title": title,
        "status": status,
        "board": "hscc",
        "branch": f"wt/{cid}",
        "assignee": "coder",
        "created_at": 1000,
        "completed_at": completed_at,
        "workspace_path": f"/repo/.worktrees/{cid}",
    }


def _report_git(cmd, repo):
    """A git ``_run`` fake: report a clean repo (no merges, nothing to do)."""
    return argparse.Namespace(returncode=0, stdout="", stderr="")


def _install_report_seams(monkeypatch, *, cards):
    monkeypatch.setattr(registry, "load_registry",
                        lambda path: [_project(name="hscc", board="hscc",
                                               repo="/repo", topic=140)])
    monkeypatch.setattr(mcp_server, "_cards", cards)
    monkeypatch.setattr(mcp_server, "_run", _report_git)
    monkeypatch.setattr(mcp_server, "_now", lambda: _NOW)
    monkeypatch.setattr(mcp_server, "_state", None)
    monkeypatch.setattr(mcp_server, "_client",
                        lambda tool, arguments: _received.append((tool, arguments)))


def test_report_defaults_apply_false_and_mutates_nothing(monkeypatch):
    """flightdeck_report(...) with defaults posts nothing to Telegram."""
    global _received
    _received = []
    _install_report_seams(monkeypatch, cards=[_report_hcard("a", title="Landed A", completed_at=9_950_000)])
    monkeypatch.setattr(report.telegram, "send_message",
                        lambda tid, text, _client=None: _received.append((tid, text)))

    out = mcp_server.flightdeck_report(project="hscc")

    assert "APPLY GATE: apply=False" in out and "nothing was changed" in out
    assert "dry-run" in out
    assert _received == [], "report dry-run must not post"


def test_report_apply_reaches_cli_command(monkeypatch):
    """flightdeck_report(apply=True) posts via the same cmd the CLI uses."""
    global _received
    _received = []
    _install_report_seams(monkeypatch, cards=[_report_hcard("a", title="Landed A", completed_at=9_950_000)])
    monkeypatch.setattr(report.telegram, "send_message",
                        lambda tid, text, _client=None: _received.append((tid, text)))

    out = mcp_server.flightdeck_report(project="hscc", apply=True)

    assert _received, "apply=True must reach telegram.send_message (the CLI path)"
    assert "posted" in out.lower()


def test_report_unknown_project_clear_text(monkeypatch):
    """flightdeck_report to an unknown project returns clear text, no traceback."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [])
    out = mcp_server.flightdeck_report(project="nope")
    assert "Traceback" not in out
    assert "no project named" in out


# --------------------------------------------------------------------------- #
# incident — apply-gated lesson append (writes docs/INCIDENTS.md when apply=True)
# --------------------------------------------------------------------------- #


def _install_incident_seams(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(registry, "load_registry",
                        lambda path: [_project(name="hscc", board="hscc", repo=str(repo))])
    return repo


def test_incident_defaults_apply_false_and_mutates_nothing(monkeypatch, tmp_path):
    """flightdeck_incident(...) with defaults writes nothing to disk."""
    repo = _install_incident_seams(monkeypatch, tmp_path)

    out = mcp_server.flightdeck_incident(symptom="queue timeout wedged",
                                          project="hscc")

    assert "APPLY GATE: apply=False" in out and "nothing was changed" in out
    assert "dry-run" in out
    assert not (repo / "docs" / "INCIDENTS.md").exists(), \
        "incident dry-run must not write the incidents file"


def test_incident_apply_reaches_cli_command(monkeypatch, tmp_path):
    """flightdeck_incident(apply=True) writes via the same cmd the CLI uses."""
    repo = _install_incident_seams(monkeypatch, tmp_path)
    incidents_path = repo / "docs" / "INCIDENTS.md"

    out = mcp_server.flightdeck_incident(
        symptom="queue timeout wedged",
        project="hscc",
        fix="sample generation_tokens_total 60s apart",
        cause="45s client timeout measures queue depth, not liveness",
        lesson="the honest probe is queue depth, not liveness",
        apply=True,
    )

    assert incidents_path.exists(), "apply=True must write the incidents file"
    text = incidents_path.read_text()
    assert "**Lesson:** the honest probe is queue depth, not liveness" in text
    assert "dry-run" not in out


# --------------------------------------------------------------------------- #
# hygiene — apply-gated board-decay fixer
# --------------------------------------------------------------------------- #


class _RecordingHygieneKdb:
    """A kdb stand-in that records archive/create ops for hygiene's plan."""

    def __init__(self):
        self.archived = []
        self.created = []

    class _Conn:
        def __init__(self, board):
            self.board = board

        def close(self):
            pass

    def connect(self, board):
        return self._Conn(board)

    def archive_task(self, conn, cid):
        self.archived.append(cid)
        return True

    def create_task(self, conn, **kwargs):
        self.created.append(kwargs.get("title"))
        return "t_new"


def _install_hygiene_seams(monkeypatch, *, cards, kdb, worktrees=()):
    monkeypatch.setattr(registry, "load_registry",
                        lambda path: [_project(name="hscc", board="hscc", repo="/repo")])
    monkeypatch.setattr(hygiene.kanban, "list_cards", lambda **kw: cards)
    monkeypatch.setattr(hygiene, "_collect_worktrees", lambda projects, _listdir=None: list(worktrees))
    monkeypatch.setattr(mcp_server, "_kdb", kdb)
    monkeypatch.setattr(mcp_server, "_run", _report_git)


def test_hygiene_defaults_apply_false_and_mutates_nothing(monkeypatch):
    """flightdeck_hygiene(...) with defaults archives/recreates nothing."""
    kdb = _RecordingHygieneKdb()
    _install_hygiene_seams(monkeypatch, cards=[
        _hcard("a", title="dup", status="review"),
        _hcard("b", title="dup", status="review"),
    ], kdb=kdb)

    out = mcp_server.flightdeck_hygiene()

    assert "APPLY GATE: apply=False" in out and "nothing was changed" in out
    assert "dry-run" in out
    assert kdb.archived == [] and kdb.created == [], \
        "hygiene dry-run must not archive/recreate"


def test_hygiene_apply_reaches_cli_command(monkeypatch):
    """flightdeck_hygiene(apply=True) reaches the CLI's apply-card path."""
    kdb = _RecordingHygieneKdb()
    _install_hygiene_seams(monkeypatch, cards=[
        _hcard("a", title="dup", status="review"),
        _hcard("b", title="dup", status="review"),
    ], kdb=kdb)
    # Force hygiene to find ONE duplicate: keep a, archive b.
    # ``build_plan`` lives in flightdeck.core.hygiene, which the command module
    # imports as ``hygiene`` — patch it where the command resolves it.
    core_hygiene = hygiene.hygiene
    monkeypatch.setattr(
        core_hygiene, "build_plan",
        lambda active, git_facts, worktrees, closed_ids, threshold=0.88: {
            "duplicates": [{"board": "hscc", "keep": active[0] if active else {},
                            "archive": [active[1] if len(active) > 1 else {}]}],
            "triage": [],
            "stale_worktrees": [],
        },
    )

    out = mcp_server.flightdeck_hygiene(apply=True)

    assert "applied" in out.lower()
    assert kdb.archived, "apply=True must reach hygiene.apply_card_plan (the CLI path)"


def test_hygiene_unknown_project_clear_text(monkeypatch):
    """flightdeck_hygiene returns a clear message, never a traceback."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [])
    monkeypatch.setattr(hygiene.kanban, "list_cards", lambda **kw: [])
    monkeypatch.setattr(hygiene, "_collect_worktrees", lambda projects, _listdir=None: [])
    monkeypatch.setattr(mcp_server, "_kdb", _RecordingHygieneKdb())
    monkeypatch.setattr(mcp_server, "_run", _report_git)

    out = mcp_server.flightdeck_hygiene()
    assert "Traceback" not in out
    assert out


# --------------------------------------------------------------------------- #
# failure surfaces clear text, never a traceback (all tools)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tool,args", [
    ("flightdeck_review", {"card": "t_x"}),
    ("flightdeck_reconcile", {}),
    ("flightdeck_roadmap_adopt", {"project": "nope"}),
    ("flightdeck_message_send", {"project": "nope", "text": "x"}),
])
def test_failure_returns_clear_text_not_traceback(monkeypatch, tool, args):
    """A failing mutating tool returns clear text, not a traceback."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [])
    out = getattr(mcp_server, tool)(**args)
    assert "Traceback" not in out
    assert out
