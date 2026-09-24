"""hscc-commands themed-render helper — shared by the slash-command handlers.

WHY
---
hscc-commands is converting its one raw machine-data render site — the
``json.dumps(...)`` inside ``cmd_template`` that embeds a template CLI result
JSON block into a chat reply — onto the themed Rich layer, matching the epic
established for hscc_daemon, flightdeck, hscc-cluster and hscc-roles. The
single source of truth for that palette is ``hscc_daemon.cli_theme``
(Hermes-default gold, dark + light). This module is the hscc-commands-side
handle on it.

hscc_daemon is a PEER plugin (+ a repo-root sibling dir), so it is on sys.path
in the normal ``hscc`` process and in the test workspace, but NOT necessarily
in a truly standalone hscc-commands invocation. So the import is deferred +
guarded (the same pattern flightdeck/commands/_theme.py and
hscc-cluster/_theme.py use): when hscc_daemon is unavailable we fall back to a
plain ``rich.console.Console`` bound to ``_FALLBACK_THEME`` (semantic names
registered as neutral styles, so markup never raises MissingStyle while output
stays unthemed); only if ``rich`` itself is missing does the module refuse to
import.

HARD INVARIANT (inherited from the epic): a slash command's reply is never a
terminal — it is delivered to chat, so it must NEVER carry ANSI escape bytes.
``render_json`` returns the verbatim ``json.dumps(..., indent=..., default=str)``
string (deliberately bypassing a wrapping Console so the JSON block is never
reflowed or truncated differently), which by construction never contains ANSI; a
regression test pins this against a named constant. The one place a value should
be a named constant (never a literal escape byte) is pinned in the regression
test for this package.

``theme`` is a module import, not a constant, so tests can monkeypatch
``hscc_daemon.cli_theme`` attributes if they ever need to (they shouldn't).
"""

from __future__ import annotations

from rich.console import Console
from rich.theme import Theme

# The hscc_daemon.cli_theme module, or None when the peer package is absent.
_theme = None
_theme_loaded = False

# Fallback Theme used ONLY when hscc_daemon.cli_theme is unavailable (a truly
# standalone hscc-commands invocation, or any context where the repo root is not
# on sys.path). It registers the SAME semantic names as the real palette so every
# handler's markup / ``border_style`` resolves, but maps them to intentionally
# NEUTRAL styles (no colour), so the fallback stays genuinely unthemed while
# never crashing with MissingStyle. Bold is kept on the emphasis names so output
# still reads structurally.
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

    Deferred + guarded so a standalone hscc-commands invocation (hscc_daemon not
    on sys.path) still imports and renders using the plain fallback below.
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


# The non-tty anti-collapse width: when stdout is a pipe/capture, Rich would
# otherwise default to 80 and word-wrap genuinely wider rows (template file
# lists) mid-phrase or collapse a Rich table's leading column. Only used when
# the resolved console is NOT a terminal and no explicit width was passed; a
# real TTY uses the actual terminal width. A slash command's reply is never a
# terminal, so this is the width that always applies to its rendered output.
# Asserted as a constant, not a literal value, in the no-ANSI regression test.
DEFAULT_NONTTY_WIDTH = 200


def make_console(_name: str | None = None, **kwargs) -> Console:
    """Return a Console for the human view.

    When ``hscc_daemon.cli_theme`` is available, the Console is themed and
    honours any ``HSCC_THEME`` env override (and would honour a ``--theme``
    ``_name``); otherwise a plain Console bound to ``_FALLBACK_THEME`` (the
    semantic names registered as neutral styles, so markup never raises
    MissingStyle while output stays unthemed). Rich detects the colour-system
    itself, so on a non-TTY stdout this emits PLAIN text with no ANSI — the
    mandatory invariant for a slash-command reply.
    """
    kwargs.setdefault("width", DEFAULT_NONTTY_WIDTH)
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


def render_json(payload, *, indent: int = 2, max_chars: int | None = None) -> str:
    """Render ``payload`` to a PLAIN, no-ANSI JSON string — the data path.

    A slash command's reply is a chat string — never a terminal — so it must
    never carry ANSI escape bytes. JSON is DATA, not prose: routing it through
    a wrapping Rich Console (``console.print`` + ``export_text``) would
    word-wrap long lines and change the truncation count, corrupting the block
    that consumers may parse. So this deliberately bypasses the Console and
    returns the verbatim ``json.dumps(..., indent=..., default=str)`` string,
    byte-identical to the raw site it replaces (the epic's rule that machine
    JSON stays byte-identical), truncated to ``max_chars`` exactly as before.

    ``json.dumps`` never emits ANSI escape bytes, so the no-ANSI invariant is
    guaranteed structurally; the regression test still pins it against the
    ``_ESC`` constant. ``max_chars`` mirrors the historical truncation in the
    raw site (the old chat reply capped the JSON block at 3000 chars) so
    behaviour is unchanged.
    """
    import json
    text = json.dumps(payload, indent=indent, default=str)
    if max_chars is not None:
        text = text[:max_chars]
    return text


def panel(title: str, renderable: str = "", **kwargs):
    """Themed Panel (gold title, bronze border) with plain-Console fallback."""
    t = theme()
    if t is not None:
        return t.make_panel(title, renderable, **kwargs)
    from rich.panel import Panel
    kwargs.setdefault("title_align", "left")
    return Panel(renderable, title=title, **kwargs)


def status_panel(message: str, status: str = "ok", *, title: str = "status",
                 _theme: str | None = None, **kwargs):
    """Themed status Panel coloured by ok/warn/error, with plain fallback."""
    t = theme()
    if t is not None:
        return t.make_status_panel(message, status, title=title, theme=_theme,
                                   **kwargs)
    from rich.panel import Panel
    kwargs.setdefault("title_align", "left")
    return Panel(f"{status.upper()}  {message}", title=title, **kwargs)


def table(title: str | None = None, **kwargs):
    """Themed Table (accent header, gold title, bronze border), plain fallback."""
    t = theme()
    if t is not None:
        return t.make_table(title, **kwargs)
    from rich.table import Table
    return Table(title=title, **kwargs)
