#!/usr/bin/env python3
"""The ONE implementation of "does this content carry a real operator address".

pom11/hscc is a PUBLIC repository, so a real LAN host or tailnet address that
reaches a commit is a leak, not a typo. This module is the detector; it has TWO
callers and must stay the single source of truth for the pattern:

  * ``hscc_daemon/tests/test_no_real_addresses_committed.py`` — the pytest gate,
    which scans everything git *tracks*.
  * ``.githooks/pre-commit`` — the commit-time gate (``--staged``), which scans
    the files in the index. Needed because the pytest gate only fires when
    somebody runs the suite: the 2026-10-08 leak (a ledger tick that recorded a
    verbatim ``mount_nfs`` command) was a docs-only commit that never ran it.

Detection lives HERE, not in either caller — a second regex is a second
opportunity to get it wrong, and the two gates must never disagree about what
is a leak.

Placeholders are the documented convention and are ALLOWED:
  * ``10.0.0.x``   for a LAN node
  * ``100.64.0.1`` for a tailnet host

100.64.0.0/10 is CGNAT, which is where Tailscale hands out addresses, so a real
tailnet address lives somewhere in it. Rather than ban the whole range — tests
legitimately need a tailnet-shaped host — 100.64.0.0/24 is reserved as the
SANCTIONED FIXTURE BLOCK (100.64.0.1, 100.64.0.3, ...). Anything else in CGNAT,
or anything on the live LAN, is a real address and must not be committed.

Stdlib only, no network, no config files: the pre-commit hook runs this on every
commit, so anything it costs gets paid by every commit in the repo.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

LAN_PLACEHOLDER = "10.0.0.x"
TAILNET_PLACEHOLDER = "100.64.0.1"

# The boundary is an ANTI-TRUNCATION guard, not a delimiter requirement.
#
# It used to be `\b ... \b`, which requires the address to sit between two
# NON-WORD characters — i.e. it assumes an address is written the way prose
# writes it. Measured (t_1b7b3166, py3.11.16 + py3.13.12, 26x26 delimiter
# cross-product) that misses every occurrence touching a word character on
# EITHER side, and `\w` is Unicode, so the blind class was not just a
# `LABEL_<addr>` identifier token but also ordinary CJK prose with no spacing
# (`服务器<addr>`, `<addr>的配置`), plus the byte-adjacency inside a compiled blob.
# 315 of 676 matrix cells missed. (No real address is spelled out anywhere in this
# file, including here and including adjacent to a label — the widened boundary
# below now flags that shape in its own source, which is the correct outcome for a
# gate whose first test subject is its own repo.)
#
# What the guard actually needs to reject is an address that is a FRAGMENT of a
# longer digit run — a digit glued on either side means the token is not a dotted
# quad at all, and flagging those would make the tool unusable. A word character
# on either side is not that: it is a label, and the address in it is still a real
# address. So the boundary is now digit-exclusion on both sides, which drops the
# miss set from 315 cells to exactly the digit row and column (51), while keeping
# `10.0.0.x`, `100.64.0.1` and the whole sanctioned `100.64.0.0/24` fixture block
# accepted — including when they too are adjacent to a word char.
#
# `[0-9]` and not `\d`: in text mode `\d` is Unicode-aware, and these octets are
# ASCII. The explicit class makes the pattern byte-for-byte identical whether it
# is applied to str or to bytes (the redactor ships this `.pattern` over a
# child-process protocol and re-compiles it on the far side).
FORBIDDEN = re.compile(
    r"(?<![0-9])(?:"
    r"100\.(?:6[5-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}"  # CGNAT above 100.64
    r"|100\.64\.(?!0\.)\d{1,3}\.\d{1,3}"                          # 100.64.x, x != 0
    r"|192\.168\.88\.\d{1,3}"                                      # the live LAN
    r")(?![0-9])"
)

# Binary/vendored paths that would only produce noise.
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".pdf", ".ico", ".zip", ".gz", ".xcuserstate")

# Build artefacts: refused outright, whatever their content (t_3e6d3db7).
#
# Why a *refusal* and not an extract-and-scan. The guard used to treat any
# NUL-bearing blob as out of scope, so a committed `.pyc` was invisible to the
# hook, the pytest gate and the CI backstop alike -- and a `.pyc` embeds the
# string constants of the source it was compiled from, which is exactly where a
# real address lands (this repo had one in published history:
# `hscc-provision/__pycache__/hscc.cpython-313.pyc`, removed by the 2026-10-09
# rewrite). `.gitignore` cannot help once the blob is tracked: a fresh clone and
# a CI checkout get what was committed.
#
# Extract-and-scan was declined for this class, and t_3e6d3db7's stated reason was
# that FORBIDDEN was measurably BLIND to a compiled blob (`\b` assumed a delimited
# occurrence; marshal writes no delimiter after a string constant, so the next
# interned name's first char -- a word char -- broke the trailing boundary).
# t_1b7b3166 MEASURED that premise again after replacing `\b` with digit-exclusion
# and it is NO LONGER TRUE: 7 marshal framings of a genuine `py_compile` output are
# now all seen, in text, bytes and latin-1 form. The refusal is kept, on the two
# blind spots that ARE irreducible and that no boundary change can reach:
#
#   * a DEFLATE-compressed container member (`.whl`/`.zip`/`.jar`) does not contain
#     the address bytes at all -- pinned by
#     test_a_deflated_container_member_is_unreachable_at_any_boundary, whose stored
#     control shows readability is a property of the format, not of the pattern;
#   * a NUL-bearing blob with an unlisted extension is never decoded at all --
#     pinned by test_the_nul_gate_not_the_pattern_is_now_the_blind_layer, which now
#     demonstrates the pattern CAN match those bytes, so the gate owns that silence.
#
# Either way the refusal never reads the bytes, so it cannot be defeated by framing.
# The stopping-rule argument stands untouched -- a `.pyc` is a container, but so are
# `.zip`, `.gz`, `.jar` and PDFs with embedded fonts, and every layer is code paid
# for on every commit in the repo, which is the thing that gets `--no-verify`'d.
# Full reasoning + measurements: docs/audits/address-guard-binary-policy-t_3e6d3db7.md
# and docs/audits/address-guard-boundary-t_1b7b3166.md
BUILD_ARTEFACT_SUFFIXES = (
    ".pyc", ".pyo", ".pyd", ".o", ".a", ".so", ".dylib", ".dll",
    ".class", ".jar", ".egg", ".whl", ".pyz",
)
BUILD_ARTEFACT_PATH_SEGMENTS = ("__pycache__",)

# The escape hatch for a binary that genuinely belongs in this repo. It is
# deliberately WEAKER than it looks. t_3e6d3db7's §1b reason was "the text scan is
# BLIND to a compiled blob, so a hatch that waived a compiled refusal would waive
# detection with it"; t_1b7b3166 re-measured that and the compiled blob is now READABLE
# (see the note above BUILD_ARTEFACT_SUFFIXES). The tier survives on the blindness that
# IS irreducible -- a deflated container member has no bytes to scan, and whether any
# given blob is readable depends on the container format and on whatever interpreter
# emitted it, neither of which a reviewer controlling this list can see. A hatch whose
# safety depends on bytes a foreign build produced is not a control. Hence two tiers:
#
#   * a path whose SUFFIX is in BUILD_ARTEFACT_SUFFIXES is refused no matter what
#     this list says -- a compiled artefact is never waivable, so widening the
#     hatch can never hide a leak;
#   * everything else the refusal covers (a `__pycache__/` segment, a path with an
#     unknown extension that is NUL-bearing) may be waived, and such a path is
#     then decoded and text-scanned even when it carries NUL bytes -- it stays
#     open to the detector rather than being skipped unread.
#
# Consequence to weigh before widening: a prebuilt `.so` can never be tracked
# here. Today's tracked binary population is two `.png` files, so that costs
# nothing; if a real need appears, add a *non-artefact* extension rather than
# reaching for this list. Empty today, and it lives in the one file the guard's
# tests pin, so widening it is a visible diff in a security control rather than a
# config edit.
ALLOWED_BINARY_PATHS = frozenset()


class GuardError(RuntimeError):
    """The guard could not run (no git, unreadable repo). Not a leak verdict."""


# ── pure detection ───────────────────────────────────────────────────────────

def find_in_text(text):
    """Return [(line_no, matched_address), ...] for every forbidden address."""
    hits = []
    for i, line in enumerate(text.split("\n"), 1):
        m = FORBIDDEN.search(line)
        if m:
            hits.append((i, m.group(0)))
    return hits


def is_build_artefact(rel):
    """True if ``rel`` is a build artefact that must never be tracked here.

    Name-based, deliberately: it must be decidable WITHOUT reading the file, so
    the verdict costs nothing on the commit path and applies even to a path whose
    bytes are unreadable or absent from the working tree. A compiled artefact
    renamed to a text extension is not caught — see the limits section of
    docs/audits/address-guard-binary-policy-t_3e6d3db7.md.

    The suffix tier runs FIRST and ignores ``ALLOWED_BINARY_PATHS`` on purpose
    (round-1 review of t_3e6d3db7, re-measured by t_1b7b3166): a compiled blob is
    now READABLE by the pattern, so this tier is no longer justified by scanner
    blindness -- it holds because readability depends on the container format and
    on whichever interpreter emitted the blob, neither visible to the reviewer who
    edits the hatch. If the hatch could waive a `.pyc`/`.so` refusal, a leak could
    still ride through in a deflated container member that no pattern can read,
    and the guarantee "widening the hatch can never hide a leak" would be false.
    Only the segment tier is waivable.
    """
    lowered = rel.lower()
    if lowered.endswith(BUILD_ARTEFACT_SUFFIXES):
        return True                                  # never waivable
    if rel in ALLOWED_BINARY_PATHS:
        return False                                 # segment tier only
    # Git always reports paths with forward slashes; a backslash is matched too
    # so a Windows-authored `pkg\__pycache__\x.pyc` cannot dodge the segment test.
    normalised = lowered.replace("\\", "/")
    return any(seg in normalised.split("/") for seg in BUILD_ARTEFACT_PATH_SEGMENTS)


# The verdict text for a refused build artefact. A constant, not an inline
# literal, because `artefact_offender` writes it and `report` has to recognise it
# to sort offenders into the two classes it explains differently.
BUILD_ARTEFACT_OFFENDER_SUFFIX = "build artefact must not be tracked"


def artefact_offender(rel):
    """The offender string for a refused build artefact, or None.

    One formatter for both gates: `scan_blob` (staged) and `scan_paths` (tracked)
    must name an artefact identically, or the hook and the CI job disagree about
    the same tree -- the exact failure this repo keeps being reminded of.
    """
    if is_build_artefact(rel):
        # Line 0: there is no line to point at. The offence is the path itself.
        return f"{rel}:0: {BUILD_ARTEFACT_OFFENDER_SUFFIX}"
    return None


def is_skipped_asset(rel):
    """A declared asset the guard does not decode (`.png`, `.zip`, ...).

    ONE predicate for both gates: `scan_paths` has a pre-read skip shortcut and
    `scan_blob` has the same rule after the read, and if those two spellings ever
    drift the hook and the CI job start disagreeing about the same tree. So both
    call this. It is also the reason the hatch belongs here and not in the
    callers: an allowlisted path must never be skipped unread by either gate,
    whatever extension it has.
    """
    return rel not in ALLOWED_BINARY_PATHS and rel.endswith(SKIP_SUFFIXES)


def scan_blob(rel, data):
    """Scan one file's bytes; return offender strings ``rel:line: address``.

    Bytes (not str) so the same function serves a working-tree read and a staged
    blob read. Four cases, in this order:

      * a build artefact is refused on its NAME, before anything is decoded
        (t_3e6d3db7), and a compiled-artefact SUFFIX is refused even if it sits on
        ``ALLOWED_BINARY_PATHS`` — the text scan below cannot see an address in a
        compiled blob, so waiving the refusal would waive detection;
      * a path on ``ALLOWED_BINARY_PATHS`` (segment tier only, per the previous
        bullet) is exempt from the refusal and is therefore decoded and scanned
        EVEN IF it carries NUL bytes and even if its extension is in
        ``SKIP_SUFFIXES`` — the hatch may say "this path may be tracked", never
        "this path may be unread" (round-1 review: an allowlisted `.pdf` used to
        short-circuit here and go unscanned);
      * anything else whose extension is in ``SKIP_SUFFIXES`` is skipped — declared
        assets the guard has no business decoding;
      * anything else with a NUL byte is treated as binary and skipped — same
        intent as SKIP_SUFFIXES, and it keeps a vendored asset from being decoded.
    """
    offender = artefact_offender(rel)
    if offender:
        return [offender]
    # Two ways a blob stays unread -- a declared asset (`is_skipped_asset`, which
    # already exempts the hatch) and an unknown NUL-bearing blob (exempted here).
    # The hatch may say "this path may be tracked", never "this path may be unread".
    if is_skipped_asset(rel) or (rel not in ALLOWED_BINARY_PATHS
                                 and not isinstance(data, str) and b"\0" in data):
        return []
    if not isinstance(data, str):
        data = data.decode("utf-8", errors="ignore")
    return [f"{rel}:{line}: {addr}" for line, addr in find_in_text(data)]


def scan_paths(root, rels, read=None):
    """Scan ``rels`` (relative to ``root``) from disk. Missing/unreadable files
    are skipped, matching the pytest gate (a deleted-but-tracked path is not a
    leak, and an unreadable file cannot be judged) -- EXCEPT a build artefact,
    which is refused on its name alone: a path in HEAD's index is a tracked
    artefact whether or not the working tree happens to carry the bytes."""
    root = Path(root)
    read = read or (lambda p: p.read_bytes())
    offenders = []
    for rel in rels:
        offender = artefact_offender(rel)
        if offender:
            offenders.append(offender)
            continue
        if is_skipped_asset(rel):
            continue
        p = root / rel
        if not p.is_file():
            continue
        try:
            blob = read(p)
        except OSError:
            continue
        offenders.extend(scan_blob(rel, blob))
    return offenders


# ── git plumbing ─────────────────────────────────────────────────────────────

def _git(repo, args, *, input_bytes=None, check=True):
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo)] + args,
            capture_output=True, input=input_bytes,
        )
    except OSError as exc:  # git not on PATH
        raise GuardError(f"git is not runnable ({exc})") from exc
    if check and proc.returncode != 0:
        raise GuardError(
            "git " + " ".join(args) + f" failed: {proc.stderr.decode('utf-8', 'ignore').strip()}"
        )
    return proc


def tracked_paths(repo):
    """Every path git tracks in HEAD's index (the pytest gate's population)."""
    out = _git(repo, ["ls-files", "-z"]).stdout
    return [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]


def staged_paths(repo):
    """Paths ADDED/COPY/MODIFIED/RENAMED/TYPED-CHANGE in the index.

    Deletions are excluded on purpose: a removed file cannot introduce a new
    leak, and its staged blob does not exist so it could not be read anyway.
    """
    if _git(repo, ["rev-parse", "--verify", "-q", "HEAD"], check=False).returncode != 0:
        # No HEAD yet: the whole index is the "staged tree".
        out = _git(repo, ["ls-files", "--cached", "-z"]).stdout
        return [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]
    out = _git(
        repo,
        ["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRT", "HEAD"],
    ).stdout
    return [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]


def staged_blobs(repo, rels):
    """Return {rel: bytes} for the STAGED content of ``rels``.

    Reads the INDEX (``:path`` specs), never the working tree, via ONE
    ``cat-file --batch`` subprocess. This is the whole point of the hook: the
    2026-08-30 audit's pre-push check grepped the working tree, which had already
    been scrubbed while the committed blob still carried the address.

    ``--batch`` eats one spec per LINE, so a path containing a newline would be
    split into two bogus specs and silently skipped — an evasion. Those names
    (git allows them) go through a per-path ``cat-file blob`` instead, where the
    path is a single argv element and cannot be split. Normal names keep the
    single-batched read, so the common case stays one subprocess.
    """
    if not rels:
        return {}
    batchable = [r for r in rels if "\n" not in r]
    awkward = [r for r in rels if "\n" in r]
    blobs = {}

    if batchable:
        specs = b"\n".join(b":" + r.encode("utf-8", "surrogateescape") for r in batchable)
        buf = _git(repo, ["cat-file", "--batch"], input_bytes=specs).stdout
        i = 0
        for rel in batchable:
            nl = buf.find(b"\n", i)
            if nl < 0:
                break  # batch stream ended early (should not happen)
            header = buf[i:nl].decode("utf-8", "ignore").split()
            if len(header) == 3 and header[1] == "blob":
                size = int(header[2])
                blobs[rel] = buf[nl + 1: nl + 1 + size]
                i = nl + 1 + size + 1  # skip the record terminator
            else:
                # Unresolvable spec ("<op> <path> missing"): skip this path
                # rather than desynchronise the stream.
                i = nl + 1

    for rel in awkward:
        proc = _git(repo, ["cat-file", "blob", ":" + rel], check=False)
        if proc.returncode == 0:
            blobs[rel] = proc.stdout
    return blobs


# ── gates ────────────────────────────────────────────────────────────────────

def scan_staged(repo):
    """Offenders in the staged tree (the pre-commit gate).

    The artefact verdict is taken from `staged_paths` directly, not from the blob
    dict: `staged_blobs` legitimately has no entry for a spec git could not
    resolve, and a name-based refusal must not depend on having read the bytes.
    """
    rels = staged_paths(repo)
    offenders = [o for o in (artefact_offender(r) for r in rels) if o]
    offenders.extend(
        o
        for rel, blob in staged_blobs(repo, rels).items()
        if not is_build_artefact(rel)   # already refused by name above
        for o in scan_blob(rel, blob)
    )
    return offenders


def scan_tracked(repo):
    """Offenders across every tracked file on disk (the pytest gate)."""
    return scan_paths(repo, tracked_paths(repo))


def report(offenders, scope="staged tree"):
    """The failure text: offending file:line, then the fix for each class.

    Two classes of offender, two different fixes, so they are reported apart. An
    address is scrubbed to a placeholder; a build artefact is removed from the
    index (telling someone to "scrub to 10.0.0.x" a `.pyc` is a dead end, and a
    dead end on a commit path is how `--no-verify` gets used).
    """
    artefacts, addresses = [], []
    for o in offenders:
        (artefacts if o.endswith(BUILD_ARTEFACT_OFFENDER_SUFFIX) else addresses).append(o)

    lines = [
        (f"REAL OPERATOR ADDRESS in the {scope} — this repo is PUBLIC, the commit is blocked."
         if addresses else
         f"BUILD ARTEFACT in the {scope} — this repo is PUBLIC, the commit is blocked."),
        "",
    ]
    lines += [f"  {o}" for o in addresses[:20]]
    if len(addresses) > 20:
        lines.append(f"  ... and {len(addresses) - 20} more")
    if addresses:
        lines += [
            "",
            "Scrub them to the documented placeholders:",
            f"  LAN node     -> {LAN_PLACEHOLDER}",
            f"  tailnet host -> {TAILNET_PLACEHOLDER}",
        ]
    if artefacts:
        lines += [
            "",
            f"BUILD ARTEFACTS tracked in the {scope} — refused whatever they contain",
            "(a .pyc embeds the string constants of its source, so it can carry an",
            "address no text scan will ever see):",
        ]
        lines += [f"  {o}" for o in artefacts[:20]]
        if len(artefacts) > 20:
            lines.append(f"  ... and {len(artefacts) - 20} more")
        lines += [
            "",
            "Untrack them:  git rm --cached <path>   (and keep them ignored; see",
            "BUILD_ARTEFACT_SUFFIXES in scripts/address_guard.py for what is refused",
            "and ALLOWED_BINARY_PATHS for the escape hatch — which can waive a path",
            "segment, never a compiled-artefact suffix).",
        ]
    lines += [
        "",
        "If a file genuinely must carry a real address it must not be committed"
        " to this repo at all. Do not use `git commit --no-verify` to get past"
        " this on a docs file: that is how the 2026-10-08 leak reached origin.",
    ]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Block real operator addresses from commits.")
    ap.add_argument("--staged", action="store_true", help="scan the staged tree (pre-commit default)")
    ap.add_argument("--tracked", action="store_true", help="scan every tracked file on disk")
    ap.add_argument("--repo", default=None, help="repo/worktree to inspect (default: cwd)")
    args = ap.parse_args(argv)

    try:
        repo = Path(args.repo).resolve() if args.repo else Path(
            _git(".", ["rev-parse", "--show-toplevel"]).stdout.decode().strip()
        )
        if args.tracked:
            offenders, scope = scan_tracked(repo), "tracked tree"
        else:
            offenders, scope = scan_staged(repo), "staged tree"
    except GuardError as exc:
        print(f"address_guard: CANNOT RUN — {exc}", file=sys.stderr)
        return 2
    if offenders:
        print(report(offenders, scope), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
