"""The main-only cluster surface resolver (card t_e98c6ad4).

SOT: ``docs/audits/2026-11-23-main-only-surface.md`` §6 (D-A, D-B, D-C) as
ruled by the driver review (``~/fleet-check/verdict-main-only.txt``), with the
card's own tightening of D-B:

  * **D-A** — "the main profile" is resolved through ``main_orch``, never
    hard-coded. A test that only ever uses the default name proves nothing, so
    every assertion below stores a NON-default name first.
  * **D-B / R3** — TWO surfaces, two answers. The cluster TOOL catalog
    (``hscc-cluster``'s tools, ``sparkrun_exec``) registers only where the home
    resolves to ``<main>``: "NO agent model in the root home may get the cluster
    tool catalog unless QA proves the gateway slash path needs it". The operator
    slash verbs DO stay at the root, because they are the wedge path and run in
    the gateway rather than by a model. Hooks register everywhere.
  * **D-C** — every OTHER home still loads ``hscc-cluster`` for its hooks.
    ``hscc-commands`` / ``sparkrun-hermes`` register no hooks, so they must not
    appear in the hooks-only tier.

The CONFIG tier is deliberately the audit §6 shape verbatim (root and ``<main>``
carry all three names); what a home may then *register* is the separate question
above. A test below pins that split, because collapsing the two is how a future
edit would either strip the operator's verbs or hand the root a tool catalog.

Nothing here touches ``~/.hscc`` or a real Hermes home: the resolver reads
``HSCC_DIR`` and an explicit ``home``/``env``, both sandboxed per test.
"""
import os

import pytest

import main_orch


@pytest.fixture
def hscc_dir(tmp_path, monkeypatch):
    root = tmp_path / "hscc"
    root.mkdir()
    monkeypatch.setenv("HSCC_DIR", str(root))
    return root


@pytest.fixture
def hermes_root(tmp_path):
    """A fake ``~/.hermes`` with one worker profile home inside it."""
    root = tmp_path / "hermes"
    (root / "profiles" / "backend-engineer").mkdir(parents=True)
    return root


def _env(home, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(home))
    return {"HERMES_HOME": str(home)}


# ── D-C: the hooks-only tier is exactly hscc-cluster ─────────────────────────

def test_hooks_only_tier_is_hscc_cluster_alone():
    # The 6 hooks that keep the worker/dispatcher lanes alive all live in
    # hscc-cluster (audit §3.2). Widening this tier would silently re-enable
    # slash commands + the sparkrun passthrough in every worker home.
    assert main_orch.HOOKS_ONLY_PLUGINS == ("hscc-cluster",)
    assert set(main_orch.HOOKS_ONLY_PLUGINS) < set(main_orch.CLUSTER_PLUGINS)


def test_cluster_tier_names_the_three_plugins():
    assert main_orch.CLUSTER_PLUGINS == ("hscc-cluster", "hscc-commands",
                                         "sparkrun-hermes")


# ── D-A: the main name is resolved, never spelled ────────────────────────────

def test_main_home_gets_the_full_tier_under_a_custom_name(hscc_dir, hermes_root,
                                                          monkeypatch):
    main_orch.store_name("ops", registry_path="/nonexistent.yaml")
    home = hermes_root / "profiles" / "ops"
    home.mkdir(parents=True)
    assert main_orch.plugins_for_home(profile="ops", home=home) == main_orch.CLUSTER_PLUGINS
    assert main_orch.cluster_tools_allowed(profile="ops", home=home) is True
    assert main_orch.operator_commands_allowed(profile="ops", home=home) is True


def test_the_default_name_is_not_special_once_another_name_is_stored(hscc_dir,
                                                                     hermes_root,
                                                                     monkeypatch):
    """The anti-hard-code guard.

    If anything in the resolver compared against the literal default name, the
    profile with that name would still look like the orchestrator after the
    operator renamed the main profile. It must fall to the hooks-only tier.
    """
    main_orch.store_name("ops", registry_path="/nonexistent.yaml")
    stale = hermes_root / "profiles" / main_orch.DEFAULT_NAME
    stale.mkdir(parents=True)
    assert main_orch.plugins_for_home(profile=main_orch.DEFAULT_NAME,
                                      home=stale) == main_orch.HOOKS_ONLY_PLUGINS
    assert main_orch.cluster_tools_allowed(profile=main_orch.DEFAULT_NAME,
                                           home=stale) is False
    assert main_orch.operator_commands_allowed(profile=main_orch.DEFAULT_NAME,
                                               home=stale) is False


def test_main_home_gets_full_tier_before_anything_is_stored(hscc_dir, hermes_root):
    """No ``main-orchestrator.json`` yet (fresh install) -> the resolver's own
    default names the home, so a first bootstrap still seeds it."""
    home = hermes_root / "profiles" / main_orch.main_profile()
    home.mkdir(parents=True)
    assert main_orch.plugins_for_home(profile=main_orch.main_profile(),
                                      home=home) == main_orch.CLUSTER_PLUGINS


# ── worker / project-orch homes ──────────────────────────────────────────────

def test_worker_home_is_hooks_only(hscc_dir, hermes_root, monkeypatch):
    main_orch.store_name("ops", registry_path="/nonexistent.yaml")
    home = hermes_root / "profiles" / "backend-engineer"
    assert main_orch.plugins_for_home(profile="backend-engineer",
                                      home=home) == main_orch.HOOKS_ONLY_PLUGINS
    assert main_orch.cluster_tools_allowed(profile="backend-engineer",
                                           home=home) is False
    assert main_orch.operator_commands_allowed(profile="backend-engineer",
                                               home=home) is False


def test_project_orch_home_is_hooks_only(hscc_dir, hermes_root):
    """§6 puts ``<worker-or-orch>`` in one tier: a per-project orchestrator is
    not the main orchestrator."""
    main_orch.store_name("ops", registry_path="/nonexistent.yaml")
    home = hermes_root / "profiles" / "ecofire-orch"
    home.mkdir(parents=True)
    assert main_orch.plugins_for_home(profile="ecofire-orch",
                                      home=home) == main_orch.HOOKS_ONLY_PLUGINS
    assert main_orch.cluster_tools_allowed(profile="ecofire-orch",
                                           home=home) is False
    assert main_orch.operator_commands_allowed(profile="ecofire-orch",
                                               home=home) is False


# ── the root home (operator CLI + gateway + dispatcher) ──────────────────────

def test_root_home_keeps_config_and_verbs_but_no_tools(hscc_dir, hermes_root,
                                                       monkeypatch):
    """The two surfaces part company exactly here, and that is the ruling.

    R3/D-B tightened: the root home is where the gateway runs and where an
    operator types ``/cluster-...`` — so it keeps the plugins in its config
    (§6's shape, unchanged) and the verbs at registration. It is NOT the main
    orchestrator, so a model chatting in it gets no cluster tool catalog.
    """
    main_orch.store_name("ops", registry_path="/nonexistent.yaml")
    env = _env(hermes_root, monkeypatch)
    assert main_orch.plugins_for_home(profile=main_orch.ROOT_PROFILE_NAME,
                                      home=hermes_root, env=env) == main_orch.CLUSTER_PLUGINS
    assert main_orch.operator_commands_allowed(profile=main_orch.ROOT_PROFILE_NAME,
                                               home=hermes_root, env=env) is True
    assert main_orch.cluster_tools_allowed(profile=main_orch.ROOT_PROFILE_NAME,
                                           home=hermes_root, env=env) is False


def test_root_home_recognised_by_path_alone(hscc_dir, hermes_root):
    assert main_orch.is_root_home(hermes_root) is True
    assert main_orch.is_root_home(hermes_root / "profiles" / "worker") is False
    assert main_orch.hermes_root(hermes_root / "profiles" / "worker") == str(hermes_root)


def test_profile_scoped_home_is_not_root(hscc_dir, hermes_root):
    home = hermes_root / "profiles" / "backend-engineer"
    assert main_orch.cluster_tools_allowed(profile="", home=home) is False
    assert main_orch.operator_commands_allowed(profile="", home=home) is False
    assert main_orch.plugins_for_home(profile="", home=home) == main_orch.HOOKS_ONLY_PLUGINS


def test_writer_never_fails_open_on_a_named_home(hscc_dir, hermes_root, monkeypatch):
    """The one asymmetry between the two resolvers, pinned.

    ``cluster_tools_allowed`` fails open for an unresolvable caller (stripping
    is the dangerous direction); ``plugins_for_home`` is the config WRITER, so
    an answer that names a home must be honoured even when the caller forgot the
    profile name — a worker home that came back with the full tier would widen
    the surface instead of narrowing it.
    """
    main_orch.store_name("ops", registry_path="/nonexistent.yaml")
    home = hermes_root / "profiles" / "backend-engineer"
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert main_orch.plugins_for_home() == main_orch.HOOKS_ONLY_PLUGINS
    assert main_orch.plugins_for_home(home=home) == main_orch.HOOKS_ONLY_PLUGINS
    # Nothing resolvable at all -> the resolver's own default shape.
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert main_orch.plugins_for_home(home=None) == main_orch.CLUSTER_PLUGINS


# ── fail-open: stripping is the dangerous direction ──────────────────────────

def test_unresolvable_caller_is_allowed(hscc_dir, monkeypatch):
    """A stub ctx / catalog pass: no name, no HERMES_HOME -> do NOT strip."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert main_orch.cluster_tools_allowed() is True
    assert main_orch.cluster_tools_allowed(env={}) is True
    assert main_orch.operator_commands_allowed(env={}) is True


def test_named_non_main_profile_without_a_home_is_denied(hscc_dir, monkeypatch):
    """A worker's profile name is already a complete answer; HERMES_HOME must
    not be able to talk us into widening it."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert main_orch.cluster_tools_allowed(profile="backend-engineer") is False
    assert main_orch.operator_commands_allowed(profile="backend-engineer") is False


def test_a_directory_alone_resolves_the_profile_name(hscc_dir, hermes_root):
    """A tree-sweeping writer knows only the path.

    Under ``<root>/profiles/<name>`` the basename IS the profile name, so
    ``<main>``'s home must resolve to the full tier without the caller having to
    spell the name it is looking at — otherwise bootstrap would write the
    hooks-only tier into the orchestrator's own config.
    """
    main_orch.store_name("ops", registry_path="/nonexistent.yaml")
    main_home = hermes_root / "profiles" / "ops"
    main_home.mkdir(parents=True)
    assert main_orch.plugins_for_home(home=main_home) == main_orch.CLUSTER_PLUGINS
    assert main_orch.plugins_for_home(
        home=hermes_root / "profiles" / "backend-engineer") == main_orch.HOOKS_ONLY_PLUGINS
    assert main_orch.plugins_for_home(home=hermes_root) == main_orch.CLUSTER_PLUGINS


def test_answer_follows_the_current_stored_name(hscc_dir, hermes_root):
    """A rename is the operator's statement; the resolver re-reads the state
    document per call, so no process restart is needed to honour it."""
    home = hermes_root / "profiles" / "ops"
    home.mkdir(parents=True)
    assert main_orch.cluster_tools_allowed(profile="ops", home=home) is False
    main_orch.store_name("ops", registry_path="/nonexistent.yaml")
    assert main_orch.cluster_tools_allowed(profile="ops", home=home) is True


# ── the register()-side wrappers ─────────────────────────────────────────────

class _Ctx:
    """Minimal stand-in for Hermes' PluginContext (only ``profile_name`` is read)."""

    def __init__(self, profile_name):
        self.profile_name = profile_name


def test_stub_ctx_is_allowed_even_under_a_profile_home(hscc_dir, hermes_root,
                                                      monkeypatch):
    """A ctx with no ``profile_name`` is not a Hermes context.

    The suites export ``HERMES_HOME`` at a live profile, so if the wrapper read
    ambient env for a stub, every existing ``register(ctx)`` test would start
    depending on which home the suite happens to run under.
    """
    home = hermes_root / "profiles" / "backend-engineer"
    monkeypatch.setenv("HERMES_HOME", str(home))
    for wrapper in (main_orch.plugin_tools_allowed,
                    main_orch.plugin_commands_allowed):
        assert wrapper(object(), env=os.environ) is True
        assert wrapper(_Ctx(None), env={"HERMES_HOME": str(home)}) is True
        assert wrapper(_Ctx("   "), env={"HERMES_HOME": str(home)}) is True


def test_wrapper_denies_tools_in_a_worker_home(hscc_dir, hermes_root, monkeypatch):
    main_orch.store_name("ops", registry_path="/nonexistent.yaml")
    home = hermes_root / "profiles" / "backend-engineer"
    env = {"HERMES_HOME": str(home)}
    assert main_orch.plugin_tools_allowed(_Ctx("backend-engineer"), env=env) is False
    assert main_orch.plugin_commands_allowed(_Ctx("backend-engineer"), env=env) is False


def test_wrapper_allows_main_home_for_both_surfaces(hscc_dir, hermes_root):
    main_orch.store_name("ops", registry_path="/nonexistent.yaml")
    env = {"HERMES_HOME": str(hermes_root / "profiles" / "ops")}
    assert main_orch.plugin_tools_allowed(_Ctx("ops"), env=env) is True
    assert main_orch.plugin_commands_allowed(_Ctx("ops"), env=env) is True


def test_wrapper_splits_the_two_surfaces_at_the_root_home(hscc_dir, hermes_root):
    """The card's tightened R3, at the wrapper the plugins actually call."""
    main_orch.store_name("ops", registry_path="/nonexistent.yaml")
    env = {"HERMES_HOME": str(hermes_root)}
    ctx = _Ctx(main_orch.ROOT_PROFILE_NAME)
    assert main_orch.plugin_commands_allowed(ctx, env=env) is True
    assert main_orch.plugin_tools_allowed(ctx, env=env) is False


# ── D-A grep guard: the new block must not spell a profile name ──────────────

def test_no_literal_profile_name_in_the_new_block():
    src = open(main_orch.__file__, encoding="utf-8").read()
    block = src[src.index("# The main-only cluster surface"):]
    block = block[:block.index("# Validation")]
    assert main_orch.DEFAULT_NAME not in block, (
        "the tier resolver must resolve the main name, never compare against "
        "the default literal (D-A)")
    assert main_orch.LEGACY_GENERAL_PROFILE not in block
