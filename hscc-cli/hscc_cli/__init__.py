"""HSCC CLI wrapper — forwards to hscc_daemon package."""
import re
import sys
from pathlib import Path


def _libexec_daemon_dirs(here):
    """Brew-libexec payload roots visible from a CLI installed under a
    libexec venv (t_bc5a3fee).

    The homebrew-hscc tap installs the payload at the version-STABLE
    ``<prefix>/.../libexec/hscc/`` (Formula: ``tree = libexec/"hscc"``) and
    installs this package into a venv inside ``libexec/`` — from
    ``site-packages`` NEITHER the sibling-``plugins`` candidate NOR the
    repo-root walk-up below can see the payload. Walk up to the nearest
    ``libexec`` ancestor and offer the stable ``hscc`` dir plus every
    ``hscc-<ver>`` dir (newest first — numeric version order, so 2.10.0 beats
    2.9.0 — the same posture brew takes for the active formula version) that
    carries an ``hscc_daemon``. The stable name is what the shipped tap
    produces; the versioned glob also covers a hand-built or future tree.
    Normally candidate #1 (the tap's ``site-packages/plugins`` symlink) wins
    first — these are the fallback when that link is missing. Returns [] for
    every non-brew layout, so existing installs resolve exactly as before.
    """
    out = []
    try:
        ancestors = list(Path(here).resolve().parents)
    except OSError:
        return out

    def _ver_key(p):
        digits = re.findall(r"\d+", p.name)
        return ([int(d) for d in digits], p.name)

    for anc in ancestors:
        libexec = anc / "libexec"
        if libexec.is_dir():
            stable = libexec / "hscc"
            if (stable / "hscc_daemon").is_dir():
                out.append(stable)
            try:
                out.extend(d for d in sorted(libexec.glob("hscc-*"),
                                             key=_ver_key, reverse=True)
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
