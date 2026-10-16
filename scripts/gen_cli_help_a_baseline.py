"""Regenerate the committed (A)-count fixture for the help-surface gate
(t_cfdcefa9). Run from the repo root:

    python3 scripts/gen_cli_help_a_baseline.py            # print + write
    python3 scripts/gen_cli_help_a_baseline.py --check    # exit 1 on drift

Writes scripts/tests/cli_help_a_counts.json — the per-surface canonical (A)
detail table. test_cli_help_surface.py pins two invariants on it: the audit's
live table equals it, and the table is equal under every sibling interpreter
found on PATH. The file IS the contract: review its diff, never regenerate
blindly (same rule as cli_help_expected.json).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

import hscc_cli_help_audit as audit_mod  # noqa: E402

OUT = REPO / "scripts" / "tests" / "cli_help_a_counts.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="fail if the committed file differs from the audit")
    args = ap.parse_args()

    rep = audit_mod.audit(str(REPO))
    payload = {
        "note": "Committed (A) count/lines table for "
                "scripts/tests/test_cli_help_surface.py. (A) identity is the "
                "widest render, whitespace-canonicalised — this table must be "
                "byte-identical on every CPython the suite runs on. Review "
                "diffs; never regenerate blindly.",
        "counts": {k: len(v) for k, v in rep["a_counts"].items()},
        "lines": rep["a_counts"],
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.check:
        if not OUT.exists() or OUT.read_text() != text:
            print("DRIFT: scripts/tests/cli_help_a_counts.json differs from "
                  "the live audit table", file=sys.stderr)
            return 1
        print("in sync")
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT}: {len(payload['counts'])} surfaces, "
          f"{sum(payload['counts'].values())} A-lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
