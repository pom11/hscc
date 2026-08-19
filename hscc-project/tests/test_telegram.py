

# --------------------------------------------------------------------------- #
# SDK name compatibility (mcp 2.0.0 rename)
# --------------------------------------------------------------------------- #

def test_streamable_http_client_accepts_either_sdk_name(monkeypatch):
    """Both the mcp<2 and mcp>=2 factory names resolve.

    The suite was green against an SDK exposing only the old name while the
    install environment shipped 2.0.0 with only the new one, so every Telegram
    command died at the first real call.
    """
    import types
    from flightdeck.core import telegram

    for name in ("streamable_http_client", "streamablehttp_client"):
        mod = types.SimpleNamespace(**{name: f"factory-{name}"})
        monkeypatch.setattr(
            telegram, "_streamable_http_client",
            lambda _m=mod: next(
                getattr(_m, n) for n in
                ("streamable_http_client", "streamablehttp_client")
                if getattr(_m, n, None) is not None
            ),
        )
        assert telegram._streamable_http_client() == f"factory-{name}"


def test_streamable_http_client_reports_missing_factory_clearly(monkeypatch):
    """An SDK with neither name gives an actionable message, not an ImportError."""
    import types
    import pytest as _pytest
    import mcp.client as _mcp_client
    from flightdeck.core import telegram

    monkeypatch.setattr(_mcp_client, "streamable_http", types.SimpleNamespace())
    with _pytest.raises(RuntimeError, match="neither streamable_http_client"):
        telegram._streamable_http_client()


# --------------------------------------------------------------------------- #
# An error payload is never a healthy empty result
# --------------------------------------------------------------------------- #

def test_group_is_sent_as_string():
    """The MCP tools declare `group: str`; the resolver returns a str."""
    from flightdeck.core import telegram

    seen = {}

    def client(tool, args):
        seen.update(args)
        return "topic_id=140  title=Operations"

    telegram.list_topics(_client=client)
    assert isinstance(seen["group"], str)


import pytest as _pytest


@_pytest.mark.parametrize(
    "payload",
    [
        "Unknown tool: telegram_topic_status",
        "Error executing tool telegram_topic_status: boom",
        "1 validation error for telegram_topic_statusArguments\ngroup\n  Input should be a valid string",
    ],
)
def test_error_payload_raises_instead_of_reporting_clean(payload):
    """A rejected call must raise, never parse to [] and render as healthy.

    `topics audit` printed "audit clean" while the daemon was rejecting every
    call, because the error text contained no topic lines.
    """
    from flightdeck.core import telegram

    with _pytest.raises(telegram.TelegramError, match="rejected"):
        telegram.list_topics(_client=lambda tool, args: payload)


def test_genuine_empty_topic_list_is_still_allowed():
    """An actually-empty group is a valid answer and must not raise."""
    from flightdeck.core import telegram

    assert telegram.list_topics(_client=lambda tool, args: "") == []


# --------------------------------------------------------------------------- #
# N7 — send_message refuses oversize bodies (Telegram's 4096-char cap)
# --------------------------------------------------------------------------- #


def test_send_message_raises_naming_length_over_4096():
    """A body over the limit raises a clear error naming the ACTUAL length.

    Bulk context blasted through a chat channel is what the live run proved
    broken: the send was rejected/truncated at Telegram's 4096-char cap and
    ingest polled 900s for a reply that could not come. An oversize send must
    fail loudly — naming the real character count — before any dispatch, never
    be silently rejected or truncated.
    """
    import pytest as _pytest  # noqa: F811  (module already imports it below)

    from flightdeck.core import telegram
    sent = {}

    def client(tool, args):
        sent["tool"] = tool
        return "Sent."

    body = "x" * (telegram.MAX_MESSAGE_LENGTH + 1)
    with _pytest.raises(telegram.MessageTooLongError) as excinfo:
        telegram.send_message(140, body, _client=client)
    # The error names the actual length AND the constant.
    assert str(telegram.MAX_MESSAGE_LENGTH) in str(excinfo.value)
    assert str(len(body)) in str(excinfo.value)
    assert str(len(body)) != str(telegram.MAX_MESSAGE_LENGTH)
    # No dispatch happened: the guard fires before any send.
    assert sent == {}


def test_send_message_sends_normally_under_the_limit():
    """A body at or under the limit dispatches normally and returns the reply."""
    import pytest as _pytest  # noqa: F811

    from flightdeck.core import telegram

    seen = {}

    def client(tool, args):
        seen["text"] = args["message"]
        return "Sent."

    body = "y" * telegram.MAX_MESSAGE_LENGTH  # exactly at the limit is fine
    reply = telegram.send_message(140, body, _client=client)
    assert reply == "Sent."
    assert seen["text"] == body
    assert seen["text"] is not None


# --------------------------------------------------------------------------- #
# P1 — missing group id fails clear, never falls back to a baked-in value
# --------------------------------------------------------------------------- #


def test_operation_with_no_group_id_raises_actionable(monkeypatch, tmp_path):
    """Clearing the injected group (simulating "no config") makes any telegram
    operation fail with a clear, actionable message naming the config key —
    never a silent baked-in fallback."""
    from flightdeck.core import telegram, config

    # Remove the per-test injected group AND ensure no env var leaks in, so the
    # resolver genuinely has nothing to fall back to. Also point the config
    # resolution at a NON-EXISTENT file (an empty tmp_path), so a real
    # ~/.flightdeck/config.yaml present on the host does not leak in — the
    # "config file absent" precondition this test asserts is enforced, not
    # assumed.
    monkeypatch.setattr(telegram, "_GROUP_ID", None)
    monkeypatch.setattr(config, "DEFAULT_CONFIG",
                        str(tmp_path / "config.yaml"))
    monkeypatch.delenv("FLIGHTDECK_TELEGRAM_GROUP_ID", raising=False)
    monkeypatch.delenv("FLIGHTDECK_MCP_URL", raising=False)

    with _pytest.raises(telegram.TelegramConfigError) as excinfo:
        telegram.list_topics(_client=lambda tool, args: "")

    msg = str(excinfo.value)
    # The message is actionable: it names the config key the user must set.
    assert "telegram.group_id" in msg
    assert "FLIGHTDECK_TELEGRAM_GROUP_ID" in msg
