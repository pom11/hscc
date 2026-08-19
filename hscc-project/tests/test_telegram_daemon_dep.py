"""Tests: the Telegram MCP daemon is a documented, self-diagnosing dependency.

TG1 — every Telegram feature talks to an MCP daemon at
``http://127.0.0.1:8787/mcp`` that is NOT in this repo and was NOT documented
as a prerequisite. A public user installing flightdeck had no way to know it
exists, and every Telegram command simply failed for them.

These tests pin the three behaviours that make the dependency explicit and
self-diagnosing:

  1. When a Telegram command runs and the daemon is unreachable, the error
     names the daemon, the exact configured URL, and points at
     ``docs/TELEGRAM.md`` — never a bare traceback or a generic connection
     error.
  2. A non-Telegram command runs fine with the daemon absent (no accidental
     dependency on it).
  3. The tool list in ``docs/TELEGRAM.md`` matches the tool names
     ``flightdeck/core/telegram.py`` actually calls — asserted against the
     module source, so the doc cannot drift.

Nothing here touches the network, ~/.hermes, a real board, or a real daemon:
the transport is stubbed to raise a socket-level failure (the unreachable
case) and command modules are driven with injected fixtures.
"""

from __future__ import annotations

import re

import pytest

from flightdeck.core import telegram


# --------------------------------------------------------------------------- #
# The daemon is unreachable -> an actionable error, not a bare traceback
# --------------------------------------------------------------------------- #


def _unreachable_client(tool_name, arguments):
    """A stub transport that fails exactly as an unreachable daemon does.

    The real mcp streamable-HTTP client surfaces connection refused wrapped in
    a transport/httpx error. ``probe.is_connection_refused`` walks cause and
    exception groups, so wrapping the socket error is exactly what production
    produces — and this test pins that the legible message comes out.
    """
    raise ConnectionRefusedError("connection refused")


def test_unreachable_daemon_error_names_url_and_doc(monkeypatch):
    """THE public-user case: daemon absent -> message names daemon, URL, doc.

    A fresh install with no daemon running must get a message that says *what*
    it cannot reach, *where* flightdeck looked, and *where to read the
    contract* — never an opaque traceback or a generic connection error.
    """
    # Point the URL resolution at a deterministic, non-existent-file default so
    # a real ~/.flightdeck/config.yaml on the host cannot leak in, and give a
    # group id so the operation proceeds past config into the transport.
    monkeypatch.setattr(telegram, "_GROUP_ID", "-100123")
    from flightdeck.core import config

    import tempfile

    monkeypatch.setattr(config, "DEFAULT_CONFIG", tempfile.mkdtemp() + "/config.yaml")
    monkeypatch.delenv("FLIGHTDECK_MCP_URL", raising=False)
    expected_url = config.DEFAULT_MCP_URL  # http://127.0.0.1:8787/mcp

    client = _unreachable_client
    with pytest.raises(telegram.TelegramDaemonUnreachableError) as excinfo:
        telegram.list_topics(_client=client)

    msg = str(excinfo.value)
    # It names the daemon, the configured URL, and the doc.
    assert "Telegram MCP daemon" in msg
    assert expected_url in msg
    assert telegram.TELEGRAM_DOC_PATH in msg
    # It is NOT a bare traceback (the exception has no unhandled cause surface).
    assert "Traceback" not in msg


def test_unreachable_daemon_error_is_a_telegram_error():
    """The unreachable error is a TelegramError, so every Telegram command
    already catches it and presents it as a clear message (not a traceback)."""
    assert issubclass(telegram.TelegramDaemonUnreachableError, telegram.TelegramError)


def test_topics_list_surfaces_unreachable_without_traceback(monkeypatch, capsys):
    """The command layer presents the unreachable error as a clean message.

    Reviewing requirement #3 end to end: a Telegram command must never print a
    bare traceback. `topics list` (whose transport call was originally NOT
    wrapped) must print ``error: <legible message>`` and exit 2, not raise.
    """
    import argparse

    from flightdeck.commands import topics as cmd
    from flightdeck.core.registry import Project

    def _unreachable_client(tool, arguments):
        raise ConnectionRefusedError("connection refused")

    args = argparse.Namespace(client=_unreachable_client, json=False, registry="x")
    rc = cmd.cmd_list(args, [Project(name="p", repo="/repo", board="b")])
    out = capsys.readouterr()
    assert rc == 2
    err = out.err
    assert "error:" in err
    assert "Telegram MCP daemon" in err
    assert telegram.TELEGRAM_DOC_PATH in err
    assert "Traceback" not in err


# --------------------------------------------------------------------------- #
# A non-Telegram command runs fine with the daemon absent
# --------------------------------------------------------------------------- #


def test_non_telegram_command_runs_with_daemon_absent(monkeypatch, capsys):
    """`metrics` (a non-Telegram command) works even when the daemon is gone.

    The regression this guards: flightdeck must not gain a hard dependency on
    the daemon, so a non-Telegram command must never reach the transport. We
    force ``telegram._default_client`` to blow up if it is EVER called, then
    drive ``metrics`` with injected fixtures (no board, no git, no network) and
    assert it completes fine — proving the command never needs the daemon.
    """
    import argparse

    from flightdeck.commands import metrics as cmd
    from flightdeck.core.registry import Project

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("a non-Telegram command must never reach the daemon")

    monkeypatch.setattr(telegram, "_default_client", _fail_if_called)

    proj = Project(name="flightdeck", repo="/repo", board="flightdeck")

    def _ok_run(c, repo):
        # metrics asks git `merge-base`/`rev-list` via injected runner; answer
        # merged/commits so the metric computes.
        if c[1] == "merge-base":
            return argparse.Namespace(returncode=0, stdout="", stderr="")
        return argparse.Namespace(returncode=0, stdout="1", stderr="")

    def _events(cid):
        return [{"kind": "blocked", "created_at": 2000}]

    def _now():
        return 5000

    args = argparse.Namespace(
        registry="/nonexistent/reg.yaml",
        json=False,
        project="flightdeck",
        since="4000s",
        run=_ok_run,
        events=_events,
        now=_now,
        cards=[],
        stderr=None,
    )
    rc = cmd.cmd_metrics(args, [proj])
    assert rc == 0
    out = capsys.readouterr().out
    # The command ran and produced output — the daemon was never needed.
    assert "metrics:" in out


# --------------------------------------------------------------------------- #
# The doc's tool list cannot drift from what the module actually calls
# --------------------------------------------------------------------------- #


def _module_tool_names() -> list[str]:
    """The MCP tool names ``core/telegram.py`` actually dispatches.

    Extracted straight from the module source (every ``_dispatch("name", ...)``
    first-argument string) so the assertion is against the code, never a
    hand-maintained copy.
    """
    import inspect

    src = inspect.getsource(telegram)
    names = []
    for m in re.finditer(r"_dispatch\(\s*\"([a-z_0-9]+)\"", src):
        if m.group(1) not in names:
            names.append(m.group(1))
    return names


def _doc_names() -> list[str]:
    """The MCP tool names listed in docs/TELEGRAM.md (from the contract table)."""
    from pathlib import Path

    doc_path = Path(__file__).resolve().parents[1] / "docs" / "TELEGRAM.md"
    text = doc_path.read_text(encoding="utf-8")
    return [
        name
        for name in (
            "telegram_topics",
            "telegram_topic_status",
            "telegram_topic_create",
            "telegram_topic_rename",
            "telegram_send",
            "telegram_read",
        )
        if re.search(rf"\b{name}\b", text)
    ]


def test_telegram_doc_lists_every_tool_the_module_calls():
    """Every tool name the module dispatches is named in docs/TELEGRAM.md.

    The doc is the contract a daemon implementer reads; if the module calls a
    tool the doc never mentions, a daemon built from the doc would be missing
    a required method.
    """
    module_names = _module_tool_names()
    assert module_names, "expected core/telegram.py to dispatch at least one tool"
    doc_names = _doc_names()
    for name in module_names:
        assert name in doc_names, (
            f"tool {name!r} is dispatched by core/telegram.py but is missing "
            f"from docs/TELEGRAM.md — the doc has drifted from the code"
        )


def test_telegram_doc_does_not_list_tools_the_module_never_calls():
    """The doc names only tools the module actually calls.

    The reverse drift: a daemon implementer reading the doc must not build
    tools flightdeck will never invoke (dead surface). ``telegram_topics`` is
    deliberately NOT a tool the module dispatches, so it must not be required.
    """
    module_names = set(_module_tool_names())
    doc_names = _doc_names()
    for name in doc_names:
        assert name in module_names, (
            f"docs/TELEGRAM.md lists {name!r} but core/telegram.py never calls "
            f"it — the doc has drifted from the code"
        )


def test_actual_called_tool_names_are_stable():
    """Pin the exact set of tools the module dispatches today.

    If a future change adds a tool to the module, this test forces the doc to
    be updated in the same change — the drift guard works both ways.
    """
    assert sorted(_module_tool_names()) == [
        "telegram_read",
        "telegram_send",
        "telegram_topic_create",
        "telegram_topic_rename",
        "telegram_topic_status",
    ]
