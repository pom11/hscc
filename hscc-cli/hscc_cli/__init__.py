"""HSCC CLI wrapper — forwards to hscc_daemon package."""
import sys
from pathlib import Path

# Find hscc_daemon: check common locations
_candidates = [
    Path(__file__).resolve().parent.parent / "plugins",  # installed alongside hscc-* plugins
    Path.home() / ".hermes" / "plugins",                 # standard hermes plugins dir
    Path(__file__).resolve().parent.parent.parent.parent, # repo root (for dev)
]
for _cand in _candidates:
    if (_cand / "hscc_daemon").is_dir() or (_cand / "hscc_daemon" / "hscc_daemon").is_dir():
        if str(_cand) not in sys.path:
            sys.path.insert(0, str(_cand))
        break
else:
    # Fallback: try repo at ~/dev/hscc
    repo_root = Path.home() / "dev" / "hscc"
    if (repo_root / "hscc_daemon").is_dir():
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))

from hscc_daemon.hscc import main

__all__ = ["main"]
