"""Tests for decompose._default_ask — the ask seam's reply/prompt correlation.

``_default_ask`` (shared by ``decompose`` and ``ingest``) SENDS a prompt into a
project's topic and reads the orchestrator's reply back. Historically it read
\"the newest thing in the topic\" with no correlation, and a LIVE failure
returned a day-old chain-of-thought block as the \"reply\" (2026-08-09 16:41,
kanban-worker troubleshooting + memory-entry character counts) because it
happened to be the newest message.

These tests drive ``_default_ask`` directly against a stub MCP client whose
feed is served as a sequence of snapshots (each successive ``telegram_read``
serves the next snapshot, simulating replies arriving over time), plus an
injected clock/sleep so no test takes real time.

The contract under test: a message OLDER than the send watermark is never the
reply; a message that arrives after the send is; multi-part \"(1/2)\"/\"(2/2)\"
fragments concatenate in order; ``(unparsed)`` and status lines are skipped;
a timeout with no genuine reply errors clearly and returns nothing.
"""

from flightdeck.commands import decompose as dec
from flightdeck.core.telegram import TelegramError

# The prompt as it appears in the topic AFTER being sent: ``[ts] You: <text>``
# is the shape read_messages parses, and its text must equal the real ``prompt``
# argument so the seam can tell its own post from a reply.
_PROMPT = "PROMPT"
_PROMPT_LINE = "[2026-08-10 10:04] You: PROMPT"

_OLD_COT = "[2026-08-09 16:41] Hermes: thinking about kanban worker troubleshooting and memory-entry character counts"
_REPLY = "[2026-08-10 10:05] Hermes: {\"cards\": [{\"id\": 1, \"title\": \"do it\"}]}"


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #

class SequentialFeed:
    """MCP client stub: serves ``snapshots[k]`` on the k-th telegram_read.

    ``snapshots[0]`` is the topic BEFORE the prompt is sent (what the watermark
    records); later snapshots simulate the orchestrator's reply arriving over
    time. ``telegram_send`` records the call and returns the daemon's ack. Each
    post-send snapshot includes the sent prompt (newest end) because the real
    topic keeps it — the seam must skip it, never mistake it for a reply.
    """

    def __init__(self, snapshots):
        self.snapshots = [list(s) for s in snapshots]
        self._reads = 0
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if tool_name == "telegram_send":
            return "Sent."
        if tool_name == "telegram_read":
            idx = min(self._reads, len(self.snapshots) - 1)
            self._reads += 1
            return "\n".join(self.snapshots[idx])
        raise AssertionError(f"unknown tool {tool_name!r}")


class Clock:
    """An injectable clock: ``now()``/``sleep()`` advance ``self.t`` in step."""

    def __init__(self, start=100.0):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def _ask(snapshots, *, prompt=_PROMPT, timeout=300, start=100.0, accept=None):
    """Run _default_ask against the given snapshot feed with an injected clock.

    Returns the reply string on success, or the raised TelegramError. Also
    returns the feed and clock for assertions. ``accept`` is passed straight
    through to the seam (None = today's first-genuine-message behaviour).
    """
    feed = SequentialFeed(snapshots)
    clock = Clock(start)
    try:
        out = dec._default_ask(
            prompt, 140, feed, timeout=timeout, now=clock.now, sleep=clock.sleep,
            accept=accept,
        )
        return out, feed, clock
    except TelegramError as exc:
        return exc, feed, clock


# --------------------------------------------------------------------------- #
# Watermark correlation — the fix for the live failure
# --------------------------------------------------------------------------- #

def test_default_ask_never_returns_a_message_older_than_the_send_watermark():
    """A message predating the prompt is NEVER the reply (the live failure).

    Feed[0] (pre-send) already contains the day-old chain-of-thought block. It
    stays in the post-send snapshot too, so the old seam would have read it as
    the newest thing. With the watermark, it is excluded; no genuine reply
    ever arrives, so the ask times out rather than answering with stale content.
    """
    result, feed, _ = _ask(
        [
            [_OLD_COT],                        # watermark: day-old CoT present
            [_OLD_COT, _PROMPT_LINE],          # after send: old CoT still newest
        ],
        timeout=5,
    )
    assert isinstance(result, dec.NoReplyError)
    assert "no reply in the topic within 5s" in str(result)


def test_default_ask_returns_a_new_message_after_the_watermark():
    """A message that arrives AFTER the send is returned as the reply."""
    result, feed, _ = _ask(
        [
            [_OLD_COT],                        # watermark before send
            [_OLD_COT, _PROMPT_LINE, _REPLY],  # reply arrives after the send
        ],
    )
    assert result == "{\"cards\": [{\"id\": 1, \"title\": \"do it\"}]}"


# --------------------------------------------------------------------------- #
# Multi-part concatenation, in order
# --------------------------------------------------------------------------- #

def test_default_ask_concatenates_multipart_fragments_in_order():
    """\"(1/2)\"/\"(2/2)\" parts arriving over time concatenate oldest-first."""
    part1_line = "[2026-08-10 10:06] Hermes: here is the first half (1/2)"
    part2_line = "[2026-08-10 10:06] Hermes: and the second half (2/2)"
    result, _, _ = _ask(
        [
            [],                                # empty topic before send
            [_PROMPT_LINE, part1_line],        # first poll: part 1 lands
            [_PROMPT_LINE, part2_line, part1_line],  # second poll: part 2 lands
        ],
    )
    assert result == "here is the first half (1/2)\nand the second half (2/2)"


def test_default_ask_concatenates_multipart_present_together():
    """Parts already present together (newest-last) concatenate in order."""
    # newest-LAST: TWO (newest) comes after ONE (older) in the feed.
    result, _, _ = _ask(
        [[], [_PROMPT_LINE, "[..] Hermes: TWO (2/2)", "[..] Hermes: ONE (1/2)"]]
    )
    assert result == "ONE (1/2)\nTWO (2/2)"


# --------------------------------------------------------------------------- #
# Noise skipping
# --------------------------------------------------------------------------- #

def test_default_ask_skips_unparsed_and_status_lines_keeps_the_answer():
    """(unparsed) fragments and \"⏳ Working\"/\"✓ Context compaction\" lines are
    skipped; the genuine answer is returned alone."""
    status = "[..] Hermes: ⏳ Working on the proposal..."
    compaction = "[..] Hermes: ✓ Context compaction done"
    unparsed = "[..] this-line-has-no-sender-colon"
    answer = "[..] Hermes: {\"cards\": []}"
    result, _, _ = _ask([[], [_PROMPT_LINE, status, compaction, unparsed, answer]])
    assert result == "{\"cards\": []}"


def test_default_ask_skips_noise_between_multipart_fragments():
    """Noise between (1/2) and (2/2) is dropped, and genuine parts still
    concatenate in order — noise must not truncate the stream."""
    result, _, _ = _ask(
        [[], [_PROMPT_LINE, "[..] Hermes: end (2/2)",
              "[..] Hermes: ⏳ Working...", "[..] Hermes: start (1/2)"]]
    )
    assert result == "start (1/2)\nend (2/2)"


# --------------------------------------------------------------------------- #
# Timeout — an honest failure, never old content
# --------------------------------------------------------------------------- #

def test_default_ask_timeout_with_no_new_message_errors_and_returns_nothing():
    """No genuine reply within the timeout raises a clear NoReplyError."""
    result, feed, _ = _ask(
        [[_OLD_COT], [_OLD_COT, _PROMPT_LINE]],   # no reply ever arrives
        timeout=3,
    )
    assert isinstance(result, dec.NoReplyError)
    assert "no reply" in str(result)
    assert "within 3s" in str(result)


def test_default_ask_sends_through_the_client_and_returns_the_reply():
    """Ensures send+read flow through the injected client, and the reply wins."""
    feed = SequentialFeed([[_OLD_COT], [_OLD_COT, _PROMPT_LINE, _REPLY]])
    clock = Clock()
    out = dec._default_ask(_PROMPT, 140, feed, now=clock.now, sleep=clock.sleep)
    tools = [t for t, _ in feed.calls]
    assert tools.count("telegram_send") == 1
    assert tools.count("telegram_read") >= 2
    assert out == "{\"cards\": [{\"id\": 1, \"title\": \"do it\"}]}"


# --------------------------------------------------------------------------- #
# N7 — a failed send fails immediately, it does NOT poll for a reply
# --------------------------------------------------------------------------- #

def test_default_ask_send_failure_raises_without_polling(monkeypatch):
    """A failed send must fail ingest immediately, never polling.

    The live run's over-4096 prompt was rejected/truncated on send, then ingest
    went on to poll the topic for 900s waiting for a reply that could not come.
    ``_default_ask`` SENDS before it polls: if ``telegram.send_message`` raises
    (here :class:`MessageTooLongError` for an oversize body), the error must
    propagate IMMEDIATELY — the poll loop's ``sleep`` seam is never reached.
    The injected ``sleep`` here is a spike, so any poll would blow up and prove
    the send-failure path never waited for a reply.
    """
    from flightdeck.core.telegram import MessageTooLongError

    def _boom(topic_id, text, _client=None):
        raise MessageTooLongError(
            f"message of {len(text)} characters exceeds Telegram's limit"
        )

    monkeypatch.setattr(dec.telegram, "send_message", _boom)

    def _no_sleep(seconds):
        raise AssertionError("the ask seam slept after a failed send — it polled!")

    clock = Clock()
    try:
        dec._default_ask(_PROMPT, 140, SequentialFeed([[]]), now=clock.now, sleep=_no_sleep)
    except MessageTooLongError as exc:
        assert "exceeds" in str(exc)
    else:
        raise AssertionError("expected MessageTooLongError to propagate")


# --------------------------------------------------------------------------- #
# N8 — the accept seam: wait for the ANSWER, not the first message to arrive
# --------------------------------------------------------------------------- #

# The live 2026-08-10 failure: the orchestrator ACKs first with a preamble
# ("I'll read the ingest context file..."), then posts the real roadmap later.
# Without `accept`, the seam returns the ack and stops. With `accept`, it must
# skip the ack and keep polling until a message that actually answers arrives.
# Each feed line must be `[ts] sender: text` in ONE line — the reader parses
# line-by-line and a bare continuation line becomes `(unparsed)` noise.
_ACK_PREAMBLE = (
    "I'll read the ingest context file, then produce the roadmap in the exact format."
)
_ACK_LINE = f"[2026-08-10 10:07] Hermes: {_ACK_PREAMBLE}"
_ROADMAP_TEXT = "# Subproject: hscc; ## Milestone: init <!-- id: init -->; - [x] something delivered"
_ROADMAP_LINE = f"[2026-08-10 10:08] Hermes: {_ROADMAP_TEXT}"
# Another genuine (prefixed) but non-roadmap fragment — for the timeout test.
_MORE_PROSE = "still no roadmap here, just more reasoning"
_MORE_PROSE_LINE = f"[2026-08-10 10:09] Hermes: {_MORE_PROSE}"


def _accept_has_roadmap_region(text: str) -> bool:
    """A stand-in for ingest's accept predicate: True once the growing reply
    contains a roadmap-shaped line (``# Subproject:`` / ``## Milestone:``)."""
    return any(
        line.strip().startswith(("# Subproject:", "## Milestone:"))
        for line in text.splitlines()
    )


def test_default_ask_with_accept_skips_the_will_read_the_file_preamble():
    """The live failure: the seam must skip the \"I'll read the ingest context
    file\" acknowledgement and keep polling for the reply that ANSWERS.

    The ack (a genuine but non-roadmap message) arrives first; a later poll
    brings the actual roadmap. With ``accept`` given, the seam ignores the ack
    and returns only once a message satisfies the predicate. Without the fix it
    would have returned just the ack and stopped — truncating the answer.
    """
    result, feed, _ = _ask(
        [
            [],                                   # empty topic before send
            [_PROMPT_LINE, _ACK_LINE],            # poll 1: ack arrives, rejected
            [_PROMPT_LINE, _ACK_LINE, _ROADMAP_LINE],  # poll 2: roadmap arrives
        ],
        accept=_accept_has_roadmap_region,
    )
    assert isinstance(result, str)  # the reply, not a NoReplyError
    assert "# Subproject: hscc" in result
    assert "## Milestone: init" in result
    # The ack itself carried no roadmap region; the clean extracted roadmap is
    # exactly the roadmap message — proof the ack preamble was safely skipped.
    assert _extract(result) == _ROADMAP_TEXT


def _extract(text: str) -> str:
    """The clean roadmap region of a returned reply (matches ingest's N3 helper)."""
    from flightdeck.commands.ingest import _extract_roadmap
    return _extract_roadmap(text)


def test_default_ask_without_accept_returns_the_first_genuine_message():
    """Without ``accept`` the seam behaves EXACTLY as before (decompose
    unchanged): the first genuine message after the watermark is the reply.

    Here that is the ack — the live-failure shape — because the real roadmap
    has not been posted yet within the settle window, so the old seam settles
    and returns the ack. This contrasts with the accept path, which keeps
    polling instead. Proves the new parameter defaults to today's behaviour.
    """
    result, _, _ = _ask(
        [
            [],
            [_PROMPT_LINE, _ACK_LINE],        # only the ack has arrived so far
        ],
    )
    assert result == _ACK_PREAMBLE  # the ack was the first genuine message


def test_default_ask_with_accept_re_tests_multipart_and_accepts_on_complete():
    """Multi-part fragments are RE-TESTED as they concatenate, and accepted
    only once the combined reply satisfies the predicate.

    Each ``(1/2)``/``(2/2)`` part carries only one of the two anchor lines; the
    full answer — both a subproject AND a milestone — exists only once they are
    concatenated (a topology fact verified below). The seam must keep polling
    past each partial fragment and return only when the complete answer lands.
    """

    def _needs_both_anchors(text: str) -> bool:
        return "# Subproject:" in text and "## Milestone:" in text

    part1 = "# Subproject: hscc (1/2)"
    part2 = "## Milestone: init <!-- id: init --> (2/2)"
    assert not _needs_both_anchors(part1)
    assert not _needs_both_anchors(part2)
    assert _needs_both_anchors(part1 + "\n" + part2)

    # Prefix each raw fragment so the reader parses them as genuine messages
    # (sender=Hermes) rather than ``(unparsed)`` noise.
    part1_line = f"[2026-08-10 10:06] Hermes: {part1}"
    part2_line = f"[2026-08-10 10:06] Hermes: {part2}"
    result, _, _ = _ask(
        [
            [],                                # empty topic before send
            [_PROMPT_LINE, part1_line],        # poll 1: first half lands, rejected
            [_PROMPT_LINE, part2_line, part1_line],  # poll 2: second half lands, accepted
        ],
        accept=_needs_both_anchors,
    )
    assert result == part1 + "\n" + part2


def test_default_ask_with_accept_timeout_reports_messages_seen():
    """On timeout with ``accept``, the error reports HOW MANY messages were
    seen and rejected, so a wrong-shaped answer is diagnosable rather than
    indistinguishable from silence."""
    result, _, _ = _ask(
        [
            [],
            [_PROMPT_LINE, _ACK_LINE],                      # ack seen, rejected
            [_PROMPT_LINE, _ACK_LINE, _MORE_PROSE_LINE],    # another fragment, rejected
        ],
        timeout=5,
        accept=_accept_has_roadmap_region,
    )
    assert isinstance(result, dec.NoReplyError)
    assert "no accepted reply within 5s" in str(result)
    assert "2 messages seen" in str(result)


# --------------------------------------------------------------------------- #
# N15 — decompose waits for the JSON proposal, not the prose preamble
# --------------------------------------------------------------------------- #

# The live 2026-08-11 failure: `flightdeck decompose ... --milestone release-flow`
# exited 2 with "no JSON object found in the orchestrator's reply". The
# orchestrator's reply was a prose preamble; decompose's default ask returned
# the FIRST genuine message (the preamble) and parse_proposal then found no
# JSON. N15 wires decompose's OWN accept predicate (_proposal_accept) so the
# seam skips prose and waits for a message that actually carries a proposal.

_PROPOSAL_TEXT = (
    '{"cards": [{"id": 1, "title": "do it", "body": "b", "concern": "single", '
    '"verify": "v", "acceptance": "a"}]}'
)
_PROPOSAL_LINE = f"[2026-08-11 11:00] Hermes: {_PROPOSAL_TEXT}"
_PREAMBLE = "Here is my proposal. I'll break this goal into cards."
_PREAMBLE_LINE = f"[2026-08-11 10:59] Hermes: {_PREAMBLE}"


def test_default_ask_with_proposal_accept_skips_the_prose_preamble():
    """The live failure: with _proposal_accept the seam skips the orchestrator's
    prose preamble and keeps polling until a message carrying JSON arrives.

    The preamble (a genuine but non-JSON message) lands first; a later poll
    brings the JSON proposal. The seam must reject the preamble and return only
    once a message satisfies the predicate. Without the fix it would have
    returned the preamble and decompose would then have failed parsing it.
    """
    result, _, _ = _ask(
        [
            [],
            [_PROMPT_LINE, _PREAMBLE_LINE],                     # preamble, rejected
            [_PROMPT_LINE, _PREAMBLE_LINE, _PROPOSAL_LINE],     # JSON arrives
        ],
        accept=dec._proposal_accept,
    )
    assert isinstance(result, str)  # the reply, not a NoReplyError
    assert '"cards"' in result
    assert "do it" in result


def test_default_ask_accepts_fenced_and_prose_wrapped_json_reply():
    """A JSON reply wrapped in a ```json fence and/or prose is accepted.

    Models commonly wrap JSON in a code fence or surround it with prose;
    _extract_json (which _proposal_accept reuses via parse_proposal) strips
    the wrapper and finds the JSON object. The seam returns the raw wrapped
    reply; decompose's own parse_proposal later extracts the JSON from it.
    """
    fenced = (
        'Sure, here is the proposal:\n'
        '```json\n'
        f'{_PROPOSAL_TEXT}\n'
        '```\n'
        'Let me know if you want changes.'
    )
    fenced_line = f"[2026-08-11 11:01] Hermes: {fenced}"
    result, _, _ = _ask(
        [
            [],
            [_PROMPT_LINE, fenced_line],
        ],
        accept=dec._proposal_accept,
    )
    assert isinstance(result, str)
    assert '"cards"' in result
    assert "do it" in result


def test_proposal_accept_rejects_prose_and_wrong_shape_accepts_proposal():
    """Unit contract for the predicate decompose passes to the ask seam.

    It returns True only for a parseable JSON object of the shape decompose
    expects (a ``cards`` list): a bare prose acknowledgement is False, a JSON
    object that is not a proposal (no ``cards``) is False, a fenced response is
    True, and a proposal wrapped in prose is True. This is exactly the seam's
    "keep polling or settle" decision boundary.
    """
    assert dec._proposal_accept("I'll get right on it") is False
    assert dec._proposal_accept('{"foo": "bar"}') is False
    assert dec._proposal_accept('```json\n' + _PROPOSAL_TEXT + '\n```') is True
    assert dec._proposal_accept(
        "Preamble text.\n```json\n" + _PROPOSAL_TEXT + "\n```\nTrailing prose."
    ) is True


