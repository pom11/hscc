"""Copy HSCC plugin files from the work repo into the Hermes runtime dir.

The repo is the source of truth; bootstrap installs a copy into
``~/.hermes/plugins/`` so the git checkout and the runtime dir stay separate
(edit in the repo, run bootstrap, runtime updates — a real user install).

Backup-then-overwrite: an existing runtime ``<dir>`` is moved aside to a
sibling ``plugins-backups/<dir>.bak-<ts>`` (never inside the scanned
plugins_dir) before the fresh copy lands. Build artifacts and tests are
not shipped to the runtime dir.
"""

import os
import shutil
from datetime import datetime
from pathlib import Path

try:
    import backup_util
except ImportError:  # pragma: no cover - import-path robustness only
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import backup_util

# Never ship these into the runtime dir.
_EXCLUDE = {"__pycache__", ".pytest_cache", "tests", ".git"}

# Extra deploy destinations for payload entries whose repo staging path is NOT
# a location Hermes' plugin scanner can discover. Hermes scans each plugins dir
# for manifests at two depths only (hermes_cli.plugins_discovery.scan_directory
# recurses ONE category level below the root), so a provider plugin staged at
# install/hscc-plugins/video_gen/<name>/ would sit four levels under
# ~/.hermes/plugins and never load. The extra dest puts a second, identical
# copy at the category level <plugins>/video_gen/<name>/, which the scanner
# keys "video_gen/openai_compat" — the exact key plugins.enabled must carry
# (user-source plugins are opt-in; bundled auto-load does not apply).
# Additive, not a redirect: the staging path still deploys under
# plugins_dir/<staging-name>, because hscc_daemon/verify.py::
# check_plugin_payload compares every payload name at its same-named deployed
# location — a redirect there would flag permanent phantom "not deployed"
# drift. Keys: repo-relative staging path; values: tuple of
# plugins_dir-relative extra destinations.
PAYLOAD_EXTRA_DESTS = {
    "install/hscc-plugins/video_gen/openai_compat": ("video_gen/openai_compat",),
}


def _ignore(_dir, names):
    """shutil.copytree ignore: drop caches/tests/pyc from the runtime copy."""
    drop = {n for n in names if n in _EXCLUDE or n.endswith(".pyc")}
    return drop


def _backups_dir(plugins_dir):
    """Return the sibling backup directory next to ``plugins_dir``."""
    return plugins_dir.parent / "plugins-backups"


def _sweep_old_backups(plugins_dir, backups_dir):
    """Move any pre-existing ``<name>.bak-<ts>`` entries out of ``plugins_dir``.

    Best-effort — never raises; silently skips anything that fails.
    """
    try:
        candidates = [e for e in plugins_dir.iterdir() if ".bak-" in e.name]
    except OSError:
        return
    if not candidates:
        return
    try:
        backups_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for entry in candidates:
        try:
            shutil.move(str(entry), str(backups_dir / entry.name))
        except OSError:
            pass


def install_payload(repo_root, plugins_dir, payload, *, backup=True, ts=None):
    """Copy each ``payload`` entry from ``repo_root`` into ``plugins_dir``.

    ``payload`` is a list of names (dirs or files) relative to the repo root.
    Entries listed in ``PAYLOAD_EXTRA_DESTS`` are ALSO copied to extra
    plugins_dir-relative destinations (a staging path that Hermes' plugin
    scanner cannot discover needs a second copy at a scannable location — see
    the PAYLOAD_EXTRA_DESTS comment). Returns a summary dict. If ``repo_root``
    and ``plugins_dir`` resolve to the same directory (the old in-place layout,
    or a clone into the runtime dir), the copy is skipped — there is nothing to
    install.

    Backups are written to the sibling ``plugins-backups/`` directory (never
    inside the scanned ``plugins_dir``) so stale copies can never shadow
    freshly-installed plugins.
    """
    repo_root = Path(repo_root).expanduser().resolve()
    plugins_dir = Path(plugins_dir).expanduser().resolve()

    if repo_root == plugins_dir:
        return {
            "skipped": True,
            "reason": "repo_root == plugins_dir (in-place layout); nothing to copy",
            "installed": [], "backed_up": [], "missing": [],
        }

    stamp = ts or datetime.now().strftime("%Y%m%d-%H%M%S")
    plugins_dir.mkdir(parents=True, exist_ok=True)

    # Sweep stale .bak-* entries out of the scanned plugins_dir (best effort).
    _sweep_old_backups(plugins_dir, _backups_dir(plugins_dir))

    installed, backed_up, missing = [], [], []

    def _copy_into(src, dst, label):
        """One backup-then-overwrite copy (dir or file) into ``dst``."""
        if dst.exists() or dst.is_symlink():
            if backup:
                bak_dir = _backups_dir(plugins_dir)
                bak_dir.mkdir(parents=True, exist_ok=True)
                # Flatten path separators so nested dests (video_gen/openai_compat)
                # back up as ONE name directly inside bak_dir (the sweep +
                # prune only see direct children of bak_dir).
                bak = bak_dir / f"{label.replace('/', '__')}.bak-{stamp}"
                shutil.move(str(dst), str(bak))
                backed_up.append(bak.name)
                # Retain-limit the pile this stage creates (t_9462260b):
                # plugins-backups/ grew to 3,385 entries / 1.2 GB here.
                # Default stamped_only keeps operator-labelled bookmarks alive.
                backup_util.prune_backup_dir(bak_dir)
            elif dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        if src.is_dir():
            shutil.copytree(src, dst, ignore=_ignore, symlinks=False)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        installed.append(label)

    for name in payload:
        src = repo_root / name
        if not src.exists():
            missing.append(name)
            continue
        _copy_into(src, plugins_dir / name, name)
        # Extra destinations for entries whose staging path is not scannable
        # by Hermes' plugin discovery (see PAYLOAD_EXTRA_DESTS).
        for extra in PAYLOAD_EXTRA_DESTS.get(name, ()):
            _copy_into(src, plugins_dir / extra, extra)

    return {
        "skipped": False,
        "installed": installed,
        "backed_up": backed_up,
        "missing": missing,
        "plugins_dir": str(plugins_dir),
    }


def _named_profile_plugins_dirs(plugins_dir):
    """Plugin dirs of every existing named profile under the same home as
    ``plugins_dir`` (``<root>/profiles/<name>/plugins``).

    Each Hermes profile loads its plugins and memory provider from its OWN
    ``$HERMES_HOME/plugins`` (``get_hermes_home()/plugins``), NOT the root
    ``~/.hermes/plugins``. So a payload installed only into the root home never
    reaches the running profile sessions — the exact failure that kept the
    memori_byodb fix (t_57e5b3f7) from ever taking effect for the orchestrator
    profile, which still ran the OLD buggy ``local_augmentation.py``.

    Returns [] when ``plugins_dir`` is itself profile-scoped (already inside a
    ``<root>/profiles/<name>``) or when no profiles exist, so the call is a
    no-op in those cases.
    """
    plugins_dir = Path(plugins_dir).expanduser().resolve()
    if plugins_dir.name != "plugins":
        return []  # not a root layout — cannot derive a profiles sibling
    profiles_dir = plugins_dir.parent / "profiles"
    if not profiles_dir.is_dir():
        return []
    out = []
    try:
        for child in sorted(profiles_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            pdir = child / "plugins"
            if pdir.is_dir():
                out.append(pdir)
    except OSError:
        return []
    return out


def install_payload_profiles(
    repo_root, plugins_dir, payload, *, backup=True, ts=None
):
    """Install ``payload`` into the root home AND every named profile's plugin
    dir under the same home, so the runtime code is current for every profile
    that loads it. Returns the primary summary with a ``profiles`` child list
    (one summary per profile plugin dir; a failing profile is recorded, not
    fatal).
    """
    primary = install_payload(repo_root, plugins_dir, payload, backup=backup, ts=ts)
    profile_results = []
    for p_dir in _named_profile_plugins_dirs(plugins_dir):
        try:
            profile_results.append(
                install_payload(repo_root, p_dir, payload, backup=backup, ts=ts)
            )
        except Exception as exc:  # a bad profile must never sink the whole deploy
            profile_results.append(
                {"plugins_dir": str(p_dir),
                 "error": f"{type(exc).__name__}: {exc}"}
            )
    primary["profiles"] = profile_results
    return primary


# Default payload: everything the runtime needs (scope B — full sibling layout
# so install/ walk-ups and SCRIPT_DIR resolution keep working).
DEFAULT_PAYLOAD = [
    "hscc-cluster", "hscc-commands", "hscc-roles", "hscc-skills",
    "hscc_daemon", "hscc-bootstrap", "sparkrun-hermes",
    # hscc-project is the relocated flightdeck; `hscc project ...` (in
    # hscc_daemon/hscc.py:_handle_project) imports it as a SIBLING of
    # hscc_daemon, so it must deploy alongside it or the verb breaks.
    "hscc-project",
    # hscc-api ships the `hscc api ...` HTTP server (hscc_daemon/hscc.py:
    # _handle_api). Its server imports the cluster/project plugins as siblings
    # at runtime, so it must deploy alongside them or the installed `hscc api`
    # verb fails with 'package not found' — the same gap that bit hscc-project.
    "hscc-api",
    "memori", "memori_byodb",
    "install", "docs", "assets", "configs",
    # Hermes provider plugins the fleet ships (media enablement, t_ec488ce3).
    # Staged under install/ alongside the bundled skills; PAYLOAD_EXTRA_DESTS
    # additionally deploys it at the category level
    # <plugins>/video_gen/openai_compat/ so Hermes' one-category-level scan
    # discovers it under key "video_gen/openai_compat" — the key
    # plugins.enabled must carry.
    "install/hscc-plugins/video_gen/openai_compat",
    "README.md", "CHANGELOG.md", "LICENSE", "requirements.txt", "VERSION",
]


if __name__ == "__main__":
    import json
    import sys

    boot_dir = Path(__file__).resolve().parent
    repo_root = os.environ.get("REPO_ROOT") or str(boot_dir.parent)
    plugins_dir = os.environ.get("PLUGINS") or os.path.expanduser("~/.hermes/plugins")
    no_backup = "--no-backup" in sys.argv

    result = install_payload_profiles(repo_root, plugins_dir, DEFAULT_PAYLOAD,
                                      backup=not no_backup)
    print(json.dumps(result, indent=2))
