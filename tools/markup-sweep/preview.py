#!/usr/bin/env python3
"""Preview a batch WITHOUT writing: report which edits match / don't.

Same assertion rules as apply_lines.py (fragment appears once on the target
line; repeated fragments need ctx anchors that match), but read-only. Use this
to find stale line numbers before committing to an apply pass.

Usage: preview.py batch.py [...]
"""
import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "hscc"


def load(batch):
    spec = importlib.util.spec_from_file_location("b_" + Path(batch).stem, batch)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.EDITS


def main(batches):
    edits = []
    for b in batches:
        edits.extend(load(b))
    by_file = {}
    for e in edits:
        by_file.setdefault(e[0], []).append(e)
    bad = 0
    for rel, ops in by_file.items():
        lines = (ROOT / rel).read_text(encoding="utf-8").splitlines(keepends=True)
        print(f"--- {rel}")
        for op in ops:
            rel_, lineno, old, new = op[:4]
            ctx = op[4] if len(op) > 4 else None
            if lineno < 1 or lineno > len(lines):
                print(f"  BAD  L{lineno}: out of range ({len(lines)} lines)")
                bad += 1
                continue
            cur = lines[lineno - 1]
            if cur.count(old) != 1:
                print(f"  BAD  L{lineno}: x{cur.count(old)} {old!r}")
                print(f"       have: {cur.rstrip()!r}")
                bad += 1
                continue
            others = [i for i, ln in enumerate(lines, 1) if i != lineno and old in ln]
            if others and not ctx:
                print(f"  WARN L{lineno}: repeated on {others}, no ctx anchor")
                bad += 1
                continue
            if ctx and others:
                mism = [f"L{cl} lacks {want!r}" for cl, want in ctx
                        if cl > len(lines) or want not in lines[cl - 1]]
                if mism:
                    print(f"  BAD  L{lineno}: ctx mismatch: {'; '.join(mism)}")
                    bad += 1
                    continue
            merged = cur.replace(old, new)
            if rel.endswith(".py"):
                probe = list(lines)
                probe[lineno - 1] = merged
                try:
                    ast.parse("".join(probe), filename=rel)
                except SyntaxError as exc:
                    print(f"  BAD  L{lineno}: result does not parse: {exc}")
                    print(f"       -> {merged.rstrip()!r}")
                    bad += 1
                    continue
            print(f"  ok   L{lineno}")
    print("PREVIEW:", "ALL GOOD" if not bad else f"{bad} PROBLEM(S)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
