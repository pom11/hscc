"""Tests for hscc_daemon/cli_theme.py — the shared Rich theme foundation.

The palettes must stay byte-identical to Hermes' default skin
(hermes_cli/skin_engine.py ``_BUILTIN_SKINS["default"]``: ``colors`` = dark,
``light_colors`` = light). These tests pin the exact hex values so a palette
drift between HSCC and Hermes is caught in CI, not noticed on a terminal.
"""

import io

import pytest
from rich.panel import Panel
from rich.table import Table
from rich.theme import Theme

from hscc_daemon import cli_theme as t


# ── Palette must match Hermes' default skin (skin_engine.py) exactly ─────

_expected_dark = {
    "accent": "#FFBF00", "primary": "#FFD700", "title": "#FFD700",
    "dim": "#B8860B", "label": "#DAA520", "border": "#CD7F32",
    "text": "#FFF8DC", "ok": "#4caf50", "warn": "#ffa726", "error": "#ef5350",
}

_expected_light = {
    "accent": "#D89B04", "primary": "#C8961E", "title": "#C8961E",
    "dim": "#B8860B", "label": "#A97E10", "border": "#A97E10",
    "text": "#5C4718", "ok": "#2E7D32", "warn": "#D97706", "error": "#C62828",
}


@pytest.mark.parametrize("palette_name,expected", [
    ("dark", _expected_dark),
    ("light", _expected_light),
])
def test_palette_matches_hermes_default_skin(palette_name, expected):
    assert t.PALETTES[palette_name] == expected
    # And the same roles must be present in each palette (no missing keys).
    assert set(expected) <= set(t.PALETTES[palette_name])


def test_theme_names():
    assert t.THEME_NAMES == ("dark", "light")


def test_module_exposes_default_theme():
    # The deliverable exposes a canonical ``THEME`` (default/dark palette).
    assert isinstance(t.THEME, Theme)
    assert t.THEME.styles["accent"].color.name.lower() == "#ffbf00"


def test_theme_for_returns_theme_with_correct_styles():
    th = t.theme_for("dark")
    assert isinstance(th, Theme)
    # Styles carry the palette hex (lowercased by rich's Color).
    assert th.styles["accent"].color.name.lower() == "#ffbf00"
    assert th.styles["title"].color.name.lower() == "#ffd700"
    assert th.styles["error"].color.name.lower() == "#ef5350"


@pytest.mark.parametrize("style_name", ["accent", "primary", "title", "dim",
                                        "label", "border", "text", "ok",
                                        "warn", "error"])
def test_every_style_is_resolvable(style_name):
    for name in t.THEME_NAMES:
        th = t.theme_for(name)
        assert style_name in th.styles  # resolvable by Console


# ── Theme resolution / auto-detect ────────────────────────────────────────

def test_resolve_theme_accepts_both_palettes():
    assert t.resolve_theme("dark") == "dark"
    assert t.resolve_theme("light") == "light"
    # Case-insensitive + whitespace-tolerant.
    assert t.resolve_theme("  LIGHT ") == "light"


def test_resolve_theme_rejects_unknown(monkeypatch):
    with pytest.raises(ValueError):
        t.resolve_theme("blurple")


def test_detect_theme_obeys_hscc_theme_override(monkeypatch):
    monkeypatch.setenv("HSCC_THEME", "light")
    monkeypatch.delenv("COLORTERM", raising=False)
    assert t.detect_theme() == "light"
    monkeypatch.setenv("HSCC_THEME", "dark")
    assert t.detect_theme() == "dark"


def test_detect_theme_picks_light_from_colorterm(monkeypatch):
    monkeypatch.setenv("COLORTERM", "light")
    monkeypatch.setenv("HSCC_THEME", "")
    assert t.detect_theme() == "light"


def test_detect_theme_defaults_dark(monkeypatch):
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("HSCC_THEME", "")
    assert t.detect_theme() == "dark"


# ── Console factory ───────────────────────────────────────────────────────

def test_make_console_uses_requested_theme():
    c = t.make_console("light", file=io.StringIO())
    # The Console carries the light-theme styles, so its ``style`` resolution
    # for a semantic name yields the light accent.
    assert "d89b04" in str(c.get_style("accent"))


# ── Constructors downstream cards render through ──────────────────────────

def test_make_panel_returns_panel_with_themed_border():
    p = t.make_panel("A title")
    assert isinstance(p, Panel)
    # Border style is the ``border`` semantic (bronze on dark).
    assert p.border_style == "border"
    # Title is wrapped in the ``title`` style markup so it renders gold.
    assert "[title]" in p.title


def test_make_table_returns_table_with_styles():
    tb = t.make_table("A table")
    assert isinstance(tb, Table)
    assert tb.header_style == "accent"
    assert tb.title_style == "title"
    assert tb.border_style == "border"


def test_make_status_panel_boxes_differ_by_status():
    ok = t.make_status_panel("fine", "ok")
    warn = t.make_status_panel("low", "warn")
    err = t.make_status_panel("down", "error")
    assert isinstance(ok, Panel)
    # Each status maps to a distinct border colour.
    colors = {p.border_style.color.name for p in (ok, warn, err)}
    assert len(colors) == 3


def test_make_status_panel_unknown_status_falls_back_to_title():
    p = t.make_status_panel("idle", "idle")  # not ok/warn/error
    # Falls back to the ``title`` colour — still resolves to a real colour.
    assert p.border_style.color is not None
    assert "[title]" in p.title


def test_make_status_panel_color_override():
    p = t.make_status_panel("custom", "ok", color="accent")
    # Explicit ``color`` wins over the status lookup.
    assert p.border_style.color.name.lower() == "#ffbf00"
