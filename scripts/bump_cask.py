#!/usr/bin/env python3
"""Render (and optionally publish) the Homebrew cask for HSCC Widgets.

WHY THIS EXISTS
---------------
The macOS app is a notarized, Developer ID-signed ``HSCC Widgets.app`` inside a
release-asset zip. Homebrew installs it from the **cask** in
``pom11/homebrew-hscc``, and a cask pins the same three release facts a formula
does: the asset ``url``, its ``sha256`` and the ``version``. Hand-editing those
into the tap is the step that goes stale, and a wrong digest makes every
``brew install --cask pom11/hscc/hscc-widgets`` fail on a user's Mac.

    python3 scripts/bump_cask.py --version 0.1.0 --zip dist/HSCC-Widgets-0.1.0.zip
    python3 scripts/bump_cask.py --version 0.1.0 --zip dist/HSCC-Widgets-0.1.0.zip --push

WHAT IT COMPUTES
----------------
* ``version``  = the release version, without a leading ``v`` (``macos-widgets-v``
  is the *git tag* prefix, not part of the cask version).
* ``url``      = ``https://github.com/pom11/hscc/releases/download/
  macos-widgets-v<version>/HSCC-Widgets-<version>.zip``.
* ``sha256``   = the hash of the LOCAL zip that was uploaded to that asset. There
  is deliberately no default hand-typed digest: pass ``--zip`` (preferred) or
  ``--sha256`` for an offline render, and the same bytes are what Homebrew
  verifies at install time.

NO LOCAL TAP CLONE
------------------
``--push`` writes ``Casks/hscc-widgets.rb`` through the GitHub contents API with
``gh api`` (create when absent, update with the blob sha when present). It never
clones the tap, so a release host needs ``gh auth`` but no checkout. The script
is idempotent: if the remote file already holds exactly the rendered bytes it
prints ``up to date`` and PUTs nothing.

GATES BEFORE A PUSH
-------------------
The tap is PUBLIC and hosts the same vendored address guard as this repo, so the
rendered cask passes:

  1. the address guard's ``find_in_text`` — detection stays in
     ``scripts/address_guard.py``; this script carries no pattern of its own;
  2. ``ruby -c`` — a cask that does not parse breaks ``brew tap`` for every user.

Neither gate can be skipped. A push failure exits non-zero and says whether the
file was created or updated, so a retry is unambiguous.

EXIT CODES (same convention as ``scripts/address_guard.py``)
------------------------------------------------------------
    0  rendered (and pushed, or already up to date)
    1  a gate rejected the cask, or ``gh api`` rejected the commit
    2  usage error, or the step COULD NOT RUN (bad version, missing zip, no gh,
       no ruby, no guard) — never a silent pass

Env overrides: ``HSCC_RELEASE_REPO`` (default ``pom11/hscc``), ``HSCC_TAP_REPO``
(default ``pom11/homebrew-hscc``), ``HSCC_TAP_BRANCH`` (default ``main``),
``HSCC_RUBY_BIN``, ``HSCC_ADDRESS_GUARD_PY``.

Stdlib only. The only writes are stdout, a temp file for ``ruby -c`` and (with
``--push``) the tap's cask blob.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ── configuration ────────────────────────────────────────────────────────────

RELEASE_REPO = os.environ.get("HSCC_RELEASE_REPO", "pom11/hscc")
TAP_REPO = os.environ.get("HSCC_TAP_REPO", "pom11/homebrew-hscc")
TAP_BRANCH = os.environ.get("HSCC_TAP_BRANCH", "main")
CASK_REL = "Casks/hscc-widgets.rb"
TEMPLATE_REL = Path("templates") / "hscc-widgets.cask.rb.tmpl"
TAG_PREFIX = "macos-widgets-v"
ASSET_TEMPLATE = "HSCC-Widgets-{version}.zip"

#: ``v`` is tolerated on input and stripped; a pre-release suffix is not a cask
#: version Homebrew can compare, so it is rejected rather than published.
VERSION_RE = re.compile(r"^v?(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class CaskError(Exception):
    """The cask was rejected (exit 1). Nothing was pushed."""


class CannotRun(CaskError):
    """The step could not run at all (exit 2) — never reported as success."""

    RC = 2


CaskError.RC = 1


# ── version / url / digest ───────────────────────────────────────────────────

def normalize_version(raw: str) -> str:
    m = VERSION_RE.match(raw or "")
    if not m:
        raise CannotRun(
            f"version {raw!r} is not a release version — expected MAJOR.MINOR.PATCH "
            "(e.g. 0.1.0, optionally prefixed with v); a pre-release or build "
            "suffix is not a cask version Homebrew can compare")
    return m.group("version")


def asset_name(version: str) -> str:
    return ASSET_TEMPLATE.format(version=version)


def asset_url(version: str, repo: str | None = None) -> str:
    repo = repo or RELEASE_REPO
    return (f"https://github.com/{repo}/releases/download/{TAG_PREFIX}{version}/"
            f"{asset_name(version)}")


def sha256_file(path: Path) -> str:
    if not path.is_file():
        raise CannotRun(f"{path} is not a file — pass the zip that was uploaded")
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── the template render (pure apart from reading the template) ───────────────

def template_path() -> Path:
    return Path(__file__).resolve().parent / TEMPLATE_REL


def load_template() -> str:
    path = template_path()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CannotRun(f"cannot read the cask template {path}: {exc}") from exc


def render_cask(version: str, sha256: str, *, url: str | None = None,
                template_text: str | None = None) -> str:
    """Return the cask text. Pure: no tap, no network, no ruby.

    The digest is validated before interpolation so a malformed value can never
    become a cask Homebrew only rejects at install time.
    """
    version = normalize_version(version)
    if not SHA_RE.match(sha256 or ""):
        raise CaskError(f"sha256 {sha256!r} is not 64 lowercase hex chars — "
                        "refusing to write it")
    text = template_text if template_text is not None else load_template()
    replacements = {
        "__HSCC_VERSION__": version,
        "__HSCC_SHA256__": sha256,
        "__HSCC_URL__": url or asset_url(version),
    }
    for token, value in replacements.items():
        text = text.replace(token, value)
    leftover = sorted(set(re.findall(r"__HSCC_[A-Z0-9_]+__", text)))
    if leftover:
        raise CaskError("the cask template still contains placeholder(s) "
                        f"{', '.join(leftover)} — refusing to render a cask with "
                        "an unresolved value")
    if f'cask "hscc-widgets" do' not in text:
        raise CaskError("the cask template lost its `cask \"hscc-widgets\"` header")
    return text


# ── the two gates ────────────────────────────────────────────────────────────

def guard_path() -> Path:
    path = Path(__file__).resolve().parent / "address_guard.py"
    if not path.is_file():
        raise CannotRun(
            f"no address guard at {path} — a public-tap cask is never rendered "
            "unscanned")
    return path


def load_guard():
    """Import the one guard module for its ``find_in_text`` (no second regex)."""
    path = guard_path()
    spec = importlib.util.spec_from_file_location("hscc_cask_guard", path)
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


def check_text(guard, text: str, where: str = CASK_REL) -> None:
    hits = guard.find_in_text(text)
    if hits:
        raise CaskError(
            f"address guard rejected {where}: "
            + ", ".join(f"line {n}" for n, _ in hits[:5])
            + " — the tap is PUBLIC, so scrub to the documented placeholders")


def _run(cmd, *, check=True, timeout=600):
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CannotRun(f"{cmd[0]} timed out after {exc.timeout}s") from exc
    except OSError as exc:
        raise CannotRun(f"cannot run {cmd[0]}: {exc}") from exc
    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    if check and proc.returncode != 0:
        raise CaskError(f"{cmd[0]} failed (rc={proc.returncode}): "
                        f"{err or out or '(no output)'}")
    return out, err, proc.returncode


def ruby_check(text: str) -> None:
    """``ruby -c`` the rendered cask. A broken cask breaks ``brew tap`` for all."""
    ruby = os.environ.get("HSCC_RUBY_BIN") or shutil.which("ruby")
    if not ruby:
        raise CannotRun(
            "ruby is not available (set $HSCC_RUBY_BIN) — a cask that does not "
            "parse cannot be published, so the render is not reported as usable")
    tmpdir = tempfile.mkdtemp(prefix="hscc-cask-")
    try:
        path = Path(tmpdir) / "hscc-widgets.rb"
        path.write_text(text, encoding="utf-8")
        out, err, _ = _run([ruby, "-c", str(path)], check=False)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    combined = f"{out}\n{err}".strip()
    if "Syntax OK" not in combined:
        raise CaskError(f"ruby -c rejected the rendered cask: "
                        f"{combined or '(no output)'}")


# ── gh api (no tap clone) ────────────────────────────────────────────────────

def _gh(args, *, check=True, timeout=300):
    gh = shutil.which("gh")
    if not gh:
        raise CannotRun("gh is not on PATH — --push publishes through `gh api`, "
                        "so it needs the GitHub CLI (brew install gh)")
    return _run([gh, *args], check=check, timeout=timeout)


def remote_cask(*, tap_repo: str | None = None,
                branch: str | None = None) -> tuple[str | None, str | None]:
    """Return ``(text, blob_sha)`` for the cask on the tap's branch.

    ``(None, None)`` means the tap has no cask yet; any other failed read is
    CannotRun, because guessing that the file is absent would turn an auth or
    network failure into a create that later conflicts.
    """
    tap_repo = tap_repo or TAP_REPO
    branch = branch or TAP_BRANCH
    endpoint = f"repos/{tap_repo}/contents/{CASK_REL}?ref={branch}"
    out, err, rc = _gh(["api", endpoint], check=False)
    if rc != 0:
        detail = f"{err}\n{out}"
        if "not found" in detail.lower() or "404" in detail:
            return None, None
        raise CannotRun(f"cannot read {CASK_REL} from {tap_repo}#{branch}: "
                        f"{err or out or '(no output)'}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise CannotRun(f"gh api returned non-JSON for {CASK_REL}: {exc}") from exc
    content = data.get("content") or ""
    try:
        text = base64.b64decode(content).decode("utf-8") if content else ""
    except (ValueError, UnicodeDecodeError) as exc:
        raise CannotRun(f"the tap's {CASK_REL} is not base64 UTF-8 text: {exc}") from exc
    return text, data.get("sha")


def push_cask(text: str, version: str, *, tap_repo: str | None = None,
              branch: str | None = None, dry_run: bool = False) -> dict:
    """Create or update ``Casks/hscc-widgets.rb`` through the contents API."""
    tap_repo = tap_repo or TAP_REPO
    branch = branch or TAP_BRANCH
    if dry_run:
        print(f"dry run: would commit {CASK_REL} to {tap_repo}#{branch} via gh api "
              "(nothing pushed)")
        return {"status": "dry-run", "pushed": False, "tap_repo": tap_repo,
                "branch": branch}

    existing, blob_sha = remote_cask(tap_repo=tap_repo, branch=branch)
    if existing == text:
        print(f"up to date: {CASK_REL} on {tap_repo}#{branch} already holds the "
              f"cask for {version} (nothing pushed)")
        return {"status": "up-to-date", "pushed": False, "tap_repo": tap_repo,
                "branch": branch}

    action = "add" if existing is None else "bump to"
    message = f"hscc-widgets.rb: {action} {version}"
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    args = ["api", "-X", "PUT", f"repos/{tap_repo}/contents/{CASK_REL}",
            "-f", f"message={message}",
            "-f", f"content={payload}",
            "-f", f"branch={branch}"]
    if blob_sha:
        args += ["-f", f"sha={blob_sha}"]
    out, err, rc = _gh(args, check=False)
    if rc != 0:
        raise CaskError(
            f"gh api could not commit {CASK_REL} to {tap_repo}#{branch} "
            f"(rc={rc}): {err or out or '(no output)'}\n"
            "  The tap was NOT changed. Check `gh auth status` and that the token "
            "can write to the tap, then re-run --push.")
    commit = ""
    try:
        commit = (json.loads(out).get("commit") or {}).get("sha", "")
    except json.JSONDecodeError:
        pass
    print(f"{'updated' if blob_sha else 'created'} {CASK_REL} on "
          f"{tap_repo}#{branch} for {version}"
          + (f" (commit {commit[:12]})" if commit else ""))
    return {"status": "pushed", "pushed": True, "tap_repo": tap_repo,
            "branch": branch, "commit": commit, "created": blob_sha is None}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Render the HSCC Widgets cask; optionally publish it to the "
                    "Homebrew tap with `gh api`.")
    ap.add_argument("--version", required=True,
                    help="release version, e.g. 0.1.0 (the git tag is "
                         f"{TAG_PREFIX}<version>)")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--zip", default=None,
                        help="the HSCC-Widgets-<version>.zip that was uploaded; "
                             "its sha256 is pinned")
    source.add_argument("--sha256", default=None,
                        help="explicit digest for an offline render (prefer --zip)")
    ap.add_argument("--push", action="store_true",
                    help="commit the cask to the tap through `gh api`")
    ap.add_argument("--dry-run", action="store_true",
                    help="render and gate; with --push, print the commit target "
                         "without calling gh")
    ap.add_argument("--tap-repo", default=None,
                    help=f"owner/repo of the tap (default: {TAP_REPO})")
    ap.add_argument("--branch", default=None,
                    help=f"tap branch to commit to (default: {TAP_BRANCH})")
    args = ap.parse_args(argv)

    try:
        version = normalize_version(args.version)
        if args.zip:
            digest = sha256_file(Path(args.zip).expanduser())
        else:
            if not SHA_RE.match(args.sha256 or ""):
                raise CannotRun(f"--sha256 {args.sha256!r} is not 64 lowercase hex "
                                "chars")
            digest = args.sha256
        text = render_cask(version, digest)
        check_text(load_guard(), text)
        ruby_check(text)
    except CaskError as exc:
        print(f"bump_cask: {exc}", file=sys.stderr)
        return getattr(type(exc), "RC", 1)

    print(text)
    print(f"cask: {CASK_REL}  version {version}  sha256 {digest}", file=sys.stderr)
    print(f"asset: {asset_url(version)}", file=sys.stderr)

    if args.push:
        try:
            push_cask(text, version, tap_repo=args.tap_repo, branch=args.branch,
                      dry_run=args.dry_run)
        except CaskError as exc:
            print(f"bump_cask: {exc}", file=sys.stderr)
            return getattr(type(exc), "RC", 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
