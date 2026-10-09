#!/usr/bin/env python3
"""Print the source of every function in a flightdeck command module that
contains a Rich render call (console.print / panel / status_panel / add_row /
add_column / table), with line numbers, so a human can judge which interpolations
carry string-derived DATA.

Usage: show_render_fns.py <file.py> [...]
"""
import ast
import sys
from pathlib import Path

RICH = {"print", "add_row", "add_column"}
CTOR = {"panel", "status_panel", "table", "rule"}


def kind(call, src):
    f = call.func
    if isinstance(f, ast.Attribute) and f.attr in RICH:
        base = ast.get_source_segment(src, f.value) or ""
        if base in ("sys", "sys.stderr", "json"):
            return None
        return f.attr
    if isinstance(f, ast.Name) and f.id in CTOR:
        return f.id
    return None


def main(paths):
    for p in paths:
        src = Path(p).read_text(encoding="utf-8")
        tree = ast.parse(src, filename=p)
        lines = src.splitlines()
        hits = []
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            kinds = set()
            for n in ast.walk(fn):
                if isinstance(n, ast.Call):
                    k = kind(n, src)
                    if k:
                        kinds.add(k)
            if kinds:
                hits.append((fn, sorted(kinds)))
        if not hits:
            continue
        print(f"##################### {p}")
        for fn, kinds in hits:
            print(f"--- def {fn.name}({''}) L{fn.lineno}-{fn.end_lineno} "
                  f"render={','.join(kinds)}")
            for i in range(fn.lineno - 1, min(fn.end_lineno, len(lines))):
                print(f"{i+1:5d}| {lines[i]}")
            print()


if __name__ == "__main__":
    main(sys.argv[1:])
