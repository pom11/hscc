"""Bootstrap writes the §6 per-home plugins.enabled shape (card t_e98c6ad4).

R5 of the driver ruling is explicit about why this is not cosmetic:
``_migrate_to_21`` (hermes_cli/config_migrations.py:328-347) fills
``plugins.enabled`` with EVERY manifest-bearing dir under ``$HERMES_HOME/plugins/``
for any config that LACKS the key, and the audit measured the key present in
**0 of 52** profile configs while every one of those homes carries 21 plugin dirs.
So an explicit value is what inoculates a home, and a home bootstrap never
writes is one migration away from the whole cluster surface.

Tier (docs/audits/2026-11-23-main-only-surface.md §6, resolved through
``hscc-roles/main_orch`` — never a spelled name):

    root home            [hscc-cluster, hscc-commands, sparkrun-hermes]
    <main>               the same three           (the only HOME that gets tools)
    every other profile  [hscc-cluster]           (hooks only, D-C)

Everything here runs against a fake ``~/.hermes`` tree in tmp and an ``HSCC_DIR``
holding a NON-default stored main name. Nothing live is read or written, and the
tests pin that too (the argument-vs-env case below).
"""
import os

import pytest
import yaml

import enable_plugins

CLUSTER = ["hscc-cluster", "hscc-commands", "sparkrun-hermes"]
MAIN = "ops"


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """root + <main> + a worker + a project orch, each with a config.yaml."""
    root = tmp_path / "hermes"
    hscc = tmp_path / "hscc"
    hscc.mkdir()
    monkeypatch.setenv("HSCC_DIR", str(hscc))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_tier_test_main_orch",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "hscc-roles", "main_orch.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.store_name(MAIN, registry_path="/nonexistent.yaml")

    def home(name, cfg):
        d = root if name is None else root / "profiles" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False),
                                       encoding="utf-8")
        return d / "config.yaml"

    paths = {
        "root": home(None, {"model": {"default": "orchestrator-model"}}),
        MAIN: home(MAIN, {"toolsets": ["hermes-cli"]}),
        "worker": home("backend-engineer", {"toolsets": ["hermes-cli"]}),
        "orch": home("ecofire-orch", {"toolsets": ["hermes-cli"]}),
    }
    monkeypatch.setattr(enable_plugins, "CLUSTER_GUARD_DST",
                        str(tmp_path / "hooks" / "cluster-guard.py"))
    monkeypatch.setattr(enable_plugins, "HOOKS_DIR", str(tmp_path / "hooks"))
    return {"root": root, "paths": paths}


def _enabled(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return (doc.get("plugins") or {}).get("enabled")


# ── the sweep ────────────────────────────────────────────────────────────────

def test_sweep_writes_the_three_tiers(tree):
    changed = enable_plugins.ensure_plugins_tier_everywhere(home=str(tree["root"]))
    assert _enabled(tree["paths"]["root"]) == CLUSTER
    assert _enabled(tree["paths"][MAIN]) == CLUSTER
    assert _enabled(tree["paths"]["worker"]) == ["hscc-cluster"]
    assert _enabled(tree["paths"]["orch"]) == ["hscc-cluster"]
    assert set(changed) == {str(p) for p in tree["paths"].values()}
    assert changed[str(tree["paths"]["root"])]["tier"] == "root"
    assert changed[str(tree["paths"][MAIN])]["tier"] == MAIN
    assert changed[str(tree["paths"]["worker"])]["tier"] == "backend-engineer"


def test_sweep_is_idempotent_and_backs_up_nothing(tree):
    enable_plugins.ensure_plugins_tier_everywhere(home=str(tree["root"]))
    before = {str(p): p.read_text(encoding="utf-8") for p in tree["paths"].values()}
    mtimes = {str(p): os.stat(p).st_mtime_ns for p in tree["paths"].values()}
    assert enable_plugins.ensure_plugins_tier_everywhere(home=str(tree["root"])) == {}
    for path, text in before.items():
        assert os.stat(path).st_mtime_ns == mtimes[path], (
            "a re-run rewrote a config it had nothing to change")
        assert text == open(path).read()


def test_sweep_prunes_a_migrated_home_but_keeps_foreign_entries(tree):
    """The exact state _migrate_to_21 writes: everything under plugins/ enabled.

    Pruning is limited to HSCC's OWN cluster names — a third-party entry an
    operator enabled survives, as does every other key under ``plugins:``.
    """
    worker = tree["paths"]["worker"]
    worker.write_text(yaml.safe_dump({
        "plugins": {"enabled": CLUSTER + ["video_gen/openai_compat"],
                    "entries": {"video_gen/openai_compat": {"llm": {}}}},
    }, sort_keys=False), encoding="utf-8")

    changed = enable_plugins.ensure_plugins_tier_everywhere(home=str(tree["root"]))
    doc = yaml.safe_load(worker.read_text(encoding="utf-8"))
    assert doc["plugins"]["enabled"] == ["hscc-cluster", "video_gen/openai_compat"]
    assert doc["plugins"]["entries"] == {"video_gen/openai_compat": {"llm": {}}}
    rec = changed[str(worker)]
    assert rec["pruned"] == ["hscc-commands", "sparkrun-hermes"]
    assert rec["added"] == []          # hscc-cluster was already there


def test_sweep_never_creates_a_missing_profile_config(tree):
    fresh = tree["root"] / "profiles" / "brand-new-role"
    fresh.mkdir(parents=True)
    enable_plugins.ensure_plugins_tier_everywhere(home=str(tree["root"]))
    assert not (fresh / "config.yaml").exists(), (
        "Hermes owns fresh profile creation; bootstrap must not scaffold it")


def test_root_comes_from_the_argument_never_from_env(tree, monkeypatch, tmp_path):
    """The ambient-HERMES_HOME class of bug (t_267f9d88).

    A dispatched worker carries ``HERMES_HOME=<root>/profiles/<name>``. Deriving
    the sweep root from the environment would let a test (or a worker running
    bootstrap) silently rewrite the LIVE tree, so the argument is authoritative.
    """
    other = tmp_path / "live"
    (other / "profiles" / "victim").mkdir(parents=True)
    victim = other / "profiles" / "victim" / "config.yaml"
    victim.write_text("toolsets: [hermes-cli]\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(other / "profiles" / "victim"))

    enable_plugins.ensure_plugins_tier_everywhere(home=str(tree["root"]))

    assert yaml.safe_load(victim.read_text(encoding="utf-8")) == {"toolsets": ["hermes-cli"]}
    assert _enabled(tree["paths"]["worker"]) == ["hscc-cluster"]


# ── enable()'s own wiring ────────────────────────────────────────────────────

def test_enable_at_the_root_seeds_the_whole_tree(tree, monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    res = enable_plugins.enable(str(tree["paths"]["root"]))
    assert res["plugins"] == CLUSTER
    assert str(tree["paths"][MAIN]) in res["plugins_homes"]
    assert res["plugins_homes"][str(tree["paths"][MAIN])]["tier"] == MAIN
    assert _enabled(tree["paths"][MAIN]) == CLUSTER
    assert _enabled(tree["paths"]["worker"]) == ["hscc-cluster"]
    assert _enabled(tree["paths"]["orch"]) == ["hscc-cluster"]


def test_enable_with_home_touches_only_that_profile(tree, monkeypatch):
    """``--home`` means 'wire this one home deliberately' (the flag's own help).

    The sweep must not run from a profile-scoped call, or an operator wiring one
    worker would rewrite every other home they never asked about.
    """
    monkeypatch.delenv("HERMES_HOME", raising=False)
    res = enable_plugins.enable(str(tree["paths"]["worker"]))
    assert res["plugins"] == ["hscc-cluster"]
    assert res["plugins_homes"] == {}
    assert _enabled(tree["paths"]["worker"]) == ["hscc-cluster"]
    assert (yaml.safe_load(tree["paths"][MAIN].read_text(encoding="utf-8"))
            .get("plugins") is None)


def test_enable_reports_pruned_names(tree, monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    tree["paths"]["worker"].write_text(
        yaml.safe_dump({"plugins": {"enabled": CLUSTER}}, sort_keys=False),
        encoding="utf-8")
    res = enable_plugins.enable(str(tree["paths"]["worker"]))
    assert res["plugins_pruned"] == ["hscc-commands", "sparkrun-hermes"]
    assert _enabled(tree["paths"]["worker"]) == ["hscc-cluster"]


# ── the single-home path (R5's "seed NEW profiles the same way") ─────────────

def _home(tree, key):
    return os.path.dirname(str(tree["paths"][key]))


def test_single_home_writer_touches_only_its_own_home(tree):
    """A provisioning path that creates ONE profile must not sweep 52.

    This is the function ``hscc-roles/generator.py`` calls from
    ``generate_profile`` — the only seeding a profile created after the last
    bootstrap ever gets — so it has to answer the same tiered question as the
    sweep while writing exactly one file.
    """
    rec = enable_plugins.ensure_plugins_tier_for_home(_home(tree, "worker"))
    assert rec["added"] == ["hscc-cluster"]
    assert _enabled(tree["paths"]["worker"]) == ["hscc-cluster"]
    for key in ("root", MAIN, "orch"):
        assert (yaml.safe_load(tree["paths"][key].read_text(encoding="utf-8"))
                .get("plugins") is None), f"{key} was rewritten by a 1-home call"


def test_single_home_writer_uses_the_same_tier_as_the_sweep(tree):
    main_rec = enable_plugins.ensure_plugins_tier_for_home(_home(tree, MAIN))
    assert main_rec is not None and main_rec["tier"] == MAIN
    assert _enabled(tree["paths"][MAIN]) == CLUSTER
    orch_rec = enable_plugins.ensure_plugins_tier_for_home(_home(tree, "orch"))
    assert orch_rec is not None and orch_rec["tier"] == "ecofire-orch"
    assert _enabled(tree["paths"]["orch"]) == ["hscc-cluster"]


def test_single_home_writer_is_a_noop_when_already_tiered(tree):
    enable_plugins.ensure_plugins_tier_for_home(_home(tree, "worker"))
    text = tree["paths"]["worker"].read_text(encoding="utf-8")
    backups = sorted(tree["paths"]["worker"].parent.glob("config.yaml.bak-*"))
    assert backups, "the seeding write is supposed to back up first"
    assert enable_plugins.ensure_plugins_tier_for_home(_home(tree, "worker")) is None
    assert tree["paths"]["worker"].read_text(encoding="utf-8") == text
    assert sorted(tree["paths"]["worker"].parent.glob("config.yaml.bak-*")) == backups


def test_single_home_writer_never_creates_a_config(tree):
    fresh = tree["root"] / "profiles" / "not-yet-run"
    fresh.mkdir(parents=True)
    assert enable_plugins.ensure_plugins_tier_for_home(fresh) is None
    assert not (fresh / "config.yaml").exists()


# ── fail-open, and the escape hatch ─────────────────────────────────────────

def test_missing_resolver_falls_back_to_the_flat_shape(tree, monkeypatch):
    monkeypatch.setattr(enable_plugins, "_load_main_orch", lambda: None)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    res = enable_plugins.enable(str(tree["paths"]["worker"]))
    assert res["plugins"] == CLUSTER          # pre-existing behaviour, not a guess
    assert res["plugins_pruned"] == []        # and never a half-applied tier
    assert _enabled(tree["paths"]["worker"]) == CLUSTER


def test_escape_hatch_restores_the_flat_shape(tree, monkeypatch):
    """``HSCC_CLUSTER_PLUGINS_MAIN_ONLY=0`` -> exactly the pre-card behaviour:
    the three names are added everywhere and NOTHING is pruned."""
    monkeypatch.setattr(enable_plugins, "CLUSTER_PLUGINS_MAIN_ONLY", False)
    tree["paths"]["worker"].write_text(
        yaml.safe_dump({"plugins": {"enabled": CLUSTER}}, sort_keys=False),
        encoding="utf-8")
    changed = enable_plugins.ensure_plugins_tier_everywhere(home=str(tree["root"]))
    assert _enabled(tree["paths"]["worker"]) == CLUSTER
    assert str(tree["paths"]["worker"]) not in changed      # nothing to do there
    assert all(rec["pruned"] == [] for rec in changed.values())
    assert _enabled(tree["paths"]["orch"]) == CLUSTER


def test_broker_plugins_config_is_left_alone(tree, monkeypatch):
    """A malformed ``plugins`` value is an operator statement, not a crash."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    tree["paths"]["worker"].write_text("plugins: weird-string\n", encoding="utf-8")
    res = enable_plugins.enable(str(tree["paths"]["worker"]))
    assert res["plugins"] == []
    assert yaml.safe_load(tree["paths"]["worker"].read_text(
        encoding="utf-8"))["plugins"] == "weird-string"


# ── D-A: the writer never spells a profile name ──────────────────────────────

def test_new_writer_code_names_no_profile_literal():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "enable_plugins.py"), encoding="utf-8").read()
    block = src[src.index("# ── the main-only cluster surface"):]
    block = block[:block.index("def _ensure_kanban_routing")]
    assert '"general"' not in block and "'general'" not in block
    assert "main_orch" in block or "_load_main_orch" in block
