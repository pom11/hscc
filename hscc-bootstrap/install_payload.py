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

# Never ship these into the runtime dir.
_EXCLUDE = {"__pycache__", ".pytest_cache", "tests", ".git"}


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
    Returns a summary dict. If ``repo_root`` and ``plugins_dir`` resolve to the
    same directory (the old in-place layout, or a clone into the runtime dir),
    the copy is skipped — there is nothing to install.

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
    for name in payload:
        src = repo_root / name
        if not src.exists():
            missing.append(name)
            continue
        dst = plugins_dir / name
        if dst.exists() or dst.is_symlink():
            if backup:
                bak_dir = _backups_dir(plugins_dir)
                bak_dir.mkdir(parents=True, exist_ok=True)
                bak = bak_dir / f"{name}.bak-{stamp}"
                shutil.move(str(dst), str(bak))
                backed_up.append(bak.name)
            elif dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        if src.is_dir():
            shutil.copytree(src, dst, ignore=_ignore, symlinks=False)
        else:
            shutil.copy2(src, dst)
        installed.append(name)

    return {
        "skipped": False,
        "installed": installed,
        "backed_up": backed_up,
        "missing": missing,
        "plugins_dir": str(plugins_dir),
    }


# Default payload: everything the runtime needs (scope B — full sibling layout
# so install/ walk-ups and SCRIPT_DIR resolution keep working).
DEFAULT_PAYLOAD = [
    "hscc-cluster", "hscc-commands", "hscc-roles", "hscc-skills",
    "hscc_daemon", "hscc-bootstrap", "sparkrun-hermes",
    "memori", "memori_byodb",
    "install", "docs", "assets", "configs",
    "README.md", "CHANGELOG.md", "LICENSE", "requirements.txt", "VERSION",
]


if __name__ == "__main__":
    import json
    import sys

    boot_dir = Path(__file__).resolve().parent
    repo_root = os.environ.get("REPO_ROOT") or str(boot_dir.parent)
    plugins_dir = os.environ.get("PLUGINS") or os.path.expanduser("~/.hermes/plugins")
    no_backup = "--no-backup" in sys.argv

    result = install_payload(repo_root, plugins_dir, DEFAULT_PAYLOAD,
                             backup=not no_backup)
    print(json.dumps(result, indent=2))
