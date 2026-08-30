#!/usr/bin/env python3
"""Snapshot operator-state files for hermeticity proof.

The audit card (t_2985e00b) requires an EMPIRICAL before/after proof that the
test suite leaves live operator state untouched:

    ~/.hscc/*.json  and  ~/.hermes/profiles/*/config.yaml

This script writes a JSON manifest {abs_path: {"sha256": ..., "content": ...}}
for every target file, so a later diff can distinguish a DAEMON write (content
that looks like daemon-owned fields) from a TEST write (fixture values /
idle_minutes / enabled booleans) by comparing the actual content, not just the
hash.

Usage:
    python scripts/hscc_hermeticity_snapshot.py out.manifest.json
"""
import hashlib
import json
import os
import sys


def _sha256(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except (OSError, FileNotFoundError):
        return None


def _content(path):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except (OSError, FileNotFoundError):
        return None


def main(out_path):
    manifest = {}
    hscc = os.path.expanduser("~/.hscc")
    # Target set: every *.json at ~/.hscc root (like the card's examples, not the
    # daemon state dir which legitimately churns on its own 60s tick — the daemon
    # write exclusion is handled at diff time for autodown.json specifically).
    for fn in sorted(os.listdir(hscc)):
        if fn.endswith(".json"):
            p = os.path.join(hscc, fn)
            if os.path.islink(p):
                continue  # symlinks (agents.json) resolve elsewhere; skip
            manifest[p] = {"sha256": _sha256(p), "content": _content(p)}
    # Hermes profile configs.
    profiles = os.path.expanduser("~/.hermes/profiles")
    if os.path.isdir(profiles):
        for name in sorted(os.listdir(profiles)):
            cfg = os.path.join(profiles, name, "config.yaml")
            if os.path.isfile(cfg):
                manifest[cfg] = {"sha256": _sha256(cfg), "content": _content(cfg)}
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    print(f"wrote {len(manifest)} files to {out_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(
            "usage: hscc_hermeticity_snapshot.py <manifest.json>\n"
            "\n"
            "Writes a snapshot of ~/.hscc/*.json and ~/.hermes/profiles/*/config.yaml.\n"
            "Take one before a suite run and one after, then diff them:\n"
            "\n"
            "  python3 scripts/hscc_hermeticity_snapshot.py /tmp/before.json\n"
            "  HSCC_TEST_PY=... bash scripts/run_tests.sh\n"
            "  python3 scripts/hscc_hermeticity_snapshot.py /tmp/after.json\n"
            "  diff <(python3 -m json.tool /tmp/before.json) "
            "<(python3 -m json.tool /tmp/after.json)\n"
            "\n"
            "A daemon write touches only last_activity_iso / rolling failures[];\n"
            "anything else changing is a TEST leaking into operator state."
        )
    main(sys.argv[1])
