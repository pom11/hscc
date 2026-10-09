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

TRUST BOUNDARY (review round 3, t_9a4b7687; extended by round 4, t_38fd345f) —
read this before adding a layer: **this process does not execute the guard's
module code.** It asks a CHILD process to execute the exact source bytes this
process read and hashed, and to hand the pattern back over a one-line protocol on
a pipe. That is the whole design decision, and rounds 1-2 are the reason it is a
process boundary rather than another filter:

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

LANDING ON THE PIPE IS ITSELF AN AUTHORSHIP OPPORTUNITY (review round 4, D0), so the
pipe is inert only in the printing sense, never in the authoring sense. A guard
module body runs inside the child and can therefore write a well-formed verdict
line ITSELF and ``os._exit(0)`` before the child's own emit: the last-emit guarantee
the protocol quietly assumes is gone, and the parent is left holding exactly one
well-formed verdict that the guard authored. So the verdict is no longer trusted on
FORM alone. This process derives the pattern a second time, independently, from the
guard's SOURCE TEXT with ``ast`` (zero execution: the module-level
``FORBIDDEN = re.compile(<literal>)``, with node-shape gates so only a single
string/int ``ast.Constant`` is accepted) and the child's verdict must AGREE with it
by compiled equality — ``.pattern`` and ``.flags`` of two ``re`` objects, not argv
tuples. Any deviation fails closed: source that cannot be parsed, no module-level
literal, a non-literal argument (``FORBIDDEN = _m.FORBIDDEN``, an f-string, a
concatenation), or a verdict that disagrees. What the redactor ultimately masks with
is the SOURCE-DERIVED pattern; the child's line is an attestation of what it
executed, and an attestation that does not match is a failure, not a fallback.

THE CHILD EXECUTES THE SOURCE THIS PROCESS READ, NOT A MODULE IT LOOKED UP (review
round 4, D1). The child used to ``importlib.util.spec_from_file_location`` the guard,
which means ``SourceFileLoader``, which means the cache directory
``scripts/__pycache__/`` — and an unchecked-hash ``.pyc`` (PEP 552; the official
``compileall --invalidation-mode unchecked-hash`` artifact) is trusted WITHOUT
reading the source at all, so it beats a NEWER ``.py``. That handed the child a
different ``FORBIDDEN`` from the one the step's own ``python3 scripts/address_guard.py``
executed (the main module never consults a cache), i.e. detection stayed honest
while redaction used the attacker's pattern — and a committed ``.pyc`` is invisible
to the pre-commit hook and the pytest gate alike, because ``scan_blob`` skips any
NUL-containing file. There is now no import on that path at all: the parent reads the
guard's bytes and passes their sha256 to the child as an argument; the child
re-reads the file itself, refuses to emit a verdict unless its bytes hash to the
pinned digest, and ``compile()``\\ s what it read. ``compile()`` of a byte string
neither consults nor writes a cache, so the cache directory is not merely
invalidated, it is not on the path. ``sys.dont_write_bytecode`` covers the write
side; on its own it never did anything about the read side.

OUT OF SCOPE, named so it is not re-litigated as a leak: a guard whose ``FORBIDDEN``
has been *weakened* (narrowed, or replaced by one matching nothing) still redacts
"correctly" and this job still passes. That is a guard-INTEGRITY attack, not a
publish channel — the value never leaves the process and nothing gets printed.
THE SAME CARVE-OUT COVERS THE ROUND-4 RESIDUAL, in these words: a compromised
contributor can keep the source literal strong and make the CLI *report* values the
literal does not match (a detection path that diverges from the one regex, e.g.
through a committed ``__pycache__`` the redactor no longer reads, or a ``scan_blob``
blind spot). That is guard-INTEGRITY territory too, not a publish channel: the
check can only flag what its one regex detects, and the report is attacker-authored
bytes which the redactor passes through (minus whatever the source literal does
match). Detection correctness is pinned by the parent's probe battery and
``scripts/tests/test_address_guard.py``; this file's guarantee is narrower and is
the one that matters for a public log: **a real address never reaches the log, on
any path, including the failure paths — and the pattern it masks with is the one in
the guard's source text, never one the child (or a cache artifact) authored.**
"""

from __future__ import annotations

import ast
import base64
import hashlib
import os
import re
import selectors
import signal
import subprocess
import sys
import time
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "address_guard.py"

# Fixed diagnostics only — nothing below is derived from the guard's content.
MSG_CANNOT_LOAD = "redact_guard_report: CANNOT LOAD PATTERN"
# A DIFFERENT failure, so a different fixed sentence: the child answered the
# protocol correctly but its verdict is not the pattern the guard's SOURCE TEXT
# says (review round 4 D0/D1). Blaming "cannot load" here would send the reader
# to the child, when the mismatch is the whole point — same lesson as the
# step's rc=2-before-redactor ordering.
MSG_DISAGREEMENT = "redact_guard_report: PATTERN DISAGREEMENT — child verdict is not the guard source's pattern"
MSG_UNEXPECTED = "redact_guard_report: UNEXPECTED ERROR"
# Third failure class: the source is fine and the child agrees with it, but an
# unchecked-hash pyc sits on the guard's cache name. The CURRENT child cannot be
# fooled by it (compile() never consults a cache) — we refuse anyway rather than
# depend on every future reader being loader-free. Fixed string, no path: the
# reader can find the cache dir from the guard path the step already names.
MSG_CACHE_SHADOWED = "redact_guard_report: CACHE SHADOW — unchecked-hash pyc on the guard's cache name"
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
# Cap what we ever hand to the child as the guard's source. The shipped guard is
# ~10 KB; anything past a megabyte is not this file, and the cap also bounds the
# write side of the pipe (a child that never reads cannot be waited out forever).
MAX_SOURCE_BYTES = 1024 * 1024
# Exception NAMES (never messages) may cross the process boundary. A real address
# is not expressible in this charset: no digits, no dots.
TYPE_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
# re's flags fit in this range; anything bigger is not a flags value.
MAX_FLAGS = 0x1FF

# Runs in the child, which has no descriptor that can reach the job log, so this is
# all it does: read the source bytes the parent is holding, prove they are the ones
# the parent pinned, execute them WITHOUT the import system, and emit the pattern —
# or emit an exception TYPE and nothing else.
# ``-W ignore`` matters on the child's side too — the exec'd source may contain an
# invalid-escape SyntaxWarning whose renderer prints the source line.
CHILD_SOURCE = r"""
import sys

# With `-c`, CPython puts the CURRENT DIRECTORY at sys.path[0] (measured: '' on
# both 3.11.16 and 3.13.7, with and without any flag). In CI that directory is the
# checkout root, so a committed `base64.py` / `re.py` / `importlib.py` there would
# satisfy the stdlib imports below and its module body would run in this child.
# Drop cwd from the path BEFORE importing anything resolvable from it. `sys` itself
# is a builtin, so the import above can never be shadowed.
#
# This is deliberately done HERE rather than with the `-P` interpreter flag: `-P`
# needs Python >= 3.11, and this repo's floor is 3.10 (hscc-cli/pyproject.toml,
# hscc-project/pyproject.toml both `requires-python = ">=3.10"`). An unsupported
# flag makes the child exit 2 before it can emit a verdict, i.e. it would take the
# whole guard down on a supported interpreter — a fail-closed that is a breakage.
# Sanitising here closes the same vector on every version and is pinnable.
sys.path = [p for p in sys.path if p not in ("", ".")]

import base64, hashlib, re


def emit(kind, payload=""):
    sys.stdout.write("%s\t%s\n" % (kind, payload))
    sys.stdout.flush()


try:
    # The guard path is argv[1]; argv[2] is the sha256 of the EXACT bytes the
    # parent parsed into its source-derived pattern. We read the file ourselves
    # and refuse to speak for bytes we cannot vouch for: a digest mismatch
    # raises BEFORE any OK can be emitted, so the parent only ever sees ERR
    # (type NAME — the message never crosses) and fails closed. That is how the
    # child proves it executed the source the parent verified; a file swapped
    # between the parent's read and ours lands here, not in a forged OK.
    with open(sys.argv[1], "rb") as fh:
        source = fh.read()
    if hashlib.sha256(source).hexdigest() != sys.argv[2]:
        raise RuntimeError("source bytes do not match the pinned digest")
    # compile() of a byte string neither CONSULTS nor writes __pycache__: the
    # read side of the import cache is where a committed unchecked-hash .pyc
    # (PEP 552) used to shadow this file (review round 4, D1). There is no
    # import machinery on this path at all — spec_from_file_location/
    # SourceFileLoader are gone — so the cache dir is not invalidated, it is
    # simply not on the path. sys.dont_write_bytecode would only have covered
    # the write side.
    namespace = {
        "__name__": "address_guard_extract",
        "__file__": sys.argv[1],
        "__package__": None,
        "__loader__": None,
        "__spec__": None,
    }
    exec(compile(source, "address_guard_source", "exec"), namespace)
    pattern = namespace.get("FORBIDDEN")
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


class PatternDisagreement(Exception):
    """The child's verdict is not the pattern the guard's SOURCE TEXT carries.

    Round 4's answer to "the protocol validates FORM, never AUTHORSHIP": a verdict
    that parses perfectly and still did not come from the source the parent read
    (a forged ``OK`` from the guard body, or a poisoned cache artifact) lands here.
    Deliberately carries NOTHING — no child bytes, no pattern text, no names —
    because there is no safe thing to say about a verdict we just refused.
    """

    def __init__(self):
        super().__init__("child verdict disagrees with the guard source")


class PatternCacheShadowed(Exception):
    """A committed unchecked-hash ``.pyc`` shadows the guard's cache name.

    The child no longer CONSULTS the cache (it ``compile()``\\ s the bytes the
    parent passes, and ``compile()`` of a byte string neither reads nor writes a
    cache), so such an artifact is inert on the current path — but PEP 552
    unchecked-hash is precisely the format whose whole purpose is to be trusted
    without reading the source, and a committed one is invisible to the guard's
    own scanners (NUL bytes ⇒ binary ⇒ skipped). We refuse to redact at all
    rather than depend on every present and future reader being loader-free.
    Deliberately carries nothing: a path here is our own plumbing, and the log
    gains nothing from it.
    """

    def __init__(self):
        super().__init__("unchecked-hash pyc shadows the guard")


def _read_guard_source():
    """The shipped guard's bytes — read by THIS process, executed by no code here.

    These exact bytes are what the parent parses with ``ast`` and what the child
    is handed (digest-pinned) to execute. Reading them here rather than letting
    the child open the path is the point: the parent verifies the same bytes the
    child runs, and the child proves it by refusing to emit without a match.
    """
    try:
        blob = GUARD.read_bytes()
    except OSError as exc:  # missing/broken checkout: the same rc=3 as before
        raise PatternUnavailable(type(exc).__name__) from None
    if len(blob) > MAX_SOURCE_BYTES:
        raise PatternUnavailable()
    return blob


def _refuse_if_cache_shadows():
    """Refuse to redact while a committed unchecked-hash pyc shadows the guard.

    The child no longer consults ``__pycache__`` at all (compile() of bytes), so
    such an artifact is already inert FOR THE CHILD — but it is exactly the thing
    the round-4 probe planted (PEP 552 ``compileall --invalidation-mode
    unchecked-hash``), it is invisible to the guard's own scanners (``scan_blob``
    skips NUL-containing files), and any future path that DOES import the guard
    by file location would honour it without reading the source. So we refuse
    outright rather than rely on every future reader being loader-free.

    Header layout (PEP 552): magic(4) | flags(4) | ... ; ``flags & 0b11``:
    0b00 timestamp-based, 0b01 unchecked-hash (loader trusts it WITHOUT reading
    the source — the dangerous class), 0b11 checked-hash. A timestamp/checked-
    hash artifact written by a normal local test run (flags 0b00/0b11) is inert
    or self-invalidating, and must NOT fail the job — dev machines legitimately
    carry pytest-generated ``scripts/__pycache__`` (measured: scripts/tests
    imports the guard bare, which writes one on every suite run).
    """
    cache_dir = GUARD.parent / "__pycache__"
    try:
        names = sorted(p.name for p in cache_dir.glob("address_guard.*.pyc"))
    except OSError:
        return  # no cache dir (the CI case): nothing to refuse
    for name in names:
        try:
            with open(cache_dir / name, "rb") as fh:
                header = fh.read(8)
        except OSError:
            continue
        if len(header) < 8:
            continue
        # flags field: little-endian uint32 at offset 4 (same on every CPython
        # that writes PEP 552 headers; struct keeps it one expression).
        flags = header[4] | (header[5] << 8) | (header[6] << 16) | (header[7] << 24)
        if (flags & 0b11) == 0b01:
            raise PatternCacheShadowed()


def _extract_source_pattern(blob):
    """The FORBIDDEN pattern as the guard's SOURCE TEXT states it — zero execution.

    Only ONE shape is accepted: a module-level ``FORBIDDEN = re.compile(<str
    literal>)`` or ``FORBIDDEN = re.compile(<str literal>, <int literal>)``
    (also as an annotated assignment, since exec binds those too). Adjacent
    literals fold into ONE ``ast.Constant`` at parse time — that is how the
    shipped guard's multi-line pattern passes; ``flags`` must be an INT literal
    or absent (``re.IGNORECASE`` as a name is a deviation: the file must state
    its pattern as data, and adding flags means a literal number).
    The LAST module-level binding wins, mirroring what exec ends up binding.
    EVERY deviation fails closed: unparsable source, no module-level literal,
    a non-literal argument (``_m.FORBIDDEN``, an f-string, a concat, a name, a
    call, an attribute — any of which lets a stub smuggle whatever pattern it
    likes), a keyword argument, three arguments. The gates are NODE-SHAPE gates
    (``ast.Constant``), not ``ast.literal_eval``: what an evaluator agrees to
    compute is version-dependent (measured: it refuses a str-concat BinOp but
    FOLDS a numeric one, so a flags expression like 2-0 would pass an
    evaluator check), and on an f-string it RAISES — a naive evaluator-based
    check turns that raise into a traceback in the log instead of a clean rc=3.

    Returns a COMPILED pattern. Agreement with the child is later checked on the
    compiled objects (``.pattern`` / ``.flags``), never on the (text, flags)
    tuple the AST saw: a bare ``re.compile(text)`` adds ``re.UNICODE`` (32) at
    compile time, so the AST tuple is ``flags=0`` while the executed object is
    ``flags=32`` on the SHIPPED guard (measured, both interpreters) — a
    tuple-compare implementation fails on the repo's own current state.
    """
    with warnings.catch_warnings():
        # An invalid-escape SyntaxWarning can be raised while parsing untrusted
        # source on 3.12+, and the warning renderer prints the source LINE. The
        # parent's diagnostics are fixed strings; silence the renderer here
        # (round 1's lesson, applied to this process for the first time).
        warnings.simplefilter("ignore")
        try:
            tree = ast.parse(blob)
        except Exception:  # noqa: BLE001 - SyntaxError/ValueError on NULs etc
            raise PatternUnavailable() from None

    last = None
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "FORBIDDEN" for t in node.targets):
            target = node.value
        elif (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
              and node.target.id == "FORBIDDEN" and node.value is not None):
            target = node.value
        if target is not None:
            last = target
    if last is None:
        raise PatternUnavailable()

    call = last
    if not (isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "compile"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "re"):
        raise PatternUnavailable()
    if len(call.args) == 1 and not call.keywords:
        text_node, flags_node = call.args[0], None
    elif len(call.args) == 2 and not call.keywords:
        text_node, flags_node = call.args
    else:
        raise PatternUnavailable()
    # NODE-SHAPE gate, not an evaluator: what the source states must be ONE
    # literal datum, not something an evaluator agrees to compute. Which nodes
    # ``ast.literal_eval`` accepts is a moving target across versions (measured
    # here: it refuses a str-concat BinOp with ValueError but FOLDS a numeric
    # one, so a flags expression like 2-0 would pass an evaluator check and is
    # refused here). An f-string is a JoinedStr, a concat a BinOp, an
    # attribute-access an Attribute, a bare name a Name: all fail on shape,
    # never by depending on what an evaluator happens to raise or return.
    if not isinstance(text_node, ast.Constant) or not isinstance(text_node.value, str):
        raise PatternUnavailable()
    text = text_node.value
    if flags_node is None:
        flags = 0
    elif (isinstance(flags_node, ast.Constant)
          and isinstance(flags_node.value, int)
          and not isinstance(flags_node.value, bool)):
        flags = flags_node.value
    else:
        raise PatternUnavailable()
    if not text or len(text) > MAX_PATTERN_CHARS:
        raise PatternUnavailable()
    if not 0 <= flags <= MAX_FLAGS:
        raise PatternUnavailable()
    try:
        pattern = re.compile(text, flags)
    except Exception:  # noqa: BLE001 - a source pattern that will not compile is unusable
        raise PatternUnavailable() from None
    if not isinstance(pattern, re.Pattern):
        raise PatternUnavailable("TypeError")
    return pattern


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


def _child_pipe(source_digest):
    """Execute the guard's verified source bytes in a child; return its stdout.

    ``source_digest`` is the sha256 of the exact bytes the parent parsed into its
    source-derived pattern. The child re-reads the file, hashes what it read and
    refuses to emit any verdict unless the digests match — the child's line is an
    attestation of executing THOSE bytes, not a lookup of its own choosing.

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
        #     dir cannot shadow what the child imports.
        # (cwd shadowing — sys.path[0] under `-c` — is closed inside CHILD_SOURCE,
        #  not by the -P flag, so it works on the 3.10 floor too; see there.)
        # -W ignore: no warning may render the guard's source line inside the child.
        # bufsize=0: an unbuffered handle, so os.read below is the only reader.
        [sys.executable, "-E", "-s", "-W", "ignore", "-c", CHILD_SOURCE,
         str(GUARD), source_digest],
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
    """The FORBIDDEN pattern the guard's SOURCE TEXT carries — never a copy of it.

    Round 4 made this TWO independent derivations that must agree, because either
    one alone was a publish channel:

      1. source authority (zero execution, in this process): the module-level
         ``FORBIDDEN = re.compile(<literal>)`` in the shipped file's bytes,
         extracted with ``ast``;
      2. execution attestation (in the child): the pattern the guard's module
         body actually binds when those same bytes run, digest-pinned to the
         bytes parsed in (1).

    The verdict returned is the SOURCE-DERIVED pattern, and only after the
    child's compiled pattern AGREES with it (``.pattern`` and ``.flags`` of two
    ``re`` objects — never the AST's (text, flags) tuple, which misses the
    implicit ``re.UNICODE``). Fails closed — rc=3, nothing echoed, nothing
    printed — on ANY deviation:

      * ``PatternUnavailable``: source unparsable / no module-level literal /
        non-literal argument (an f-string, a concat, a name, an attribute) / the
        child timed out, produced no verdict, sent a payload that is not a
        base64 ``str``, flags not a bounded int, or a pattern that will not
        compile;
      * ``PatternDisagreement``: a well-formed verdict that is not the source's
        pattern — the forged-``OK`` (D0) and poisoned-cache (D1) classes;
      * ``PatternCacheShadowed``: an unchecked-hash ``.pyc`` on the guard's cache
        name (defense-in-depth; see the check's own docstring).
    """
    blob = _read_guard_source()
    _refuse_if_cache_shadows()
    source_pattern = _extract_source_pattern(blob)
    digest = hashlib.sha256(blob).hexdigest()
    try:
        raw = _child_pipe(digest)
    except (PatternUnavailable, PatternDisagreement, PatternCacheShadowed):
        raise
    except BaseException as exc:  # noqa: BLE001 - OSError starting the child, etc.
        # str(exc) here would be OUR OWN plumbing (it carries only our argv, never
        # the guard's content), but the log does not need it: type name only.
        raise PatternUnavailable(type(exc).__name__) from None
    child_pattern = _parse_verdict(raw)
    # COMPILED equality, not argv tuples: ast sees flags=0 on a bare
    # re.compile(text) while the executed object carries re.UNICODE (32) — a
    # (text, flags) comparison fails on the repo's own shipped guard (measured,
    # round 4). PatternDisagreement carries no payload: a verdict we refused has
    # nothing safe to say about it.
    if (child_pattern.pattern != source_pattern.pattern
            or child_pattern.flags != source_pattern.flags):
        raise PatternDisagreement()
    return source_pattern


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
    except PatternCacheShadowed:
        # Distinct sentence on top of the shared baseline: an operator seeing it
        # must not chase a redactor bug when the tree carries a cache artifact.
        _emit(MSG_CACHE_SHADOWED)
        _emit(MSG_CANNOT_LOAD)
        _emit(MSG_REFUSING)
        return 3
    except PatternDisagreement:
        # The child answered correctly and was still refused. Never quote what it
        # said — the reason we refused is that we cannot trust its bytes.
        _emit(MSG_DISAGREEMENT)
        _emit(MSG_CANNOT_LOAD)
        _emit(MSG_REFUSING)
        return 3
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
