#!/usr/bin/env python3
"""diff_alone_vs_leg.py <out-dir> — is a leg's answer a property of the
interpreter, or of the file order?

Compares, for every test file the sweep ran alone (``<out>/xml/<file>.xml``,
``<out>/rc/<file>.rc``, ``<out>/logs/<file>.log``) against the one-process leg
run (``<out>/inleg.xml``, ``<out>/inleg.log``):

  1. every nodeid's status, from the junit xml of both sides — so a file that
     FAILS, ERRORS at collection, or skips a case the leg ran is caught; and
  2. every skip SITE + REASON string, from the ``-rs`` short summary of both
     sides — because the stamp rule is read off ``-rs``, and a skip whose reason
     changed is a different answer to "why did this not run" even when the
     counts match. A resident ``hermes_cli`` is exactly that case: the same
     ``importorskip`` line reported ``No module named 'hermes_cli'`` alone and
     ``No module named 'utils'`` in-leg.

Exit 0 = every swept file answers identically alone and in-leg.
Exit 1 = at least one difference, printed with both sides.

Companion to ``alone_sweep.sh``; write-up in
``docs/audits/daemon-alone-run-order-dependence_t_572b4856.md``.
"""
import glob
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

SKIP_LINE = re.compile(r"^SKIPPED \[\d+\] (\S+):(\d+): (.*)$")


def module_of(classname):
    """`pkg.mod.Class` -> `pkg.mod` (the module, which is the file we swept)."""
    parts = (classname or "").split(".")
    for i, p in enumerate(parts):
        if p.startswith("test_"):
            return ".".join(parts[:i + 1])
    return parts[0] if parts else "?"


def load_junit(path):
    """-> {module: {nodeid: passed|skipped|failed|error}}"""
    out = defaultdict(dict)
    for tc in ET.parse(path).iter("testcase"):
        mod = module_of(tc.get("classname"))
        cls = tc.get("classname") or ""
        cls = cls[len(mod) + 1:] if cls.startswith(mod) else ""
        name = tc.get("name") or "<collection>"
        node = f"{mod}::{cls}::{name}" if cls else f"{mod}::{name}"
        status = "passed"
        for child in tc:
            if child.tag in ("skipped", "failure", "error"):
                status = child.tag
        out[mod][node] = status
    return out


def tally(mapping):
    counts = defaultdict(int)
    for status in mapping.values():
        counts[status] += 1
    return (counts["passed"], counts["failed"], counts["skipped"],
            counts["error"])


def scan_skips(path):
    """-> {(basename, line): {reason, ...}} from a `-rs` short summary."""
    out = defaultdict(set)
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            m = SKIP_LINE.match(raw.rstrip("\n"))
            if m:
                out[(os.path.basename(m.group(1)), m.group(2))].add(m.group(3))
    return out


def main(outdir):
    leg = load_junit(os.path.join(outdir, "inleg.xml"))
    leg_skips = scan_skips(os.path.join(outdir, "inleg.log"))
    files = sorted(glob.glob(os.path.join(outdir, "xml", "*.xml")))
    if not files:
        print(f"no per-file junit xml under {outdir}/xml — run alone_sweep.sh first")
        return 2
    bad = 0
    for path in files:
        stem = os.path.basename(path)[:-4]
        mod = "tests." + stem if not stem.startswith("test_") else None
        mod = next((m for m in leg if m.endswith("." + stem) or m == stem),
                   mod or stem)
        alone = load_junit(path)
        amap = {}
        for _k, v in alone.items():          # a lone file may key its error oddly
            amap.update(v)
        imap = leg.get(mod, {})
        rc_path = os.path.join(outdir, "rc", stem + ".rc")
        rc = open(rc_path).read().strip() if os.path.exists(rc_path) else "NO-RC"
        at, it = tally(amap), tally(imap)
        changed = sorted(n for n in set(amap) | set(imap)
                         if amap.get(n) != imap.get(n))
        alone_skips = scan_skips(os.path.join(outdir, "logs", stem + ".log"))
        reason_diffs = sorted(
            key for key in set(leg_skips) | set(alone_skips)
            if key[0] == stem + ".py" and leg_skips.get(key) != alone_skips.get(key))
        if at == it and not changed and not reason_diffs and rc == "0" and imap:
            continue
        bad += 1
        print(f"{stem}: in-leg {it} | alone {at} | rc={rc}")
        for node in changed:
            print(f"    status  {imap.get(node, 'ABSENT-IN-LEG'):>8} -> "
                  f"{amap.get(node, 'ABSENT-ALONE'):>8}  {node}")
        for key in reason_diffs:
            print(f"    reason  in-leg={sorted(leg_skips.get(key, []))}")
            print(f"            alone ={sorted(alone_skips.get(key, []))}  "
                  f"at {key[0]}:{key[1]}")
    print(f"\n# files swept: {len(files)}   "
          f"files whose ALONE run differs from the leg: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
