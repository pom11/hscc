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

FORBIDDEN = re.compile(
    r"\b(?:"
    r"100\.(?:6[5-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}"  # CGNAT above 100.64
    r"|100\.64\.(?!0\.)\d{1,3}\.\d{1,3}"                          # 100.64.x, x != 0
    r"|192\.168\.88\.\d{1,3}"                                      # the live LAN
    r")\b"
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
# Extracting strings from arbitrary binaries answers the wrong question. If it
# finds an address in a `.pyc` the fix is still "do not track this file", and if
# it finds none the file is still build output with no business being here. It
# would also have no stopping rule -- a `.pyc` is a container, but so are `.zip`,
# `.gz`, `.jar` and PDFs with embedded fonts -- and every layer is code paid for
# on every commit in the repo, which is the thing that gets `--no-verify`'d.
# Full reasoning + measurements:
# docs/audits/address-guard-binary-policy-t_3e6d3db7.md
BUILD_ARTEFACT_SUFFIXES = (
    ".pyc", ".pyo", ".pyd", ".o", ".a", ".so", ".dylib", ".dll",
    ".class", ".jar", ".egg", ".whl", ".pyz",
)
BUILD_ARTEFACT_PATH_SEGMENTS = ("__pycache__",)

# A binary that genuinely belongs in this repo goes here: it is exempt from the
# artefact REFUSAL only -- the address scan still applies to it, so widening this
# can never hide a leak. Empty today; adding to it must be a deliberate,
# reviewed diff in this file (the guard's tests pin the scanner's contract, not
# this list, so this stays the single visible place to widen the policy).
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
    """
    if rel in ALLOWED_BINARY_PATHS:
        return False
    lowered = rel.lower()
    if lowered.endswith(BUILD_ARTEFACT_SUFFIXES):
        return True
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


def scan_blob(rel, data):
    """Scan one file's bytes; return offender strings ``rel:line: address``.

    Bytes (not str) so the same function serves a working-tree read and a staged
    blob read. Three cases, in this order:

      * a build artefact is refused on its NAME, before anything is decoded
        (t_3e6d3db7);
      * a path on ALLOWED_BINARY_PATHS is exempt from that refusal, and is
        therefore scanned as text EVEN IF it carries NUL bytes — utf-8/ignore
        decoding is what makes it cheap, and the one binary we chose to track is
        exactly the binary whose contents we cannot afford to wave through
        unread (the hatch must never be able to hide a leak);
      * anything else with a NUL byte is treated as binary and skipped — same
        intent as SKIP_SUFFIXES, and it keeps a vendored asset from being decoded.
    """
    offender = artefact_offender(rel)
    if offender:
        return [offender]
    allowlisted = rel in ALLOWED_BINARY_PATHS
    if rel.endswith(SKIP_SUFFIXES) or (not allowlisted and not isinstance(data, str)
                                       and b"\0" in data):
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
        if rel.endswith(SKIP_SUFFIXES):
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
            "and ALLOWED_BINARY_PATHS for the escape hatch).",
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
