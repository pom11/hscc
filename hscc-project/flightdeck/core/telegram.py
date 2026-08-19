"""telegram.py — Telegram forum topic operations via the shared MCP daemon.

Transport: the existing single-writer Telegram MCP (``~/.hermes-tg/mcp_server.py``)
owns the one Telethon session for the fleet. Flightdeck NEVER opens its own
Telethon session — a second client on the same session file is exactly what
raises ``database is locked``. Instead this module is a thin MCP client to the
daemon at ``http://127.0.0.1:8787/mcp``, reusing the same transport every other
fleet tool already uses.

Every MCP tool call flows through an injectable ``_client`` — a plain callable
``(tool_name, arguments) -> str``. Tests pass a stub; production falls back to
:func:`_default_client`, which lazily imports the ``mcp`` SDK so a test suite
that stubs the client never pays to load it.

All functions honour the design hard rule: a topic whose name or mapping is
missing is surfaced, never silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from . import config as _config
from . import probe as _probe

# The doc that explains what the Telegram MCP daemon must provide and how to
# configure it. Named in every unreachable-daemon error so a public user who
# has never heard of the daemon is pointed straight at the contract.
TELEGRAM_DOC_PATH = "docs/TELEGRAM.md"

# The Telegram group id and MCP URL are no longer source constants: the
# operator's private group id and machine-specific URL must never ship in a
# public repo. They live in ~/.flightdeck/config.yaml (env vars override),
# resolved at call time by `_resolve_group_id` / config.telegram_mcp_url().
#
# `_GROUP_ID` is a test-injectable override: tests set it to control which
# group the module targets without touching config, disk, or the network. In
# production it is None and every call resolves from config, so no baked-in
# value can leak. `group_id` has NO default — an unset id raises a clear,
# actionable MissingGroupIdError rather than guessing.
_GROUP_ID: str | None = None


class TelegramError(Exception):
    """Base class for telegram transport errors."""


class TelegramConfigError(TelegramError, _config.MissingGroupIdError):
    """A Telegram operation was attempted with no configured group id.

    Subclasses both :class:`TelegramError` (so existing callers that catch
    ``TelegramError`` keep working) and :class:`config.MissingGroupIdError`
    (so the config test asserts the underlying cause). Carries a clear,
    actionable message naming the config key.
    """


class TopicLockedError(TelegramError):
    """The Telegram session is held by another writer ("database is locked").

    The session is single-writer, so transient contention (a handover, another
    tool mid-write) throws SQLite's "database is locked". This surfaces as a
    clear message with a retry hint, never a raw traceback.
    """


class TelegramDaemonUnreachableError(TelegramError):
    """The Telegram MCP daemon could not be reached (nothing listening).

    Raised by :func:`_dispatch` when the transport fails at the socket level —
    connection refused, DNS lookup failure, or a timeout — detected via the
    shared :func:`probe.is_connection_refused` helper. The message names the
    daemon, the exact configured URL, and the doc that explains what it must
    provide, so a public user who has never seen the daemon is pointed at the
    fix — never a bare traceback or a generic connection error. Every Telegram
    command catches ``TelegramError``, so this presents as a clear message.
    """


# Telegram's hard per-message limit, in characters. A single send over this is
# rejected or truncated by Telegram; flightdeck must never push bulk context
# through a chat channel. Compute len() in CHARACTERS, matching how the user
# sees the limit (and matching the char counts this repo quotes elsewhere).
MAX_MESSAGE_LENGTH = 4096


class MessageTooLongError(TelegramError):
    """A message body exceeds Telegram's per-message character limit.

    Raised by :func:`send_message` BEFORE any dispatch so an oversize send is
    impossible to miss — a body that Telegram would reject or truncate is
    never silently sent. The message names the actual length so the fix is
    obvious (write the bulk to a file and send a short pointer instead).
    """


def _resolve_group_id() -> str:
    """Resolve the Telegram group id for one operation.

    Order: injected ``_GROUP_ID`` (tests) > env ``FLIGHTDECK_TELEGRAM_GROUP_ID``
    > config file ``telegram.group_id``. An unset id raises
    :class:`TelegramConfigError` with an actionable message naming the config
    key — never a baked-in fallback, never a guess.
    """
    if _GROUP_ID is not None:
        return _GROUP_ID
    try:
        return _config.telegram_group_id()
    except _config.MissingGroupIdError as exc:
        raise TelegramConfigError(str(exc)) from exc


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Topic:
    """A forum topic as currently seen on Telegram."""

    id: int
    name: str


@dataclass(frozen=True)
class Message:
    """One message as read from a topic, newest last."""

    timestamp: str
    sender: str
    text: str


# --------------------------------------------------------------------------- #
# Injectable transport
# --------------------------------------------------------------------------- #

def _streamable_http_client():
    """Return the MCP streamable-HTTP client factory, whatever it is called here.

    ``mcp`` 2.0.0 renamed ``streamablehttp_client`` to ``streamable_http_client``.
    Both names are accepted because the interpreter that runs the installed
    console script is not necessarily the one the tests ran under: the suite was
    green against an SDK exposing the old name while the install environment
    shipped 2.0.0, and every Telegram command failed with an ImportError at the
    first real call. Resolving the name at call time keeps that mismatch from
    being fatal, and an SDK exposing neither is reported as a clear install
    problem rather than an opaque ImportError.
    """
    from mcp.client import streamable_http as _mod

    for name in ("streamable_http_client", "streamablehttp_client"):
        factory = getattr(_mod, name, None)
        if factory is not None:
            return factory
    raise RuntimeError(
        "the installed mcp SDK exposes neither streamable_http_client nor "
        "streamablehttp_client; flightdeck cannot reach the Telegram daemon. "
        "Check which interpreter runs `flightdeck` and its mcp version."
    )


def _default_client() -> Callable[[str, dict], str]:
    """Return the production MCP-over-HTTP client bound to the shared daemon.

    Lazy: the ``mcp`` SDK is imported only when this is called, so tests that
    stub ``_client`` never import it (keeping import time and the dependency
    footprint of the test suite down).
    """

    def call(tool_name: str, arguments: dict) -> str:
        import asyncio

        from mcp import ClientSession

        http_client = _streamable_http_client()

        async def _invoke() -> str:
            async with http_client(_config.telegram_mcp_url()) as streams:
                # mcp < 2 yields (read, write, get_session_id); 2.0 yields
                # (read, write). Take the two stream ends positionally so the
                # extra element is tolerated rather than required.
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
            parts = []
            for block in getattr(result, "content", []) or []:
                text = getattr(block, "text", None)
                if text is not None:
                    parts.append(text)
            return "\n".join(parts)

        return asyncio.run(_invoke())

    return call


def _dispatch(
    tool_name: str,
    arguments: dict,
    client: Callable[[str, dict], str] | None,
) -> str:
    """Resolve the injectable client and invoke one MCP tool call.

    ``_client=None`` falls back to the real daemon client. Any error whose text
    mentions the single-writer condition is normalised to :class:`TopicLockedError`
    so commands can present it as a clear message rather than a traceback.
    """
    if client is None:
        client = _default_client()
    try:
        return client(tool_name, arguments)
    except TelegramDaemonUnreachableError:
        raise
    except TopicLockedError:
        raise
    except Exception as exc:  # transport/protocol failure from any layer
        if re.search(r"database is locked", str(exc), re.IGNORECASE):
            raise TopicLockedError(
                "the shared Telegram session is locked by another writer "
                "(single-writer session). Wait a moment and retry."
            ) from exc
        if _probe.is_connection_refused(exc):
            # The daemon is genuinely not reachable (socket-level failure). Name
            # the daemon, the exact configured URL, and the doc — never a bare
            # traceback or a generic connection error.
            raise TelegramDaemonUnreachableError(
                "could not reach the Telegram MCP daemon (the single-writer "
                "Telethon session flightdeck relies on for topics, messaging, "
                "ask, ingest, report and sync). It is configured via "
                f"`telegram.mcp_url` / FLIGHTDECK_MCP_URL and flightdeck is "
                f"looking for it at {_config.telegram_mcp_url()!r}. Make sure "
                f"the daemon is running there, or see {TELEGRAM_DOC_PATH} for "
                "what it must provide."
            ) from exc
        raise


# --------------------------------------------------------------------------- #
# Operations (thin parsing of the MCP daemon's text output)
# --------------------------------------------------------------------------- #


_ERROR_PAYLOAD = re.compile(
    r"^\s*(unknown tool|error executing tool|.*validation error)", re.IGNORECASE
)


def _reject_error_payload(raw: str, tool_name: str) -> str:
    """Raise when the daemon answered with an error instead of data.

    The MCP transport returns tool errors as ordinary text. Every parser in this
    module looks for its own shape and ignores anything else, so an error reply
    used to parse into an empty list and render as a clean, healthy result --
    `topics audit` reported "audit clean" while the daemon was actually
    rejecting every call. An unparseable answer is an unknown state, and an
    unknown state is never reported as OK.
    """
    if _ERROR_PAYLOAD.match(raw or ""):
        raise TelegramError(
            f"the Telegram daemon rejected {tool_name}: {raw.strip()[:300]}"
        )
    return raw


def list_topics(
    _client: Callable[[str, dict], str] | None = None,
) -> list[Topic]:
    """Return every topic with its CURRENT name, as Telegram sees it.

    Uses the MCP ``telegram_topic_status`` tool, which reads live forum-topic
    titles via GetForumTopicsRequest — the authoritative source for detecting
    names that were overwritten. Never raises for a missing topic; a topic that
    cannot be named is surfaced with an "?" name rather than dropped.
    """
    raw = _reject_error_payload(
        _dispatch("telegram_topic_status", {"group": _resolve_group_id()}, _client),
        "telegram_topic_status",
    )
    topics: list[Topic] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"topic_id=(\d+)\s+title=(.*)$", line)
        if m:
            topics.append(Topic(id=int(m.group(1)), name=m.group(2)))
    return topics


def create_topic(
    name: str,
    _client: Callable[[str, dict], str] | None = None,
) -> Topic:
    """Create a forum topic named ``name``; returns the new Topic."""
    raw = _dispatch("telegram_topic_create", {"group": _resolve_group_id(), "name": name}, _client)
    m = re.search(r"topic_id=(\d+)\s+title=(.*)$", raw)
    if not m:
        raise TelegramError(f"could not parse create response: {raw!r}")
    return Topic(id=int(m.group(1)), name=m.group(2))


def rename_topic(
    topic_id: int,
    name: str,
    _client: Callable[[str, dict], str] | None = None,
) -> Topic:
    """Rename the topic ``topic_id`` to ``name``; returns it with the new name.

    Detects whether the target topic exists so a caller can tell a successful
    rename from a no-op on a nonexistent id.
    """
    raw = _dispatch(
        "telegram_topic_rename", {"group": _resolve_group_id(), "topic_id": topic_id, "name": name},
        _client,
    )
    m = re.search(r"topic_id=(\d+)\s+title=(.*)$", raw)
    if not m:
        raise TelegramError(f"could not parse rename response: {raw!r}")
    return Topic(id=int(m.group(1)), name=m.group(2))


def send_message(
    topic_id: int,
    text: str,
    _client: Callable[[str, dict], str] | None = None,
) -> str:
    """Send ``text`` into forum topic ``topic_id``; returns the raw daemon reply.

    ``text`` is passed verbatim. A locked single-writer session surfaces as
    :class:`TopicLockedError` (via ``_dispatch``), never a traceback.

    A body longer than ``MAX_MESSAGE_LENGTH`` raises :class:`MessageTooLongError`
    naming the actual length BEFORE any dispatch — Telegram hard-caps one
    message at 4096 characters, so an oversize send could never succeed and
    must fail loudly rather than be rejected/truncated silently. Bulk context
    belongs in a file the receiver reads, not in a chat message.
    """
    if len(text) > MAX_MESSAGE_LENGTH:
        raise MessageTooLongError(
            f"message of {len(text)} characters exceeds Telegram's "
            f"{MAX_MESSAGE_LENGTH}-character per-message limit; refusing to "
            f"send. Write the bulk to a file and send a short pointer to its "
            f"path instead."
        )
    return _dispatch(
        "telegram_send",
        {"group": _resolve_group_id(), "message": text, "topic_id": topic_id},
        _client,
    )


def read_messages(
    topic_id: int,
    n: int = 20,
    _client: Callable[[str, dict], str] | None = None,
) -> list[Message]:
    """Return up to ``n`` most recent messages in topic ``topic_id``, newest last.

    Parses the daemon's ``[ts] sender(@user): text`` lines. A line whose shape
    does not match is preserved as a message with an ``(unparsed)`` sender so
    nothing read from the topic is silently dropped.
    """
    raw = _dispatch(
        "telegram_read",
        {"group": _resolve_group_id(), "limit": n, "topic_id": topic_id},
        _client,
    )
    messages: list[Message] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\[(.*?)\] (.*?): (.*)$", line)
        if m:
            messages.append(
                Message(timestamp=m.group(1), sender=m.group(2), text=m.group(3))
            )
        else:
            messages.append(Message(timestamp="", sender="(unparsed)", text=line))
    return messages


def topic_exists(topic_id: int, topics: list[Topic]) -> bool:
    """True if ``topic_id`` is among the given topics.

    Separated out so a caller can rename/audit against a snapshot without extra
    round-trips. An unknown id is reported honestly, not guessed.
    """
    return any(t.id == topic_id for t in topics)
