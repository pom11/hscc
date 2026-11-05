"""``onboard.py`` must mirror model.default against the Hermes ROOT, not $HERMES_HOME.

What shipped, verbatim (reproduced 2026-11-05 on this checkout, fabricated root,
``HERMES_HOME=<root>/profiles/alpha`` — i.e. how every dispatched worker runs)::

    module-level HERMES_HOME  = /tmp/f5root.../profiles/alpha
    module-level PROFILES_DIR = /tmp/f5root.../profiles/alpha/profiles  exists: False
      hermes config.yaml: model.default updated          # <- alpha's own file
    --- resulting model.default values ---
    ROOT   old/model-A      # untouched, though wire claimed to edit "the" root
    alpha  new/model-B
    beta   old/model-A      # skipped: the walk never saw a sibling

Three module-level constants at ``onboard.py:41-43`` joined ``$HERMES_HOME`` at
IMPORT time. Hermes exports ``HERMES_HOME`` as the *profile* dir
(``<root>/profiles/<name>``) for every profile session — the fleet's normal run
mode — so the per-profile ``model.default`` mirroring silently skipped EVERY
profile while editing the profile's own config as if it were the root, and
printed a green line (audit t_8893ab89 finding 5).

Pinned here:

1. PROFILE SESSION. With ``HERMES_HOME=<root>/profiles/alpha`` the mirror walks
   ``ROOT/profiles/*`` and edits ``ROOT/config.yaml`` — root, alpha and beta all
   move; the profile dir is never mistaken for the root.
2. LAZY RESOLUTION. The root is resolved at call time, so an env change after
   import takes effect (an import-time constant could not).
3. EXPLICIT OVERRIDE. ``$HSCC_HERMES_ROOT`` wins outright, marker-free.
4. NO SILENT SKIP. When no home resolves, the step reports the paths it ruled
   out instead of printing green.
5. BACKUP COVERS THE WRITE SET. ``backup_configs`` snapshots the root config and
   EVERY profile config — the old set carried a hard-coded "worker" name from a
   directory that did not exist under a profile session.
6. LADDER DRIFT. The script's standalone fallback equals
   ``hscc_daemon.hermes_python.hermes_homes`` (t_314eb13c), which the profile
   walk-up depends on.

Everything runs against FABRICATED tmp homes; the operator's ``~/.hermes``,
``~/.hscc`` and the fleet are never touched (no topology, no ssh, no sparkrun).
"""

import importlib.util
import os
import sys

import pytest

from hscc_daemon import hermes_python

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ONBOARD_SRC = os.path.join(
    REPO, "install", "hscc-skills", "hscc-model-onboard", "scripts", "onboard.py")

_ROOT_CFG = (
    "model:\n"
    "  default: old/model-A\n"
    "  temperature: 0.7\n"
    "secrets:\n"
    "  token: not-a-real-secret\n"
)


def load_onboard(name="onboard_under_test"):
    """Import the skill script as a standalone module (it is not a package)."""
    spec = importlib.util.spec_from_file_location(name, ONBOARD_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def onboard(monkeypatch, tmp_path):
    """Fresh module, with the ambient HERMES_HOME/HSCC_HERMES_ROOT/HOME env scrubbed.

    The dispatcher exports HERMES_HOME at the live profile. Scrubbing it AND
    ``HOME`` (so the ladder's ``~/.hermes`` step lands inside tmp_path) means a
    test that forgets to point the env at a fabricated tree resolves to *nothing*
    rather than to the operator's real ``~/.hermes`` — this suite edits configs,
    so the safe default is "no home", never "the live home". Cases that need a
    home set the env explicitly.
    """
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HSCC_HERMES_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    return load_onboard()


def mk_root(tmp_path, profiles=("alpha", "beta"), *, profile_cfg=True):
    """Fabricate a Hermes ROOT with ``profiles/<name>`` children; return the root.

    ``profile_cfg``: each profile gets its own ``config.yaml`` with a
    ``model.default`` — a profile dir carries its own config, which is exactly
    why a marker test alone cannot tell a profile from a root.
    """
    root = tmp_path / "hermes"
    (root / "hermes-agent").mkdir(parents=True)
    (root / "config.yaml").write_text(_ROOT_CFG)
    for name in profiles:
        pdir = root / "profiles" / name
        pdir.mkdir(parents=True)
        if profile_cfg:
            (pdir / "config.yaml").write_text(_ROOT_CFG)
    return root


def model_default(path):
    for line in open(path):
        if line.strip().startswith("default:"):
            return line.split(":", 1)[1].strip()
    return None


# ── 1. the headline case: a profile session must mirror the whole tree ────── #

def test_profile_scoped_home_mirrors_root_and_every_profile(onboard, tmp_path,
                                                            monkeypatch, capsys):
    root = mk_root(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "alpha"))

    ok = onboard.wire_hermes_and_profiles("new/model-B")

    assert ok is True
    assert model_default(root / "config.yaml") == "new/model-B"
    assert model_default(root / "profiles" / "alpha" / "config.yaml") == "new/model-B"
    assert model_default(root / "profiles" / "beta" / "config.yaml") == "new/model-B"
    # The old code printed a green "hermes config.yaml" line while the real root
    # kept the old model; the resolution must now be visible in the output.
    out = capsys.readouterr().out
    assert str(root) in out
    assert "profile beta: model.default updated" in out


def test_root_scoped_home_still_works(onboard, tmp_path, monkeypatch):
    """No regression for a plain (root-scoped) session."""
    root = mk_root(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root))

    assert onboard.wire_hermes_and_profiles("new/model-B") is True
    assert model_default(root / "config.yaml") == "new/model-B"
    assert model_default(root / "profiles" / "beta" / "config.yaml") == "new/model-B"


def test_home_without_profiles_never_wins_over_the_root(onboard, tmp_path,
                                                        monkeypatch):
    """A ``profiles/<name>`` candidate is skipped even though it has a config.yaml.

    This is the discrimination the old join lacked: the profile dir satisfies
    "there is a config.yaml here", so a marker-only test would re-create the bug
    one level down.
    """
    root = mk_root(tmp_path)
    profile = root / "profiles" / "alpha"
    monkeypatch.setenv("HERMES_HOME", str(profile))

    resolved, _ = onboard.resolve_hermes_root()
    assert resolved == str(root)
    assert onboard._is_profile_dir(str(profile)) is True
    assert onboard._is_profile_dir(str(root)) is False


# ── 2. lazy, not import-time ─────────────────────────────────────────────── #

def test_resolution_is_deferred_not_import_time(onboard, tmp_path, monkeypatch):
    """The module must not carry a baked-in HERMES_HOME/PROFILES_DIR constant.

    A bare-terminal run, a profile session and a cron watcher import this script
    under different env; a constant frozen the first one for the whole process.
    """
    for stale in ("HERMES_HOME", "HERMES_CONFIG", "PROFILES_DIR"):
        assert not hasattr(onboard, stale), (
            f"{stale} is back as a module-level constant — resolution must stay lazy")

    root = mk_root(tmp_path)
    # Env set AFTER import (the module is already loaded) must still take effect.
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "alpha"))
    assert onboard.resolve_hermes_root()[0] == str(root)
    monkeypatch.setenv("HERMES_HOME", str(root))
    assert onboard.resolve_hermes_root()[0] == str(root)


# ── 3. explicit override ─────────────────────────────────────────────────── #

def test_explicit_override_is_honoured(onboard, tmp_path, monkeypatch):
    """$HSCC_HERMES_ROOT wins outright and beats the tree $HERMES_HOME points at."""
    custom = tmp_path / "opt" / "hermes-install"
    (custom / "profiles" / "solo").mkdir(parents=True)
    (custom / "config.yaml").write_text(_ROOT_CFG)
    (custom / "profiles" / "solo" / "config.yaml").write_text(_ROOT_CFG)
    decoy = mk_root(tmp_path)  # a perfectly good tree the override must beat
    monkeypatch.setenv("HERMES_HOME", str(decoy))
    monkeypatch.setenv("HSCC_HERMES_ROOT", str(custom))

    assert onboard.resolve_hermes_root()[0] == str(custom)
    assert onboard.hermes_home_candidates() == [str(custom)]

    assert onboard.wire_hermes_and_profiles("new/model-B") is True
    assert model_default(custom / "config.yaml") == "new/model-B"
    assert model_default(custom / "profiles" / "solo" / "config.yaml") == "new/model-B"
    # The overridden tree is edited; the tree the env pointed at is not.
    assert model_default(decoy / "config.yaml") == "old/model-A"


def test_override_to_a_missing_dir_is_reported_not_silently_ok(onboard, tmp_path,
                                                               monkeypatch, capsys):
    root = mk_root(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HSCC_HERMES_ROOT", str(tmp_path / "not-installed"))

    resolved, searched = onboard.resolve_hermes_root()
    assert resolved is None and "not-installed" in searched

    assert onboard.wire_hermes_and_profiles("new/model-B") is False
    assert model_default(root / "config.yaml") == "old/model-A"
    out = capsys.readouterr().out
    assert "NO Hermes home resolved" in out and "not-installed" in out


# ── 4. no silent skip ────────────────────────────────────────────────────── #

def test_no_hermes_anywhere_reports_rather_than_printing_green(onboard, tmp_path,
                                                               monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    monkeypatch.delenv("HERMES_HOME", raising=False)

    resolved, searched = onboard.resolve_hermes_root(home=str(tmp_path / "empty-home"))
    assert resolved is None
    assert searched  # names what it ruled out

    assert onboard.wire_hermes_and_profiles("new/model-B") is False
    out = capsys.readouterr().out
    assert "NO Hermes home resolved" in out
    assert "model.default updated" not in out


# ── 5. backup covers the write set ───────────────────────────────────────── #

def test_backup_snapshots_root_and_every_profile(onboard, tmp_path, monkeypatch):
    """The rollback set must match what wire writes.

    Old code: ``for n in ("worker",)`` over a PROFILES_DIR that did not exist
    under a profile session — so from a worker, wire was about to edit N configs
    and backed up zero of them.
    """
    root = mk_root(tmp_path, profiles=("alpha", "beta", "worker"))
    hscc = tmp_path / "hscc"
    hscc.mkdir()
    monkeypatch.setattr(onboard, "HSCC_HOME", str(hscc))
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "alpha"))

    dest = onboard.backup_configs()

    assert os.path.exists(os.path.join(dest, "hermes-config.yaml"))
    for name in ("alpha", "beta", "worker"):
        p = os.path.join(dest, f"profile-{name}-config.yaml")
        assert os.path.exists(p), f"{name} config not backed up"
        assert "not-a-real-secret" in open(p).read()


def test_backup_survives_unresolvable_home(onboard, tmp_path, monkeypatch, capsys):
    """No home -> no hermes copies, but the ~/.hscc copies still happen and the
    gap is announced (a missing backup set must not read as 'nothing to back up')."""
    hscc = tmp_path / "hscc"
    hscc.mkdir()
    (hscc / "serving.json").write_text("{}\n")
    monkeypatch.setattr(onboard, "HSCC_HOME", str(hscc))
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(onboard, "resolve_hermes_root",
                        lambda env=None, *, home=None: (None, "nothing here"))

    dest = onboard.backup_configs()

    assert os.path.exists(os.path.join(dest, "serving.json"))
    assert "NOT backed up" in capsys.readouterr().out


# ── 6. ladder drift ──────────────────────────────────────────────────────── #

@pytest.mark.parametrize("hermes_home,home", [
    ("", "fake-home"),
    ("/srv/hermes", "fake-home"),
    ("/srv/hermes/profiles/worker", "fake-home"),
    ("/srv/hermes/profiles/worker/sub", "fake-home"),
    ("/no-such/profiles", "fake-home"),
])
def test_ladder_drift(onboard, hermes_home, home):
    """The standalone fallback must equal the shared ladder (t_314eb13c).

    A shorter list here re-creates the bug for any install where the script
    cannot import ``hscc_daemon`` (skills-only deploy), and it would only show
    up on the operator's machine — which is how this class of bug kept coming
    back.
    """
    env = {"HERMES_HOME": hermes_home} if hermes_home else {}
    assert onboard._fallback_homes(env, home=home) == hermes_python.hermes_homes(
        env, home=home), f"ladder drift for env={env} home={home}"


def test_script_prefers_the_shared_ladder_module(onboard):
    """The script must resolve through the shared module when it is importable."""
    hp = onboard._load_hermes_python()
    assert hp is not None, "walk-up import of hscc_daemon.hermes_python failed"
    assert hp.hermes_homes is hermes_python.hermes_homes


# ── 7. mirror integrity + partial trees ──────────────────────────────────── #

def test_mirror_preserves_everything_but_the_default_line(onboard, tmp_path,
                                                          monkeypatch):
    root = mk_root(tmp_path)
    before = (root / "profiles" / "beta" / "config.yaml").read_text()
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "alpha"))

    onboard.wire_hermes_and_profiles("new/model-B")

    after = (root / "profiles" / "beta" / "config.yaml").read_text()
    assert after == before.replace("old/model-A", "new/model-B")
    assert "not-a-real-secret" in after  # secrets untouched, still present


def test_profile_without_config_is_announced_not_silently_dropped(onboard, tmp_path,
                                                                  monkeypatch, capsys):
    root = mk_root(tmp_path, profiles=("alpha", "beta"))
    (root / "profiles" / "beta" / "config.yaml").unlink()
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "alpha"))

    onboard.wire_hermes_and_profiles("new/model-B")

    out = capsys.readouterr().out
    assert "profiles without a config.yaml (skipped): beta" in out


def test_hidden_dirs_are_not_profiles(onboard, tmp_path, monkeypatch, capsys):
    root = mk_root(tmp_path, profiles=("alpha", "beta"))
    (root / "profiles" / ".git").mkdir()
    (root / "profiles" / ".git" / "config.yaml").write_text(_ROOT_CFG)
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "alpha"))

    onboard.wire_hermes_and_profiles("new/model-B")

    assert model_default(root / "profiles" / ".git" / "config.yaml") == "old/model-A"
    assert ".git" not in capsys.readouterr().out
