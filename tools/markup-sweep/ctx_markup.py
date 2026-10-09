#!/usr/bin/env python3
"""Print a compact, patch-ready context window around every Rich render site
that interpolates an expression NOT already passed through escape()/esc().

Reports the *flow-resolved* suspects (assignments feeding a renderable) the same
way flow_markup.py does, but instead of just naming the variables it prints the
source lines you would have to edit, so a human/agent can write the patch
directly from the output.

Usage: ctx_markup.py <file.py> [...]
"""
import ast
import sys
from pathlib import Path

RICH = {"print", "add_row", "add_column"}
CTOR = {"panel", "status_panel", "table", "rule"}
# Wrappers whose output cannot smuggle markup. NOTE: repr/!r deliberately NOT
# here — repr("a[/bold]b") keeps the brackets, so it is still an injection vector.
SAFE = {"escape", "esc", "len", "int", "float", "min", "max", "sorted",
        "_human_bytes", "_fmt_duration", "_short", "_card_label",
        "_format_session_row", "_iso", "_human", "_window_str", "_format_age"}


def is_safe(node):
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.JoinedStr):
        return all(is_safe(v.value) for v in node.values
                   if isinstance(v, ast.FormattedValue))
    if isinstance(node, ast.Call):
        return getattr(node.func, "attr", getattr(node.func, "id", "")) in SAFE
    if isinstance(node, ast.BinOp):
        return is_safe(node.left) and is_safe(node.right)
    if isinstance(node, ast.IfExp):
        return all(is_safe(x) for x in (node.body, node.orelse))
    if isinstance(node, ast.FormattedValue):
        return is_safe(node.value)
    return False


def suspects(node, src):
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.JoinedStr):
            for v in sub.values:
                if isinstance(v, ast.FormattedValue) and not is_safe(v.value):
                    out.append((v.lineno, ast.get_source_segment(src, v.value)))
    return out


def kind(call, src):
    f = call.func
    if isinstance(f, ast.Attribute) and f.attr in RICH:
        base = ast.get_source_segment(src, f.value) or ""
        if base.startswith("sys") or base in ("json",):
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
        # name -> assignment value nodes within each function scope (coarse: whole file)
        assigns = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        assigns.setdefault(t.id, []).append(n.value)
            elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
                assigns.setdefault(n.target.id, []).append(n.value)
            elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr in ("append", "extend")
                  and isinstance(n.func.value, ast.Name)):
                assigns.setdefault(n.func.value.id, []).extend(n.args)

        def resolve(node, depth=0, seen=None):
            if node is None or depth > 3:
                return []
            seen = seen or set()
            if isinstance(node, ast.Name):
                if node.id in seen:
                    return []
                seen = seen | {node.id}
                out = []
                for v in assigns.get(node.id, []):
                    out.extend(resolve(v, depth + 1, seen))
                return out
            if isinstance(node, (ast.List, ast.Tuple)):
                out = []
                for e in node.elts:
                    out.extend(suspects(e, src))
                    out.extend(resolve(e, depth + 1, seen))
                return out
            if isinstance(node, ast.Call):
                fname = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if fname == "join":
                    out = []
                    for a in node.args:
                        out.extend(resolve(a, depth + 1, seen))
                    return out
                if fname in CTOR or fname in RICH:
                    out = []
                    for a in list(node.args) + [k.value for k in node.keywords]:
                        out.extend(suspects(a, src))
                        out.extend(resolve(a, depth + 1, seen))
                    return out
            return suspects(node, src)

        want = set()
        report = []
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            k = kind(n, src)
            if k is None:
                continue
            sug = []
            for a in list(n.args) + [kw.value for kw in n.keywords]:
                sug.extend(suspects(a, src))
                sug.extend(resolve(a))
            sug = sorted(set(sug))
            if sug:
                report.append((n.lineno, k, sug))
                want.add(n.lineno)
                want.update(l for l, _ in sug)
        if not report:
            continue
        print(f"##################### {p}")
        shown = set()
        for ln in sorted(want):
            lo, hi = max(1, ln - 2), min(len(lines), ln + 2)
            if shown >= set(range(lo, hi)):
                continue
            for i in range(lo, hi + 1):
                if i in shown:
                    continue
                shown.add(i)
                print(f"{i:5d}| {lines[i-1]}")
            print("        ----")
        for ln, k, sug in sorted(report):
            print(f"  SITE L{ln} [{k}] " + " ; ".join(f"{s}" for _, s in sug))


if __name__ == "__main__":
    main(sys.argv[1:])
