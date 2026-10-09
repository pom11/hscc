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

So: every diagnostic this file can emit is a FIXED string; a loaded pattern that
is not a compiled regex is rejected instead of raising AttributeError later; and
an unexpected error is reported as ``<exception type>`` only — never ``str(exc)``,
never a traceback, for ANY exception including ``BaseException``/``SystemExit``
(review round 2: ``except Exception`` let ``sys.exit("... <addr>")`` at guard
module level unwind past main(), and CPython prints the frame text, which IS the
guard's source line).

Silencing the import also takes TWO layers, because they close different holes
(review round 2): ``warnings.catch_warnings()`` + ``redirect_stdout/stderr``
rebind the *objects*, so a write straight to **fd 1/2** — ``os.write(2, ...)`` in
the guard, or C-level output — still lands on the pipes this process inherited
from the CI step, which the step echoes verbatim. That published the address while
the ADVISORY job reported GREEN. The only layer that catches it is the descriptor
table: ``load_pattern()`` points fd 1/2 at ``/dev/null`` for the duration of the
import and hands the real descriptors back afterwards.

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
import os
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

    The import is silenced on purpose, at TWO layers that close different holes
    (review round 2). ``catch_warnings`` + ``redirect_stdout/stderr`` catch the
    Python-level channels — a warning whose renderer prints the guard's source
    line, or a stray module-level ``print``. But those rebind the *objects*;
    ``os.write(2, ...)`` inside the guard — or C-level output — goes to the fd,
    and the fd still points at the step's inherited pipes. Measured: the
    ADVISORY job stayed GREEN while the address reached the public log. So fd 1/2
    are pointed at ``/dev/null`` for the duration of the import and the real
    descriptors handed back in ``finally``, which is the only layer that holds.
    """
    spec = importlib.util.spec_from_file_location("address_guard_for_redact", GUARD)
    if spec is None or spec.loader is None:
        raise RuntimeError("guard module cannot be located")
    module = importlib.util.module_from_spec(spec)
    # Anything already buffered belongs to the real streams, not to /dev/null.
    sys.stdout.flush()
    sys.stderr.flush()
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    saved_out = os.dup(1)
    saved_err = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with contextlib.redirect_stdout(io.StringIO()):
                with contextlib.redirect_stderr(io.StringIO()):
                    spec.loader.exec_module(module)
    finally:
        # Descriptors back first, unconditionally — then the high-level objects.
        # `sys.stdout` is already the original wrapper here (the redirect_*
        # context managers exited inside the try), but a guard that reassigned
        # `sys.stdout` itself during import would have left something else
        # behind. Restoring the objects we saved is what lets main() write the
        # redacted report and its own diagnostics.
        # NOTE: deliberately NOT a fresh io.open(1, ...) — that would orphan the
        # interpreter's own std wrapper, whose deallocation can close fd 1.
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(devnull)
        os.close(saved_out)
        os.close(saved_err)
        sys.stdout, sys.stderr = saved_stdout, saved_stderr
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except BaseException:  # noqa: BLE001 - a guard that CLOSED our streams
            # must not abort the restore above; the diagnostics that follow are
            # ours (fixed strings), and worst case they are lost, never leaked.
            pass
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
    except BaseException as exc:  # noqa: BLE001 - NOT Exception; see below
        # `except Exception` was not enough (review round 2): a guard that calls
        # sys.exit("... <addr>") at module level raises SystemExit, and any
        # BaseException unwinds past this function — where CPython prints the
        # traceback, and the frame text IS the guard's source line. Type name
        # only: str(exc) can quote the very value we are hiding.
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
