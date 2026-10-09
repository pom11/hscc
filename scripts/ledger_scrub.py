#!/usr/bin/env python3
"""Redact real operator addresses from ledger text BEFORE it is written or committed.

WHY THIS EXISTS
---------------
pom11/hscc is a PUBLIC repository. The commit-time guard (``.githooks/pre-commit``
plus ``scripts/address_guard.py``, t_ec2c2f95) blocks a real address with the
offending ``file:line`` — correct, but it fires at the *end* of a ledger tick: the
tick has already written the capture into ``docs/audits/GOAL_LEDGER_*.md``, the
commit fails, and the operator's own automation is stuck holding the leak. Redact
at the source instead, so the commit-time guard never has to reject a ledger
commit. This is follow-up 3 ("ledger hygiene") of
``docs/audits/commit-time-address-guard-t_ec2c2f95.md`` section 7, and section 6
("post-merge behaviour") is the failure mode it removes.

INTERFACE (fixed — the ``hscc-orch-goal-heartbeat`` cron prompt calls exactly this)
------------------------------------------------------------------------------------
    python3 scripts/ledger_scrub.py --stdin      # stdin -> scrubbed text on stdout
    python3 scripts/ledger_scrub.py FILE...      # scrub each file in place (atomic)

stdout is the pure scrubbed text; the count line goes to stderr so the tick can
record what it redacted without contaminating the text it writes:

    ledger_scrub: 2 address(es) scrubbed

NO SECOND REGEX
---------------
Detection lives in ``scripts/address_guard.py`` and nowhere else — the same rule
``.githooks/pre-commit`` follows and the one ``test_no_real_addresses_committed``
proves structurally. This module imports ``FORBIDDEN``, ``find_in_text``,
``LAN_PLACEHOLDER`` and ``TAILNET_PLACEHOLDER`` from it and nothing else, so the
scrubber and both commit gates can never disagree about what is a leak. The only
regex-adjacent call here is ``FORBIDDEN.subn`` on the detector's own compiled
pattern. The discriminator between the two placeholder kinds is derived from the
detector's own constant too, so no address — not even a fragment of one — appears
in this file (the repo's guard would, correctly, block the commit).

PLACEHOLDER MAPPING (fixed by the parent card)
----------------------------------------------
    a live-LAN match      -> LAN_PLACEHOLDER
    a CGNAT match         -> TAILNET_PLACEHOLDER
The sanctioned fixture block and the placeholders themselves are outside
``FORBIDDEN``, so they pass through untouched and a second pass is a byte-for-byte
no-op (idempotency is a hard requirement: the tick may re-read text it already
scrubbed).

Exit codes, mirroring the guard: 0 ok, 1 a scrub could not be completed (unreadable
file, or a leftover offender — this tool fails closed and never emits unscrubbed
text), 2 usage error.

Stdlib only, no network, no config files: the tick runs it on every capture.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

# scripts/ is not an importable package (repo dirs here deploy standalone); put it
# on sys.path and import the detector bare, same as the pytest gates do.
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from address_guard import (  # noqa: E402  the ONE detector, imported not re-derived
    FORBIDDEN,
    LAN_PLACEHOLDER,
    TAILNET_PLACEHOLDER,
    find_in_text,
)

# Which placeholder a match becomes. Every CGNAT match FORBIDDEN can make starts
# with the tailnet placeholder's own first octet; everything else it flags is the
# live LAN. Deriving the prefix from the detector's constant (instead of writing
# an address down) keeps this module free of address literals AND means a future
# edit to FORBIDDEN cannot silently drift away from what gets scrubbed: if the
# detector learns a new shape, the sub covers it on the same day it starts
# blocking it.
_CGNAT_PREFIX = TAILNET_PLACEHOLDER.split(".", 1)[0] + "."


def _placeholder_for(match):
    """Map one match object to its documented placeholder (LAN vs tailnet kind)."""
    addr = match.group(0)
    if addr.startswith(_CGNAT_PREFIX):
        return TAILNET_PLACEHOLDER
    return LAN_PLACEHOLDER


def scrub_text(text):
    """Return ``(scrubbed_text, n_addresses_replaced)``.

    The count comes from ``subn`` on the detector's compiled pattern, so it counts
    every address — including a second one on the same line, which ``find_in_text``
    (one hit per line, built for ``file:line`` reports) would not.
    """
    if not text:
        return text, 0
    return FORBIDDEN.subn(_placeholder_for, text)


def _emit(text):
    """Write scrubbed text to stdout as raw BYTES (no newline translation).

    A closed downstream pipe (``ledger_scrub --stdin | head -1``) is NOT an error
    for a redaction filter: the text was clean the moment we had it. Both the write
    and the flush can see EPIPE, and if either escapes, Python's own shutdown flush
    re-raises and the interpreter exits 120 — which a ``set -o pipefail`` tick would
    read as a failed scrub. Redirecting fd1 to /devnull is the documented way to
    swallow it (https://docs.python.org/3/library/signal.html#note-on-sigpipe).
    """
    try:
        sys.stdout.buffer.write(text.encode("utf-8", errors="surrogateescape"))
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except ValueError:  # stdout already closed
        pass


def _read(path):
    """Read a file as text, byte-preserving.

    ``surrogateescape`` means a lone undecodable byte round-trips unchanged rather
    than being silently dropped by an ``errors="ignore"`` decode. A file with a NUL
    byte is binary and is refused (``None``) — same intent as the detector's
    ``SKIP_SUFFIXES``: scrubbing a binary is not our business.
    """
    data = path.read_bytes()
    if b"\0" in data:
        return None
    return data.decode("utf-8", errors="surrogateescape")


def _write_atomic(path, text):
    """Replace ``path`` in place: same directory, same mode, one atomic rename.

    Same directory so ``os.replace`` cannot cross a filesystem boundary; mode is
    copied so a ledger file a human reads does not come back world-writable or
    non-readable; ``fsync`` before the rename so a crash mid-tick leaves the old
    or the new file, never a truncated one.
    """
    try:
        mode = path.stat().st_mode & 0o7777
    except OSError:
        mode = 0o644
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".scrubtmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8", errors="surrogateescape"))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _leftover(text):
    """Offenders still present after scrubbing — must always be empty."""
    return find_in_text(text)


def scrub_stdin():
    """--stdin mode: pure scrubbed text to stdout, count line to stderr.

    Fails closed: if anything FORBIDDEN survived the mapping (only possible if the
    detector and the placeholder convention ever disagree) nothing is emitted and
    the exit is non-zero, so a broken scrubber cannot quietly hand the tick dirty
    text to commit.
    """
    raw = sys.stdin.buffer.read()
    text = raw.decode("utf-8", errors="surrogateescape")
    scrubbed, n = scrub_text(text)
    left = _leftover(scrubbed)
    if left:
        print(f"ledger_scrub: BUG — {len(left)} address(es) survived the scrub; "
              "emitting NOTHING rather than dirty text", file=sys.stderr)
        return 1
    _emit(scrubbed)
    print(f"ledger_scrub: {n} address(es) scrubbed", file=sys.stderr)
    return 0


def scrub_files(paths):
    """FILE... mode: scrub each file in place. Returns the exit code.

    An unchanged file is NOT rewritten: the tick re-reads text it already scrubbed,
    and rewriting it would churn mtime (and make ``git status`` lie).
    """
    rc = 0
    total = 0
    files_changed = 0
    for name in paths:
        path = Path(name)
        try:
            text = _read(path)
        except OSError as exc:
            print(f"ledger_scrub: cannot read {name} — {exc}", file=sys.stderr)
            rc = 1
            continue
        if text is None:
            print(f"ledger_scrub: {name} is binary — left alone", file=sys.stderr)
            continue
        scrubbed, n = scrub_text(text)
        left = _leftover(scrubbed)
        if left:
            print(f"ledger_scrub: BUG — {len(left)} address(es) survived in {name}; "
                  f"file left UNWRITTEN (fail closed)", file=sys.stderr)
            rc = 1
            continue
        total += n
        if n == 0:
            continue
        try:
            _write_atomic(path, scrubbed)
        except OSError as exc:
            print(f"ledger_scrub: cannot write {name} — {exc}", file=sys.stderr)
            rc = 1
            continue
        files_changed += 1
        print(f"ledger_scrub: {name}: {n} address(es) scrubbed", file=sys.stderr)
    print(f"ledger_scrub: {total} address(es) scrubbed across {files_changed} file(s)",
          file=sys.stderr)
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Redact real operator addresses from ledger text, using the "
                    "commit-time guard's own detector (no second regex).")
    ap.add_argument("--stdin", action="store_true",
                    help="read stdin, write the scrubbed text to stdout")
    ap.add_argument("files", nargs="*", metavar="FILE",
                    help="files to scrub in place (atomic write)")
    args = ap.parse_args(argv)

    if args.stdin and args.files:
        print("ledger_scrub: --stdin and FILE arguments are mutually exclusive",
              file=sys.stderr)
        return 2
    if not args.stdin and not args.files:
        print("ledger_scrub: nothing to do — pass --stdin or one or more FILEs",
              file=sys.stderr)
        return 2
    if args.stdin:
        return scrub_stdin()
    return scrub_files(args.files)


if __name__ == "__main__":
    sys.exit(main())
