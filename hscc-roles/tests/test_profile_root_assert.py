"""Profile-root INTENT: a wrong-target write must be impossible (t_d39c720e).

The incident (t_10ccaf80): an acceptance harness set ``HERMES_HOME`` to a
throwaway home, believed it, and wrote the operator's live ``~/.hermes/profiles``
— then exited 0. Two facts made that possible, both measured at 9422c740:

1. ``hermes_constants._hermes_root_of`` returns the NATIVE home whenever
   ``HERMES_HOME`` resolves *under* ``~/.hermes``. A throwaway home placed in a
   Hermes scratch dir (``~/.hermes/profiles/<p>/cache/scratch/…``) therefore
   resolves straight back to the live tree. ``HERMES_HOME`` is not ignored — it
   is honoured only for a root outside the live one, and nothing tells you which
   case you are in.
2. Two roots are in play in one run and they can disagree: the native writer uses
   ``get_profile_dir`` (generator.py) while skill seeding uses
   ``rolelib.PROFILES_DIR`` (orchestrators.py). In the incident the second
   followed the sandbox and the first did not.

So these tests pin the two things that make it impossible: the assert resolves
the SAME way the writers do and refuses on divergence, and a refused run leaves
the live tree byte-for-byte untouched. Everything here runs against a FAKE live
root under ``tmp_path`` (HOME relocated before the child starts), so a
regression cannot touch the machine's real profiles even though the tests
exercise the real writers.
"""
import json
import os
import subprocess
import sys

import pytest

import rolelib

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HSCC_PY = os.path.join(PLUGIN_DIR, "hscc.py")

# Files whose (mtime, size) fingerprint a wrong-target write would change.
_WATCHED = ("config.yaml", "SOUL.md", "profile.yaml")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _file_state(root):
    """{relative path: (mtime_ns, size)} for every watched file under *root*.

    The check the incident lacked: a wrong-target write changes mtime and/or size
    of something under the live profiles root, so the fingerprint must be
    identical before and after.
    """
    state = {}
    if not os.path.isdir(root):
        return state
    for name in sorted(os.listdir(root)):
        for fn in _WATCHED:
            f = os.path.join(root, name, fn)
            if os.path.isfile(f):
                st = os.stat(f)
                state[f"{name}/{fn}"] = (st.st_mtime_ns, st.st_size)
    return state


def _fake_live_home(tmp_path):
    """A scratch HOME whose ``.hermes/profiles`` stands in for the live tree.

    Seeded with one profile so "untouched" is a real assertion about real files,
    not about an empty directory.
    """
    home = tmp_path / "fakehome"
    live = home / ".hermes" / "profiles"
    seeded = live / "seeded-profile"
    seeded.mkdir(parents=True)
    (seeded / "config.yaml").write_text(
        "toolsets:\n  - hermes-cli\n_memory_marker: do-not-touch\n",
        encoding="utf-8")
    (seeded / "SOUL.md").write_text("# seeded\n", encoding="utf-8")
    (home / ".hermes" / "config.yaml").write_text("_config_version: 7\n",
                                                  encoding="utf-8")
    return home, live


def _child_env(home, *, profile_mode=True):
    """Env for a child that believes it runs under a profile of *home*'s root.

    Profile mode is the dispatched-worker shape (HERMES_HOME points AT a profile
    dir) plus the delegated-child marker, i.e. the environment the incident ran
    in. Relocating HOME is what makes the fake root the native one for the child.
    """
    env = dict(os.environ)
    root = str(home / ".hermes")
    env["HOME"] = str(home)
    env["HERMES_REAL_HOME"] = str(home)
    env["HERMES_HOME"] = os.path.join(root, "profiles", "worker") if profile_mode else root
    env["HERMES_DELEGATED_CHILD_CONTEXT"] = "1"
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    env.setdefault("HSCC_SKIP_COMPACT_PROBE", "1")
    env.pop("HSCC_REGISTRY", None)
    for pin in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_ATTACHMENTS_ROOT"):
        env.pop(pin, None)
    return env


def _run(args, env, cwd=None):
    return subprocess.run([sys.executable, HSCC_PY, *args],
                          capture_output=True, text=True, env=env,
                          cwd=cwd or PLUGIN_DIR)


def _registry(tmp_path, projects=("alpha",)):
    reg = tmp_path / "registry.yaml"
    reg.write_text(
        "projects:\n"
        + "".join(f"  - name: {p}\n    board: {p}-board\n    repo: /srv/{p}\n"
                  for p in projects),
        encoding="utf-8")
    return str(reg)


# --------------------------------------------------------------------------- #
# the resolver rule these tests are built on (documented, not assumed)
# --------------------------------------------------------------------------- #

def test_requested_home_inside_the_native_root_resolves_back_to_live(tmp_path):
    """PREMISE, not policy: a home under the live root does NOT redirect.

    This is the behaviour the incident trusted. If hermes ever changes it, this
    test goes red and the flag's refusal becomes redundant — a good outcome, but
    one a human should learn about deliberately rather than by a silently weaker
    guard.
    """
    home = tmp_path / "fakehome"
    (home / ".hermes").mkdir(parents=True)
    inside = home / ".hermes" / "scratch" / "throwaway"
    inside.mkdir(parents=True)
    env = _child_env(home)
    env["HERMES_HOME"] = str(inside)
    code = (
        "import os,sys;sys.path.insert(0,%r)\n"
        "from hermes_cli.profiles import get_profile_dir\n"
        "import rolelib\n"
        "print(get_profile_dir('architect'))\n"
        "print(rolelib.PROFILES_DIR)\n"
    ) % PLUGIN_DIR
    res = subprocess.run([sys.executable, "-B", "-c", code],
                         capture_output=True, text=True, env=env, cwd=PLUGIN_DIR)
    if res.returncode != 0:
        pytest.skip("hermes_cli not importable on this interpreter")
    native, rolelib_dir = res.stdout.strip().splitlines()[:2]
    live = str(home / ".hermes" / "profiles")
    assert native.startswith(live), f"native resolver moved to {native}"
    # ...and rolelib followed the throwaway instead: the divergence that made the
    # incident silent (two roots, one run).
    assert rolelib_dir != native


# --------------------------------------------------------------------------- #
# assert_profile_root: it must resolve like the writer, and refuse loudly
# --------------------------------------------------------------------------- #

def test_assert_profile_root_accepts_a_honoured_intent(tmp_path):
    """A home outside the live root: apply + assert agree, nothing raises."""
    home = tmp_path / "other-home"
    home.mkdir()
    root = rolelib.apply_hermes_home(str(home))
    assert root == os.path.realpath(str(home))
    got = rolelib.assert_profile_root(os.path.join(root, "profiles"),
                                      purpose="test")
    assert got == os.path.join(root, "profiles")


def test_assert_profile_refuses_a_home_inside_the_live_root(tmp_path, monkeypatch):
    """THE INCIDENT as a function call: intent inside the live root must raise.

    Before the fix this pair (apply + write) reported success. Now the assert
    fires and nothing has been written when it does.
    """
    home, live = _fake_live_home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes" / "profiles" / "worker"))
    monkeypatch.setenv("HERMES_REAL_HOME", str(home))
    before = _file_state(live)
    throwaway = home / ".hermes" / "scratch" / "throwaway"
    throwaway.mkdir(parents=True)
    root = rolelib.apply_hermes_home(str(throwaway))
    with pytest.raises(rolelib.ProfileRootMismatch) as exc:
        rolelib.assert_profile_root(os.path.join(root, "profiles"),
                                    purpose="unit")
    msg = str(exc.value)
    assert "refusing to write" in msg
    assert "DIVERGENT" in msg
    assert "get_profile_dir" in msg
    assert _file_state(live) == before, "the refusal itself must not write"


def test_assert_profile_pins_both_roots_in_play(tmp_path, monkeypatch):
    """Each arm of the guard bites on its own.

    A guard that only checks hermes would still let skill seeding escape into a
    sandbox (or the reverse), so a divergence in EITHER root must refuse.
    """
    home = tmp_path / "clean-home"
    home.mkdir()
    rolelib.apply_hermes_home(str(home))
    expected = os.path.realpath(os.path.join(str(home), "profiles"))
    # arm 1: the fallback/skill-seeding root drifts
    monkeypatch.setattr(rolelib, "PROFILES_DIR", os.path.join(expected, "nest"))
    with pytest.raises(rolelib.ProfileRootMismatch) as exc:
        rolelib.assert_profile_root(expected)
    assert "rolelib.PROFILES_DIR" in str(exc.value)
    # arm 2: the native root drifts while rolelib is right
    monkeypatch.setattr(rolelib, "PROFILES_DIR", expected)
    monkeypatch.setattr(rolelib, "native_profiles_dir",
                        lambda: os.path.join(os.path.dirname(expected), "elsewhere"))
    with pytest.raises(rolelib.ProfileRootMismatch) as exc:
        rolelib.assert_profile_root(expected)
    assert "get_profile_dir" in str(exc.value)


def test_no_flag_leaves_the_resolved_roots_alone():
    """A plain bootstrap path must not be perturbed by any of this."""
    assert rolelib.writer_profiles_dir() == rolelib.native_profiles_dir() \
        or rolelib.native_profiles_dir() is None
    assert os.path.basename(rolelib.PROFILES_DIR) == "profiles"


# --------------------------------------------------------------------------- #
# the two profile steps: a wrong target is refused before the first write
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("args,marker", [
    (["generate"], "config.yaml"),
    (["orch-all"], "config.yaml"),
])
def test_refused_intent_writes_nothing_for_either_profile_step(
        tmp_path, args, marker):
    """``--hermes-home`` pointing INSIDE the live root: exit 2, zero writes.

    This is the check that would have caught the incident: the command is refused
    and the live fingerprint is byte-identical afterwards.
    """
    home, live = _fake_live_home(tmp_path)
    before = _file_state(live)
    throwaway = home / ".hermes" / "scratch" / "throwaway"
    throwaway.mkdir(parents=True)
    argv = list(args)
    if args[0] == "orch-all":
        argv += ["--registry", _registry(tmp_path)]
    argv += ["--hermes-home", str(throwaway)]
    res = _run(argv, _child_env(home))
    assert res.returncode == 2, f"rc={res.returncode}\nOUT:{res.stdout}\nERR:{res.stderr}"
    assert "refusing to write" in res.stderr
    # stdout stays empty of a partial report — a caller must not see a report.
    assert res.stdout.strip() == ""
    assert _file_state(live) == before, "refused run touched the live tree"
    # Nothing was scaffolded in the refused home either.
    assert not os.path.isdir(os.path.join(str(throwaway), "profiles"))


def test_two_profile_steps_write_only_the_requested_home(tmp_path):
    """The card's item 3: point the intent at a temp dir, run BOTH profile steps,
    assert the live root's mtime/size set is untouched — and that the run really
    did something (non-vacuity: the profiles must land in the requested home).
    """
    home, live = _fake_live_home(tmp_path)
    before = _file_state(live)
    other = tmp_path / "other-home"
    other.mkdir()

    gen = _run(["generate", "--hermes-home", str(other)], _child_env(home))
    assert gen.returncode == 0, f"generate rc={gen.returncode}\nERR:{gen.stderr}"
    json.loads(gen.stdout)                      # stdout stays machine JSON
    assert "profile-root intent honoured" in gen.stderr

    orch = _run(["orch-all", "--registry", _registry(tmp_path),
                 "--hermes-home", str(other)], _child_env(home))
    assert orch.returncode == 0, f"orch-all rc={orch.returncode}\nERR:{orch.stderr}"
    report = json.loads(orch.stdout)
    assert any(e["profile"] == "alpha-orch" for e in report["ensured"]), report

    profiles = other / "profiles"
    assert (profiles / "architect" / "config.yaml").is_file(), \
        "generate wrote nothing into the requested home (vacuous pass)"
    assert (profiles / "alpha-orch" / "config.yaml").is_file(), \
        "orch-all wrote nothing into the requested home (vacuous pass)"
    assert _file_state(live) == before, "the live root was written by a sandboxed run"
    # ...and the fake-live seed is still the seed, not a generated profile.
    assert "_memory_marker" in (live / "seeded-profile" / "config.yaml").read_text()


def test_no_flag_still_targets_the_real_home(tmp_path):
    """Constraint: what a real operator bootstrap targets must not change.

    With no ``--hermes-home`` the run must write the home the environment names —
    here the fake live root — and print no intent note at all.
    """
    home, live = _fake_live_home(tmp_path)
    res = _run(["generate"], _child_env(home))
    assert res.returncode == 0, f"rc={res.returncode}\nERR:{res.stderr}"
    assert "profile-root intent" not in res.stderr
    assert (live / "architect" / "config.yaml").is_file(), \
        "no-flag generate stopped targeting the ambient home — bootstrap moved"
    # The operator's marker file is preserved by the writer, never removed.
    assert "_memory_marker" in (live / "seeded-profile" / "config.yaml").read_text()


def test_unknown_generate_argument_is_a_usage_error(tmp_path):
    """A mistyped sandbox flag must not be silently ignored (that is how a
    harness ends up believing a write was redirected)."""
    res = _run(["generate", "--profiles-dir", str(tmp_path)], _child_env(tmp_path / "h"))
    assert res.returncode == 1
    assert "--hermes-home" in res.stderr


def test_home_flag_without_a_value_is_a_usage_error(tmp_path):
    res = _run(["orch-all", "--hermes-home"], _child_env(tmp_path / "h"))
    assert res.returncode == 1
    assert "--hermes-home" in res.stderr
