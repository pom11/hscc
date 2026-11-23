"""The NEW-profile half of R5: seed a home that has no config.yaml yet.

Card t_d1f2faa7. The tree sweep and ``ensure_plugins_tier_for_home`` both stop at
a home with no ``config.yaml`` — correctly, because Hermes owns that file. The
problem is what a freshly provisioned profile home actually is: measured against a
sandboxed ``HOME``/``HERMES_HOME`` (no live write), ``hermes profile create``
produces ``.env``, ``SOUL.md``, ``skills/`` and **no config at all**, because
``finish_profile_layout`` → ``_migrate_profile_config_if_outdated`` returns
immediately when there is nothing to migrate (``profiles_config.py:15-16``).

Two consequences, both measured, neither inferred:

* Hermes creates that config on the profile's first run and stamps it with the
  CURRENT ``_config_version`` (observed: ``_config_version: 52``), so
  ``_migrate_to_21`` never runs for that home again — a missing ``plugins`` key is
  never filled by anything, and D-C's "hooks register in every home" is simply
  never true there.
* Where the version *is* old (a cloned or legacy home), ``_migrate_to_21`` fills
  ``plugins.enabled`` with every ``plugin.yaml``-bearing dir under the home's own
  ``plugins/`` — measured at 10 names in a live ``<name>-orch`` home, i.e. the
  whole cluster surface in a home that must hold hooks only.

So the new-profile path gets its own entry point, and it writes a tier-only
config where the sweep declines. Tier still comes from ``main_orch`` and nowhere
else (D-A): this file spells no profile name and no plugin tier of its own.
"""
import os

import pytest
import yaml

import enable_plugins

CLUSTER = ["hscc-cluster", "hscc-commands", "sparkrun-hermes"]
HOOKS_ONLY = ["hscc-cluster"]
MAIN = "ops"                      # deliberately NOT the resolver's default name


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A fake Hermes tree + an HSCC_DIR storing a NON-default main name.

    ``MAIN``'s home and one other profile home exist with configs; the
    ``<project>-orch`` home exists with NO config.yaml, which is the state
    ``hermes profile create`` actually leaves behind.
    """
    root = tmp_path / "hermes"
    hscc = tmp_path / "hscc"
    hscc.mkdir()
    monkeypatch.setenv("HSCC_DIR", str(hscc))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_new_home_test_main_orch",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "hscc-roles", "main_orch.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.store_name(MAIN, registry_path="/nonexistent.yaml")

    def home(name, cfg=None):
        d = root / "profiles" / name
        d.mkdir(parents=True, exist_ok=True)
        if cfg is not None:
            (d / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False),
                                           encoding="utf-8")
        return d

    monkeypatch.setattr(enable_plugins, "CLUSTER_GUARD_DST",
                        str(tmp_path / "hooks" / "cluster-guard.py"))
    monkeypatch.setattr(enable_plugins, "HOOKS_DIR", str(tmp_path / "hooks"))
    return {
        "root": root,
        MAIN: home(MAIN, {"toolsets": ["hermes-cli"]}),
        "orch": home("pickolo-orch"),                    # NO config.yaml
        "worker": home("backend-engineer", {"toolsets": ["hermes-cli"]}),
    }


def _doc(home):
    path = home / "config.yaml"
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _enabled(home):
    return (_doc(home).get("plugins") or {}).get("enabled") if _doc(home) else None


# ── the new-home write ───────────────────────────────────────────────────────

def test_new_home_without_config_gets_the_hooks_tier(tree):
    """The gap this card owns: a config-less per-project orch home."""
    rec = enable_plugins.ensure_plugins_tier_for_new_home(str(tree["orch"]))
    assert rec is not None
    assert rec["tier"] == "pickolo-orch"
    assert _enabled(tree["orch"]) == HOOKS_ONLY
    assert rec["added"] == HOOKS_ONLY and rec["pruned"] == []


def test_new_home_is_tiered_by_the_resolver_not_by_a_spelled_name(tree):
    """The SAME call on <main>'s home opens the tier; on any other it closes it.

    Only ``main_orch`` knows which home is ``<main>`` — the two assertions are
    one call each, so a hard-coded name or a hard-coded tier could not pass both.
    """
    main_rec = enable_plugins.ensure_plugins_tier_for_new_home(str(tree[MAIN]))
    orch_rec = enable_plugins.ensure_plugins_tier_for_new_home(str(tree["orch"]))
    assert _enabled(tree[MAIN]) == CLUSTER
    assert _enabled(tree["orch"]) == HOOKS_ONLY
    assert main_rec["tier"] == MAIN and orch_rec["tier"] == "pickolo-orch"


def test_written_config_carries_nothing_but_the_tier(tree):
    """This is the tier write, not the wire — no toolsets/kanban/delegation.

    A provisioning step that wanted the full bootstrap wire would have to ask for
    it; silently provisioning a per-project orchestrator with the main install's
    toolsets, kanban routing, delegation and hooks is out of scope for R5.
    """
    enable_plugins.ensure_plugins_tier_for_new_home(str(tree["orch"]))
    doc = _doc(tree["orch"])
    assert list(doc) == ["plugins"], f"tier-only write leaked keys: {sorted(doc)}"


def test_existing_keyless_config_is_still_seeded(tree):
    """A home Hermes already created a config for behaves as before."""
    rec = enable_plugins.ensure_plugins_tier_for_new_home(str(tree["worker"]))
    assert rec["tier"] == "backend-engineer"
    assert _enabled(tree["worker"]) == HOOKS_ONLY
    # the home's own content survives
    assert _doc(tree["worker"])["toolsets"] == ["hermes-cli"]


def test_existing_cluster_surface_is_pruned_back_to_the_hooks_tier(tree):
    """A home _migrate_to_21 already widened is pulled back to what it may hold."""
    (tree["orch"] / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": CLUSTER + ["hscc_daemon"]},
    }, sort_keys=False), encoding="utf-8")
    rec = enable_plugins.ensure_plugins_tier_for_new_home(str(tree["orch"]))
    assert sorted(rec["pruned"]) == ["hscc-commands", "sparkrun-hermes"]
    assert _enabled(tree["orch"]) == ["hscc-cluster", "hscc_daemon"]


def test_second_call_writes_nothing(tree):
    enable_plugins.ensure_plugins_tier_for_new_home(str(tree["orch"]))
    text = (tree["orch"] / "config.yaml").read_text(encoding="utf-8")
    stamp = os.stat(tree["orch"] / "config.yaml").st_mtime_ns
    assert enable_plugins.ensure_plugins_tier_for_new_home(str(tree["orch"])) is None
    assert (tree["orch"] / "config.yaml").read_text(encoding="utf-8") == text
    assert os.stat(tree["orch"] / "config.yaml").st_mtime_ns == stamp


def test_missing_home_is_a_no_op_not_a_crash(tmp_path):
    assert enable_plugins.ensure_plugins_tier_for_new_home(
        str(tmp_path / "never-created")) is None


def test_escape_hatch_restores_the_flat_shape(tree, monkeypatch):
    monkeypatch.setenv("HSCC_CLUSTER_PLUGINS_MAIN_ONLY", "0")
    monkeypatch.setattr(enable_plugins, "CLUSTER_PLUGINS_MAIN_ONLY", False)
    enable_plugins.ensure_plugins_tier_for_new_home(str(tree["orch"]))
    assert _enabled(tree["orch"]) == CLUSTER


# ── the CLI the provisioning step calls ──────────────────────────────────────

def test_cli_tier_only_seeds_one_home_and_exits_zero(tree, capsys):
    rc = enable_plugins.main(["--plugins-tier-only", "--home", str(tree["orch"])])
    assert rc == 0
    out = capsys.readouterr().out
    assert "tier=pickolo-orch" in out and "hscc-cluster" in out
    assert _enabled(tree["orch"]) == HOOKS_ONLY
    # exactly one home touched
    assert _doc(tree[MAIN]) is not None and "plugins" not in _doc(tree[MAIN])


def test_cli_tier_only_is_idempotent(tree, capsys):
    enable_plugins.main(["--plugins-tier-only", "--home", str(tree["orch"])])
    capsys.readouterr()
    assert enable_plugins.main(
        ["--plugins-tier-only", "--home", str(tree["orch"])]) == 0
    assert "already tiered" in capsys.readouterr().out


def test_cli_tier_only_refuses_to_run_without_a_home(tree):
    """No --home means the root/default home — a tier-only sweep of the tree is
    exactly what this flag must never become."""
    with pytest.raises(SystemExit) as exc:
        enable_plugins.main(["--plugins-tier-only"])
    assert exc.value.code == 2


def test_cli_tier_only_does_not_run_the_full_wire(tree, monkeypatch):
    """A tier-only call must not reach enable().

    The full wire is what the card forbids here; pinning that it is never entered
    is stronger than pinning the output shape.
    """
    def _boom(*a, **k):                                     # pragma: no cover
        raise AssertionError("the tier-only path must not call enable()")
    monkeypatch.setattr(enable_plugins, "enable", _boom)
    assert enable_plugins.main(
        ["--plugins-tier-only", "--home", str(tree["orch"])]) == 0


def test_cli_default_path_is_unchanged_by_the_flag(tree, monkeypatch):
    """The installer entry still runs the wire: the new branch is opt-in only."""
    called = {}

    def _fake_enable(path, *a, **k):
        called["path"] = path
        return {"plugins": [], "config_missing": True}
    monkeypatch.setattr(enable_plugins, "enable", _fake_enable)
    assert enable_plugins.main(["--home", str(tree["orch"])]) == 0
    assert called["path"].endswith("config.yaml")
