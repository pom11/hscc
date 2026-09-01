#!/usr/bin/env python3
"""fix_model_decode_chars.py - collapse the double-escaped Swift interpolation.

The file-write tool turns Swift `\\(` interpolations into literal `\\` + `(`.
Swift then fails to compile with "invalid escape sequence in literal". This
script rewrites `\\(` -> `\\(` (two-backslash -> one-backslash before paren) so
the harness compiles again. Idempotent.
"""
import re
import sys

for path in sys.argv[1:]:
    with open(path) as f:
        src = f.read()
    fixed = src.replace("\\\\(", "\\(")          # \\(  ->  \(
    if fixed == src:
        print("OK (no change):", path)
        continue
    with open(path, "w") as f:
        f.write(fixed)
    bad = len(re.findall(r"\\\\\(", fixed))       # any remaining double-escapes?
    print(f"FIXED: {path} (wrote {len(fixed)} chars, remaining double-escapes: {bad})")
    if bad:
        sys.exit(1)
print("done")
