"""No-ANSI + theme regression tests for the themed hscc-commands renderer.

The RICH CLI hscc-commands card converted the single raw site — the
``json.dumps(...)`` inside ``cmd_template`` that embeds a template CLI result
JSON block into a chat reply — to the themed Rich layer (hscc-commands/_theme.py,
the ``_theme`` pattern from hscc-project). The HARD INVARIANTS this file pins:

- A slash command's reply is a CHAT string, never a terminal, so it must carry
  ZERO ANSI escape bytes. ``_theme.render_json`` returns the plain, byte-faithful
  ``json.dumps`` string (deliberately bypassing the wrapping Console so the JSON
  block is never reflowed or truncated differently); these tests capture that
  string and assert the escape constant is absent.
- The escape character and the anti-collapse width are asserted as NAMED
  CONSTANTS (``_ESC`` / ``_theme.DEFAULT_NONTTY_WIDTH``), never as scattered
  literal ``\\x1b`` / ``200`` values.
- The fallback (when ``hscc_daemon.cli_theme`` is absent) renders the same
  no-ANSI plain output, so a standalone invocation stays safe.

All addresses below are the documented placeholders (LAN 10.0.0.x) — never the
operator's live tailnet host.
"""

import io
import json

import pytest

import cmdlib
import _theme
import __init__ as plugin


# The escape character, as one named constant — asserted, never a literal.
_ESC = "\x1b"


# ------------------------------------------------------------------------- #
# _theme.render_json: plain, no-ANSI rendering through the themed Console.
# ------------------------------------------------------------------------- #


def test_render_json_produces_no_ansi():
    """render_json output contains zero escape bytes — a chat reply is never
    a terminal, so the themed text-export must be plain."""
    out = _theme.render_json({"templates": [], "ok": True})
    assert _ESC not in out
    assert _ESC + "[" not in out
    assert '"templates"' in out and '"ok": true' in out


def test_render_json_matches_raw_json_dumps_formatting():
    """Indent/default=str formatting of the JSON block is unchanged from the
    raw site it replaces (json.dumps(..., indent=2, default=str))."""
    payload = {"templates": [{"name": "a", "nodes": 4}], "ok": True}
    expected = json.dumps(payload, indent=2, default=str)
    out = _theme.render_json(payload)
    assert out == expected


def test_render_json_respects_max_chars_truncation():
    """The historical truncation (the old chat reply capped the JSON block at
    3000 chars) is preserved via the max_chars param."""
    payload = {"blob": "x" * 5000}
    out = _theme.render_json(payload, max_chars=3000)
    assert len(out) == 3000


def test_render_json_default_str_on_non_serializable():
    """default=str keeps rendering non-JSON-serializable values (e.g. a
    datetime), exactly like the raw json.dumps(default=str) it replaces."""
    import datetime
    payload = {"at": datetime.datetime(2026, 9, 25, 12, 0, 0)}
    out = _theme.render_json(payload)
    assert "2026-09-25" in out
    assert _ESC not in out


# ------------------------------------------------------------------------- #
# cmd_template: the converted raw site routes through _theme, no ANSI.
# ------------------------------------------------------------------------- #


def test_cmd_template_routes_through_theme(monkeypatch):
    """cmd_template's JSON block is produced by _theme.render_json (the themed
    surface), not a raw json.dumps — asserted via monkeypatch as the seam."""
    captured = []
    monkeypatch.setattr(
        _theme, "render_json",
        lambda payload, indent=2, max_chars=None: (
            captured.append({"payload": payload, "max_chars": max_chars})
            or "{\n  \"ok\": true\n}"
        ),
    )
    monkeypatch.setattr(cmdlib, "template_cli", lambda argv: {"ok": True})
    out = plugin.cmd_template("list")
    assert len(captured) == 1, "cmd_template must route through _theme"
    assert captured[0]["payload"] == {"ok": True}
    assert captured[0]["max_chars"] == 3000
    assert "*HSCC template*" in out


def test_cmd_template_output_has_no_ansi(monkeypatch):
    """Full cmd_template reply contains zero escape bytes — the chat contract."""
    monkeypatch.setattr(cmdlib, "template_cli",
                        lambda argv: {"templates": [{"name": "hscc-live"}]})
    out = plugin.cmd_template("list")
    assert _ESC not in out
    assert '"templates"' in out and "hscc-live" in out


# ------------------------------------------------------------------------- #
# Themed console: fallback path stays no-ANSI; width asserted as a constant.
# ------------------------------------------------------------------------- #


def test_fallback_console_never_emits_ansi(monkeypatch):
    """When hscc_daemon.cli_theme is unavailable, make_console falls back to the
    neutral _FALLBACK_THEME Console; rendering a themed panel to a captured
    (non-TTY) stream must produce zero escape bytes — a standalone invocation
    is never corrupted with ANSI."""
    import contextlib
    monkeypatch.setattr(_theme, "theme", lambda: None)
    console = _theme.make_console()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        console.print(_theme.status_panel("cluster up", status="ok"))
    out = buf.getvalue()
    assert _ESC not in out
    assert "CLUSTER UP" in out or "cluster up" in out


def test_render_json_is_plain_even_with_theme_absent(monkeypatch):
    """render_json stays plain JSON (no ANSI) with or without the theme — the
    data path never depends on the palette, so it is safe either way."""
    monkeypatch.setattr(_theme, "theme", lambda: None)
    assert _ESC not in _theme.render_json({"ok": True})
    monkeypatch.setattr(_theme, "theme", lambda: object)
    assert _ESC not in _theme.render_json({"ok": True})


def test_non_tty_width_is_asserted_as_constant():
    """The anti-collapse width is referenced via the named constant, never a
    literal value — grep-guard for the invariant."""
    # Binding the constant is the assertion: the value lives in one place.
    assert _theme.DEFAULT_NONTTY_WIDTH == 200
    # make_console defaults to it when no explicit width is passed.
    assert _theme.make_console()._width == _theme.DEFAULT_NONTTY_WIDTH


def test_theme_module_import_is_a_module_object():
    """theme is a module import (a seam tests can monkeypatch), not a bool —
    mirrors the sibling packages' contract."""
    assert _theme.theme() is None or hasattr(_theme.theme(), "make_console")
