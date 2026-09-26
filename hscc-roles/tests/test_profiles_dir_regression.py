"""PROFILES_DIR path regression: the double-nest bug (t_b6ec32d6).

Hermes sets HERMES_HOME to the PROFILE dir (e.g. ~/.hermes/profiles/hscc-orch)
when it runs under that profile. rolelib.py used to derive PROFILES_DIR as
``$HERMES_HOME/profiles``, which in that runtime became
``<profile-dir>/profiles`` — a NON-EXISTENT path — so provision-check resolved
every profile's config and reported DEFAULT provisioning while silently marking
everything healthy. These tests pin the fix: PROFILES_DIR must resolve to the
hermes ROOT's ``profiles/`` dir (~/.hermes/profiles) regardless of whether the
ambient HERMES_HOME is the root or a profile dir.

The derivation is exercised directly through :func:`rolelib._hermes_root` (the
pure resolver) and through the module-level ``PROFILES_DIR`` that the CLI reads
— never by monkeypatching PROFILES_DIR, which would mask the bug being fixed.
"""
import os
import sys

import rolelib


def _tmp_profiles(tmp_path, name):
    """Create tmp_path/profiles/<name> and return the profile dir path."""
    pdir = tmp_path / "profiles" / name
    pdir.mkdir(parents=True, exist_ok=True)
    return pdir


# --------------------------------------------------------------------------- #
# _hermes_root: the pure resolver, both run modes.
# --------------------------------------------------------------------------- #

def test_hermes_root_hermes_root_mode(tmp_path):
    """HERMES_HOME pointing at the hermes root resolves to itself (root mode)."""
    root = str(tmp_path)
    assert rolelib._hermes_root(root) == root


def test_hermes_root_profile_dir_mode(tmp_path):
    """HERMES_HOME pointing at a profile dir resolves UP to the hermes root:
    the grandparent of the profile dir, i.e. the parent of the `profiles` dir —
    NOT the profile dir itself (the double-nest)."""
    pdir = _tmp_profiles(tmp_path, "hscc-orch")
    assert rolelib._hermes_root(str(pdir)) == str(tmp_path)


def test_hermes_root_expands_tilde_default():
    """The default (~/.hermes) expands to the home hermes root."""
    resolved = rolelib._hermes_root("~/.hermes")
    assert resolved == os.path.expanduser("~/.hermes")


def test_hermes_root_empty_returns_empty():
    """An empty/unset HERMES_HOME resolves to empty — the caller's default
    applies elsewhere and we never mis-derive a path from nothing."""
    assert rolelib._hermes_root(None) == ""
    assert rolelib._hermes_root("") == ""


# --------------------------------------------------------------------------- #
# Module-level PROFILES_DIR: what the CLI actually reads.
# --------------------------------------------------------------------------- #

def test_profiles_dir_resolves_to_root_profiles_when_home_is_profile_dir(
        monkeypatch, tmp_path):
    """REGRESSION (t_b6ec32d6): even when the ambient HERMES_HOME is a profile
    dir (the double-nest runtime), importing rolelib must derive PROFILES_DIR
    as the hermes root's `profiles/` dir, NOT <profile-dir>/profiles."""
    pdir = _tmp_profiles(tmp_path, "hscc-orch")
    monkeypatch.setenv("HERMES_HOME", str(pdir))
    # Re-derive the module-level constants from the monkeypatched env, exactly
    # as a fresh import would. (We cannot reimport rolelib mid-session cheaply,
    # so replicate the two-line derivation to assert it lands correctly.)
    root = rolelib._hermes_root(os.environ.get("HERMES_HOME", "~/.hermes"))
    profiles_dir = os.path.join(root, "profiles")
    assert profiles_dir == str(tmp_path / "profiles")
    assert os.path.basename(profiles_dir) == "profiles"
    # The double-nest path the bug used to produce must NOT be the derivation.
    assert profiles_dir != os.path.join(str(pdir), "profiles")
    # And the derived root is the dir that actually CONTAINS the profile.
    assert root == str(tmp_path)
    assert pdir != profiles_dir


def test_profiles_dir_end_to_end_import(monkeypatch, tmp_path, capsys):
    """Import rolelib in a subprocess whose HERMES_HOME points at a profile
    dir, and assert the module's PROFILES_DIR lands on the root profiles dir.
    This exercises the real import-time derivation (no monkeypatching of
    PROFILES_DIR) end to end, under the Python under test."""
    import subprocess
    pdir = _tmp_profiles(tmp_path, "hscc-orch")
    code = (
        "import os, sys\n"
        "sys.path.insert(0, %r)\n"
        "import rolelib\n"
        "print(rolelib.PROFILES_DIR)\n"
    ) % os.path.dirname(rolelib.__file__)
    env = dict(os.environ, HERMES_HOME=str(pdir))
    res = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(tmp_path / "profiles")
