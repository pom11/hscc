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
pattern. Still one regex in the repo — this file never carries a copy of it.

Reads the report on stdin, writes the redacted report on stdout. Fails closed.

TRUST BOUNDARY (review round 3, t_9a4b7687) — read this before adding a layer:
**this process does not execute the guard's module code.** It asks a CHILD process
to import the guard and hand the pattern back over a one-line protocol on a pipe.
That is the whole design decision, and rounds 1-2 are the reason it is a process
boundary rather than another filter:

  * round 1 — the load-failure path interpolated ``str(exc)``, and the import let
    Python's warning renderer print the guard's SOURCE LINE. Fixed by making every
    diagnostic this file can emit a FIXED string.
  * round 2 — ``except Exception`` did not catch ``BaseException``/``SystemExit``
    (CPython prints the traceback, and the frame text IS the guard's source line),
    and a write straight to **fd 1/2** ignored ``redirect_stdout/stderr`` because
    those rebind the *objects*, not the descriptor. Fixed with ``except
    BaseException`` plus a ``dup``/``dup2``-over-``/dev/null`` window around the
    import.
  * round 3 — that window was still a WINDOW, and guard code outlives it from
    inside the process: ``atexit`` handlers run after ``finally`` has handed fd 1/2
    back; a non-daemon thread started at import is joined at shutdown, after it;
    and the window's OWN ``os.dup(1)``/``os.dup(2)`` saved descriptors sit live in
    the fd table for the whole import, so a module that scans fd 3..63 finds
    writable copies of the job log — or ``dup2``s one back over fd 1. Reproduced
    12/12 on py3.11.16 AND py3.13.7 against the gated tip, ADVISORY JOB GREEN WHILE
    THE ADDRESS REACHED THE PUBLIC LOG.

Three in-process layers, each individually reasonable, each eventually bypassed.
The pattern is that in-process containment of untrusted import-time code has no
closed boundary, so the containment is now a process boundary: the child's stdin
and stderr are ``/dev/null``, its stdout is a pipe ONLY this process reads,
``close_fds`` drops everything else, and it runs in its own process group so it can
be taken down whole. Whatever the guard's module body does — ``print``,
``os.write`` to any descriptor, atexit dumps, threads joined at shutdown, fd scans,
``dup2`` games, forks — it reaches the pipe or ``/dev/null``, never the job log.

The pipe is not a printing channel either. The parent accepts exactly one
well-formed protocol line, validates the payload strictly (base64 with
``validate=True``, flags bounded, ``ast.literal_eval`` only, exception NAMES matched
against a fixed charset) and **never echoes a byte read from the child**. Pickle is
deliberately not used: unpickling guard-derived bytes would execute guard code in
this process, i.e. reintroduce the thing being fixed.

OUT OF SCOPE, named so it is not re-litigated as a leak: a guard whose ``FORBIDDEN``
has been *weakened* (narrowed, or replaced by one matching nothing) still redacts
"correctly" and this job still passes. That is a guard-INTEGRITY attack, not a
publish channel — the value never leaves the process and nothing gets printed.
Detection correctness is pinned by the parent's probe battery and
``scripts/tests/test_address_guard.py``; this file's guarantee is narrower and is
the one that matters for a public log: **a real address never reaches the log, on
any path, including the failure paths.**
"""

from __future__ import annotations

import ast
import base64
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "address_guard.py"

# Fixed diagnostics only — nothing below is derived from the guard's content.
MSG_CANNOT_LOAD = "redact_guard_report: CANNOT LOAD PATTERN"
MSG_UNEXPECTED = "redact_guard_report: UNEXPECTED ERROR"
MSG_REFUSING = "redact_guard_report: refusing to echo unredacted output."
# What is written where an address was. A module constant so the shape is stated
# once (tests assert on it) and nothing assembles a value-shaped string by accident.
MASK = "*" * 3

# The child does nothing but import one stdlib module and print a line, so 30 s is
# generous. Every wait on it is bounded, because a check that can hang is a check
# that fails SLOW (job timeout) and gets ignored.
CHILD_TIMEOUT_S = 30
REAP_TIMEOUT_S = 5
# Cap what we ever read from the child: anything past a verdict line is junk, we
# stop reading, close the pipe and kill the group. Bounds memory as well as time.
MAX_PIPE_BYTES = 64 * 1024
MAX_PATTERN_CHARS = 4096
# Exception NAMES (never messages) may cross the process boundary. A real address
# is not expressible in this charset: no digits, no dots.
TYPE_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
# re's flags fit in this range; anything bigger is not a flags value.
MAX_FLAGS = 0x1FF

# Runs in the child, which has no descriptor that can reach the job log, so this is
# all it does: emit the pattern, or emit an exception TYPE and nothing else.
# ``-W ignore`` matters on the child's side too — a CI checkout has no __pycache__,
# so the guard is recompiled every run and an invalid-escape SyntaxWarning would
# otherwise render, and its renderer prints the source line.
CHILD_SOURCE = r"""
import base64, importlib.util, re, sys

def emit(kind, payload=""):
    sys.stdout.write("%s\t%s\n" % (kind, payload))
    sys.stdout.flush()

try:
    spec = importlib.util.spec_from_file_location("address_guard_extract", sys.argv[1])
    if spec is None or spec.loader is None:
        raise RuntimeError("guard module cannot be located")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pattern = module.FORBIDDEN
    if not isinstance(pattern, re.Pattern):
        raise TypeError("FORBIDDEN is not a compiled regex")
    blob = pattern.pattern.encode("utf-8")
    if len(blob) > 4096:
        raise ValueError("FORBIDDEN pattern text is implausibly large")
    emit("OK", base64.b64encode(blob).decode("ascii") + "\t" + str(int(pattern.flags)))
except BaseException as exc:
    # BaseException on purpose: SystemExit / KeyboardInterrupt from guard module
    # level must still produce a verdict, not a silent stream. Type NAME only —
    # the message is the channel round 1 died on.
    name = type(exc).__name__
    ok = name.isascii() and all(c.isalnum() or c == "_" for c in name) \
        and not name[:1].isdigit()
    emit("ERR", name if ok and len(name) <= 64 else "Invalid")
"""


class PatternUnavailable(Exception):
    """The pattern could not be extracted; may carry a VALIDATED child type name.

    Deliberately an ``Exception`` (not a BaseException subclass) so main()'s
    blanket ``except BaseException`` still catches it, and deliberately holding
    only a type NAME it has already matched against ``TYPE_NAME`` — never text from
    the child.
    """

    def __init__(self, type_name=None):
        super().__init__("pattern extraction failed")
        self.type_name = type_name if type_name and TYPE_NAME.match(type_name) else None


def _kill_group(proc, pgid=None):
    """Take the child and anything it spawned down; never wait on them forever.

    ``pgid`` is the group id captured while the child was ALIVE (see
    ``_child_pipe``). Passing it matters: the child usually exits before this runs,
    and ``os.getpgid`` of a reaped pid then raises ESRCH — a group-kill looked up
    that way quietly degrades to a no-op and a spawned helper survives (measured:
    my first cut killed the child, left the grandchild sleeping on the runner, and
    the pinning test caught it).
    """
    if pgid is None:
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except (AttributeError, OSError):
            # AttributeError: no killpg on this platform. OSError (ESRCH/EPERM):
            # the group is gone or not ours — fall back to the direct kill.
            pass
    try:
        proc.kill()
    except BaseException:  # noqa: BLE001 - best effort; nothing to print
        pass


def _child_pipe():
    """Import the guard in a child and return the raw bytes it wrote on stdout.

    Bounded on every path. The read is a ``select`` loop with one overall deadline
    and a byte cap — never a blocking ``read()`` — so neither a guard that sleeps
    nor a grandchild holding the pipe's write end can wedge us. Then the pipe is
    closed (further child writes die on EPIPE instead of queueing for a reader) and
    the child's WHOLE process group is SIGKILLed and reaped with a second, shorter
    bound, so a spawned helper cannot hold the step open to the job timeout.

    Deliberately no helper thread and no buffered reads: my first cut read the pipe
    in a thread and then closed it from the main thread, which deadlocks on the
    ``BufferedReader`` lock the blocked reader holds — and the deadlock happens
    BEFORE the kill, so the step hangs forever (measured, py3.11.16).
    """
    proc = subprocess.Popen(
        # -E: no PYTHONPATH / PYTHONSTARTUP hijacking the child.
        # -s: no user site-packages, so a module installed in the runner's user
        #     dir cannot shadow what the guard imports.
        # -W ignore: no warning may render the guard's source line inside the child.
        # bufsize=0: an unbuffered handle, so os.read below is the only reader.
        [sys.executable, "-E", "-s", "-W", "ignore", "-c", CHILD_SOURCE, str(GUARD)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        bufsize=0,
        start_new_session=True,  # own process group -> killable whole
    )
    # Capture the group id NOW, while the child is alive. By the time the finally
    # block runs the child has usually exited, and os.getpgid of a reaped pid is
    # ESRCH — a group kill looked up then is a silent no-op that leaves any helper
    # the guard spawned running on the runner.
    try:
        child_pgid = os.getpgid(proc.pid)
    except OSError:
        child_pgid = None
    buf = bytearray()
    timed_out = False
    pipe = proc.stdout
    if pipe is None:  # unreachable with stdout=PIPE; keeps the failure explicit
        _kill_group(proc, child_pgid)
        raise PatternUnavailable("RuntimeError")
    deadline = time.monotonic() + CHILD_TIMEOUT_S
    try:
        fd = pipe.fileno()
        with selectors.DefaultSelector() as sel:
            sel.register(fd, selectors.EVENT_READ)
            while len(buf) < MAX_PIPE_BYTES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                if not sel.select(remaining):
                    timed_out = True
                    break
                try:
                    chunk = os.read(fd, 4096)
                except OSError:  # EPIPE-adjacent, or the child killed our read end
                    break
                if not chunk:  # EOF — every write end is closed
                    break
                buf.extend(chunk)
    except OSError:
        # Selector setup on a pipe that is already gone: no verdict, fail closed.
        timed_out = False
        buf = bytearray()
    finally:
        # Order matters: take the process group DOWN before closing the read end,
        # so a grandchild is not left writing into a pipe nobody will drain.
        _kill_group(proc, child_pgid)
        try:
            pipe.close()
        except BaseException:  # noqa: BLE001
            pass
        try:
            proc.wait(REAP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            # SIGKILL is already in flight for the group. A process stuck in
            # uninterruptible I/O cannot be waited out; leaving it is bounded (the
            # runner reaps the job), hanging here would not be.
            pass
    if timed_out:
        raise PatternUnavailable("TimeoutError")
    return bytes(buf)


def _parse_verdict(raw):
    """Turn the child's pipe bytes into a compiled pattern, or fail closed.

    The protocol contract, stated explicitly because review round 3 asked for the
    ambiguity to be pinned rather than left to the suite:

      * a junk line terminated by ``\\n`` is not a verdict, is dropped without
        being echoed, and a single well-formed ``OK`` is still honoured — this is
        what keeps an otherwise-healthy guard's stray chatter advisory-green;
      * junk with NO trailing newline MERGES into the verdict line, so that line is
        no longer well-formed, there is no single ``OK`` verdict, and we fail closed
        (rc=3);
      * any ``ERR`` line poisons the verdict, because a guard that could not import
        is a guard whose report we cannot redact.

    Both are safe for the same reason: a child byte is never printed. Nothing read
    from the child is echoed on any path — it is parsed as a literal, compiled, or
    dropped.
    """
    lines = raw.decode("utf-8", errors="replace").splitlines()
    err_lines = [ln for ln in lines if ln.startswith("ERR\t")]
    ok_lines = [ln for ln in lines if ln.startswith("OK\t")]
    if err_lines:
        names = sorted({ln.partition("\t")[2] for ln in err_lines
                        if TYPE_NAME.match(ln.partition("\t")[2])})
        # Only a NAME is trusted. Two different names means the stream is not a
        # verdict at all, so it goes without one.
        raise PatternUnavailable(names[0] if len(names) == 1 else None)
    if len(ok_lines) != 1:
        raise PatternUnavailable()
    blob, sep, flags_txt = ok_lines[0][len("OK\t"):].partition("\t")
    if not sep:
        raise PatternUnavailable()
    try:
        pattern_text = base64.b64decode(blob, validate=True).decode("utf-8")
    except Exception:  # noqa: BLE001 - payload is untrusted; never quote it
        raise PatternUnavailable() from None
    if not pattern_text or len(pattern_text) > MAX_PATTERN_CHARS:
        raise PatternUnavailable()
    try:
        flags = ast.literal_eval(flags_txt)
    except Exception:  # noqa: BLE001 - literal only, never eval of child text
        raise PatternUnavailable() from None
    if isinstance(flags, bool) or not isinstance(flags, int) or not 0 <= flags <= MAX_FLAGS:
        raise PatternUnavailable()
    try:
        pattern = re.compile(pattern_text, flags)
    except Exception:  # noqa: BLE001 - a pattern that will not compile is unusable
        raise PatternUnavailable() from None
    if not isinstance(pattern, re.Pattern):
        raise PatternUnavailable("TypeError")
    return pattern


def load_pattern():
    """The FORBIDDEN pattern from the shipped guard — never a copy of it.

    One child process per invocation, and no guard code in this one. Fails closed
    (``PatternUnavailable`` -> rc=3) on ANY deviation: the child timed out, exited
    without exactly one well-formed verdict, sent a payload that is not a base64
    ``str``, reported flags that are not a bounded int, or a pattern that will not
    compile.
    """
    try:
        raw = _child_pipe()
    except PatternUnavailable:
        raise
    except BaseException as exc:  # noqa: BLE001 - OSError starting the child, etc.
        # str(exc) here would be OUR OWN plumbing (it carries only our argv, never
        # the guard's content), but the log does not need it: type name only.
        raise PatternUnavailable(type(exc).__name__) from None
    return _parse_verdict(raw)


def redact(line, pattern):
    """Blank the matched address, keeping the ``file:line`` position."""
    return pattern.sub(MASK, line)


def _emit(text):
    """Write a diagnostic. stdout may already carry report text, so flush both."""
    sys.stdout.flush()
    print(text, file=sys.stderr, flush=True)


# The replacement written where an address was. A module constant so the shape is
# stated once (tests assert on it) and so nothing in this file assembles a
# value-shaped string by accident.
MASK = "*" * 3


def main():
    try:
        pattern = load_pattern()
    except BaseException as exc:  # noqa: BLE001 - fail closed on ANY escape
        # Fixed text + a VALIDATED type name; never str(exc), never a traceback.
        # The guard's code ran in the child, so there is no guard-derived traceback
        # left in this process to hide and no child text that could be echoed.
        name = getattr(exc, "type_name", None)
        _emit(f"{MSG_CANNOT_LOAD} [{name}]" if name else MSG_CANNOT_LOAD)
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
