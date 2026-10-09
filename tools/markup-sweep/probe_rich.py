#!/usr/bin/env python3
"""Probe Rich semantics that decide the sweep's idiom (t_12f3c8a8).

Answers, with real Rich output (not reasoning):
  1. does an unescaped [/bold] in a Panel body / Table row / title raise?
  2. does escape() neutralise it?
  3. does {escape(x)!r} -- escape THEN a conversion flag -- stay safe, or does
     repr() re-open the hole (repr doubles the backslash escape)?
  4. does esc(repr(x)) stay literal?
"""
import io

from rich.console import Console
from rich.errors import MarkupError
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

BAD = "a[bold]b[/bold]c"
CLOSE = "path/x[/bold]"


def render(renderable):
    buf = io.StringIO()
    Console(file=buf, width=200, color_system=None).print(renderable)
    return buf.getvalue()


def probe(label, fn):
    try:
        out = fn()
        print(f"  OK    {label}: {out.strip()[:90]!r}")
    except MarkupError as exc:
        print(f"  RAISE {label}: MarkupError: {exc}")


print("1. raw closing tag in a Panel body:")
probe("panel body raw", lambda: render(Panel(CLOSE, title="t")))
probe("panel body escape()", lambda: render(Panel(escape(CLOSE), title="t")))
probe("panel TITLE raw", lambda: render(Panel("x", title=CLOSE)))
probe("panel TITLE escape()", lambda: render(Panel("x", title=escape(CLOSE))))

print("2. table row + title:")


def _t1():
    t = Table()
    t.add_column("c")
    t.add_row(CLOSE)
    return render(t)


def _t2():
    t = Table()
    t.add_column("c")
    t.add_row(escape(CLOSE))
    return render(t)


def _t3():
    t = Table(title=CLOSE)
    t.add_column("c")
    t.add_row("ok")
    return render(t)


probe("add_row raw", _t1)
probe("add_row escape()", _t2)
probe("Table title raw", _t3)

print("3. the {escape(x)!r} idiom (escape THEN repr):")


def _repr_after_escape():
    return render(Panel(f"[ok]{escape(CLOSE)!r}[/ok]", title="t"))


def _repr_after_esc():
    from rich.markup import escape as e
    return render(Panel(f"[ok]{e(repr(CLOSE))}[/ok]", title="t"))


def _esc_repr_plain():
    from rich.markup import escape as e
    return render(Panel(f"{e(repr(CLOSE))}", title="t"))


probe("{escape(x)!r}", _repr_after_escape)
probe("{esc(repr(x))}", _repr_after_esc)
probe("{esc(repr(x))} no style", _esc_repr_plain)

print("4. does repr() alone leave the brackets?")
print("   repr(CLOSE) =", repr(repr(CLOSE)))
print("   escape(CLOSE) =", repr(escape(CLOSE)))
print("   escape(repr(CLOSE)) =", repr(escape(repr(CLOSE))))


def _raw_repr():
    return render(Panel(f"{repr(CLOSE)}", title="t"))


probe("{repr(x)} alone", _raw_repr)

print("5. nested markup-open tag only (no close):")
probe("body [bold only", lambda: render(Panel("oops [bold here", title="t")))
