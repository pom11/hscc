#!/usr/bin/env python3
"""Prove the flightdeck markup-escape sweep is complete (t_12f3c8a8).

For every Rich render surface (Console.print / panel / status_panel / table /
add_row / add_column) in the given files, resolve the renderable body — direct
f-strings AND the variables/assignments feeding them (list builds, .append()s,
`lines += [...]`, `"\n".join(lines)`) — and report every interpolated expression
that is not provably markup-safe.

An expression is SAFE when it is:
  * a constant (str/int/None/...), or an f-string whose fields are all safe;
  * a call to escape()/esc() (or another whitelisted pure formatter);
  * a name whose every assignment in the enclosing function is safe (this is
    what kills the `plural = "" if len(rows) == 1 else "s"` class of noise);
  * len()/int()/sorted()/... over safe args.

Deliberately NOT safe: repr()/`!r` — repr("a[/bold]b") keeps the brackets, so
repr'd data still needs esc().

Usage: check_sweep.py <repo/relative/path.py> [...]   (from the workspace root)
Exit 0 = every render site clean; exit 1 = remaining sites listed.
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "hscc"

RICH = {"print", "add_row", "add_column"}
CTOR = {"panel", "status_panel", "table", "rule"}
# Only numeric/structural builtins and the escapers themselves count as safe.
# Module helpers (_card_label, _short, _window_str, ...) are deliberately NOT
# whitelisted: a "formatter" may return raw data — start.py's _card_label
# returns f"{card['id']} {card['title']}", i.e. an unescaped card title — so
# the escaper has to be visible AT THE CALL SITE. That is also exactly what the
# card's acceptance asks for ("every f-string that interpolates non-constant
# text passes it through the escaper").
SAFE_FUNCS = {"escape", "esc", "len", "int", "float", "min", "max", "sorted",
              "abs", "round", "sum", "count"}

# Sites the checker cannot prove safe by expression shape alone, each reviewed
# by hand and recorded with WHY. The allow-list is part of the audit artifact: a
# site may only be silenced here with a written justification. Keys are per-file
# EXPRESSION TEXT (not line numbers) — the justification is about where that
# value comes from, so it holds wherever the same expression appears, and a
# different expression at the same line is still reported.
ALLOW = {}


def _allowed(rel, expr):
    return ALLOW.get(rel, {}).get(expr)


class Scope:
    """Per-function assignment index + a safety verdict per expression."""

    def __init__(self, fn, src):
        self.src = src
        self.assigns = {}
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        self.assigns.setdefault(t.id, []).append(n.value)
            elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
                self.assigns.setdefault(n.target.id, []).append(n.value)
            elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr in ("append", "extend")
                  and isinstance(n.func.value, ast.Name)):
                self.assigns.setdefault(n.func.value.id, []).extend(n.args)

    def safe(self, node, depth=0, seen=None):
        if node is None or depth > 6:
            return False
        seen = seen or frozenset()
        if isinstance(node, ast.Constant):
            return True
        if isinstance(node, ast.JoinedStr):
            return all(self.safe(v.value, depth + 1, seen) for v in node.values
                       if isinstance(v, ast.FormattedValue))
        if isinstance(node, ast.Call):
            fname = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if fname in SAFE_FUNCS:
                return True
            if fname == "join":
                # "\n".join(lines) is safe iff every value appended to lines is
                return all(self.safe(a, depth + 1, seen) for a in node.args)
            return False
        if isinstance(node, ast.BinOp):
            return self.safe(node.left, depth + 1, seen) and \
                self.safe(node.right, depth + 1, seen)
        if isinstance(node, ast.IfExp):
            return all(self.safe(x, depth + 1, seen)
                       for x in (node.body, node.orelse))
        if isinstance(node, ast.Name):
            if node.id in seen or node.id not in self.assigns:
                return False
            nxt = seen | {node.id}
            vals = self.assigns[node.id]
            return bool(vals) and all(self.safe(v, depth + 1, nxt) for v in vals)
        return False

    def suspects(self, node):
        """Unescaped interpolated expressions anywhere inside this arg."""
        out = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.JoinedStr):
                for v in sub.values:
                    if not isinstance(v, ast.FormattedValue):
                        continue
                    if v.conversion not in (-1, None) and not isinstance(
                            v.value, ast.Constant):
                        # !r / !s / !a run AFTER escape(): escape() inserts a
                        # backslash, repr() then escapes that backslash, and the
                        # bracket is live again. Probed against real Rich:
                        # f"{escape('x[/bold]')!r}" still raises MarkupError.
                        # The safe form is esc(repr(x)) / esc(str(x)). A
                        # conversion on a literal constant stays harmless.
                        out.append((v.lineno,
                                    ast.get_source_segment(self.src, v)))
                        continue
                    if not self.safe(v.value):
                        out.append((v.lineno,
                                    ast.get_source_segment(self.src, v.value)))
        return out

    def resolve(self, node, depth=0, seen=None):
        """Suspects in a renderable arg, following names through assignments.

        NOTE: never run suspects() directly on a nested render call — that would
        walk into its TITLE argument, which _theme escapes centrally. Every
        nested call is descended through this function so title_args() applies
        at each level.
        """
        if node is None or depth > 4:
            return []
        seen = seen or set()
        if isinstance(node, ast.Name):
            if node.id in seen:
                return []
            seen = seen | {node.id}
            out = []
            for v in self.assigns.get(node.id, []):
                out.extend(self.resolve(v, depth + 1, seen))
            return out
        if isinstance(node, (ast.List, ast.Tuple)):
            out = []
            for e in node.elts:
                out.extend(self.resolve(e, depth + 1, seen))
            return out
        if isinstance(node, ast.IfExp):
            return (self.resolve(node.body, depth + 1, seen)
                    + self.resolve(node.orelse, depth + 1, seen))
        if isinstance(node, ast.Call):
            fname = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if fname == "join":
                out = []
                for a in node.args:
                    out.extend(self.resolve(a, depth + 1, seen))
                return out
            if fname in CTOR or fname in RICH:
                skip = title_args(node, self.src)
                out = []
                for a in list(node.args) + [k.value for k in node.keywords]:
                    if id(a) in skip:
                        continue
                    out.extend(self.resolve(a, depth + 1, seen))
                return out
        return self.suspects(node)


def kind(call, src):
    f = call.func
    if isinstance(f, ast.Attribute) and f.attr in RICH:
        base = ast.get_source_segment(src, f.value) or ""
        if base.startswith("sys") or base in ("json",):
            return None          # stderr/JSON paths are out of scope by design
        return f.attr
    if isinstance(f, ast.Name) and f.id in CTOR:
        return f.id
    return None


def title_args(call, src):
    """id()s of the renderable TITLE arguments of a constructor call.

    panel()/status_panel()/table() route their TITLE through _theme, which
    escapes it once (cli_theme._view_title on the themed path, esc() on the
    plain-Console fallback). Escaping a title AGAIN at the call site is a bug —
    the backslashes escape() inserts get escaped themselves and the operator
    sees literal \\[ \\]. So the checker must not demand esc() on a title.
    """
    out = set()
    if isinstance(call.func, ast.Name):
        name = call.func.id
        if name == "panel" and call.args:
            out.add(id(call.args[0]))
        elif name == "status_panel" and len(call.args) > 2:
            out.add(id(call.args[2]))
        elif name == "table" and call.args:
            out.add(id(call.args[0]))
    for kw in call.keywords:
        if kw.arg == "title":
            out.add(id(kw.value))
    return out


def check(rel):
    p = ROOT / rel
    src = p.read_text(encoding="utf-8")
    bad_sites = 0
    try:
        tree = ast.parse(src, filename=str(p))
    except SyntaxError as exc:
        print(f"SYNTAX-ERROR {rel}: {exc}")
        return 1
    scopes = {}
    rows = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes[id(fn)] = Scope(fn, src)

    def scope_for(node):
        # nearest enclosing function we indexed (parent map built per call site)
        return getattr(node, "_scope", None)

    parent = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parent[id(c)] = n

    def enclosing_scope(call):
        cur = parent.get(id(call))
        while cur is not None:
            if id(cur) in scopes:
                return scopes[id(cur)]
            cur = parent.get(id(cur))
        return Scope(tree, src)      # module level

    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        k = kind(n, src)
        if k is None:
            continue
        sc = enclosing_scope(n)
        skip = title_args(n, src)
        sug = set()
        for a in list(n.args) + [kw.value for kw in n.keywords]:
            if id(a) in skip:
                continue     # title is escaped centrally by _theme/_view_title
            # resolve() only — calling suspects() here too would walk straight
            # into a nested panel(...)/status_panel(...) TITLE and demand an
            # esc() that would double-escape it.
            sug.update(sc.resolve(a))
        for ln, s in sug:
            rows.add((ln, k, s))

    if rows:
        print(f"REMAINING {rel}:")
        for ln, k, s in sorted(rows):
            why = _allowed(rel, s)
            if why:
                print(f"    L{ln} [{k}] {s}  — allowed: {why}")
                continue
            print(f"    L{ln} [{k}] {s}")
            bad_sites += 1
    if bad_sites:
        return 1
    print(f"CLEAN {rel} ({len(rows)} reviewed-safe sites)" if rows
          else f"CLEAN {rel}")
    return 0


def main(paths):
    rels = []
    for a in paths:
        q = ROOT / a
        if q.is_dir():
            rels.extend(sorted(str(r.relative_to(ROOT))
                               for r in q.glob("*.py")
                               if r.name != "_theme.py"))
        else:
            rels.append(a)
    bad = 0
    for rel in rels:
        bad |= check(rel)
    print("RESULT:", "NEEDS WORK" if bad else "ALL CLEAN")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
