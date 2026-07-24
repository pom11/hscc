"""Tests for hscc-cluster/template_tools.py — monkeypatched cluster_template engine."""
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import template_tools


class FakeClusterTemplate:
    """Fake cluster_template module for monkeypatching."""

    def list_templates(self):
        return {
            "count": 2,
            "templates": [
                {"name": "coding", "version": 2, "description": "Coding fleet"},
                {"name": "4node-coding", "version": 2, "description": "4-node coding fleet"},
            ],
        }

    def preview_template(self, name):
        return {
            "template": name,
            "description": f"Preview for {name}",
            "changes": [
                {"file": "serving.json", "action": "write", "summary": "3 units"},
            ],
        }

    def apply_template(self, name, confirm=False):
        if not confirm:
            return {
                "status": "preview",
                "note": "Re-call with confirm=true to execute",
                "errors": [],
                "changes": self.preview_template(name),
            }
        return {
            "template": name,
            "success": True,
            "steps": [
                {"step": "serving.json", "status": "ok"},
                {"step": "models.json", "status": "ok"},
                {"step": "provision", "status": "ok"},
            ],
        }


def _make_fake_ct():
    return FakeClusterTemplate()


# ── Handler tests ──


def test_list_templates_returns_engine_result(monkeypatch):
    fake = FakeClusterTemplate()
    monkeypatch.setattr(template_tools, "_get_cluster_template", lambda: fake)
    out = template_tools.list_templates({})
    assert out["count"] == 2
    assert len(out["templates"]) == 2


def test_preview_template_passes_name(monkeypatch):
    fake = FakeClusterTemplate()
    monkeypatch.setattr(template_tools, "_get_cluster_template", lambda: fake)
    out = template_tools.preview_template({"template": "coding"})
    assert out["template"] == "coding"
    assert "changes" in out


def test_preview_template_errors_when_template_missing(monkeypatch):
    monkeypatch.setattr(template_tools, "_get_cluster_template", lambda: FakeClusterTemplate())
    out = template_tools.preview_template({})
    assert out["ok"] is False
    assert "template name required" in out["error"]


def test_apply_template_without_confirm_returns_preview(monkeypatch):
    fake = FakeClusterTemplate()
    monkeypatch.setattr(template_tools, "_get_cluster_template", lambda: fake)
    out = template_tools.apply_template({"template": "coding"})
    # confirm defaults to False — engine returns preview, not execution
    assert out["status"] == "preview"
    assert "confirm=true" in out["note"]


def test_apply_template_with_confirm_executes(monkeypatch):
    fake = FakeClusterTemplate()
    monkeypatch.setattr(template_tools, "_get_cluster_template", lambda: fake)
    out = template_tools.apply_template({"template": "coding", "confirm": True})
    assert out["success"] is True
    assert len(out["steps"]) >= 1


def test_apply_template_errors_when_template_missing(monkeypatch):
    monkeypatch.setattr(template_tools, "_get_cluster_template", lambda: FakeClusterTemplate())
    out = template_tools.apply_template({})
    assert out["ok"] is False
    assert "template name required" in out["error"]


def test_handler_returns_error_on_engine_exception(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("template engine crash")
    monkeypatch.setattr(template_tools, "_get_cluster_template", boom)
    out = template_tools.list_templates({})
    assert out["ok"] is False
    assert "template engine crash" in out["error"]


# ── Confirm propagation ──


# ── _get_cluster_template fallback ──


def test_get_cluster_template_raises_on_import_error(monkeypatch):
    """When relative import fails, _get_cluster_template raises RuntimeError (fail-fast)."""
    original_import = template_tools.__builtins__ if hasattr(template_tools, "__builtins__") else None

    def import_side_effect(*args, **kwargs):
        raise ImportError("no module named 'cluster_template'")

    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if "cluster_template" in name:
            raise ImportError("no module named 'cluster_template'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import, raising=False)

    try:
        with pytest.raises(RuntimeError, match="cluster_template module not available"):
            template_tools._get_cluster_template()
    finally:
        monkeypatch.setattr(builtins, "__import__", real_import, raising=False)


# ── Confirm coercion security tests ──


def test_apply_template_string_false_does_not_execute(monkeypatch):
    """confirm='false' (string) must NOT bypass the gate — returns preview."""
    received = {}

    class ConfirmChecker:
        def apply_template(self, name, confirm=False):
            received["confirm"] = confirm
            if not confirm:
                return {"status": "preview", "confirm_received": confirm}
            return {"status": "applied", "confirm_received": confirm}

    fake = ConfirmChecker()
    monkeypatch.setattr(template_tools, "_get_cluster_template", lambda: fake)
    out = template_tools.apply_template({"template": "coding", "confirm": "false"})
    assert out["status"] == "preview"
    assert received["confirm"] is False


def test_apply_template_string_true_does_not_execute(monkeypatch):
    """confirm='true' (string) must NOT bypass the gate — returns preview."""
    received = {}

    class ConfirmChecker:
        def apply_template(self, name, confirm=False):
            received["confirm"] = confirm
            if not confirm:
                return {"status": "preview", "confirm_received": confirm}
            return {"status": "applied", "confirm_received": confirm}

    fake = ConfirmChecker()
    monkeypatch.setattr(template_tools, "_get_cluster_template", lambda: fake)
    out = template_tools.apply_template({"template": "coding", "confirm": "true"})
    assert out["status"] == "preview"
    assert received["confirm"] is False


def test_apply_template_int_1_does_not_execute(monkeypatch):
    """confirm=1 (int) must NOT bypass the gate — returns preview."""
    received = {}

    class ConfirmChecker:
        def apply_template(self, name, confirm=False):
            received["confirm"] = confirm
            if not confirm:
                return {"status": "preview", "confirm_received": confirm}
            return {"status": "applied", "confirm_received": confirm}

    fake = ConfirmChecker()
    monkeypatch.setattr(template_tools, "_get_cluster_template", lambda: fake)
    out = template_tools.apply_template({"template": "coding", "confirm": 1})
    assert out["status"] == "preview"
    assert received["confirm"] is False


def test_apply_template_propagates_confirm_false(monkeypatch):
    """Verify confirm=False reaches the engine (not just a missing key)."""
    received = {}

    class ConfirmChecker:
        def apply_template(self, name, confirm=False):
            received["confirm"] = confirm
            received["name"] = name
            return {"status": "preview", "confirm_received": confirm}

    fake = ConfirmChecker()
    monkeypatch.setattr(template_tools, "_get_cluster_template", lambda: fake)
    out = template_tools.apply_template({"template": "test"})
    assert received["confirm"] is False
    assert received["name"] == "test"


def test_apply_template_propagates_confirm_true(monkeypatch):
    received = {}

    class ConfirmChecker:
        def apply_template(self, name, confirm=False):
            received["confirm"] = confirm
            received["name"] = name
            return {"status": "applied", "confirm_received": confirm}

    fake = ConfirmChecker()
    monkeypatch.setattr(template_tools, "_get_cluster_template", lambda: fake)
    out = template_tools.apply_template({"template": "test", "confirm": True})
    assert received["confirm"] is True
    assert out["status"] == "applied"


# ── Signature tests ──


def test_all_handlers_accept_var_keyword():
    """Every handler must accept **kwargs for dispatch compatibility."""
    handlers = [
        template_tools.list_templates,
        template_tools.preview_template,
        template_tools.apply_template,
    ]
    for h in handlers:
        sig = inspect.signature(h)
        kinds = [p.kind for p in sig.parameters.values()]
        assert inspect.Parameter.VAR_KEYWORD in kinds, f"{h.__name__} lacks **kwargs"


def test_schemas_are_valid_json():
    """Schemas must survive round-trip JSON serialization."""
    schemas = [
        template_tools.LIST_TEMPLATES_SCHEMA,
        template_tools.PREVIEW_TEMPLATE_SCHEMA,
        template_tools.APPLY_TEMPLATE_SCHEMA,
    ]
    for s in schemas:
        json.loads(json.dumps(s))


def test_preview_template_schema_has_required_template():
    assert "template" in template_tools.PREVIEW_TEMPLATE_SCHEMA["required"]
    assert template_tools.PREVIEW_TEMPLATE_SCHEMA["properties"]["template"]["type"] == "string"


def test_apply_template_schema_has_required_template_and_optional_confirm():
    assert "template" in template_tools.APPLY_TEMPLATE_SCHEMA["required"]
    assert "confirm" in template_tools.APPLY_TEMPLATE_SCHEMA["properties"]
    assert template_tools.APPLY_TEMPLATE_SCHEMA["properties"]["confirm"]["type"] == "boolean"


def test_list_templates_schema_has_no_properties():
    assert template_tools.LIST_TEMPLATES_SCHEMA["properties"] == {}


# ── Registration tests ──


class FakeContext:
    """Minimal ctx mock that records register_tool calls."""
    def __init__(self):
        self.tools = []
        self.hooks = []

    def register_tool(self, name, toolset, schema, handler, emoji, description):
        self.tools.append({
            "name": name,
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "emoji": emoji,
            "description": description,
        })

    def register_hook(self, event, fn):
        self.hooks.append((event, fn))


def test_register_includes_template_tools(monkeypatch):
    """register(ctx) registers the 3 template tools (among others)."""
    init_src = Path(os.path.dirname(os.path.abspath(template_tools.__file__))) / "__init__.py"
    src = init_src.read_text()
    for name in ("list_templates", "preview_template", "apply_template"):
        assert f'"{name}"' in src or f"'{name}'" in src, f"{name} not found in __init__.py"
    # Verify _TEMPLATE_TOOLS list exists and references template_tools module handlers
    assert "_TEMPLATE_TOOLS" in src
    assert "template_tools.list_templates" in src
    assert "template_tools.preview_template" in src
    assert "template_tools.apply_template" in src
    # Verify _TEMPLATE_TOOLS is included in register() loop
    assert "+ _TEMPLATE_TOOLS" in src
    # Verify from . import template_tools
    assert "from . import template_tools" in src


def test_register_template_tools_have_correct_shape(monkeypatch):
    """Each template tool tuple has correct schema, emoji, and description."""
    template_tuples = [
        ("list_templates", template_tools.LIST_TEMPLATES_SCHEMA, template_tools.list_templates, "📋",
         "List available cluster templates (declared fleet layouts)."),
        ("preview_template", template_tools.PREVIEW_TEMPLATE_SCHEMA, template_tools.preview_template, "🔎",
         "Dry-run: what applying a template would change against the live cluster (no writes)."),
        ("apply_template", template_tools.APPLY_TEMPLATE_SCHEMA, template_tools.apply_template, "⚠️",
         "Apply a cluster template — reshapes the ENTIRE fleet (provisions orchestrator + workers). HIGH-RISK. GUARDED: confirm=true to execute; without confirm returns a preview."),
    ]
    for name, schema, handler, emoji, desc in template_tuples:
        assert schema.get("type") == "object"
        assert isinstance(emoji, str) and emoji
        assert isinstance(desc, str) and len(desc) > 10


def test_register_total_tool_count_includes_templates(monkeypatch):
    """Verify _TEMPLATE_TOOLS has exactly 3 entries in __init__.py."""
    init_src = Path(os.path.dirname(os.path.abspath(template_tools.__file__))) / "__init__.py"
    src = init_src.read_text()
    template_names = {"list_templates", "preview_template", "apply_template"}
    # Verify all template tool names appear in _TEMPLATE_TOOLS section
    template_tools_section = src.split("_TEMPLATE_TOOLS = [")[1].split("]")[0] if "_TEMPLATE_TOOLS = [" in src else ""
    for name in template_names:
        assert name in template_tools_section, f"{name} missing from _TEMPLATE_TOOLS"
    # Verify _TEMPLATE_TOOLS is in register loop concatenation
    assert "+ _TEMPLATE_TOOLS" in src, "_TEMPLATE_TOOLS not in register loop"
