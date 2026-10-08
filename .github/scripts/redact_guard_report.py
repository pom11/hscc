#!/usr/bin/env python3
"""Strip real operator addresses out of the address guard's report for CI logs.

Why this exists: ``pom11/hscc`` is a PUBLIC repo, and a GitHub Actions job log is
public too. ``scripts/address_guard.py`` prints each offender as
``rel:line: <matched address>`` — exactly right for a local ``git commit`` where
you need to see the value to fix it, and exactly wrong for a public log, where
the backstop would itself publish the leak it was built to stop.

So the CI job keeps the useful half (``file:line``) and drops the value. It must
NOT do that by guessing the line shape: paths here can contain colons, spaces,
quotes and newlines (measured in the t_ec2c2f95 review probe battery), and a
shape-based rule silently stops matching on exactly those paths — which is where
an evading commit would live. Instead we delete the match using the guard's OWN
pattern, imported from the shipped module. Still one regex in the repo.

Reads the report on stdin, writes the redacted report on stdout. Fails closed:
if the pattern cannot be loaded we exit non-zero WITHOUT echoing anything, so an
unredacted address can never reach the log on our account.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "address_guard.py"


def load_pattern():
    """The FORBIDDEN pattern from the shipped guard — never a copy of it."""
    spec = importlib.util.spec_from_file_location("address_guard_for_redact", GUARD)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {GUARD}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FORBIDDEN


def redact(line, pattern):
    """Blank the matched address, keeping the ``file:line`` position."""
    return pattern.sub("***", line)


def main():
    try:
        pattern = load_pattern()
    except Exception as exc:  # noqa: BLE001 - any import failure means fail closed
        print(f"redact_guard_report: CANNOT LOAD PATTERN — {exc}", file=sys.stderr)
        print("redact_guard_report: refusing to echo unredacted output.", file=sys.stderr)
        return 3
    for line in sys.stdin:
        sys.stdout.write(redact(line, pattern))
    return 0


if __name__ == "__main__":
    sys.exit(main())
