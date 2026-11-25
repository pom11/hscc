"""HSCC preflight doctor (D13).

Checks every prerequisite a fresh HSCC install needs and explains failures in
plain language with a one-line fix. Used by bootstrap.sh (Stage 1) and runnable
standalone: `python3 doctor.py`.

Each check returns a Check(name, ok, detail, fix, fatal). `run_doctor()` returns
a summary dict; `main()` prints a ✓/✗ checklist and exits non-zero if any FATAL
check failed (so bootstrap can hard-stop).

## ONE-TIME migration to the alias scheme (Card D, v1.5.1)
Existing live configs (pre-v1.5.1) hold CONCRETE model ids — that was the
original bug. After the serving layer advertises stable aliases
(`orchestrator-model` / `worker-model`) and writers emit aliases, the fleet
converges on aliases. `doctor --fix` performs a ONE-TIME conversion of existing
live configs: any concrete model id whose paired `base_url` is UNAMBIGUOUSLY
the orchestrator or worker proxy endpoint is replaced with the matching alias.
Remote/cloud endpoints are never guessed at — operator-chosen endpoints
survive untouched.

SAFETY GATE: before any entry is converted, the endpoint is PROBED (reusing the
`/models` machinery of the "models served" check — `_models_url` + Bearer auth)
and confirmed to actually serve the target alias. If the alias is absent from
the served list, or the endpoint is unreachable, that entry is left UNCONVERTED
and reported loudly (never convert blind — an alias the serving layer does not
resolve would fail every call with 404). Once converged, the serving layer
resolves the alias back to the concrete served id, so converting a confirmed
alias is safe.
"""

from __future__ import annotations

import copy
import inspect
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

import _theme
import backup_util
import doctor_node_recovery
import doctor_role_soul
import doctor_rows


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    fatal: bool = True   # a failed fatal check should hard-stop bootstrap


def _python_ok() -> Check:
    v = sys.version_info
    ok = v >= (3, 9)
    return Check("python", ok,
                 detail=f"{v.major}.{v.minor}.{v.micro}",
                 fix="Install Python 3.9+ (the Hermes venv python is preferred).",
                 fatal=True) if not ok else Check(
        "python", True, detail=f"{v.major}.{v.minor}.{v.micro}")


def _pyyaml_ok() -> Check:
    try:
        import yaml  # noqa: F401
        return Check("pyyaml", True, detail="importable")
    except ImportError:
        return Check("pyyaml", False,
                     detail="PyYAML not importable",
                     fix="pip install pyyaml (or run with the Hermes venv python).",
                     fatal=True)


# ── base-row in-suite fence (t_0d991e1d) ─────────────────────────────────────
# The rows in ``run_doctor()``'s opening ``checks`` list are plain ``Check`` calls:
# they never pass ``doctor_rows.build_row``, so the feature-row fence (t_eb768c2a)
# never applied to them and they carried NO in-suite fence at all. Measured with the
# door spawned as a child of a pytest item (HOME sealed, fake fleet binaries on PATH):
# ``gateway`` reached the machine's service manager, ``sparkrun`` exec'd ``uv python
# find``, ``sparkrun cluster``/``nas`` shelled out to ``sparkrun cluster list``,
# ``models served`` opened its configured endpoints, and the two skill rows walked the
# profile trees.
#
# The ruling is per row and it is about the SEAM a row crosses, not about it being a
# check:
#
#   ACCEPTED — ``python``/``pyyaml`` (the interpreter itself), ``disk space`` (a
#     statvfs), ``hermes`` (a stat under a root the CALLER named), and ``sparkrun``'s
#     PATH lookup (the row's own subject). Cheap, read-only, a property of the machine
#     the door runs on, and nothing a suite verdict can depend on.
#   REFUSED — anything crossing a boundary a suite cannot be trusted with: the fleet
#     CLI (``sparkrun cluster``, ``nas``), the service manager and process table
#     (``gateway``), a socket (``models served``), an operator-owned toolchain binary
#     (``sparkrun``'s uv probe), or a tree whose ROOT came from the environment rather
#     than from a caller (the two skill rows — ``_skills_home()`` walks a
#     profile-scoped HERMES_HOME UP to the shared root, so a suite child would walk
#     every profile on the machine).
#
# A refusal has one shape in this file: an honest ERROR naming the seam a caller must
# inject, ``fatal=False`` (an unverified row is not a broken install), never a silent
# pass and never the real read. The predicate is the ONE shared, child-visible one —
# ``doctor_rows.under_pytest()`` — because a fence that reads only ``sys.modules``
# guards the process nobody is asking about.
def _base_row_fence(injected, name: str, what: str, seam: str,
                    fix: str) -> Optional["Check"]:
    """The shared in-suite fence for a BASE doctor row.

    Returns the refusal when the row would otherwise cross its live seam from inside a
    suite — including from the door a suite SPAWNS, which is why the predicate reads
    the marker pytest exports per item and not just ``sys.modules`` — and ``None``
    when the row may proceed (a seam was injected, or this is the operator's own run).
    """
    if injected is not None or not doctor_rows.under_pytest():
        return None
    return Check(name, False, fatal=False,
                 detail=f"{what} refused inside a pytest process — inject {seam}",
                 fix=fix)


def _root_is_ambient(hermes_home) -> bool:
    """True when a row's Hermes root came from the environment, not from a caller.

    A fixture dir or ``--home PATH`` is the caller's own tree: scanning it is what the
    skill rows exist to do, and ``doctor.py --skills --home <tmp>`` is a supported
    hermetic acceptance run. ``HERMES_HOME`` / ``~/.hermes`` are the operator's, and in
    the CI shape (HOME sealed by scripts/run_tests.sh, HERMES_HOME left pointing at the
    running profile) they are live profile state.
    """
    ambient = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    try:
        return (os.path.realpath(_skills_home(hermes_home))
                == os.path.realpath(_skills_home(ambient)))
    except OSError:
        return True          # cannot tell: treat it as the operator's and refuse


def _uv_default_python(_runner=None):
    """(major, minor) uv resolves for `uv python find` OUTSIDE any project.

    None when UV_PYTHON is exported (an operator pin wins), uv is absent, the
    probe fails, or its output carries no version. This is a WARNING input,
    never a verdict on its own: sparkrun launches LiteLLM through
    `uv tool uvx`, whose interpreter comes from uv's machine-global default —
    the 2026-11-06 outage was uv defaulting to 3.14, where uvloop cannot
    import BaseDefaultEventLoopPolicy (t_425990ae).
    """
    if os.environ.get("UV_PYTHON"):
        return None
    uv = shutil.which("uv")
    if not uv:
        return None
    if _runner is None:
        def _runner():
            return subprocess.run(
                [uv, "python", "find", "--no-project", "--show-version"],
                capture_output=True, text=True, timeout=15)
    try:
        r = _runner()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    m = re.search(r"(\d+)\.(\d+)", getattr(r, "stdout", "") or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def _sparkrun_ok(_uv_runner=None) -> Check:
    path = shutil.which("sparkrun")
    if not path:
        return Check("sparkrun", False, detail="not on PATH",
                     fix="Install sparkrun and ensure it's on PATH.", fatal=True)
    # RULING (t_0d991e1d): the PATH lookup above is ACCEPTED — it is the row's own
    # subject and a property of the machine the door runs on. The uv probe below is
    # REFUSED: it execs an operator-owned toolchain binary (`uv python find`, with a
    # 15 s budget) and reports a machine-global setting the suite did not plant. The
    # row keeps the verdict it DID earn (sparkrun is on PATH) and says out loud which
    # half it could not check — dropping to `ok` with no note would be the silent pass
    # this fence exists to prevent.
    if _uv_runner is None and doctor_rows.under_pytest():
        return Check(
            "sparkrun", True,
            detail=f"{path} — uv default probe refused inside a pytest process — "
                   "inject _uv_runner")
    uv_py = _uv_default_python(_uv_runner)
    if uv_py is not None and uv_py >= (3, 14):
        return Check(
            "sparkrun", False,
            detail=(f"{path} — uv's no-project default is Python "
                    f"{uv_py[0]}.{uv_py[1]}; sparkrun's LiteLLM proxy "
                    "(uvloop) cannot run there"),
            fix=("Set UV_PYTHON=3.12 in the environment that launches the "
                 "daemon (or `uv python pin --global 3.12`) before the next "
                 "proxy relaunch."),
            fatal=False)
    return Check("sparkrun", True, detail=path)


def _sparkrun_cluster_ok(_runner=None) -> Check:
    # RULING (t_0d991e1d): REFUSE. Uninjected the row shells out to `sparkrun cluster
    # list --json`, i.e. the live fleet CLI and the operator's cluster config — the
    # exact thing hscc-cluster/tests/sparkrun_fence.py exists to stop in a test child,
    # except that fence cannot see across a spawn.
    refused = _base_row_fence(
        _runner, "sparkrun cluster", "cluster listing", "_cluster_runner",
        "Pass run_doctor(_cluster_runner=lambda: ...) built from fixture cluster "
        "state; a suite must never shell out to the live sparkrun CLI.")
    if refused is not None:
        return refused
    runner = _runner or _run_cluster_list
    raw = runner()
    if not raw:
        return Check("sparkrun cluster", False,
                     detail="no cluster configured / sparkrun not reachable",
                     fix="sparkrun cluster add <name> <host1> <host2> ...",
                     fatal=True)
    return Check("sparkrun cluster", True, detail="configured")


def _load_hermes_python():
    """Import ``hscc_daemon/hermes_python.py`` for the canonical root rule.

    Deferred + guarded, same pattern as ``ensure_review_feature._load_hermes_python``:
    the profiles-grandparent transform and its predicate live with the shared
    ladder (t_53f5b85a), so doctor cannot drift from backup_util/rolelib.
    Returns None when hscc_daemon is not deployed next to this plugin (a
    bootstrap-only install), in which case the caller applies the same rule.
    """
    try:
        from hscc_daemon import hermes_python
        return hermes_python
    except Exception:
        pass
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from hscc_daemon import hermes_python
        return hermes_python
    except Exception:
        return None


def hermes_root_from_home(hermes_home) -> Optional[str]:
    """Hermes ROOT for a home that may be PROFILE-scoped; None if no install.

    Hermes sets ``HERMES_HOME`` to the *profile* dir
    (``<root>/profiles/<name>``) whenever it runs under a profile — that is its
    normal run mode, not a misconfiguration (upstream
    ``hermes_constants.get_hermes_home`` resolves config/SOUL/skills relative to
    that profile dir, while ``get_default_hermes_root`` strips the
    ``profiles/<name>`` tail for root-level ops). So a profile-scoped home is
    legitimate and must be tolerated.

    Returns the root when ``<candidate>/hermes-agent`` exists, trying in order:
    the home itself, then the grandparent of a ``<root>/profiles/<name>`` path,
    then the platform default ``~/.hermes``. Returns None when none of them
    carries an install — i.e. Hermes really is absent. Same
    "parent dir is named ``profiles``" rule ``hscc-roles/rolelib.py`` already
    uses, so the fleet resolves homes consistently.
    """
    candidates: List[str] = []

    def _push(path):
        if not path:
            return
        path = os.path.expanduser(str(path))
        if path not in candidates:
            candidates.append(path)

    _push(hermes_home)
    home = os.path.expanduser(str(hermes_home)) if hermes_home else ""
    if home:
        hp = _load_hermes_python()
        if hp is not None:
            if hp.is_profile_dir(home):
                _push(hp.hermes_root(home))
        else:
            parent = os.path.dirname(home)
            if parent and os.path.basename(parent) == "profiles":
                _push(os.path.dirname(parent))
    _push(os.path.join(os.path.expanduser("~"), ".hermes"))

    for cand in candidates:
        if os.path.isdir(os.path.join(cand, "hermes-agent")):
            return cand
    return None


def hermes_install_dir(hermes_home=None) -> str:
    """Path of the ``hermes-agent`` checkout for an optionally profile-scoped home.

    Falls back to the first candidate path when Hermes is absent, so callers can
    report an honest "missing" detail naming where it was looked for.
    """
    root = hermes_root_from_home(hermes_home)
    if root:
        return os.path.join(root, "hermes-agent")
    home = hermes_home or os.environ.get("HERMES_HOME") \
        or os.path.join(os.path.expanduser("~"), ".hermes")
    return os.path.join(os.path.expanduser(str(home)), "hermes-agent")


def _skills_home(hermes_home) -> str:
    """Root Hermes home for skill scanning.

    A PROFILE-scoped ``HERMES_HOME`` (``<root>/profiles/<name>`` — the normal
    worker mode) is walked up ONE level to the root, because the fleet-wide
    audit must see every sibling profile. Any other home is scanned as-is.
    Deliberately NO fallback to ``~/.hermes`` (unlike
    ``hermes_root_from_home``): doctor may be pointed at a throwaway tree
    (``--home`` in tests and acceptance runs), and silently reading the
    operator's real home from a tmp run would both leak state into a "hermetic"
    verdict and report the wrong tree.
    """
    home = os.path.expanduser(str(hermes_home or "~/.hermes"))
    parent = os.path.dirname(home)
    if parent and os.path.basename(parent) == "profiles":
        return os.path.dirname(parent)
    return home


def _skill_findings(hermes_home) -> tuple[list, list]:
    """Pure scan: returns ``(dup_lines, reviewer_lines)`` for one Hermes root.

    Finding (i) — duplicate skill NAMES — runs over EVERY home (root + every
    named profile): a duplicated name is a resolver landmine whatever the
    profile. Finding (ii) — reviewer readiness — runs over the REVIEWER-ROLE
    SET only (``skill_guard.is_reviewer_capable``: the ``*-orch`` set + the
    reviewer role profile; the root home counts as ``default (root)``,
    which a spawn CAN target): the dispatcher force-injects ``sdlc-review``
    for review-lane spawns for ANY assignee, so every profile that can
    review must resolve it EXACTLY ONCE — using the FULL resolver rule,
    including the same-root rank gate (identical copies resolve only when
    their ranks differ; identical equal-rank copies refuse) and
    ``skills.disabled`` (a disabled name is MISSING for a preload spawn).
    A non-reviewer profile missing the skill is not listed at all — the card
    forbids nagging profiles that never review. A reviewer-capable home that
    carries ``.no-bundled-skills`` and cannot resolve the name is listed like
    any other broken home, annotated with the marker (never silent).

    Global-vs-profile duplication is deliberately NOT reported: a profile
    session scans ONLY ``<profile>/skills`` + that profile's
    ``skills.external_dirs`` (``tools/skills_tool._skill_search_dirs``); the
    root ``~/.hermes/skills`` is not a fallback root for a profile spawn
    (grounded in resolver source 2026-10-28), so a copy in one home can never
    collide with a same-named copy in another.
    """
    try:
        import skill_guard
    except ImportError:  # pragma: no cover - deployed tree always ships it
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import skill_guard

    home = Path(_skills_home(hermes_home))
    homes = [("default (root)", home)] + [(n, p) for n, p in
                                          skill_guard.discover_profiles(home)]
    dup_lines: list[str] = []
    reviewer_lines: list[str] = []
    for label, path in homes:
        roots = skill_guard.profile_skill_roots(path)
        dups: dict[str, list[str]] = {}
        for root in roots:
            for name, paths in skill_guard.duplicate_names(root).items():
                dups.setdefault(name, []).extend(paths)
        for name in sorted(dups):
            dup_lines.append(f"{label}: {name} -> " + "; ".join(dups[name]))
        reviewer = label == "default (root)" or skill_guard.is_reviewer_capable(label)
        if not reviewer:
            continue
        res = skill_guard.resolution(
            roots, skill_guard.REVIEW_SKILL,
            disabled=skill_guard.disabled_skills_for_home(path))
        # B3 transparency: an opted-out home reads RED like any other broken
        # reviewer home and the detail says the marker is present. For the
        # MISSING case the detail also says what hscc will do about it (the
        # installer overrides the marker for reviewer-capable homes, so a
        # bootstrap run closes it) — but NOT for collision/disabled, where the
        # guard deliberately writes nothing and only the operator can fix it.
        has_marker = skill_guard.bundled_seeding_opted_out(path)
        marker_note = (" (home carries .no-bundled-skills; hscc still seeds "
                       "this reviewer-capable home on bootstrap)"
                       if has_marker else "")
        marker_note_guarded = (" (home carries .no-bundled-skills)"
                               if has_marker else "")
        if res["state"] == "missing":
            reviewer_lines.append(
                f"{label}: missing {skill_guard.REVIEW_SKILL}{marker_note}")
        elif res["state"] == "disabled":
            reviewer_lines.append(
                f"{label}: {skill_guard.REVIEW_SKILL} present but listed in "
                f"skills.disabled — a preload spawn counts it missing"
                f"{marker_note_guarded}")
        elif res["state"] == "collision":
            rank_note = (" (identical copies at equal rank — hermes ranks "
                         "same-root copies only when their depths differ)"
                         if res.get("equal_rank") else "")
            reviewer_lines.append(
                f"{label}: {skill_guard.REVIEW_SKILL} collides "
                f"({res['count']} candidates) -> " + "; ".join(res["paths"])
                + rank_note + marker_note_guarded)
    return dup_lines, reviewer_lines


def _skills_duplicates_ok(hermes_home, *, _scan=None) -> "Check":
    """Doctor finding (i): no skill NAME resolves twice within any home.

    RULING (t_0d991e1d): the scan is ACCEPTED when the root is the caller's own tree
    (a fixture dir, ``--home PATH``) — reading it is what the row exists to do — and
    REFUSED when the root came from the environment, because ``_skills_home()`` walks a
    profile-scoped ``HERMES_HOME`` UP to the shared root and a suite child would then
    walk every profile of the operator's install. One seam (``_scan``) covers both skill
    rows, because they share the ONE scan.
    """
    if _scan is None and _root_is_ambient(hermes_home):
        refused = _base_row_fence(
            None, "skill names (duplicates)",
            "skill-tree scan of the ambient Hermes home", "_scan",
            "Pass _skills_duplicates_ok(home, _scan=lambda home: (dups, rev)) built "
            "from fixture trees, or point the door at --home <tmp>; a suite must "
            "never walk the operator's profile trees.")
        if refused is not None:
            return refused
    scan = _scan or _skill_findings
    try:
        dup_lines, _ = scan(hermes_home)
    except Exception as exc:  # noqa: BLE001 - doctor must never crash preflight
        return Check("skill names (duplicates)", True, fatal=False,
                     detail=f"scan skipped ({type(exc).__name__}: {exc})")
    if not dup_lines:
        return Check("skill names (duplicates)", True, fatal=False,
                     detail="no duplicated skill names")
    shown = dup_lines[:8]
    more = f"; ... (+{len(dup_lines) - len(shown)} more)" if len(dup_lines) > len(shown) else ""
    return Check(
        "skill names (duplicates)", False, fatal=False,
        detail="; ".join(shown) + more,
        fix="Two SKILL.md packages share a name — hermes refuses to guess and "
            "spawns die with 'Unknown skill(s)'. Move one aside yourself "
            "(hscc never deletes operator data); see "
            "hscc-bootstrap/install_review_skill.py --replace-top-level for "
            "the backed-up move of HSCC's own stale flat copies.")


def _reviewer_readiness_ok(hermes_home, *, _scan=None) -> "Check":
    """Doctor finding (ii): every reviewer-capable profile resolves
    ``sdlc-review`` exactly once (the skill the dispatcher force-injects).

    Same seam and same ruling as :func:`_skills_duplicates_ok` above — they are two
    renderings of one scan (t_0d991e1d).
    """
    if _scan is None and _root_is_ambient(hermes_home):
        refused = _base_row_fence(
            None, "reviewer readiness (sdlc-review)",
            "skill-tree scan of the ambient Hermes home", "_scan",
            "Pass _reviewer_readiness_ok(home, _scan=lambda home: (dups, rev)) built "
            "from fixture trees, or point the door at --home <tmp>; a suite must "
            "never walk the operator's profile trees.")
        if refused is not None:
            return refused
    scan = _scan or _skill_findings
    try:
        _, rev_lines = scan(hermes_home)
    except Exception as exc:  # noqa: BLE001 - doctor must never crash preflight
        return Check("reviewer readiness (sdlc-review)", True, fatal=False,
                     detail=f"scan skipped ({type(exc).__name__}: {exc})")
    if not rev_lines:
        return Check("reviewer readiness (sdlc-review)", True, fatal=False,
                     detail="every reviewer-capable profile resolves it once")
    shown = rev_lines[:6]
    more = f"; ... (+{len(rev_lines) - len(shown)} more)" if len(rev_lines) > len(shown) else ""
    return Check(
        "reviewer readiness (sdlc-review)", False, fatal=False,
        detail="; ".join(shown) + more,
        fix="A review-lane spawn to this profile dies with 'Unknown skill(s): "
            "sdlc-review' (dispatcher force-injection). Run "
            "hscc-bootstrap/install_review_skill.py --skill sdlc-review "
            "(guarded: installs only where missing, never creates collisions).")


def _hermes_ok(hermes_home: str) -> Check:
    """Hermes install check — fatal, and honest in BOTH directions.

    A profile-scoped ``HERMES_HOME`` must NOT be reported as a missing install
    (the checkout lives at the root, so the old code false-failed every worker
    run, which sets HERMES_HOME to a profile dir), but a machine with no
    ``hermes-agent`` anywhere still has to fail fatally.
    """
    root = hermes_root_from_home(hermes_home)
    if root is None:
        missing = os.path.join(os.path.expanduser(str(hermes_home)),
                               "hermes-agent") if hermes_home \
            else "~/.hermes/hermes-agent"
        return Check("hermes", False, detail=f"{missing} missing",
                     fix="Install Hermes (expected at ~/.hermes/hermes-agent).",
                     fatal=True)
    agent = os.path.join(root, "hermes-agent")
    detail = agent if root == os.path.expanduser(str(hermes_home)) \
        else f"{agent} (hermes root resolved from profile home {hermes_home})"
    return Check("hermes", True, detail=detail)


def _disk_ok(path: str, min_gb: float = 5.0) -> Check:
    try:
        usage = shutil.disk_usage(path)
        free_gb = usage.free / (1024 ** 3)
        ok = free_gb >= min_gb
        return Check("disk space", ok, detail=f"{free_gb:.1f} GB free",
                     fix=f"Free up disk — need ≥{min_gb} GB on {path}.",
                     fatal=False)
    except OSError as e:
        return Check("disk space", False, detail=str(e), fatal=False)


def _nas_ok(_runner=None, _cluster_raw=None) -> Check:
    """NAS reachability (non-fatal — NAS is optional but recommended for weight
    staging). Uses the sparkrun cluster's cache_dir + a ping to the NAS host if
    discoverable.

    ``_runner`` supplies the NAS identifier directly (legacy injection point).
    ``_cluster_raw`` supplies the raw `sparkrun cluster list` JSON instead, so a
    caller that already injects the cluster runner can stop that subprocess
    reaching the live fleet config — see ``run_doctor``.
    """
    # RULING (t_0d991e1d): REFUSE. Uninjected this row re-derives the NAS identifier
    # from `sparkrun cluster list` — the same live fleet CLI the cluster row refuses.
    refused = _base_row_fence(
        _runner if _runner is not None else _cluster_raw, "nas",
        "NAS lookup (sparkrun cluster list)", "_cluster_runner/_cluster_raw",
        "Pass run_doctor(_cluster_runner=...) (or _nas_ok(_cluster_raw=...)) with "
        "fixture cluster state; a suite must never read the live fleet config.")
    if refused is not None:
        return refused
    nas = _runner() if _runner else _detect_nas(_cluster_raw)
    if not nas:
        return Check("nas", True, detail="none configured (optional)", fatal=False)
    # nas may be a mount path (cache_dir) and/or an ip; just report it — a deep
    # mount probe needs ssh to a worker, which the live heal tools (nas_diagnose)
    # do. Doctor only flags presence here.
    return Check("nas", True, detail=str(nas), fatal=False)


def _detect_nas(_cluster_raw=None):
    """Best-effort NAS identifier from the sparkrun cluster (cache_dir).

    ``_cluster_raw`` is an injectable stand-in for `sparkrun cluster list`
    (returns the raw JSON string) so tests do not shell out to the live fleet.
    """
    raw = (_cluster_raw or _run_cluster_list)()
    if not raw:
        return None
    try:
        import json
        clusters = json.loads(raw)
        chosen = next((c for c in clusters if c.get("default")), clusters[0])
        return (chosen.get("cache_dir") or "").strip() or None
    except (ValueError, IndexError, KeyError):
        return None


# ── gateway liveness (GAP-F-3, t_f766ddd5; single impl t_c14d18cb) ──────────
# The transports and the trust order behind the "gateway" row live in
# ``gateway_liveness.py`` (sibling file, same plugin dir) — NOT here. This row
# only renders the verdict.
#
# WHY A SEPARATE FILE. The same verdict used to be computed twice in this repo:
# here, and in ``hscc_daemon/health.py::_gateway_job_alive`` for the daemon's
# gateway stream. #148 rewrote this copy after it was measured wrong; the
# daemon copy still carried ``pgrep -f "hermes.*gateway"``, which is BLIND to
# the gateway process on the operator machine (``pgrep -f .`` -> 1210 pids,
# ``ps -axww`` -> 1216; the gateway is in the second set only). Keeping two
# implementations of a liveness probe is how the second one stayed wrong after
# the first was fixed, so there is now one, and both consumers call it.
#
# The row stays non-fatal either way: bootstrap can legitimately run before the
# gateway is up.
try:
    import gateway_liveness as _gateway_liveness
except ImportError:  # pragma: no cover - import-path robustness only
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gateway_liveness as _gateway_liveness

# The two forwards the row's existing tests and sibling callers address
# directly. Everything else in gateway_liveness is reached through
# `_gateway_running` below — no alias is kept for a name nothing calls, so a
# removed helper cannot leave a dangling forward behind.
_argv_is_gateway = _gateway_liveness.argv_is_gateway
_gateway_argv_pid = _gateway_liveness.gateway_argv_pid


def _gateway_running(*, _run=None, _ps_lines=None) -> Check:
    """The "gateway" row. All probe logic lives in ``gateway_liveness``.

    ``_run`` injects the probe runner (``fn(argv, timeout) -> CompletedProcess``)
    and ``_ps_lines`` injects raw ``ps`` output, so tests never touch the live
    process table. Informational either way — bootstrap can legitimately run
    before the gateway is up — so the row stays non-fatal.

    Trust order (see the module note above and gateway_liveness' docstring):
    the supervisor the gateway is installed under first, then a token scan of
    ``ps`` argv for when no supervisor can answer. pgrep is never invoked — it
    cannot see this process, which is the whole GAP-F-3 failure.
    """
    # RULING (t_0d991e1d): REFUSE — this is the row the card measured. Uninjected it
    # asks the SERVICE MANAGER and the process table (launchctl list / systemctl
    # is-active / ps -axww) and prints what it gets back: from a suite-spawned door
    # that is the operator's live gateway pid inside a test payload.
    refused = _base_row_fence(
        _run if _run is not None else _ps_lines, "gateway",
        "gateway liveness probe (launchctl/systemctl/ps)", "_run/_ps_lines",
        "Pass _gateway_running(_run=...) or (_ps_lines=...) built from fixture probe "
        "output; a suite must never ask the machine's service manager.")
    if refused is not None:
        return refused
    alive, detail, fix = _gateway_liveness.gateway_verdict(
        _run=_run, _ps_lines=_ps_lines)
    return Check("gateway", alive, detail=detail, fix=fix, fatal=False)


def _models_url(base_url: str) -> str:
    """Normalize an endpoint base_url to an OpenAI-style ``/models`` probe URL.

    The version path is PRESERVED: an OpenAI-compatible server serves the
    model list at ``{base_url}/models`` (e.g. ``http://host:port/v1/models``),
    NOT at a version-stripped root (``http://host:port/models``). So we append
    ``/models`` to the base_url as configured, never strip ``/v1``.

    ``http://host:port/v1``   -> ``http://host:port/v1/models``
    ``http://host:port/v1/``  -> ``http://host:port/v1/models``
    ``http://host:port``      -> ``http://host:port/models``
    Returns "" for missing/blank input.
    """
    url = (base_url or "").strip()
    if not url:
        return ""
    url = url.rstrip("/")
    # If the configured base_url carries a /v1 version prefix, keep (exactly)
    # one of them and append /models to it — do NOT strip the version path.
    if "/v1" in url:
        head = url.split("/v1", 1)[0].rstrip("/")
        return head + "/v1/models"
    return url + "/models"


# ── BEGIN mirrored block: lan_curl_fallback (t_391b084e) ──────────────────
# macOS binds Privacy > Local Network to the *signing identity* of the
# executable, so an adhoc-signed interpreter — a Homebrew keg's Python.app, a
# python.org Framework build — is denied on EVERY private-range connect, no
# matter how often the Python entries are re-toggled. urllib then reports
# `OSError(65, 'No route to host')` (EHOSTUNREACH) for a host that is up and
# answering, while Apple-signed /usr/bin/curl — which keeps its own TCC grant
# — reaches the same URL fine. That is what made `hscc cluster monitor --live`
# render every backend `down` on a healthy fleet (host stats survived only
# because they ride ssh). Do NOT try to fix this with TCC/system changes: it is
# an OS identity rule, not our bug. The workaround is to retry exactly that one
# failure through curl.
#
# THREE PLUGINS CARRY THIS BLOCK VERBATIM (hscc_daemon/cluster_live.py,
# hscc-bootstrap/doctor.py, hscc-cluster/cluster_template.py). It is NOT a
# cross-plugin import on purpose: each plugin deploys standalone into
# ~/.hermes/plugins and the hyphenated dirs are not importable package names,
# so none of them can import another. The block is pinned byte-identical by
# hscc-cluster/tests/test_lan_curl_fallback.py::test_mirrored_block_is_identical
# — edit all three copies in the same commit, or that test fails.
#
# Deliberately narrow. The fallback fires ONLY when urllib reports errno 65
# SPECIFICALLY on a PRIVATE host:
#   * Linux EHOSTUNREACH is 113 and means there genuinely is no route, so it
#     must NOT be retried by anything — a "helpful" fallback there would hide a
#     dead node behind a second probe.
#   * A public host is not the Local Network story at all (TCC gates only local
#     traffic), so falling back there would paper over a real firewall or DNS
#     problem.
#   * Loopback never falls back: TCC does not gate localhost, so errno 65
#     there is not this bug.
# HTTPError, timeouts and every other errno propagate UNCHANGED, so callers
# keep their existing 'probe error' vs 'unreachable' distinction.
import ipaddress
import queue
import urllib.error
import urllib.parse

LAN_CURL_FALLBACK_NOTICE = "LAN via curl fallback (Local Network permission)"

_CURL_BIN = "/usr/bin/curl"
_CURL_UNUSABLE_HINT = (" — /usr/bin/curl fallback unavailable: restore it, or grant "
                       "macOS Local Network to this interpreter")

# One-shot ticket: filled at import, consumed by the first successful fallback.
# Consume-once is the whole point — `cluster monitor --live` re-renders every
# tick and the daemon loop ticks forever, so the notice must not repeat. A
# maxsize-1 Queue rather than a bool because the monitor scrapes backends from
# a thread pool, where a read-then-write flag would let two workers announce.
_CURL_FALLBACK_TICKET: "queue.Queue" = queue.Queue(maxsize=1)
_CURL_FALLBACK_TICKET.put_nowait(LAN_CURL_FALLBACK_NOTICE)

# 100.64.0.0/10 is CGNAT — where Tailscale hands out addresses — and Python
# does NOT classify it private, so it is named explicitly. fe80::/10 and
# fc00::/7 cover IPv6 link-local and ULA.
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_ULA_NET = ipaddress.ip_network("fc00::/7")


def _lan_private_host(host) -> bool:
    """True for a private/LAN host the Local Network gate can deny.

    RFC1918, 100.64.0.0/10 (tailnet CGNAT), 169.254.0.0/16 + fe80::/10
    link-local, fc00::/7 ULA, and ``.local`` mDNS names. Loopback — numeric or
    ``localhost`` — is False: TCC does not gate it, so errno 65 there is not
    this bug. A name that is not an IP is False unless it is ``.local``, so a
    public hostname can never reach the fallback.
    """
    h = (host or "").strip().rstrip(".").lower()
    if not h or h == "localhost":
        return False
    if h.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    if ip.is_loopback:
        return False
    # Family guard: `IPv4Address in <IPv6Network>` raises TypeError, so a
    # membership test must only run against the network of the same family.
    if ip.version == 4:
        return bool(ip.is_private or ip.is_link_local or ip in _CGNAT_NET)
    return bool(ip.is_private or ip.is_link_local or ip in _ULA_NET)


def _lan_errno(exc):
    """The errno behind a urllib failure, or None when it carries none.

    ``URLError.reason`` is the OSError raised by the connect. ``HTTPError`` is
    a URLError subclass whose reason is a *status string*, so it lands here as
    None — a status is never a connectivity denial, and never a fallback.
    """
    return getattr(getattr(exc, "reason", None), "errno", None)


def _lan_curl_notice_once() -> None:
    """Print the fallback notice the first time only, then stay quiet.

    stderr, not stdout: the two probe mirrors print structured output on stdout
    (doctor's checklist, template's ``--json``) and must not be polluted, and
    the daemon log picks stderr up — one line, then silence.
    """
    import sys
    try:
        _CURL_FALLBACK_TICKET.get_nowait()
    except queue.Empty:
        return
    print(LAN_CURL_FALLBACK_NOTICE, file=sys.stderr, flush=True)


def _lan_curl_reset_notice() -> None:
    """Refill the one-shot notice ticket.

    Test seam only — no runtime path calls it. A test that asserts 'the notice
    appears exactly once across N fallbacks' has to start from a known state,
    and the suite raises far more than one fallback per process, so without
    this the assertion would silently depend on test order.
    """
    while not _CURL_FALLBACK_TICKET.empty():
        _CURL_FALLBACK_TICKET.get_nowait()
    _CURL_FALLBACK_TICKET.put_nowait(LAN_CURL_FALLBACK_NOTICE)


def _lan_curl_run(argv, timeout):
    """Run one curl argv LIST (never a shell) -> (returncode, stdout, stderr).

    Injectable seam: the suite replaces this module attribute with a stub that
    inspects argv — which is also how 'the api_key never reaches argv' is
    proven, since every test asserts on the argv the stub was handed.
    """
    import subprocess
    p = subprocess.run(argv, capture_output=True, timeout=timeout + 10)
    return p.returncode, p.stdout, p.stderr


def _lan_curl_unusable(original, cause) -> Exception:
    """What the caller sees when curl could not be run at all.

    Same ``URLError`` family the caller already handles, and its text still
    carries the ORIGINAL errno-65 message — the hint is appended, never
    substituted, so a reader still learns the endpoint was denied.
    """
    return urllib.error.URLError(f"{original}{_CURL_UNUSABLE_HINT} [{cause}]")


def _lan_curl_argv(url, timeout, header_file=None) -> list:
    """The curl argv.

    ``-sS`` quiet but loud on error, ``--fail`` so a 4xx/5xx becomes exit 22
    (urllib's HTTPError equivalent), ``-L`` because urllib follows redirects
    too, ``--max-time`` so a hung node cannot hang the monitor. The URL is
    always the LAST token, and no token ever holds a secret.
    """
    argv = [_CURL_BIN, "-sS", "--fail", "-L", "--max-time", str(timeout)]
    if header_file:
        argv += ["-H", f"@{header_file}"]
    argv.append(url)
    return argv


def _lan_curl_write_header(api_key) -> str:
    """Write the Bearer header to a 0600 temp file and return its path.

    The api_key must NEVER be an argv token: ``ps`` shows every argument of a
    child process for the life of the request, so ``-H "Authorization: Bearer
    <key>"`` would leak the key to anyone on the box. curl reads the header
    from the file instead (``-H @path``), and the caller unlinks it.
    """
    import os
    import tempfile
    fd, path = tempfile.mkstemp(prefix="hscc-lan-hdr-")
    try:
        os.fchmod(fd, 0o600)  # mkstemp is 0600 already; pin it explicitly
        with os.fdopen(fd, "w") as fh:
            fh.write(f"Authorization: Bearer {api_key}\n")
    except OSError:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def _lan_curl_http_error(url, stderr) -> Exception:
    """Map curl's exit-22 back onto urllib's HTTPError family.

    Parity matters more than the number: callers branch on HTTPError to say
    'endpoint reachable, probe/auth wrong' as opposed to 'unreachable', so the
    fallback raises the same family urllib would, carrying the status curl
    reported. ``code`` is 0 when curl did not name one — the exception TYPE is
    what callers branch on.
    """
    import re
    m = re.search(r"returned error: (\d{3})",
                  (stderr or b"").decode("utf-8", "replace"))
    code = int(m.group(1)) if m else 0
    reason = f"curl fallback: HTTP {code}" if code else "curl fallback: HTTP error"
    return urllib.error.HTTPError(url, code, reason, None, None)  # type: ignore[arg-type]


def _lan_curl_fallback(url, timeout, exc, api_key=None):
    """Retry one denied GET through /usr/bin/curl -> the body bytes, or None.

    Returns the response body when the retry succeeded; None means 'not
    eligible, or curl could not reach it either' and the caller MUST re-raise
    the original error unchanged. ``api_key`` is only ever sent as a header
    from a 0600 temp file — never in argv, never in a message.
    """
    import os
    if _lan_errno(exc) != 65:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    if not _lan_private_host(parsed.hostname):
        return None
    header_file = None
    try:
        if api_key:
            header_file = _lan_curl_write_header(api_key)
        rc, out, err = _lan_curl_run(_lan_curl_argv(url, timeout, header_file),
                                     timeout)
    except Exception as cause:      # curl missing / not executable / timed out
        raise _lan_curl_unusable(exc, cause) from exc
    finally:
        if header_file:
            try:
                os.unlink(header_file)
            except OSError:
                pass
    if rc == 22:
        raise _lan_curl_http_error(url, err) from exc
    if rc != 0:
        # curl could not reach it either — it really is down. Let the original
        # errno-65 error stand with the caller's own wording.
        return None
    _lan_curl_notice_once()
    return out
# ── END mirrored block: lan_curl_fallback ──────────────────────────────────

def _http_get_default(url: str, api_key: str | None = None) -> str:
    """Fetch ``url`` and return its body as text.

    Sends ``Authorization: Bearer <api_key>`` when a key is provided (the
    configs carry api_key next to base_url; an endpoint that requires a key
    returns 401 without it). Raises ``urllib.error.HTTPError`` on a non-2xx
    HTTP status (endpoint reachable but the path/auth is wrong) and
    ``urllib.error.URLError``/other for network failures, so callers can tell
    a probe/config error apart from an unreachable endpoint.
    """
    import urllib.error
    import urllib.request
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.URLError as e:
        # A macOS Local-Network denial (errno 65 on a private host) retries
        # once through /usr/bin/curl; everything else propagates unchanged, so
        # callers still tell a probe error apart from an unreachable endpoint.
        body = _lan_curl_fallback(url, 10, e, api_key=api_key)
        if body is None:
            raise
        return body.decode("utf-8")


def _collect_model_entries(cfg: dict) -> list:
    """Return ``(model_id, base_url, api_key, config_key)`` for every model.

    Sources: top-level ``model``, ``delegation``, each ``fallback_providers[]``,
    and every ``auxiliary.*`` entry. Entries missing a model id or base_url are
    skipped (no probe URL means nothing to verify). ``api_key`` is carried from
    the same config entry so the probe can authenticate.
    """
    entries = []

    def add(model, base_url, api_key, key):
        if model and isinstance(model, str) and model.strip():
            entries.append((model.strip(), (base_url or "").strip(),
                            (api_key or "").strip() or None, key))

    top = cfg.get("model")
    if isinstance(top, dict):
        add(top.get("default"), top.get("base_url"), top.get("api_key"),
            "model.default")

    dlg = cfg.get("delegation")
    if isinstance(dlg, dict):
        add(dlg.get("model"), dlg.get("base_url"), dlg.get("api_key"),
            "delegation.model")

    for i, fp in enumerate(cfg.get("fallback_providers") or []):
        if isinstance(fp, dict):
            add(fp.get("model"), fp.get("base_url"), fp.get("api_key"),
                f"fallback_providers[{i}].model")

    aux = cfg.get("auxiliary")
    if isinstance(aux, dict):
        for task, ent in aux.items():
            if isinstance(ent, dict):
                add(ent.get("model"), ent.get("base_url"), ent.get("api_key"),
                    f"auxiliary.{task}.model")

    return entries


def _check_models_served(hermes_home=None, *, _http_get=None) -> Check:
    """Verify every configured model id is actually served by its endpoint.

    Non-fatal (warning): a stale model id is drift to fix (every call 404s),
    not a reason to hard-stop bootstrap.

    Three distinct outcomes are reported (never collapsed):
      - endpoint UNREACHABLE (network/timeout) -> ok (explain + skip), because
        we can't verify anything and it must not false-alarm;
      - endpoint reachable but the ``/models`` path returns 404/401 (or an
        unparsable body) -> PROBE/CONFIG ERROR, reported loudly as a real
        problem rather than a non-event;
      - a model id absent from a successfully-parsed served list -> the
        mismatch, which names the config key + endpoint + served ids.

    ``_http_get(url, api_key=None) -> str`` receives the RESOLVED probe URL
    (e.g. ``http://host:port/v1/models``) plus the entry's api_key for auth and
    returns the response body text; injectable for tests (mirrors how
    ``_cluster_runner`` is injected elsewhere).
    """
    import json
    from urllib.error import HTTPError

    home = (hermes_home or os.environ.get("HERMES_HOME")
            or os.path.expanduser("~/.hermes"))
    config_path = os.path.join(home, "config.yaml")

    try:
        import yaml
        with open(config_path) as fh:
            cfg = yaml.safe_load(fh) or {}
    except Exception as exc:
        return Check("models served", True,
                     detail=f"config unreadable ({exc}); skipped", fatal=False)

    if not isinstance(cfg, dict):
        return Check("models served", True,
                     detail="config has no model entries; skipped", fatal=False)

    entries = _collect_model_entries(cfg)
    if not entries:
        return Check("models served", True,
                     detail="no configured model ids; nothing to verify",
                     fatal=False)

    # RULING (t_0d991e1d): REFUSE. Reading the caller's config.yaml is fine; the row
    # then opens a SOCKET to every endpoint in it (and through the curl fallback, execs
    # a binary) — from a suite child that is a live probe of the operator's serving
    # endpoints. Without a getter there is no answer to give, so the row says so.
    refused = _base_row_fence(
        _http_get, "models served", "endpoint /models probe", "_http_get",
        "Pass run_doctor(_http_get=lambda url, api_key=None: ...) returning fixture "
        "body text; a suite must never open a socket to a configured endpoint.")
    if refused is not None:
        return refused
    getter = _http_get or _http_get_default

    # Group by resolved probe URL, deduping duplicate endpoints and keeping
    # first-seen order so detail output is stable. The probe api_key is taken
    # from the first entry at that endpoint that carries one.
    by_url = {}
    for model_id, base_url, api_key, key in entries:
        url = _models_url(base_url)
        if not url:
            continue
        group = by_url.setdefault(url, [])
        if not any(entry[2] for entry in group) and api_key:
            group.insert(0, (model_id, key, api_key))
            continue
        group.append((model_id, key, None))

    if not by_url:
        return Check("models served", True,
                     detail="no model endpoints found; nothing to verify",
                     fatal=False)

    unreachable = []
    probe_errors = []
    problems = []
    checked = []
    for url, pairs in by_url.items():
        api_key = next((k for _, _, k in pairs if k), None)
        try:
            raw = getter(url, api_key=api_key)
        except HTTPError as exc:
            # Endpoint reachable but the /models path/auth is wrong -> a real
            # config/probe problem, report it loudly (not a non-event).
            probe_errors.append(
                f"{url} returned HTTP {exc.code} on the /models probe "
                f"(endpoint reachable; wrong path or missing/bad auth key)")
            continue
        except Exception as exc:
            # Network/timeout -> can't verify, explain and skip, no false alarm.
            unreachable.append(f"{url} unreachable ({exc}); skipped")
            continue
        try:
            data = json.loads(raw)
            served = [str(m.get("id")) for m in (data.get("data") or [])
                      if isinstance(m, dict) and m.get("id")]
        except Exception:
            probe_errors.append(
                f"{url} returned an unparsable body on the /models probe; skipped")
            continue

        served_line = ", ".join(served) if served else "(none)"
        served_set = set(served)
        for model_id, key, _ in pairs:
            checked.append(f"{key} -> {model_id} @ {url}")
            if model_id not in served_set:
                problems.append(
                    f"{key} ('{model_id}') not served at {url}; "
                    f"served: {served_line}")

    fail_bits = []
    fail_bits += problems
    # Probe/config errors are real problems too — report them alongside
    # mismatches so the operator sees an unhealthy endpoint, not silence.
    fail_bits += probe_errors

    if fail_bits:
        fix = ("Set each misconfigured model id to one already served by its "
               "endpoint (see detail), or re-run apply_template to regenerate "
               "the config from current templates. If an endpoint 404s on its "
               "/models path, fix the base_url (or add the correct auth key).")
        return Check("models served", False,
                     detail="; ".join(fail_bits), fix=fix, fatal=False)

    if unreachable and not checked:
        return Check("models served", True,
                     detail="; ".join(unreachable) + " — nothing verified",
                     fatal=False)

    bits = checked + unreachable
    if not bits:
        return Check("models served", True,
                     detail="no endpoints probed", fatal=False)

    return Check("models served", True,
                 detail="; ".join(bits) + " — all configured models served",
                 fatal=False)


def _run_cluster_list() -> str:
    try:
        r = subprocess.run(["sparkrun", "cluster", "list", "--json"],
                           capture_output=True, text=True, timeout=15)
        out = (r.stdout or "").strip()
        return out if (r.returncode == 0 and out and out != "[]") else ""
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""


# ── snap firmware-notifier mask check (card t_61850b24) ────────────────────
# Name is defined here (not read from the module) so the "check missing"
# rendering still names the check when the module itself is absent.
# Hosts come from the ONE cluster reader `_node_hosts()` (the sibling
# node-recovery check uses the same); this card deliberately does not add a
# second one.
_NOTIFIER_CHECK_NAME = "snap firmware-notifier mask (node-recovery)"


# ── proxy-outage 2026-10-11 report-only checks (t_proxy_outage_20261011) ────
#
# Three fleet-state checks the outage proved worth having BEFORE the next
# outage: (4) sparkrun's own interpreter can be denied macOS Local Network
# (errno 65 on every private-range connect while nc/ssh work fine); (5) the
# saved sparkrun cluster can drift from serving.json (it lacked the
# orchestrator node during the outage); (6) duplicate sparkrun jobs on one
# node+port (the intent-hash duplicate class) and stale serving.json endpoint
# records. All three are warnings — report only, never fix, never fatal.


def _node_hosts(_cluster_raw=None) -> List[str]:
    """Every host of the default sparkrun cluster (for the node-recovery check).

    Reuses the ONE cluster-list runner in this module; ``_cluster_raw`` is the
    same injectable stand-in the NAS check uses, so the suite never shells out
    to the live fleet. Unresolvable is ``[]`` — the node-recovery check then
    renders an honest ERROR, never a vacuous pass.
    """
    import json
    raw = (_cluster_raw or _run_cluster_list)()
    if not raw:
        return []
    try:
        clusters = json.loads(raw)
        chosen = next((c for c in clusters if c.get("default")),
                      (clusters[0] if clusters else {}))
        return [h for h in (chosen.get("hosts") or []) if h]
    except (ValueError, IndexError, KeyError, AttributeError):
        return []


def _serving_units(_serving_raw=None):
    """Units list from serving.json (injectable raw text for tests)."""
    import json
    raw = _serving_raw
    if raw is None:
        path = os.path.expanduser(os.environ.get("HSCC_SERVING_JSON",
                                                 "~/.hscc/serving.json"))
        try:
            with open(path) as fh:
                raw = fh.read()
        except OSError:
            return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    units = data.get("units") if isinstance(data, dict) else None
    return [u for u in (units or []) if isinstance(u, dict)]


def _sparkrun_interpreter():
    """(cli_path, interpreter) for the sparkrun CLI: which → ~/.local/bin,
    then read the shebang. (None, None) when the CLI cannot be found.

    The 2026-10-11 incident class: sparkrun installed under a Homebrew or
    python.org Framework python — a binary whose TCC identity may lack the
    macOS Local Network grant, so EVERY private-range connect from it fails
    errno 65 while ssh/nc from other binaries work.
    """
    cli = shutil.which("sparkrun") or os.path.expanduser("~/.local/bin/sparkrun")
    if not os.path.isfile(cli) or not os.access(cli, os.X_OK):
        return None, None
    try:
        with open(cli, "rb") as fh:
            first = fh.readline(256)
    except OSError:
        return cli, None
    if not first.startswith(b"#!"):
        return cli, None
    shebang = first[2:].decode("utf-8", "replace").strip()
    # "#!/usr/bin/env python3" style → the interpreter is resolved at exec;
    # take the literal path when absolute, else None (env/relative).
    parts = shebang.split()
    interp = parts[0] if parts else ""
    if interp.startswith("/"):
        return cli, interp
    if parts and parts[0].endswith("/env") and len(parts) > 1:
        return cli, shutil.which(parts[1])  # best effort via PATH
    return cli, None


_INTERP_DENY_HINT = ("macOS Local Network permission blocks this Python — "
                    "rebuild sparkrun on a uv Python")
_FRAMEWORK_HINT = ("sparkrun runs on a Homebrew/Framework python — its TCC "
                   "identity may lack the Local Network grant; a uv-built "
                   "sparkrun is the supported install")


def _sparkrun_lan_reachability(_venv_python=None, _probe=None,
                               _serving_raw=None) -> Check:
    """TCP-probe every serving node FROM SPARKRUN'S OWN INTERPRETER (report only).

    A python with no Local Network grant answers errno 65 for every
    private-range connect, so the outage looked like "fleet down" while the
    fleet was fine — the ONLY probe that catches it is one that runs under
    sparkrun's interpreter, not doctor's own. Connect target is port 22:
    what sparkrun itself must reach.
    """
    cli, interp = (None, None)
    if _venv_python is None:
        cli, interp = _sparkrun_interpreter()
    else:
        interp = _venv_python
    if not interp:
        return Check("sparkrun lan reachability", True,
                     detail="sparkrun interpreter unresolved — skipped",
                     fatal=False)

    nodes = []
    for u in _serving_units(_serving_raw):
        for n in (u.get("nodes") or []):
            if n and n not in nodes:
                nodes.append(n)
    if not nodes:
        return Check("sparkrun lan reachability", True,
                     detail="no serving nodes to probe — skipped", fatal=False)

    deny_detected = False
    failures = []
    for node in nodes:
        if _probe is not None:
            err = _probe(interp, node)
        else:
            err = _tcp_probe_error(interp, node, 22)
        if err is None:
            continue
        failures.append(f"{node}: {err}")
        if "errno 65" in err or "No route to host" in err:
            deny_detected = True

    detail_bits = []
    is_homebrew = interp.startswith("/opt/homebrew/") or interp.startswith(
        "/usr/local/Cellar/") or interp.startswith("/Library/Frameworks/")
    if is_homebrew:
        detail_bits.append(_FRAMEWORK_HINT)
    if deny_detected:
        return Check("sparkrun lan reachability", False,
                     detail="; ".join(failures) + " — " + _INTERP_DENY_HINT,
                     fix=_INTERP_DENY_HINT + " (uv venv), re-grant Local "
                         "Network to it in System Settings > Privacy.",
                     fatal=False)
    if failures:
        return Check("sparkrun lan reachability", False,
                     detail="; ".join(failures)
                     + ("" if not detail_bits else " — " + "; ".join(detail_bits)),
                     fix="Check node reachability / ssh.", fatal=False)
    if detail_bits:
        return Check("sparkrun lan reachability", True,
                     detail=f"all {len(nodes)} serving node(s) reachable; "
                            + "; ".join(detail_bits), fatal=False)
    return Check("sparkrun lan reachability", True,
                 detail=f"all {len(nodes)} serving node(s) reachable from "
                        f"{os.path.basename(interp)}", fatal=False)


def _tcp_probe_error(interpreter, host, port, timeout=5):
    """None when <interpreter> can TCP-connect host:port; else the error text.

    Runs the connect INSIDE the given interpreter so the verdict belongs to
    that binary's TCC identity — the entire point (doctor's own python having
    the grant says nothing about sparkrun's).
    """
    script = (
        "import socket,sys\n"
        "try:\n"
        "    s=socket.create_connection((sys.argv[1],int(sys.argv[2])),5)\n"
        "    s.close()\n"
        "except OSError as e:\n"
        "    print(f'errno={e.errno} {e.strerror}')\n"
        "    sys.exit(1)\n"
    )
    try:
        r = subprocess.run([interpreter, "-c", script, host, str(port)],
                           capture_output=True, text=True, timeout=timeout + 5)
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        return f"probe could not run: {e}"
    if r.returncode == 0:
        return None
    return (r.stdout or r.stderr or "connect failed").strip()


def _run_cluster_default() -> str:
    try:
        r = subprocess.run(["sparkrun", "cluster", "default", "--json"],
                           capture_output=True, text=True, timeout=15)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""


def _sparkrun_default_cluster_matches_serving(_runner=None,
                                              _serving_raw=None) -> Check:
    """Warn when `sparkrun cluster default` hosts differ from serving.json nodes.

    During the outage the saved cluster LACKED the orchestrator node, so any
    sparkrun operation scoped to the cluster (including the proxy relaunch)
    silently excluded it. Report only — re-writing a saved cluster is an
    operator decision.
    """
    import json
    raw = (_runner or _run_cluster_default)()
    if not raw:
        return Check("sparkrun cluster vs serving", True,
                     detail="no default cluster / sparkrun not reachable — skipped",
                     fatal=False)
    try:
        data = json.loads(raw)
    except ValueError:
        return Check("sparkrun cluster vs serving", True,
                     detail="unparseable cluster default output — skipped",
                     fatal=False)
    if not isinstance(data, dict):
        # ``sparkrun cluster default --json`` prints the literal ``null`` on a host
        # with no default cluster — measured under the roster-wide HOME seal (empty
        # HOME, so the real sparkrun resolves no ``~/.config/sparkrun``; that posture
        # is what a CI runner has too). ``json.loads`` ACCEPTS ``null``, so the only
        # thing between that host and a traceback was this branch: without it the
        # mapping access below crashed doctor's preflight and the whole installer
        # died with it — 4 isolated-install cases in hscc-bootstrap went from skip
        # (blind ~ gate) to RED (honest gate) the moment the leg grew a real-tree
        # seam (t_5b560adf). A document that is not a mapping carries no hosts, so
        # it is the same "nothing to compare" answer as an empty cluster, and
        # report-only like every other branch in this check.
        return Check("sparkrun cluster vs serving", True,
                     detail=f"cluster default returned {type(data).__name__}, "
                            "not a mapping — skipped",
                     fatal=False)
    cluster_hosts = {h for h in (data.get("hosts") or []) if h}
    serving_nodes = set()
    for u in _serving_units(_serving_raw):
        serving_nodes.update(n for n in (u.get("nodes") or []) if n)
    if not cluster_hosts or not serving_nodes:
        return Check("sparkrun cluster vs serving", True,
                     detail="nothing to compare — skipped", fatal=False)
    missing_in_cluster = serving_nodes - cluster_hosts
    extra_in_cluster = cluster_hosts - serving_nodes
    if not missing_in_cluster and not extra_in_cluster:
        return Check("sparkrun cluster vs serving", True,
                     detail=f"cluster hosts == serving nodes ({len(cluster_hosts)})",
                     fatal=False)
    bits = []
    if missing_in_cluster:
        bits.append(f"serving nodes MISSING from sparkrun cluster: "
                    f"{sorted(missing_in_cluster)}")
    if extra_in_cluster:
        bits.append(f"cluster hosts not in serving.json: {sorted(extra_in_cluster)}")
    return Check("sparkrun cluster vs serving", False,
                 detail="; ".join(bits),
                 fix="sparkrun cluster update <name> --hosts <full serving set>",
                 fatal=False)


def _run_cluster_status(cluster) -> str:
    if not cluster:
        return ""
    try:
        r = subprocess.run(["sparkrun", "status", "--cluster", cluster, "--json"],
                           capture_output=True, text=True, timeout=30)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""


def _serving_consistency(_status_runner=None, _serving_raw=None,
                         cluster=None) -> Check:
    """Flag duplicate sparkrun jobs on one node+port + stale endpoint records.

    The 2026-10-11 duplicate class: force-recreates with a changed
    --served-model-name carry a different sparkrun intent hash, so the OLD
    container is left running beside the NEW one on the same node+port. One
    of the pair maps to serving.json's serve_cmd / (node,port); the unmapped
    extra is the duplicate. The inverse — a serving.json (node,port) with no
    running container — is a stale record: something the fleet believes is
    served but sparkrun cannot see. Report only.
    """
    import json
    if cluster is None:
        cluster = os.environ.get("HSCC_CLUSTER", "hscc")
    raw = (_status_runner or _run_cluster_status)(cluster)
    units = _serving_units(_serving_raw)
    if not raw or not units:
        return Check("serving consistency", True,
                     detail="no status data or no serving units — skipped",
                     fatal=False)
    try:
        data = json.loads(raw)
    except ValueError:
        return Check("serving consistency", True,
                     detail="unparseable sparkrun status JSON — skipped",
                     fatal=False)

    # (host, port) -> [container entries] from groups + solo_entries
    running = {}
    for _cid, group in (data.get("groups") or {}).items():
        meta = group.get("meta") or {}
        port = meta.get("port")
        for c in group.get("containers") or []:
            host = c.get("host")
            if host and port is not None:
                running.setdefault((host, int(port)), []).append(c)
    for entry in data.get("solo_entries") or []:
        meta = entry.get("meta") or {}
        host, port = entry.get("host"), meta.get("port")
        if host and port is not None:
            running.setdefault((host, int(port)), []).append(entry)

    # serving.json's authoritative (host, port) set: every unit's PRIMARY
    # (nodes[0]) on the unit's port; tp peers expose no endpoint of their own.
    mapped = set()
    for u in units:
        nodes = [n for n in (u.get("nodes") or []) if n]
        port = u.get("port")
        if nodes and port is not None:
            mapped.add((nodes[0], int(port)))

    problems = []
    for (host, port), containers in sorted(running.items()):
        if len(containers) > 1 and (host, port) in mapped:
            problems.append(
                f"{host}:{port} has {len(containers)} sparkrun containers; "
                f"serving.json maps only one — the extra is the duplicate class "
                f"(intent-hash drift on a served-name change), stop the unmapped one")
    for (host, port) in sorted(mapped):
        if (host, port) not in running:
            problems.append(
                f"serving.json record {host}:{port} has no running container "
                f"(stale endpoint record)")
    if problems:
        return Check("serving consistency", False,
                     detail="; ".join(problems),
                     fix="Report only — verify each duplicate/stale pair with "
                         "`sparkrun status --cluster "
                         f"{cluster}` before stopping anything.",
                     fatal=False)
    return Check("serving consistency", True,
                 detail=f"{len(mapped)} serving.json endpoint(s) == sparkrun "
                        f"containers, no duplicates", fatal=False)


def _refused_role_soul_scan() -> dict:
    """In-suite stand-in for the role-SOUL scan when no seam was injected.

    The check's real target is SOUL.md under rolelib.PROFILES_DIR — the operator's
    LIVE profile root when the suite runs from a dispatched worker profile. Same
    fence shape as ``_refused_transport`` above: the check comes back an ERROR
    naming the missing seam, never a silent pass and never a real read.
    """
    return {"name": doctor_role_soul.check_name(), "ok": False, "fatal": False,
            "rows": [], "scanned": 0,
            "detail": "role-SOUL scan refused inside a pytest process — inject "
                      "_role_soul_scan (fixture profile dirs)",
            "fix": "Pass _role_soul_scan=lambda: {...} built from tmp profile "
                   "fixtures; a suite must never read the operator's profiles."}


def _load_orch_waker():
    """Import ``hscc_daemon/orch_waker.py`` for the orch-waker doctor check.

    The check's logic lives with the feature (its config loader, its stream
    name, its freshness budget) so the doctor cannot drift from what the
    running daemon means by "enabled" or "last tick"; doctor.py only adapts the
    result dict to its five Check fields, exactly as it does for
    ``doctor_node_recovery`` / ``doctor_notifier_mask``. Returns the module, or
    None when the daemon plugin is not deployed next to this one (the check
    then reports itself unverifiable rather than vanishing).
    """
    try:
        from hscc_daemon import orch_waker
        return orch_waker
    except Exception:
        pass
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from hscc_daemon import orch_waker
        return orch_waker
    except Exception:
        return None


#: Fallback for the check name when the module itself cannot be imported —
#: same trick as ``_NOTIFIER_CHECK_NAME`` so the row still prints.
_ORCH_WAKER_CHECK_NAME = "orch-waker (orchestrator waker)"


def _refused_orch_waker_check() -> dict:
    """In-suite stand-in for the orch-waker check when no seam was injected.

    The check's real targets are ``~/.hscc/orch_waker.json`` and
    ``~/.hscc/state/orch_waker.json`` — the OPERATOR's live daemon state when
    the suite runs from a dispatched worker. Same fence shape as
    ``_refused_transport`` / ``_refused_role_soul_scan``: an ERROR naming the
    missing seam, never a silent pass and never a real read.
    """
    return {"name": _ORCH_WAKER_CHECK_NAME, "ok": False, "fatal": False,
            "detail": "orch-waker check refused inside a pytest process — "
                      "inject _orch_waker_check",
            "fix": "Pass _orch_waker_check=lambda: {...} built from tmp "
                   "config/state fixtures; a suite must never read the "
                   "operator's daemon state."}


def _orch_waker_check_row(injected=None, hscc_dir=None) -> "Check":
    """The orch-waker row for the shared check list (five Check fields).

    Three sources, in order of trust: an injected callable (tests pass a
    fixture-backed lambda), the real :func:`hscc_daemon.orch_waker.doctor_check`
    (a human CLI run), and the in-suite refusal (pytest with no seam — never a
    real read of the operator's daemon state). A module that cannot be imported
    at all reports itself UNVERIFIABLE rather than vanishing from the list, the
    same honesty rule as the notifier-mask check.

    ``hscc_dir`` is accepted because every feature row's function takes the same
    shape, and is deliberately NOT forwarded (t_be690204). Every writer of what
    this row reads resolves from ``$HOME`` — the waker's config
    (``orch_waker.DEFAULT_CONFIG_PATH``), its stream stamp
    (``state.STATE_DIR``) and ``daemon.pid`` (``daemon_ops``) — and not one of
    them reads ``HSCC_DIR`` from the environment. A row handed the ambient root
    was reading a directory nothing can write to and answering "the daemon is
    not running" about a daemon that had ticked seconds ago (and FATAL, once a
    pid file happened to sit at that root — both measured). The registry entry
    for this row declares no ``row_kwargs``, so the wiring itself cannot hand a
    root to a row that must not choose one; ``orch_waker.doctor_check`` keeps the
    parameter purely as t_e9b0f048's caller-threaded seam, and refuses a redirect
    onto a root that has never held a waker artefact.
    """
    if injected is not None:
        res = injected()
    elif doctor_rows.under_pytest():
        res = _refused_orch_waker_check()
    else:
        waker = _load_orch_waker()
        if waker is None:
            return Check(_ORCH_WAKER_CHECK_NAME, False,
                         detail="hscc_daemon/orch_waker.py not importable — "
                                "the waker's health cannot be verified",
                         fix="Deploy the hscc_daemon plugin next to "
                             "hscc-bootstrap (re-run bootstrap).",
                         fatal=False)
        # No hscc_dir: the waker's root is its writers' root, not this
        # process's environment (t_be690204, above).
        res = waker.doctor_check()
    return Check(name=res["name"], ok=res["ok"], detail=res["detail"],
                 fix=res["fix"], fatal=res["fatal"])


def _load_cron_watchers():
    """Import ``hscc_daemon/cron_watchers.py`` for the GAP-B-4 doctor check.

    Same rule as :func:`_load_orch_waker`: the verdict lives with the feature
    (its cadence SOT, its stamp format, its freshness budget) so doctor cannot
    drift from what the watcher means by "last pass"; doctor.py only adapts the
    result dict to its five Check fields. Returns the module, or None when the
    daemon plugin is not deployed next to this one (the check then reports
    itself unverifiable rather than vanishing from the list).
    """
    try:
        from hscc_daemon import cron_watchers
        return cron_watchers
    except Exception:
        pass
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from hscc_daemon import cron_watchers
        return cron_watchers
    except Exception:
        return None


#: Fallback for the check name when the module itself cannot be imported —
#: same trick as ``_ORCH_WAKER_CHECK_NAME`` so the row still prints. Must stay
#: byte-identical to ``cron_watchers.CHECK_NAME``, which enumerates every stream
#: in its WATCHERS roster (dep_pr, escalate, alone_sweep).
_CRON_WATCHERS_CHECK_NAME = "cron watchers (dep_pr/escalate/alone_sweep)"


def _refused_cron_watchers_check() -> dict:
    """In-suite stand-in for the cron-watcher check when no seam was injected.

    The check's real targets are ``~/.hscc/state/dep_pr_watcher.json``,
    ``~/.hscc/state/escalate_watcher.json``,
    ``~/.hscc/state/alone_sweep_gate.json`` and the install marker beside them
    — the OPERATOR's live tree when the suite runs from a dispatched worker.
    Same fence shape as ``_refused_orch_waker_check``: an ERROR naming the
    missing seam, never a silent pass and never a real read.
    """
    return {"name": _CRON_WATCHERS_CHECK_NAME, "ok": False, "fatal": False,
            "detail": "cron-watcher check refused inside a pytest process — "
                      "inject _cron_watchers_check",
            "fix": "Pass _cron_watchers_check=lambda: {...} built from tmp "
                   "state fixtures; a suite must never read the operator's "
                   "watcher stamps."}


def _cron_watchers_check_row(injected=None, hscc_dir=None) -> "Check":
    """The GAP-B-4 row for the shared check list (five Check fields).

    Three sources, in order of trust: an injected callable (tests pass a
    fixture-backed lambda), the real :func:`hscc_daemon.cron_watchers.doctor_check`
    (a human CLI run), and the in-suite refusal (pytest with no seam — never a
    real read). ``hscc_dir`` is accepted because every feature row's function
    takes the same shape, and is deliberately NOT forwarded (t_adb66608): the
    watcher ledger — ``~/.hscc/state/<stream>.json`` plus the install marker
    beside it — has one root, the canonical state writer's, and the writers
    (Hermes cron jobs, the installers) never see ``HSCC_DIR``. A row that read
    wherever the ambient variable pointed was reading a root nothing can write
    to and reporting live watchers as "not configured" — the live-host
    measurement in the t_572b4856 audit §10.1. ``cron_watchers.doctor_check``
    keeps the parameter as a caller-threaded test seam only, and this row's
    registry entry declares no ``row_kwargs``, so the wiring itself cannot hand
    a root to a row that must not choose one.

    The check itself is warn-only (fatal=False always) — GAP-B-4 asks for
    detection, not for a bootstrap that dies over an out-of-band cron job.
    """
    if injected is not None:
        res = injected()
    elif doctor_rows.under_pytest():
        res = _refused_cron_watchers_check()
    else:
        cw = _load_cron_watchers()
        if cw is None:
            return Check(_CRON_WATCHERS_CHECK_NAME, False,
                         detail="hscc_daemon/cron_watchers.py not importable — "
                                "the cron watchers' liveness cannot be verified",
                         fix="Deploy the hscc_daemon plugin next to "
                             "hscc-bootstrap (re-run bootstrap).",
                         fatal=False)
        # No hscc_dir: the ledger's root is the writer's root, not this
        # process's environment (t_adb66608, above).
        #
        # t_95eb9f15: the live doctor also compares each watcher's DEPLOYED copy
        # against the repo copy the installer took it from. Opt-in on purpose --
        # doctor_check reads no host scripts dir unless it is asked to -- and
        # probed rather than assumed, because the cron_watchers this row loads
        # may predate the seam (the deployed plugin payload is a release, not
        # this checkout): a warn-only row must degrade to the stamp read, not
        # raise out of the whole doctor run.
        if "check_deployed_copies" in inspect.signature(cw.doctor_check).parameters:
            res = cw.doctor_check(check_deployed_copies=True)
        else:
            res = cw.doctor_check()
    return Check(name=res["name"], ok=res["ok"], detail=res["detail"],
                 fix=res["fix"], fatal=res["fatal"])


#: Fallback for the supervision row's name when the module cannot be imported
#: — same trick as ``_NOTIFIER_CHECK_NAME`` so the row still prints. A missing
#: check must never render as absence-of-output.
_SUPERVISION_CHECK_NAME = "daemon supervision (service manager owns the pid)"


def _refused_supervision_check() -> dict:
    """In-suite stand-in for the supervision check when no seam was injected.

    Its real targets are the live service manager (``launchctl print`` /
    ``systemctl is-active``) and ``~/.hscc/daemon.pid`` — the OPERATOR's
    machine when the suite runs from a dispatched worker. Same fence shape as
    ``_refused_orch_waker_check``: an ERROR naming the missing seam, never a
    silent pass and never a real probe.
    """
    return {"name": _SUPERVISION_CHECK_NAME, "ok": False, "fatal": False,
            "detail": "supervision check refused inside a pytest process — "
                      "inject _supervision_check",
            "fix": "Pass _supervision_check=lambda: {...} built from tmp "
                   "pid-file fixtures plus stubbed _launchd/_systemd probes; "
                   "a suite must never ask the live service manager."}


def _supervision_check_row(injected=None, hscc_dir=None) -> "Check":
    """The GAP-F-5 supervision row (five Check fields, fatal=False always).

    Why this row exists: the t_61457fbd incident — an unsupervised orphan
    daemon-loop (``hscc start`` beside the job, or a TCC-blind bootstrap) —
    keeps stream state fresh, so every other doctor/verify row reads green
    while NOTHING supervises the daemon. The comparison nothing else makes:
    the pid file's pid vs the pid the owning service manager reports, using
    restart_daemon.sh's own probes. Non-fatal on every verdict (the daemon is
    legitimately not running before the install starts it; doctor is the
    Stage-1 gate), so it warns (○) like the orch-waker and models-served rows.

    Three sources, same trust order as the waker row: an injected callable
    (tests), the real ``doctor_supervision.check`` (a human CLI run), and the
    in-suite refusal (pytest with no seam — never a live probe). A module that
    cannot be imported reports itself UNVERIFIABLE rather than vanishing.
    """
    if injected is not None:
        res = injected()
    elif doctor_rows.under_pytest():
        res = _refused_supervision_check()
    else:
        try:
            import doctor_supervision
        except ImportError:
            return Check(_SUPERVISION_CHECK_NAME, False,
                         detail="doctor_supervision.py missing from "
                                f"{os.path.dirname(os.path.abspath(__file__))}"
                                " — supervision cannot be verified",
                         fix="Restore hscc-bootstrap/doctor_supervision.py "
                             "(re-run bootstrap).",
                         fatal=False)
        res = doctor_supervision.check(hscc_dir=hscc_dir)
    return Check(name=res["name"], ok=res["ok"], detail=res["detail"],
                 fix=res["fix"], fatal=res["fatal"])


def _load_kanban_pause():
    """Import ``hscc_daemon/kanban_pause.py`` for the GAP-F-4 doctor check.

    Same rule as :func:`_load_orch_waker`: the verdict lives with the feature
    (its stream name, its cadence SOT, its freshness budget, its definition of
    a coherent paused-policy document) so doctor cannot drift from what the
    running daemon's enforcer stream means by "last enforce". doctor.py only
    adapts the result dict to its five Check fields. Returns the module, or
    None when the daemon plugin is not deployed next to this one (the check
    then reports itself unverifiable rather than vanishing).
    """
    try:
        from hscc_daemon import kanban_pause
        return kanban_pause
    except Exception:
        pass
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from hscc_daemon import kanban_pause
        return kanban_pause
    except Exception:
        return None


#: Fallback for the check name when the module itself cannot be imported —
#: same trick as ``_ORCH_WAKER_CHECK_NAME`` so the row still prints.
_KANBAN_ENFORCE_CHECK_NAME = "kanban enforce (paused-board hold)"


def _refused_kanban_enforce_check() -> dict:
    """In-suite stand-in for the kanban-enforce check when no seam was injected.

    The check's real targets are ``~/.hscc/state/kanban_paused.json``,
    ``~/.hscc/state/kanban_enforce.json`` and ``~/.hscc/daemon.pid`` — the
    OPERATOR's live daemon state when the suite runs from a dispatched worker.
    Same fence shape as ``_refused_orch_waker_check``: an ERROR naming the
    missing seam, never a silent pass and never a real read.
    """
    return {"name": _KANBAN_ENFORCE_CHECK_NAME, "ok": False, "fatal": False,
            "detail": "kanban-enforce check refused inside a pytest process — "
                      "inject _kanban_enforce_check",
            "fix": "Pass _kanban_enforce_check=lambda: {...} built from tmp "
                   "paused-state/stream fixtures; a suite must never read the "
                   "operator's daemon state."}


def _kanban_enforce_check_row(injected=None, hscc_dir=None) -> "Check":
    """The GAP-F-4 row for the shared check list (five Check fields).

    Why this row exists (audit F GAP-F-4): `hscc kanban enforce` — the verb
    that re-holds cards an in-flight orchestrator turn leaves ready/running on
    a PAUSED board — used to have no scheduler and no coverage at all, so a
    paused board could silently leak work while every other row stayed green.
    The verb is now the daemon's ``kanban_enforce`` stream (60 s,
    PERIODIC_INTERVALS); this row asserts the enforcer is alive and the policy
    document is coherent (the audit's "N board(s) paused, last enforce Ns
    ago"). RED is reserved for the orch-waker class: boards policy exists,
    the daemon runs, and the stream has never stamped.

    Three sources, same trust order as the waker row: an injected callable
    (tests), the real :func:`hscc_daemon.kanban_pause.doctor_check` (a human
    CLI run), and the in-suite refusal (pytest with no seam — never a real
    read of the operator's daemon state).

    ``hscc_dir`` is accepted because every feature row's function takes the same
    shape, and is deliberately NOT forwarded (t_be690204). Both files this row
    reads are written by writers that resolve from ``$HOME`` — the paused-policy
    document (``kanban_pause.STATE_PATH``) and the ``kanban_enforce`` stream
    stamp (``state.STATE_DIR``), plus ``daemon.pid`` from ``daemon_ops`` — and
    none of them reads ``HSCC_DIR``. A row handed the ambient root read a
    directory nothing can write to and answered "the daemon is not running —
    nothing enforces" about a daemon whose enforce stream had stamped seconds
    ago (and FATAL, once a pid file happened to sit there — both measured). The
    registry entry declares no ``row_kwargs``, so the wiring cannot hand a root
    to a row that must not choose one; ``kanban_pause.doctor_check`` keeps the
    parameter purely as t_e9b0f048's caller-threaded seam, and refuses a redirect
    onto a root that has never held an enforcer artefact.
    """
    if injected is not None:
        res = injected()
    elif doctor_rows.under_pytest():
        res = _refused_kanban_enforce_check()
    else:
        kp = _load_kanban_pause()
        if kp is None:
            return Check(_KANBAN_ENFORCE_CHECK_NAME, False,
                         detail="hscc_daemon/kanban_pause.py not importable — "
                                "the paused-board enforcer's health cannot be "
                                "verified",
                         fix="Deploy the hscc_daemon plugin next to "
                             "hscc-bootstrap (re-run bootstrap).",
                         fatal=False)
        # No hscc_dir: the enforcer's root is its writers' root, not this
        # process's environment (t_be690204, above).
        res = kp.doctor_check()
    return Check(name=res["name"], ok=res["ok"], detail=res["detail"],
                 fix=res["fix"], fatal=res["fatal"])


#: Candidate plugin roots whose ``hscc-cluster`` subdir carries the budget hook.
#: Same resolution rule the installer uses (install_payload.py: ``PLUGINS`` env
#: wins, else ``~/.hermes/plugins``) plus the ``--home``/sandbox root, so a
#: bootstrap run pointed at a throwaway tree checks THAT tree and never the
#: operator's live one.
def _budget_plugin_dirs(hermes_home=None) -> list:
    """Ordered candidate plugin-container roots for the budget-hook check.

    ``budget_watch.doctor_check`` takes ROOTS and joins ``hscc-cluster`` itself,
    so these are dirs that CONTAIN plugin dirs (``~/.hermes/plugins``), not the
    plugin dir.

    A home contributes ONLY ITS OWN ``plugins`` dir. A PROFILE-scoped home
    (``<root>/profiles/<name>`` — the normal worker mode) therefore contributes
    ``<profile>/plugins`` and NOT the root's, which is the authority question
    t_ba14f77f asks: Hermes' user-plugin scan is
    ``get_hermes_home() / "plugins"`` (hermes_cli/plugins_discovery.py:184) and
    under a profile-scoped ``HERMES_HOME`` that IS the profile dir, with no
    fallback to the root. So the root copy ``install_payload_profiles`` also
    writes is never loaded by that worker — and while it was a candidate here,
    ``doctor_check``'s any-wired-is-green let a wired ROOT rescue an unwired
    PROFILE deploy, i.e. the row read green over the very hook the running
    worker had lost. The installer's write-both symmetry is unchanged (the root
    copy is authoritative for the root-scoped home); only who may ANSWER for a
    given home changed. The root-scoped home still checks the root dir: it is
    ``<root>/plugins`` reached as ``home/plugins`` by the ordinary branch.
    """
    dirs: List[str] = []

    def _push(path):
        if not path:
            return
        path = os.path.expanduser(str(path))
        if path not in dirs:
            dirs.append(path)

    _push(os.environ.get("PLUGINS"))
    home = os.path.expanduser(str(hermes_home)) if hermes_home else ""
    if home:
        _push(os.path.join(home, "plugins"))
        # No default tail when the caller NAMED a home. Appending
        # ``~/.hermes/plugins`` unconditionally — the shape this shipped with —
        # made the row answer about the OPERATOR's deploy: both
        # ``_load_budget_watch`` (first importable candidate wins) and
        # ``budget_watch.doctor_check`` (any wired candidate is green) could
        # rescue an unwired/broken named tree with the live one, so a sandbox or
        # ``--home`` bootstrap read all-green over the very deploy it was asked
        # to verify (t_d7f2ccd1; measured: three test_doctor_budget_hook cases
        # failing on any host whose own deploy is wired). It also no longer has
        # a legitimate job: home == ``~/.hermes`` dedupes onto it, and the
        # profile case above deliberately does NOT want the root's answer
        # (t_ba14f77f).
    else:
        # Human CLI run with no ``--home``: the operator's own deploy is the
        # target, so the default root stays.
        _push(os.path.join(os.path.expanduser("~"), ".hermes", "plugins"))
    return dirs


def _load_budget_watch(hermes_home=None):
    """Import the DEPLOYED ``hscc-cluster/budget_watch.py`` for the doctor row.

    Same rule as :func:`_load_orch_waker`: the verdict lives with the feature
    (its thresholds, its wiring markers) so doctor cannot drift from what the
    hook means by "registered"; doctor.py only adapts the result dict to its
    five Check fields.

    ``hscc-cluster`` is hyphenated — not an importable package name — and each
    plugin deploys standalone (see the mirrored-block note at the top of this
    file), so this resolves the deployed FILE and loads it by path
    (``importlib.util.spec_from_file_location``, the in-tree precedent at
    ``enable_plugins.py:375``) rather than importing across plugin boundaries.
    The module is stdlib-only, so loading it has no side effects.

    Returns the module, or None when no candidate root carries it (the row then
    reports itself unverifiable rather than vanishing from the list).
    """
    import importlib.util

    for root in _budget_plugin_dirs(hermes_home):
        cand = os.path.join(root, "hscc-cluster", "budget_watch.py")
        if not os.path.isfile(cand):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                "hscc_budget_watch_doctor", cand)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        except Exception:
            continue
    return None


def _home_has_hscc_cluster_payload(plugin_dirs) -> bool:
    """True when ANY resolved plugin root carries an ``hscc-cluster`` dir.

    Separates the two RED classes the row can report (t_5720ef53):

      A. the home has no ``hscc-cluster`` payload at all — it was never
         deployed here, so there is no wiring to have lost;
      B. the payload is here but the wiring is gone or the module will not
         load — the failure class this row was written to catch.

    Deliberately a dir-exists test, NOT an import or a wiring test: class A has
    to be recognised by a caller that has already failed to import the module,
    and "does the dir exist at all" is precisely the question the two classes
    answer differently. ``budget_watch.doctor_check`` keeps the verdict on the
    wiring itself (verdict-with-feature); this only says whether it had
    anything to judge.
    """
    for root in plugin_dirs or ():
        if os.path.isdir(os.path.join(str(root), "hscc-cluster")):
            return True
    return False


#: Fallback for the row's name when the module cannot be imported — same trick
#: as ``_ORCH_WAKER_CHECK_NAME`` so the row still prints. A missing check must
#: never render as absence-of-output.
_BUDGET_HOOK_CHECK_NAME = "worker budget hook ([budget] note)"


def _refused_budget_hook_check() -> dict:
    """In-suite stand-in for the budget-hook check when no seam was injected.

    Its real target is the DEPLOYED plugin tree (``~/.hermes/plugins`` or the
    live profile's ``plugins`` dir) — the OPERATOR's install when the suite runs
    from a dispatched worker. Same fence shape as ``_refused_orch_waker_check``:
    an ERROR naming the missing seam, never a silent pass and never a real read.
    """
    return {"name": _BUDGET_HOOK_CHECK_NAME, "ok": False, "fatal": False,
            "detail": "budget-hook check refused inside a pytest process — "
                      "inject _budget_hook_check",
            "fix": "Pass _budget_hook_check=lambda: {...} built from tmp "
                   "plugin-tree fixtures, or call _budget_hook_check_row() "
                   "directly with a fixture tree; a suite must never read the "
                   "operator's deployed plugin tree."}


def _budget_hook_check_row(injected=None, hermes_home=None) -> "Check":
    """The t_6c4f38f2 budget-hook row for the shared check list (five fields).

    Why this row exists: the worker SOUL promises a ``[budget]`` note and
    ``budget_watch.doctor_check`` verifies the two ``ctx.register_hook`` calls
    that make it real — but until this row was wired, nothing in doctor ever
    called it, so a deploy that silently lost the wiring still read all-green.
    The comparison nothing else makes: the DEPLOYED tree (what Hermes actually
    loads), not the repo copy.

    Three sources, same trust order as the waker row: an injected callable
    (tests), the real ``budget_watch.doctor_check(plugins_dirs=...)`` (a human
    CLI run), and the in-suite refusal (pytest with no seam — never a real read
    of the operator's plugin tree). A module that cannot be imported reports
    itself UNVERIFIABLE rather than vanishing. Non-fatal on every verdict: a
    missing hook costs the budget note, never an install.
    """
    if injected is not None:
        res = injected()
    elif doctor_rows.under_pytest():
        res = _refused_budget_hook_check()
    else:
        roots = _budget_plugin_dirs(hermes_home)
        bw = _load_budget_watch(hermes_home)
        if bw is None:
            if not _home_has_hscc_cluster_payload(roots):
                # CLASS A — this home never carried the payload. Red, because
                # it is the true verdict: Hermes discovers user plugins at
                # ``get_hermes_home()/plugins`` only
                # (``hermes_cli/plugins_discovery.py:184``), so a home whose own
                # tree has no ``hscc-cluster`` loads NO hscc plugin — not just
                # the budget hook, no hscc tools either. Measured on this host
                # (t_5720ef53): every profile home except the two that are
                # wired reads this way, and the red set includes homes the
                # kanban dispatcher has really run cards in (ios-engineer,
                # worker, devops-engineer, reviewer, coder), so "never
                # deployed" is not a synonym for "not an HSCC home". It stays
                # red — a warning or a skip would hide the gap — but the detail
                # says the payload is ABSENT rather than "unverifiable",
                # because nothing here is broken-but-hidden: there is no wiring
                # to have lost, so the honest fix is to deploy (or to say on
                # purpose that this home is not HSCC's).
                # ``install_payload._named_profile_plugins_dirs`` only writes
                # into a profile that ALREADY has a ``plugins/`` dir, which is
                # how a home ends up in this class.
                searched = ", ".join(str(r) for r in roots[:3]) or \
                    "no plugin roots resolved"
                return Check(
                    _BUDGET_HOOK_CHECK_NAME, False,
                    detail=f"no hscc-cluster payload in this home: {searched} "
                           "has no hscc-cluster dir, so this home never "
                           "received the payload (it loads no hscc plugin at "
                           "all, not only no budget hook)",
                    fix="Deploy the payload here (re-run hscc-bootstrap; note "
                        "install_payload only writes into a profile that "
                        "already has a plugins/ dir) — or, if this home is "
                        "deliberately not an HSCC one, that is expected and "
                        "no fix is needed here.",
                    fatal=False)
            searched = ", ".join(roots[:3]) or "no plugin roots resolved"
            return Check(_BUDGET_HOOK_CHECK_NAME, False,
                         detail="hscc-cluster/budget_watch.py not importable "
                                f"(looked in: {searched}) — the budget hook's "
                                "wiring cannot be verified",
                         fix="Deploy the hscc-cluster plugin next to "
                             "hscc-bootstrap (re-run bootstrap).",
                         fatal=False)
        res = bw.doctor_check(plugins_dirs=roots)
    return Check(name=res["name"], ok=res["ok"], detail=res["detail"],
                 fix=res["fix"], fatal=res["fatal"])


def _load_main_board():
    """Import the main-board feature module (M3, t_7b55db57).

    The verdict lives in ``hscc-bootstrap/main_board.py`` with the resolver
    (``hscc-roles/main_orch.py``) and the Hermes current-board reader, so doctor
    only adapts it to its five Check fields. Returns None when the module is
    not deployed next to bootstrap — the row then reports itself unverifiable
    instead of vanishing.
    """
    try:
        import main_board
        return main_board
    except Exception:
        pass
    bootstrap_dir = os.path.dirname(os.path.abspath(__file__))
    if bootstrap_dir not in sys.path:
        sys.path.insert(0, bootstrap_dir)
    try:
        import main_board
        return main_board
    except Exception:
        return None


#: Fallback for the row name when the module itself cannot be imported — same
#: trick as ``_ORCH_WAKER_CHECK_NAME`` so the row still prints.
_MAIN_BOARD_CHECK_NAME = "kanban current board (main orchestrator)"


def _refused_main_board_check() -> dict:
    """In-suite stand-in for the main-board check when no seam was injected.

    The check reads Hermes' machine-wide kanban current pointer — the
    OPERATOR's live board state when the suite runs from a dispatched worker.
    Same fence shape as ``_refused_kanban_enforce_check``: an ERROR naming the
    missing seam, never a silent pass and never a real read.
    """
    return {"name": _MAIN_BOARD_CHECK_NAME, "ok": False, "fatal": False,
            "detail": "main-board check refused inside a pytest process — "
                      "inject _main_board_check",
            "fix": "Pass _main_board_check=lambda: {...} built from a tmp "
                   "HERMES_HOME/kanban fixture; a suite must never read the "
                   "operator's current board."}


def _load_main_wakeups():
    """Import ``hscc_daemon/main_wakeups.py`` for the M5 doctor check.

    The verdict lives with the feature (config loader, state, schedule math and
    the pane seam), so doctor only adapts it to its five Check fields. Returns
    None when the daemon plugin is not deployed next to bootstrap — the row
    then reports itself unverifiable rather than vanishing.
    """
    try:
        from hscc_daemon import main_wakeups
        return main_wakeups
    except Exception:
        pass
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from hscc_daemon import main_wakeups
        return main_wakeups
    except Exception:
        return None


#: Fallback for the row name when the module itself cannot be imported.
_MAIN_WAKEUPS_CHECK_NAME = "main wakeups (daemon-owned schedule)"


def _refused_main_wakeups_check() -> dict:
    """In-suite stand-in when no wake-up verdict was injected.

    The real check reads ``~/.hscc/main_wakeups.json``, its state file and the
    operator's tmux panes. Same fence shape as the other daemon rows: an ERROR
    naming the seam, never a silent pass and never a real read.
    """
    return {"name": _MAIN_WAKEUPS_CHECK_NAME, "ok": False, "fatal": False,
            "detail": "main-wakeups check refused inside a pytest process — "
                      "inject _main_wakeups_check",
            "fix": "Pass _main_wakeups_check=lambda: {...} built from tmp "
                   "config/state fixtures; a suite must never read the "
                   "operator's wake-ups or panes."}


#: Fallback for the check name when the feature module cannot be imported at
#: all — same trick as ``_MAIN_BOARD_CHECK_NAME`` so the row still prints. The
#: authoritative spelling lives in the feature module
#: (``doctor_node_pauses.CHECK_NAME``); the two are pinned equal by
#: tests/test_doctor_node_pauses.py.
_NODE_PAUSES_CHECK_NAME = "node pauses (operator overlay)"


def _load_doctor_node_pauses():
    """Import the node-pause feature module (2.17.3, t_ba1fec3e).

    The verdict lives next to the reader it wraps —
    ``hscc-bootstrap/doctor_node_pauses.py`` owns what counts as a malformed
    overlay (it listens to ``node_pauses``' own warnings instead of
    re-implementing the overlay's validation) and the wording that separates an
    operator's parked node from a broken one. doctor only adapts the result to
    its five Check fields. Returns None when the module is not deployed next to
    bootstrap, and the row reports itself unverifiable rather than vanishing.
    """
    try:
        import doctor_node_pauses
        return doctor_node_pauses
    except Exception:  # noqa: BLE001 - any import failure means "not deployed"
        return None


def _refused_node_pauses_check() -> dict:
    """In-suite stand-in for the node-pause check when no seam was injected.

    The row's real target is ``~/.hscc/node_pauses.json`` — the OPERATOR's
    overlay, whose contents would decide what the suite asserts. Same fence
    shape as ``_refused_kanban_enforce_check``: an ERROR naming the missing
    seam, never a silent pass and never a real read. The feature module applies
    the identical fence to its own default-root path
    (:func:`doctor_node_pauses.doctor_check` with ``directory=None``), so a
    caller cannot walk past it by skipping the registry.
    """
    return {"name": _NODE_PAUSES_CHECK_NAME, "ok": False, "fatal": False,
            "detail": "node-pause check refused inside a pytest process — "
                      "inject _node_pauses_check",
            "fix": "Call doctor_node_pauses.doctor_check(directory=<tmp "
                   "overlay root>) for the real read path, or pass "
                   "_node_pauses_check=lambda: {...} to pin the row's "
                   "routing; a suite must never read the operator's overlay."}


#: Fallback for the check name when the stream's module cannot be imported —
#: same trick as ``_KANBAN_ENFORCE_CHECK_NAME`` so the row still prints.
_NODE_PAUSE_EXPIRY_CHECK_NAME = "node pause expiry (durable-until stream)"


def _load_node_pause_exec():
    """Import ``hscc_daemon/node_pause_exec.py`` for the expiry-stream row.

    The verdict lives with the stream (t_32a06db9's rule): its cadence SOT is
    ``daemon_ops.PERIODIC_INTERVALS``, its stamp shape is what
    ``expiry_tick`` writes, and its definition of "past due" is the tick's own
    ``_due_entries`` — a row written down here in bootstrap would drift from all
    three. doctor only adapts the result to its five Check fields. Two-step
    import like :func:`_load_kanban_pause` (deployed plugin, then repo root),
    and None means the row reports itself unverifiable rather than vanishing.
    """
    try:
        from hscc_daemon import node_pause_exec
        return node_pause_exec
    except Exception:  # noqa: BLE001 - any import failure means "not deployed"
        pass
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from hscc_daemon import node_pause_exec
        return node_pause_exec
    except Exception:  # noqa: BLE001
        return None


def _refused_node_pause_expiry_check() -> dict:
    """In-suite stand-in for the expiry-stream row when no seam was injected.

    The row's real targets are ``~/.hscc/state/node_pause_expiry.json`` and
    ``~/.hscc/node_pauses.json`` — the operator's live stamp and overlay, which
    would decide what the suite asserts. Same fence as
    :func:`_refused_node_pauses_check`, and the feature module applies the
    identical fence to its own default-root path
    (:func:`hscc_daemon.node_pause_exec.doctor_check` with ``root=None``), so a
    caller cannot walk past it by skipping the registry.
    """
    return {"name": _NODE_PAUSE_EXPIRY_CHECK_NAME, "ok": False, "fatal": False,
            "detail": "node-pause-expiry check refused inside a pytest process "
                      "— inject _node_pause_expiry_check",
            "fix": "Call node_pause_exec.doctor_check(root=<tmp ~/.hscc-shaped "
                   "root>) for the real read path, or pass "
                   "_node_pause_expiry_check=lambda: {...} to pin the row's "
                   "routing; a suite must never read the operator's stamp."}


# ── main-only cluster surface (t_caf98114) ─────────────────────────────────
# Why this row exists: the 2026-11-23 main-only audit (PR #666) had to MEASURE,
# by hand over 52 profile homes, which homes carried the cluster control surface.
# Bootstrap had never written a per-profile ``plugins.enabled`` at all
# (``enable_plugins.enable()`` wires only the config.yaml it is handed), so the
# homes that did name the tool plugins were written by something else — the
# class the audit called the ``_migrate_to_21`` landmine — and nothing in the
# fleet's own health surface could see it. This row is that comparison: the
# cluster tools/skills must sit on the main orchestrator's home and nowhere
# else, while the hscc-cluster HOOKS stay everywhere (binding ruling R2/D-C —
# hook presence is NOT drift and this row never reports it).

#: Fallback for the check name when the feature module cannot be imported — same
#: trick as ``_NODE_PAUSES_CHECK_NAME`` so the row still prints, and pinned equal
#: to ``doctor_main_only.CHECK_NAME`` by the row's own suite.
_MAIN_ONLY_SURFACE_CHECK_NAME = \
    "main-only cluster surface (cluster tools/skills off <main>)"


def _load_doctor_main_only():
    """Import the main-only-surface feature module (t_caf98114).

    The verdict lives with the feature: ``doctor_main_only.py`` owns which
    surface counts (the ``plugins.enabled`` tool plugins and the hscc-owned
    cluster skills), which does not (the ``hscc-cluster`` hooks, an on-disk
    plugin dir, a cosmetic ``toolsets:`` entry), and the wording that names the
    profile, the surface and the repair. It also refuses to hardcode the main
    identity and reads it through ``hscc-roles/main_orch.main_name()``
    (R2/D-A). doctor only adapts the result to its five Check fields.

    Returns None when the module is not deployed next to bootstrap; the row then
    reports itself unverifiable rather than vanishing from the list.
    """
    try:
        import doctor_main_only
        return doctor_main_only
    except Exception:  # noqa: BLE001 - any import failure means "not deployed"
        return None


def _refused_main_only_check() -> dict:
    """In-suite stand-in for the main-only check when no seam was injected.

    Its real target is the operator's ``~/.hermes`` profile trees — their
    contents would decide what the suite asserts. Same fence shape as
    ``_refused_node_pauses_check``: an ERROR naming the missing seam, never a
    silent pass and never a real read. The wording is delegated to the feature
    module when it is importable, so doctor and the check cannot state two
    different rules for the same refusal.
    """
    mod = _load_doctor_main_only()
    refused_hook = getattr(mod, "refused", None)
    if callable(refused_hook):
        verdict = refused_hook()
        if isinstance(verdict, dict):
            return verdict
    return {"name": _MAIN_ONLY_SURFACE_CHECK_NAME, "ok": False, "fatal": False,
            "detail": "main-only-surface check refused inside a pytest process "
                      "— inject _main_only_surface_check",
            "fix": "Pass _main_only_surface_check=lambda: {...} built from "
                   "tmp fake profile trees, or call "
                   "doctor_main_only.doctor_check(hermes_home=...) directly; a "
                   "suite must never read the operator's profile trees."}


# --- kanban notify+wake subscription posture (t_639b2834) ------------------
#
# The 2026-11-23 duplicate-orchestrator incident was 307 notify+wake
# subscriptions pointing at one gateway session while the real orchestrator sat
# in a tmux pane; the gateway kept waking a SECOND copy of that identity, which
# worked the boards alongside the pane REPL for about two weeks. The
# subscribe-side gate is ``hscc-roles/orch_subscribe.py`` (t_31c8b258); this row
# is the regression alarm behind it, because a pane can appear long after a
# profile was provisioned and the gate is new.

#: Fallback for the check name when the feature module cannot be imported — same
#: trick as ``_MAIN_ONLY_SURFACE_CHECK_NAME``, pinned equal to
#: ``doctor_orch_subs.CHECK_NAME`` by the row's own suite.
_ORCH_WAKE_SUBS_CHECK_NAME = \
    "orch wake subs (notify+wake target = live pane session)"


def _load_doctor_orch_subs():
    """Import the wake-subscription feature module (t_639b2834).

    The verdict lives with the feature: ``doctor_orch_subs.py`` owns which
    subscription counts (a wake mode on a platform whose ``chat_id`` names a
    session), which does not (a chat channel, a plain ``notify`` sub, an
    unattributed row, a paneless owner, the root profile), and the wording that
    names the profile, the subscription id, the target session, the expected
    pane session and the repair. It resolves the pane through
    ``hscc-roles/orch_subscribe.py`` rather than restating the lease rule.

    Returns None when the module is not deployed next to bootstrap; the row then
    reports itself unverifiable rather than vanishing from the list.
    """
    try:
        import doctor_orch_subs
        return doctor_orch_subs
    except Exception:  # noqa: BLE001 - any import failure means "not deployed"
        return None


def _refused_orch_wake_subs_check() -> dict:
    """In-suite stand-in for the wake-subscription check when no seam was given.

    Its real inputs are the operator's live board DBs and live session
    registries — reading them inside a suite would let the live fleet decide the
    suite's assertions. Same fence shape as ``_refused_main_only_check``: an
    ERROR naming the seams, never a silent pass and never a real read. The
    wording is delegated to the feature module when it is importable, so doctor
    and the check cannot state two different rules for the same refusal.
    """
    mod = _load_doctor_orch_subs()
    refused_hook = getattr(mod, "refused", None)
    if callable(refused_hook):
        verdict = refused_hook()
        if isinstance(verdict, dict):
            return verdict
    return {"name": _ORCH_WAKE_SUBS_CHECK_NAME, "ok": False, "fatal": False,
            "detail": "wake-subscription check refused inside a pytest process "
                      "— inject _orch_wake_subs_check",
            "fix": "Call doctor_orch_subs.doctor_check(boards=..., "
                   "subscriptions=..., registry=...) with fake sub rows and "
                   "fake leases, or pass "
                   "_orch_wake_subs_check=lambda: {...}; a suite must never "
                   "open a live board or a live active-session registry."}


def _load_doctor_process_posture():
    """Import ``hscc-bootstrap/doctor_process_posture.py`` for the t_8b17cb40 row.

    Same rule as :func:`_load_orch_waker`: the verdict lives with the feature, and
    a module that cannot be imported is reported UNVERIFIABLE rather than dropped.
    """
    try:
        import doctor_process_posture
    except ImportError:
        return None
    return doctor_process_posture


#: Fallback for the posture row's name when the module cannot be imported — the
#: same trick as ``_ORCH_WAKE_SUBS_CHECK_NAME`` so the row still prints. Must stay
#: equal to ``doctor_process_posture.CHECK_NAME`` (pinned by
#: test_doctor_process_posture_t_8b17cb40.py, and the equality pin in
#: test_doctor_row_registry.py).
_PROCESS_POSTURE_CHECK_NAME = "daemon scheduling posture (launchd ProcessType=Standard)"


def _refused_process_posture_check() -> dict:
    """In-suite stand-in for the posture check when no seam was injected.

    Its real targets are the OPERATOR's installed launchd plist
    (``~/Library/LaunchAgents/com.hermes.hscc_daemon.plist``) and the live
    service manager. Same fence shape as ``_refused_supervision_check``: an ERROR
    naming the missing seam, never a silent pass and never a real read.
    """
    return {"name": _PROCESS_POSTURE_CHECK_NAME, "ok": False, "fatal": False,
            "detail": "scheduling-posture check refused inside a pytest "
                      "process — inject _process_posture_check",
            "fix": "Pass _process_posture_check=lambda: {...} built from a tmp "
                   "plist fixture plus stubbed _launchd/_systemd probes; a suite "
                   "must never read ~/Library/LaunchAgents or ask launchd."}


# ---------------------------------------------------------------------------
# FEATURE ROW REGISTRY (t_32a06db9)
#
# The five rows above used to be wired into run_doctor() by hand: one call plus
# one "why this row exists" comment in the check list, and one keyword in
# run_doctor()'s signature each. Every feature that added a row edited the same
# three hunks, which is why PR #175 and PR #173 collided and needed the
# t_3f805c4f rebase — and PR #175 then collided with this card's own branch the
# same way, which is the strongest argument for the registry that exists.
# The wiring is now ONE entry per row below, iterated by run_doctor()
# (machinery in hscc-bootstrap/doctor_rows.py), so a new row is one edit in one
# place — never the signature, never the check list, never the adapter block.
#
# Adding a row:
#   1. put the verdict in the feature's own module (its config loader, its
#      stream name, its freshness budget — the verdict must live with the
#      feature so doctor cannot drift from what the running daemon means).
#      Precedent: doctor_supervision.py, doctor_notifier_mask.py, or the
#      feature's daemon module exposing doctor_check(hscc_dir=...).
#   2. add a loader + a refusal helper in this file (the refusal is the pytest
#      fence: the check's default target is the operator's live ~/.hscc or
#      deployed plugin tree, so an uninjected check inside a suite must come
#      back an ERROR naming the seam, never a silent pass and never a real read).
#   3. append ONE FeatureCheckRow here naming those helpers. Do not touch
#      run_doctor() — its **feature_rows kwargs are validated against the
#      registry, so the seam keyword exists the moment the entry does.
#
# `row_fn` is set for the five pre-existing rows because their own suites call
# those functions directly (and pin the five-field copy at the bridge); a NEW
# row should leave it empty and let doctor_rows.build_row assemble the row from
# loader/refusal/fallback_name. `row_kwargs` names the per-row context each
# row function wants: "hscc_dir" (the sandboxed ~/.hscc root, the
# doctor_notifier_mask convention) or "hermes_home" (the deployed plugin tree,
# what the budget-hook row reads). A row whose subject has a root the CALLER may
# not choose declares `row_kwargs=()` — the cron-watcher row does, because the
# ledger it reads is written by processes that never see HSCC_DIR (t_adb66608,
# note on that entry).
# ---------------------------------------------------------------------------
FEATURE_CHECK_ROWS: List["doctor_rows.FeatureCheckRow"] = [
    # orch-waker W3 (t_2b34b5c7): is the daemon-resident orchestrator waker
    # enabled and ticking? Its own check (hscc_daemon/orch_waker.py) owns the
    # verdict; only the five Check fields are taken, so `doctor --json` keeps
    # one shape. A STALE waker is a warning (fatal=False) — drift to fix, never
    # a reason to abort an install; RED is reserved for "enabled, daemon
    # running, this stream never stamped".
    doctor_rows.FeatureCheckRow(
        key="orch_waker", seam_kwarg="_orch_waker_check",
        row_fn="_orch_waker_check_row",
        # NO row_kwargs — deliberately not even the default "hscc_dir"
        # (t_be690204). Everything this row reads is written by a writer that
        # resolves from HOME (waker config, stream stamp through
        # state.STATE_DIR, daemon.pid), and none of them sees HSCC_DIR, so a row
        # handed the ambient root reads a directory nothing can write to and
        # calls a live waker "not running" — RED once a pid file sits there.
        # Declaring no context makes the writer/reader split un-wireable rather
        # than merely unwired today (the shape t_adb66608 proved for the cron
        # row).
        row_kwargs=()),
    # GAP-B-4 (audit B): the dep_pr / escalate Hermes-cron watchers run OUTSIDE
    # the daemon, so no stream notices when they stop firing — the dep-watcher
    # once failed 17 consecutive daily runs unnoticed (t_3aadd2c0). Each pass
    # stamps into ~/.hscc/state through the daemon's own state writer; the row
    # warns when a stamp passes 3x its cadence and says "not configured"
    # cleanly when nobody installed the watcher. Warn-only by construction
    # (fatal=False always).
    doctor_rows.FeatureCheckRow(
        key="cron_watchers", seam_kwarg="_cron_watchers_check",
        row_fn="_cron_watchers_check_row",
        # NO row_kwargs — deliberately not even the default "hscc_dir"
        # (t_adb66608). The watcher ledger has one root, the canonical state
        # writer's (state.STATE_DIR), and the writers of that ledger (Hermes
        # cron jobs, the installers) never see HSCC_DIR; a row handed the
        # ambient root therefore reads a directory nothing can write to and
        # calls live watchers "not configured" (audit t_572b4856 §10.1).
        # Declaring no context is what makes the writer/reader split
        # un-wireable instead of merely unwired today.
        row_kwargs=()),
    # GAP-F-5 (audit F): is the running daemon loop the process the service
    # manager OWNS? The t_61457fbd incident class (an orphan `hscc start` /
    # TCC-blind bootstrap loop with PPID 1) keeps stream state fresh, so every
    # other row reads green while nothing restarts the daemon on crash. This row
    # makes the ONE comparison nobody did: ~/.hscc/daemon.pid's pid vs the pid
    # launchd/systemd reports (the same probes restart_daemon.sh's pid-turnover
    # poll trusts). Warn, never fatal — doctor is Stage 1 and runs before the
    # daemon exists.
    doctor_rows.FeatureCheckRow(
        key="supervision", seam_kwarg="_supervision_check",
        row_fn="_supervision_check_row"),
    # GAP-F-4 (audit F, t_51931733): is the paused-board policy actually being
    # ENFORCED? `hscc kanban enforce` used to be a verb nobody called: an
    # orchestrator turn in flight could leave cards ready/running on a paused
    # board and nothing noticed. It is the daemon's kanban_enforce stream now
    # (60 s, PERIODIC_INTERVALS), and this row is the coverage half:
    # paused-count + last-enforce freshness + policy-document coherence, with
    # fatal RED reserved for the orch-waker class (policy live, daemon running,
    # stream never stamped).
    doctor_rows.FeatureCheckRow(
        key="kanban_enforce", seam_kwarg="_kanban_enforce_check",
        row_fn="_kanban_enforce_check_row",
        # NO row_kwargs — same reason as the orch-waker row above
        # (t_be690204): the paused-policy document and the kanban_enforce stamp
        # are written by HOME-bound writers (kanban_pause.STATE_PATH,
        # state.STATE_DIR) that never see HSCC_DIR, so a root taken from the
        # environment is a root no pause can ever reach.
        row_kwargs=()),
    # M3 (t_7b55db57): does the machine-wide kanban current board still point
    # at the main orchestrator's board? Bootstrap creates it and switches to it
    # so board-less cards land there, never on a project board; this row flags
    # any later drift (a manual switch, a board migration) with the one-line
    # `hermes kanban boards switch <name>` fix. WARN-only: a wrong current
    # board is work to steer, not a reason to abort an install.
    doctor_rows.FeatureCheckRow(
        key="main_board", seam_kwarg="_main_board_check",
        loader="_load_main_board",
        refusal="_refused_main_board_check",
        fallback_name="_MAIN_BOARD_CHECK_NAME",
        missing_detail="hscc-bootstrap/main_board.py not importable — the "
                       "kanban current board cannot be verified",
        missing_fix="Deploy the hscc-bootstrap plugin next to the doctor "
                    "(re-run bootstrap)."),
    # M5: are the daemon-owned wake-ups enabled and delivering? The row is the
    # doctor half of "skip and log when the pane is missing": it reads the
    # persisted last-result stamp, so a missing main-orchestrator pane says so
    # with the `hscc main start` fix even between two scheduled fires. WARN-only
    # (fatal=False) — wake-up drift is work to steer, never a bootstrap stop.
    doctor_rows.FeatureCheckRow(
        key="main_wakeups", seam_kwarg="_main_wakeups_check",
        loader="_load_main_wakeups",
        refusal="_refused_main_wakeups_check",
        fallback_name="_MAIN_WAKEUPS_CHECK_NAME",
        missing_detail="hscc_daemon/main_wakeups.py not importable — the "
                       "main orchestrator's wake-ups cannot be verified",
        missing_fix="Deploy the hscc_daemon plugin next to hscc-bootstrap "
                    "(re-run bootstrap)."),
    # t_6c4f38f2 (ships 2.15.0): the worker SOUL promises a `[budget]` note and
    # budget_watch.doctor_check verifies the two ctx.register_hook calls that
    # make it fire — but until that row existed nothing in doctor ever CALLED
    # it, so a deploy that lost the wiring (stale payload, hand-patched plugin
    # dir) still read all-green. Warn-only by construction: a missing hook
    # costs the note, never an install. Same fence as the waker row — its
    # default target is the operator's DEPLOYED plugin tree, so inside a pytest
    # process the verdict must be injected, never read. Reads hermes_home (the
    # plugin-tree root), not hscc_dir.
    doctor_rows.FeatureCheckRow(
        key="budget_hook", seam_kwarg="_budget_hook_check",
        row_fn="_budget_hook_check_row", row_kwargs=("hermes_home",)),
    # 2.17.3 (t_ba1fec3e): is a node parked ON PURPOSE, and can the record that
    # says so still be read? Every repair path skips a parked node and the fleet
    # views read it `paused` (#560 overlay, #569 API, t_480cee2d cluster views),
    # but doctor — the one surface an operator runs to ask "why is this machine
    # not serving" — said nothing. A pause is an operator DECISION, so the row is
    # INFO (ok=True: host, reason, since, until) and never WARN, never FAIL: a
    # doctor that calls `hscc node pause` a fault trains the operator to ignore
    # it, and a FATAL here would hard-stop bootstrap over a parked worker. The
    # one loud posture is a file the reader cannot trust (node_pauses.load
    # answers "no pauses" for a corrupt overlay, which is safe for a repair path
    # and dangerous for a report) — WARN, fatal=False.
    # NO row_kwargs, same reason as cron_watchers / kanban_enforce above
    # (t_adb66608, t_be690204): node_pauses has no HSCC_DIR override — its
    # default root is HOME's ~/.hscc, resolved at import, and that IS the root
    # `hscc node pause` writes. A root handed in from the environment would be a
    # directory no pause can ever reach, read back as "nothing is parked". The
    # row takes a root only from an explicit ``directory=`` caller.
    doctor_rows.FeatureCheckRow(
        key="node_pauses", seam_kwarg="_node_pauses_check", row_kwargs=(),
        loader="_load_doctor_node_pauses",
        refusal="_refused_node_pauses_check",
        fallback_name="_NODE_PAUSES_CHECK_NAME",
        missing_detail="hscc-bootstrap/doctor_node_pauses.py not importable — "
                       "parked nodes cannot be told apart from broken ones",
        missing_fix="Deploy the hscc-bootstrap plugin next to the doctor "
                    "(re-run bootstrap)."),
    # t_005e87fd (split out of t_110adc1a): is the daemon actually ENDING an
    # expired pause? `hscc node pause --until 2h` is a promise about the future,
    # and the `node_pause_expiry` stream (60 s, PERIODIC_INTERVALS) is what keeps
    # it — `hscc verify` already covers the stream automatically, but verify says
    # "did the stream run", never "did the operator's schedule get honoured". The
    # stream goes RED for exactly the two states that mean it did not: `kept` (a
    # due pause whose restart failed) and `skipped` (a due pause another pause
    # still holds part of its span). This row is the kanban_enforce half for it
    # (t_51931733): parked/past-due counts + last-pass freshness + the stamped
    # RED named out of the stamp, with fatal RED reserved for the orch-waker
    # class (the daemon runs and this stream has never stamped — the wiring
    # itself), warn for stale / red-tick / predates-build / daemon-down.
    # NO row_kwargs, same reason as kanban_enforce and node_pauses above
    # (t_adb66608, t_be690204): both artefacts this row reads are written by
    # HOME-bound writers with no HSCC_DIR override — the stamp through
    # ``state.STATE_DIR`` and the overlay through ``node_pauses``' own default
    # root — so a root lifted from the environment is a directory no pause can
    # reach, and the row would call a live stream "never stamped" (a FATAL, once
    # a pid file happened to sit there). The feature module keeps a ``root=``
    # caller seam for tests, threaded through all three reads at once.
    doctor_rows.FeatureCheckRow(
        key="node_pause_expiry", seam_kwarg="_node_pause_expiry_check",
        row_kwargs=(),
        loader="_load_node_pause_exec",
        refusal="_refused_node_pause_expiry_check",
        fallback_name="_NODE_PAUSE_EXPIRY_CHECK_NAME",
        missing_detail="hscc_daemon/node_pause_exec.py not importable — an "
                       "expired node pause cannot be told apart from a pause "
                       "the daemon is honouring",
        missing_fix="Deploy the hscc_daemon plugin next to hscc-bootstrap "
                    "(re-run bootstrap)."),
    # t_caf98114 (main-only audit, PR #666): is the cluster CONTROL surface on a
    # home that is not the main orchestrator? Two findings count — a non-main
    # profile whose ``plugins.enabled`` names a cluster TOOL plugin
    # (``hscc-commands``, ``sparkrun-hermes``), and a non-main profile whose
    # skills tree carries an hscc-owned cluster skill. Hook presence never
    # counts (binding ruling R2/D-C): ``hscc-cluster`` must stay enabled in every
    # home or its six hooks die with it, and tools+hooks register in one call.
    # WARN on every verdict, never fatal: the row's red is the fleet's CURRENT
    # posture (the audit measured 26/52 profiles naming the cluster toolsets and
    # bootstrap has never written a per-profile ``plugins.enabled``), and doctor
    # is bootstrap's Stage-1 gate — a fatal here would hard-stop an install over
    # drift the operator is mid-way through fixing. The identity is resolved,
    # never spelled (R2/D-A), through ``main_orch.main_name()``, whose own
    # ``hscc_dir`` seam this row is handed.
    doctor_rows.FeatureCheckRow(
        key="main_only_surface", seam_kwarg="_main_only_surface_check",
        # BOTH contexts, each with a real consumer: hermes_home locates the
        # profile trees (their writers — the installers — honour a named
        # --home/HERMES_HOME, so a handed root is a root a writer can reach,
        # unlike the HOME-only daemon-state rows above: t_be690204), and
        # hscc_dir is main_orch's documented relocation seam for
        # main-orchestrator.json, which decides WHICH home is <main>.
        row_kwargs=("hermes_home", "hscc_dir"),
        loader="_load_doctor_main_only",
        refusal="_refused_main_only_check",
        fallback_name="_MAIN_ONLY_SURFACE_CHECK_NAME",
        missing_detail="hscc-bootstrap/doctor_main_only.py not importable — "
                       "which homes may hold the cluster control surface "
                       "cannot be verified",
        missing_fix="Deploy the hscc-bootstrap plugin next to the doctor "
                    "(re-run bootstrap)."),
    # t_639b2834 (regression alarm for the 2026-11-23 duplicate-orchestrator
    # incident): does any kanban ``notify+wake`` subscription owned by a
    # pane-backed profile point at a session that is NOT that profile's live
    # pane session? 307 of them did exactly that and drove a second copy of the
    # orchestrator for two weeks. The subscribe-side gate (t_31c8b258) prevents
    # new ones; this row catches the posture re-forming, which the gate cannot
    # do on its own — a pane can appear after a profile was provisioned.
    # WARN, never fatal: the row's red is the fleet's CURRENT posture (an
    # operator-owned subscription is a decision to remove, not a broken build)
    # and doctor is bootstrap's Stage-1 gate.
    #
    # ``hermes_home`` ONLY, no ``hscc_dir``: the row resolves a pane in each
    # profile's ``runtime/active_sessions.json``, which lives under the Hermes
    # home the doctor was handed (the same read ``orch_subscribe`` and the
    # budget-hook row do through this one context). It takes no ``hscc_dir``
    # because nothing here reads ``~/.hscc`` — and it must not gain a root for
    # the BOARDS, which are Hermes' and come from Hermes' own enumeration
    # (``autodown.board_db_paths``), never from a caller-chosen path.
    doctor_rows.FeatureCheckRow(
        key="orch_wake_subs", seam_kwarg="_orch_wake_subs_check",
        row_kwargs=("hermes_home",),
        loader="_load_doctor_orch_subs",
        refusal="_refused_orch_wake_subs_check",
        fallback_name="_ORCH_WAKE_SUBS_CHECK_NAME",
        missing_detail="hscc-bootstrap/doctor_orch_subs.py not importable — "
                       "whether any notify+wake subscription points off its "
                       "owner's live pane session cannot be verified",
        missing_fix="Deploy the hscc-bootstrap plugin next to the doctor "
                    "(re-run bootstrap)."),
    # t_8b17cb40 (the half of t_ae87b783 / PR #791 that the emitters cannot
    # cover): is the INSTALLED job actually running the daemon in the Standard
    # posture? The three plist emitters are pinned by
    # hscc_daemon/tests/test_daemon_process_type_standard_t_ae87b783.py, but a
    # pin on an emitter says nothing about the file on the node — and the fleet
    # ran Background until 2026-11-25, a posture any node keeps until it
    # re-bootstraps. While it does, every merge-queue gate leg pays ~7.6x
    # silently (measured `ps -o pri` 4 vs 20, 4.96 M vs 37.7 M it/s through the
    # real merge_queue._run_gate). The row reads the installed plist with
    # plistlib (launchctl print has NO process-type line — measured: its
    # `jetsam priority` is 40 for every posture) and corroborates with the
    # loaded job's `spawn type`, which measured able to tell the postures
    # apart and is the only way to catch a file already fixed but not yet
    # re-bootstrapped.
    # WARN on every non-skip, never fatal (the card's ruling, and the same
    # Stage-1 reasoning as the supervision row): a node may legitimately run
    # another posture, and doctor must not abort an install over that choice.
    # NO row_kwargs (t_be690204): the file this row reads lives under
    # ``~/Library/LaunchAgents`` and is written by install.py /
    # daemon_install.py / launchd-setup.sh, none of which honours HSCC_DIR, so a
    # handed root is a directory no emitter can reach — and a live job would
    # read back as "nothing installed". The feature module keeps a
    # ``plist_path=`` caller seam for tests instead.
    doctor_rows.FeatureCheckRow(
        key="process_posture", seam_kwarg="_process_posture_check",
        row_kwargs=(),
        loader="_load_doctor_process_posture",
        refusal="_refused_process_posture_check",
        fallback_name="_PROCESS_POSTURE_CHECK_NAME",
        missing_detail="hscc-bootstrap/doctor_process_posture.py not importable "
                       "— the scheduling posture of the installed daemon job "
                       "cannot be verified",
        missing_fix="Deploy the hscc-bootstrap plugin next to the doctor "
                    "(re-run bootstrap)."),
]

#: The seam keywords the registry owns. run_doctor() validates its
#: **feature_rows against this, so a typo'd seam cannot silently downgrade a
#: row to its refusal verdict with nobody noticing.
_FEATURE_SEAM_NAMES = doctor_rows.seam_names(FEATURE_CHECK_ROWS)


def run_doctor(hermes_home: Optional[str] = None, *, _cluster_runner=None,
               _http_get=None, _serving_raw=None, _venv_python=None,
               _lan_probe=None, _cluster_default_runner=None,
               _status_runner=None, fleet_checks=None, _ssh_factory=None,
               hscc_dir: Optional[str] = None, _role_soul_scan=None,
               _orch_waker_check=None, _cron_watchers_check=None,
               _supervision_check=None, _kanban_enforce_check=None,
               _budget_hook_check=None, **feature_rows) -> dict:
    """Run all checks.

    Fleet checks (lan reachability / cluster-vs-serving / serving
    consistency) shell out to sparkrun and TCP-probe the fleet — READ-ONLY,
    but still fleet I/O. ``fleet_checks=True`` forces them, False skips them
    (reported as skipped); the default (None) auto-detects: they run in a
    human CLI process and skip inside a pytest process, so the test suite can
    never accidentally probe the live fleet by calling run_doctor(). A test
    that WANTS them passes fleet_checks=True plus the stub seams.

    ``_ssh_factory`` / ``hscc_dir`` are the node-recovery check's seams: the
    per-node ssh transport (production: sparkrun's own remote execution) and
    the dir its ``cluster.json`` opt-out block is read from. Both are injected
    in tests so no doctor test can reach a node.

    ``_role_soul_scan`` is the role-SOUL identity check's seam (t_c823ebc9): a
    zero-arg callable returning the Check-shaped dict. Inside a pytest process
    the scan MUST be injected, exactly like ``_ssh_factory`` — its default
    target reads SOUL.md under the live profiles root, which in the operator's
    env is real profile state. Uninjected inside a suite it renders as an ERROR
    naming the missing seam, never as a pass.

    Feature rows (orch-waker, cron watchers, supervision, kanban-enforce,
    budget hook, …) are wired by ``FEATURE_CHECK_ROWS`` below, not by this
    signature: the keywords above are the rows that existed before the registry
    and stay for compatibility. A NEW row's seam arrives through
    ``**feature_rows`` and is validated against the registry, so run_doctor()
    never needs editing again (an unknown keyword is a TypeError rather than a
    silently ignored seam).
    """
    if fleet_checks is None:
        fleet_checks = not doctor_rows.under_pytest()
    home = (hermes_home or os.environ.get("HERMES_HOME")
            or os.path.expanduser("~/.hermes"))
    checks: List[Check] = [
        _python_ok(),
        _pyyaml_ok(),
        _sparkrun_ok(),
        _sparkrun_cluster_ok(_cluster_runner),
        _hermes_ok(home),
        # _nas_ok also shells out to `sparkrun cluster list`, so the injected
        # runner has to reach it too — otherwise every run_doctor() call in the
        # suite pays a real 15s subprocess against the live cluster config.
        _nas_ok(_cluster_runner),
        _disk_ok(os.path.expanduser("~")),
        _gateway_running(),
        # Skill-tree integrity (card t_bc611809): 'Unknown skill(s) on a
        # spawn' is a per-profile resolver state (collision or absence) that
        # nothing else surfaces — the dispatcher force-injects sdlc-review
        # for review-lane spawns, so one bad profile silently kills review
        # cards. Non-fatal (bootstrap must not die over a skills tree the
        # operator has to adjudicate), reported with every colliding path.
        _skills_duplicates_ok(home),
        _reviewer_readiness_ok(home),
        # Models-served is non-fatal (warning): drift to fix, not a reason to
        # hard-stop. It probes each distinct endpoint once; a probe failure is
        # reported as ok (no false alarm), so it never flips preflight `ok`.
        _check_models_served(home, _http_get=_http_get),
    ]
    # The feature rows (orch-waker, cron watchers, supervision, kanban-enforce,
    # budget hook) come from FEATURE_CHECK_ROWS right before run_doctor(), in
    # registry order — the same position and the same relative order they had
    # when each was a literal entry in the list above. Each keeps its own row
    # function, its own pytest fence, and its own seam; only the wiring moved
    # here (t_32a06db9).
    #
    # The pre-registry seams stay explicit keywords for compatibility with the
    # suites that inject them, and are folded into the same mapping a new row's
    # seam arrives through. An unknown keyword is rejected instead of ignored,
    # so a typo'd seam can never silently downgrade a row to its refusal verdict.
    seams = {
        "_orch_waker_check": _orch_waker_check,
        "_cron_watchers_check": _cron_watchers_check,
        "_supervision_check": _supervision_check,
        "_kanban_enforce_check": _kanban_enforce_check,
        "_budget_hook_check": _budget_hook_check,
    }
    seams.update(feature_rows)
    unknown = sorted(set(seams) - _FEATURE_SEAM_NAMES)
    if unknown:
        raise TypeError(
            f"run_doctor() got unexpected feature-row seam(s) {unknown}; the "
            f"registry knows {sorted(_FEATURE_SEAM_NAMES)}. Add a "
            f"FeatureCheckRow for a new row — do not add a keyword here.")
    # hscc_dir comes from the environment (the doctor_notifier_mask convention):
    # bootstrap's sandboxed tests keep HOME real and relocate only HSCC_DIR, and
    # doctor runs there as the Stage-1 gate. hermes_home is the deployed
    # plugin-tree root the budget-hook row reads.
    checks += doctor_rows.render_rows(
        sys.modules[__name__], FEATURE_CHECK_ROWS, seams,
        hscc_dir=os.environ.get("HSCC_DIR"), hermes_home=home)
    if fleet_checks:
        # A pytest process must never acquire sparkrun's production ssh
        # transport, even when a test forces fleet_checks=True: the transport
        # has to be injected. Without it the node-recovery check renders as an
        # ERROR naming the missing seam — an honest "cannot verify", never a
        # silent skip and never a real ssh attempt against the live fleet.
        def _refused_transport(_host):
            def refused(_cmd, _timeout):
                return {"ok": False, "stdout": "", "code": None,
                        "stderr": "production ssh transport refused inside a "
                                  "pytest process — inject _ssh_factory"}
            return refused
        nr_factory = _ssh_factory or (
            _refused_transport if doctor_rows.under_pytest() else None)
        nr_res = doctor_node_recovery.check(
            _node_hosts(_cluster_runner), ssh_factory=nr_factory,
            hscc_dir=hscc_dir)
        # Same fence for the role-SOUL audit: it reads SOUL.md files under
        # rolelib.PROFILES_DIR, which in the operator's env is the LIVE profile
        # root. A pytest process must never read it, so inside a suite the scan
        # has to be injected (fixture dirs); without it the check renders as an
        # ERROR naming the missing seam — an honest "cannot verify", never a
        # silent pass and never a real read of the operator's profiles.
        rs_scan = _role_soul_scan or (
            _refused_role_soul_scan if doctor_rows.under_pytest()
            else doctor_role_soul.check)
        rs_res = rs_scan()

        # Only the five Check fields go into the list, so every entry of
        # `checks` — and therefore `doctor --json` — keeps one shape. The
        # per-node rows stay available to callers of
        # doctor_node_recovery.check() (the CLI, and the tests).
        checks += [
            Check(name=nr_res["name"], ok=nr_res["ok"], detail=nr_res["detail"],
                  fix=nr_res["fix"], fatal=nr_res["fatal"]),
            # Role-SOUL identity drift (t_c823ebc9): WARNING, never fatal — a
            # profile running under another role's identity is a loud red line
            # to fix, not a reason to hard-stop an install.
            Check(name=rs_res["name"], ok=rs_res["ok"], detail=rs_res["detail"],
                  fix=rs_res["fix"], fatal=rs_res["fatal"]),
            _sparkrun_lan_reachability(_venv_python=_venv_python,
                                       _probe=_lan_probe,
                                       _serving_raw=_serving_raw),
            _sparkrun_default_cluster_matches_serving(
                _runner=_cluster_default_runner, _serving_raw=_serving_raw),
            _serving_consistency(_status_runner=_status_runner,
                                 _serving_raw=_serving_raw),
        ]
    else:
        checks.append(Check("sparkrun lan reachability", True,
                            detail="skipped (fleet checks off)", fatal=False))
        # Same honesty for the node-recovery check: a skipped check prints as
        # skipped, never as "armed/ok" and never as absence-of-output.
        checks.append(Check(doctor_node_recovery.CHECK_NAME, True,
                            detail="skipped (fleet checks off)", fatal=False))
        # ...and the same for role-SOUL identity, so a suite run prints an
        # explicit skipped line instead of quietly not auditing anything. The
        # name comes through check_name() so the skipped line can never print a
        # different key than the real check would have used.
        checks.append(Check(doctor_role_soul.check_name(), True,
                            detail="skipped (fleet checks off)", fatal=False))
    # snap firmware-notifier mask (card t_61850b24) — its OWN check, kept in a
    # self-contained block so it cannot collide with the sibling node-recovery
    # check's wiring above. Same honesty rule as that check and as the install
    # stage: a node we could not read is an ERROR, never ok, never a skip; a
    # skipped check says it was skipped instead of vanishing.
    #
    # No new run_doctor() parameter is needed: the check resolves its own
    # transport through doctor_notifier_mask._default_ssh_factory, which REFUSES
    # to produce one inside a pytest process, so the suite can never ssh the
    # live fleet by calling run_doctor(). A test injects its transport by
    # calling doctor_notifier_mask.check(ssh_factory=...) directly.
    if fleet_checks:
        try:
            import doctor_notifier_mask
        except ImportError:
            checks.append(Check(_NOTIFIER_CHECK_NAME, False,
                                detail=f"doctor_notifier_mask.py missing from "
                                       f"{os.path.dirname(os.path.abspath(__file__))} "
                                       f"— the mask cannot be verified",
                                fix="Restore hscc-bootstrap/doctor_notifier_mask.py "
                                    "(re-run bootstrap); the notifier mask is "
                                    "currently unverifiable.",
                                fatal=False))
        else:
            nm_res = doctor_notifier_mask.check(
                _node_hosts(_cluster_runner),
                hscc_dir=os.environ.get("HSCC_DIR"))
            # Only the five Check fields enter the list, so `doctor --json`
            # keeps one shape; per-node rows stay available to callers of
            # doctor_notifier_mask.check().
            checks.append(Check(name=nm_res["name"], ok=nm_res["ok"],
                                detail=nm_res["detail"], fix=nm_res["fix"],
                                fatal=nm_res["fatal"]))
    else:
        try:
            import doctor_notifier_mask
            _nm_name = doctor_notifier_mask.CHECK_NAME
        except ImportError:
            _nm_name = _NOTIFIER_CHECK_NAME
        checks.append(Check(_nm_name, True, detail="skipped (fleet checks off)",
                            fatal=False))
    # GPU clock cap — same shape as the notifier block: own module, own check,
    # the transport refuses to exist inside pytest (inject via
    # install_gpu_clock_cap.doctor_check(ssh_factory=...)).
    try:
        import install_gpu_clock_cap
    except ImportError:
        checks.append(Check("GPU clock cap (node-recovery)", not fleet_checks,
                            detail=("install_gpu_clock_cap.py missing — the cap cannot "
                                    "be verified" if fleet_checks
                                    else "skipped (fleet checks off)"),
                            fix="Restore hscc-bootstrap/install_gpu_clock_cap.py (re-run bootstrap).",
                            fatal=False))
    else:
        if fleet_checks:
            gc = install_gpu_clock_cap.doctor_check(_node_hosts(_cluster_runner))
            checks.append(Check(name=gc["name"], ok=gc["ok"], detail=gc["detail"],
                                fix=gc["fix"], fatal=gc["fatal"]))
        else:
            checks.append(Check(install_gpu_clock_cap.CHECK_NAME, True,
                                detail="skipped (fleet checks off)", fatal=False))
    fatal_failed = [c for c in checks if not c.ok and c.fatal]
    return {
        "ok": not fatal_failed,
        "checks": [asdict(c) for c in checks],
        "fatal_failures": [c.name for c in fatal_failed],
    }


# ---------------------------------------------------------------------------
# Card D: one-time CONCRETE → alias conversion for live configs.
# See module docstring. Conservative endpoint-based mapping plus a SAFETY
# GATE: an entry is converted ONLY after the endpoint is probed (`/models`,
# reusing `_models_url` + Bearer auth from the "models served" check above,
# via `_probe_served_models`) and confirmed to serve the target alias.
# ---------------------------------------------------------------------------

# Stable logical aliases the serving layer advertises (Card A1).
ORCHESTRATOR_ALIAS = "orchestrator-model"
WORKER_ALIAS = "worker-model"

# Endpoint discriminators — mirror enable_plugins / generator topology:
#   orchestrator = loopback/local at the orch vLLM port, or the known orch host.
#   worker       = loopback/proxy at :4000 (sparkrun LiteLLM load-balancer).
_ORCH_HOST = "192.0.2.244"
_ORCH_PORT = 8000
_WORKER_HOST = "192.0.2.245"
_WORKER_PORT = 4000


def _classify_endpoint(base_url):
    """Classify a base_url as 'orchestrator', 'worker', or None (indeterminate).

    Conservative: only unambiguous loopback / localnode / known-cluster-host
    matches resolve. Any remote/cloud base_url (e.g. api.openai.com,
    api.anthropic.com) or anything we cannot clearly attribute returns None, so
    the migration never guesses an endpoint.
    """
    if not base_url or not isinstance(base_url, str):
        return None
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base_url.strip())
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except Exception:
        return None
    if not host or port is None:
        return None

    if host in ("localhost", "127.0.0.1"):
        if port == _ORCH_PORT:
            return "orchestrator"
        if port == _WORKER_PORT:
            return "worker"
        return None
    if host == _ORCH_HOST:
        return "orchestrator" if port == _ORCH_PORT else None
    if host == _WORKER_HOST:
        return "worker" if port == _WORKER_PORT else None
    return None


def _probe_served_models(base_url, api_key=None, *, _http_get=None):
    """Probe an endpoint's ``/models`` and report what it serves.

    Reuses the same probe machinery as the ``models served`` doctor check
    (``_models_url`` for URL normalization preserving ``/v1``, and
    ``_http_get`` / ``_http_get_default`` for the Bearer-authenticated GET) -
    endpoint probing is NOT re-implemented here.

    Returns ``(status, served_ids)`` where ``status`` is one of:
      - ``"ok"``          - endpoint reachable and ``/models`` parsed; ``served_ids``
                            is the list of served model ids (possibly empty).
      - ``"unreachable"`` - network/timeout; nothing could be verified.
      - ``"error"``       - endpoint reachable but ``/models`` returned a non-2xx
                            HTTP status or an unparseable body (probe/config
                            problem).

    ``_http_get(url, api_key=None) -> str`` is injectable for tests (mirrors
    ``_check_models_served``) so the gate can be exercised without the network.
    """
    getter = _http_get or _http_get_default
    url = _models_url(base_url)
    if not url:
        return ("error", [])
    import json
    from urllib.error import HTTPError
    try:
        raw = getter(url, api_key=api_key)
    except HTTPError as exc:
        return ("error", [f"{url} returned HTTP {exc.code} on /models probe"])
    except Exception as exc:
        return ("unreachable", [f"{url} unreachable ({exc})"])
    try:
        data = json.loads(raw)
        served = [m.get("id") for m in (data.get("data") or [])
                  if isinstance(m, dict) and m.get("id")]
    except Exception as exc:
        return ("error", [f"{url} body not parseable as /models JSON ({exc})"])
    return ("ok", served)


def _convert_orchestrator_ids_to_alias(cfg, alias=ORCHESTRATOR_ALIAS,
                                       worker_alias=WORKER_ALIAS, *,
                                       _http_get=None,
                                       refused=None) -> list[str]:
    """Convert concrete model ids pointing at orch/worker endpoints to aliases.

    SAFETY GATE (Card D revision): an entry is ONLY converted after the endpoint
    is probed and the target alias is confirmed present in the served ``/models``
    list. This directly guards the failure mode that caused the 404 incident -
    converting to an alias the serving layer does not actually resolve.

    Per entry:
      - endpoint is remote/indeterminate (see ``_classify_endpoint``) -> left
        untouched, never guessed (no probe needed).
      - endpoint reachable and serves the alias -> model id set to the alias.
      - endpoint reachable but the alias is ABSENT from the served list -> NOT
        converted, and reported loudly on ``refused`` (which key/endpoint and
        what IS served).
      - endpoint unreachable -> NOT converted, reported loudly (never convert
        blind).

    Returns the list of changed key paths (e.g. ``["model.default"]``).
    Already-alias values are skipped (idempotent). ``cfg`` is mutated in place;
    callers write back (with backup) only if the returned list is non-empty.
    ``_http_get`` is injectable for tests; ``refused`` (a list) collects the
    loud skip reports when provided.
    """
    changed: list[str] = []
    if refused is None:
        refused = []
    if not isinstance(cfg, dict):
        return changed

    # Probe results keyed by base_url so several entries sharing one endpoint
    # do not re-hit the network. Each value is (status, served_ids, detail).
    _cache: dict = {}

    def gate(pair, key_path, want):
        """Probe-gate one candidate. Returns True only if we may convert."""
        base_url = pair.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            refused.append(
                f"! NOT CONVERTED {key_path}: no base_url; cannot probe endpoint")
            return False
        if base_url not in _cache:
            status, served = _probe_served_models(
                base_url, api_key=pair.get("api_key"), _http_get=_http_get)
            _cache[base_url] = (status, served)
        status, served = _cache[base_url]
        if status == "unreachable":
            refused.append(
                f"! NOT CONVERTED {key_path}: endpoint {base_url} unreachable "
                "- refusing to convert blind")
            return False
        if status == "error":
            refused.append(
                f"! NOT CONVERTED {key_path}: probe of {base_url} failed "
                "(reachable but /models problem); refusing to convert")
            return False
        if want not in served:
            served_str = ", ".join(served) if served else "(none reported)"
            refused.append(
                f"! NOT CONVERTED {key_path}: alias '{want}' NOT served by "
                f"{base_url} - served: {served_str}; refusing to convert "
                "(would produce a 404). Operator must resolve before migrate.")
            return False
        return True

    def convert_pair(pair, key_path, model_key, want):
        """Probe-gate and (if safe) convert one model/base_url pair in place."""
        if not isinstance(pair, dict):
            return False
        model = pair.get(model_key)
        if not isinstance(model, str) or not model:
            return False
        if model in (alias, worker_alias):
            return False  # already an alias - idempotent
        if not gate(pair, key_path, want):
            return False
        pair[model_key] = want
        changed.append(key_path)
        return True

    def _treat(pair, orch_kind, alias, worker_alias):
        """Resolve the target alias for ``pair``, or None if indeterminate.

        Returns the alias when the paired base_url is UNAMBIGUOUSLY the
        orchestrator or worker proxy. Returns None for a remote/cloud or
        otherwise indeterminate endpoint — such entries are never probed and
        never guessed at (operator-chosen endpoints survive untouched).
        """
        kind = _classify_endpoint(pair.get("base_url"))
        if kind is None:
            return None
        return alias if kind == orch_kind else worker_alias

    # Root model block - orchestrator-pointing in a live fleet.
    root_m = cfg.get("model")
    if isinstance(root_m, dict) and "default" in root_m:
        want = _treat(root_m, "orchestrator", alias, worker_alias)
        if want is not None:
            convert_pair(root_m, "model.default", "default", want)

    # Delegation - worker proxy usually.
    dlg = cfg.get("delegation")
    if isinstance(dlg, dict) and "model" in dlg:
        want = _treat(dlg, "orchestrator", alias, worker_alias)
        if want is not None:
            convert_pair(dlg, "delegation.model", "model", want)

    # fallback_providers[] - each may point at orch or worker.
    fps = cfg.get("fallback_providers")
    if isinstance(fps, list):
        for i, fp in enumerate(fps):
            if not isinstance(fp, dict):
                continue
            want = _treat(fp, "orchestrator", alias, worker_alias)
            if want is not None:
                convert_pair(fp, f"fallback_providers[{i}].model", "model",
                             want)

    # Every auxiliary.<task> block (compression + text aux + vision/web_extract).
    aux_root = cfg.get("auxiliary")
    if isinstance(aux_root, dict):
        for task, aux in aux_root.items():
            if not isinstance(aux, dict):
                continue
            want = _treat(aux, "orchestrator", alias, worker_alias)
            if want is not None:
                convert_pair(aux, f"auxiliary.{task}.model", "model", want)

    return changed


def _walk_path(cfg, path):
    """Resolve a key path in ``cfg``; supports '.' and '[i]' (e.g.
    'fallback_providers[0].model', 'auxiliary.compression.model')."""
    import re
    # Tokenize "fallback_providers[0].model" -> ["fallback_providers", "0", "model"]
    tokens = [
        t for t in re.split(r"[.\[]", path.replace("]", "")) if t != ""
    ]
    cur = cfg
    for t in tokens:
        if isinstance(cur, dict):
            if t not in cur:
                return None
            cur = cur[t]
        elif isinstance(cur, list):
            try:
                cur = cur[int(t)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _write_yaml(config_path, cfg):
    """Backup + write ``cfg`` to ``config_path`` (mirror enable_plugins).

    Uses the shared backup helper so doctor's backups are atomic (never a
    truncated ``.bak``) and retain-limited like every other HSCC writer
    (t_9462260b) — the old ``shutil.copy`` here is what left 26
    ``config.yaml.bak-*`` plus a zero-byte rollback point behind.
    """
    import yaml
    backup_util.backup_file(config_path)
    with open(config_path, "w") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False, default_flow_style=False)


def run_doctor_fix(config_path: Optional[str] = None,
                   hermes_home: Optional[str] = None,
                   *, _cluster_runner=None, _http_get=None) -> dict:
    """Run doctor + fix all non-fatal HSCC config drift.

    Reads the current config, runs checks, then calls enable_plugins.enable()
    if there are non-fatal failures. Reports what was wrong and what was fixed.

    Returns the same dict as run_doctor() plus "fixes_applied" list describing
    each corrected key and "alias_conversion_refused" (the loud safety-gate skip
    reports from the Card D migration). ``_http_get`` is injectable for tests
    (mirrors ``_check_models_served``) so the endpoint probe never needs the
    real network in the test suite.
    """
    checks_result = run_doctor(hermes_home, _cluster_runner=_cluster_runner,
                               _http_get=_http_get)
    fixes_applied: list[str] = []
    alias_conversion_refused: list[str] = []

    has_nonfatal_failures = any(
        not c["ok"] and not c.get("fatal") for c in checks_result["checks"]
    )

    if config_path:
        # Capture pre-fix snapshot for drift reporting
        snapshot = {}
        try:
            import yaml
            if os.path.exists(config_path):
                with open(config_path) as fh:
                    snapshot = yaml.safe_load(fh) or {}
        except Exception:
            snapshot = {}

        # Reconcile config via enable_plugins (idempotent, preserves operator caps)
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from enable_plugins import enable as _enable
        result = _enable(config_path)

        # Read post-fix config for drift reporting
        fixed_cfg = {}
        try:
            import yaml
            with open(config_path) as fh:
                fixed_cfg = yaml.safe_load(fh) or {}
        except Exception:
            pass

        # Build "was X -> set Y" report for each changed key
        for section, keys in result.items():
            # `result` sections are lists of config key names; `hooks_file`
            # (the hook-script install status dict) is surfaced alongside but
            # is not a config section — skip it. `goal_judge_profiles` is a
            # list of PROFILE FILE paths the sweep wired (t_proxy_outage_20261011),
            # not keys of THIS config — reported separately below.
            if not isinstance(keys, list) or section == "goal_judge_profiles":
                continue
            for k in keys:
                old_val = _get_nested(snapshot, section, k)
                new_val = _get_nested(fixed_cfg, section, k)
                if old_val is None or old_val == "":
                    fixes_applied.append(
                        f"{section}/{k}: was missing -> set {new_val}"
                    )
                else:
                    fixes_applied.append(
                        f"{section}/{k}: was {old_val} -> set {new_val}"
                    )

        for prof in result.get("goal_judge_profiles") or []:
            fixes_applied.append(f"auxiliary.goal_judge wired in profile file {prof}")

        # Card D: one-time CONCRETE → alias migration. Runs on the reconciled
        # config only; never touches ~/.hermes directly (config_path is the
        # sole target). SAFETY GATE: an entry is converted ONLY when the
        # endpoint is probed and confirmed to serve the alias (see
        # _convert_orchestrator_ids_to_alias). Concrete ids pointing
        # unambiguously at the orchestrator or worker proxy are replaced with
        # the alias; remote/cloud endpoints are left untouched; endpoints that
        # are unreachable or do not serve the alias are left UNCONVERTED and
        # reported loudly on alias_conversion_refused.
        try:
            import yaml
            with open(config_path) as fh:
                cur_cfg = yaml.safe_load(fh) or {}
        except Exception:
            cur_cfg = {}
        if isinstance(cur_cfg, dict):
            pre_cfg = copy.deepcopy(cur_cfg)
            alias_changes = _convert_orchestrator_ids_to_alias(
                cur_cfg, _http_get=_http_get, refused=alias_conversion_refused)
            for path in alias_changes:
                old_val = _walk_path(pre_cfg, path)
                new_val = _walk_path(cur_cfg, path)
                fixes_applied.append(
                    f"{path}: was {old_val} -> set {new_val}"
                )
            if alias_changes:
                _write_yaml(config_path, cur_cfg)

    return {**checks_result, "fixes_applied": fixes_applied,
            "alias_conversion_refused": alias_conversion_refused}


def _get_nested(cfg: dict, section: str, key: str):
    """Safely extract cfg[section][key] for drift reporting."""
    try:
        val = cfg.get(section, {})
        if isinstance(val, dict):
            val = val.get(key)
        if val is None:
            return None
        if isinstance(val, (dict, list)):
            return str(val)
        return val
    except Exception:
        return None


def _home_from_argv(argv) -> str:
    """``--home PATH`` wins over ambient HERMES_HOME.

    One explicit home is then threaded into *both* the config path and the
    hermes check, so the two can never disagree and a caller (or a test) is not
    at the mercy of whatever HERMES_HOME the surrounding process happens to
    carry. Without this, `main(["--fix"])` reads and WRITES
    $HERMES_HOME/config.yaml no matter what the caller intended.
    """
    for i, tok in enumerate(argv):
        if tok == "--home" and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith("--home="):
            return tok.split("=", 1)[1]
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def main(argv=None) -> int:
    import json
    argv = argv if argv is not None else sys.argv[1:]
    home = _home_from_argv(argv)

    if "--skills" in argv:
        # Skills-only fast mode (card t_bc611809): the two skill findings and
        # nothing else — no sparkrun, no fleet probes. Deterministic and
        # read-only, so the orchestrator can re-run acceptance against any
        # --home (e.g. a fabricated tmp tree) without a live cluster.
        out = {
            "home": _skills_home(home),
            "skill_duplicates": asdict(_skills_duplicates_ok(home)),
            "reviewer_readiness": asdict(_reviewer_readiness_ok(home)),
        }
        print(json.dumps(out, indent=2))
        return 0 if (out["skill_duplicates"]["ok"]
                     and out["reviewer_readiness"]["ok"]) else 1

    fix_mode = "--fix" in argv
    config_path = os.path.join(os.path.expanduser(home), "config.yaml")

    if fix_mode:
        res = run_doctor_fix(config_path=config_path, hermes_home=home)
    else:
        res = run_doctor(home)

    if "--json" in argv:
        print(json.dumps(res, indent=2))
        return 0 if res["ok"] else 1

    # Human view — themed Rich (make_console degrades to plain on a non-tty).
    _console = _theme.make_console()
    lines: list[str] = []
    for c in res["checks"]:
        mark = "✓" if c["ok"] else ("✗" if c["fatal"] else "○")
        line = f"  {mark} {c['name']}: {c['detail']}"
        if not c["ok"] and c["fix"]:
            line += f"\n      → {c['fix']}"
        lines.append(line)

    # Print fixes if we ran in --fix mode
    if fix_mode and res.get("fixes_applied"):
        lines.append("")
        lines.append("  🛠 Applied fixes:")
        for fix_line in res["fixes_applied"]:
            lines.append(f"    {fix_line}")

    # Print safety-gate refusals LOUDLY (Card D): entries we deliberately did
    # NOT convert because the endpoint did not serve the alias or was
    # unreachable. These need operator attention before re-running --fix.
    if fix_mode and res.get("alias_conversion_refused"):
        lines.append("")
        lines.append("  ⚠ ALIAS MIGRATION SKIPPED (safety gate):")
        for line in res["alias_conversion_refused"]:
            lines.append(f"    {line}")

    _console.print(_theme.panel("doctor", "\n".join(
        _theme.escape(line) for line in lines)))

    if not res["ok"]:
        _console.print(_theme.panel(
            "preflight",
            "[error]" + _theme.escape(
                "✗ FAILED: " + ", ".join(res['fatal_failures'])) + "[/error]",
        ))
        return 1
    _console.print(_theme.panel(
        "preflight", "[ok]" + _theme.escape("✓ OK") + "[/ok]"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
