"""D-B: the operator slash verbs register only in <main> and the root home.

Card t_e98c6ad4 / docs/audits/2026-11-23-main-only-surface.md §6 D-B as ruled:
the root home keeps the wedge-path verbs (they run in the gateway, not by a
model) and ``<main>`` gets them; a worker home must not hold
``/cluster-reboot``. The verbs carry no hooks, so a denied home registers
nothing at all.

Sandboxed: a fake ``~/.hermes`` tree in tmp plus ``HSCC_DIR`` at a tmp state dir
with a NON-default stored main name, so the gate is exercised against a name it
did not choose (D-A). Nothing live is read or written.
"""
import importlib.util
import os
import sys

import pytest

import __init__ as plugin

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_main_orch():
    """The resolver by path (the dir is hyphenated, so not importable by name).

    Deliberately a SEPARATE module object from the one the plugin loads: the
    fixture writes state through it and the plugin's own loader reads the same
    ``HSCC_DIR`` document, which is exactly the coupling under test.
    """
    path = os.path.join(_REPO, "hscc-roles", "main_orch.py")
    spec = importlib.util.spec_from_file_location("_gate_test_main_orch", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_UNSET = object()


class Ctx:
    """Records register_command calls; optionally looks like a Hermes ctx."""

    def __init__(self, profile_name=_UNSET):
        self.names = []
        if profile_name is not _UNSET:
            self.profile_name = profile_name

    def register_command(self, **kw):
        self.names.append(kw["name"])


ALL_VERBS = {"cluster", "status", "orch-restart", "cluster-restart",
             "cluster-reboot", "cluster-down", "cluster-docker-prune",
             "cluster-apt-upgrade", "cluster-prune", "heal", "template",
             "workers-up"}


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """A fake root home + two profiles, and a stored NON-default main name."""
    root = tmp_path / "hermes"
    (root / "profiles" / "ops").mkdir(parents=True)
    (root / "profiles" / "backend-engineer").mkdir(parents=True)
    hscc = tmp_path / "hscc"
    hscc.mkdir()
    monkeypatch.setenv("HSCC_DIR", str(hscc))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    _load_main_orch().store_name("ops", registry_path="/nonexistent.yaml")
    return {"root": str(root), "main": str(root / "profiles" / "ops"),
            "worker": str(root / "profiles" / "backend-engineer")}


def _run(monkeypatch, ctx, home):
    monkeypatch.setenv("HERMES_HOME", home)
    ctx = Ctx(profile_name=ctx) if isinstance(ctx, str) else ctx
    plugin.register(ctx)
    return ctx.names


def test_worker_home_gets_no_verbs(monkeypatch, homes):
    assert _run(monkeypatch, "backend-engineer", homes["worker"]) == []


def test_main_home_gets_every_verb(monkeypatch, homes):
    assert set(_run(monkeypatch, "ops", homes["main"])) == ALL_VERBS


def test_root_home_keeps_the_wedge_path(monkeypatch, homes):
    """R3/D-B: the verbs are the operator's wedge path and run in the gateway,
    which lives in the root home — so root keeps them (and is why the gate is
    not "main only")."""
    assert set(_run(monkeypatch, "default", homes["root"])) == ALL_VERBS


def test_stub_ctx_still_registers(monkeypatch, homes):
    """A ctx that is not a Hermes PluginContext (no ``profile_name``) must not
    be stripped — and must not be judged by whatever HERMES_HOME the suite
    exports."""
    monkeypatch.setenv("HERMES_HOME", homes["worker"])
    ctx = Ctx()
    plugin.register(ctx)
    assert set(ctx.names) == ALL_VERBS


def test_gate_is_denied_by_name_alone(monkeypatch, homes):
    """No HERMES_HOME at all: a named non-main profile is already a complete
    answer."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert _run(monkeypatch, "backend-engineer", homes["worker"]) == []


def test_gate_routes_through_the_resolver_not_a_local_name_check():
    """D-A guard: no plugin may decide main-ness itself.

    The gate must reach ``main_orch.plugin_surface_allowed``; a local
    comparison (``profile == "general"``, a ``-orch`` regex, an ``os.environ``
    sniff) would be a second resolver, and the one this card exists to remove.
    """
    src = open(os.path.join(_REPO, "hscc-commands", "__init__.py"),
               encoding="utf-8").read()
    block = src[src.index("def _cluster_surface_allowed"):]
    assert "plugin_surface_allowed" in block
    for needle in ('"general"', "'general'", "-orch", "get_active_profile_name"):
        assert needle not in block, f"second resolver sneaked back in: {needle}"
