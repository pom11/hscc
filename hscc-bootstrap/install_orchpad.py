"""Install orchpad, and its Hermes plugin on ORCHESTRATOR profiles only.

Orchpad is the orchestrator's own pad: follow-ups, ideas, observations and
waiting-on items. It is deliberately not a worker surface (operator rule
2026-11-23: cluster/control surfaces belong to the main orchestrator and the
per-project ``<project>-orch`` profiles), so this installer resolves exactly
that set and never touches a worker or reviewer home.

  install_orchpad.py                 report what is missing (JSON)
  install_orchpad.py --apply         install the package and the plugin
  install_orchpad.py --apply --profile <name>
                                     ensure ONE orchestrator profile (used by
                                     `project new` and `ensure_orchestrator`)

The package is pinned to a git tag, the same way bootstrap installs its other
Python dependencies (``uv pip install --python <venv>``). The plugin itself is
installed by orchpad's own console script, ``orchpad-hermes``, not by passing a
flag to the main ``orchpad`` CLI: the two are separate entry points on purpose,
and the plugin installer is the one that knows the Hermes profile layout.

Copying the files is not enough: Hermes plugins are OPT-IN. Its loader reads
``plugins.enabled`` from the ACTIVE home's config through
``_get_enabled_plugins`` (``hermes_cli/plugins_discovery.py``, called from
``hermes_cli/plugins.py``); a missing key is ``None`` = nothing enabled. A
profile is its own home — ``load_config()`` reads only
``$HERMES_HOME/config.yaml`` and the dispatcher sets ``HERMES_HOME`` to the
profile dir — so a profile's ``plugins.enabled`` neither overlays nor inherits
the root home's list. That is why each orchestrator profile gets ``orchpad``
appended to its OWN ``plugins.enabled`` (existing entries and order preserved;
no write in the steady state). The root ``config.yaml`` is never touched: the
pad is an orchestrator surface, and operator rule 2026-11-23 keeps
orchestrator-only surfaces off the other homes.

Idempotent: the package is skipped when the pinned version is already importable
(so a re-run does not hit the network), ``orchpad-hermes`` only rewrites files
whose bytes changed — its JSON summary names what it wrote — and the enable step
is a no-op once the name is present.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional

#: The pin. A tag, never a branch: bootstrap must be reproducible.
PIN = "orchpad @ git+https://github.com/pom11/orchpad@v0.1.0"
PIN_VERSION = "0.1.0"

#: The plugin name Hermes must find in the profile's ``plugins.enabled``.
PLUGIN_NAME = "orchpad"

#: The files orchpad's plugin installer copies into ``<profile>/plugins/orchpad``.
PLUGIN_FILES = ("plugin.yaml", "__init__.py", "tools.py")

Runner = Callable[..., subprocess.CompletedProcess]


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=600,
                          **kwargs)  # noqa: S603


def hermes_root(hermes_home) -> Path:
    """``<root>/profiles/<name>`` -> ``<root>``; anything else unchanged."""
    home = Path(hermes_home).expanduser()
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def resolve_venv_python(hermes_home=None,
                        explicit: Optional[str] = None) -> Optional[str]:
    """First existing Hermes venv python: explicit > env > conventional paths.

    Deliberately existence-only (never executes the candidate): the install is
    the caller's explicit step, and a resolver that probes by importing would
    run the target interpreter on every check. ``None`` means this home has no
    Hermes venv, so callers report that instead of pip-installing into whatever
    python happened to run them.
    """
    explicit = (explicit or os.environ.get("HSCC_HERMES_PYTHON", "")).strip()
    if explicit:
        return explicit
    root = hermes_root(hermes_home or os.environ.get("HERMES_HOME")
                       or "~/.hermes")
    for name in ("python3", "python"):
        candidate = root / "hermes-agent" / "venv" / "bin" / name
        if candidate.is_file():
            return str(candidate)
    return None


def installed_version(venv_python, run: Runner = _run) -> Optional[str]:
    """The orchpad version importable from *venv_python*, or None."""
    res = run([str(venv_python), "-I", "-c",
               "import importlib.metadata as m; print(m.version('orchpad'))"])
    if res.returncode != 0:
        return None
    return (res.stdout or "").strip() or None


def install_package(venv_python, run: Runner = _run, uv: Optional[str] = None):
    """Install the pinned orchpad into the Hermes venv."""
    uv = uv if uv is not None else shutil.which("uv")
    if uv:
        return run([uv, "pip", "install", "--quiet", "--no-deps",
                    "--python", str(venv_python), PIN])
    return run([str(venv_python), "-m", "pip", "install", "--quiet",
                "--no-deps", PIN])


def plugin_present(profile_home: Path) -> bool:
    """True when every plugin file orchpad-hermes writes is on disk."""
    target = Path(profile_home) / "plugins" / "orchpad"
    return all((target / name).is_file() for name in PLUGIN_FILES)


def plugin_enabled(profile_home: Path, name: str = PLUGIN_NAME) -> bool:
    """True when this home's own ``plugins.enabled`` names *name*."""
    path = Path(profile_home) / "config.yaml"
    try:
        import yaml
        with open(path) as fh:
            cfg = yaml.safe_load(fh) or {}
    except Exception:  # missing/unreadable -> not enabled (doctor reports it)
        return False
    plugins = cfg.get("plugins") if isinstance(cfg, dict) else None
    enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
    return isinstance(enabled, list) and name in enabled


def enable_plugin(profile_home: Path) -> Optional[dict]:
    """Append ``orchpad`` to THIS profile's own ``plugins.enabled``.

    A freshly created profile has no ``config.yaml`` at all (Hermes creates it
    on first run), and the single-home writer correctly declines to invent a
    ROOT config. Here the home is a profile this step just created, so the R5
    new-home seed is the right writer: it creates only ``plugins.enabled``,
    which also self-defuses Hermes' later migration. The name is then appended
    with the normal backup-then-write helper.
    """
    import enable_plugins  # flat sibling; reuses its backup-then-write helper

    if not (Path(profile_home) / "config.yaml").is_file():
        enable_plugins.ensure_plugins_tier_for_new_home(profile_home)
    return enable_plugins.ensure_plugin_enabled_for_home(
        profile_home, PLUGIN_NAME)


def installer_cmd(venv_python, hermes_home: Path, profile: str) -> list[str]:
    """The plugin-installer command, preferring the venv's console script."""
    script = Path(venv_python).parent / "orchpad-hermes"
    head = [str(script)] if script.exists() else [
        str(venv_python), "-m", "orchpad.hermes_plugin.cli"]
    return head + ["--hermes-home", str(hermes_home), "--profile", profile,
                   "--json"]


def main_name(explicit: Optional[str] = None,
              hscc_dir: Optional[str] = None) -> str:
    """The main orchestrator's profile name (never spelled into this module).

    Bootstrap exports ``MAIN_ORCH_NAME`` once it has stored the name; the
    resolver is the fallback for a standalone run. Its own default is returned
    when ``hscc-roles`` is not deployed next to this module.
    """
    if explicit:
        return explicit
    env = os.environ.get("MAIN_ORCH_NAME")
    if env:
        return env
    try:
        roles = Path(__file__).resolve().parent.parent / "hscc-roles"
        if str(roles) not in sys.path:
            sys.path.append(str(roles))
        import main_orch  # noqa: PLC0415 - sibling plugin, path added above

        return main_orch.main_name(hscc_dir)
    except Exception:  # noqa: BLE001 - resolver missing: use its documented default
        return "general"


def orchestrator_profiles(hermes_home, main: str) -> List[str]:
    """Profile names that get the pad: <main> plus every existing ``*-orch``.

    The suffix is the per-project orchestrator namespace (``hscc-roles``), so
    a worker or reviewer home can never match. A profile is returned only when
    its home exists — nothing is created for a project that was never
    provisioned.
    """
    root = Path(hermes_home).expanduser()
    names: List[str] = []

    def exists(name: str) -> bool:
        return (root / "profiles" / name).is_dir()

    if exists(main):
        names.append(main)
    profiles_dir = root / "profiles"
    try:
        entries = sorted(os.scandir(profiles_dir), key=lambda e: e.name)
    except OSError:
        entries = []
    for entry in entries:
        name = entry.name
        if (entry.is_dir() and not name.startswith(".")
                and name.endswith("-orch") and name not in names):
            names.append(name)
    return names


def _ensure_package(venv_python, apply: bool, run: Runner):
    """Ensure the pinned package once; return (report, usable, changed).

    Shared by the multi-profile fan-out and the single-profile repair path so
    "already importable" and "installation failed" mean the same thing
    everywhere.
    """
    before = installed_version(venv_python, run)
    package = {"pin": PIN, "expected": PIN_VERSION, "found": before}
    if before == PIN_VERSION:
        package["status"] = "present"
        return package, True, False
    if not apply:
        package["status"] = "missing"
        return package, False, False
    res = install_package(venv_python, run)
    after = installed_version(venv_python, run)
    if res.returncode == 0 and after == PIN_VERSION:
        package["status"] = "installed"
        package["found"] = after
        return package, True, True
    package["status"] = "failed"
    package["error"] = ((res.stderr or res.stdout or "").strip()
                        or f"install exited {res.returncode}")[-400:]
    return package, False, False


def _install_profile(hermes_home: Path, name: str, venv_python,
                     installer_run: Runner) -> dict:
    """Run the plugin installer + enable step on ONE profile home."""
    profile_home = hermes_home / "profiles" / name
    entry = {"profile": name, "path": str(profile_home / "plugins" / "orchpad")}
    res = installer_run(installer_cmd(venv_python, hermes_home, name))
    if res.returncode != 0:
        entry["status"] = "failed"
        entry["error"] = ((res.stderr or res.stdout or "").strip()
                          or f"orchpad-hermes exited {res.returncode}")[-400:]
        return entry
    try:
        written = json.loads(res.stdout or "{}").get("written") or []
    except (TypeError, ValueError):
        written = []
    try:
        enable_rec = enable_plugin(profile_home)
    except Exception as exc:  # noqa: BLE001 - name the profile, keep going
        entry["status"] = "failed"
        entry["error"] = (f"could not add {PLUGIN_NAME} to config.yaml "
                          f"plugins.enabled: {exc}")[-400:]
        return entry
    entry["written"] = list(written)
    if enable_rec:
        entry["enabled_added"] = list(enable_rec["added"])
    entry["enabled"] = plugin_enabled(profile_home)
    if not entry["enabled"]:
        entry["status"] = "failed"
        entry["error"] = (f"{PLUGIN_NAME} is installed but {profile_home / 'config.yaml'} "
                          "does not name it in plugins.enabled")[-400:]
        return entry
    entry["status"] = ("installed" if written or enable_rec
                       else "verified")
    return entry


def ensure(hermes_home, venv_python, *, apply: bool, main: Optional[str] = None,
           hscc_dir: Optional[str] = None, run: Runner = _run,
           installer_run: Optional[Runner] = None) -> dict:
    """Report (and with *apply*, install) orchpad on the orchestrator homes."""
    home = hermes_root(hermes_home)
    main = main_name(main, hscc_dir)
    profiles = orchestrator_profiles(home, main)
    installer_run = installer_run or run

    package, usable, changed = _ensure_package(venv_python, apply, run)
    ok = package["status"] != "failed"

    report = {"ok": ok, "package": package, "main": main, "profiles": []}
    for name in profiles:
        profile_home = home / "profiles" / name
        if not apply:
            entry = {"profile": name,
                     "path": str(profile_home / "plugins" / "orchpad")}
            entry["enabled"] = plugin_enabled(profile_home)
            entry["status"] = ("present" if plugin_present(profile_home)
                               else "missing")
            report["profiles"].append(entry)
            continue
        if not usable:
            report["profiles"].append(
                {"profile": name,
                 "path": str(profile_home / "plugins" / "orchpad"),
                 "status": "unknown"})
            continue
        entry = _install_profile(home, name, venv_python, installer_run)
        if entry["status"] == "failed":
            ok = False
        else:
            changed = changed or entry["status"] == "installed"
        report["profiles"].append(entry)

    report["ok"] = ok
    report["action"] = ("failed" if not ok
                        else "installed" if changed
                        else "verified" if apply else "reported")
    return report


def ensure_profile(hermes_home, profile: str, venv_python=None, *,
                   apply: bool = True, main: Optional[str] = None,
                   run: Optional[Runner] = None,
                   installer_run: Optional[Runner] = None) -> str:
    """Ensure package + plugin files + ``plugins.enabled`` on ONE profile.

    This is the single-profile repair path every profile-creating command uses
    (``project new``, ``ensure_orchestrator``, bootstrap): a caller that has
    just created ``<name>-orch`` asks for that profile only and gets a status
    token, never an exception. Returns ``enabled`` when this call changed
    something, ``already`` when the package, files and config entry were all in
    place, and ``failed: <why>`` otherwise.

    The guard is deliberate: only the main orchestrator and ``*-orch`` homes
    are orchestrator surfaces. A worker or reviewer name is refused rather than
    silently given a pad it must not have.
    """
    try:
        home = hermes_root(hermes_home)
        profile_home = home / "profiles" / profile
        if not profile_home.is_dir():
            return f"failed: profile {profile!r} has no home at {profile_home}"
        resolved_main = main_name(main)
        if profile != resolved_main and not profile.endswith("-orch"):
            return (f"failed: {profile!r} is not an orchestrator profile "
                    "(only the main orchestrator and *-orch get orchpad)")
        if not apply:
            if plugin_present(profile_home) and plugin_enabled(profile_home):
                return "already"
            return f"failed: orchpad is not installed on {profile!r}"
        venv = resolve_venv_python(home, venv_python)
        if venv is None:
            return ("failed: no Hermes venv python found under "
                    f"{home / 'hermes-agent' / 'venv' / 'bin'} "
                    "(set HSCC_HERMES_PYTHON)")
        run = run or _run
        installer_run = installer_run or run
        package, usable, changed = _ensure_package(venv, True, run)
        if not usable:
            why = package.get("error") or package["status"]
            return f"failed: package {why}"[:400]
        entry = _install_profile(home, profile, venv, installer_run)
        if entry["status"] == "failed":
            return f"failed: {entry['error']}"[:400]
        changed = changed or entry["status"] == "installed"
        return "enabled" if changed else "already"
    except Exception as exc:  # noqa: BLE001 - callers must never fail on this
        return f"failed:{type(exc).__name__}: {exc}"[:400]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="install where missing (default: report only)")
    ap.add_argument("--hermes-home",
                    default=os.environ.get("HERMES_HOME",
                                           str(Path.home() / ".hermes")))
    ap.add_argument("--python", default=None,
                    help="the Hermes venv python to install into "
                         "(default: resolve under --hermes-home)")
    ap.add_argument("--main-name",
                    default=os.environ.get("MAIN_ORCH_NAME"),
                    help="the main orchestrator's profile name")
    ap.add_argument("--profile", default=None,
                    help="ensure just this profile (e.g. <project>-orch)")
    args = ap.parse_args(argv)
    if args.profile:
        status = ensure_profile(
            args.hermes_home, args.profile, args.python, apply=args.apply,
            main=args.main_name)
        report = {"ok": not status.startswith("failed"),
                  "profile": args.profile, "status": status}
    else:
        report = ensure(args.hermes_home, args.python, apply=args.apply,
                        main=args.main_name)
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
