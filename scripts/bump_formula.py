#!/usr/bin/env python3
"""Bump the Homebrew tap's ``Formula/hscc.rb`` to a released hscc tag.

WHY THIS EXISTS
---------------
``pom11/homebrew-hscc`` installs hscc from the GitHub **release tarball**, so the
formula pins three facts about a release: the archive ``url``, its ``sha256`` and
(implicitly) the ``version``. Before this script every release hand-edited those
three lines, which is exactly the kind of step that gets forgotten: the tap then
serves the previous release while ``CHANGELOG`` and the ``latest`` release say
otherwise, and the only symptom is a user silently installing an old build.

This script is the release-flow step (``release`` card / orchestrator gate):

    python3 scripts/bump_formula.py --tag v2.13.0 --dry-run   # prove the read-path
    python3 scripts/bump_formula.py --tag v2.13.0             # commit + push the tap

WHAT IT COMPUTES (nothing is trusted, nothing is hand-entered)
--------------------------------------------------------------
* ``url``       = ``https://github.com/<release-repo>/archive/refs/tags/<tag>.tar.gz``
* ``sha256``    = the hash of the bytes downloaded from THAT url. There is no
  ``--sha256`` flag on purpose: a value typed in by a caller (or copied from a
  release body) is a value that can be wrong, and Homebrew's only integrity check
  for the tarball is this digest. Downloaded to a temp file, hashed, deleted.
* ``version``   = the tag minus its leading ``v``, cross-checked against the
  ``VERSION`` file **inside the tarball** (see ``--check-version``), because the
  tap's own ``test do`` block asserts ``hscc #{version}`` — a tag cut without
  bumping ``VERSION`` produces a formula that installs and then fails ``brew test``.

THE TAP HAS NO ``version`` LINE
------------------------------
Homebrew derives a formula's version from its url when the formula does not state
one, and the tap's formula does not state one (its version comes from the
``v2.12.0`` in the archive url). So the edit is normally **two lines** — url +
sha256. If a ``version "..."`` line is ever added to the formula this script
rewrites it too, and it refuses to proceed when an explicit ``version`` line
contradicts the tag. It never *adds* one: Homebrew's own ``brew audit --strict``
treats a version that duplicates the url as noise, and the tap is green under
that audit today.

WHAT IT REFUSES TO TOUCH
------------------------
Only the three release-pinned lines, inside the pre-``resource`` window. The
formula's ``resource`` blocks pin **PyPI** sdists (PyYAML, rich, psutil and
rich's transitives), not hscc, so they are not this release's facts and a bump
that rewrote them would be shipping an untested dependency change under a
version-bump commit message. After rewriting, the script asserts every changed
line index is before the first ``resource "`` and that the count of ``sha256``
lines did not move — so a future formula shape that would swallow a resource
digest fails loudly instead.

THE TWO GATES BEFORE A COMMIT
-----------------------------
The tap is a PUBLIC repo with its own vendored copy of this repo's address guard
(byte-identical to ``scripts/address_guard.py``, run by its own CI). A bump
writes into that repo, so it passes the same gates the tap's own commits pass:

  1. ``ruby -c`` on the rewritten formula — a broken formula breaks ``brew tap``
     for every user of the tap, not just for hscc.
  2. the tap's vendored ``address_guard.py --tracked --repo <tap>`` on the tap
     tree **with the new formula written**. Detection stays in that one module —
     this script does not carry an address pattern of its own; it loads the tap's
     file and calls ``find_in_text`` on the proposed text for ``--dry-run``, and
     shells out to the same file with ``--tracked`` on the write path.

Either gate failing restores the formula byte-for-byte and exits non-zero, so a
half-written tap is not a state the next person can inherit.

IDEMPOTENT
----------
Re-running for the version the formula already pins writes nothing and exits 0
with ``up to date`` (and still runs the read-path checks, which is what makes the
dry run useful evidence). A malformed tag, an unreachable tarball or a tap whose
tree is dirty / diverged from ``origin`` exits non-zero with the tap untouched.

EXIT CODES (same convention as ``scripts/address_guard.py``)
-----------------------------------------------------------
    0  bumped, or already up to date
    1  a gate or a git step rejected the bump (tap restored / never touched)
    2  usage error, or the step COULD NOT RUN (bad tag, unreachable tarball, no
       ruby, no guard, tap missing) — never a silent pass

Env overrides (all optional, for a release host that differs from the operator's):
    HSCC_TAP_DIR           tap checkout to bump            (default: ~/dev/homebrew-hscc,
                                                            else clone into the cache dir)
    HSCC_TAP_CACHE_DIR     clone target root               (default: ~/.cache/hscc/taps)
    HSCC_RELEASE_REPO      owner/repo that carries tags    (default: pom11/hscc)
    HSCC_TAP_REPO          owner/repo of the tap           (default: pom11/homebrew-hscc)
    HSCC_ADDRESS_GUARD_PY  interpreter for the guard       (default: this interpreter)
    HSCC_RUBY_BIN          ruby used for ``ruby -c``       (default: ruby on PATH)

Stdlib only, no config files. The one network read is the tarball it must hash.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# ── configuration ────────────────────────────────────────────────────────────

RELEASE_REPO = os.environ.get("HSCC_RELEASE_REPO", "pom11/hscc")
TAP_REPO = os.environ.get("HSCC_TAP_REPO", "pom11/homebrew-hscc")
TAP_URL = f"https://github.com/{TAP_REPO}.git"
FORMULA_REL = "Formula/hscc.rb"
DEFAULT_TAP_DIR = Path(os.path.expanduser("~/dev/homebrew-hscc"))
CACHE_ROOT = Path(os.path.expanduser(
    os.environ.get("HSCC_TAP_CACHE_DIR", "~/.cache/hscc/taps")))

#: A release tag is ``v`` + three dot-separated numeric components. Deliberately
#: NOT a full semver grammar: hscc releases are ``X.Y.Z`` and the tap's version is
#: that string verbatim, so a pre-release suffix (``v2.13.0-rc1``) must be
#: rejected here rather than pushed into a formula Homebrew would read as ``2.13.0-rc1``.
TAG_RE = re.compile(r"^v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$")

#: Guards a bump may not rewrite: the resources pin PyPI, not hscc.
RESOURCE_RE = re.compile(r"^\s*resource\s+\"")


class BumpError(Exception):
    """The bump was rejected (exit 1). The tap is untouched or restored."""


class CannotRun(BumpError):
    """The step could not run at all (exit 2) — never reported as success."""

    RC = 2


BumpError.RC = 1


# ── git / subprocess plumbing ────────────────────────────────────────────────

def _git(repo: Path, args, *, check=True, input_bytes=None, strip=True):
    """Run git inside ``repo``. Raises CannotRun if git itself is unavailable.

    A timeout is CannotRun, not a traceback: a fetch/push that hangs on a network
    is a host condition the release flow has to see as "could not run", and a
    stack trace on the release path hides the one line that says which step hung.
    """
    if shutil.which("git") is None:
        raise CannotRun("git is not on PATH — the tap flow needs git")
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              input=input_bytes, timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise CannotRun(f"git {' '.join(args)} timed out after {exc.timeout}s") from exc
    except OSError as exc:
        raise CannotRun(f"cannot run git: {exc}") from exc
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace").strip()
    if strip:
        out = out.strip()
    if check and proc.returncode != 0:
        raise BumpError(f"git {' '.join(args)} failed (rc={proc.returncode}): {err or out}")
    return out, err, proc.returncode


def _run(cmd, *, check=True):
    """Run a non-git command (ruby -c, the guard) and capture both streams.

    A command that cannot even start (an $HSCC_RUBY_BIN pointing at nothing) is
    CannotRun, not a crash: "the gate was unavailable" and "the gate rejected the
    content" are different answers, and only the first is a host problem.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise CannotRun(f"{cmd[0]} timed out after {exc.timeout}s") from exc
    except OSError as exc:
        raise CannotRun(f"cannot run {cmd[0]}: {exc}") from exc
    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    if check and proc.returncode != 0:
        detail = err or out or "(no output)"
        raise BumpError(f"{cmd[0]} failed (rc={proc.returncode}): {detail}")
    return out, err, proc.returncode


# ── tag / url / tarball ──────────────────────────────────────────────────────

def parse_tag(tag: str) -> str:
    """``v2.13.0`` -> ``2.13.0``. Malformed input is a usage error, not a bump."""
    m = TAG_RE.match(tag or "")
    if not m:
        raise CannotRun(
            f"tag {tag!r} is not a release tag — expected 'v' + MAJOR.MINOR.PATCH "
            "(e.g. v2.13.0); a pre-release or non-versioned tag must not become a "
            "formula version")
    return m.group("version")


def tarball_url(version: str) -> str:
    return f"https://github.com/{RELEASE_REPO}/archive/refs/tags/v{version}.tar.gz"


def fetch_tarball(url: str, dest: Path, timeout: float = 180.0) -> Path:
    """Download ``url`` to ``dest``. An unreachable tarball is CannotRun: there is
    nothing to hash, and guessing a digest is the one failure mode that must never
    be possible."""
    req = urllib.request.Request(url, headers={"User-Agent": "hscc-bump-formula/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                raise CannotRun(f"{url} returned HTTP {status}")
            with dest.open("wb") as fh:
                shutil.copyfileobj(resp, fh, length=1 << 16)
    except CannotRun:
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        raise CannotRun(
            f"cannot fetch the release tarball {url}: {exc}\n"
            "  The sha256 in the formula must be the hash of THIS archive, so the "
            "bump stops here rather than writing a value it could not verify.") from exc
    if not dest.exists() or dest.stat().st_size == 0:
        raise CannotRun(f"{url} downloaded as an empty file")
    return dest


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tarball_version(path: Path) -> str | None:
    """The ``VERSION`` file at the tarball root, or None if the archive has none.

    GitHub stages the archive under ``hscc-<version>/``, so the member is looked
    for one level down as well as at the root.
    """
    try:
        with tarfile.open(path, "r:gz") as tf:
            for member in tf.getmembers():
                parts = member.name.split("/")
                if len(parts) in (1, 2) and parts[-1] == "VERSION" and member.isfile():
                    fh = tf.extractfile(member)
                    if fh is None:
                        continue
                    return fh.read().decode("utf-8", "replace").strip()
    except (tarfile.TarError, OSError) as exc:
        raise BumpError(f"{path.name} is not a readable gzip tarball: {exc}") from exc
    return None


def check_tarball(path: Path, version: str, *, verify_version: bool = True) -> str:
    """Hash the spooled tarball and (by default) cross-check its VERSION file.

    The version cross-check is load-bearing, not belt-and-braces: the tap's
    ``test do`` asserts ``hscc #{version}`` where ``version`` is the tag, while
    ``hscc --version`` prints the tarball's ``VERSION`` file. A release cut
    without bumping ``VERSION`` would therefore install cleanly and then fail
    ``brew test hscc`` on every machine — caught here, at the one place that has
    the bytes.
    """
    digest = sha256_file(path)
    if not verify_version:
        return digest
    inner = tarball_version(path)
    if inner is None:
        raise BumpError(
            "the release tarball carries no VERSION file — the tap formula asserts "
            "it exists (odie in install), so this tag is not a releasable tree")
    if inner != version:
        raise BumpError(
            f"tag says v{version} but the tarball's VERSION file says {inner!r} — "
            "the tap formula would pin the tag while `hscc --version` prints the "
            "file, and `brew test hscc` asserts the two agree. Fix the release "
            "(bump VERSION, re-tag) instead of bumping the tap.")
    return digest


# ── the formula rewrite (pure) ───────────────────────────────────────────────

def _formula_lines(text: str) -> list[str]:
    return text.split("\n")


def _resource_start(lines: list[str]) -> int:
    for i, line in enumerate(lines):
        if RESOURCE_RE.match(line):
            return i
    return len(lines)


def rewrite_formula(text: str, version: str, sha256: str, *, url: str | None = None):
    """Return ``(new_text, changes)`` — changes is [(lineno, before, after), ...].

    Pure: no I/O, so the three pinned lines can be tested against a fabricated
    formula without a tap, a network, or ruby. Raises BumpError (tap untouched)
    when the formula does not have the shape this flow assumes.
    """
    url = url or tarball_url(version)
    if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
        raise BumpError(f"sha256 {sha256!r} is not 64 hex chars — refusing to write it")

    lines = _formula_lines(text)
    limit = _resource_start(lines)
    changes: list[tuple[int, str, str]] = []

    # 1. the archive url — matched on the release-repo archive shape, so a
    #    pythonhosted resource url can never be the thing that gets rewritten.
    url_idx = None
    url_re = re.compile(
        r'^(?P<indent>\s*)url\s+"https://github\.com/' + re.escape(RELEASE_REPO)
        + r'/archive/refs/tags/[^"]+\.tar\.gz"$')
    for i in range(limit):
        m = url_re.match(lines[i])
        if m:
            if url_idx is not None:
                raise BumpError(
                    f"two hscc archive urls in {FORMULA_REL} (lines {url_idx + 1} "
                    f"and {i + 1}) — refusing to guess which one pins the release")
            url_idx = i
    if url_idx is None:
        raise BumpError(
            f"no hscc release-archive url line found in {FORMULA_REL} before the "
            "first resource block — the formula layout is not what this flow expects; "
            "update rewrite_formula() rather than editing the formula by hand")
    indent = url_re.match(lines[url_idx]).group("indent")
    new_url_line = f'{indent}url "{url}"'
    if new_url_line != lines[url_idx]:
        changes.append((url_idx + 1, lines[url_idx], new_url_line))
        lines[url_idx] = new_url_line

    # 2. the sha256 that belongs to THAT url: the first one between the url and
    #    the first resource block. (Resource blocks carry their own sha256 lines.)
    sha_idx = None
    for i in range(url_idx + 1, limit):
        if re.match(r'^\s*sha256\s+"', lines[i]):
            sha_idx = i
            break
    if sha_idx is None:
        raise BumpError(
            f"no sha256 line for the release archive in {FORMULA_REL} (searched "
            f"lines {url_idx + 2}-{limit}) — refusing to pin a release with no digest")
    sha_line = re.sub(r'(")[0-9a-fA-F]{64}(")', rf'\g<1>{sha256}\g<2>', lines[sha_idx])
    if sha_line != lines[sha_idx]:
        changes.append((sha_idx + 1, lines[sha_idx], sha_line))
        lines[sha_idx] = sha_line

    # 3. an explicit version line, IF the formula has one (the tap's does not).
    for i in range(limit):
        m = re.match(r'^(?P<indent>\s*)version\s+"(?P<v>[^"]*)"$', lines[i])
        if not m:
            continue
        if m.group("v") == version:
            break
        new_line = f'{m.group("indent")}version "{version}"'
        changes.append((i + 1, lines[i], new_line))
        lines[i] = new_line
        break

    # Post-conditions on the edit itself: the diff must stay inside the
    # pre-resource window and must not change how many digests exist.
    if any(lineno - 1 >= limit for lineno, _, _ in changes):
        raise BumpError(
            "internal guard: the rewrite touched a line inside a resource block")
    before_digests = sum(1 for l in _formula_lines(text) if re.match(r'^\s*sha256\s+"', l))
    after_digests = sum(1 for l in lines if re.match(r'^\s*sha256\s+"', l))
    if before_digests != after_digests:
        raise BumpError("internal guard: the number of sha256 lines changed")

    return "\n".join(lines), sorted(changes, key=lambda c: c[0])


def formula_version(text: str) -> str:
    """The version this formula installs: an explicit ``version`` line if present,
    else the ``vX.Y.Z`` in the release archive url (what Homebrew itself infers)."""
    lines = _formula_lines(text)
    limit = _resource_start(lines)
    for i in range(limit):
        m = re.match(r'^\s*version\s+"(?P<v>[^"]*)"$', lines[i])
        if m:
            return m.group("v")
    for i in range(limit):
        m = re.search(r'/archive/refs/tags/v(?P<v>[0-9]+\.[0-9]+\.[0-9]+)\.tar\.gz"',
                      lines[i])
        if m:
            return m.group("v")
    raise BumpError(f"{FORMULA_REL} states no version anywhere — cannot decide "
                    "whether it is already current")


# ── tap discovery / state ────────────────────────────────────────────────────

def resolve_tap_dir(explicit: str | None) -> Path:
    """The tap checkout: --tap, else $HSCC_TAP_DIR, else ~/dev/homebrew-hscc,
    else a clone into the cache dir (a release host that has never had the tap)."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.environ.get("HSCC_TAP_DIR"):
        return Path(os.environ["HSCC_TAP_DIR"]).expanduser().resolve()
    if DEFAULT_TAP_DIR.exists():
        return DEFAULT_TAP_DIR
    dest = CACHE_ROOT / Path(TAP_REPO).name
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"tapping: cloning {TAP_URL} into {dest}")
    if shutil.which("git") is None:
        raise CannotRun("git is not on PATH — cannot clone the tap")
    proc = subprocess.run(["git", "clone", "--quiet", TAP_URL, str(dest)],
                          capture_output=True, timeout=600)
    if proc.returncode != 0:
        raise CannotRun(f"cannot clone {TAP_URL}: "
                        f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    return dest


def validate_tap(tap: Path) -> Path:
    tap = tap.expanduser().resolve()
    if not tap.exists():
        raise CannotRun(f"tap checkout {tap} does not exist")
    toplevel, _, rc = _git(tap, ["rev-parse", "--show-toplevel"], check=False)
    if rc != 0 or not toplevel:
        raise CannotRun(f"{tap} is not a git repository")
    formula = tap / FORMULA_REL
    if not formula.is_file():
        raise CannotRun(f"{tap} has no {FORMULA_REL} — is this really the "
                        f"{TAP_REPO} tap?")
    remote, _, rrc = _git(tap, ["remote", "get-url", "origin"], check=False)
    if rrc != 0 or not remote:
        raise CannotRun(f"tap checkout {tap} has no origin remote — the bump "
                        f"pushes to {TAP_REPO}, so a checkout without one is refused")
    if not remote.rstrip("/").rstrip(".git").endswith(TAP_REPO.split("/")[1]):
        raise CannotRun(f"tap checkout {tap} points at {remote}, not {TAP_REPO} — "
                        "refusing to commit and push into another repository")
    return tap


def require_clean(tap: Path) -> None:
    """The bump commit must contain exactly one file. A dirty tap would drag the
    operator's unrelated work into it (or be silently mixed with it)."""
    out, _, _ = _git(tap, ["status", "--porcelain", "--untracked-files=all"])
    if out:
        raise BumpError(
            f"tap checkout {tap} is not clean — commit or stash this first:\n"
            + "\n".join(f"  {line}" for line in out.split("\n")[:10]))


def branch_of(tap: Path) -> str:
    name, _, _ = _git(tap, ["rev-parse", "--abbrev-ref", "HEAD"])
    if name == "HEAD":
        raise BumpError(f"tap checkout {tap} is detached — check out the tap's "
                        "default branch (main) before bumping")
    return name


def sync_tap(tap: Path) -> str:
    """Fast-forward the checkout onto origin; return where it stood.

    "in-sync" | "behind" (origin had moved; the checkout was fast-forwarded) |
    "ahead" (this checkout carries commits origin does not — a bump whose push died).
    Divergence is an error, never a rebase or a merge: the release flow must not
    invent history in a public tap.
    """
    _git(tap, ["fetch", "--quiet", "origin"])
    branch = branch_of(tap)
    local, _, _ = _git(tap, ["rev-parse", "HEAD"])
    remote, _, rrc = _git(tap, ["rev-parse", "--verify", f"origin/{branch}"],
                          check=False)
    if rrc != 0 or not remote:
        raise CannotRun(f"tap checkout {tap} has no origin/{branch} to sync against")
    if local == remote:
        return "in-sync"
    _, _, anc = _git(tap, ["merge-base", "--is-ancestor", remote, local], check=False)
    if anc != 0:
        _, _, anc2 = _git(tap, ["merge-base", "--is-ancestor", local, remote],
                          check=False)
        if anc2 != 0:
            raise BumpError(
                f"tap checkout {tap} has diverged from origin/{branch} — reconcile it "
                "by hand; this tool will not rebase or merge public tap history")
        _git(tap, ["merge", "--ff-only", f"origin/{branch}"])
        return "behind"
    return "ahead"


# ── the two gates ────────────────────────────────────────────────────────────

def guard_path(tap: Path) -> Path:
    """The guard to run: the tap's vendored copy (its CI uses the same file), else
    this repo's. Never a second regex, and never silently absent."""
    for cand in (tap / ".github" / "address_guard.py",
                 Path(__file__).resolve().parent / "address_guard.py"):
        if cand.is_file():
            return cand
    raise CannotRun(
        "no address_guard.py found (looked for the tap's .github/address_guard.py "
        "and scripts/address_guard.py) — the tap bump fails closed rather than "
        "committing an unscanned tree")


def load_guard(tap: Path):
    """Import the guard module (for find_in_text on PROPOSED text in --dry-run).

    ``sys.dont_write_bytecode`` is forced during the import: without it Python
    drops ``.github/__pycache__/address_guard.cpython-XYZ.pyc`` into the TAP
    working tree, and that stray untracked file then trips this script's own
    clean-tree check on the very next run (self-inflicted, and it looks like
    operator dirt).
    """
    path = guard_path(tap)
    spec = importlib.util.spec_from_file_location(f"hscc_bump_guard_{abs(hash(str(path)))}",
                                                  path)
    if spec is None or spec.loader is None:
        raise CannotRun(f"cannot load the address guard at {path}")
    mod = importlib.util.module_from_spec(spec)
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - a broken guard must not pass
        raise CannotRun(f"the address guard at {path} failed to load: {exc}") from exc
    finally:
        sys.dont_write_bytecode = prev
    if not hasattr(mod, "find_in_text"):
        raise CannotRun(f"{path} has no find_in_text — not the address guard?")
    return mod


def check_text(guard, text: str, where: str) -> None:
    hits = guard.find_in_text(text)
    if hits:
        raise BumpError(
            f"address guard rejected {where}: "
            + ", ".join(f"line {n}" for n, _ in hits[:5])
            + " — the tap is PUBLIC, so scrub to the documented placeholders "
              "(LAN node / tailnet host) before bumping")


def ruby_check(formula: Path) -> None:
    """``ruby -c`` on the formula. A tap whose formula does not parse breaks
    ``brew tap`` for everyone, so this is not optional — but the binary is
    overridable via $HSCC_RUBY_BIN so the suite is host-independent."""
    ruby = os.environ.get("HSCC_RUBY_BIN") or shutil.which("ruby")
    if not ruby:
        raise CannotRun(
            "ruby is not available (set $HSCC_RUBY_BIN) — a formula that does not "
            "parse breaks `brew tap` for every user of the tap, so the bump is not "
            "attempted without a syntax check")
    out, err, _ = _run([ruby, "-c", str(formula)], check=False)
    combined = f"{out}\n{err}".strip()
    if "Syntax OK" not in combined:
        raise BumpError(f"ruby -c rejected the rewritten formula: {combined or '(no output)'}")


def guard_tracked(tap: Path) -> None:
    """The tap-wide gate, on the tap tree AS WRITTEN (the new formula included)."""
    guard = guard_path(tap)
    py = os.environ.get("HSCC_ADDRESS_GUARD_PY") or sys.executable
    out, err, rc = _run([py, str(guard), "--tracked", "--repo", str(tap)], check=False)
    if rc != 0:
        detail = (err or out or "(no output)").split("\n")
        raise BumpError(
            f"the tap's address guard ({guard}) rejected the tree — rc={rc}, "
            "formula restored:\n" + "\n".join(f"  {line}" for line in detail[:12]))


# ── the flow ─────────────────────────────────────────────────────────────────

def _plan(original: str, version: str, digest: str, url: str, tap: Path, phase: str):
    """Rewrite + verify + guard the PROPOSED text; return (new_text, changes).

    Called once before the tap is touched and again after the fast-forward, because
    the sync can bring someone else's formula edit in. ``phase`` only names the
    check in the error text so a rejection says which content was rejected.
    """
    new_text, changes = rewrite_formula(original, version, digest, url=url)
    pinned = formula_version(new_text)
    if pinned != version:
        raise BumpError(f"internal guard: the rewrite pins version {pinned!r}, "
                        f"expected {version!r}")
    where = FORMULA_REL if phase == "pre" else f"{FORMULA_REL} (after sync)"
    check_text(load_guard(tap), new_text, where if phase == "pre"
               else f"proposed {where}")
    return new_text, changes


def _ahead_detail(tap: Path, branch: str):
    """(count, other_paths) for commits the checkout has that origin lacks.

    ``other_paths`` are the files those commits touch besides the formula. The tool
    may publish its OWN bump commit, but it must not push an operator's unpublished
    work into a public tap on the side — that is a disclosure decision, not a
    release step.
    """
    n, _, _ = _git(tap, ["rev-list", "--count", f"origin/{branch}..HEAD"])
    files, _, _ = _git(tap, ["diff", "--name-only", f"origin/{branch}..HEAD"])
    others = sorted({f for f in files.split("\n") if f and f != FORMULA_REL})
    return n, others


def bump(tag: str, *, tap_dir=None, dry_run=False, push=True,
         verify_version=True, tarball=None, timeout=180.0):
    """Run the whole flow. Returns a dict describing the outcome (used by tests).

    Order matters: every read-only check that can fail (tag shape, tarball
    reachability + hash + VERSION, proposed-content guard) happens BEFORE the tap's
    working tree is touched at all. Only then: sync -> write -> ruby -> guard ->
    commit -> push, with the original bytes restored if anything fails before the
    commit lands.

    "up to date" is a claim about the PUBLIC tap, so it is only ever made after the
    checkout has been synced onto origin and found to carry no unpushed commit. The
    three ways a local formula can look correct while the tap still serves the old
    release — an uncommitted hand edit, a stale checkout, a bump commit whose push
    died — are each answered below, in the order a repair must happen in.
    """
    version = parse_tag(tag)
    url = tarball_url(version)
    tap = validate_tap(resolve_tap_dir(tap_dir))
    formula = tap / FORMULA_REL
    original = formula.read_text(encoding="utf-8")
    current = formula_version(original)

    # Fetch + hash + cross-check BEFORE touching anything: there is no point
    # reporting a bump the archive cannot support, and nothing is written until the
    # digest is real.
    tmpdir = None
    try:
        if tarball:
            src = Path(tarball).expanduser().resolve()
            if not src.is_file():
                raise CannotRun(f"--tarball {src} is not a file")
            tar_path = src
        else:
            tmpdir = tempfile.mkdtemp(prefix="hscc-tap-bump-")
            tar_path = fetch_tarball(url, Path(tmpdir) / f"v{version}.tar.gz",
                                     timeout=timeout)
        digest = check_tarball(tar_path, version, verify_version=verify_version)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

    new_text, changes = _plan(original, version, digest, url, tap, "pre")

    if dry_run:
        # Read-only: no sync, no clean check, no write. A preview says what the
        # bytes say right now, plus the one thing a real run would refuse on.
        if not changes:
            dirty, _, _ = _git(tap, ["status", "--porcelain", "--", FORMULA_REL])
            print(f"up to date: {FORMULA_REL} already pins v{version} "
                  f"(sha256 {digest[:12]}… re-hashed from the live tarball and it "
                  "matches)")
            if dirty:
                print(f"note: that pin is an UNCOMMITTED edit here — a real run "
                      "refuses to commit an edit it did not write")
            print("dry run: tap untouched (no sync, no write, no commit, no push)")
            return {"status": "up-to-date", "version": version, "sha256": digest,
                    "url": url, "changes": [], "pushed": False, "committed": False,
                    "dry_run": True}
        print(f"bumping {FORMULA_REL}: v{current} -> v{version} "
              f"({len(changes)} line(s)):")
        print("\n".join(x for c in changes
                        for x in (f"  line {c[0]}:", f"    - {c[1]}", f"    + {c[2]}")))
        print("dry run: tap untouched (no sync, no write, no commit, no push)")
        return {"status": "dry-run", "version": version, "sha256": digest,
                "url": url, "changes": changes, "pushed": False, "committed": False}

    # A pin the tool did not write is not the tool's to commit — checked before the
    # generic clean check so the message names the real problem.
    if not changes:
        dirty, _, _ = _git(tap, ["status", "--porcelain", "--", FORMULA_REL])
        if dirty:
            raise BumpError(
                f"{FORMULA_REL} pins v{version} only as an UNCOMMITTED edit in "
                f"{tap} — the public tap does not serve it, so this run cannot "
                "call the release done. Commit the edit (or revert it and re-run "
                "without --dry-run) — this tool will not commit an edit it did "
                "not write.")

    # State checks BEFORE the tree is touched: `require_clean` can only mean "the
    # operator's own dirt" while the formula still matches HEAD, and syncing first
    # means the bump commit lands on top of origin, never beside it.
    require_clean(tap)
    branch = branch_of(tap)
    state = sync_tap(tap)

    # A fast-forward can carry someone ELSE's formula edit, and an "up to date"
    # verdict computed from pre-sync bytes can be stale. Re-plan against what the
    # checkout holds now; the plan, not the earlier read, decides what gets written.
    synced = formula.read_text(encoding="utf-8")
    if synced != original:
        original, current = synced, formula_version(synced)
        new_text, changes = _plan(original, version, digest, url, tap, "post")

    if not changes:
        # Nothing to write. If the checkout is ahead of origin, this is a bump whose
        # push died — publish it rather than report that everything was already fine.
        if state == "ahead":
            n, others = _ahead_detail(tap, branch)
            if others:
                raise BumpError(
                    f"{tap} already pins v{version} but its {n} unpushed commit(s) "
                    "touch files besides " + FORMULA_REL + ": "
                    + ", ".join(others[:5]) + ("…" if len(others) > 5 else "")
                    + f" — pushing would publish them into the public {TAP_REPO}. "
                    "Push or drop them by hand first; this tool publishes only the "
                    "formula bump.")
            if not push:
                raise BumpError(
                    f"the tap checkout already pins v{version} but the commit is not "
                    f"on origin/{branch}, and --no-push was given: the PUBLIC tap "
                    "still serves the previous release. Push it (or drop --no-push).")
            _git(tap, ["push", "origin", f"HEAD:refs/heads/{branch}"])
            print(f"published the unpushed commit(s) that carry it: "
                  f"{FORMULA_REL} -> {version} (tap: {tap})")
            return {"status": "bumped", "version": version, "sha256": digest,
                    "url": url, "changes": [], "pushed": True,
                    "committed": False, "recovered_push": True}
        where = " after sync" if state == "behind" else ""
        print(f"up to date{where}: {FORMULA_REL} already pins v{version} "
              f"(sha256 {digest[:12]}… re-hashed from the live tarball and it matches)")
        return {"status": "up-to-date", "version": version, "sha256": digest,
                "url": url, "changes": [], "pushed": False, "committed": False}

    # An "ahead" checkout may carry the operator's unpublished work. A push publishes
    # everything on the branch, so foreign files in those commits stop the run BEFORE
    # anything is written: publishing someone's private tap work is a disclosure
    # decision, not a release step. (A bump commit of its own is fine to publish —
    # that is the recovery case above.) With --no-push nothing is published, so the
    # situation is only worth a notice.
    if state == "ahead":
        n, others = _ahead_detail(tap, branch)
        listed = ", ".join(others[:5]) + ("…" if len(others) > 5 else "")
        if others and push:
            raise BumpError(
                f"{tap} has {n} unpushed commit(s) touching files besides "
                f"{FORMULA_REL}: {listed} — pushing would publish them into the "
                f"public {TAP_REPO}. Push or drop them by hand first; this tool "
                "publishes only the formula bump.")
        if others:
            print(f"notice: {n} unpushed commit(s) here besides the bump ({listed}); "
                  "they stay local because nothing is pushed with --no-push")

    # `wrote` makes the restore exact: once the commit has landed the formula
    # content IS the released state, so a later failure must not roll the tap back
    # to the old pin and leave a commit on top of it describing a file that file no
    # longer is.
    wrote = False
    try:
        formula.write_text(new_text, encoding="utf-8")
        ruby_check(formula)
        guard_tracked(tap)
        _git(tap, ["add", "--", FORMULA_REL])
        # The message carries no paths, no usernames, no hosts: it lands in a public
        # repo. `--no-verify` is never needed here because the same content has just
        # passed the tap's own vendored guard.
        subject = f"hscc.rb: bump to {version} for release {tag}"
        body = (f"Automated by scripts/bump_formula.py from the {tag} release tarball.\n"
                "sha256 computed from the archive at publish time; the tap's ruby "
                "syntax check and address guard both ran on the committed tree.")
        _git(tap, ["commit", "-m", subject, "-m", body])
        wrote = True
    except BaseException:
        if not wrote:
            formula.write_text(original, encoding="utf-8")
        raise

    pushed = False
    try:
        if push:
            _git(tap, ["push", "origin", f"HEAD:refs/heads/{branch}"])
            pushed = True
    except BumpError as exc:
        raise BumpError(f"{exc}\n  The bump commit is in the tap checkout but was NOT "
                        "pushed — fix the remote (auth, or a non-fast-forward) and "
                        "re-run: the commit is already there, so re-running will only "
                        "push it.") from exc
    print(f"{'pushed' if pushed else 'committed (not pushed; --no-push)'} "
          f"{FORMULA_REL} -> {version} (tap: {tap})")
    return {"status": "bumped", "version": version, "sha256": digest, "url": url,
            "changes": changes, "pushed": pushed, "committed": True}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=(__doc__ or "").split("WHY THIS EXISTS")[0].strip()
                    or "Bump the Homebrew tap formula to a released hscc tag.")
    ap.add_argument("--tag", required=True, help="release tag to pin, e.g. v2.13.0")
    ap.add_argument("--tap", default=None,
                    help=f"tap checkout (default: $HSCC_TAP_DIR or {DEFAULT_TAP_DIR}, "
                         "else a clone into $HSCC_TAP_CACHE_DIR)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch, hash, verify and print the diff; touch nothing")
    ap.add_argument("--no-push", action="store_true",
                    help="commit into the tap checkout but do not push")
    ap.add_argument("--no-version-check", dest="verify_version", action="store_false",
                    help="skip the tarball-VERSION == tag cross-check (debug only; "
                         "`brew test hscc` asserts they agree)")
    ap.add_argument("--tarball", default=None,
                    help="hash this local archive instead of downloading (offline/debug)")
    ap.add_argument("--timeout", type=float, default=180.0,
                    help="seconds to allow for the tarball download (default: 180)")
    args = ap.parse_args(argv)

    try:
        bump(args.tag, tap_dir=args.tap, dry_run=args.dry_run, push=not args.no_push,
             verify_version=args.verify_version, tarball=args.tarball,
             timeout=args.timeout)
    except BumpError as exc:
        print(f"bump_formula: {exc}", file=sys.stderr)
        return getattr(type(exc), "RC", 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
