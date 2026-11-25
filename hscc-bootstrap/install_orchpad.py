"""Install orchpad, and its Hermes plugin on ORCHESTRATOR profiles only.

Orchpad is the orchestrator's own pad: follow-ups, ideas, observations and
waiting-on items. It is deliberately not a worker surface (operator rule
2026-11-23: cluster/control surfaces belong to the main orchestrator and the
per-project ``<project>-orch`` profiles), so this installer resolves exactly
that set and never touches a worker or reviewer home.

  install_orchpad.py                 report what is missing (JSON)
  install_orchpad.py --apply         install the package and the plugin

The package is pinned to a git tag, the same way bootstrap installs its other
Python dependencies (``uv pip install --python <venv>``). The plugin itself is
installed by orchpad's own console script, ``orchpad-hermes``, not by passing a
flag to the main ``orchpad`` CLI: the two are separate entry points on purpose,
and the plugin installer is the one that knows the Hermes profile layout.

Idempotent: the package is skipped when the pinned version is already importable
(so a re-run does not hit the network), and ``orchpad-hermes`` only rewrites
files whose bytes changed — its JSON summary names what it wrote.
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

#: The files orchpad's plugin installer copies into ``<profile>/plugins/orchpad``.
PLUGIN_FILES = ("plugin.yaml", "__init__.py", "tools.py")

Runner = Callable[..., subprocess.CompletedProcess]


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=600,
                          **kwargs)  # noqa: S603


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


def ensure(hermes_home, venv_python, *, apply: bool, main: Optional[str] = None,
           hscc_dir: Optional[str] = None, run: Runner = _run,
           installer_run: Optional[Runner] = None) -> dict:
    """Report (and with *apply*, install) orchpad on the orchestrator homes."""
    home = Path(hermes_home).expanduser()
    main = main_name(main, hscc_dir)
    profiles = orchestrator_profiles(home, main)
    installer_run = installer_run or run

    before = installed_version(venv_python, run)
    package = {"pin": PIN, "expected": PIN_VERSION, "found": before}
    ok = True
    changed = False
    usable = before == PIN_VERSION

    if usable:
        package["status"] = "present"
    elif not apply:
        package["status"] = "missing"
    else:
        res = install_package(venv_python, run)
        after = installed_version(venv_python, run)
        if res.returncode == 0 and after == PIN_VERSION:
            package["status"] = "installed"
            package["found"] = after
            usable = changed = True
        else:
            package["status"] = "failed"
            package["error"] = ((res.stderr or res.stdout or "").strip()
                                or f"install exited {res.returncode}")[-400:]
            ok = False

    report = {"ok": ok, "package": package, "main": main, "profiles": []}
    for name in profiles:
        profile_home = home / "profiles" / name
        entry = {"profile": name, "path": str(profile_home / "plugins" / "orchpad")}
        if not apply:
            entry["status"] = ("present" if plugin_present(profile_home)
                               else "missing")
            report["profiles"].append(entry)
            continue
        if not usable:
            entry["status"] = "unknown"
            report["profiles"].append(entry)
            continue
        res = installer_run(installer_cmd(venv_python, home, name))
        if res.returncode != 0:
            entry["status"] = "failed"
            entry["error"] = ((res.stderr or res.stdout or "").strip()
                              or f"orchpad-hermes exited {res.returncode}")[-400:]
            ok = False
            report["profiles"].append(entry)
            continue
        try:
            written = json.loads(res.stdout or "{}").get("written") or []
        except (TypeError, ValueError):
            written = []
        entry["written"] = list(written)
        entry["status"] = "installed" if written else "verified"
        changed = changed or bool(written)
        report["profiles"].append(entry)

    report["ok"] = ok
    report["action"] = ("failed" if not ok
                        else "installed" if changed
                        else "verified" if apply else "reported")
    return report


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="install where missing (default: report only)")
    ap.add_argument("--hermes-home",
                    default=os.environ.get("HERMES_HOME",
                                           str(Path.home() / ".hermes")))
    ap.add_argument("--python", default=sys.executable,
                    help="the Hermes venv python to install into")
    ap.add_argument("--main-name",
                    default=os.environ.get("MAIN_ORCH_NAME"),
                    help="the main orchestrator's profile name")
    args = ap.parse_args(argv)
    report = ensure(args.hermes_home, args.python, apply=args.apply,
                    main=args.main_name)
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
