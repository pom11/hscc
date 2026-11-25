"""Doctor coverage for the main-only cluster surface (card t_caf98114).

One check, answering the question the audit (`docs/audits/2026-11-23-main-only-
surface.md`, PR #666) could only measure by hand over 52 profile homes: **is the
cluster control/monitor surface sitting on a home that is not the main
orchestrator?** Two surfaces count:

* the per-home ``plugins.enabled`` list naming a cluster TOOL plugin
  (``hscc-commands`` slash verbs, ``sparkrun-hermes``'s ``sparkrun_exec``
  passthrough), and
* the profile's own skills tree carrying an hscc-owned cluster skill
  (``hscc``, ``hscc-cluster``, ``hscc-model-onboard``) — the skill that
  documents a toolset the profile cannot call is "a lie in the prompt"
  (audit §5.3 row 6).

What deliberately does NOT count as drift
-----------------------------------------
**Hook presence (binding ruling R2/D-C).** ``hscc-cluster`` must stay enabled in
every home: its six hooks (``kanban_task_claimed``/``_blocked``/``_completed``,
``pre_tool_call``, ``post_api_request``, ``transform_tool_result``,
``hscc-cluster/__init__.py:135-167``) all serve a worker or a dispatcher acting
for a worker, and Hermes registers tools and hooks in the SAME ``register()``
call — a home that does not load the plugin loses the hooks too, and "a disabled
plugin's hooks are silently dead, not degraded" (audit §3.1). So this check names
only the *other* two plugins. The companion test that keeps ``hscc-cluster``
registering its hooks on a non-main home is the folded hook-site pin in
``hscc_daemon/tests/test_hook_registration_sites_t_caf98114.py`` (R6).

**On-disk plugin trees.** ``install_payload_profiles`` deploys the whole payload
into every allowlisted profile home, and the hooks need the payload there, so a
``plugins/hscc-commands`` DIRECTORY on a worker is not drift. Enabled-ness is the
authority: the loader turns an unlisted user plugin into a
``not enabled in config`` placeholder (audit §5.2, run against production).

**The root home.** ``<hermes_home>/config.yaml`` is the operator's interactive
``hermes``, the gateway and the kanban dispatcher. Driver ruling 3 keeps the
hooks plus the ``hscc-commands`` operator slash verbs there, and the audit's
accepted shape (§6) lists all three plugins for the root "unchanged". The card's
subject is the fleet's *profiles*; the root's skills tree is also the bundled
seed Hermes itself maintains. Neither half of this check reads the root.

**A top-level ``toolsets:`` entry naming a cluster toolset.** Audit §6 rule 2:
removing those is "cosmetic, not a control" — they are not what gates the tools.

The main profile's NAME comes from ``hscc-roles/main_orch.main_name()`` and is
never spelled here (binding ruling R2/D-A); the fallback for an install where
bootstrap has not stored one belongs to the resolver, not to this module.

Fence
-----
The row's real target is the operator's ``~/.hermes`` trees. Inside a pytest
process with no injected seam this returns a refusal ERROR naming the seam — the
same shape as ``doctor_node_pauses.doctor_check`` — and ``doctor.py`` applies the
same fence one level up through the registry, so a suite can neither read the
live install nor decide its assertions from it.

READ-ONLY: this module opens config/skill files and nothing else. The repair is
bootstrap's (card t_e98c6ad4 writes the per-home ``plugins.enabled`` shape) and
the skills mover's (card t_b52e70ba), and the fix text says so.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

# The in-suite fence is ONE predicate, shared with the row registry: see
# ``doctor_rows.under_pytest`` for why the module-local test is not enough.
import doctor_rows  # flat sibling, next to this module (hscc-bootstrap/)

#: The row's printed name. Kept here (not only in doctor.py) so the verdict and
#: the row cannot drift; doctor.py carries a fallback constant for the case where
#: this module itself is not importable, and a test pins the two equal.
CHECK_NAME = "main-only cluster surface (cluster tools/skills off <main>)"

#: The ONE cluster plugin whose presence on a non-main home is required, because
#: its six hooks fire there (R2/D-C). Never reported by this check.
HOOK_ONLY_PLUGIN = "hscc-cluster"

#: Fallbacks used only when the declaration cannot be read from the module that
#: owns it. Both are pinned equal to the real declaration by
#: ``test_doctor_main_only_surface.py``, so neither can drift silently — the same
#: convention as the ``_CHECK_NAME`` fallbacks in doctor.py.
_FALLBACK_TOOL_PLUGINS = ("hscc-commands", "sparkrun-hermes")
_FALLBACK_CLUSTER_SKILLS = ("hscc", "hscc-cluster", "hscc-model-onboard")

#: Findings rendered per detail line before the rest is counted instead of
#: spelled out. A row that prints 40 findings is a row nobody reads; the count
#: is what tells the operator the sweep was not one bad home.
_MAX_RENDERED = 6

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_ROLES_DIR = os.path.join(_REPO_ROOT, "hscc-roles")
_SKILLS_INSTALLER = os.path.join(_REPO_ROOT, "hscc-skills", "hscc.py")


# ── the two declarations this check refuses to duplicate ────────────────────

def cluster_tool_plugins() -> Tuple[str, ...]:
    """The cluster plugins whose ENABLEMENT on a non-main home is drift.

    Read from ``enable_plugins.HSCC_PLUGINS`` — the installer's own list, the
    thing bootstrap writes — minus :data:`HOOK_ONLY_PLUGIN`. A literal here would
    go stale the day the payload gains a cluster plugin, which is the drift class
    this whole row exists to catch.
    """
    try:
        import enable_plugins  # noqa: PLC0415 - flat sibling of hscc-bootstrap
        names = tuple(enable_plugins.HSCC_PLUGINS)
    except Exception:  # noqa: BLE001 - any failure falls back to the pinned pair
        return _FALLBACK_TOOL_PLUGINS
    return tuple(n for n in names if n != HOOK_ONLY_PLUGIN) or \
        _FALLBACK_TOOL_PLUGINS


def cluster_skills() -> Tuple[str, ...]:
    """The hscc-owned cluster skills: ``<main>`` only (driver ruling 4).

    Parsed — not imported — out of ``hscc-skills/hscc.py``, the installer that
    owns the skill list. Importing the deployed installer would run its
    module-level env reads inside the agent host for no benefit, and a second
    literal list here would let doctor and the installer disagree about what is
    hscc's. ``BUNDLED_SKILLS`` as a whole is NOT the answer: most of it
    (``brainstorming``, ``caveman``, ``sdlc-review``, the devops group) is generic
    Hermes material that belongs on every profile, so flagging it would call the
    fleet's healthy skill trees drift.

    Returns the fallback pair-of-three when the installer is not deployed next to
    bootstrap; :func:`doctor_check` reports that as "cannot verify", never as a
    pass.
    """
    names = _parse_tuple(_SKILLS_INSTALLER, "CLUSTER_SKILLS")
    if names is None:
        # The install-template path is the other place the file exists.
        for cand in _installer_candidates():
            names = _parse_tuple(cand, "CLUSTER_SKILLS")
            if names:
                break
    return names or _FALLBACK_CLUSTER_SKILLS


def _installer_candidates() -> List[str]:
    """Other plausible locations of the skills installer, best first."""
    out = []
    plugins_env = os.environ.get("PLUGINS")
    if plugins_env:
        out.append(os.path.join(os.path.expanduser(plugins_env),
                                "hscc-skills", "hscc.py"))
    out.append(os.path.join(os.path.expanduser("~"), ".hermes", "plugins",
                            "hscc-skills", "hscc.py"))
    return out


def _parse_tuple(path: str, const: str) -> Optional[Tuple[str, ...]]:
    """The string tuple ``const`` declares at the top level of *path*, or None.

    One regex serves both shapes the repo uses — a one-line
    ``X = ("a", "b")`` and the multi-line ``X = (\\n    "a",\\n)`` — because
    ``[^)]`` cannot run past the closing paren and ``re.M`` pins the name to the
    start of a line, so a comment or an f-string that merely mentions the
    constant cannot be mistaken for the declaration.
    """
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return None
    block = re.search(rf"^{const}\s*=\s*\(([^)]*)\)", src, re.M)
    if not block:
        return None
    names = re.findall(r"[\"']([^\"']+)[\"']", block.group(1))
    return tuple(names) or None


# ── the identity, from the resolver ─────────────────────────────────────────

def _load_main_orch():
    """Import ``hscc-roles/main_orch.py`` (the one identity resolver).

    The sibling dir is APPENDED to ``sys.path``, never prepended:
    ``hscc-bootstrap`` and ``hscc-roles`` each ship a ``_theme.py``, and a
    prepend would let a later ``import _theme`` on the bootstrap side silently
    pick up the roles copy (the reason documented at
    ``hscc-bootstrap/doctor_role_soul.py``). ``main_orch`` shadows nothing.
    """
    if _ROLES_DIR not in sys.path:
        sys.path.append(_ROLES_DIR)
    import main_orch  # noqa: PLC0415 - sibling plugin, path added above
    return main_orch


def main_profile_name(hscc_dir: Optional[str] = None) -> Optional[str]:
    """The main orchestrator's profile name, or None when it cannot be resolved.

    None is an honest failure, not a default: a row that guessed the identity it
    was written to protect would call the wrong home clean.
    """
    try:
        return _load_main_orch().main_name(hscc_dir)
    except Exception:  # noqa: BLE001 - the resolver missing is a report, not a 500
        return None


def _hermes_root(home: str) -> str:
    """``<root>/profiles/<name>`` -> ``<root>``; anything else unchanged.

    The fleet sweep has to see every sibling profile, so a profile-scoped
    ``HERMES_HOME`` (the normal worker mode) is walked up one level — the same
    rule ``doctor._skills_home`` applies for the same reason, owned by
    ``hscc_daemon/hermes_python.hermes_root`` (a pure string transform that never
    probes the filesystem).
    """
    home = os.path.expanduser(str(home))
    try:
        from hscc_daemon import hermes_python  # noqa: PLC0415
        return hermes_python.hermes_root(home)
    except Exception:  # noqa: BLE001 - bootstrap-only layout has no daemon sibling
        pass
    parent = os.path.dirname(home)
    if parent and os.path.basename(parent) == "profiles":
        return os.path.dirname(parent)
    return home


# ── reading one home ────────────────────────────────────────────────────────

def enabled_plugins(config_path: str) -> List[str]:
    """The home's own ``plugins.enabled`` names, in file order.

    A home declares its OWN list — there is no root→profile overlay
    (``hermes_cli/plugins_discovery.py:112-131`` reads the active home's config,
    which is the fact the audit had to establish first). A missing file, a
    missing key, or a value that is not a list of strings all read as "declares
    nothing", which is the clean posture for a worker home: an unlisted user
    plugin loads as a ``not enabled in config`` placeholder.
    """
    try:
        import yaml
    except Exception:  # noqa: BLE001 - no PyYAML means nothing can be read
        return []
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except OSError:
        return []
    except Exception:  # noqa: BLE001 - unparsable config is not this row's claim
        return []
    if not isinstance(cfg, dict):
        return []
    plugins = cfg.get("plugins")
    if not isinstance(plugins, dict):
        return []
    enabled = plugins.get("enabled")
    if not isinstance(enabled, list):
        return []
    return [str(e) for e in enabled if isinstance(e, str)]


def _skill_dirs(skills_dir: str) -> List[str]:
    """Top-level skill dir names under *skills_dir* (dot-dirs excluded).

    Top level only, which is where an installed skill lives; a nested
    ``devops/hscc-architecture`` is part of the generic ``devops`` group skill,
    not a cluster skill of its own.
    """
    try:
        return sorted(p.name for p in os.scandir(skills_dir)
                      if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []


def discover_profiles(hermes_home: str) -> List[Tuple[str, str]]:
    """``[(name, profile_home)]`` for every non-dot dir under ``profiles/``.

    Delegates to ``skill_guard.discover_profiles`` — the sweep the skill findings
    already use — so the dot-dir rule and the sort order stay in one place. The
    dot-dir rule is load-bearing, not tidiness: ``profiles/.deleted`` is a recycle
    dir (Oct 26, two ``probe-*`` markers) that a plain directory glob counts, and
    a home list carrying it means a finding that can never be fixed.
    """
    try:
        import skill_guard  # noqa: PLC0415 - flat sibling of hscc-bootstrap
        return [(str(n), str(p)) for n, p in skill_guard.discover_profiles(
            hermes_home)]
    except Exception:  # noqa: BLE001 - sibling missing: fall back to a local scan
        pass
    out = []
    profiles_dir = os.path.join(hermes_home, "profiles")
    try:
        for entry in sorted(os.scandir(profiles_dir), key=lambda e: e.name):
            if entry.is_dir() and not entry.name.startswith("."):
                out.append((entry.name, entry.path))
    except OSError:
        return []
    return out


# ── the verdict ─────────────────────────────────────────────────────────────

def home_findings(hermes_home: str, *, main_name: Optional[str],
                  tool_plugins: Sequence[str], skills: Sequence[str],
                  skills_declared: bool = True) -> List[Dict[str, Any]]:
    """Every misplaced cluster surface under *hermes_home*, as plain records.

    Pure scan: no probes, no fleet I/O, no writes. Each finding is
    ``{"profile", "home", "surface", "names", "want"}`` where ``surface`` is
    ``"plugins.enabled"`` or ``"skills"`` — the shape the row renders and the
    tests assert on, so the wording and the predicate cannot disagree.

    The root home is skipped on purpose (module docstring: "The root home").
    """
    findings: List[Dict[str, Any]] = []
    profiles = discover_profiles(hermes_home)
    main_seen = False

    for name, home in profiles:
        if main_name is not None and name == main_name:
            main_seen = True
            findings += _main_gaps(name, home, tool_plugins, skills,
                                   skills_declared)
            continue
        # A non-main profile: the tool plugins must not be enabled and no
        # hscc-owned cluster skill may be present.
        enabled = enabled_plugins(os.path.join(home, "config.yaml"))
        stray = [p for p in tool_plugins if p in enabled]
        if stray:
            findings.append({
                "profile": name, "home": home, "surface": "plugins.enabled",
                "names": stray, "want": [HOOK_ONLY_PLUGIN]})
        present = [s for s in skills
                   if s in _skill_dirs(os.path.join(home, "skills"))]
        if present:
            findings.append({
                "profile": name, "home": home, "surface": "skills",
                "names": present, "want": []})

    if main_name is not None and not main_seen:
        findings.append({
            "profile": main_name, "home": os.path.join(hermes_home, "profiles",
                                                       main_name),
            "surface": "profile", "names": [main_name], "want": []})
    return findings


def _main_gaps(name: str, home: str, tool_plugins: Sequence[str],
               skills: Sequence[str], skills_declared: bool) -> List[Dict]:
    """What ``<main>`` must have and does not (audit §8 row: "``<main>`` loads
    all three")."""
    want = [HOOK_ONLY_PLUGIN] + [p for p in tool_plugins]
    enabled = enabled_plugins(os.path.join(home, "config.yaml"))
    missing = [p for p in want if p not in enabled]
    out: List[Dict] = []
    if missing:
        out.append({"profile": name, "home": home,
                    "surface": "plugins.enabled", "names": missing,
                    "want": want})
    if skills_declared:
        present = _skill_dirs(os.path.join(home, "skills"))
        absent = [s for s in skills if s not in present]
        if absent:
            out.append({"profile": name, "home": home, "surface": "skills",
                        "names": absent, "want": list(skills)})
    return out


def _fix_for(finding: Dict[str, Any], main_name: Optional[str]) -> str:
    """The one-line repair for ONE finding — surface, not sermon."""
    profile, home = finding["profile"], finding["home"]
    names = ", ".join(finding["names"])
    if finding["surface"] == "plugins.enabled":
        if profile == main_name:
            return (f"enable {names} in {home}/config.yaml — bootstrap writes "
                    "this shape (card t_e98c6ad4); re-run it, or add them here")
        return (f"set plugins.enabled to ['{HOOK_ONLY_PLUGIN}'] in "
                f"{home}/config.yaml (drop {names}) — that home keeps the "
                "hscc-cluster hooks only; bootstrap writes the shape "
                "(card t_e98c6ad4), so re-running it is the durable fix")
    if finding["surface"] == "skills":
        if profile == main_name:
            return (f"install {names} for <main> — re-run the skills installer "
                    "(hscc-skills install-skills); a profile opted out via "
                    ".no-bundled-skills gets nothing seeded")
        return (f"move {names} out of {home}/skills — the copies belong on "
                "<main> only; the mover keeps them in the dated sibling "
                "backups dir skills-backups/<skill>.bak-<YYYYmmdd-HHMMSS> "
                "(card t_b52e70ba), and re-running bootstrap post t_e98c6ad4 "
                "stops re-seeding them here")
    return (f"create the <main> profile home ({home}) — re-run bootstrap "
            "(card t_e98c6ad4 stores the name and seeds the profile)")


def _render(findings: List[Dict[str, Any]]) -> str:
    """The one-line detail: who, what surface, which names — count first."""
    parts = []
    for finding in findings[:_MAX_RENDERED]:
        parts.append(f"{finding['profile']} "
                     f"[{finding['surface']}: {', '.join(finding['names'])}]")
    tail = ""
    if len(findings) > _MAX_RENDERED:
        tail = f" (+{len(findings) - _MAX_RENDERED} more)"
    return "; ".join(parts) + tail


def doctor_check(hermes_home: Optional[str] = None,
                 hscc_dir: Optional[str] = None, *,
                 main_name: Optional[str] = None,
                 tool_plugins: Optional[Sequence[str]] = None,
                 cluster_skills_: Optional[Sequence[str]] = None) -> dict:
    """The row's verdict dict (the five doctor ``Check`` fields).

    ``hermes_home`` / ``hscc_dir`` are what ``doctor_rows.render_rows`` forwards
    for a row declaring both (``row_kwargs=("hermes_home", "hscc_dir")``): the
    first locates the profile trees — the writers here are the installers, which
    DO honour a named ``--home``/``HERMES_HOME``, so a handed root is a root a
    writer can reach (unlike the daemon-state rows, which must declare no
    context: t_be690204) — the second is ``main_orch``'s own documented
    relocation seam for ``main-orchestrator.json``.

    The keyword overrides exist for the row's own tests; production calls leave
    them alone and read the declarations from their owners.
    """
    if hermes_home is None and doctor_rows.under_pytest():
        return refused()
    root = _hermes_root(hermes_home or os.environ.get("HERMES_HOME")
                        or os.path.expanduser("~/.hermes"))
    resolved = main_name if main_name is not None else main_profile_name(
        hscc_dir)
    if resolved is None:
        return {"name": CHECK_NAME, "ok": False, "fatal": False,
                "detail": "the main orchestrator's name could not be resolved "
                          f"({os.path.join(_ROLES_DIR, 'main_orch.py')} is not "
                          "importable) — the check cannot tell <main> from a "
                          "worker",
                "fix": "Deploy the hscc-roles plugin next to hscc-bootstrap "
                       "(re-run bootstrap); do not hand-set the identity here."}
    plugins = list(tool_plugins) if tool_plugins is not None \
        else list(cluster_tool_plugins())
    if cluster_skills_ is not None:
        skills, declared = list(cluster_skills_), True
    else:
        skills, declared = list(cluster_skills()), _skills_declared()
    findings = home_findings(root, main_name=resolved, tool_plugins=plugins,
                             skills=skills, skills_declared=declared)
    if not findings:
        detail = (f"clean: <{resolved}> carries the cluster surface, every "
                  "other profile has only the hscc-cluster hooks")
        if not declared:
            detail = ("cluster-skill ownership unverifiable (hscc-skills/hscc.py "
                      "not found) — plugins.enabled half checked only; " + detail)
        return {"name": CHECK_NAME, "ok": True, "fatal": False,
                "detail": detail, "fix": ""}
    fixes = []
    for finding in findings:
        text = _fix_for(finding, resolved)
        if text not in fixes:
            fixes.append(text)
    if not declared:
        fixes.insert(0, "cluster-skill ownership unverifiable: the check could "
                        "not read CLUSTER_SKILLS from hscc-skills/hscc.py, so "
                        "only the plugins.enabled half of the sweep ran.")
    return {"name": CHECK_NAME, "ok": False, "fatal": False,
            "detail": f"{len(findings)} home(s) carry the cluster control "
                      f"surface away from <{resolved}>: " + _render(findings),
            "fix": " || ".join(fixes[:3])}


def _skills_declared() -> bool:
    """Whether :func:`cluster_skills` read a real declaration or used the
    fallback. False makes the skills half of the sweep report itself rather than
    pass silently."""
    if _parse_tuple(_SKILLS_INSTALLER, "CLUSTER_SKILLS"):
        return True
    return any(_parse_tuple(c, "CLUSTER_SKILLS") for c in _installer_candidates())


def refused() -> dict:
    """The in-suite refusal: an ERROR naming the seam, never a silent pass and
    never a read of the operator's install."""
    return {"name": CHECK_NAME, "ok": False, "fatal": False,
            "detail": "main-only-surface check refused inside a pytest process "
                      "— inject _main_only_surface_check",
            "fix": "Call doctor_main_only.doctor_check(hermes_home=<tmp fake "
                   "root>, hscc_dir=<tmp .hscc>, main_name=...) for the real "
                   "sweep, or pass _main_only_surface_check=lambda: {...} to "
                   "pin the row's routing; a suite must never read the "
                   "operator's profile trees."}


if __name__ == "__main__":  # standalone probe: python3 doctor_main_only.py
    import json
    import argparse
    ap = argparse.ArgumentParser(
        description="main-only cluster surface sweep (read-only)")
    ap.add_argument("--home", default=None, help="HERMES_HOME to sweep")
    ap.add_argument("--hscc-dir", default=None, help="~/.hscc for the identity")
    args = ap.parse_args()
    print(json.dumps(doctor_check(args.home, args.hscc_dir), indent=2,
                     sort_keys=True))
