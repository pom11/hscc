"""HSCC CLI → Rich theme foundation, in the Hermes default skin palette.

WHY
---
The `hscc` CLI (entry -> hscc_daemon.hscc.main) is migrating from raw
``print(json.dumps(...))`` to Rich so its output matches the rest of Hermes —
which uses the "Classic Hermes — gold and kawaii" skin. HSCC is another layer
of Hermes, so it should look native. Every other HSCC-CLI-Rich card renders
through this module's helpers, so a single source of truth drives the palette.

This module is ONLY the theme foundation. It does not restyle daemon
background log lines / streaming debug print(), does not touch the machine
``--json`` output, and does not change what the hscc-cluster plugin engine
returns (rendering is the CLI's job, the engine still returns plain dicts).

Palette source of truth: hermes_cli/skin_engine.py ``_BUILTIN_SKINS["default"]``
(``colors`` = dark, plus its ``light_colors`` overlay). The hex values below
are copied verbatim from that dict so HSCC and Hermes cannot drift.
"""

from __future__ import annotations

import os

from rich.color import Color
from rich.console import Console
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.theme import Theme

__all__ = [
    "THEME",
    "PALETTES",
    "THEME_NAMES",
    "detect_theme",
    "resolve_theme",
    "theme_for",
    "make_console",
    "make_panel",
    "make_table",
    "make_status_panel",
]


# ── Palette (semantic role -> hex), verbatim from skin_engine.py default skin ─

_DARK = {
    # accent: ui_accent — headings / links / key-values / active
    "accent": "#FFBF00",
    # primary / title: banner_title — gold, the strongest highlight
    "primary": "#FFD700",
    "title": "#FFD700",
    # dim: banner_dim — muted / connectors / secondary lines
    "dim": "#B8860B",
    # label: ui_label — labels / secondary keys
    "label": "#DAA520",
    # border: banner_border — bronze, panels / rules / input rule
    "border": "#CD7F32",
    # text: banner_text — body / primary text (cornsilk, near-white on dark)
    "text": "#FFF8DC",
    # status colours are Hermes' ui_ok / ui_warn / ui_error, kept as-is
    "ok": "#4caf50",
    "warn": "#ffa726",
    "error": "#ef5350",
}

_LIGHT = {
    # light_colors overlay from skin_engine.py; border inherits the light
    # goldenrod ladder (light_colors does not override banner_border, so we
    # pick the label goldenrod — readable as both a border and a label on white)
    "accent": "#D89B04",
    "primary": "#C8961E",
    "title": "#C8961E",
    "dim": "#B8860B",
    "label": "#A97E10",
    "border": "#A97E10",
    "text": "#5C4718",
    "ok": "#2E7D32",
    "warn": "#D97706",
    "error": "#C62828",
}

# theme name -> semantic palette. The keys are what a user passes to
# ``--theme`` (dark | light); "default" aliases dark per the body ("Default
# auto-detect from terminal").
PALETTES: dict[str, dict[str, str]] = {
    "dark": _DARK,
    "light": _LIGHT,
}

DEFAULT_THEME = "dark"
THEME_NAMES = tuple(PALETTES.keys())


def detect_theme() -> str:
    """Best-effort dark/light guess from the terminal environment.

    Returns "dark" or "light". This is a heuristic only: Rich's Console
    already handles NO_COLOR and colour-system detection itself, so this
    function only picks the *palette*. Cheap explicit escapes win:
      - ``HSCC_THEME=light`` sets it outright (useful for shells that never
        set COLORTERM, e.g. many tmux/screen wrappers).
      - a light-looking `COLORTERM` (e.g. ``COLORTERM=light``) picks light.
      - otherwise default to dark (matches Hermes' own dark-authored skin).
    """
    explicit = os.getenv("HSCC_THEME", "").strip().lower()
    if explicit in PALETTES:
        return explicit
    colorterm = os.getenv("COLORTERM", "").strip().lower()
    if "light" in colorterm:
        return "light"
    return DEFAULT_THEME


def resolve_theme(override: str | None = None) -> str:
    """Return the effective theme name: explicit override or auto-detected.

    ``override`` is the value a caller parsed from ``--theme`` (or None).
    An invalid override is rejected loudly rather than silently ignored, so a
    typo in a script surfaces instead of unexpectedly rendering the wrong
    palette.
    """
    if override is not None:
        name = override.strip().lower()
        if name not in PALETTES:
            raise ValueError(
                f"unknown theme {override!r}; choose from {', '.join(THEME_NAMES)}"
            )
        return name
    return detect_theme()


def theme_for(name: str | None = None) -> Theme:
    """Build a Rich ``Theme`` from the requested palette (default auto-detect)."""
    palette = PALETTES[resolve_theme(name)]

    # Semantic style -> Rich style string. ``bold`` on the emphasis styles so
    # headings/labels/ok/error read strongly at the head of a line; body text
    # stays plain for readability.
    return Theme(
        {
            "accent": f"bold {palette['accent']}",
            "primary": f"bold {palette['primary']}",
            "title": f"bold {palette['title']}",
            "dim": palette["dim"],
            "label": f"bold {palette['label']}",
            "border": palette["border"],
            "text": palette["text"],
            "ok": f"bold {palette['ok']}",
            "warn": f"bold {palette['warn']}",
            "error": f"bold {palette['error']}",
        }
    )


# The canonical default theme (dark, from the Hermes-default palette). Import
# ``THEME`` when you want the default; use ``theme_for(name)`` /
# ``make_console(name)`` when the theme may vary (e.g. ``--theme light``).
THEME = theme_for(DEFAULT_THEME)


def make_console(name: str | None = None, **kwargs) -> Console:
    """Return a Console bound to the resolved theme.

    ``name`` is a ``--theme`` override or None for auto-detect. Extra ``**kwargs``
    pass through to Console (e.g. ``file=`` for capturing, ``highlight=False``).

    Rich's Console handles the ``NO_COLOR`` env convention and colour-system
    auto-detection itself, so we do not re-implement either here — we only
    supply the palette. Colour is NEVER forced on: if the terminal is a pipe
    or has NO_COLOR set, output degrades to plain text automatically.
    """
    return Console(theme=theme_for(name), **kwargs)


def _view_title(title: str) -> str:
    """Wrap a title in the gold ``title`` style markup."""

    # Panel/Table titles parse Rich markup, so ``[title]<t>[/title]`` renders
    # the text gold. Escape literal brackets in the title first so a title
    # containing markup cannot inject styles or break the box.
    escaped = title.replace("[", r"\[").replace("]", r"\]")
    return f"[title]{escaped}[/title]"


# ── Standard constructors downstream cards render through ────────────────

def make_panel(title: str, renderable: str = "", **kwargs) -> Panel:
    """A Panel with a gold title and a bronze border.

    ``title`` is rendered in the ``title`` style; the box border uses the
    ``border`` style. Named styles (``title``/``border``) resolve against the
    console's active theme at print time, so the same renderable automatically
    picks the dark or light palette. Rich's Panel has no separate
    ``title_style`` parameter, so the title colour is applied via inline markup
    and the border via ``border_style``. Cards pass the body as ``renderable``
    (a ``str`` or any Rich renderable) or via ``**kwargs``.
    """
    kwargs.setdefault("border_style", "border")
    kwargs.setdefault("title_align", "left")
    return Panel(renderable, title=_view_title(title), **kwargs)


def make_table(title: str | None = None, **kwargs) -> Table:
    """A Table pre-skinned for the Hermes palette.

    The header row is accented, the title is gold, and visible box lines use
    the bronze ``border`` style so tables match panels/rules. Style names
    resolve against the console's active theme at print time.
    """
    kwargs.setdefault("header_style", "accent")
    kwargs.setdefault("title_style", "title")
    kwargs.setdefault("border_style", "border")
    kwargs.setdefault("expand", False)
    if title is not None:
        title = _view_title(title)
    return Table(title=title, **kwargs)


def _palette_color(theme_name: str | None, role: str) -> Color:
    """Resolve a semantic role to a concrete Color for the effective palette."""
    palette = PALETTES[resolve_theme(theme_name)]
    return Color.parse(palette[role])


def make_status_panel(
    message: str,
    status: str = "ok",
    *,
    title: str = "status",
    theme: str | None = None,
    **kwargs,
) -> Panel:
    """A status Panel whose box + glyph are coloured by ok/warn/error.

    ``status`` selects the semantic colour from the theme ("ok" | "warn" |
    "error"; anything else falls back to the gold title so an unknown status
    still renders sanely). An explicit ``color`` kwarg overrides the status
    lookup entirely (useful for custom states like "info" or "idle").
    """
    color_role = kwargs.pop("color", None) or (
        status if status in ("ok", "warn", "error") else "title"
    )
    border_style = Style(color=_palette_color(theme, color_role))
    kwargs.setdefault("title_align", "left")
    kwargs.setdefault("border_style", border_style)
    return Panel(
        f"[{color_role}]{status.upper()}[/]  {message}",
        title=_view_title(title),
        **kwargs,
    )
