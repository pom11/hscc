"""hscc-bootstrap themed-render helper — shared by every bootstrap Rich card.

WHY
---
hscc-bootstrap is converting its human-facing CLI output from bare
``print(...)`` / ``json.dumps(...)`` to the themed Rich layer, matching the
pool the [1/5]..[5/5] Rich epic established for hscc_daemon and the flightdeck
``_theme`` pattern (hscc-project/flightdeck/commands/_theme.py). The single
source of truth for that palette is ``hscc_daemon.cli_theme`` (Hermes-default
gold, dark + light). This module is the bootstrap-side handle on it.

These bootstrap scripts deploy as FLAT modules (the ``hscc-bootstrap`` dir has
no ``__init__.py``): each runs standalone via ``python <script>.py`` and its
tests import modules bare (``import doctor``) with this dir on sys.path
(``tests/conftest.py``). So unlike flightdeck's ``from ._theme import ...``
relative import, bootstrap imports this as a bare ``import _theme`` — which
resolves identically when running a script directly (script dir on
sys.path[0]) and under the test suite (conftest inserts this dir).

The hscc_daemon import is deferred + guarded (the same pattern flightdeck
uses): when hscc_daemon is unavailable we fall back to a plain
``rich.console.Console`` so bootstrap still renders (just unthemed); only if
``rich`` itself is missing does the module refuse to import.

HARD INVARIANT (inherited from the Rich epic and the task's own invariant):
the machine ``--json`` output stays BYTE-IDENTICAL — the JSON `print(json.dumps)`
paths in the bootstrap modules still print raw, never routed through a Console.
Non-TTY stdout degrades to PLAIN — Rich's Console detects colour-system itself
and emits no ANSI on a pipe/capture; the per-command no-ANSI regression tests
pin this.

``theme`` is a module import, not a constant, so tests can monkeypatch
``hscc_daemon.cli_theme`` attributes if they ever need to (they shouldn't).
"""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.theme import Theme

# The hscc_daemon.cli_theme module, or None when the peer package is absent.
_theme = None
_theme_loaded = False

# Fallback Theme used ONLY when hscc_daemon.cli_theme is unavailable (a
# standalone bootstrap install, or any invocation where the repo/parents are
# not on sys.path). It registers the SAME semantic names as the real palette so
# every card's markup / ``border_style`` resolves, but maps them to
# intentionally NEUTRAL styles (no colour), so the fallback stays genuinely
# unthemed while never crashing with MissingStyle. Without this, any card that
# renders ``border_style="error"`` or ``[dim]`` / ``[error]`` / ``[ok]`` /
# ``[label]`` markup dies on a plain Console because 'error' etc. are not valid
# colour names. Bold is kept on the emphasis names so output still reads
# structurally.
_FALLBACK_THEME = Theme(
    {
        "accent": "bold",
        "primary": "bold",
        "title": "bold",
        "dim": "",
        "label": "bold",
        "border": "",
        "text": "",
        "ok": "bold",
        "warn": "bold",
        "error": "bold",
    }
)


def theme():
    """Return the ``hscc_daemon.cli_theme`` module, or None when unavailable.

    Deferred + guarded so a standalone bootstrap install (hscc_daemon not on
    sys.path) still imports and renders using the plain fallback below.
    """
    global _theme, _theme_loaded
    if not _theme_loaded:
        _theme_loaded = True
        try:
            from hscc_daemon import cli_theme as _t
            _theme = _t
        except ImportError:
            _theme = None
    return _theme


def make_console(_name: str | None = None, **kwargs) -> Console:
    """Return a Console for the human view.

    When ``hscc_daemon.cli_theme`` is available, the Console is themed and
    honours any ``HSCC_THEME`` env override (and would honour a ``--theme``
    ``_name``); otherwise a plain Console bound to ``_FALLBACK_THEME`` (the
    semantic names registered as neutral styles, so markup never raises
    MissingStyle while output stays unthemed). Rich detects the colour-system
    itself, so on a non-TTY stdout this emits PLAIN text with no ANSI — the
    mandatory regression is that ``--json`` stays the only machine path and
    piped output stays clean.

    Width: on a real TTY Rich uses the actual terminal width. For a non-tty
    file (piped / captured) Rich would otherwise default to 80, which word-wraps
    genuinely wider lines (repo paths, multi-column lists). So when the resolved
    console is NOT a terminal and no explicit ``width=`` was given, it is
    recreated at width 200: content stays on one line in a pipe, where
    preserving content beats wrapping. An explicit ``width=`` always wins.
    """
    kwargs.setdefault("width", 200)
    t = theme()
    if t is not None:
        console = t.make_console(_name, **kwargs)
    else:
        console = Console(theme=_FALLBACK_THEME, **kwargs)
    if console.is_terminal:
        # A real terminal: drop the fixed width so Rich uses the actual screen
        # width (a hard 200 would overflow a narrower TTY).
        widthless = {k: v for k, v in kwargs.items() if k != "width"}
        if t is not None:
            console = t.make_console(_name, **widthless)
        else:
            console = Console(theme=_FALLBACK_THEME, **widthless)
    return console


def panel(title: str, renderable: str = "", **kwargs):
    """Themed Panel (gold title, bronze border) with plain-Console fallback."""
    t = theme()
    if t is not None:
        return t.make_panel(title, renderable, **kwargs)
    from rich.panel import Panel
    kwargs.setdefault("title_align", "left")
    return Panel(renderable, title=title, **kwargs)
