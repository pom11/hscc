#!/usr/bin/env python3
"""Line-targeted asserting applier for the flightdeck markup-escape sweep
(t_12f3c8a8).

An edit is (relpath, lineno, old_fragment, new_fragment[, ctx]) where `ctx` is
an optional list of (lineno, must_contain) anchors. The script asserts:
  * the file's line `lineno` contains `old_fragment` exactly once, and
  * either `old_fragment` appears in NO other line of the file (so a stale line
    number cannot silently patch the wrong site), OR `ctx` is given and every
    ctx anchor matches — use ctx for fragments that legitimately repeat on a
    near-duplicate code path (e.g. `"detail": res["detail"],` in both pull and
    push), where the anchor proves you are in the intended function.
then rewrites just that line, writes the file, and re-reads it to confirm the
new fragment is on disk and the old one is gone from that line.

Why not a blind sed / editor-return: this repo has already had a doc edit
silently reverted minutes after a patch tool reported success-with-diff, and
f-string fragments repeat across near-duplicate code paths. Assert + re-read.

Batches are additive and idempotent-safe: re-running a batch after it has been
applied FAILS loudly (fragment no longer present) instead of double-escaping.

Usage: apply_lines.py batch1.py [batch2.py ...]
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

    total = 0
    for rel, ops in by_file.items():
        p = ROOT / rel
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        failed = []
        for op in ops:
            _rel, lineno, old, new = op[:4]
            ctx = op[4] if len(op) > 4 else None
            if lineno < 1 or lineno > len(lines):
                failed.append(f"L{lineno} out of range (file has {len(lines)} lines)")
                continue
            cur = lines[lineno - 1]
            if cur.count(old) != 1:
                failed.append(f"L{lineno}: fragment x{cur.count(old)}: {old!r}")
                continue
            others = [i for i, ln in enumerate(lines, 1)
                      if i != lineno and old in ln]
            if others:
                # Fragment legitimately repeats (near-duplicate code path). Only
                # allowed when ctx anchors prove we are in the intended function.
                if not ctx:
                    failed.append(f"L{lineno}: fragment also on line(s) {others}: {old!r}")
                    continue
                bad = [f"L{cl}: expected {want!r}" for cl, want in ctx
                       if cl > len(lines) or want not in lines[cl - 1]]
                if bad:
                    failed.append(f"L{lineno}: repeated fragment and ctx mismatch -> "
                                  + "; ".join(bad))
                    continue
            lines[lineno - 1] = cur.replace(old, new)
        if failed:
            print(f"FAIL {rel}:")
            for f in failed:
                print("   " + f)
            sys.exit(1)
        text = "".join(lines)
        # AST gate BEFORE writing: a botched f-string edit (e.g. {esc(x!r)}, which
        # is invalid Python — a conversion flag is only legal at the top of a
        # replacement field) must never reach disk, let alone the suite.
        if rel.endswith(".py"):
            try:
                ast.parse(text, filename=str(p))
            except SyntaxError as exc:
                print(f"FAIL {rel}: result does not parse — nothing written: {exc}")
                sys.exit(1)
        p.write_text(text, encoding="utf-8")
        check = p.read_text(encoding="utf-8").splitlines(keepends=True)
        for op in ops:
            _rel, lineno, old, new = op[:4]
            if new not in check[lineno - 1] or old in check[lineno - 1]:
                print(f"FAIL {rel}: L{lineno} not as expected after write: "
                      f"{check[lineno-1]!r}")
                sys.exit(1)
        total += len(ops)
        print(f"OK {rel}: {len(ops)} line edits")
    print(f"APPLIED {total} edits across {len(by_file)} files")


if __name__ == "__main__":
    main(sys.argv[1:])
