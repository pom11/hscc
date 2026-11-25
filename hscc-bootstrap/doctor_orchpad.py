"""Doctor row: is the orchpad plugin on every orchestrator profile? (t_2d87d94d)

Bootstrap installs orchpad into the Hermes venv and runs its plugin installer
for the main orchestrator and every ``<project>-orch`` profile only. The ways
that posture silently decays are a profile created after bootstrap (never
visited), a plugin directory removed or left half-written, and — because Hermes
plugins are opt-in — installed files whose name is absent from that profile's
own ``plugins.enabled`` (``_get_enabled_plugins`` in
``hermes_cli/plugins_discovery.py``; a profile reads only its own config, it
does not inherit the root list). The pad is exactly the surface nobody notices
missing until an orchestrator has lost its follow-ups.

READ-ONLY: this row stats the plugin files and reads each profile's config. The
repair belongs to bootstrap (or a direct ``orchpad-hermes`` run plus the enable
step), and the fix text says so.

Inside a pytest process with no injected seam the row refuses, naming the seam:
its default target is the operator's live ``~/.hermes``, so a suite must never
decide its assertions from a real profile tree (the ``doctor_main_only`` fence
shape). ``doctor_check(hermes_home=<tmp>)`` is the test seam; ``main_name=``
keeps a suite independent of the operator's stored identity.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional

# The in-suite fence is ONE predicate, shared with the row registry: see
# ``doctor_rows.under_pytest`` for why the module-local test is not enough.
import doctor_rows  # flat sibling, next to this module (hscc-bootstrap/)

#: The row's printed name. ``doctor.py`` carries a fallback constant for the
#: case where this module itself is not importable; a test pins them equal.
CHECK_NAME = "orchpad plugin on orchestrator profiles"

PLUGIN_FILES = ("plugin.yaml", "__init__.py", "tools.py")
PLUGIN_NAME = "orchpad"

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROLES_DIR = os.path.join(os.path.dirname(_HERE), "hscc-roles")


def _hermes_root(home: str) -> str:
    """``<root>/profiles/<name>`` -> ``<root>``; anything else unchanged."""
    home = os.path.expanduser(str(home))
    parent = os.path.dirname(home)
    if parent and os.path.basename(parent) == "profiles":
        return os.path.dirname(parent)
    return home


def _main_profile(hscc_dir: Optional[str] = None) -> Optional[str]:
    """The main orchestrator's name from the one resolver, or None."""
    try:
        if _ROLES_DIR not in sys.path:
            sys.path.append(_ROLES_DIR)
        import main_orch  # noqa: PLC0415 - sibling plugin, path added above

        return main_orch.main_name(hscc_dir)
    except Exception:  # noqa: BLE001 - the resolver missing is a report, not a 500
        return None


def orchestrator_homes(hermes_home, main_name: Optional[str]) -> List[Path]:
    """Profile homes that must carry the pad: <main> plus every ``*-orch``.

    Only existing homes are returned: a project that was never provisioned is
    not this row's finding, and Stage-1 doctor runs before any profile exists.
    """
    root = Path(_hermes_root(str(hermes_home)))
    found: List[Path] = []
    main_home = root / "profiles" / main_name if main_name else None
    if main_home and main_home.is_dir():
        found.append(main_home)
    try:
        entries = sorted(os.scandir(root / "profiles"), key=lambda e: e.name)
    except OSError:
        entries = []
    for entry in entries:
        if (entry.is_dir() and not entry.name.startswith(".")
                and entry.name.endswith("-orch")
                and Path(entry.path) not in found):
            found.append(Path(entry.path))
    return found


def plugin_present(profile_home) -> bool:
    target = Path(profile_home) / "plugins" / "orchpad"
    return all((target / name).is_file() for name in PLUGIN_FILES)


def plugin_enabled(profile_home, name: str = PLUGIN_NAME) -> bool:
    """True when this home's own ``plugins.enabled`` names *name*.

    Hermes plugins are opt-in, and a profile reads only its own config (no
    root list overlay), so the files alone do not make the pad loadable.
    """
    path = Path(profile_home) / "config.yaml"
    try:
        import yaml
        with open(path) as fh:
            cfg = yaml.safe_load(fh) or {}
    except Exception:  # missing/unreadable -> not enabled
        return False
    plugins = cfg.get("plugins") if isinstance(cfg, dict) else None
    enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
    return isinstance(enabled, list) and name in enabled


def refused() -> dict:
    return {"name": CHECK_NAME, "ok": False, "fatal": False,
            "detail": "orchpad check refused inside a pytest process — "
                      "inject _orchpad_check",
            "fix": "Call doctor_orchpad.doctor_check(hermes_home=<tmp fake "
                   "home>, main_name=...), or pass "
                   "_orchpad_check=lambda: {...}; a suite must never read the "
                   "operator's live ~/.hermes."}


def doctor_check(hermes_home: Optional[str] = None, *,
                 main_name: Optional[str] = None,
                 hscc_dir: Optional[str] = None) -> dict:
    """WARN when an orchestrator profile is missing the orchpad plugin."""
    if hermes_home is None and doctor_rows.under_pytest():
        return refused()
    if hermes_home is None:
        hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser(
            "~/.hermes")
    root = _hermes_root(hermes_home or os.path.expanduser("~/.hermes"))
    main = main_name or _main_profile(hscc_dir)
    if main is None:
        return {"name": CHECK_NAME, "ok": False, "fatal": False,
                "detail": "cannot resolve the main orchestrator's profile name "
                          "— orchestrator pads cannot be verified",
                "fix": "Restore hscc-roles/main_orch.py next to the bootstrap "
                       "(re-run bootstrap)."}
    homes = orchestrator_homes(root, main)
    if not homes:
        return {"name": CHECK_NAME, "ok": True, "fatal": False,
                "detail": "no orchestrator profiles found — nothing to verify",
                "fix": ""}
    missing = [home.name for home in homes if not plugin_present(home)]
    disabled = [home.name for home in homes
                if plugin_present(home) and not plugin_enabled(home)]
    if missing or disabled:
        parts = []
        if missing:
            parts.append(f"{len(missing)} of {len(homes)} orchestrator "
                         f"profile(s) missing the orchpad plugin: "
                         f"{', '.join(missing)}")
        if disabled:
            parts.append(f"{len(disabled)} of {len(homes)} orchestrator "
                         f"profile(s) have the orchpad files but do not name "
                         f"orchpad in their own config.yaml plugins.enabled: "
                         f"{', '.join(disabled)}")
        return {"name": CHECK_NAME, "ok": False, "fatal": False,
                "detail": "; ".join(parts),
                "fix": "Re-run bootstrap (it installs the plugin and adds "
                       "'orchpad' to each orchestrator profile's own "
                       "config.yaml plugins.enabled), or install it directly: "
                       "orchpad-hermes --hermes-home <hermes home> --profile "
                       "<name>."}
    names = ", ".join(home.name for home in homes)
    return {"name": CHECK_NAME, "ok": True, "fatal": False,
            "detail": f"orchpad plugin on all {len(homes)} orchestrator "
                      f"profile(s): {names}",
            "fix": ""}
