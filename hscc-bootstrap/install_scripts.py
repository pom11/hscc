"""Copy HSCC operator shell scripts from the work repo into ``~/.hermes/scripts/``.

Distinct from ``install_payload`` which handles plugin directories. Scripts live
in a separate Hermes runtime dir (``~/.hermes/scripts/``) so they can be
referenced from cron jobs by short name (``hermes cron create ... --script
hscc_proxy_watchdog.sh``).

Only files matching ``hscc_*.sh`` are installed — user-added scripts in the
runtime dir are preserved. README files in the repo's ``scripts/`` are not
shipped (they're docs, not runtime).

Backup-then-overwrite: an existing runtime script is copied to
``<name>.bak-<ts>`` before the fresh copy lands. Executable bit (0o755) is
re-applied on the dest since the source mode may not survive all copy paths.
"""

import os
import shutil
from datetime import datetime
from pathlib import Path


def install_scripts(repo_root, scripts_dir, *, backup=True, ts=None):
    """Copy ``<repo_root>/scripts/hscc_*.sh`` into ``scripts_dir``.

    Returns a summary dict shaped like ``install_payload`` so callers can
    treat both stages uniformly.
    """
    repo_scripts = Path(repo_root).expanduser().resolve() / "scripts"
    scripts_dir = Path(scripts_dir).expanduser().resolve()

    if not repo_scripts.is_dir():
        return {
            "skipped": True,
            "reason": f"no scripts/ directory in {repo_root}",
            "installed": [], "backed_up": [], "missing": [],
        }

    stamp = ts or datetime.now().strftime("%Y%m%d-%H%M%S")
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # `hscc_*.sh` are the watchdog shells. The cron-invoked *_run.py watchers
    # belong here too: `hscc-escalate-watcher` executes
    # ~/.hermes/scripts/escalate_watcher_run.py every 15 minutes, and until this
    # was added bootstrap never deployed it — a repo fix to that file silently
    # never reached the running system. Both name shapes are ours; user scripts
    # in the runtime dir are still left alone.
    sources = sorted(repo_scripts.glob("hscc_*.sh")) + sorted(repo_scripts.glob("*_run.py"))

    installed, backed_up = [], []
    for src in sources:
        dst = scripts_dir / src.name
        if dst.exists():
            if backup:
                bak = scripts_dir / f"{src.name}.bak-{stamp}"
                shutil.copy2(dst, bak)
                backed_up.append(bak.name)
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755 if src.suffix == ".sh" else 0o700)
        installed.append(src.name)

    return {
        "skipped": False,
        "installed": installed,
        "backed_up": backed_up,
        "missing": [],
        "scripts_dir": str(scripts_dir),
    }


if __name__ == "__main__":
    import json
    import sys

    boot_dir = Path(__file__).resolve().parent
    repo_root = os.environ.get("REPO_ROOT") or str(boot_dir.parent)
    scripts_dir = os.environ.get("SCRIPTS") or os.path.expanduser("~/.hermes/scripts")
    no_backup = "--no-backup" in sys.argv

    result = install_scripts(repo_root, scripts_dir, backup=not no_backup)
    print(json.dumps(result, indent=2))
