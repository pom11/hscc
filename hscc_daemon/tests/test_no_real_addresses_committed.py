"""Repo hygiene: no real operator address may be committed.

pom11/hscc is a PUBLIC repository. During the 2026-08-30 audit an audit report
quoted the live tailnet address and it reached origin before anyone noticed —
the pre-push check that was supposed to catch it grepped the WORKING TREE,
which had already been scrubbed, while the COMMITTED blob still carried the
address.

This test closes that gap for good: it scans the files git actually tracks, so
a scrubbed checkout cannot mask a dirty commit. Placeholders are the documented
convention (10.0.0.x for LAN, 100.64.0.1 for tailnet) and are allowed.
"""

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# 100.64.0.0/10 is CGNAT, which is where Tailscale hands out addresses, so a
# real tailnet address lives somewhere in it. Rather than ban the whole range —
# tests legitimately need a tailnet-shaped host — 100.64.0.0/24 is reserved as
# the SANCTIONED FIXTURE BLOCK (100.64.0.1, 100.64.0.3, ...). Anything else in
# CGNAT, or anything on the live LAN, is a real address and must not be
# committed. 10.0.0.x is the documented LAN placeholder.
FORBIDDEN = re.compile(
    r"\b(?:"
    r"100\.(?:6[5-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}"  # CGNAT above 100.64
    r"|100\.64\.(?!0\.)\d{1,3}\.\d{1,3}"                          # 100.64.x, x != 0
    r"|192\.168\.88\.\d{1,3}"                                       # the live LAN
    r")\b"
)

# Binary/vendored paths that would only produce noise.
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".pdf", ".ico", ".zip", ".gz", ".xcuserstate")


def _tracked_files():
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [f for f in out.split("\0") if f]


def test_no_real_operator_address_in_tracked_files():
    offenders = []
    for rel in _tracked_files():
        if rel.endswith(SKIP_SUFFIXES):
            continue
        p = REPO / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.split("\n"), 1):
            m = FORBIDDEN.search(line)
            if m:
                offenders.append(f"{rel}:{i}: {m.group(0)}")

    assert not offenders, (
        "Real operator addresses found in tracked files — this repo is PUBLIC.\n"
        "Use the documented placeholders (10.0.0.x for LAN, 100.64.0.1 for tailnet).\n"
        + "\n".join(offenders[:20])
    )
