"""HSCC CLI wrapper — forwards to hscc_daemon package."""
import sys
from pathlib import Path


def _libexec_daemon_dirs(here):
    """Brew-libexec payload roots visible from a CLI installed under a
    libexec venv (t_bc5a3fee).

    The homebrew-hscc tap extracts the tarball under ``<prefix>/libexec/
    hscc-<ver>/`` and installs this package into a venv inside ``libexec/``
    — from ``site-packages`` NEITHER the sibling-``plugins`` candidate NOR
    the repo-root walk-up below can see the payload. Walk up to the nearest
    ``libexec`` ancestor and offer every ``hscc-<ver>`` dir (newest first,
    the same posture brew takes for the active formula version) that carries
    an ``hscc_daemon``. Returns [] for every non-brew layout, so existing
    installs resolve exactly as before.
    """
    out = []
    try:
        ancestors = list(Path(here).resolve().parents)
    except OSError:
        return out
    for anc in ancestors:
        libexec = anc / "libexec"
        if libexec.is_dir():
            try:
                out.extend(d for d in sorted(libexec.glob("hscc-*"), reverse=True)
                           if (d / "hscc_daemon").is_dir())
            except OSError:
                pass
            break
    return out


# Find hscc_daemon: check common locations
_candidates = [
    Path(__file__).resolve().parent.parent / "plugins",  # installed alongside hscc-* plugins
]
# brew libexec trees next (no-ops for every other layout; see
# _libexec_daemon_dirs). Before ~/.hermes/plugins on purpose: a brew-owned
# tree must beat a possibly-stale shared plugins copy.
_candidates += _libexec_daemon_dirs(Path(__file__))
_candidates += [
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
