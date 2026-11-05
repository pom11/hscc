"""t_314eb13c — Hermes venv interpreter resolution under a profile home.

The bug: Hermes exports ``HERMES_HOME=<root>/profiles/<profile>`` for every
profile session (the fleet's normal run mode), and ``hermes-agent`` — where the
venv lives — sits at the ROOT. A resolver that joins the two resolves a path
that only exists when HERMES_HOME happens to be the root, so
``hscc api install-service`` dead-ended from every profile session with
``no Hermes venv python found (HERMES_HOME/hermes-agent/venv/bin/python3)``.

Every test here fabricates a throwaway HOME/HERMES_HOME; none of them read or
write the operator's real ``~/.hermes``.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import hermes_python  # noqa: E402

NOT_A_HERMES_VENV = "/opt/homebrew/Cellar/python@3.13/3.13.1/bin/python3"


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    return str(path)


def _fake_root(root):
    """Create ``<root>/hermes-agent/venv/bin/python3`` and return its path."""
    return _touch(root / "hermes-agent" / "venv" / "bin" / "python3")


# --------------------------------------------------------------------------- #
# hermes_homes — which ROOTS are searched, in what order
# --------------------------------------------------------------------------- #

def test_profile_home_yields_the_grandparent_root(tmp_path):
    """The rule that was missing: <root>/profiles/<name> → <root>."""
    profile = tmp_path / ".hermes" / "profiles" / "backend-engineer"
    homes = hermes_python.hermes_homes({"HERMES_HOME": str(profile)},
                                       home=str(tmp_path))
    assert homes == [str(profile), str(tmp_path / ".hermes")]


def test_non_profile_home_is_not_walked_up(tmp_path):
    """A custom (non-profile) HERMES_HOME is used as-is — no invented parent."""
    custom = tmp_path / "srv" / "hermes-custom"
    homes = hermes_python.hermes_homes({"HERMES_HOME": str(custom)},
                                       home=str(tmp_path))
    assert homes == [str(custom), str(tmp_path / ".hermes")]


def test_default_home_is_always_a_candidate(tmp_path):
    """Unset HERMES_HOME → the platform default only (same as before the fix)."""
    homes = hermes_python.hermes_homes({}, home=str(tmp_path))
    assert homes == [str(tmp_path / ".hermes")]


def test_repeated_default_is_deduplicated(tmp_path):
    """HERMES_HOME == ~/.hermes must not search (or print) the path twice."""
    home = tmp_path
    env = {"HERMES_HOME": str(tmp_path / ".hermes")}
    homes = hermes_python.hermes_homes(env, home=str(home))
    assert homes == [str(tmp_path / ".hermes")]


# --------------------------------------------------------------------------- #
# hermes_venv_python — the ladder, and the bug it fixes
# --------------------------------------------------------------------------- #

def test_profile_exported_home_still_finds_the_real_venv(tmp_path):
    """THE regression (t_314eb13c): HERMES_HOME=<root>/profiles/<p> finds
    <root>/hermes-agent/venv/bin/python3 instead of returning None."""
    root = tmp_path / ".hermes"
    real = _fake_root(root)
    profile = root / "profiles" / "backend-engineer"
    profile.mkdir(parents=True)  # exists, carries no hermes-agent — the live shape

    got = hermes_python.hermes_venv_python(
        {"HERMES_HOME": str(profile)}, home=str(tmp_path),
        sys_executable=NOT_A_HERMES_VENV)
    assert got == real


def test_profile_local_checkout_wins_over_the_root(tmp_path):
    """A profile dir CAN carry its own checkout; the exported home stays first."""
    root = tmp_path / ".hermes"
    _fake_root(root)
    profile = root / "profiles" / "worker"
    local = _fake_root(profile)

    got = hermes_python.hermes_venv_python(
        {"HERMES_HOME": str(profile)}, home=str(tmp_path),
        sys_executable=NOT_A_HERMES_VENV)
    assert got == local


def test_unset_home_uses_the_default_root(tmp_path):
    real = _fake_root(tmp_path / ".hermes")
    got = hermes_python.hermes_venv_python({}, home=str(tmp_path),
                                           sys_executable=NOT_A_HERMES_VENV)
    assert got == real


def test_python3_preferred_over_python(tmp_path):
    bindir = tmp_path / ".hermes" / "hermes-agent" / "venv" / "bin"
    py = _touch(bindir / "python")
    py3 = _touch(bindir / "python3")
    got = hermes_python.hermes_venv_python({}, home=str(tmp_path),
                                           sys_executable=NOT_A_HERMES_VENV)
    assert got == py3 and got != py


def test_skips_a_home_whose_venv_is_absent(tmp_path):
    """A HERMES_HOME whose hermes-agent exists but has no venv python must fall
    through to the next root, not return the dead path."""
    profile = tmp_path / ".hermes" / "profiles" / "worker"
    (profile / "hermes-agent").mkdir(parents=True)
    real = _fake_root(tmp_path / ".hermes")

    got = hermes_python.hermes_venv_python(
        {"HERMES_HOME": str(profile)}, home=str(tmp_path),
        sys_executable=NOT_A_HERMES_VENV)
    assert got == real


def test_fail_closed_when_no_candidate_exists(tmp_path):
    """(3) neither home carries a venv AND we are not inside a hermes venv →
    None. Never a plausible-looking path that isn't there."""
    got = hermes_python.hermes_venv_python(
        {"HERMES_HOME": str(tmp_path / "profiles" / "worker")},
        home=str(tmp_path), sys_executable=NOT_A_HERMES_VENV)
    assert got is None


# --------------------------------------------------------------------------- #
# the running-interpreter fallback, and the shape test that guards it
# --------------------------------------------------------------------------- #

def test_running_hermes_venv_is_used_when_the_homes_have_none(tmp_path):
    """Last-resort candidate: the venv this process is actually running (it
    demonstrably has Hermes' deps)."""
    running = _fake_root(tmp_path / "checkout") 
    got = hermes_python.hermes_venv_python({}, home=str(tmp_path / "empty"),
                                           sys_executable=running)
    assert got == running


@pytest.mark.parametrize("exe", [
    NOT_A_HERMES_VENV,                                    # brew / system python
    "/opt/x/venv/bin/python3",                            # a venv, not hermes-agent's
    "/Users/op/.hermes/hermes-agent/venv/lib/python3.13/site-packages/x.py",
    "/Users/op/.hermes/hermes-agent/venv/bin/pypy3",      # not an accepted name
    "",                                                   # nothing
])
def test_non_hermes_venv_executables_are_rejected(exe):
    """The shape test: only ``<...>/hermes-agent/venv/bin/python3|python`` may be
    advertised as the Hermes venv. A brew python would produce an API unit with
    no websockets/starlette in it."""
    assert hermes_python.running_hermes_venv_python(exe) is None


def test_running_fallback_is_last_not_first(tmp_path):
    """A real Hermes home beats the running interpreter — the API unit must
    record the fleet's venv, not whichever python invoked the installer."""
    real = _fake_root(tmp_path / ".hermes")
    running = _fake_root(tmp_path / "elsewhere")
    got = hermes_python.hermes_venv_python({}, home=str(tmp_path),
                                          sys_executable=running)
    assert got == real


# --------------------------------------------------------------------------- #
# error-message honesty (the fail-closed path an operator actually reads)
# --------------------------------------------------------------------------- #

def test_describe_search_names_every_place_it_looked(tmp_path):
    profile = tmp_path / ".hermes" / "profiles" / "worker"
    desc = hermes_python.describe_search({"HERMES_HOME": str(profile)},
                                         home=str(tmp_path),
                                         sys_executable=NOT_A_HERMES_VENV)
    assert str(profile / "hermes-agent" / "venv" / "bin" / "python3") in desc
    assert str(tmp_path / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3") in desc
    # The rejected executable is NOT offered as a location it searched for.
    assert NOT_A_HERMES_VENV not in desc


# --------------------------------------------------------------------------- #
# hermes_agent_dir — the CHECKOUT ladder (t_919ac971)
# --------------------------------------------------------------------------- #

def _fake_checkout(root):
    """Create ``<root>/hermes-agent`` as a runtime-ish dir; return its path."""
    agent = root / "hermes-agent"
    (agent / "tools").mkdir(parents=True)
    return str(agent)


def test_agent_dir_profile_exported_home_finds_the_root_checkout(tmp_path):
    """THE regression (t_919ac971): a profile-exported HERMES_HOME whose root
    carries hermes-agent must resolve the ROOT checkout, not report None."""
    root = tmp_path / ".hermes"
    real = _fake_checkout(root)
    (root / "profiles" / "backend-engineer").mkdir(parents=True)

    got = hermes_python.hermes_agent_dir(
        {"HERMES_HOME": str(root / "profiles" / "backend-engineer")},
        home=str(tmp_path))
    assert got == real


def test_agent_dir_profile_local_checkout_wins(tmp_path):
    """A profile dir that carries its own checkout stays first, as for the venv."""
    root = tmp_path / ".hermes"
    _fake_checkout(root)
    local = _fake_checkout(root / "profiles" / "worker")

    got = hermes_python.hermes_agent_dir(
        {"HERMES_HOME": str(root / "profiles" / "worker")}, home=str(tmp_path))
    assert got == local


def test_agent_dir_fail_closed_when_no_checkout_exists(tmp_path):
    """No candidate carries hermes-agent → None (the caller must report it)."""
    got = hermes_python.hermes_agent_dir(
        {"HERMES_HOME": str(tmp_path / "profiles" / "worker")},
        home=str(tmp_path))
    assert got is None


def test_agent_dir_candidates_do_not_invent_a_running_interpreter_path():
    """The checkout ladder has NO running-interpreter fallback (unlike the venv
    one). ``ensure_review_feature`` WRITES into the resolved checkout, so the
    target must come from the configured home, not from whichever python
    happened to invoke it."""
    cands = hermes_python.agent_dir_candidates(
        {"HERMES_HOME": ""}, home="/nonexistent-fixture")
    assert all(c.endswith("hermes-agent") for c in cands)
    assert sys.executable not in cands


def test_agent_dir_skips_a_home_whose_checkout_is_a_file(tmp_path):
    """A stale ``hermes-agent`` FILE is not a checkout — fall through, don't
    hand a non-directory to a caller that is about to run git in it."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "worker").mkdir(parents=True)
    (root / "hermes-agent").write_text("not a dir\n")

    assert hermes_python.hermes_agent_dir(
        {"HERMES_HOME": str(root / "profiles" / "worker")},
        home=str(tmp_path)) is None


def test_describe_agent_search_names_every_checkout_ruled_out(tmp_path):
    profile = tmp_path / ".hermes" / "profiles" / "worker"
    desc = hermes_python.describe_agent_search({"HERMES_HOME": str(profile)},
                                              home=str(tmp_path))
    assert str(profile / "hermes-agent") in desc
    assert str(tmp_path / ".hermes" / "hermes-agent") in desc


# --------------------------------------------------------------------------- #
# drift guard: bootstrap.sh resolves the same ladder in shell
# --------------------------------------------------------------------------- #

def test_bootstrap_sh_follows_the_same_root_rule():
    """``hscc-bootstrap/bootstrap.sh`` picks PYBIN the same way this module does.

    bootstrap.sh used to be ``PYBIN="$HERMES_HOME/hermes-agent/venv/bin/python"``,
    which from a profile session resolved a nonexistent path and silently fell
    back to a brew ``python3`` — and then `install_cli.py "$PYBIN"` installed
    the hscc-cli shim into brew python instead of the Hermes venv. Same bug
    class as the API unit, fixed in the same commit; this pin keeps the shell
    ladder from drifting back to a single HERMES_HOME join.
    """
    from pathlib import Path
    sh = Path(hermes_python.__file__).resolve().parent.parent \
        / "hscc-bootstrap" / "bootstrap.sh"
    body = sh.read_text()
    assert 'basename "$(dirname "$_cand")"' in body, (
        "bootstrap.sh must walk a profile-scoped HERMES_HOME up to the root")
    assert '"$HOME/.hermes"' in body, "bootstrap.sh must keep the default root"
    assert 'PYBIN="$HERMES_HOME/hermes-agent/venv/bin/python"' not in body, (
        "the single HERMES_HOME join is the bug (t_314eb13c) — use the ladder")


# --------------------------------------------------------------------------- #
# hermes_root — one canonical, deterministic transform (t_53f5b85a)
# --------------------------------------------------------------------------- #

def test_hermes_root_is_the_grandparent_for_a_profile_dir(tmp_path):
    profile = tmp_path / ".hermes" / "profiles" / "worker"
    assert hermes_python.hermes_root(str(profile)) == str(tmp_path / ".hermes")
    assert hermes_python.is_profile_dir(str(profile)) is True


def test_hermes_root_leaves_a_plain_root_alone(tmp_path):
    root = tmp_path / ".hermes"
    custom = tmp_path / "srv" / "hermes-custom"
    assert hermes_python.hermes_root(str(root)) == str(root)
    assert hermes_python.hermes_root(str(custom)) == str(custom)
    assert hermes_python.is_profile_dir(str(root)) is False


def test_hermes_root_never_probes_for_a_first_existing_root(tmp_path):
    """The transform is string-only: an existing ~/.hermes must NOT win over
    the profile's own grandparent (F6's deterministic-transform stamp)."""
    (tmp_path / ".hermes" / "hermes-agent").mkdir(parents=True)
    profile = tmp_path / "srv" / "hermes" / "profiles" / "worker"
    # nothing along the profile path exists — the rule still answers
    assert not profile.exists()
    assert hermes_python.hermes_root(str(profile)) == str(tmp_path / "srv" / "hermes")


def test_hermes_homes_keeps_exported_home_first_over_its_root(tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    assert hermes_python.hermes_homes(
        {"HERMES_HOME": str(profile)}, home=str(tmp_path)) == \
        [str(profile), str(root)]
