"""Regression: the _theme fallback must never crash with MissingStyle.

When ``hscc_daemon.cli_theme`` is unavailable -- a standalone flightdeck
install, or ``cd hscc-project && pytest`` where the repo root is not on
sys.path -- ``_theme.make_console()`` falls back to a plain Console. That
fallback console must still register the semantic style names ('error',
'dim', 'ok', 'warn', 'label', 'accent', 'title', 'border', ...) so any card's
``border_style="error"`` or ``[error]`` / ``[dim]`` / ``[ok]`` markup renders
instead of raising ``rich.errors.MissingStyle`` ("'error' is not a valid
colour"). Before the fix (t_e6bff987), the two partial-failure tests crashed
with exactly that error whenever hscc_daemon was off sys.path, while they
passed under run_tests.sh (repo-root cwd puts hscc_daemon on sys.path) -- a
cwd-dependent flake masked by the canonical harness.
"""

import io
from contextlib import redirect_stdout

import pytest

from flightdeck.commands import _theme


@pytest.fixture(autouse=True)
def _force_fallback(monkeypatch):
    """Force the plain-Console fallback regardless of whether hscc_daemon is
    importable, pinning the standalone-flightdeck / non-repo-root behaviour.
    Without this autouse fixture the test would silently exercise the themed
    (hscc_daemon) path under run_tests.sh and prove nothing about the fallback.
    """
    monkeypatch.setattr(_theme, "theme", lambda: None)


def _render(renderable) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        _theme.make_console().print(renderable)
    return buf.getvalue()


def test_fallback_console_renders_error_border_and_status_panel():
    """The exact crash: a card using border_style='error' and status_panel on
    a fallback console used to raise MissingStyle; now it renders."""
    out = _render(_theme.panel("project new", "body", border_style="error"))
    assert "project new" in out

    out = _render(_theme.status_panel("board step failed", "error"))
    assert "ERROR  board step failed" in out

    out = _render("[error]boom[/error] [dim]muted[/dim] [ok]fine[/ok]")
    assert "boom" in out and "muted" in out and "fine" in out


def test_fallback_stays_no_ansi():
    """The fallback is genuinely unthemed: no escape bytes in a pipe."""
    out = _render(_theme.panel("x", "body", border_style="error"))
    out += _render(_theme.status_panel("failed", "error"))
    assert "\x1b[" not in out
    assert "\x1b" not in out


def test_fallback_keeps_non_tty_width():
    """Width preservation (content staying on one line in a pipe) must survive
    the fallback path too -- the whole reason make_console recreates the
    console at width 200 for non-tty output."""
    c = _theme.make_console()
    assert c.is_terminal is False
    assert c.width == 200
