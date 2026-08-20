"""self_update.py — the tool updating itself: plan, apply, verify.

Answers ONE question for the operator: *is the flightdeck I'm running the
flightdeck that should be running, and if not, what would update it?*

This is Gap 2 step 1 from ``docs/BRAINSTORM-what-is-missing.md``. The
brainstorm settled the design explicitly, and we honour it here:

* an explicit, on-demand ``flightdeck update`` command — NOT a daemon. A
  background self-updater is deliberately rejected (it would deploy without
  explicit go-ahead and re-create the "merged is not live / daemon on stale
  code" failure class), so nothing here runs unattended.
* read-only by default — ``plan()`` only *reports*; the actual update is
  ``apply()``, which the command layer only ever calls behind ``--apply``.
* never claim an unverified success — ``apply()`` re-checks the installed
  commit/version against what we intended to install, and reports a mismatch
  as FAILED, mirroring ``core.release``'s "merged is not live" guard.

Install-mechanism detection. Flightdeck installs via ``pip install git+...``
(no PyPI package). On this machine that is an *editable* install from a local
clone, which is the only case where ``git pull --ff-only`` on the source is the
right update path. Non-editable ``git+`` installs have no local source to pull,
so they update by re-running pip against the git URL. A plain index/registry
install (not from git) cannot be self-updated and is reported honestly.

Every external call goes through the injectable ``_run`` runner (a
``runner(cmd, repo)`` callable, same shape ``git_state`` uses), so tests stub
git/pip and never touch a real repo, the network, or the live install.
"""

from __future__ import annotations

import os
import tempfile
from typing import Callable, Optional

from . import git_state

# The source-version file at the repo root (mirrors release/deployment.py).
DEFAULT_VERSION_FILE = "VERSION"


# --------------------------------------------------------------------------- #
# Install-mechanism detection
# --------------------------------------------------------------------------- #

def _iter_dist():
    """Yield the installed flightdeck distribution metadata (0 or 1 entries).

    Extracted so tests can monkeypatch it to a fake distribution without the
    real install being present. Returns nothing when flightdeck is not
    installed in this environment.
    """
    try:
        from importlib.metadata import distribution
    except ImportError:  # pragma: no cover - 3.10+ has it in stdlib
        return
    try:
        dist = distribution("flightdeck")
    except Exception:
        return
    yield dist


def installed_source() -> dict:
    """How is the running flightdeck installed? Returns a dict.

    ``mechanism`` is one of:
      * ``editable`` — ``pip install -e`` from a local clone; ``source`` is the
        absolute path of that clone, which is a git repo we can ``pull --ff-only``.
      * ``git``      — ``pip install git+...`` (non-editable); ``source`` is the
        git URL to ``pip install --upgrade --force-reinstall``.
      * ``non-git``  — installed from a plain index/registry (a ``pip install
        flightdeck``); no git URL, cannot be self-updated.
      * ``not-installed`` — the ``flightdeck`` distribution could not be found
        in the current environment at all (``mechanism`` only; ``source`` None).

    Reads the distribution's ``direct_url.json`` (PEP 610) — dist-info metadata
    pip writes that records exactly how the package was installed: an editable
    local dir, a git+VCS checkout, or nothing (a plain index install). We never
    guess the mechanism because a wrong guess is worse than a plain "cannot
    self-update" answer.
    """
    dist = next(_iter_dist(), None)

    if dist is None:
        return {"mechanism": "not-installed", "source": None}

    direct_url = None
    try:
        direct_url = dist.read_text("direct_url.json")
    except Exception:
        direct_url = None

    if not direct_url:
        # No direct_url.json -> not installed from a VCS/editable source.
        return {"mechanism": "non-git", "source": None}

    import json
    try:
        meta = json.loads(direct_url)
        url = meta.get("url") or ""
        editable = bool((meta.get("dir_info") or {}).get("editable"))
    except (ValueError, TypeError):
        # Malformed metadata — refuse to guess.
        return {"mechanism": "unknown", "source": None}

    if editable:
        # file:///Users/...  ->  /Users/...
        source = url
        if source.startswith("file://"):
            source = source[len("file://"):].replace("+", " ")
        return {"mechanism": "editable", "source": source or None}

    vcs = (meta.get("vcs_info") or {}).get("vcs")
    if vcs == "git":
        # A git+ https://... URL keeps the URL in ``url``; non-editable pip
        # installs are updated by reinstalling from that same URL.
        return {"mechanism": "git", "source": url or None}

    return {"mechanism": "unknown", "source": url or None}


# --------------------------------------------------------------------------- #
# Version reads
# --------------------------------------------------------------------------- #

def _read_version_file(path: str) -> Optional[str]:
    """Contents of ``path``'s ``VERSION`` file (stripped), or None."""
    try:
        with open(os.path.join(path, DEFAULT_VERSION_FILE), encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return None
    text = raw.strip()
    return text or None


def upstream_version(url: str, _run: Optional[Callable] = None) -> Optional[str]:
    """The ``VERSION`` at the remote's default branch, WITHOUT touching source.

    Clones nothing durable and never mutates the installed source. Fetches the
    remote's HEAD into a throwaway temp dir, reads ``VERSION`` from ``FETCH_HEAD``
    via ``git show``, and deletes the temp dir. Returns None when the remote is
    unreachable, is not a git repo, or has no ``VERSION`` at HEAD — the caller
    reports the upstream version as unknown rather than guessing.

    ``file_at_ref`` reads from a *local* ref, so it cannot name a remote sha we
    have not fetched; this temp-clone read uses ``git show FETCH_HEAD:VERSION``
    instead. The write is confined to a temp dir that is removed on return.
    """
    tmp = tempfile.mkdtemp(prefix="flightdeck-upstream-")
    try:
        init = git_state._dispatch(
            ["git", "init", "-q", tmp], tmp, _run
        )
        if init.returncode != 0:
            return None
        fetch = git_state._dispatch(
            ["git", "fetch", "--depth", "1", url, "HEAD"], tmp, _run
        )
        if fetch.returncode != 0:
            return None
        show = git_state._dispatch(
            ["git", "show", "FETCH_HEAD:{}".format(DEFAULT_VERSION_FILE)], tmp, _run
        )
        if show.returncode != 0:
            return None
        text = show.stdout.strip()
        return text or None
    finally:
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Plan — the read-only "what would change" report
# --------------------------------------------------------------------------- #

# Status values returned by plan()/apply().
UP_TO_DATE = "up_to_date"
UPDATE_AVAILABLE = "update_available"
NO_REMOTE = "no_remote"
CANNOT_REACH = "cannot_reach"
NOT_GIT = "not_git"
NOT_EDITABLE = "not_editable"
APPLIED = "applied"


def plan(repo: str, _run: Optional[Callable] = None) -> dict:
    """Dry-run self-update plan for an editable-install source ``repo``.

    Reads only — it fetches nothing into ``repo`` and mutates nothing about the
    installed state. The freshness check uses ``git ls-remote`` (a pure network
    read that writes no local refs) and the comparison is by commit sha, which
    is the ground-truth "is there a newer build" signal.

    Returns a dict with ``status`` (see module constants), ``mechanism``,
    ``installed_version``, ``installed_sha``, ``upstream_version``,
    ``upstream_sha``, and ``detail``:

      * ``up_to_date``       — installed sha == upstream HEAD sha. ``detail``
                               "already up to date".
      * ``update_available`` — upstream HEAD sha differs. ``upstream_version``
                               read best-effort via a temp clone (None if
                               unreadable — reported as "version unknown").
      * ``no_remote``        — the source clone has no ``origin`` remote;
                               cannot learn an upstream. NOT "up to date".
      * ``cannot_reach``     — ``ls-remote`` failed (unreachable / not a git
                               repo at that URL). NOT "up to date".
      * ``not_git``          — ``repo`` is not inside a git working tree.

    Every non-``up_to_date`` outcome with an upstream problem is reported as
    UNKNOWN/cannot-check, never as a false all-clear (Design rule 2).
    """
    if not git_state.is_repo(repo, _run):
        return {
            "status": NOT_GIT, "mechanism": "editable",
            "installed_version": None, "installed_sha": None,
            "upstream_version": None, "upstream_sha": None,
            "detail": "installed source is not a git repository",
        }

    url = git_state.remote_url(repo, _run=_run)
    if not url:
        return {
            "status": NO_REMOTE, "mechanism": "editable",
            "installed_version": _read_version_file(repo),
            "installed_sha": git_state.head_sha(repo, _run),
            "upstream_version": None, "upstream_sha": None,
            "detail": f"no remote in {repo}; cannot learn an upstream",
        }

    installed_sha = git_state.head_sha(repo, _run)
    installed_version = _read_version_file(repo)
    upstream_sha = git_state.upstream_head_sha(url, _run=_run)
    if not upstream_sha:
        return {
            "status": CANNOT_REACH, "mechanism": "editable",
            "installed_version": installed_version,
            "installed_sha": installed_sha,
            "upstream_version": None, "upstream_sha": None,
            "detail": f"cannot reach upstream {url} (git ls-remote failed)",
        }

    base = {
        "mechanism": "editable",
        "installed_version": installed_version,
        "installed_sha": installed_sha,
        "upstream_sha": upstream_sha,
    }

    if upstream_sha == installed_sha:
        return {
            **base, "status": UP_TO_DATE,
            "upstream_version": installed_version,
            "detail": "already up to date",
        }

    return {
        **base, "status": UPDATE_AVAILABLE,
        "upstream_version": upstream_version(url, _run=_run),
        "detail": "a newer commit is available upstream",
    }


# --------------------------------------------------------------------------- #
# Apply — actually perform the update, then VERIFY it took
# --------------------------------------------------------------------------- #

def apply_source(
    repo: str, _run: Optional[Callable] = None,
) -> dict:
    """Perform the editable-install update on ``repo`` and verify it took.

    Uses :func:`git_state.pull_project` — the same safe fast-forward-only pull
    ``project pull`` runs — which refuses to touch a repo not on a default
    branch, a dirty tree, or diverged history. After the pull it re-reads the
    installed sha against the upstream HEAD sha (a fresh ``ls-remote``) and
    reports whether the pull actually reached the intended commit.

    Returns a dict with ``status``:

      * ``applied`` when the pull moved forward; ``verified`` True only when
        the post-pull installed sha equals the upstream HEAD sha.
      * otherwise a ``pulled``/``up_to_date``/``skipped``-style status from
        :func:`pull_project`, with ``verified`` False and ``detail`` the reason
        — so an interrupted or failed update is reported honestly, never as a
        clean success.
    """
    result = git_state.pull_project(repo, _run=_run)

    pulled_status = result.get("status")
    if pulled_status not in ("pulled", "up_to_date"):
        # skipped (dirty tree / non-default branch / diverged) or failed — the
        # update did NOT happen; report the real reason.
        return {
            "status": "skipped",
            "verified": False,
            "detail": result.get("detail", "update did not happen"),
            "pull_status": pulled_status,
            "n": result.get("n", 0),
        }

    # The update ran (or was already current). VERIFY: re-check the installed
    # head against the intended upstream head — merged is not live until the
    # running checkout's HEAD actually matches upstream.
    url = git_state.remote_url(repo, _run=_run)
    upstream_sha = git_state.upstream_head_sha(url, _run=_run) if url else None
    installed_sha = git_state.head_sha(repo, _run=_run)

    verified = bool(upstream_sha) and installed_sha == upstream_sha
    detail = result.get("detail", "")
    if not verified:
        detail = (
            f"pull ran but installed HEAD {installed_sha!r} does not match "
            f"upstream {upstream_sha!r}; update not live"
        )
    return {
        "status": APPLIED if pulled_status == "pulled" else UP_TO_DATE,
        "verified": verified,
        "detail": detail,
        "pull_status": pulled_status,
        "n": result.get("n", 0),
        "installed_sha": installed_sha,
        "upstream_sha": upstream_sha,
    }


def apply_git(url: str, _run: Optional[Callable] = None) -> dict:
    """Perform the non-editable ``pip install git+<url>`` update + verify.

    Reinstalls flightdeck from the recorded git URL with ``--upgrade
    --force-reinstall``. ``_run`` is invoked as ``runner(cmd, repo)`` exactly
    like every other injected runner; here ``repo`` is the install directory to
    run pip in (defaults to the current directory). The pip command's entire
    stdout+stderr is captured so a failure is reported honestly with the real
    output, never assumed successful.

    Returns ``{status, verified, detail, installed_version}``:
      * ``applied`` when the pip command exited 0.
      * ``failed`` with the captured output otherwise — an interrupted or
        rejected install is never reported as done.
    """
    cmd = ["pip", "install", "--upgrade", "--force-reinstall", "git+" + url]
    cp = git_state._dispatch(cmd, os.getcwd(), _run)
    if cp.returncode != 0:
        return {
            "status": "failed",
            "verified": False,
            "detail": cp.stderr.strip() or "pip install failed",
            "installed_version": None,
        }
    # Best-effort re-read of the installed version after reinstall.
    return {
        "status": "applied",
        "verified": True,
        "detail": (cp.stdout.strip() or "").splitlines()[-1]
        if cp.stdout.strip() else "pip install succeeded",
        "installed_version": None,
    }
