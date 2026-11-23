"""D-B: hscc-cluster registers tools by home, hooks in every home (t_e98c6ad4).

The audit's whole point (docs/audits/2026-11-23-main-only-surface.md §3.1, §7
G-1) is that Hermes cannot split hooks from tools: ``register(ctx)`` is
all-or-nothing at load. The ruling therefore puts the split inside this plugin —
tools only in ``<main>`` and the operator/gateway root home, hooks everywhere —
and the pair of facts below is what keeps that honest:

  * a non-main home registers NO tools, and
  * the same home still registers ALL SIX hooks (audit §3.2), because those six
    are the worker/dispatcher lanes: the ``[budget]`` note, the per-profile tool
    ledger and the kanban completion/blocked observers.

``cluster_tools_allowed`` is a thin reach to ``main_orch.plugin_surface_allowed``
(the resolver D-A requires); this file pins both the resolver's verdicts and the
source shape that keeps hooks outside the gate.
"""
import importlib.util
import os

import pytest

import orch_message

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_INIT = os.path.join(_REPO, "hscc-cluster", "__init__.py")

HOOK_EVENTS = {"kanban_task_claimed", "kanban_task_blocked", "kanban_task_completed",
               "pre_tool_call", "post_api_request", "transform_tool_result"}


_UNSET = object()


class Ctx:
    def __init__(self, profile_name=_UNSET):
        self.tools = []
        self.hooks = []
        if profile_name is not _UNSET:
            self.profile_name = profile_name

    def register_tool(self, **kw):
        self.tools.append(kw["name"])

    def register_hook(self, event, fn):
        self.hooks.append(event)


@pytest.fixture
def homes(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    (root / "profiles" / "ops").mkdir(parents=True)
    (root / "profiles" / "backend-engineer").mkdir(parents=True)
    hscc = tmp_path / "hscc"
    hscc.mkdir()
    monkeypatch.setenv("HSCC_DIR", str(hscc))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    path = os.path.join(_REPO, "hscc-roles", "main_orch.py")
    spec = importlib.util.spec_from_file_location("_gate_test_main_orch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.store_name("ops", registry_path="/nonexistent.yaml")
    return {"root": str(root), "main": str(root / "profiles" / "ops"),
            "worker": str(root / "profiles" / "backend-engineer")}


def _allowed(monkeypatch, profile, home):
    monkeypatch.setenv("HERMES_HOME", home)
    return orch_message.cluster_tools_allowed(Ctx(profile_name=profile))


def test_worker_home_denied_main_and_root_allowed(monkeypatch, homes):
    assert _allowed(monkeypatch, "backend-engineer", homes["worker"]) is False
    assert _allowed(monkeypatch, "ops", homes["main"]) is True
    assert _allowed(monkeypatch, "default", homes["root"]) is True


def test_stub_ctx_is_allowed(monkeypatch, homes):
    """Not a Hermes ctx -> fail open, and NOT judged by the ambient home."""
    monkeypatch.setenv("HERMES_HOME", homes["worker"])
    assert orch_message.cluster_tools_allowed(Ctx()) is True


def test_missing_resolver_fails_open(monkeypatch, homes):
    """No hscc-roles on the deployed tree is no gate — never a silent strip."""
    monkeypatch.setenv("HERMES_HOME", homes["worker"])
    monkeypatch.setattr(orch_message, "_load_main_orch", lambda: None)
    assert orch_message.cluster_tools_allowed(Ctx(profile_name="backend-engineer")) is True


def test_throwing_resolver_fails_open(monkeypatch, homes):
    monkeypatch.setenv("HERMES_HOME", homes["worker"])
    real = orch_message._load_main_orch

    class _Boom:
        def plugin_surface_allowed(self, ctx, **kw):
            raise RuntimeError("resolver unusable")

    monkeypatch.setattr(orch_message, "_load_main_orch", lambda: _Boom())
    try:
        assert orch_message.cluster_tools_allowed(
            Ctx(profile_name="backend-engineer")) is True
    finally:
        assert real is not None


# ── the source shape: tools inside the gate, hooks outside it ────────────────

def test_tool_loop_is_inside_the_gate_and_hooks_are_outside():
    """The one shape the ruling allows, pinned on the source.

    Nesting the hooks inside the gate would silently kill the budget watch and
    the kanban observers in every worker home — the failure D-C forbids — and
    a text pin is how this suite already guards the register() loop
    (test_template_tools.py, test_orch_message.py).
    """
    src = open(_INIT, encoding="utf-8").read()
    body = src[src.index("def register(ctx)"):]
    lines = body.splitlines()
    gate = next(i for i, ln in enumerate(lines)
                if ln.startswith("    if orch_message.cluster_tools_allowed(ctx):"))
    # The gated body is the tool loop, indented under the gate.
    tool = next(i for i, ln in enumerate(lines) if "ctx.register_tool(" in ln)
    assert tool > gate
    assert lines[tool].startswith("            "), lines[tool]
    # Every hook lane is a SIBLING of the gate (4-space ``if``), never nested in
    # it, so a denied home still gets all six hooks.
    hook_lanes = [ln for ln in lines
                  if ln.strip().startswith('if hasattr(ctx, "register_hook"):')]
    assert len(hook_lanes) >= 5, hook_lanes          # 5 guarded lanes, 6 hooks
    for ln in hook_lanes:
        assert ln.startswith("    if "), repr(ln)
    # And the six events are all registered by register().
    for event in HOOK_EVENTS:
        assert f'ctx.register_hook("{event}"' in body, event
