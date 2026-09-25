"""Regression tests for memori_byodb/local_augmentation.py.

Covers t_57e5b3f7: `process()` used to pass a list of plain dicts to the
SDK's ``_schedule_entity_writes`` (which expects a ``Memories`` object), so
every capture raised ``AttributeError: 'list' object has no attribute
'entity'`` before any conversation / message / fact row was written.

These tests are hermetic: the local LLM endpoint is stubbed (no network) and
the driver is a fake recording the calls the augmentation would make — so no
live or scratch SQLite DB is touched, and the SDK's ``memori`` package is NOT
imported (its name collides with the repo's cloud ``memori`` plugin when the
worktree root is on sys.path).
"""

from __future__ import annotations

import asyncio
from typing import Any

from memori_byodb.local_augmentation import LocalLLMAugmentation


class _Msg:
    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content


class _Payload:
    conversation_id: str
    entity_id: str
    process_id: str | None
    conversation_messages: list[_Msg]

    def __init__(self, *, conversation_id: str, entity_id: str,
                 process_id: str | None, conversation_messages: list[_Msg]):
        self.conversation_id = conversation_id
        self.entity_id = entity_id
        self.process_id = process_id
        self.conversation_messages = conversation_messages


class _Ctx:
    def __init__(self, payload: _Payload):
        self.payload = payload
        self.data: dict[str, Any] = {}


class _Driver:
    """Fake driver recording augmentation write calls."""

    def __init__(self) -> None:
        self.entity_creates: list[str] = []
        self.conversation_creates: list[tuple] = []
        self.message_creates: list[tuple] = []
        self.fact_creates: list[tuple] = []

        self.entity = _Entity(self)
        self.conversation = _Conversation(self)
        self.entity_fact = _EntityFact(self)


class _Entity:
    def __init__(self, outer: _Driver) -> None:
        self._outer = outer

    def create(self, external_id: str) -> int:
        self._outer.entity_creates.append(external_id)
        return 1


class _Conversation:
    def __init__(self, outer: _Driver) -> None:
        self._outer = outer
        self.message = _ConversationMessage(outer)

    def create(self, session_id: str, timeout: int) -> int:
        self._outer.conversation_creates.append((session_id, timeout))
        return 10


class _ConversationMessage:
    def __init__(self, outer: _Driver) -> None:
        self._outer = outer

    def create(self, conv_id, role, type_, content):
        self._outer.message_creates.append((conv_id, role, type_, content))


class _EntityFact:
    def __init__(self, outer: _Driver) -> None:
        self._outer = outer

    def create(self, entity_id, facts, embeddings, conversation_id):
        self._outer.fact_creates.append(
            (entity_id, facts, embeddings, conversation_id)
        )


def test_parse_response_parses_facts():
    aug = LocalLLMAugmentation()
    response = (
        '[{"subject": "user", "predicate": "prefers", "object": "tea", '
        '"confidence": 0.9}]'
    )
    parsed = aug._parse_response(response)
    assert len(parsed) == 1
    assert parsed[0]["content"] == "user prefers tea"
    assert parsed[0]["metadata"]["predicate"] == "prefers"
    assert parsed[0]["metadata"]["confidence"] == 0.9


def test_parse_response_strips_markdown_fence():
    aug = LocalLLMAugmentation()
    response = (
        "```json\n"
        '[{"subject": "project", "predicate": "uses", "object": "sqlite", '
        '"confidence": 1.0}]\n'
        "```"
    )
    parsed = aug._parse_response(response)
    assert len(parsed) == 1
    assert parsed[0]["content"] == "project uses sqlite"


def test_parse_response_skips_invalid_and_falls_back():
    aug = LocalLLMAugmentation()
    # One valid, one missing object -> only the valid survives.
    parsed = aug._parse_response(
        '[{"subject": "a", "predicate": "is", "object": "x", "confidence": 0.8},'
        '{"subject": "b", "predicate": "is", "object": "", "confidence": 0.8}]'
    )
    assert len(parsed) == 1
    assert parsed[0]["content"] == "a is x"

    # Invalid JSON -> fallback single fact.
    parsed = aug._parse_response("not json at all")
    assert len(parsed) == 1
    assert parsed[0]["metadata"].get("fallback") is True


def _run(coro):
    return asyncio.run(coro)


def test_process_writes_conversation_messages_and_facts():
    """Regression: capture must land conversation + message + fact rows."""
    aug = LocalLLMAugmentation()

    async def fake_llm(system_prompt, user_prompt):
        return (
            '[{"subject": "user", "predicate": "prefers", "object": "tea", '
            '"confidence": 0.9}]'
        )

    aug._call_local_llm = fake_llm

    ctx = _Ctx(_Payload(
        conversation_id="session-abc",
        entity_id="entity-1",
        process_id="process-1",
        conversation_messages=[_Msg("user", "hi"), _Msg("assistant", "hello")],
    ))
    driver = _Driver()

    async def go():
        await aug.process(ctx, driver)
        if aug._session is not None:
            await aug._session.close()

    _run(go())

    # Entity resolvance.
    assert driver.entity_creates == ["entity-1"]
    # Conversation created once with the session id + timeout.
    assert driver.conversation_creates == [("session-abc", 30)]
    # Both messages written to the conversation.
    assert len(driver.message_creates) == 2
    assert driver.message_creates[0] == (10, "user", "text", "hi")
    assert driver.message_creates[1] == (10, "assistant", "text", "hello")
    # Facts written, linked to the conversation (id 10).
    assert len(driver.fact_creates) == 1
    entity_id, facts, embeddings, conv_id = driver.fact_creates[0]
    assert entity_id == 1
    assert facts == ["user prefers tea"]
    assert conv_id == 10
    # Context kept for downstream use.
    assert ctx.data["memories"][0]["content"] == "user prefers tea"


def test_process_noop_without_conversation_messages():
    aug = LocalLLMAugmentation()

    async def fake_llm(system_prompt, user_prompt):  # pragma: no cover
        raise AssertionError("should not be called")

    aug._call_local_llm = fake_llm
    ctx = _Ctx(_Payload(
        conversation_id="session-abc",
        entity_id="entity-1",
        process_id="process-1",
        conversation_messages=[],
    ))
    driver = _Driver()

    async def go():
        await aug.process(ctx, driver)

    _run(go())
    # No writes, no LLM call.
    assert driver.entity_creates == []
    assert driver.conversation_creates == []
    assert driver.fact_creates == []
