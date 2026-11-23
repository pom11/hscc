"""D-B: ``sparkrun_exec`` registers only in <main> and the root home (t_e98c6ad4).

The audit left the passthrough's reach as LEFT-2 ("the SOUL claims
orchestrator-only; nothing enforces it"). This card closes the enforcement half:
the tool is cluster authority through a raw CLI passthrough, so a worker or a
per-project orchestrator home must not get it, while ``<main>`` and the
operator/gateway root home do.

Sandboxed exactly as the sibling gate test does (fake hermes tree + ``HSCC_DIR``
holding a NON-default main name). Nothing live is read, and the suite's own
sparkrun fence still guards any exec.
"""
import importlib.util
import os

import pytest

import __init__ as plugin

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_main_orch():
    path = os.path.join(_REPO, "hscc-roles", "main_orch.py")
    spec = importlib.util.spec_from_file_location("_gate_test_main_orch", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_UNSET = object()


class Ctx:
    def __init__(self, profile_name=_UNSET):
        self.tools = []
        if profile_name is not _UNSET:
            self.profile_name = profile_name

    def register_tool(self, **kw):
        self.tools.append(kw["name"])


@pytest.fixture
def homes(tmp_path, monkeypatch):
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


def _run(monkeypatch, profile, home):
    monkeypatch.setenv("HERMES_HOME", home)
    ctx = Ctx(profile_name=profile)
    plugin.register(ctx)
    return ctx.tools


def test_worker_home_gets_no_passthrough(monkeypatch, homes):
    assert _run(monkeypatch, "backend-engineer", homes["worker"]) == []


def test_project_orch_home_gets_no_passthrough(monkeypatch, homes):
    """LEFT-2's ruling: the passthrough follows the main-only rule, so a
    per-project orchestrator does not hold it either."""
    orch = os.path.join(os.path.dirname(homes["worker"]), "ecofire-orch")
    os.makedirs(orch, exist_ok=True)
    assert _run(monkeypatch, "ecofire-orch", orch) == []


def test_main_and_root_homes_get_it(monkeypatch, homes):
    assert _run(monkeypatch, "ops", homes["main"]) == ["sparkrun_exec"]
    assert _run(monkeypatch, "default", homes["root"]) == ["sparkrun_exec"]


def test_stub_ctx_still_registers(monkeypatch, homes):
    monkeypatch.setenv("HERMES_HOME", homes["worker"])
    ctx = Ctx()
    plugin.register(ctx)
    assert ctx.tools == ["sparkrun_exec"]


def test_gate_routes_through_the_resolver_not_a_local_name_check():
    src = open(os.path.join(_REPO, "sparkrun-hermes", "__init__.py"),
               encoding="utf-8").read()
    block = src[src.index("def _cluster_surface_allowed"):]
    assert "plugin_surface_allowed" in block
    for needle in ('"general"', "'general'", "-orch", "get_active_profile_name"):
        assert needle not in block, f"second resolver sneaked back in: {needle}"
