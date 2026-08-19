"""ingest.py — `flightdeck ingest <project> [--limit N] [--apply]`.

Drafts a project's ROADMAP.md from what ALREADY exists. The operator runs 8
live projects with YEARS of context and hand-written roadmaps for none of them;
this command turns the context flightdeck can already see into a first roadmap.

It GATHERS context from three LOCAL sources, in order of trust:

  a. ``~/.hermes/skills/**/references/*.md`` — Hermes' own distilled project
     notes. THE BEST SOURCE: curated understanding, not raw chat. We select the
     skill references whose path or content names the project, and read them
     via an injectable ``_read_refs`` seam.
  b. the project's repo — ``README.md``, ``docs/*.md``, and
     ``git log --oneline -n 200`` (subject lines describe what was actually
     built), via an injectable ``_run`` runner.
  c. the project's Telegram topic, last N messages (default 200), via the
     EXISTING ``flightdeck.core.telegram.read_messages`` — never a Telethon
     session directly, the session is single-writer.

Any source that is missing or unreadable is REPORTED and skipped; one missing
source never aborts the draft, and a source is never pretended to have been
read when it was not.

It then ASKS the cluster orchestrator (reusing ``decompose._default_ask`` — the
existing send+read seam; we do NOT write a second sender) to synthesise that
context into the EXACT ROADMAP.md format ``core/roadmap.parse_roadmap`` handles:

    # Subproject: <name>
    ## Milestone: <title> <!-- id: <stable-slug> -->
    status: now|next|later
    - [x] something already delivered
    - [ ] something still open

The gathered context is NOT pushed through the chat channel. Telegram caps one
message at 4096 characters and a large project's context blows far past that —
the live failure (2026-08-10) gathered correctly, blew past the cap, and polled
900s for a reply that could not come. So ingest stages the context to a SCRATCH
file ``~/.flightdeck/ingest-context-<project>.md`` (overwritten each run) and
sends only a SHORT pointer prompt (well under 3000 chars) naming that file's
ABSOLUTE path — the orchestrator runs on this same host and has file tools, so
a path is enough. ``send_message`` enforces the 4096 cap loudly, so any future
oversize send is impossible to miss.

By DEFAULT (N11) ingest does NOT ask synchronously at all. ``--apply`` routes
the synthesis through a KANBAN CARD on the project's board instead: ingest
stages the context file, then creates a card whose body tells a fleet worker to
read that file and write ``docs/ROADMAP.draft.md`` in the exact parseable
format — no multi-token prefill through the orchestrator span, no terminal
block. ``--ask-inline`` opts back into TODAY's synchronous send+read behaviour
for small projects, so nothing is lost. The card dispatch and the synchronous
ask never both run: the default path creates the card, ``--ask-inline`` asks.

Items already delivered must be ticked ``[x]`` — the git log is the evidence
for what shipped.

Output is a PROPOSAL: print the drafted markdown. ``--apply`` writes it, and
NEVER overwrites an existing ROADMAP.md — if one exists, write
``docs/ROADMAP.draft.md`` beside it and say so. Losing a hand-written roadmap
to a generated one is unacceptable.

    REPO: ~/dev/flightdeck
    CONTRACTS: docs/DESIGN.md, docs/FEATURES-2.md
    CLI WIRING: cli.py auto-discovers this module via build_subparser + run.
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
from pathlib import Path

from ..core import git_state, kanban, registry, roadmap, telegram
from ..core.telegram import TelegramError, TopicLockedError
from .decompose import NoReplyError, _default_ask

# How many git-log subject lines to gather (a hard cap; a longer log is
# truncated and the truncation is reported in the gather report -- a single
# enormous prompt is what produced the unbounded nine-minute stream).
_GIT_LOG_N = 150

# How many skill-reference files may reach the prompt (a hard cap; more are
# truncated and the truncation is reported, e.g. "ok (40 of 63 files)").
_SKILL_REF_MAX = 40

# Default bound on the orchestrator call, seconds, before ingest gives up and
# writes nothing. The live run streamed for nine minutes with no bound at all.
_DEFAULT_TIMEOUT = 300

# Where ingest stages the gathered context so the orchestrator can read it off
# the host instead of receiving it through Telegram's 4096-char channel. It is
# a SCRATCH artifact: overwritten on every run, never a source of truth.
_DEFAULT_CONTEXT_DIR = "~/.flightdeck"

# A roadmap region begins at the first line that opens a subproject or a
# milestone. Everything above it is prose preamble to strip, never to parse.
_ROADMAP_START_RE = re.compile(r"^(?:#\s*Subproject:|##\s*Milestone:)")
# A markdown code-fence line: ``` or ```markdown etc. (no content, optional id).
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*$")


class OrchestratorTimeout(Exception):
    """The orchestrator did not reply within the --timeout bound."""


def _ask_with_timeout(ask, prompt, topic_id, client, timeout):
    """Call ``ask``, aborting after ``timeout`` seconds.

    Runs the injectable ask seam in a worker thread so a slow (or never-ending)
    orchestrator reply cannot hold ingest open indefinitely. Returns the draft
    string, or raises :class:`OrchestratorTimeout` when ``timeout`` elapses
    first. The timed-out worker thread is a daemon, so it never blocks process
    exit. ``timeout <= 0`` (or None) means "no bound" -- call directly.
    """
    if timeout is None or timeout <= 0:
        return ask(prompt, topic_id, client)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest-ask")
    try:
        fut = executor.submit(ask, prompt, topic_id, client)
        try:
            return fut.result(timeout=timeout)
        except _FutureTimeout:
            raise OrchestratorTimeout(timeout) from None
    finally:
        # Do NOT wait on the daemon worker: finish as soon as the timeout (or
        # the draft) is decided, and let the orphaned thread die on its own.
        executor.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------------- #
# Source A — Hermes' own distilled skill references (~/.hermes/skills)
# --------------------------------------------------------------------------- #


def _skills_root() -> str:
    """Resolve the Hermes skills root, honouring a ``$HOME`` override.

    On macOS ``os.path.expanduser`` consults the password-database rather than
    ``$HOME``, so tests that monkeypatch ``HOME`` to point at a fixture would
    silently still read the real skills. Prefer ``$HOME`` when set (as tests
    do), else the conventional ``~/.hermes/skills``.
    """
    home = os.environ.get("HOME")
    if home:
        return os.path.join(home, ".hermes", "skills")
    return os.path.expanduser("~/.hermes/skills")


def _mentions_project(project, path, text) -> bool:
    """True when the skill reference's path or content names the project.

    Matching is on the project's registry name (case-insensitive) against both
    the file's path (a skill dir is often named after the project) and its
    content (the reference may be about the project without the dir carrying
    the name). Only references that name the project are selected — a random
    reference is never pulled in.
    """
    hay = f"{path}\n{text}".lower()
    return project.name.lower() in hay


def _default_read_refs(project) -> tuple[str, str | None]:
    """Concatenate the skill references that name ``project``.

    Returns ``(content, report)``. Walks ``~/.hermes/skills/*/references/*.md``,
    selects those whose path or content mentions the project name, and returns
    their markdown joined with headers, capped at ``_SKILL_REF_MAX`` files —
    a single enormous context is what produced the unbounded stream. The report
    states how many of how many were used, so truncation is visible
    (e.g. ``ok (40 of 63 files)``). A missing skills root, or no matching
    references, returns ``("", report)`` — the caller reports the source as
    unavailable rather than pretending it had content. This is the injectable
    ``_read_refs`` seam; tests stub it so the suite never reads the real
    ``~/.hermes/skills``.
    """
    root = Path(_skills_root())
    if not root.is_dir():
        return "", "EMPTY (0 files): no skill references name this project (skills root missing)"
    selected: list[tuple[Path, str]] = []
    for path in sorted(root.glob("*/references/*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, IOError, UnicodeDecodeError):
            continue  # an unreadable reference is skipped, not fatal
        if _mentions_project(project, path.name, text):
            selected.append((path, text))
    if not selected:
        return "", "EMPTY (0 files): no skill references name this project"
    total = len(selected)
    used = selected[:_SKILL_REF_MAX]
    blocks = [f"### {path.relative_to(root)}\n{text}" for path, text in used]
    content = "\n\n".join(blocks)
    if total > _SKILL_REF_MAX:
        report = f"ok ({_SKILL_REF_MAX} of {total} files)"
    else:
        report = f"ok ({total} files)"
    return content, report


# --------------------------------------------------------------------------- #
# Source B — the project's repo (README, docs, git log)
# --------------------------------------------------------------------------- #


def _default_read(path: str) -> str | None:
    """Read a UTF-8 text file, or None when missing/unreadable.

    The injectable ``_read`` seam: every file read in this command goes through
    it so tests stub reads instead of touching the real filesystem. None is the
    "not read" signal — it is reported, never silently swallowed.
    """
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, IOError, UnicodeDecodeError):
        return None


def _gather_repo(project, *, read=None, run=None) -> tuple[str, str | None]:
    """The project repo's docs + git log as one block.

    Returns ``(content, report)`` where ``report`` always states WHAT WAS READ
    with counts (e.g. ``ok (README.md, 3 docs, 150 commits)``); a repo that
    yielded nothing renders ``EMPTY (0 files)``. README.md, docs/*.md and the
    last ``_GIT_LOG_N`` commit subjects are each included when present; any
    that are missing are noted in the report but never abort the draft, and a
    git log longer than ``_GIT_LOG_N`` is truncated (with truncation reported).
    """
    if read is None:
        read = _default_read
    repo = project.repo
    parts: list[str] = []
    missing: list[str] = []
    notes: list[str] = []
    got: list[str] = []  # what-was-read summary pieces, e.g. ["README.md", "3 docs", "150 commits"]

    readme = os.path.join(repo, "README.md")
    readme_text = read(readme)
    if readme_text:
        parts.append(f"### {project.name}/README.md\n{readme_text}")
        got.append("README.md")
    else:
        missing.append("README.md")

    docs_dir = os.path.join(repo, "docs")
    docs_idx: list[str] = []
    if os.path.isdir(docs_dir):
        names = sorted(n for n in os.listdir(docs_dir) if n.endswith(".md"))
        if names:
            for n in names:
                text = read(os.path.join(docs_dir, n))
                if text:
                    parts.append(f"### {project.name}/docs/{n}\n{text}")
                    docs_idx.append(n)
            if not docs_idx:
                missing.append("docs/*.md (no readable markdown)")
        else:
            missing.append("docs/*.md (no markdown files)")
    else:
        missing.append("docs/ dir")
    if docs_idx:
        got.append(f"{len(docs_idx)} docs")

    # Probe one line past the cap so a longer log is detected and reported as
    # truncated, then keep only the first _GIT_LOG_N subjects in the prompt.
    log = _git_log(repo, _GIT_LOG_N + 1, run=run)
    commits = 0
    if log:
        log_lines = [ln for ln in log.splitlines() if ln.strip()]
        truncated = len(log_lines) > _GIT_LOG_N
        shown = log_lines[:_GIT_LOG_N]
        commits = len(shown)
        parts.append(f"### git log --oneline -n {_GIT_LOG_N}\n" + "\n".join(shown))
        got.append(f"{commits} commits")
        if truncated:
            notes.append(
                f"git log truncated (last {_GIT_LOG_N} of {len(log_lines)} subjects)"
            )
    else:
        missing.append("git log")

    if not got:
        return "", f"EMPTY (0 files): repo source unavailable ({'; '.join(missing)})"
    report = f"ok ({', '.join(got)})"
    if missing or notes:
        report += " — " + "; ".join(missing + notes)
    return "\n\n".join(parts), report


def _git_log(repo: str, n: int, run=None) -> str:
    """The last ``n`` ``git log --oneline`` lines, or '' when unavailable."""
    cp = git_state._dispatch(
        ["git", "log", "--oneline", "-n", str(n)], repo, run
    )
    if cp.returncode != 0:
        return ""
    return cp.stdout


# --------------------------------------------------------------------------- #
# Source C — the project's Telegram topic (reuses core/telegram.read_messages)
# --------------------------------------------------------------------------- #


def _gather_topic(project, limit: int, client=None) -> tuple[str, str | None]:
    """The topic's last ``limit`` messages as one block.

    Returns ``(content, report)`` where ``report`` always states the count
    (e.g. ``ok (37 messages)``); a topic that yielded zero messages renders
    ``EMPTY (0 messages)`` so an empty read is unmistakable at a glance. Uses
    the EXISTING ``core/telegram.read_messages`` — never a Telethon session
    directly. A project with no topic, or a read failure, is reported and
    skipped; it never aborts the draft.
    """
    if project.topic is None:
        return "", "EMPTY (0 messages): project has no topic"
    try:
        msgs = telegram.read_messages(project.topic, n=limit, _client=client)
    except TelegramError as exc:
        return "", f"EMPTY (0 messages): telegram source unavailable: {exc}"
    # Trust no further than the cap: the daemon may ignore ``limit`` and return
    # MORE messages than asked for (the live run returned 309 for --limit 40).
    # Cap locally so the count REPORTED equals the count ACTUALLY used in the
    # prompt — a gather line that says 309 while the flag said 40 is the same
    # dishonesty this command exists to remove. read_messages is newest-last, so
    # ``[:limit]`` keeps the LAST (newest) ``limit`` messages.
    msgs = msgs[:limit]
    if not msgs:
        return "", "EMPTY (0 messages): topic returned no messages"
    lines = [f"- [{m.sender}] {m.text}" for m in msgs]
    return "\n".join(lines), f"ok ({len(msgs)} messages)"


# --------------------------------------------------------------------------- #
# Ask the orchestrator — reuse decompose's send+read seam (no second sender)
# --------------------------------------------------------------------------- #


def _context_file_path(project, context_dir: str | None = None) -> str:
    """The scratch file where this project's gathered context is staged.

    ``context_dir`` is the parent directory (default ``~/.flightdeck``); it is
    injectable so tests point it at tmp_path instead of touching the real home
    dir. The orchestrator reads this file off the host — the file does the work
    of carrying the context, not the chat channel.
    """
    base = context_dir if context_dir is not None else _DEFAULT_CONTEXT_DIR
    return os.path.join(os.path.expanduser(base), f"ingest-context-{project.name}.md")


def _write_context_file(path: str, context_blocks: list[tuple[str, str]]) -> str:
    """Write the gathered context blocks to the scratch file at ``path``.

    Overwrites ``path`` on every run (it is a scratch artifact, never a source
    of truth). Each block is written under its ``## label`` header in the same
    order it was gathered, so the orchestrator can read the whole context in
    one file rather than receiving it through Telegram's 4096-char channel.
    Returns the ABSOLUTE path that was written — this is what the short prompt
    names, and it must be resolvable by the orchestrator's own file tools.
    """
    abs_path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    sections = "\n\n".join(f"## {label}\n{content}" for label, content in context_blocks)
    Path(abs_path).write_text(sections, encoding="utf-8")
    return abs_path


def _build_prompt(project, context_path: str) -> str:
    """The SHORT pointer prompt handed to the orchestrator for the ROADMAP draft.

    The gathered context is NOT inlined here — it lives in the scratch file at
    ``context_path`` (written by :func:`_write_context_file`, alongside this
    prompt's construction). Telegram caps each message at 4096 characters, and
    a large project's context blows far past that; pushing it through the chat
    channel is exactly what the live failure proved — the send was rejected or
    truncated and ingest waited 900s for a reply that could not come. Instead
    this prompt names the file's ABSOLUTE path (the orchestrator runs on this
    same host and has file tools, so a path is enough) and keeps inline only the
    one small thing the orchestrator needs verbatim: the EXACT ROADMAP format
    ``core/roadmap.parse_roadmap`` understands, plus the demand that delivered
    items be ticked ``[x]``. The prompt stays comfortably under 3000 characters.
    """
    return f"""Draft a ROADMAP.md proposal for project {project.name!r}.

Read the gathered context from this file on the host:
{context_path}

That file holds context gathered from three sources, most trusted first:
1. Hermes' own distilled skill references (curated understanding).
2. the project's repository (README, docs, and the git log — the git log
   subject lines are the EVIDENCE for what was actually built and shipped).
3. the project's Telegram topic messages (raw conversation).

Open the file above. TURN ITS CONTEXT INTO A ROADMAP STRICTLY IN THIS EXACT
FORMAT (the only format flightdeck's parser understands):

# Subproject: {project.name}

## Milestone: <short title> <!-- id: <stable-slug> -->
status: now|next|later
- [x] something already delivered
- [ ] something still open

RULES:
- Every completed/delivered item MUST be ticked [- [x]] — the git log is the
  evidence for what shipped. Do not tick anything the context does not support.
- Still-open work is unticked [- [ ]].
- Give each milestone a stable slug id (lowercase, hyphens).
- Use status: now | next | later to order milestones by priority.
- You may use multiple milestones and subprojects if the context warrants it.
- Output ONLY the markdown roadmap. No commentary, no code fences, no preamble.
"""


# --------------------------------------------------------------------------- #
# N11 — dispatch the synthesis through a KANBAN CARD, not a synchronous ask
# --------------------------------------------------------------------------- #
#   The orchestrator span that feeds kanban workers serves ~2 concurrent
#   requests at ~25 tok/s; a ~50k-token prefill from the staged context cannot
#   land synchronously (measured: three live runs all died on the reply). So
#   by DEFAULT ingest does not ask in chat at all — it creates a CARD whose
#   body tells a fleet worker to read the staged context file locally and write
#   the roadmap. The worker reads the file in its worktree (no 4096-char chat
#   limit, no prefill through the orchestrator), gets the retry/heartbeat
#   machinery, and never blocks the operator's terminal.


def _build_card_body(project, context_path: str) -> str:
    """The worker-facing card body for the ROADMAP draft task.

    The BODY replaces the synchronous ask prompt. It is SELF-CONTAINED: it
    tells the worker where the staged context lives (its ABSOLUTE path, readable
    locally in the worktree — no 4096-char chat limit, no prefill through the
    orchestrator), what to produce, and the EXACT format
    ``core/roadmap.parse_roadmap`` understands. The worker writes
    ``docs/ROADMAP.draft.md`` (never overwrites a hand-written ROADMAP.md),
    commits it, and the operator sees it via ``flightdeck qa`` / ``standup``.
    """
    return f"""Draft a ROADMAP.md proposal for project {project.name!r} from gathered context.

Read the gathered context from this file (absolute path, local to this host):
{context_path}

That file holds context from three sources, most trusted first:
1. Hermes' own distilled skill references (curated understanding).
2. the project's repository (README, docs, git log — the git log subject
   lines are the EVIDENCE for what was actually built and shipped).
3. the project's Telegram topic messages (raw conversation).

STEPS:
1. Open the context file above with your file tools.
2. Turn its context into a roadmap STRICTLY IN THIS EXACT FORMAT (the only
   format flightdeck's parser, core/roadmap.parse_roadmap, understands):

# Subproject: {project.name}

## Milestone: <short title> <!-- id: <stable-slug> -->
status: now|next|later
- [x] something already delivered
- [ ] something still open

3. Write the roadmap to
   {project.repo}/docs/ROADMAP.draft.md
   Do NOT overwrite an existing ROADMAP.md — always write
   docs/ROADMAP.draft.md beside it.
4. Commit the change.

RULES:
- Every completed/delivered item MUST be ticked [- [x]] — the git log is the
  evidence for what shipped. Do not tick anything the context does not support.
- Still-open work is unticked [- [ ]].
- Give each milestone a stable slug id (lowercase, hyphens).
- Use status: now | next | later to order milestones by priority.
- Output ONLY the roadmap markdown in the file. No commentary, no code fences.

When the draft exists, `flightdeck qa` / `standup` will show it; the operator
closes this card when the draft is accepted.
"""


def _dispatch_card(project, context_path: str, args) -> int:
    """Route the synthesis to a kanban card on the project's board (N11).

    The DEFAULT path. The context file is already staged by the caller; this
    creates ONE card whose body tells a fleet worker to read it and write
    ``docs/ROADMAP.draft.md``. It never makes a synchronous orchestrator
    request — the point is to stop making a big synchronous ask at all.

    The card lands on ``project.board`` when set; a project with NO board gets
    Hermes' CURRENT board, and the command says so (\"no board for <project>;
    created on '<current>'\") so where the card landed is always visible — a
    silent fallback is how cards end up on the wrong board. This resolves the
    current board explicitly and never relies on Hermes' global current board,
    and never mutates it. The card's workspace is the board's ``default_workdir``
    (set to the project repo at board creation), so the worker gets a worktree
    there.

    Without ``--apply`` nothing is created: this is a dry-run that still stages
    the context (so the gather + staging are proven) and reports what a real
    run would create. Returns 0 on success, 2 on a config error.
    """
    if not getattr(args, "apply", False):
        print(
            f"[ingest] dry-run: no card created (context staged at {context_path}). "
            f"pass --apply to create a kanban card that will write "
            f"docs/ROADMAP.draft.md; --ask-inline to ask synchronously instead.",
            file=sys.stderr,
        )
        return 0
    kdb = getattr(args, "_kdb", None)
    # Resolve the target board UP FRONT via the shared resolver (same code path
    # as dispatch and decompose): the project's OWN board when set, else
    # Hermes' CURRENT board — and we SAY SO, because a silent fallback is how
    # cards end up on the wrong board.
    board, used_fallback = kanban.resolve_project_board(project, _kdb=kdb)
    if used_fallback:
        print(f"no board for {project.name}; created on '{board}'", file=sys.stderr)
    try:
        card_id = kanban.create_task(
            board,
            f"ingest: draft ROADMAP for {project.name}",
            body=_build_card_body(project, context_path),
            _kdb=kdb,
        )
    except kanban.KanbanError as exc:
        print(
            f"error: could not create the ingest card on board {board!r}: {exc}",
            file=sys.stderr,
        )
        return 2
    print(f"created ingest card {card_id} on board {board!r}.", file=sys.stderr)
    print(
        f"Ingest dispatched to card {card_id} (board {board!r}). "
        f"The ROADMAP draft will appear as docs/ROADMAP.draft.md when the card "
        f"completes; `flightdeck qa` / `standup` will show it."
    )
    return 0


# --------------------------------------------------------------------------- #
# Output — print the draft, and --apply writes it (never overwrites)
# --------------------------------------------------------------------------- #


def _write_draft(project, text: str) -> str:
    """Write the draft, never overwriting an existing ROADMAP.md.

    Resolves the target ``ROADMAP.md`` (project.roadmap or the default). When
    it already exists, writes ``docs/ROADMAP.draft.md`` BESIDE it and returns
    the draft path — a hand-written roadmap is never lost to a generated one.
    Otherwise writes the target itself and returns it. Returns the absolute
    path that was written.
    """
    target = os.path.join(project.repo, project.roadmap or "ROADMAP.md")
    if os.path.exists(target):
        draft = os.path.join(project.repo, "docs", "ROADMAP.draft.md")
        os.makedirs(os.path.dirname(draft), exist_ok=True)
        Path(draft).write_text(text, encoding="utf-8")
        return draft
    Path(target).write_text(text, encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# Command
# --------------------------------------------------------------------------- #


def _resolve_project(projects, name: str):
    """Return ``(project, None)`` or ``(None, error_string)`` (mirrors decompose)."""
    for proj in projects:
        if proj.name == name:
            return proj, None
    known = ", ".join(sorted(p.name for p in projects)) or "none"
    return None, f"unknown project: {name!r} (known projects: {known})"


def _print_report(report: str | None, label: str) -> None:
    """Print a per-source report line to stderr (always shown, even absent)."""
    if report is None:
        print(f"[ingest] {label}: ok", file=sys.stderr)
    else:
        print(f"[ingest] {label}: {report}", file=sys.stderr)


def _roundtrip(text: str, repo: str):
    """Parse ``text`` via ``core/roadmap.parse_roadmap`` through a temp file.

    The parser works on a PATH, not raw text. To validate a draft string we
    stage it at a throwaway file inside ``repo`` (tmp_path in tests), parse it,
    and delete the staging file — the parse itself never touches anything else.
    """
    import tempfile

    fd, path = tempfile.mkstemp(prefix=".ingest-", suffix=".md", dir=repo)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        return roadmap.parse_roadmap(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _unwrap_fence(text: str) -> str:
    """Strip a surrounding ``` markdown code fence from ``text``.

    Removes a leading fence line (````` ``` ```` `` or ````` ```markdown ```` ``)
    and a trailing fence line, when present. A fence with prose on either side
    is left to the region-extraction; this only handles the clean surround
    case, never "repairs" malformed content by guessing.
    """
    lines = text.splitlines()
    if lines and _FENCE_RE.match(lines[0].strip()):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def _extract_roadmap(raw: str) -> str:
    """Extract the roadmap region from a possibly-wrapped orchestrator reply.

    The reply is truncated at the first line that opens a subproject or a
    milestone (``^# Subproject:`` or ``^## Milestone:``) — everything above it
    is Preamble to strip, never to parse. A surrounding ``` code fence is then
    unwrapped (a closing fence line ends the region; prose after it is
    postamble, not part of the roadmap). Returns ``""`` when no roadmap-shaped
    line is found: a rejection per the acceptance rule, never an attempt to
    "repair" a reply by guessing.
    """
    lines = raw.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _ROADMAP_START_RE.match(line.strip()):
            start = i
            break
    if start is None:
        return ""
    region: list[str] = []
    for line in lines[start:]:
        if region and _FENCE_RE.match(line.strip()):
            break  # a closing fence line ends the roadmap region
        region.append(line)
    return _unwrap_fence("\n".join(region))


def _is_valid_roadmap(parsed) -> bool:
    """True when ``parsed`` has at least one milestone AND at least one item."""
    return bool(parsed.milestones) and any(m.total > 0 for m in parsed.milestones)


def _roadmap_accept(text: str) -> bool:
    """True when ``text`` already contains a roadmap region.

    The ``accept`` predicate passed to the ask seam (N8). It reports True ONLY
    when the growing reply has reached a roadmap-shaped line (``^# Subproject:``
    or ``^## Milestone:``) — i.e. an actual ANSWER, not the orchestrator's
    acknowledgement preamble. It reuses the SAME N3 extraction helper
    (:func:`_extract_roadmap`): a non-empty extraction means a region has begun.
    This is what lets the seam skip the \"I'll read the ingest context file\"
    ack and keep polling for the reply that actually contains the roadmap.
    """
    return bool(_extract_roadmap(text))


def _print_raw_reply(draft: str) -> None:
    """Print the first ~40 lines of the rejected reply as a diagnostic."""
    print("\nRAW REPLY (not a roadmap):", file=sys.stderr)
    print("-" * 60, file=sys.stderr)
    for line in draft.splitlines()[:40]:
        print(line, file=sys.stderr)
    if draft.count("\n") + 1 > 40:
        print("... (reply truncated for display)", file=sys.stderr)
    print("-" * 60, file=sys.stderr)


def cmd_ingest(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    """Draft a project's ROADMAP.md from existing context.

    Gathers the three sources, asks the orchestrator to synthesise them into
    the ROADMAP format, prints the draft proposal, and (with ``--apply``) writes
    it without overwriting an existing ROADMAP.md. Returns 0 on success, 2 on a
    config/usage error, 3 when the draft could not be produced (e.g. every
    source is empty so there is nothing to synthesise from, or no ask seam
    answered).
    """
    proj, err = _resolve_project(projects, args.project)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    assert proj is not None

    read_refs = getattr(args, "read_refs", None) or _default_read_refs
    read = getattr(args, "read", None) or _default_read
    run = getattr(args, "run", None)
    client = getattr(args, "client", None)
    limit = getattr(args, "limit", 200) or 200

    # Gather the three sources. Each is reported, and a missing one is skipped
    # — it never aborts the draft, and we never claim a source we did not read.
    # If EVERY source yields nothing, we do NOT ask: sending an empty prompt
    # and asking a model to invent a roadmap invites the fabricated reply the
    # N3 guard now rejects, so we say there is nothing to synthesise from.
    context_blocks: list[tuple[str, str]] = []
    any_content = False

    refs = read_refs(proj)
    # The default seam returns ``(content, report)``; an injected stub may
    # return a bare string. Normalise both so the report (incl. truncation
    # counts) is surfaced either way.
    if isinstance(refs, tuple):
        refs_content, refs_report = refs
    else:
        refs_content, refs_report = refs, None
    if refs_content:
        context_blocks.append(("Hermes skill references", refs_content))
        any_content = True
    if refs_report is None:
        # A bare-string stub: report ok when it contributed, else empty.
        refs_report = "ok" if refs_content else "EMPTY (0 files): no skill references name this project"
    _print_report(refs_report, "skill references")

    repo_content, repo_report = _gather_repo(proj, read=read, run=run)
    if repo_content:
        context_blocks.append(("project repository (README, docs, git log)", repo_content))
        any_content = True
    _print_report(repo_report, "project repository")

    topic_content, topic_report = _gather_topic(proj, limit, client=client)
    if topic_content:
        context_blocks.append(("Telegram topic (last %d messages)" % limit, topic_content))
        any_content = True
    _print_report(topic_report, "telegram topic")

    if not any_content:
        print(
            "error: every source is empty — nothing to synthesise a roadmap from.",
            file=sys.stderr,
        )
        return 3

    # N7: STOP pushing bulk context through the chat channel. Write the
    # gathered blocks to a scratch FILE the orchestrator reads off the host,
    # then send only a SHORT pointer prompt naming that file's absolute path.
    # The live run (2026-08-10) gathered correctly then died with "did not
    # reply within 900s" because the one-message prompt embedded all the
    # context and blew past Telegram's 4096-char limit — the send was rejected,
    # the orchestrator never saw a coherent prompt, and we polled in vain.
    context_path = _context_file_path(proj, getattr(args, "context_dir", None))
    _write_context_file(context_path, context_blocks)
    prompt = _build_prompt(proj, context_path)
    print(f"[ingest] staged context: {context_path}", file=sys.stderr)

    # N11: by DEFAULT the synthesis runs as a KANBAN CARD, not a synchronous
    # chat ask (a ~50k-token prefill through the orchestrator span cannot land
    # synchronously). --ask-inline opts back into today's send+read behaviour.
    # The two paths never both run: the default creates the card, --ask-inline
    # asks. The card path returns here — no synchronous orchestrator call, no
    # terminal block.
    if not getattr(args, "ask_inline", False):
        return _dispatch_card(proj, context_path, args)

    # ---- --ask-inline: the synchronous path (today's behaviour, preserved) ----
    ask = getattr(args, "ask", None)
    using_default_ask = ask is None
    timeout = getattr(args, "timeout", _DEFAULT_TIMEOUT) or _DEFAULT_TIMEOUT
    if using_default_ask:
        # Thread the caller's --timeout INTO the default ask seam so ONE flag
        # governs the whole wait. _default_ask polls the topic with its OWN
        # internal deadline (default _DEFAULT_ASK_TIMEOUT = 300); left untouched
        # that inner bound fires BEFORE --timeout and the flag is ignored — the
        # live 300s-vs-420s failure. Binding it here makes _default_ask raise
        # NoReplyError("...within {N}s") quoting the value actually used.
        # Also pass the N8 `accept` predicate (reuses the N3 extraction helper)
        # so the seam KEEPS POLLING past the orchestrator's acknowledgement
        # preamble and only returns when the reply actually contains a roadmap
        # region — the live 2026-08-10 failure captured the "I'll read the
        # ingest context file" ack and stopped.
        ask = functools.partial(
            _default_ask, timeout=timeout, accept=_roadmap_accept
        )
    # The default ask reaches the orchestrator THROUGH the project's topic. A
    # project with no topic can only be asked via an injected seam; with the
    # default we must say so rather than posting to a None topic id.
    if using_default_ask and proj.topic is None:
        print(
            f"error: project {args.project} has no topic; cannot reach the "
            f"orchestrator to draft a roadmap. run: flightdeck project repair {args.project}",
            file=sys.stderr,
        )
        return 2
    try:
        draft = _ask_with_timeout(ask, prompt, proj.topic, client, timeout)
    except OrchestratorTimeout:
        print(
            f"error: the orchestrator did not reply within {timeout}s (--timeout); "
            f"nothing written.",
            file=sys.stderr,
        )
        return 3
    except NoReplyError as exc:
        print(f"error: {exc}; nothing written.", file=sys.stderr)
        return 3
    except TopicLockedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "hint: another process is probably holding the ~/.hermes-tg session; "
            "wait a moment and retry.",
            file=sys.stderr,
        )
        return 2
    except TelegramError as exc:
        print(f"error: could not reach the orchestrator: {exc}", file=sys.stderr)
        return 2

    if not draft or not draft.strip():
        print("error: the orchestrator returned no roadmap draft.", file=sys.stderr)
        return 3

    # VALIDATE BEFORE PRESENTING. Pull the roadmap region out of any prose /
    # code-fence wrapper, then parse it with the SAME parser flightdeck ships.
    # Accept ONLY when it yields at least one milestone AND at least one item.
    # Anything else is a rejection: never printed under PROPOSED ROADMAP, never
    # written, and the raw reply is surfaced under RAW REPLY as a diagnostic so
    # the operator can see what the orchestrator actually sent back.
    extracted = _extract_roadmap(draft)
    if not extracted:
        print("error: the orchestrator's reply was not a roadmap.", file=sys.stderr)
        _print_raw_reply(draft)
        return 3
    try:
        parsed = _roundtrip(extracted, proj.repo)
        if not parsed.present or not _is_valid_roadmap(parsed):
            print("error: the orchestrator's reply was not a roadmap.", file=sys.stderr)
            _print_raw_reply(draft)
            return 3
    except roadmap.DuplicateMilestoneIdError as exc:
        print("error: the orchestrator's reply was not a roadmap.", file=sys.stderr)
        print(f"      duplicate milestone id: {exc}", file=sys.stderr)
        _print_raw_reply(draft)
        return 3

    # ACCEPTED. Present the CLEAN extracted roadmap (wrapper/preamble stripped)
    # under PROPOSED ROADMAP, with the parse evidence surfaced.
    _print_parse_evidence(parsed)

    print(f"\n# PROPOSED ROADMAP for {proj.name} (project {args.project})")
    print("-" * 60)
    print(extracted)
    print("-" * 60)

    if not args.apply:
        print("dry-run: nothing written. pass --apply to write it.", file=sys.stderr)
        return 0

    written = _write_draft(proj, extracted)
    if written.endswith("ROADMAP.draft.md"):
        print(
            f"note: {os.path.join(proj.repo, proj.roadmap or 'ROADMAP.md')} already "
            f"exists; kept it and wrote the draft beside it: {written}",
            file=sys.stderr,
        )
    else:
        print(f"wrote {written}", file=sys.stderr)
    return 0


def _print_parse_evidence(parsed) -> None:
    """Print parsed milestone ids + item counts so the round-trip is visible."""
    print("[ingest] draft round-trips through parse_roadmap:", file=sys.stderr)
    for m in parsed.milestones:
        total = m.total
        done = m.done_count
        print(
            f"[ingest]   {m.id}  ({done}/{total} done, status={m.status})",
            file=sys.stderr,
        )


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "ingest",
        help="draft a project's ROADMAP.md from existing context (skills, repo, topic)",
        epilog="example: flightdeck ingest flightdeck --apply",
    )
    p.add_argument("project", help="project name in the registry")
    p.add_argument(
        "--limit",
        type=int,
        default=200,
        help="number of Telegram topic messages to gather (default: %(default)s)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=_DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help="how many seconds to wait for the orchestrator's reply before "
        "giving up and writing nothing (default: %(default)s)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="write the drafted roadmap (dry-run by default; an existing "
        "ROADMAP.md is never overwritten)",
    )
    p.add_argument(
        "--ask-inline",
        action="store_true",
        help="synthesise synchronously by asking the orchestrator in chat "
        "(the default dispatches a kanban card instead; use this for small "
        "projects that fit the old behaviour)",
    )
    p.set_defaults(func=cmd_ingest)


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: run ingest with injectable seams.

    Attaches the injectable seams (``read_refs``, ``read``, ``run``, ``client``,
    ``ask``, ``context_dir``, ``_kdb``) to ``args`` so tests drive the command
    against fixtures without touching Telegram, git, the real ``~/.hermes/skills``,
    the live kanban board, the network or the cluster. ``ask`` defaults to
    decompose's send+read seam (no second sender); ``context_dir`` defaults to
    ``~/.flightdeck``.
    """
    args.registry = registry_path
    args.read_refs = getattr(args, "read_refs", None)
    args.read = getattr(args, "read", None)
    args.run = getattr(args, "run", None)
    args.client = getattr(args, "client", None)
    args.ask = getattr(args, "ask", None)
    args.limit = getattr(args, "limit", 200)
    args.timeout = getattr(args, "timeout", _DEFAULT_TIMEOUT)
    args.context_dir = getattr(args, "context_dir", None)
    args.ask_inline = getattr(args, "ask_inline", False)
    args._kdb = getattr(args, "_kdb", None)
    projects = registry.load_registry(registry_path)
    return cmd_ingest(args, projects)
