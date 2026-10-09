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

Reads the report on stdin, writes the redacted report on stdout. Fails closed.

What "fails closed" has to mean here (corrected in review, t_9a4b7687 round 1):
exiting non-zero is NOT enough, because a failure path also writes text to the
log. Two channels published guard-derived text while the job "failed closed",
both reproduced against the first version of this file:

  * the load-failure message interpolated ``str(exc)``, and an exception raised
    while importing a guard that carries an address can quote it;
  * importing the guard emits *import-time* noise — warnings, or a stray
    module-level print — and Python's default warning renderer prints the
    offending SOURCE LINE. A functional guard with one missing ``r`` prefix
    (a real tailnet-shaped host inside a non-raw literal containing an invalid
    escape) therefore echoed its own source line to the public log on every run
    while the ADVISORY job stayed GREEN: CI checkouts have no __pycache__, so the
    module recompiles each time, and Python >= 3.12 shows invalid-escape
    SyntaxWarnings by default.

So: every diagnostic this file can emit is a FIXED string; the guard's import
runs with warnings suppressed and its stdout/stderr captured into a buffer that
is discarded, never echoed; a loaded pattern that is not a compiled regex is
rejected instead of raising AttributeError later; and an unexpected error is
reported as ``<exception type>`` only — never ``str(exc)``, never a traceback.

That input class is the reason: a committed detector that is itself broken AND
carries a real address is exactly the state the local hooks cannot catch (both
the pre-commit hook and the pytest gate need the module to import cleanly), so
this job is the last line of defence and must not be the thing that publishes
the value it exists to hide.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import sys
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "address_guard.py"

# Fixed diagnostics only — nothing below is derived from the guard's content.
MSG_CANNOT_LOAD = "redact_guard_report: CANNOT LOAD PATTERN"
MSG_UNEXPECTED = "redact_guard_report: UNEXPECTED ERROR"
MSG_REFUSING = "redact_guard_report: refusing to echo unredacted output."


def load_pattern():
    """The FORBIDDEN pattern from the shipped guard — never a copy of it.

    The import is silenced on purpose (warnings + stdout + stderr, all of it):
    a warning raised while the guard compiles prints the guard's source line,
    and that line may itself be the address. Whatever the guard emits during
    import is discarded, not forwarded to our caller's log.
    """
    spec = importlib.util.spec_from_file_location("address_guard_for_redact", GUARD)
    if spec is None or spec.loader is None:
        raise RuntimeError("guard module cannot be located")
    module = importlib.util.module_from_spec(spec)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()):
                spec.loader.exec_module(module)
    pattern = module.FORBIDDEN
    if not isinstance(pattern, re.Pattern):
        # Anything else would raise AttributeError later, and an uncaught
        # traceback is one more channel to the log.
        raise TypeError("FORBIDDEN is not a compiled regex")
    return pattern


def redact(line, pattern):
    """Blank the matched address, keeping the ``file:line`` position."""
    return pattern.sub("***", line)


def _emit(text):
    """Write a diagnostic. stdout may already carry report text, so flush both."""
    sys.stdout.flush()
    print(text, file=sys.stderr, flush=True)


def main():
    try:
        pattern = load_pattern()
    except Exception as exc:  # noqa: BLE001 - any import failure means fail closed
        # Type name only: str(exc) can quote the very value we are hiding.
        _emit(f"{MSG_CANNOT_LOAD} [{type(exc).__name__}]")
        _emit(MSG_REFUSING)
        return 3
    try:
        for line in sys.stdin:
            sys.stdout.write(redact(line, pattern))
        sys.stdout.flush()
    except Exception as exc:  # noqa: BLE001 - fail closed; never leak a traceback
        _emit(f"{MSG_UNEXPECTED} [{type(exc).__name__}]")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
