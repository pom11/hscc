"""The two SYNC commands must not write a home nobody asked for (t_7edf58ce).

``generate`` / ``orch`` / ``orch-all`` got ``--hermes-home`` in t_d39c720e.
``orch-sync`` and ``reasoning-sync`` were left out of that diff and are the same
class of hazard: both resolve ``rolelib.PROFILES_DIR`` and write into it —
``orch_sync.sync_all`` rewrites ``SOUL.md`` and copies skill dirs,
``reasoning_sync.sync_all`` rewrites ``config.yaml`` keys — with no intent check
at all. A harness that set ``HERMES_HOME`` to a throwaway home *inside*
``~/.hermes`` believed it was sandboxed while the run wrote the operator's tree
(``hermes_constants._hermes_root_of`` returns the native home for any
``HERMES_HOME`` resolving under ``~/.hermes``; measured at ``9422c740``).

So these tests pin the same three things for the sync pair, against a FAKE live
root under ``tmp_path`` with HOME relocated for the child only:

1. a refused intent exits 2, prints nothing on stdout (stdout here is
   machine-captured JSON) and leaves the live tree's ``(mtime_ns, size)``
   fingerprint identical — file-for-file, including files that did not exist
   before;
2. non-vacuity, twice over: the same seed WITHOUT the flag really does change
   that fingerprint (so "unchanged" means something), and a sandboxed run really
   does produce its output in the requested home;
3. ``orch-sync``'s READ root moves with the flag too: ``--skills-source``
   defaults to ``<hermes-home>/skills``, so a sandboxed sync never copies the
   operator's own skill tree into a throwaway profile.
"""
import json
import os
import subprocess
import sys

import pytest

import orch_sync

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HSCC_PY = os.path.join(PLUGIN_DIR, "hscc.py")

# One skill is enough to discriminate read roots; take the name from the pack so
# the test cannot drift from what the syncer actually looks for.
PACK_SKILL = orch_sync.ORCH_SKILL_PACK[0]

_POLICY = {"tiers": {"max": "high", "standard": "medium", "light": "low"}}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _tree_state(root):
    """{relative path: (mtime_ns, size)} for EVERY file under *root*.

    Stronger than the parent test's watched-name list: ``orch-sync`` writes
    ``SOUL.md``, a ``SOUL.md.bak-<stamp>`` and new files under
    ``<profile>/skills/``, so a fingerprint that only knew three filenames could
    call a real write "unchanged". Walking everything means a new file, a
    rewritten file or a backup all show up.
    """
    state = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            f = os.path.join(dirpath, fn)
            try:
                st = os.stat(f)
            except OSError:
                continue
            state[os.path.relpath(f, root)] = (st.st_mtime_ns, st.st_size)
    return state


def _soul(profile):
    """A syncable orchestrator SOUL whose ops tail is stale, so a sync MUST
    change it — the seed is a real write target, not a no-op."""
    return (f"# stale base for {profile}\n"
            "\n## Role: " + profile + "\n"
            "project-specific role text that compose() keeps byte-for-byte\n"
            "\n## Operational\nstale operational tail that the template replaces\n")


def _make_profile(profiles_dir, name, *, orch=False):
    """Create ``<profiles_dir>/<name>/config.yaml`` (+ SOUL.md when orch)."""
    pdir = os.path.join(profiles_dir, name)
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, "config.yaml"), "w", encoding="utf-8") as f:
        f.write("toolsets:\n  - hermes-cli\n"
                "_memory_marker: do-not-touch\n")
    if orch:
        with open(os.path.join(pdir, "SOUL.md"), "w", encoding="utf-8") as f:
            f.write(_soul(name))
    return pdir


def _skill_tree(skills_root, marker):
    """A shared skill pack containing ``PACK_SKILL`` carrying ``marker``.

    The marker is what proves WHICH tree was read: the copy that lands in the
    profile keeps the text of whichever source supplied it.
    """
    sdir = os.path.join(skills_root, PACK_SKILL)
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {PACK_SKILL}\n---\nskill body from {marker}\n")
    return skills_root


def _fake_live_home(tmp_path):
    """A scratch HOME whose ``.hermes`` stands in for the operator's live tree.

    Seeded so BOTH sync commands have real work to do there: a per-project
    orchestrator with a stale SOUL and no shared skills (an ``orch-sync``
    target) and a plain profile with a config.yaml (a ``reasoning-sync``
    target). Plus a live skills tree with its own marker, so "this run read the
    operator's skills" is detectable rather than assumed.
    """
    home = tmp_path / "fakehome"
    live_root = home / ".hermes"
    live = live_root / "profiles"
    _make_profile(live, "seeded-profile")
    _make_profile(live, "alpha-orch", orch=True)
    _skill_tree(os.path.join(str(live_root), "skills"), "OPERATOR-LIVE-TREE")
    (live_root / "config.yaml").write_text("_config_version: 7\n", encoding="utf-8")
    return home, live


def _child_env(home):
    """Env for a child that believes it runs under a profile of *home*'s root —
    the dispatched-worker shape from the incident. Relocating HOME is what makes
    the fake root the native one for the child."""
    env = dict(os.environ)
    root = str(home / ".hermes")
    env["HOME"] = str(home)
    env["HERMES_REAL_HOME"] = str(home)
    env["HERMES_HOME"] = os.path.join(root, "profiles", "worker")
    env["HERMES_DELEGATED_CHILD_CONTEXT"] = "1"
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    env.setdefault("HSCC_SKIP_COMPACT_PROBE", "1")
    # The reasoning policy is cluster-global and lives OUTSIDE the hermes home
    # (~/.hscc by default), so it needs its own pin — see _with_policy.
    env.pop("HSCC_REGISTRY", None)
    for pin in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_ATTACHMENTS_ROOT"):
        env.pop(pin, None)
    return env


def _with_policy(env, tmp_path):
    """Point $HSCC_REASONING_POLICY at a temp policy so reasoning-sync has one."""
    path = os.path.join(str(tmp_path), "reasoning_policy.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_POLICY, f)
    env["HSCC_REASONING_POLICY"] = path
    return env


def _run(args, env, cwd=None):
    return subprocess.run([sys.executable, HSCC_PY, *args],
                          capture_output=True, text=True, env=env,
                          cwd=cwd or PLUGIN_DIR)


def _reasoning_sync_args(extra=()):
    return ["reasoning-sync", "--json", *extra]


def _orch_sync_args(extra=()):
    return ["orch-sync", "--json", *extra]


SYNC_COMMANDS = {
    "orch-sync": _orch_sync_args,
    "reasoning-sync": _reasoning_sync_args,
}


# --------------------------------------------------------------------------- #
# refusal: exit 2, empty stdout, live tree byte-identical
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cmd", sorted(SYNC_COMMANDS))
def test_refused_intent_writes_nothing(tmp_path, cmd):
    """``--hermes-home`` INSIDE the live root: refused before the first write.

    This is the check that was missing for the sync pair: the throwaway home is
    the placement the incident used, the live seed has real sync work queued,
    and nothing may be written.
    """
    home, live = _fake_live_home(tmp_path)
    before = _tree_state(live)
    throwaway = home / ".hermes" / "scratch" / "throwaway"
    throwaway.mkdir(parents=True)
    env = _with_policy(_child_env(home), tmp_path)
    # --json is part of the assertion: stdout on these commands is
    # machine-captured JSON, so a refusal must leave it EMPTY, not partial.
    res = _run(SYNC_COMMANDS[cmd](["--hermes-home", str(throwaway)]), env)
    assert res.returncode == 2, f"rc={res.returncode}\nOUT:{res.stdout}\nERR:{res.stderr}"
    assert "refusing to write" in res.stderr
    assert res.stdout.strip() == "", f"a refused run leaked a report on stdout: {res.stdout!r}"
    assert _tree_state(live) == before, "refused run touched the live tree"
    # and nothing was scaffolded in the refused home either
    assert not os.path.isdir(os.path.join(str(throwaway), "profiles"))


@pytest.mark.parametrize("cmd", sorted(SYNC_COMMANDS))
def test_the_refused_seed_is_a_real_write_target(tmp_path, cmd):
    """NON-VACUITY for the refusal above: the same seed, no flag, really writes.

    A guard looks identical whether or not there was anything to guard, so this
    pins that the live fingerprint above WOULD have changed. It is also the
    constraint that a real operator bootstrap still targets the ambient home.
    """
    home, live = _fake_live_home(tmp_path)
    before = _tree_state(live)
    env = _with_policy(_child_env(home), tmp_path)
    res = _run(SYNC_COMMANDS[cmd](), env)
    assert res.returncode == 0, f"rc={res.returncode}\nOUT:{res.stdout}\nERR:{res.stderr}"
    report = json.loads(res.stdout)          # stdout stays machine JSON
    assert report["dry_run"] is False
    assert "profile-root intent" not in res.stderr, "no flag must apply/assert nothing"
    after = _tree_state(live)
    assert after != before, (
        "the seed was not a write target — the refusal test above proves nothing")
    if cmd == "orch-sync":
        soul = open(os.path.join(live, "alpha-orch", "SOUL.md"),
                    encoding="utf-8").read()
        assert "stale operational tail" not in soul, "orch-sync did not rewrite SOUL.md"
        assert "do-not-touch" in open(os.path.join(live, "seeded-profile", "config.yaml"),
                                      encoding="utf-8").read()
    else:
        cfg = open(os.path.join(live, "seeded-profile", "config.yaml"),
                   encoding="utf-8").read()
        assert "reasoning_effort" in cfg, "reasoning-sync did not touch config.yaml"
        assert "do-not-touch" in cfg, "reasoning-sync dropped a key it does not own"


# --------------------------------------------------------------------------- #
# honoured intent: writes the requested home ONLY, and really writes it
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cmd", sorted(SYNC_COMMANDS))
def test_sync_writes_only_the_requested_home(tmp_path, cmd):
    """Point the intent at a home OUTSIDE the live root: it writes there, only.

    Both halves are asserted, because either one alone passes vacuously: the
    live fingerprint must be unmoved AND the requested home must contain the
    output the report claims.
    """
    home, live = _fake_live_home(tmp_path)
    before = _tree_state(live)
    other = tmp_path / "other-home"
    other_profiles = os.path.join(str(other), "profiles")
    os.makedirs(other_profiles)
    _make_profile(other_profiles, "seeded-profile")
    _make_profile(other_profiles, "alpha-orch", orch=True)
    _skill_tree(os.path.join(str(other), "skills"), "REQUESTED-HOME")
    env = _with_policy(_child_env(home), tmp_path)

    res = _run(SYNC_COMMANDS[cmd](["--hermes-home", str(other)]), env)
    assert res.returncode == 0, f"rc={res.returncode}\nOUT:{res.stdout}\nERR:{res.stderr}"
    assert "profile-root intent honoured" in res.stderr
    report = json.loads(res.stdout)
    names = [p["profile"] for p in report["profiles"]]
    # The two commands have different scopes, and the scope is not a detail:
    # orch-sync touches only per-project orchestrators (never a plain profile's
    # SOUL or config), reasoning-sync touches every profile with a config.yaml.
    assert "alpha-orch" in names, report
    if cmd == "orch-sync":
        assert "seeded-profile" not in names, \
            f"orch-sync reached outside its scope: {names}"
    else:
        assert "seeded-profile" in names, report

    if cmd == "orch-sync":
        soul = open(os.path.join(other_profiles, "alpha-orch", "SOUL.md"),
                    encoding="utf-8").read()
        assert "stale operational tail" not in soul, \
            "orch-sync wrote nothing into the requested home (vacuous pass)"
    else:
        cfg = open(os.path.join(other_profiles, "seeded-profile", "config.yaml"),
                   encoding="utf-8").read()
        assert "reasoning_effort" in cfg, \
            "reasoning-sync wrote nothing into the requested home (vacuous pass)"
    assert _tree_state(live) == before, "a sandboxed sync wrote the live tree"


# --------------------------------------------------------------------------- #
# orch-sync's READ root: --skills-source defaults to the same home as the write
# --------------------------------------------------------------------------- #

def test_skills_source_default_follows_hermes_home(tmp_path):
    """A sandboxed sync must not quietly read the operator's skill tree.

    Both trees carry a different marker and the flag is the only thing that
    selects between them, so this fails if the default keeps resolving to
    ``~/.hermes/skills`` — which is what it did before the flag existed.
    """
    home, live = _fake_live_home(tmp_path)
    other = tmp_path / "other-home"
    other_profiles = os.path.join(str(other), "profiles")
    os.makedirs(other_profiles)
    _make_profile(other_profiles, "alpha-orch", orch=True)
    _skill_tree(os.path.join(str(other), "skills"), "REQUESTED-HOME")
    env = _child_env(home)

    res = _run(_orch_sync_args(["--hermes-home", str(other)]), env)
    assert res.returncode == 0, f"rc={res.returncode}\nERR:{res.stderr}"
    copied = os.path.join(other_profiles, "alpha-orch", "skills", PACK_SKILL, "SKILL.md")
    assert os.path.isfile(copied), \
        "the shared pack was not copied at all (vacuous pass — check the source default)"
    body = open(copied, encoding="utf-8").read()
    assert "REQUESTED-HOME" in body, \
        "the sync read the OPERATOR's skill tree into a sandboxed profile"
    assert "OPERATOR-LIVE-TREE" not in body


def test_explicit_skills_source_beats_the_home_flag(tmp_path):
    """``--skills-source`` always wins, so a caller can pin the read root."""
    home, _live = _fake_live_home(tmp_path)
    other = tmp_path / "other-home"
    other_profiles = os.path.join(str(other), "profiles")
    os.makedirs(other_profiles)
    _make_profile(other_profiles, "alpha-orch", orch=True)
    _skill_tree(os.path.join(str(other), "skills"), "REQUESTED-HOME")
    explicit = _skill_tree(os.path.join(str(tmp_path), "shared-skills"), "EXPLICIT-SOURCE")
    env = _child_env(home)

    res = _run(_orch_sync_args(["--hermes-home", str(other),
                                "--skills-source", explicit]), env)
    assert res.returncode == 0, f"rc={res.returncode}\nERR:{res.stderr}"
    body = open(os.path.join(other_profiles, "alpha-orch", "skills",
                             PACK_SKILL, "SKILL.md"), encoding="utf-8").read()
    assert "EXPLICIT-SOURCE" in body and "REQUESTED-HOME" not in body


def test_no_flag_orch_sync_still_reads_the_ambient_skills_tree(tmp_path):
    """Constraint: with no flag, nothing moves — writes and reads both stay on
    the ambient home, which is what a real ``hscc template apply`` needs."""
    home, live = _fake_live_home(tmp_path)
    env = _child_env(home)
    res = _run(_orch_sync_args(), env)
    assert res.returncode == 0, f"rc={res.returncode}\nERR:{res.stderr}"
    body = open(os.path.join(live, "alpha-orch", "skills", PACK_SKILL, "SKILL.md"),
                encoding="utf-8").read()
    assert "OPERATOR-LIVE-TREE" in body, \
        "no-flag orch-sync stopped reading the ambient skills tree"


def test_dry_run_with_flag_writes_neither_home(tmp_path):
    """``--dry-run`` under a honoured intent reports and writes nothing.

    The report must still say what it WOULD have done, or this passes by
    doing nothing.
    """
    home, live = _fake_live_home(tmp_path)
    before_live = _tree_state(live)
    other = tmp_path / "other-home"
    other_profiles = os.path.join(str(other), "profiles")
    os.makedirs(other_profiles)
    _make_profile(other_profiles, "alpha-orch", orch=True)
    _skill_tree(os.path.join(str(other), "skills"), "REQUESTED-HOME")
    before_other = _tree_state(other)
    env = _child_env(home)

    res = _run(["orch-sync", "--dry-run", "--json",
                "--hermes-home", str(other)], env)
    assert res.returncode == 0, f"rc={res.returncode}\nERR:{res.stderr}"
    report = json.loads(res.stdout)
    assert report["dry_run"] is True
    alpha = [p for p in report["profiles"] if p["profile"] == "alpha-orch"][0]
    assert alpha["soul"] == "updated" and PACK_SKILL in alpha["skills_added"], \
        f"dry run reported nothing to do: {alpha}"
    assert _tree_state(live) == before_live
    assert _tree_state(other) == before_other, "dry run wrote the requested home"


# --------------------------------------------------------------------------- #
# argument handling: the flag must be visible and never silently ignored
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cmd", sorted(SYNC_COMMANDS))
def test_home_flag_without_a_value_is_a_usage_error(tmp_path, cmd):
    res = _run(SYNC_COMMANDS[cmd]() + ["--hermes-home"],
               _with_policy(_child_env(tmp_path / "h"), tmp_path))
    assert res.returncode == 1
    assert "--hermes-home" in res.stderr


@pytest.mark.parametrize("cmd", sorted(SYNC_COMMANDS))
def test_unknown_argument_is_a_usage_error_that_names_the_flag(tmp_path, cmd):
    """A mistyped sandbox flag must not be ignored — that is how a harness ends
    up believing a write was redirected."""
    res = _run(SYNC_COMMANDS[cmd]() + ["--profiles-dir", str(tmp_path)],
               _with_policy(_child_env(tmp_path / "h"), tmp_path))
    assert res.returncode == 1
    assert "--hermes-home" in res.stderr


def test_help_documents_the_flag_and_the_skills_source(tmp_path):
    """The operator-facing help must say the read root exists and what moves it,
    or a sandboxed run still has no way to know it read the live tree."""
    res = subprocess.run([sys.executable, HSCC_PY, "--help"],
                         capture_output=True, text=True, env=_child_env(tmp_path / "h"),
                         cwd=PLUGIN_DIR)
    assert res.returncode == 0
    doc = res.stdout
    assert doc.count("--hermes-home") >= 5, "the sync pair must show the flag in help"
    orch_block = doc[doc.index("orch-sync"):doc.index("reasoning-sync")]
    assert "skills" in orch_block and "hermes-home" in orch_block
    assert "defaults to <hermes-home>/skills" in doc.replace("\n", " ").replace("  ", " ") \
        or "defaults to <hermes-home>/skills" in doc
