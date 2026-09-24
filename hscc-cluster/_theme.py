"""hscc-cluster themed-render helper — shared by the standalone CLI entry points.

WHY
---
hscc-cluster is converting its standalone/back-compat CLI entry points
(``hscc.py:main()``, ``cluster_template_cli.py __main__``, ``cluster_template.py
main()``, ``gateway_restart.py __main__``) from bare ``print(...)`` /
``json.dumps(...)`` to the themed Rich layer, matching the epic established for
hscc_daemon and flightdeck. The single source of truth for that palette is
``hscc_daemon.cli_theme`` (Hermes-default gold, dark + light). This module is the
hscc-cluster-side handle on it.

hscc_daemon is a PEER plugin (+ a repo-root sibling dir), so it is on sys.path
in the normal ``hscc`` process and in the test workspace, but NOT necessarily in
a truly standalone hscc-cluster invocation. So the import is deferred + guarded
(the same pattern flightdeck/commands/_theme.py uses): when hscc_daemon is
unavailable we fall back to a plain ``rich.console.Console`` bound to
``_FALLBACK_THEME`` (semantic names registered as neutral styles, so markup never
raises MissingStyle while output stays unthemed); only if ``rich`` itself is
missing does the module refuse to import.

HARD INVARIANT (inherited from the epic): the machine ``--json`` path stays
BYTE-IDENTICAL — the ``--json`` branches still ``print(json.dumps(...))`` raw,
never routed through a Console. Non-TTY stdout degrades to PLAIN — Rich's Console
detects colour-system itself and emits no ANSI on a pipe/capture; the regression
tests per command pin this.

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

# Fallback Theme used ONLY when hscc_daemon.cli_theme is unavailable (a truly
# standalone hscc-cluster invocation, or any context where the repo root is not
# on sys.path). It registers the SAME semantic names as the real palette so every
# entry point's markup / ``border_style`` resolves, but maps them to
# intentionally NEUTRAL styles (no colour), so the fallback stays genuinely
# unthemed while never crashing with MissingStyle. Without this, any markup that
# references ``border_style="error"`` or ``[dim]`` / ``[error]`` / ``[ok]`` /
# ``[label]`` dies on a plain Console because those names are not valid colours.
# Bold is kept on the emphasis names so output still reads structurally.
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

    Deferred + guarded so a standalone hscc-cluster invocation (hscc_daemon not on
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


# The non-tty anti-collapse width: when stdout is a pipe/capture, Rich would
# otherwise default to 80 and word-wrap genuinely wider rows (cluster hosts,
# template file lists) mid-phrase or collapse a Rich table's leading column. Only
# used when the resolved console is NOT a terminal and no explicit width was
# passed; a real TTY uses the actual terminal width. Asserted as a constant, not
# a literal value, in every no-ANSI regression test.
DEFAULT_NONTTY_WIDTH = 200


def make_console(_name: str | None = None, **kwargs) -> Console:
    """Return a Console for the human view.

    When ``hscc_daemon.cli_theme`` is available, the Console is themed and honours
    any ``HSCC_THEME`` env override (and would honour a ``--theme`` ``_name``);
    otherwise a plain Console bound to ``_FALLBACK_THEME`` (the semantic names
    registered as neutral styles, so markup never raises MissingStyle while output
    stays unthemed). Rich detects the colour-system itself, so on a non-TTY stdout
    this emits PLAIN text with no ANSI — the mandatory regression is that piped
    output stays clean and the machine ``--json`` path stays the only JSON.
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
