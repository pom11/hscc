"""Tests for flightdeck.commands.ingest.

``ingest`` drafts a project's ROADMAP.md from existing context: Hermes' own
skill references (an injectable ``_read_refs``), the project repo (README, docs,
git log — via an injectable ``_read`` / ``_run``), and the project's Telegram
topic (via the existing ``core/telegram.read_messages`` with an injectable
``_client``). It then ASKS the orchestrator through an injectable ``ask`` seam
(defaulting to decompose's send+read) and prints/writes the ROADMAP proposal.

These tests drive it against STUBS. No test touches real ~/.hermes/skills, git,
the live kanban board, Telegram, the network or the cluster. The project repo
lives under tmp_path, so all writes happen there.

The contract under test: every present source contributes; a missing source is
reported and the draft still happens; the emitted markdown ROUND-TRIPS through
core/roadmap.parse_roadmap (asserting parsed milestone ids and item counts);
--apply never overwrites an existing ROADMAP.md and writes docs/ROADMAP.draft.md
instead; without --apply nothing is written; an unknown project errors listing
the known ones.
"""

import argparse
import os
import subprocess

import pytest

from flightdeck.commands import ingest as ing
from flightdeck.commands import decompose as dec
from flightdeck.core import registry, roadmap
# NOTE: Telegram has been removed from flightdeck, so MAX_MESSAGE_LENGTH,
# MessageTooLongError and TopicLockedError no longer exist. The tests that
# exercised the removed Telegram read/send path have been deleted; only tests
# covering the surviving ingest logic remain below.


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #

class FakeGit:
    """Stands in for the subprocess runner: callable (cmd, repo) -> process."""

    def __init__(self, log="first commit\nsecond feature\nthird fix\n"):
        self.log = log
        self.calls: list[tuple] = []

    def __call__(self, cmd, repo):
        self.calls.append((cmd, repo))
        if cmd[:2] == ["git", "log"] and cmd[2:4] == ["--oneline", "-n"]:
            return subprocess.CompletedProcess(cmd, 0, self.log, "")
        return subprocess.CompletedProcess(cmd, 128, "", "")


class FakeTG:
    """Stands in for the MCP daemon client: callable (tool, args) -> str.

    ``telegram_read`` mirrors the daemon's ``[ts] sender: text`` line format
    that ``core/telegram.read_messages`` parses. ``locked=True`` makes every
    call raise the single-writer SQLite error (normalised to TopicLockedError).
    """

    def __init__(self, lines="", locked=False):
        self.lines = lines
        self.locked = locked
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if self.locked:
            raise ConnectionError("sqlite3.OperationalError: database is locked")
        if tool_name == "telegram_read":
            return self.lines
        raise AssertionError(f"FakeTG does not know tool {tool_name!r}")


def _ask_stub(draft):
    """An ``args.ask`` stub returning ``draft`` verbatim for any prompt/topic."""
    def _ask(prompt, topic_id, client=None):
        return draft
    return _ask


def _ns(**kw):
    """Build an argparse.Namespace with the defaults cmd_ingest needs.

    Defaults ``ask_inline`` to True because every caller of this helper tests
    the SYNCHRONOUS ``--ask-inline`` path (ask is called, PROPOSED ROADMAP,
    N3 validation). The real default is card-dispatch; the N11 card tests pass
    ``ask_inline=False`` explicitly and inject a fake ``_kdb``.
    """
    defaults = dict(
        client=None,
        registry=None,
        read_refs=None,
        read=None,
        run=None,
        ask=None,
        limit=200,
        project="",
        apply=False,
        context_dir=None,
        ask_inline=True,
        _kdb=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _project(name="demo", repo=None, topic=None, roadmap_path=None, board=None):
    return registry.Project(
        name=name,
        repo=str(repo) if repo else "~/dev/demo",
        topic=int(topic) if topic is not None else None,
        roadmap=roadmap_path,
        board=board,
    )

def _repo(tmp_path):
    """Create and return the project repo dir (tmp_path / 'repo')."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    return str(repo)


_ROADMAP = """# Subproject: demo

## Milestone: auth-hardening <!-- id: auth-hardening -->
status: now
- [x] password reset restricted to self/admin
- [ ] server-side key enforcement

## Milestone: billing <!-- id: billing -->
status: next
- [x] stripe checkout wired
- [ ] chargeback handling
"""
# (milestone ids: auth-hardening (2 items, 1 done), billing (2 items, 1 done))


# --------------------------------------------------------------------------- #
# Source gathering
# --------------------------------------------------------------------------- #

def test_git_log_gathers_subject_lines():
    fg = FakeGit(log="feat: a\nfix: b\n")
    repo = "/some/repo"
    assert ing._git_log(repo, 200, run=fg) == "feat: a\nfix: b\n"
    # The caller requested "oneline -n 200" exactly (fixed by the card).
    assert fg.calls[0][0][:4] == ["git", "log", "--oneline", "-n"]


def _build_args(tmp_path, *, read_refs="SOME SKILL REF about demo\n", repo_files=None,
                topic_lines="[12:00] alice: shipped the auth hardening\n",
                draft=_ROADMAP, apply=False, project="demo", topic=140,
                roadmap_path=None, limit=200, context_dir=None):
    repo = _repo(tmp_path)
    if repo_files:
        for rel, text in repo_files.items():
            full = os.path.join(repo, rel)
            os.makedirs(os.path.dirname(full) or repo, exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(text)
    fg = FakeGit(log="feat: auth\nfix: billing\n")
    tg = FakeTG(lines=topic_lines)
    args = _ns(
        read_refs=(lambda p: read_refs),
        read=ing._default_read,
        run=fg,
        client=tg,
        ask=_ask_stub(draft),
        project=project,
        apply=apply,
        limit=limit,
        context_dir=context_dir if context_dir is not None else str(tmp_path / "context"),
        # stash runtime attrs so tests can assert on the seams
        _fg=fg,
        _tg=tg,
        _repo=repo,
    )
    return args


# --------------------------------------------------------------------------- #
# The command — all sources present, round-trip, apply
# --------------------------------------------------------------------------- #

def _demo_project(args):
    return _project(repo=args._repo, topic=args.topic if hasattr(args, "topic") else 140)


def test_all_sources_contribute_and_nothing_written_without_apply(tmp_path, monkeypatch, capsys):
    captured = {}
    def _capturing_ask(prompt, topic_id, client=None):
        captured["prompt"] = prompt
        return _ROADMAP
    args = _build_args(
        tmp_path,
        repo_files={"README.md": "# demo\nlocal readme", "docs/notes.md": "some docs"},
    )
    args.ask = _capturing_ask
    rc = ing.cmd_ingest(args, [_project(repo=args._repo, topic=140)])
    assert rc == 0
    out = capsys.readouterr().out
    # The draft was printed.
    assert "PROPOSED ROADMAP" in out
    assert "auth-hardening" in out
    # N7: the context is NOT inlined in the sent prompt. It is staged to the
    # scratch context file, and the SHORT prompt only points at that file's
    # absolute path — an inline context would blow past Telegram's 4096 limit.
    prompt = captured["prompt"]
    context_path = os.path.join(args.context_dir, "ingest-context-demo.md")
    assert "SOME SKILL REF about demo" not in prompt      # context NOT inline
    assert "local readme" not in prompt                    # context NOT inline
    assert context_path in prompt                          # the pointer names the file
    assert len(prompt) < 3000                              # stays comfortably under the limit
    # The context file holds ALL the gathered blocks, with the sources ordered.
    with open(context_path, encoding="utf-8") as f:
        staged = f.read()
    assert "## Hermes skill references" in staged
    assert "SOME SKILL REF about demo" in staged
    assert "## project repository (README, docs, git log)" in staged
    assert "local readme" in staged and "some docs" in staged
    # No --apply: nothing written.
    assert not os.path.exists(os.path.join(args._repo, "ROADMAP.md"))
    assert not os.path.exists(os.path.join(args._repo, "docs", "ROADMAP.draft.md"))


def test_emitted_markdown_roundtrips(tmp_path, monkeypatch, capsys):
    """The real acceptance test: the draft parses with the expected ids+counts."""
    args = _build_args(tmp_path, repo_files={"README.md": "# demo\n"})
    rc = ing.cmd_ingest(args, [_project(repo=args._repo, topic=140)])
    assert rc == 0
    err = capsys.readouterr().err
    # Parse evidence surfaced.
    assert "round-trips" in err
    assert "auth-hardening" in err and "1/2 done" in err
    assert "billing" in err and "1/2 done" in err

    rm_path = tmp_path / "ROADMAP.md"
    rm_path.write_text(_ROADMAP, encoding="utf-8")
    parsed = roadmap.parse_roadmap(str(rm_path))
    assert [m.id for m in parsed.milestones] == ["auth-hardening", "billing"]
    m0 = parsed.milestones[0]
    assert m0.total == 2 and m0.done_count == 1
    assert m0.items[0].checked is True      # `- [x] password reset…`
    assert m0.items[1].checked is False     # `- [ ] server-side key…`
    assert parsed.milestones[1].total == 2 and parsed.milestones[1].done_count == 1


def test_apply_writes_roadmap_when_none_exists(tmp_path, monkeypatch, capsys):
    args = _build_args(tmp_path, apply=True)
    rc = ing.cmd_ingest(args, [_project(repo=args._repo, topic=140)])
    assert rc == 0
    target = os.path.join(args._repo, "ROADMAP.md")
    assert os.path.exists(target)
    with open(target, encoding="utf-8") as f:
        assert "auth-hardening" in f.read()
    err = capsys.readouterr().err
    assert "wrote" in err and "ROADMAP.md" in err


def test_apply_never_overwrites_existing_roadmap(tmp_path, monkeypatch, capsys):
    """An existing ROADMAP.md is kept; the draft goes beside it in docs/."""
    existing = _repo(tmp_path)  # tmp_path/repo
    with open(os.path.join(existing, "ROADMAP.md"), "w", encoding="utf-8") as f:
        f.write("# Subproject: demo\n\n## Milestone: hand-written <!-- id: manual -->\nstatus: now\n- [ ] item\n")
    args = _build_args(tmp_path, apply=True)
    rc = ing.cmd_ingest(args, [_project(repo=args._repo, topic=140)])
    assert rc == 0

    # The hand-written roadmap is UNTOUCHED.
    with open(os.path.join(existing, "ROADMAP.md"), encoding="utf-8") as f:
        content = f.read()
        assert "hand-written" in content and "auth-hardening" not in content
    # The draft landed beside it in docs/.
    draft = os.path.join(existing, "docs", "ROADMAP.draft.md")
    assert os.path.exists(draft)
    with open(draft, encoding="utf-8") as f:
        assert "auth-hardening" in f.read()
    err = capsys.readouterr().err
    assert "already exists" in err and "ROADMAP.draft.md" in err


def test_without_apply_nothing_is_written(tmp_path, monkeypatch, capsys):
    args = _build_args(tmp_path, repo_files={"README.md": "# demo\n"}, apply=False)
    rc = ing.cmd_ingest(args, [_project(repo=args._repo, topic=140)])
    assert rc == 0
    assert not os.path.exists(os.path.join(args._repo, "ROADMAP.md"))
    assert not os.path.exists(os.path.join(args._repo, "docs", "ROADMAP.draft.md"))
    out = capsys.readouterr().out
    assert "PROPOSED ROADMAP" in out


# --------------------------------------------------------------------------- #
# Missing sources
# --------------------------------------------------------------------------- #

def test_missing_sources_reported_but_draft_still_happens(tmp_path, monkeypatch, capsys):
    """A PARTIALLY-empty gather still proceeds to ask.

    Here only sources B and C are absent (repo has nothing readable, project
    has no topic) but source A (skill references) contributes — so the draft
    still happens via the injected ask. Every missing source is REPORTED with
    its empty state, never silently dropped.
    """
    args = _ns(
        read_refs=lambda p: "skill notes about demo\n",
        read=lambda p: None,
        run=FakeGit(log=""),
        client=None,
        ask=_ask_stub(_ROADMAP),
        project="demo",
        apply=False,
        limit=200,
        context_dir=str(tmp_path / "context"),
    )
    rc = ing.cmd_ingest(args, [_project(repo=_repo(tmp_path), topic=None)])
    assert rc == 0
    # Every present source reported with a count; absent ones report EMPTY.
    err = capsys.readouterr().err
    assert "ok (1 files)" not in err  # (bare-string stub renders "ok") see below
    assert "repo source unavailable" in err  # repo: nothing readable
    # skill references (bare-string stub) rendered "ok"
    assert "[ingest] skill references: ok" in err


def test_no_contributing_source_and_no_injected_ask_errors(tmp_path, monkeypatch, capsys):
    """Every source is empty and there is no injected ask: exit non-zero.

    Nothing to synthesise from means the draft cannot happen — the failure is
    reported before any ask is attempted, not swallowed.
    """
    args = _ns(
        read_refs=lambda p: "",
        read=lambda p: None,
        run=FakeGit(log=""),
        client=None,
        ask=None,
        project="demo",
        apply=False,
        limit=200,
    )
    rc = ing.cmd_ingest(args, [_project(repo=_repo(tmp_path), topic=None)])
    err = capsys.readouterr().err
    assert rc != 0
    assert "nothing to synthesise" in err


def test_unknown_project_errors_listing_known_ones(tmp_path, capsys):
    args = _ns(project="nope", apply=False)
    rc = ing.cmd_ingest(args, [_project(name="demo", repo=_repo(tmp_path))])
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown project" in err
    assert "demo" in err  # the known one is listed


# =========================================================================== #
# N3 — an orchestrator reply that is not a roadmap is REJECTED, never rendered
# =========================================================================== #

def _ns_with_draft(draft, *, repo, apply=False, timeout=300, topic=140, **kw):
    """A namespace ready to run cmd_ingest with the given injected ask reply."""
    args = _ns(
        read_refs=lambda p: "skill notes about demo\n",
        read=ing._default_read,
        run=FakeGit(log="feat: a\n"),
        client=FakeTG(lines="[12:00] alice: shipped x\n"),
        ask=_ask_stub(draft),
        project="demo",
        apply=apply,
        timeout=timeout,
        topic=topic,
        limit=200,
        **kw,
    )
    args._repo = repo
    args.context_dir = os.path.join(repo, ".flightdeck-context")
    return args


def test_not_a_roadmap_is_rejected_never_presented(tmp_path, capsys):
    """A reply with no milestone/item surface is REJECTED: never shown as a
    PROPOSED ROADMAP, non-zero exit, raw reply under the RAW REPLY heading."""
    reply = (
        "the orchestrator reasoned about kanban dispatch, cli.py auto-discovery "
        "and killing in-flight workers for nine minutes.\nNo roadmap at all.\n"
    )
    args = _ns_with_draft(reply, repo=_repo(tmp_path))
    rc = ing.cmd_ingest(args, [_project(repo=args._repo, topic=140)])
    captured = capsys.readouterr()
    out, err = captured.out, captured.err.strip()
    # rejected non-zero, nothing written
    assert rc != 0
    assert not os.path.exists(os.path.join(args._repo, "ROADMAP.md"))
    assert not os.path.exists(os.path.join(args._repo, "docs", "ROADMAP.draft.md"))
    # never rendered as a draft
    assert "PROPOSED ROADMAP" not in out
    # raw reply surfaced as a diagnostic under RAW REPLY
    assert "was not a roadmap" in err
    assert "RAW REPLY (not a roadmap)" in err
    assert "No roadmap at all" in err


def test_roadmap_with_zero_milestones_is_rejected(tmp_path, capsys):
    """Even prose that LOOKS roadmap-ish (an H2) but yields zero milestones is
    not a roadmap per the parser, and is rejected."""
    reply = "## Not-A-Milestone heading\nsome prose that parses to nothing\n"
    args = _ns_with_draft(reply, repo=_repo(tmp_path))
    rc = ing.cmd_ingest(args, [_project(repo=args._repo, topic=140)])
    captured = capsys.readouterr()
    out, err = captured.out, captured.err.strip()
    assert rc != 0
    assert "PROPOSED ROADMAP" not in out
    assert "was not a roadmap" in err


def test_milestone_with_no_items_is_rejected(tmp_path, capsys):
    """A milestone with zero items yields no roadmap: at least one item is
    required, so an empty milestone is rejected."""
    reply = "# Subproject: demo\n\n## Milestone: empty <!-- id: empty -->\nstatus: now\n"
    args = _ns_with_draft(reply, repo=_repo(tmp_path))
    rc = ing.cmd_ingest(args, [_project(repo=args._repo, topic=140)])
    captured = capsys.readouterr()
    out, err = captured.out, captured.err.strip()
    assert rc != 0
    assert "PROPOSED ROADMAP" not in out
    assert "was not a roadmap" in err


def test_wrapped_in_prose_and_fence_is_unwrapped_and_accepted(tmp_path, capsys):
    """A reply wrapped in prose AND a ```markdown fence is unwrapped and the
    extracted roadmap is ACCEPTED and presented."""
    reply = (
        "Here is your roadmap proposal for demo, as requested:\n\n"
        "```markdown\n"
        + _ROADMAP +
        "\n```\n"
        "Hope that helps!\n"
    )
    args = _ns_with_draft(reply, repo=_repo(tmp_path))
    rc = ing.cmd_ingest(args, [_project(repo=args._repo, topic=140)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PROPOSED ROADMAP" in out
    assert "auth-hardening" in out and "billing" in out
    # the wrapper prose / fences are NOT presented
    assert "Here is your roadmap proposal" not in out
    assert "```" not in out
    assert "Hope that helps!" not in out


def test_fence_only_wrapped_reply_accepted(tmp_path, capsys):
    reply = "```markdown\n" + _ROADMAP + "\n```\n"
    args = _ns_with_draft(reply, repo=_repo(tmp_path))
    rc = ing.cmd_ingest(args, [_project(repo=args._repo, topic=140)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PROPOSED ROADMAP" in out and "auth-hardening" in out


def test_valid_reply_roundtrips_and_writes_under_apply(tmp_path, capsys):
    """A valid reply wrapped in prose still round-trips AND writes under
    --apply (the CLEAN extracted roadmap is written, not the wrappers)."""
    reply = "sure!\n```markdown\n" + _ROADMAP + "\n```\nglad to help\n"
    args = _ns_with_draft(reply, repo=_repo(tmp_path), apply=True)
    rc = ing.cmd_ingest(args, [_project(repo=args._repo, topic=140)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "round-trips" in err and "auth-hardening" in err and "1/2 done" in err
    assert os.path.exists(os.path.join(args._repo, "ROADMAP.md"))
    with open(os.path.join(args._repo, "ROADMAP.md"), encoding="utf-8") as f:
        content = f.read()
    assert "auth-hardening" in content and "billing" in content
    assert "sure!" not in content and "```" not in content  # wrappers stripped


def test_timeout_exits_nonzero_and_writes_nothing(tmp_path, capsys):
    """--timeout bounds the ask call: a reply slower than the bound exits
    non-zero with a timeout message and writes nothing."""
    import time

    def _slow_ask(prompt, topic_id, client=None):
        time.sleep(0.3)
        return _ROADMAP

    args = _ns_with_draft("", repo=_repo(tmp_path))
    args.ask = _slow_ask
    args.timeout = 0.1
    rc = ing.cmd_ingest(args, [_project(repo=args._repo, topic=140)])
    err = capsys.readouterr().err
    assert rc != 0
    assert "within 0.1s" in err
    assert "nothing written" in err
    assert not os.path.exists(os.path.join(args._repo, "ROADMAP.md"))
    assert not os.path.exists(os.path.join(args._repo, "docs", "ROADMAP.draft.md"))


def test_timeout_attr_default(monkeypatch):
    """cmd_ingest defaults timeout to _DEFAULT_TIMEOUT when unset on args."""
    assert ing._DEFAULT_TIMEOUT == 300
    # a zero/None timeout means no bound is applied at all (call directly)
    assert ing._ask_with_timeout(lambda p, t, c=None: "draft", "p", 1, None, 0) == "draft"


# =========================================================================== #
# N6 — --timeout and --limit must reach where they are used (the live failure)
# =========================================================================== #

def test_skill_reference_cap_reported(tmp_path, monkeypatch, capsys):
    """The default read_refs caps at _SKILL_REF_MAX files and reports the
    truncation with the counts."""
    fake_root = tmp_path / ".hermes" / "skills"
    monkeypatch.setenv("HOME", str(tmp_path))
    # 63 files, each mentioning the project
    for i in range(63):
        d = fake_root / f"skill{i}" / "references"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"ref{i}.md").write_text(f"### demo file {i} mentions demo\n", encoding="utf-8")
    proj = _project(repo=_repo(tmp_path), topic=140)
    content, report = ing._default_read_refs(proj)
    assert "ok (40 of 63 files)" in report
    # exactly _SKILL_REF_MAX files made it into the content (lexicographic cap)
    assert content.count("### demo file") == ing._SKILL_REF_MAX == 40
    # total matched files still 63 -- the cap is on inclusion, not detection
    assert len(list(fake_root.glob("*/references/*.md"))) == 63


def test_skill_reference_cap_within_limit_no_truncation(tmp_path, monkeypatch, capsys):
    fake_root = tmp_path / ".hermes" / "skills"
    monkeypatch.setenv("HOME", str(tmp_path))
    for i in range(3):
        d = fake_root / f"skill{i}" / "references"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"ref{i}.md").write_text(f"demo file {i} mentions demo\n", encoding="utf-8")
    proj = _project(repo=_repo(tmp_path), topic=140)
    content, report = ing._default_read_refs(proj)
    assert "ok (3 files)" in report
    assert content.count("demo file") == 3


def test_git_log_cap_reported(tmp_path, monkeypatch, capsys):
    """A git log longer than _GIT_LOG_N is truncated and the gather report
    states the truncation with the counts."""
    log = "\n".join(f"commit {i}" for i in range(200)) + "\n"
    repo = _repo(tmp_path)
    args = _ns(
        read_refs=lambda p: "skill\n",
        read=ing._default_read,
        run=FakeGit(log=log),
        client=FakeTG(lines="[12:00] alice: x\n"),
        ask=_ask_stub(_ROADMAP),
        project="demo",
        apply=False,
        timeout=300,
        topic=140,
        repo=repo,
        limit=200,
    )
    args._repo = repo
    rc = ing.cmd_ingest(args, [_project(repo=args._repo, topic=140)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "git log truncated (last %d of 200 subjects)" % ing._GIT_LOG_N in err


# =========================================================================== #
# N7 — a large project's context stays OFF the channel: file pointer, not inline
# =========================================================================== #

def test_large_project_sent_prompt_stays_under_the_4096_limit(tmp_path, monkeypatch, capsys):
    """The LIVE-FAILURE test: a large project's context must not blow the prompt.

    The 2026-08-10 run gathered 34 skill refs + README + 4 docs + 150 git
    subjects + 25 topic messages into ONE prompt, blew past Telegram's 4096-char
    cap, was rejected, and ingest polled 900s for a reply that could not come.
    Assert the SENT text length (the prompt actually handed to the ask seam),
    NOT the context length — the context file is allowed to be enormous, the
    sent prompt must stay small. The context is staged to the file; only a
    short pointer goes down the channel.
    """
    captured = {}

    def _capturing_ask(prompt, topic_id, client=None):
        captured["prompt"] = prompt
        return _ROADMAP

    # A genuinely large context: a fat skill reference, a fat README, many
    # topic messages — the kind of gather that used to blow past 4096.
    big_skill = ("# DEMO reference\n" + ("context line " * 2000) + "\n")  # ~26KB
    big_readme = ("# demo\n" + ("readme line about demo stuff\n" * 1000) + "\n")  # ~26KB
    lines = "\n".join(f"[12:00] alice: shipped thing {i}" for i in range(50))
    args = _build_args(tmp_path, read_refs=big_skill, topic_lines=lines, limit=50,
                       repo_files={"README.md": big_readme})
    args.ask = _capturing_ask
    rc = ing.cmd_ingest(args, [_project(repo=args._repo, topic=140)])
    assert rc == 0
    prompt = captured["prompt"]
    # Assert the SENT text length — the prompt that actually goes to the seam.
    assert len(prompt) < 3000
    # The context is NOT in this small prompt; it lives in the staged file.
    context_path = os.path.join(args.context_dir, "ingest-context-demo.md")
    with open(context_path, encoding="utf-8") as f:
        staged = f.read()
    assert len(staged) > 10000  # the context file is genuinely large
    assert context_path in prompt  # the prompt points at that path


def test_context_file_written_with_blocks_and_prompt_has_absolute_path(tmp_path, monkeypatch, capsys):
    """The context file is written with the gathered blocks and the short prompt
    contains its ABSOLUTE path.

    This is the redirect that fixes the live failure: ingest stages every
    gathered block into ``ingest-context-<project>.md`` and the message sent to
    the orchestrator is a short pointer naming that file, so the orchestrator
    (on this host, with file tools) can read all the context without a single
    oversized chat message. ``context_dir`` is injected so the write lands in
    tmp_path, never the real ``~/.flightdeck``.
    """
    captured = {}

    def _capturing_ask(prompt, topic_id, client=None):
        captured["prompt"] = prompt
        return _ROADMAP

    args = _build_args(
        tmp_path,
        repo_files={"README.md": "# demo\nlocal readme", "docs/notes.md": "some docs"},
    )
    args.ask = _capturing_ask
    rc = ing.cmd_ingest(args, [_project(repo=args._repo, topic=140)])
    assert rc == 0
    # The file exists at the expected absolute path, with all blocks.
    context_path = os.path.join(args.context_dir, "ingest-context-demo.md")
    assert os.path.isabs(context_path)
    assert os.path.exists(context_path)
    with open(context_path, encoding="utf-8") as f:
        staged = f.read()
    assert "## Hermes skill references" in staged
    assert "SOME SKILL REF about demo" in staged
    assert "## project repository (README, docs, git log)" in staged
    assert "local readme" in staged and "some docs" in staged
    # The prompt names the ABSOLUTE path (matches what cmd_ingest wrote).
    prompt = captured["prompt"]
    assert context_path in prompt
    assert os.path.isabs(context_path)


def test_extract_roadmap_helpers():
    """Unit tests for the strip helpers."""
    # preamble stripped, region kept to the end
    assert ing._extract_roadmap("intro prose\n# Subproject: demo\nx") == "# Subproject: demo\nx"
    # milestone-only start also works
    assert ing._extract_roadmap("prose\n## Milestone: m <!-- id: m -->\n- [ ] i\n").startswith("## Milestone:")
    # fence unwrapped
    assert ing._extract_roadmap("```markdown\n# Subproject: demo\ny\n```") == "# Subproject: demo\ny"
    # no roadmap-shaped line -> empty (rejection)
    assert ing._extract_roadmap("just prose, no headings\n") == ""
    # unwrap fence standalone
    assert ing._unwrap_fence("```markdown\nbody\n```") == "body"
    assert ing._unwrap_fence("body") == "body"

# =========================================================================== #
# N11 — by DEFAULT ingest dispatches a KANBAN CARD, not a synchronous ask
# =========================================================================== #

class _FakeKdb:
    """A stand-in for hermes_cli.kanban_db with create_task recorded.

    Records every ``create_task`` call (board, title, assignee, body) so tests
    can assert exactly which board the ingest card landed on, that the ABSOLUTE
    context path is in its body, and that the body tells the worker to write
    ``docs/ROADMAP.draft.md``. No test ever touches the real board.
    """

    def __init__(self, card_id="c-123", current="default"):
        self.card_id = card_id
        self.current = current
        self.created = []  # list of dicts

    def connect(self, board=None):
        return self

    def close(self):
        pass

    def get_current_board(self):
        return self.current

    def create_task(self, conn, *, title=None, body=None, assignee=None, board=None):
        self.created.append(
            {"board": board, "title": title, "body": body, "assignee": assignee}
        )
        return self.card_id


def _card_args(tmp_path, *, apply=False, **kw):
    """Args for the card-dispatch path: ask_inline disabled + a fake kdb."""
    args = _build_args(
        tmp_path,
        apply=apply,
        repo_files={"README.md": "# demo\n"},
        **kw,
    )
    args.ask_inline = False
    args._kdb = _FakeKdb()
    args.topic = 140  # a project may have a topic; the card path ignores it
    return args


def test_default_apply_creates_exactly_one_card_on_project_board(tmp_path, monkeypatch, capsys):
    """The DEFAULT path (no --ask-inline): --apply creates ONE card on the
    project's board, whose body carries the ABSOLUTE context path and the
    repo as the workspace, and never calls the ask seam."""
    captured = {"asked": False}

    def _spy_ask(prompt, topic_id, client=None):
        captured["asked"] = True
        return _ROADMAP

    kdb = _FakeKdb(card_id="t-42")
    args = _card_args(tmp_path, apply=True)
    args.ask = _spy_ask
    args._kdb = kdb
    proj = _project(repo=args._repo, topic=140, board="acme-board")
    rc = ing.cmd_ingest(args, [proj])
    assert rc == 0
    # exactly one card created, on the project's board
    assert len(kdb.created) == 1
    created = kdb.created[0]
    assert created["board"] == "acme-board"
    # the ask seam was NOT called on the default path
    assert not captured["asked"]
    # stdout carries the card id + a clear message
    out = capsys.readouterr().out
    assert "t-42" in out
    assert "docs/ROADMAP.draft.md" in out
    # the context is still staged (unchanged)
    context_path = os.path.join(args.context_dir, "ingest-context-demo.md")
    assert os.path.exists(context_path)


def test_default_apply_project_with_board_ignores_global_current_board_regression(tmp_path, capsys):
    """R1-R7 wrong-board incident (ingest): a project WITH a board lands on
    THAT board, never on Hermes' global current board, even when the current
    board points elsewhere."""
    kdb = _FakeKdb(card_id="t-9", current="default")  # GLOBAL current is 'default'
    args = _card_args(tmp_path, apply=True)
    args._kdb = kdb
    proj = _project(repo=args._repo, topic=140, board="acme-board")
    rc = ing.cmd_ingest(args, [proj])
    assert rc == 0
    assert len(kdb.created) == 1
    assert kdb.created[0]["board"] == "acme-board"
    captured = capsys.readouterr()
    # The canonical report line names the target board (the same report shape
    # every card-creating path emits), and it reports on stderr like the other
    # ingest gather lines.
    assert "created ingest card t-9 on board 'acme-board'" in captured.err
    # The project HAS a board, so no fallback say-so.
    assert "no board for demo" not in captured.err


def test_dispatch_does_not_mutate_global_current_board_even_on_failure(tmp_path, capsys):
    """The GLOBAL current board is never changed by ingest's card dispatch —
    not even when the create raises. A card-creating command must read the
    current board as a fallback, never write it, so a failure can't leave the
    global board redirected for unrelated work."""
    kdb = _FakeKdb(card_id="t-9", current="flightdeck")
    # Make the fake raise inside create_task: patch the instance method.
    def _boom_create(conn, *, title=None, body=None, assignee=None, board=None):
        raise ing.kanban.KanbanError("could not read board (stub)")
    kdb.create_task = _boom_create
    args = _card_args(tmp_path, apply=True)
    args._kdb = kdb
    proj = _project(repo=args._repo, topic=140, board=None)
    rc = ing.cmd_ingest(args, [proj])
    assert rc == 2
    err = capsys.readouterr().err
    assert "could not create the ingest card" in err
    # The current board was read for the fallback AND left exactly as it was.
    assert kdb.current == "flightdeck"


def test_card_body_names_absolute_context_path_and_draft_format(tmp_path):
    """The card body names docs/ROADMAP.draft.md, the ABSOLUTE context path,
    and the EXACT format roadmap.parse_roadmap understands."""
    kdb = _FakeKdb()
    args = _card_args(tmp_path, apply=True)
    args._kdb = kdb
    proj = _project(repo=args._repo, topic=140, board="acme-board")
    rc = ing.cmd_ingest(args, [proj])
    assert rc == 0
    body = kdb.created[0]["body"]
    context_path = os.path.join(args.context_dir, "ingest-context-demo.md")
    assert os.path.abspath(context_path) in body
    assert os.path.isabs(context_path)
    assert "docs/ROADMAP.draft.md" in body
    assert os.path.join(proj.repo, "docs", "ROADMAP.draft.md") in body
    # the exact parseable format is named
    assert "## Milestone:" in body and "status: now|next|later" in body
    assert "- [x]" in body and "- [ ]" in body
    assert "# Subproject: demo" in body


def test_without_apply_nothing_is_created_and_context_still_staged(tmp_path, capsys):
    """Default path without --apply: NO card is created, the context is still
    staged, and it is a dry-run."""
    kdb = _FakeKdb()
    args = _card_args(tmp_path, apply=False)
    args._kdb = kdb
    rc = ing.cmd_ingest(args, [_project(repo=args._repo, topic=140, board="acme-board")])
    assert rc == 0
    assert len(kdb.created) == 0
    # context still staged
    context_path = os.path.join(args.context_dir, "ingest-context-demo.md")
    assert os.path.exists(context_path)
    err = capsys.readouterr().err
    assert "dry-run" in err


def test_project_with_no_board_falls_back_to_current_and_says_so(tmp_path, capsys):
    """A project with no ``board`` falls back to Hermes' CURRENT board (not a
    hardcoded 'default') and the output SAYS which board was used — a silent
    fallback is how cards end up on the wrong board."""
    kdb = _FakeKdb(current="flightdeck")
    args = _card_args(tmp_path, apply=True)
    args._kdb = kdb
    proj = _project(repo=args._repo, topic=140, board=None)
    rc = ing.cmd_ingest(args, [proj])
    assert rc == 0
    assert len(kdb.created) == 1
    assert kdb.created[0]["board"] == "flightdeck"
    err = capsys.readouterr().err
    assert "no board for demo; created on 'flightdeck'" in err


def test_ask_inline_still_takes_synchronous_path(tmp_path, capsys):
    """--ask-inline restores the synchronous path: the ask seam IS called and
    no card is created."""
    calls = []

    def _spy_ask(prompt, topic_id, client=None):
        calls.append(prompt)
        return _ROADMAP

    args = _build_args(tmp_path, apply=True, repo_files={"README.md": "# demo\n"})
    args.ask = _spy_ask
    args.ask_inline = True
    args._kdb = _FakeKdb()
    rc = ing.cmd_ingest(args, [_project(repo=args._repo, topic=140)])
    assert rc == 0
    assert len(calls) == 1  # synchronous ask happened
    # no card was created (the fake kdb records nothing)
    assert args._kdb.created == []


def test_ask_inline_wins_over_card_dispatch_even_with_kdb(tmp_path, capsys):
    """--ask-inline with a card-producing kdb wired must still ASK, not create
    a card — the two paths are mutually exclusive, and --ask-inline wins."""
    calls = []
    kdb = _FakeKdb()

    def _spy_ask(prompt, topic_id, client=None):
        calls.append(prompt)
        return _ROADMAP

    args = _build_args(tmp_path, apply=True, repo_files={"README.md": "# demo\n"})
    args.ask = _spy_ask
    args.ask_inline = True
    args._kdb = kdb
    rc = ing.cmd_ingest(args, [_project(repo=args._repo, topic=140, board="acme-board")])
    assert rc == 0
    assert len(calls) == 1
    assert kdb.created == []


# --------------------------------------------------------------------------- #
# No-ANSI regression — themed human view degrades to plain on a non-tty stdout
# --------------------------------------------------------------------------- #
# The RICH CLI group C card mandated a NO-ANSI regression per converted
# command: with stdout captured as a NON-tty StringIO, the themed Console must
# emit NO escape (\x1b) bytes — scripts / daemons parse this output and a
# single escaped byte in a pipe breaks a watcher. ingest's PROPOSED ROADMAP
# panel contains `- [x]` / `- [ ]` checklist brackets which must render
# LITERALLY (escaped, never swallowed as Rich style tags). No --json route.
import io as _io
import contextlib as _contextlib


def _no_ansi(fn):
    buf = _io.StringIO()
    with _contextlib.redirect_stdout(buf):
        fn()
    out = buf.getvalue()
    assert "\x1b[" not in out, f"ANSI escape found in piped output: {out!r}"
    assert "\x1b" not in out, f"ANSI escape found in piped output: {out!r}"
    return out


class TestIngestNoAnsi:
    """`ingest`'s PROPOSED ROADMAP panel is plain on a non-tty stdout, and the
    `- [x]` / `- [ ]` checklist marks render literally (not swallowed as Rich
    style tags)."""

    def test_proposed_roadmap_is_plain_and_marks_literal(self, tmp_path):
        args = _build_args(tmp_path, repo_files={"README.md": "# demo\nlocal readme"})
        out = _no_ansi(lambda: ing.cmd_ingest(
            args, [_project(repo=args._repo, topic=140)]))
        assert "PROPOSED ROADMAP" in out
        assert "auth-hardening" in out and "billing" in out
        # checklist brackets survive escaping, render as literal `- [x]` / `- [ ]`
        assert "- [x]" in out and "- [ ]" in out
        # wrapper prose is NOT presented (only the clean extracted roadmap)
        assert "Here is your roadmap proposal" not in out
        assert "```" not in out

