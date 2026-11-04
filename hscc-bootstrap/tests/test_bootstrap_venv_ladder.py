"""t_314eb13c — bootstrap.sh's Hermes-venv ladder, exercised in a real shell.

The interpreter bootstrap.sh picks as ``$PYBIN`` is not cosmetic: stage 6 runs
``install_cli.py "$PYBIN" …`` which pip-installs the ``hscc`` console shim into
whatever interpreter it is handed. The old resolver was a single
``$HERMES_HOME/hermes-agent/venv/bin/python`` join, and Hermes exports
``HERMES_HOME=<root>/profiles/<name>`` for every profile session — so from any
profile session the join missed, ``PYBIN`` silently became a brew ``python3``,
and the shim landed in brew python instead of the Hermes venv.

These tests extract the EXACT ladder block from bootstrap.sh, source it in bash
against fabricated roots (never the operator's ~/.hermes), and assert which path
comes out. Mirrors tests/test_bootstrap_autoreboot_stage.py's extract-and-run
pattern for the same reason: nothing else in the suite executes shell.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
BOOT = HERE.parent / "bootstrap.sh"

_HEAD = "PYBIN=\"\"\nfor _cand in"
_TAIL = '[ -n "$PYBIN" ] || PYBIN="$(command -v python3 || echo python3)"'


def _ladder_body() -> str:
    text = BOOT.read_text()
    start = text.index(_HEAD)
    end = text.index(_TAIL, start) + len(_TAIL)
    return text[start:end]


LADDER = _ladder_body()


def resolve(tmp_path, *, hermes_home, home=None, extra_path=None):
    """Run the extracted ladder with HERMES_HOME/HOME pinned; return $PYBIN."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("HERMES_HOME", "HSCC_PYBIN")}
    env["HOME"] = str(home if home is not None else tmp_path)
    if hermes_home is not None:
        env["HERMES_HOME"] = str(hermes_home)
    # PATH deliberately minimal: a `python3` on PATH must not mask the ladder.
    # /bin stays on it because bash itself is resolved through the CHILD's PATH.
    env["PATH"] = (f"{extra_path}:/bin:/usr/bin" if extra_path else "/usr/bin:/bin")
    script = f'set -u\n{LADDER}\nprintf "%s" "$PYBIN"\n'
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=env, timeout=30)
    assert p.returncode == 0, f"ladder failed: {p.stderr}"
    return p.stdout.strip()


def _mk_venv(home: Path, name: str = "python3", executable=True):
    bindir = home / "hermes-agent" / "venv" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    py = bindir / name
    py.write_text("#!/bin/sh\n")
    if executable:
        py.chmod(0o755)
    return py


# --------------------------------------------------------------------------- #
def test_profile_home_resolves_the_root_venv(tmp_path):
    """THE regression: HERMES_HOME=<root>/profiles/<p> finds the root's venv."""
    root = tmp_path / ".hermes"
    real = _mk_venv(root)
    profile = root / "profiles" / "backend-engineer"
    profile.mkdir(parents=True)

    got = resolve(tmp_path, hermes_home=profile)
    assert got == str(real)


def test_exported_home_wins_when_it_has_a_venv(tmp_path):
    """A profile-local / custom checkout under HERMES_HOME is still first."""
    root = tmp_path / ".hermes"
    _mk_venv(root)
    profile = root / "profiles" / "worker"
    local = _mk_venv(profile)

    got = resolve(tmp_path, hermes_home=profile)
    assert got == str(local)


def test_unset_home_uses_the_default_root(tmp_path):
    real = _mk_venv(tmp_path / ".hermes")
    assert resolve(tmp_path, hermes_home=None) == str(real)


def test_python3_preferred_over_python(tmp_path):
    _mk_venv(tmp_path / ".hermes", name="python")
    real = _mk_venv(tmp_path / ".hermes", name="python3")
    assert resolve(tmp_path, hermes_home=None) == str(real)


def test_non_executable_candidate_is_skipped(tmp_path):
    """`[ -x ]`, not `-e`: a dangling/non-executable stub must not be chosen."""
    _mk_venv(tmp_path / ".hermes", name="python3", executable=False)
    real = _mk_venv(tmp_path / ".hermes", name="python")
    assert resolve(tmp_path, hermes_home=None) == str(real)


def test_falls_back_to_path_python_when_no_install(tmp_path):
    """No Hermes anywhere -> a real PATH python3 (the old behaviour, kept)."""
    shim = tmp_path / "bin"
    shim.mkdir()
    fake = shim / "python3"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    got = resolve(tmp_path, hermes_home=tmp_path / "nope", extra_path=shim)
    assert got == str(fake)


def test_still_works_under_set_u(tmp_path):
    """bootstrap.sh runs `set -uo pipefail`: the ladder must not trip -u when
    HERMES_HOME happens to be unset (it is defaulted above it, but the block
    must be self-defensive because the tests source it standalone)."""
    got = resolve(tmp_path, hermes_home=None)  # no ~/.hermes venv exists here
    assert got.endswith("python3") or got == "python3"


# --------------------------------------------------------------------------- #
# static pins: the block must keep the two rules, and not regress
# --------------------------------------------------------------------------- #

def test_ladder_walks_up_from_profiles_and_keeps_the_default():
    assert 'basename "$(dirname "$_cand")"' in LADDER
    assert '"$HOME/.hermes"' in LADDER
    assert 'PYBIN="$HERMES_HOME/hermes-agent/venv/bin/python"' not in BOOT.read_text(), \
        "the single HERMES_HOME join is the bug (t_314eb13c)"


def test_plugins_dir_still_tracks_the_raw_home():
    """The fix must NOT widen $PLUGINS: Hermes scans get_hermes_home()/plugins,
    i.e. the PROFILE's own plugins dir, so deploying payload to the root would
    make it undiscoverable (see the profile-home resolver note in doctor.py)."""
    text = BOOT.read_text()
    assert 'PLUGINS="$HERMES_HOME/plugins"' in text
    assert 'PLUGINS="$HERMES_ROOT/plugins"' not in text
