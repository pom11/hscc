"""decompose.py — `flightdeck decompose <project> "<goal>"`.

Asks the cluster orchestrator (through the project's Telegram topic) to break
a goal into atomic cards, then GATES the proposal on card quality BEFORE
anything is created. Card quality is the single strongest predictor of whether
work lands (measured in docs/FEATURES-2.md): a card phrased abstractly stalled
1h45m with zero commits while the same task naming exact functions and line
numbers succeeded first try; every bundled card stalled, every single-concern
card landed.

So `decompose` refuses to create a card that would predictably stall:

- exactly **one concern** per card (one command, one module, one behaviour)
- a **VERIFY:** line — how the operator proves it works
- **concrete references** — file paths, function names, line numbers where the
  task touches an existing module. If the orchestrator cannot name them,
  flightdeck LOCATES them (grep over the repo) and injects them — that step is
  what turns a stalling card into a landing one.
- explicit **acceptance criteria**
- a place in the **dependency order** — a card that depends on a card not in
  the proposal, or on itself, has no place there.

Output is a PROPOSAL: numbered cards, their assignee suggestions, dependency
edges, and any rejections WITH THE REASON. `--apply` creates the passing cards
via core/kanban.py; without it nothing is created. A card that failed the gate
is never created — it is reported and the operator re-runs.

## The `ask` seam

The prompt is the shipped ``decompose`` template (G4's ask template machinery
in :mod:`flightdeck.core.templates`), rendered with the goal injected and the
project context auto-filled. The sender is NOT duplicated: we reuse
:mod:`flightdeck.core.telegram` (the existing transport) — send the rendered
prompt into the project's topic, then read the orchestrator's reply back from
the same topic.

The whole ask is one injectable seam (``args.ask``: ``(prompt, topic_id) ->
proposal_text``), defaulting to :func:`_default_ask` (send + read). Tests stub
it with a fixture, following the ``_client``/``_run`` house convention — no
test touches Telegram, git, the network or the cluster.

## Reference locating

``args.locate_refs`` is an injectable ``(body, repo_root) -> [references]``,
defaulting to :func:`_default_locate` (grep the repo). The gate calls it only
when a card that touches existing modules still has no concrete references.

    REPO: ~/dev/flightdeck
    CONTRACTS: docs/DESIGN.md, docs/FEATURES-2.md
    CLI WIRING: cli.py auto-discovers this module via build_subparser + run.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sys
import time as _time
from dataclasses import dataclass, field
from typing import Callable

from ..core import kanban, registry, roadmap, telegram, templates
from ..core.lint import referenced_modules
from ..core.telegram import Message, TelegramError, TopicLockedError

# A ``.py`` reference with a concrete anchor: ``mod.py:123`` or ``mod.py:func``.
_CONCRETE_REF_RE = re.compile(r"[A-Za-z0-9_./-]+\.py\s*:\s*[A-Za-z0-9_]+")
# Separators that indicate a concern covering MORE than one thing.
_MULTI_CONCERN_RE = re.compile(r"(?:,|\band\b|\b&\b|\+|[;|])", re.IGNORECASE)


class DecomposeError(Exception):
    """Base error for decompose (unparseable proposal, missing project, ...)."""


class ProposalParseError(DecomposeError):
    """The orchestrator's reply could not be parsed into a proposal."""


# --------------------------------------------------------------------------- #
# Proposal model
# --------------------------------------------------------------------------- #

@dataclass
class ProposedCard:
    """One card in the orchestrator's proposal, as parsed from the JSON."""

    id: int
    title: str
    body: str
    concern: str
    verify: str
    acceptance: str
    references: list[str] = field(default_factory=list)
    assignee: str | None = None
    depends_on: list[int] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)

    @property
    def has_concrete_refs(self) -> bool:
        return any(_CONCRETE_REF_RE.search(r or "") for r in self.references)


# --------------------------------------------------------------------------- #
# Parsing the orchestrator's reply
# --------------------------------------------------------------------------- #

def _extract_json(raw: str) -> str:
    """Pull the outermost JSON object out of the orchestrator's reply.

    The reply may wrap the JSON in prose or a code fence; we find the first
    ``{`` and match braces to the closing ``}``. Columns/digits in the prose
    are ignored because we scan for a top-level object-start, not the first
    brace anywhere.
    """
    start = raw.find("{")
    if start == -1:
        raise ProposalParseError("no JSON object found in the orchestrator's reply")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start : i + 1]
    raise ProposalParseError("unbalanced JSON object in the orchestrator's reply")


def _to_pyint(value) -> int:
    """Normalise a JSON card id to ``int`` (json may give str from a ``1``)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ProposalParseError(f"card id is not an integer: {value!r}")


def parse_proposal(raw: str) -> list[ProposedCard]:
    """Parse the orchestrator's JSON reply into :class:`ProposedCard` objects.

    Validates that the document is a mapping with a ``cards`` list and that
    each card is a mapping carrying at least ``id`` and ``title``. Missing
    optional fields (assignee, references, depends_on, verify, acceptance,
    concern, body) default to empty rather than raising — the GATE then rejects
    a card that needs them. ``id`` and ``title`` are mandatory: a card without
    an identity is not a card and is reported, never silently dropped.
    """
    text = _extract_json(raw)
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProposalParseError(f"proposal is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ProposalParseError("proposal JSON root must be an object")
    cards = doc.get("cards")
    if not isinstance(cards, list):
        raise ProposalParseError("proposal has no 'cards' list")

    parsed: list[ProposedCard] = []
    for entry in cards:
        if not isinstance(entry, dict):
            raise ProposalParseError(f"proposal has a non-mapping card: {entry!r}")
        if "id" not in entry or "title" not in entry:
            raise ProposalParseError(f"card missing id/title: {entry!r}")
        parsed.append(
            ProposedCard(
                id=_to_pyint(entry["id"]),
                title=str(entry.get("title", "")),
                body=str(entry.get("body", "")),
                concern=str(entry.get("concern", "")),
                verify=str(entry.get("verify", "")),
                acceptance=str(entry.get("acceptance", "")),
                references=[str(r) for r in (entry.get("references") or [])],
                assignee=entry.get("assignee"),
                depends_on=[_to_pyint(d) for d in (entry.get("depends_on") or [])],
            )
        )
    return parsed


# --------------------------------------------------------------------------- #
# The gate — one function per rejection reason, all firing independently
# --------------------------------------------------------------------------- #

def _is_multi_concern(concern: str) -> bool:
    """True when a concern string names more than one thing.

    A single concern is a short phrase with no conjunction/separator. Multiple
    concerns announce themselves with a separator (``,``, ``and``, ``&``, ``+``,
    ``;``, ``|``). An empty concern is also treated as multi-concern (it owns
    nothing, so it owns more than one by accident) — the reason reads as a
    failure to isolate one.
    """
    if not concern or not concern.strip():
        return True
    return bool(_MULTI_CONCERN_RE.search(concern))


def _resolve_touched_modules(body: str, repo_root: str) -> list[str]:
    """Modules the card names that actually EXIST under ``repo_root``.

    ``referenced_modules`` is order-preserving and deduped. Only a module that
    resolves to a real file counts — a card touching an existing module needs
    concrete references; one that names a file that does not exist touches
    nothing and needs none.
    """
    touched: list[str] = []
    for mod in referenced_modules(body):
        if mod and os.path.isfile(os.path.join(repo_root, mod)):
            touched.append(mod)
    return touched


def _default_locate(body: str, repo_root: str) -> list[str]:
    """Grep the repo to find concrete ``path:line:func`` refs for the body.

    For each ``.py`` module the body names that exists under ``repo_root``,
    find the first top-level ``def``/``class`` anchor and emit
    ``module.py:line:name``. This is the step that turns a stalling card into a
    landing one — the orchestrator sometimes cannot name exact seams, so we
    locate them. I/O lives here; tests inject a stub.
    """
    refs: list[str] = []
    for mod in referenced_modules(body):
        path = os.path.join(repo_root, mod)
        if not os.path.isfile(path):
            continue
        anchor = None
        try:
            with open(path, encoding="utf-8") as f:
                for i, line in enumerate(f, start=1):
                    s = line.lstrip()
                    if s.startswith("def ") or s.startswith("class "):
                        name = s.split("(", 1)[0].split(":", 1)[0]
                        name = name.replace("def ", "").replace("class ", "").strip()
                        anchor = f"{mod}:{i}:{name}"
                        break
        except OSError:
            anchor = f"{mod}:?"
        if anchor:
            refs.append(anchor)
    return refs


def gate_card(
    card: ProposedCard,
    *,
    all_cards: list[ProposedCard],
    repo_root: str,
    locator=None,
) -> list[str]:
    """Return every rejection reason for ``card`` (empty list = PASS).

    Each rule is independent and fires on its own field.

    * multi-concern        : concern covers more than one thing
    * missing VERIFY       : no verification line
    * missing references   : touches an existing module but no concrete refs
                             survive the LOCATE step
    * missing acceptance   : no acceptance criterion
    * no dependency pos    : depends on a card not in the proposal (or self)

    ``locator`` is the injectable ``(body, repo_root) -> [references]``; it is
    only invoked when the card touches existing modules AND has no concrete
    refs. When it returns refs they are INJECTED into ``card.references`` (so
    they land in the created card) and the reference gate passes. This function
    mutates ``card.references`` only — it never touches the board.
    """
    reasons: list[str] = []

    if _is_multi_concern(card.concern):
        reasons.append("covers more than one concern")

    if not card.verify or not card.verify.strip():
        reasons.append("lacks a VERIFY: line")

    if not card.acceptance or not card.acceptance.strip():
        reasons.append("lacks acceptance criteria")

    touched = _resolve_touched_modules(card.body, repo_root)
    if touched and not card.has_concrete_refs:
        # The card touches real modules but names no concrete seam. LOCATE them.
        found = (locator if locator is not None else _default_locate)(card.body, repo_root)
        new_refs = [r for r in found if r not in card.references]
        if new_refs:
            card.references.extend(new_refs)
        if not card.has_concrete_refs:
            reasons.append("lacks concrete file/function references")

    if not _has_dependency_position(card, all_cards):
        reasons.append("no place in the dependency order")

    return reasons


def _has_dependency_position(card: ProposedCard, all_cards: list[ProposedCard]) -> bool:
    """True when ``card`` has a place in the dependency order.

    1. Every ``depends_on`` id must resolve to a *different* card present in the
       proposal.
    2. The whole proposal's dependency graph must be acyclic.
    A card whose dependencies dangle (refer to a card that does not exist) or
    that participates in a cycle has no place in the order.
    """
    present = {c.id for c in all_cards}
    if any(d not in present for d in card.depends_on):
        return False
    if any(d == card.id for d in card.depends_on):
        return False
    return not _graph_has_cycle({c.id: set(c.depends_on) for c in all_cards})


def _graph_has_cycle(deps: dict[int, set[int]]) -> bool:
    """Detect a cycle in the dependency graph ``{id: set(parent_ids)}``.

    Kahn's algorithm: repeatedly peel nodes with zero unresolved dependencies.
    If any node remains, the graph is cyclic.
    """
    indegree: dict[int, int] = {n: 0 for n in deps}
    for node in deps:
        for parent in deps[node]:
            if parent in indegree:
                indegree[node] += 1
            # A dangling parent is already caught by the caller's emptiness
            # check; we tolerate it here (it just contributes no edge).
    queue = [n for n, d in indegree.items() if d == 0]
    seen = 0
    while queue:
        n = queue.pop()
        seen += 1
        for other in deps:
            if n in deps[other]:
                indegree[other] -= 1
                if indegree[other] == 0:
                    queue.append(other)
    return seen != len(deps)


# --------------------------------------------------------------------------- #
# Rendering the prompt via the G4 ask template machinery
# --------------------------------------------------------------------------- #

def _render_prompt(
    project: registry.Project,
    goal: str,
    *,
    templates_home: str | None,
    run=None,
    list_cards=None,
) -> str:
    """Render the shipped ``decompose`` template with the goal injected.

    The template machinery (G4) fills the project context flightdeck already
    knows (repo, branch, HEAD, roadmap Now, open cards, verify command) and the
    ``goal`` override supplies the operator's target. This is the same renderer
    ``ask`` uses — nothing is duplicated here. The sender stays in the ask seam.
    """
    text = templates.show_template("decompose", home=templates_home)
    context = templates.gather_context(project, _run=run, _list_cards=list_cards)
    return templates.render_template(text, context, overrides={"goal": goal})


# --------------------------------------------------------------------------- #
# The ask seam
# --------------------------------------------------------------------------- #

# How many recent messages to read per poll. Generous so the reply (plus a
# multi-part split) stays inside the window even on a busy topic.
_ASK_READ_N = 100
# Seconds between polls. Capped by the remaining time to the deadline.
_ASK_POLL = 2
# Seconds of quiet AFTER a genuine fragment before we consider the reply
# complete. The bot splits long answers into "(1/2)", "(2/2)" parts posted back
# to back; returning the instant the first lands would truncate the answer.
_ASK_SETTLE = 3
# Default bound on how long we wait for a genuine reply before raising
# :class:`NoReplyError` — a day-old message must never be read as the answer.
_DEFAULT_ASK_TIMEOUT = 300

# Status/progress noise the orchestrator posts around its real answer. These
# are ack lines ("⏳ Working on it...", "✓ Context compaction done") that carry
# no answer content and must not be concatenated into the reply.
_NOISE_PREFIXES = ("⏳ Working", "✓ Context compaction", "✅ Context compaction")


class NoReplyError(TelegramError):
    """No genuine reply to the prompt arrived in the topic within the timeout.

    ``raw_reply`` is the concatenated fragments the seam COLLECTED before giving
    up (empty when nothing was seen). It is carried so the caller can surface
    the most recent raw reply as a diagnostic under ``RAW REPLY`` — the
    alternative to a bare count that makes a wrong-shaped answer diagnosable
    rather than indistinguishable from silence.
    """

    def __init__(self, message: str, raw_reply: str | None = None):
        super().__init__(message)
        self.raw_reply = raw_reply


def _msg_identity(m: Message) -> tuple:
    """A stable identity for one message: (timestamp, sender, text).

    Messages in a topic are immutable, so this tuple uniquely identifies a
    message for watermark correlation — a message whose identity was already
    present before the send is never the reply.
    """

    return (m.timestamp, m.sender, m.text)


def _is_noise(m: Message) -> bool:
    """True when a message is not an answer fragment (status/progress noise).

    A message the reader could not parse (sender ``(unparsed)``), an empty
    message, or a status/ack line (``⏳ Working``, ``✓ Context compaction``) is
    skippable noise — it carries no answer content. Everything else is a
    genuine fragment that belongs in the concatenated reply.
    """

    if m.sender == "(unparsed)":
        return True
    text = m.text or ""
    if not text.strip():
        return True
    return text.lstrip().startswith(_NOISE_PREFIXES)


def _default_ask(
    prompt: str,
    topic_id: int,
    _client=None,
    *,
    timeout: float = _DEFAULT_ASK_TIMEOUT,
    now=None,
    sleep=None,
    accept: Callable[[str], bool] | None = None,
) -> str:
    """Send the prompt into the topic and poll for a GENUINE reply to it.

    Reuses the existing telegram sender + reader (never duplicates them). The
    whole ask is one injectable seam; ``now``/``sleep`` are injected so tests
    drive the clock/sleep and never wait real time.

    The reply is CORRELATED to the prompt instead of being "the newest thing
    in the topic":

    1. WATERMARK — record every message identity already in the topic BEFORE
       sending. A message that predates the prompt is NEVER the reply. This is
       the fix for the live failure where a day-old chain-of-thought block was
       read as the answer because it happened to be the newest message.
    2. POLL — read the topic repeatedly until a genuine reply arrives (never
       once, immediately). Reading once is why a day-old message could win.
    3. SKIP noise — ``(unparsed)`` fragments, and status lines (``⏳ Working``,
       ``✓ Context compaction``, empty). Genuine fragments are CONCATENATED in
       order, so multi-part ``(1/2)``/``(2/2)`` answers are not truncated.
    4. TIMEOUT — if no genuine reply arrives within ``timeout`` seconds, raise
       :class:`NoReplyError` so the failure is honest instead of silently
       answering with old content. The reply is considered complete after
       ``settle`` quiet seconds following the last fragment, so a split answer
       is not cut short.
    5. ACCEPT — an optional ``accept(text)`` predicate. When given, keep
       polling past any non-matching message until ``accept`` returns True for
       the concatenated reply (re-tested as every fragment lands — a split
       answer may only satisfy it once all parts are present) or the timeout
       expires. Preamble and acknowledgements are skipped automatically. With
       ``accept`` given, ``(unparsed)`` continuation lines are also preserved
       into the concatenated reply, so a JSON answer wrapped in a ```json
       fence (whose tail reads as unparsed continuation) reaches the predicate
       and is extractable. On timeout the error reports how many messages were
       seen and rejected, so a wrong-shaped answer is diagnosable rather than
       silent.
    """
    now = now or _time.monotonic
    sleep = sleep or _time.sleep

    # 1. Watermark: every message already present before we send.
    msgs = telegram.read_messages(topic_id, n=_ASK_READ_N, _client=_client)
    watermark = {_msg_identity(m) for m in msgs}

    # 2. Send the prompt.
    telegram.send_message(topic_id, prompt, _client=_client)

    # 3. Poll for a genuine reply, concatenating fragments in order.
    collected: list[str] = []
    seen: set[tuple] = set()
    last_fragment = now()
    deadline = now() + timeout
    while True:
        remaining = deadline - now()
        if remaining <= 0:
            break  # hard timeout: no accepted reply within Ns
        msgs = telegram.read_messages(topic_id, n=_ASK_READ_N, _client=_client)
        new_this_poll = False
        # read_messages returns newest-last; walk oldest-first so fragments
        # concatenate in the order the bot posted them, and dedupe against
        # what we already collected (an old fragment re-read is skipped).
        for m in reversed(msgs):
            identity = _msg_identity(m)
            if identity in seen or identity in watermark:
                continue
            if m.text == prompt:
                # Our own just-sent prompt, not a reply to it.
                seen.add(identity)
                continue
            if _is_noise(m):
                # Not an answer fragment. ``(unparsed)`` continuation lines
                # carry no sender but may hold the tail of a fenced JSON reply
                # (the ```json fence, the JSON itself, the closing fence). When
                # `accept` is given we KEEP them so the predicate — and the
                # caller — see the full raw reply and can extract the JSON
                # despite the fence; without `accept` they stay noise (today's
                # behaviour).
                if accept is not None and m.sender == "(unparsed)":
                    seen.add(identity)
                    collected.append(m.text)
                    new_this_poll = True
                else:
                    seen.add(identity)
                continue
            seen.add(identity)
            collected.append(m.text)
            new_this_poll = True
        if not collected:
            pass  # no fragment yet; keep polling
        elif accept is not None:
            # ACCEPT seam: re-test the concatenated reply AS IT GROWS — a split
            # answer may only satisfy the predicate once every "(1/2)"/"(2/2)"
            # fragment has landed. This is what lets ingest skip the
            # orchestrator's "I'll read the ingest context file" preamble and
            # wait for the reply that actually ANSWERS. Never settle-break on a
            # non-matching reply: keep polling until accept passes or timeout.
            if accept("\n".join(collected)):
                return "\n".join(collected)
        elif new_this_poll:
            last_fragment = now()
        elif (now() - last_fragment) >= _ASK_SETTLE:
            break  # the reply is complete: quiet since the last fragment
        sleep(min(_ASK_POLL, remaining))

    # 4. An accepted reply or a clear failure — never old content.
    if not collected:
        raise NoReplyError(f"no reply in the topic within {timeout:g}s")
    # With `accept` given we only get here on a timeout: fragments arrived but
    # none shaped a reply that satisfied the predicate. Report how many were
    # seen and rejected so a wrong-shaped answer is diagnosable rather than
    # indistinguishable from silence.
    if accept is not None:
        n = len(collected)
        raise NoReplyError(
            f"no accepted reply within {timeout:g}s "
            f"({n} message{'s' if n != 1 else ''} seen)",
            raw_reply="\n".join(collected),
        )
    return "\n".join(collected)


def _proposal_accept(text: str) -> bool:
    """True when ``text`` already contains a parseable decompose proposal.

    The ``accept`` predicate passed to the ask seam (the decompose analogue of
    ingest's ``_roadmap_accept``). It reports True ONLY once the growing reply
    carries a JSON object of the shape decompose expects — a ``cards`` list of
    mapping entries with ``id``/``title``, i.e. anything :func:`parse_proposal`
    would accept. It REUSES :func:`parse_proposal` (which reuses
    :func:`_extract_json`), so the fence-stripping and shape rules are EXACTLY
    the ones the gate relies on — no second, looser parser. A prose preamble or
    acknowledgement that contains no such JSON is rejected and the seam keeps
    polling for the message that actually ANSWERS. This is the fix for the live
    failure where ``decompose`` grabbed the orchestrator's prose preamble and
    then failed because it carried no JSON.
    """
    try:
        parse_proposal(text)
        return True
    except ProposalParseError:
        return False


def _resolve_project(projects, name: str):
    """Return ``(project, None)`` or ``(None, error_string)`` (mirrors message.py)."""
    for proj in projects:
        if proj.name == name:
            return proj, None
    return None, (
        f"unknown project: {name!r} (check `flightdeck projects list`)"
    )


def _default_read_milestone(project, milestone_id: str):
    """Read a milestone's items from the project's ROADMAP.md as the goal.

    Returns ``(goal_text, None)`` on success or ``(None, error_string)``. An
    unknown milestone id is an error LISTING the ids that DO exist — never a
    silent empty decomposition. A roadmap with no matching milestone but other
    valid ids therefore reports them rather than decomposing nothing. This is
    the injectable ``args.read_milestone`` default; tests stub it.
    """
    r = roadmap.project_roadmap(project)
    if not r.present:
        return None, (
            f"no roadmap for project {project.name}: {r.path} does not exist"
        )
    m = r.milestone(milestone_id)
    if m is None:
        valid = ", ".join(sorted(x.name for x in r.milestones)) or "none"
        return None, (
            f"unknown milestone {milestone_id!r} for project {project.name} "
            f"(valid ids: {valid})"
        )
    if not m.items:
        return None, (
            f"milestone {milestone_id!r} for project {project.name} has no items"
        )
    items = "\n".join(f"- {it.text}" for it in m.items)
    return items, None



# --------------------------------------------------------------------------- #
# Command
# --------------------------------------------------------------------------- #

def _locked_message(exc: TopicLockedError) -> str:
    return (
        f"error: {exc}\n"
        "hint: another process is probably holding the ~/.hermes-tg session; "
        "wait a moment and retry."
    )


def _print_raw_reply(raw: str) -> None:
    """Print the first ~40 lines of a rejected raw reply as a diagnostic.

    Mirrors ingest's ``RAW REPLY`` block (same shape) but names the missing
    shape — here a JSON proposal, not a roadmap. A wrong-shaped answer is thus
    diagnosable rather than indistinguishable from silence.
    """
    print("\nRAW REPLY (no JSON proposal):", file=sys.stderr)
    print("-" * 60, file=sys.stderr)
    for line in raw.splitlines()[:40]:
        print(line, file=sys.stderr)
    if raw.count("\n") + 1 > 40:
        print("... (reply truncated for display)", file=sys.stderr)
    print("-" * 60, file=sys.stderr)


def cmd_decompose(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    """Decompose a goal: ask the orchestrator, gate the proposal, maybe apply.

    Returns 0 on a proposal where at least one card passed the gate; 2 on
    config/asking errors; 3 when every card was rejected (nothing created,
    non-zero so a caller knows nothing happened).
    """
    proj, err = _resolve_project(projects, args.project)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    assert proj is not None  # _resolve_project returns (None, err) on failure
    if proj.topic is None:
        print(
            f"error: project {args.project} has no topic; "
            f"run: flightdeck project repair {args.project}",
            file=sys.stderr,
        )
        return 2
    # Resolve the board UP FRONT: the project's OWN board (registry ``board``),
    # falling back to Hermes' current board when the project has none — and we
    # SAY SO, because a silent fallback is how cards end up on the wrong board.
    board, used_fallback = kanban.resolve_project_board(proj, _kdb=getattr(args, "_kdb", None))
    if used_fallback:
        print(
            f"no board for {args.project}; created on '{board}'",
            file=sys.stderr,
        )

    # Resolve the goal: a free-text ``goal`` positional OR a ``--milestone``
    # id whose items are read from ROADMAP.md. They are mutually exclusive,
    # and one of them is required — this is a usage error, caught up front so
    # a bad invocation is reported before anything is sent.
    milestone_id = getattr(args, "milestone", None)
    goal_text = args.goal
    if milestone_id and goal_text:
        print(
            "error: --milestone and a free-text goal are mutually exclusive; "
            "pass one or the other.",
            file=sys.stderr,
        )
        return 2
    if not milestone_id and not goal_text:
        print(
            "error: pass a goal or --milestone <id> (one or the other).",
            file=sys.stderr,
        )
        return 2
    if milestone_id:
        read_milestone = getattr(args, "read_milestone", None) or _default_read_milestone
        goal_text, err = read_milestone(proj, milestone_id)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2
    assert goal_text is not None  # one of milestone/goal was required above

    # Render the decompose prompt through the G4 template machinery (project
    # context auto-filled, goal injected). An unfilled slot is an error and
    # sends NOTHING — a literal {{goal}} is never posted.
    try:
        prompt = _render_prompt(
            proj,
            goal_text,
            templates_home=getattr(args, "templates_home", None),
            run=getattr(args, "run", None),
            list_cards=getattr(args, "list_cards", None),
        )
    except templates.UnfilledSlotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except templates.UnknownTemplateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    ask = getattr(args, "ask", None)
    using_default_ask = ask is None
    if using_default_ask:
        # Thread the caller's --timeout INTO the default ask seam so ONE flag
        # governs the whole wait — the same defect already fixed for ingest (N6)
        # must not be re-introduced here. Bind the N15 `accept` predicate too
        # (REUSING parse_proposal/_extract_json) so the seam KEEPS POLLING past
        # the orchestrator's prose preamble and only returns once a message
        # actually carries a parseable JSON proposal — the live 2026-08-11
        # failure grabbed the preamble and then failed for lack of JSON.
        ask = functools.partial(
            _default_ask,
            timeout=getattr(args, "timeout", _DEFAULT_ASK_TIMEOUT) or _DEFAULT_ASK_TIMEOUT,
            accept=_proposal_accept,
            now=getattr(args, "now", None),
            sleep=getattr(args, "sleep", None),
        )
    try:
        raw = ask(prompt, proj.topic, getattr(args, "client", None))
    except NoReplyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raw_reply: str | None = getattr(exc, "raw_reply", None)
        if raw_reply:
            _print_raw_reply(raw_reply)
        return 2
    except TopicLockedError as exc:
        print(_locked_message(exc), file=sys.stderr)
        return 2
    except TelegramError as exc:
        print(f"error: could not reach the orchestrator: {exc}", file=sys.stderr)
        return 2

    try:
        cards = parse_proposal(raw)
    except ProposalParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    locator = getattr(args, "locate_refs", None)
    repo_root = getattr(args, "repo_root", None) or proj.repo

    # Gate every card. Injecting located refs mutates card.references in place.
    rejected: list[ProposedCard] = []
    accepted: list[ProposedCard] = []
    for card in cards:
        reasons = gate_card(
            card,
            all_cards=cards,
            repo_root=repo_root,
            locator=locator,
        )
        if reasons:
            card.rejection_reasons = reasons
            rejected.append(card)
        else:
            accepted.append(card)

    # ------------------------------------------------------------------ #
    # Print the proposal
    # ------------------------------------------------------------------ #
    print(f"PROPOSAL: {goal_text}")
    print(f"(project {args.project}, board {board}, {len(cards)} card(s))")
    if accepted:
        print("\nACCEPTED CARDS:")
        for card in accepted:
            print(f"  {card.id}. {card.title}")
            if card.assignee:
                print(f"       assignee: {card.assignee}")
            if card.depends_on:
                print(f"       depends on: {_dep_labels(card.depends_on, cards)}")
            for ref in card.references:
                print(f"       ref: {ref}")
    if rejected:
        print("\nREJECTED CARDS:")
        for card in rejected:
            print(f"  {card.id}. {card.title}")
            print(f"       REJECTED: {'; '.join(card.rejection_reasons)}")

    # Dependency edges summary.
    edges = [(c.id, d) for c in accepted for d in c.depends_on if d in {a.id for a in accepted}]
    if edges:
        print("\nDEPENDENCY EDGES:")
        for (child, parent) in edges:
            print(f"  {child} -> {parent}")

    if args.apply:
        created: list[str] = []
        for card in accepted:
            try:
                new_id = kanban.create_task(
                    board,
                    card.title,
                    assignee=card.assignee,
                    body=_card_body(card, milestone=milestone_id),
                    workspace_kind="worktree",
                    workspace_path=repo_root,
                    _kdb=getattr(args, "_kdb", None),
                )
            except kanban.KanbanError as exc:
                print(
                    f"error: could not create card {card.id} ({card.title}) "
                    f"on board {board!r}: {exc}",
                    file=sys.stderr,
                )
                continue
            created.append(new_id)
            print(f"created card {new_id}: {card.title}")
        if not created:
            print("error: --apply created nothing (all cards rejected or failed).", file=sys.stderr)
            return 3
        print(f"created {len(created)} card(s) on board {board!r}.")
    else:
        print("\ndry-run: nothing was created. pass --apply to create the ACCEPTED cards.")
        if not accepted:
            print("no card passed the gate; nothing would be created.", file=sys.stderr)

    # Never created a card that failed the gate — report and let the operator
    # re-run. If EVERY card failed, this is a hard failure signal.
    if not accepted:
        print("\nno accepted cards: nothing was created.", file=sys.stderr)
        return 3
    return 0


def _dep_labels(dep_ids: list[int], cards: list[ProposedCard]) -> str:
    labels = []
    by_id = {c.id: c.title for c in cards}
    for d in dep_ids:
        labels.append(f"{d} ({by_id.get(d, '?')})")
    return ", ".join(labels)


def _card_body(card: ProposedCard, milestone: str | None = None) -> str:
    """The final card body: the orchestrator's text plus any located refs.

    The base body already carries the VERIFY:/ACCEPTANCE: lines. If the gate
    located concrete references and injected them, append a ``REFERENCES:``
    section so the created card actually carries the seams that make it land.
    When decomposing a ``--milestone``, stamp ``MILESTONE: <id>`` into the body
    of every created card — that is what makes tracking automatic instead of a
    discipline the operator has to remember.
    """
    body = card.body
    if card.references and "REFERENCES:" not in body:
        ref_block = "\n".join(f"  - {r}" for r in card.references)
        body = f"{body.rstrip()}\n\nREFERENCES:\n{ref_block}\n"
    if milestone:
        body = f"{body.rstrip()}\n\nMILESTONE: {milestone}\n"
    return body


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "decompose",
        help="ask the cluster to break a goal into atomic cards, gated on card quality",
        epilog='example: flightdeck decompose flightdeck "make the release installable" --apply',
    )
    p.add_argument("project", help="project name in the registry")
    p.add_argument(
        "goal",
        nargs="?",
        default=None,
        help="the roadmap item / goal to decompose (omit when --milestone is used)",
    )
    p.add_argument(
        "--milestone",
        default=None,
        help="milestone id in the project's ROADMAP.md whose items become the goal "
        "(mutually exclusive with a free-text goal)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="create the ACCEPTED cards (dry-run by default; nothing is created without this)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=_DEFAULT_ASK_TIMEOUT,
        metavar="SECONDS",
        help="how many seconds to wait for the orchestrator's JSON proposal "
        "before giving up (default: %(default)s)",
    )
    p.set_defaults(func=cmd_decompose)


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: run decompose with injectable seams.

    Attaches the injectable seams (``ask``, ``locate_refs``, ``client``, and
    the render deps ``run`` / ``list_cards`` / ``templates_home``) to ``args``
    so tests drive the command against fixtures without touching Telegram, git,
    the network or the cluster. ``repo_root`` defaults to the project's repo
    path.
    """
    args.registry = registry_path
    args.client = getattr(args, "client", None)
    args.ask = getattr(args, "ask", None)
    args.locate_refs = getattr(args, "locate_refs", None)
    args.repo_root = getattr(args, "repo_root", None)
    args.read_milestone = getattr(args, "read_milestone", None)
    args.run = getattr(args, "run", None)
    args.list_cards = getattr(args, "list_cards", None)
    args.templates_home = getattr(args, "templates_home", None)
    args.now = getattr(args, "now", None)
    args.sleep = getattr(args, "sleep", None)
    args._kdb = getattr(args, "_kdb", None)
    projects = registry.load_registry(registry_path)
    return cmd_decompose(args, projects)
