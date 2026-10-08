"""PATH-independent resolution of the ``sparkrun`` CLI and its venv interpreter.

Why this exists (t_b543e530)
----------------------------
Every daemon-side fleet fact is read THROUGH sparkrun, and every recovery action
is issued THROUGH the ``sparkrun`` CLI. Both resolutions used to be a bare
``shutil.which("sparkrun")`` — which is correct only as long as the process'
PATH happens to contain ``~/.local/bin``, where the CLI actually lives.

A launchd-supervised daemon does not get that PATH. Measured live on
2026-10-08 (daemon pid 31237, ``ps eww``):

    PATH=/Users/<user>/.hermes/hermes-agent/venv/bin:/opt/homebrew/bin:\
/Users/<user>/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

— no ``~/.local/bin``. So ``shutil.which`` returned None and the whole fleet
layer degraded at once:

  * the structured ``api.status`` query was skipped every tick
    ("structured sparkrun status unavailable", 230 occurrences in one day);
  * the auto-heal relaunch path died with ``[Errno 2] No such file or
    directory: 'sparkrun'`` — a dropped worker could not be relaunched;
  * the DGX check still logged ``ok=True`` on an empty workload list, i.e. an
    empty read was indistinguishable from a healthy empty fleet — the exact
    v1.15.0 local-``docker ps`` failure class (correct logic on a false
    environment assumption).

This module makes resolution depend on the *filesystem*, not on PATH: the
``sparkrun`` entry-point script is looked for at PATH first (respects an
operator's deliberate install), then at a deterministic candidate list, and the
first candidate that is a readable executable wins. The interpreter is then
read from that script's shebang — still sparkrun's own venv python, still no
hardcoded interpreter, and still sparkrun's own sanctioned transport
(``api.status`` / ``run_remote_script``); nothing here reimplements fleet
transport.

Resolution is cached on purpose: a service restart of the CLI's location is a
deploy event, so callers may keep the resolved absolute path for the process
lifetime (``reset_cache()`` clears it — tests, and anything that re-deploys
sparkrun in-process).
"""

from __future__ import annotations

import os
import shutil

CLI_NAME = "sparkrun"

# Where the ``sparkrun`` entry-point script can live, in probe order, tried
# whenever PATH does not yield it. The first two are the real deployment on
# this fleet (``~/.local/bin/sparkrun`` is a symlink into sparkrun's own venv);
# the rest are the conventional install locations so a fresh host resolves too.
# Paths are deliberately derived from $HOME (never a literal user path), and
# this list is a *fallback* — PATH still wins when it knows better.
def _candidate_paths(home: str) -> list:
    return [
        os.path.join(home, ".local", "bin", CLI_NAME),
        os.path.join(home, "sparkrun", ".venv-sparkrun-py313", "bin", CLI_NAME),
        os.path.join(home, "sparkrun", ".venv", "bin", CLI_NAME),
        os.path.join(home, ".venv-sparkrun", "bin", CLI_NAME),
        "/opt/homebrew/bin/" + CLI_NAME,
        "/usr/local/bin/" + CLI_NAME,
    ]


# Cache: CLI path (str) or None once probed. A None is NOT cached — a later
# sparkrun install must be picked up without a daemon restart.
_cli_cache = {"resolved": False, "path": None}
_interp_cache = {}


def _is_usable(path):
    """True when ``path`` is an existing, readable, executable regular file.

    Readable (not just executable) because the caller must be able to read the
    shebang; a non-executable file is useless as a CLI regardless.
    """
    try:
        return bool(path) and os.path.isfile(path) and os.access(path, os.R_OK | os.X_OK)
    except OSError:
        return False


def sparkrun_cli() -> str | None:
    """Absolute path to the ``sparkrun`` CLI, or None when genuinely absent.

    ``HSCC_SPARKRUN_BIN`` (an explicit operator/test override) wins when it
    names a usable file; then PATH; then the deterministic candidate list.
    Unlike ``shutil.which`` this keeps working under a service-managed PATH
    that omits ``~/.local/bin``.
    """
    override = os.environ.get("HSCC_SPARKRUN_BIN", "")
    if override:
        return override if _is_usable(override) else None
    if _cli_cache["resolved"]:
        return _cli_cache["path"]

    found = shutil.which(CLI_NAME)
    if found and _is_usable(found):
        found = os.path.realpath(found)
    else:
        found = None
        home = os.path.expanduser("~") or ""
        for cand in _candidate_paths(home):
            if _is_usable(cand):
                found = os.path.realpath(cand)
                break
    _cli_cache["resolved"] = True
    _cli_cache["path"] = found
    return found


def _shebang_line(path):
    """The raw text after ``#!`` on ``path``'s first line, or None.

    None means "no shebang" (e.g. a compiled shim) or the file is unreadable.
    """
    try:
        with open(path, "rb") as f:
            first = f.readline().decode("utf-8", "replace").strip()
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    return first[2:].strip() or None


def _resolve_shebang(shebang, cli_path):
    """Absolute interpreter named by a ``#!`` line, or None if unresolvable.

    Two forms:
      * direct — ``#!/path/to/venv/bin/python3.13`` (what a venv-installed
        console script carries): the path itself, if it exists;
      * env — ``#!/usr/bin/env python3``: a bare program name. Resolve it
        FIRST as a sibling of the CLI (a venv puts its interpreter in the same
        ``bin/`` as the console script — that sibling IS sparkrun's interpreter),
        then fall back to PATH. Resolving the sibling before PATH matters: the
        service PATH's ``python3`` is the Hermes/host python, which cannot
        ``import sparkrun``.
    """
    if not shebang:
        return None
    # ``-S`` and other env flags: the interpreter is the last token that names
    # a program (``/usr/bin/env -S python3 -u`` -> python3).
    tokens = [t for t in shebang.split() if not t.startswith("-")]
    if not tokens:
        return None
    prog = tokens[-1]
    if os.path.isabs(prog):
        return prog if os.path.exists(prog) else None
    sibling = os.path.join(os.path.dirname(cli_path), prog)
    if os.path.exists(sibling):
        return sibling
    return shutil.which(prog)


def sparkrun_venv_python() -> str | None:
    """The python interpreter that owns the ``sparkrun`` CLI, or None.

    Read from the CLI's shebang, so the structured-status script runs under
    sparkrun's OWN venv — where sparkrun and its transitive deps actually live
    (hscc_daemon itself runs under the Hermes agent venv and cannot import
    sparkrun). None means the structured path is genuinely unavailable (no CLI
    anywhere, or a CLI whose interpreter cannot be resolved): the caller must
    then use the CLI directly and report degraded visibility, never pretend the
    fleet is empty-and-healthy.
    """
    cli = sparkrun_cli()
    if not cli:
        return None
    if cli in _interp_cache:
        return _interp_cache[cli]
    resolved = _resolve_shebang(_shebang_line(cli), cli)
    _interp_cache[cli] = resolved
    return resolved


def reset_cache() -> None:
    """Forget the resolved CLI/interpreter (tests; post-deploy re-resolution)."""
    _cli_cache["resolved"] = False
    _cli_cache["path"] = None
    _interp_cache.clear()


def ensure_on_path() -> str | None:
    """Put the resolved sparkrun directory on this process' PATH; return it.

    For the remaining ``shell=True`` sparkrun call sites (``timeout 3 sparkrun
    cluster monitor …`` in hscc-cluster, the ``sparkrun cluster list --json``
    topology fallback): an argv rewrite cannot help a shell string, so the
    daemon fixes the ENVIRONMENT once at startup instead of per call. Idempotent
    and additive — the dir is only prepended when missing, so an operator's
    deliberate PATH ordering is preserved and an already-working PATH is
    untouched. Returns the directory, or None when sparkrun is not installed.
    """
    cli = sparkrun_cli()
    if not cli:
        return None
    directory = os.path.dirname(cli)
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if directory not in parts:
        os.environ["PATH"] = os.pathsep.join([directory] + parts)
    return directory


def argv(*args) -> list:
    """Build a sparkrun argv whose ``argv[0]`` is resolvable right now.

    ``sparkrun_bin.argv("status")`` replaces the logical ``["sparkrun", "status"]``
    at EXEC time, so no call site can carry a bare ``sparkrun`` argv[0] into a
    subprocess. Resolution rules are exactly :func:`exec_argv`'s: PATH-executable
    → the bare name (bit-identical to a healthy interactive shell), otherwise the
    absolute resolved path.
    """
    return exec_argv([CLI_NAME, *args])


def exec_argv(argv):
    """Rewrite a sparkrun argv so it is executable under any PATH.

    Command BUILDERS (``serving._unit_run_cmd``, ``fleet_down_cmd``,
    ``VLLM_STOP_CMD``, …) express the logical ``["sparkrun", …]`` command; this
    is the single EXECUTION chokepoint that swaps ``argv[0]`` to the resolved
    absolute path right before the shell-out.

    No-drift rule: ``sparkrun_cli()`` resolves PATH-first, so the absolute path
    is the SAME file a healthy PATH would have exec'd — the rewrite is purely
    mechanical (no behavioural change for well-configured environments) and is
    what makes the acceptance grep ("no bare ``sparkrun`` argv[0] literal")
    true. An explicit ``HSCC_SPARKRUN_BIN`` override always pins execution to
    that file (used by the hermetic test suite). If the CLI cannot be resolved
    at all the argv is returned unchanged so the subprocess fails the same loud
    ``[Errno 2]`` way it does today (never a silent no-op).

    Only an exact bare ``sparkrun`` argv[0] is rewritten — absolute paths and
    other programs pass through untouched.
    """
    if not argv or argv[0] != CLI_NAME:
        return argv
    cli = sparkrun_cli()
    if not cli:
        return argv          # genuinely absent — loud [Errno 2], never silent
    return [cli] + list(argv[1:])
