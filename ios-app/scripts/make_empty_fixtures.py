#!/usr/bin/env python3
"""make_empty_fixtures.py — generate the EMPTY fixture set for the nav-graph
walker (t_810ab152). Same file NAMES as the main set (the harness maps paths
to names identically), every top-level ARRAY zeroed: decoding stays legal
(keys/types preserved), rows vanish, so each screen must show its honest
empty row. Deterministic; prints what it zeroed.

Usage: make_empty_fixtures.py <src-dir> <dst-dir> <name> [<name> ...]
"""
import json
import sys
import pathlib


def main():
    src, dst = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    names = sys.argv[3:]
    if not names:
        sys.exit("usage: make_empty_fixtures.py <src> <dst> <name>...")
    dst.mkdir(parents=True, exist_ok=True)
    zeroed = []
    for n in names:
        f = src / f"{n}.json"
        if not f.exists():
            continue
        d = json.loads(f.read_text())
        if isinstance(d, dict):
            out = {k: ([] if isinstance(v, list) else v) for k, v in d.items()}
            # Count-style siblings would then LIE (count: 12, rows: []).
            # Honest empty means the counts go zero too: any int key that
            # reads like a count of one of the zeroed arrays.
            for k in list(out):
                if k.endswith("count") and isinstance(out[k], int):
                    out[k] = 0
            if any(isinstance(v, list) for v in d.values()):
                zeroed.append(n)
        else:
            out = []
            zeroed.append(n)
        (dst / f"{n}.json").write_text(json.dumps(out, indent=1))
    print(f"  empty set: {len(names)} files, arrays zeroed in {len(zeroed)}: "
          + ", ".join(zeroed))


if __name__ == "__main__":
    main()
