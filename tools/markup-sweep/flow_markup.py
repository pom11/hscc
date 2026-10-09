#!/usr/bin/env python3
"""Flow-aware scan for unescaped Rich DATA in flightdeck/commands.

For every Rich render surface (Console.print / panel / status_panel / table /
add_row / add_column), resolve the renderable body:
  * an f-string  -> report its unescaped interpolations directly;
  * a name/subscript/call -> find the assignments feeding it in the enclosing
    function (x = ..., x.append(...), lines += [...]) and report the
    unescaped interpolations in those f-strings.
"Safe" = constant, or wrapped in escape()/esc()/repr()/len()/... (see SAFE).

Output: file:line [site-kind] var-or-expr -> suspects  (deduped per line)
"""
import ast
import sys
from pathlib import Path

RICH_METHODS = {"print", "add_row", "add_column"}
CTOR_FUNCS = {"panel", "status_panel", "table"}
SAFE_FUNCS = {"escape", "esc", "repr", "len", "int", "float", "sorted",
              "min", "max", "_human_bytes", "_fmt_duration", "_short",
              "_card_label", "_format_session_row", "_iso", "_human"}
# renderable-constructor funcs whose TITLE arg is safe via _view_title
# (we still scan title f-strings for data; report with t= marker)


def is_safe(node):
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.JoinedStr):
        return all(is_safe(v.value) for v in node.values
                   if isinstance(v, ast.FormattedValue))
    if isinstance(node, ast.Call):
        fname = ""
        if isinstance(node.func, ast.Name):
            fname = node.func.id
        elif isinstance(node.func, ast.Attribute):
            fname = node.func.attr
        return fname in SAFE_FUNCS
    if isinstance(node, ast.BinOp):
        return is_safe(node.left) and is_safe(node.right)
    if isinstance(node, ast.IfExp):
        return all(is_safe(x) for x in (node.body, node.orelse))
    if isinstance(node, ast.Tuple):
        return all(is_safe(e) for e in node.elts)
    return False


def fstring_suspects(node, src):
    out = []
    if node is None:
        return out
    for sub in ast.walk(node):
        if isinstance(sub, ast.JoinedStr):
            for v in sub.values:
                if isinstance(v, ast.FormattedValue) and not is_safe(v.value):
                    out.append(ast.get_source_segment(src, v.value))
    return out


class FuncScan:
    def __init__(self, fn, src):
        self.fn = fn
        self.src = src
        # name -> list of expression nodes assigned to it (Assign targets,
        # AugAssign += on list, .append() args)
        self.assigns = {}
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        self.assigns.setdefault(t.id, []).append(n.value)
            elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
                self.assigns.setdefault(n.target.id, []).append(n.value)
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr in ("append", "extend") \
                    and isinstance(n.func.value, ast.Name):
                self.assigns.setdefault(n.func.value.id, []).extend(n.args)

    def resolve(self, node, depth=0, seen=None):
        """Return suspect strings for a renderable expression."""
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
                out.extend(fstring_suspects(e, self.src))
                out.extend(self.resolve(e, depth + 1, seen))
            return out
        if isinstance(node, ast.IfExp):
            return self.resolve(node.body, depth + 1, seen) + \
                self.resolve(node.orelse, depth + 1, seen)
        if isinstance(node, ast.Call):
            # join(...) over a tracked list, or panel(...) nested
            fname = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if fname == "join":
                out = []
                for a in node.args:
                    out.extend(self.resolve(a, depth + 1, seen))
                return out
            if fname in CTOR_FUNCS or fname in RICH_METHODS:
                out = []
                for a in list(node.args) + [k.value for k in node.keywords]:
                    out.extend(fstring_suspects(a, self.src))
                    out.extend(self.resolve(a, depth + 1, seen))
                return out
            return fstring_suspects(node, self.src)
        return fstring_suspects(node, self.src)


def rich_ness(call, src):
    f = call.func
    if isinstance(f, ast.Attribute) and f.attr in RICH_METHODS:
        base_src = ast.get_source_segment(src, f.value) or ""
        if base_src in ("sys",):
            return None
        return f.attr
    if isinstance(f, ast.Name) and f.id in CTOR_FUNCS:
        return f.id
    return None


def main(d):
    total = 0
    files = {}
    for p in sorted(Path(d).glob("*.py")):
        if p.name == "_theme.py":
            continue
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(p))
        funcs = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        # module-level too
        mod = type("M", (), {})()
        rows = set()
        for scope in funcs + [tree]:
            fs = FuncScan(scope, src)
            for n in ast.walk(scope):
                if not isinstance(n, ast.Call):
                    continue
                kind = rich_ness(n, src)
                if kind is None:
                    continue
                # is this call inside another render call? skip (covered)
                args = list(n.args) + [k.value for k in n.keywords]
                sug = []
                for a in args:
                    sug.extend(fstring_suspects(a, src))
                    sug.extend(fs.resolve(a))
                if sug:
                    rows.add((n.lineno, kind, tuple(sorted(set(sug)))))
        if rows:
            files[p.name] = sorted(rows)
            for lineno, kind, sug in sorted(rows):
                total += 1
                print(f"{p.name}:{lineno} [{kind}] " + " | ".join(sug))
    print(f"TOTAL {total} Rich render sites with unescaped DATA "
          f"across {len(files)} files")


if __name__ == "__main__":
    main(sys.argv[1])
