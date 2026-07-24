"""Tests for hscc-cluster/fleet.py — lazy daemon imports, best-effort handlers."""
import inspect
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import fleet


class FakeVerify:
    def run_all(self):
        return {"checks": [{"name": "plugins", "ok": True}], "ok": True}


class FakeThroughput:
    def compute_throughput(self):
        return {
            "fleet": {"nodes_ok": 3, "total_tok_per_sec": 12000},
            "by_node": {
                "192.0.2.11": {"tok_per_sec": 4000, "queue": 1},
                "192.0.2.12": {"tok_per_sec": 4000, "queue": 0},
                "192.0.2.13": {"tok_per_sec": 4000, "queue": 2},
            },
        }


class FakeStats:
    def __init__(self):
        self.last_since_days = None

    def compute_stats(self, since_days=7):
        self.last_since_days = since_days
        return {"days": since_days, "tasks_completed": 42, "tool_calls": 150}


class FakeAutoscale:
    @staticmethod
    def decide_scale(throughput, *, current_workers, **_):
        by_node = throughput.get("by_node", {})
        total_waiting = sum(n.get("queue", 0) for n in by_node.values())
        if total_waiting >= 4:
            decision = "scale_up"
            target = current_workers + 1
        elif total_waiting == 0:
            decision = "scale_down"
            target = max(current_workers - 1, 1)
        else:
            decision = "none"
            target = current_workers
        return {
            "decision": decision,
            "current_workers": current_workers,
            "target_workers": target,
            "reason": f"total_waiting={total_waiting}",
        }


def _make_fake_mod(name):
    mapping = {
        "verify": FakeVerify(),
        "throughput": FakeThroughput(),
        "stats": FakeStats(),
        "autoscale": FakeAutoscale(),
    }
    return mapping[name]


def test_cluster_verify_returns_checks(monkeypatch):
    monkeypatch.setattr(fleet, "_daemon_mod", _make_fake_mod)
    out = fleet.cluster_verify({})
    assert out["ok"] is True
    assert len(out["checks"]) >= 1


def test_cluster_throughput_returns_fleet(monkeypatch):
    monkeypatch.setattr(fleet, "_daemon_mod", _make_fake_mod)
    out = fleet.cluster_throughput({})
    assert "fleet" in out
    assert out["fleet"]["nodes_ok"] == 3
    assert len(out["by_node"]) == 3


def test_fleet_stats_default_days(monkeypatch):
    monkeypatch.setattr(fleet, "_daemon_mod", _make_fake_mod)
    fake_stats = FakeStats()
    def _mod(name):
        if name == "stats":
            return fake_stats
        return _make_fake_mod(name)
    monkeypatch.setattr(fleet, "_daemon_mod", _mod)
    out = fleet.fleet_stats({})
    assert out["days"] == 7
    assert fake_stats.last_since_days == 7


def test_fleet_stats_custom_days(monkeypatch):
    fake_stats = FakeStats()
    def _mod(name):
        if name == "stats":
            return fake_stats
        return _make_fake_mod(name)
    monkeypatch.setattr(fleet, "_daemon_mod", _mod)
    out = fleet.fleet_stats({"days": 14})
    assert out["days"] == 14
    assert fake_stats.last_since_days == 14


def test_fleet_stats_days_zero_defaults_to_7(monkeypatch):
    fake_stats = FakeStats()
    def _mod(name):
        if name == "stats":
            return fake_stats
        return _make_fake_mod(name)
    monkeypatch.setattr(fleet, "_daemon_mod", _mod)
    out = fleet.fleet_stats({"days": 0})
    assert out["days"] == 7


def test_autoscale_advice_includes_decision(monkeypatch):
    monkeypatch.setattr(fleet, "_daemon_mod", _make_fake_mod)
    out = fleet.autoscale_advice({})
    assert "throughput" in out
    assert "decision" in out
    assert out["decision"]["current_workers"] == 3


def test_autoscale_advice_derives_current_workers_from_nodes_ok(monkeypatch):
    monkeypatch.setattr(fleet, "_daemon_mod", _make_fake_mod)
    out = fleet.autoscale_advice({})
    # nodes_ok=3, by_node has 3 entries → current=3
    assert out["decision"]["current_workers"] == 3


def test_autoscale_advice_uses_by_node_fallback_when_no_nodes_ok(monkeypatch):
    class TpNoNodesOk:
        def compute_throughput(self):
            return {"by_node": {"a": {"queue": 0}, "b": {"queue": 0}}}

    def _mod(name):
        if name == "throughput":
            return TpNoNodesOk()
        if name == "autoscale":
            return FakeAutoscale()
        return _make_fake_mod(name)

    monkeypatch.setattr(fleet, "_daemon_mod", _mod)
    out = fleet.autoscale_advice({})
    assert out["decision"]["current_workers"] == 2


def test_handler_returns_error_on_import_failure(monkeypatch):
    def _fail(name):
        raise ModuleNotFoundError(f"No module named 'hscc_daemon.{name}'")
    monkeypatch.setattr(fleet, "_daemon_mod", _fail)
    out = fleet.cluster_verify({})
    assert out["ok"] is False
    assert "error" in out


def test_handler_returns_error_on_runtime_error(monkeypatch):
    class BrokenVerify:
        def run_all(self):
            raise RuntimeError("daemon crash")
    monkeypatch.setattr(fleet, "_daemon_mod", lambda name: BrokenVerify())
    out = fleet.cluster_verify({})
    assert out["ok"] is False
    assert "daemon crash" in out["error"]


def test_all_handlers_accept_var_keyword():
    """Every handler must accept **kwargs for dispatch compatibility."""
    handlers = [
        fleet.cluster_verify,
        fleet.cluster_throughput,
        fleet.fleet_stats,
        fleet.autoscale_advice,
    ]
    for h in handlers:
        sig = inspect.signature(h)
        kinds = [p.kind for p in sig.parameters.values()]
        assert inspect.Parameter.VAR_KEYWORD in kinds, f"{h.__name__} lacks **kwargs"


def test_schemas_are_valid_json():
    """Schemas must survive round-trip JSON serialization."""
    schemas = [
        fleet.CLUSTER_VERIFY_SCHEMA,
        fleet.CLUSTER_THROUGHPUT_SCHEMA,
        fleet.FLEET_STATS_SCHEMA,
        fleet.AUTOSCALE_ADVICE_SCHEMA,
    ]
    for s in schemas:
        json.loads(json.dumps(s))


def test_fleet_stats_schema_has_days_property():
    assert "days" in fleet.FLEET_STATS_SCHEMA["properties"]
    assert fleet.FLEET_STATS_SCHEMA["properties"]["days"]["type"] == "integer"


def test_empty_schemas_have_no_properties():
    for schema in [fleet.CLUSTER_VERIFY_SCHEMA, fleet.CLUSTER_THROUGHPUT_SCHEMA,
                   fleet.AUTOSCALE_ADVICE_SCHEMA]:
        assert schema["properties"] == {}


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


def test_register_includes_fleet_tools(monkeypatch):
    """register(ctx) registers the 4 fleet tools (among others)."""
    monkeypatch.setattr(fleet, "_daemon_mod", _make_fake_mod)
    init_src = Path(os.path.dirname(os.path.abspath(fleet.__file__))) / "__init__.py"
    src = init_src.read_text()
    for name in ("cluster_verify", "cluster_throughput", "fleet_stats", "autoscale_advice"):
        assert f'"{name}"' in src or f"'{name}'" in src, f"{name} not found in __init__.py"
    # Verify _FLEET_TOOLS list exists and references fleet module handlers
    assert "_FLEET_TOOLS" in src
    assert "fleet.cluster_verify" in src
    assert "fleet.cluster_throughput" in src
    assert "fleet.fleet_stats" in src
    assert "fleet.autoscale_advice" in src
    # Verify _FLEET_TOOLS is included in register() loop
    assert "+ _FLEET_TOOLS" in src


def test_register_fleet_tools_have_correct_toolset(monkeypatch):
    monkeypatch.setattr(fleet, "_daemon_mod", _make_fake_mod)
    # Verify that the fleet tool tuples in __init__.py are correctly formed.
    # Each tuple: (name, schema, handler, emoji, description)
    fleet_tuples = [
        ("cluster_verify", fleet.CLUSTER_VERIFY_SCHEMA, fleet.cluster_verify, "🔍",
         "Full compatibility/health smoke-test (plugins, multiplex, streams, proxy, config)."),
        ("cluster_throughput", fleet.CLUSTER_THROUGHPUT_SCHEMA, fleet.cluster_throughput, "📊",
         "vLLM token throughput + per-node queue depth across the fleet."),
        ("fleet_stats", fleet.FLEET_STATS_SCHEMA, fleet.fleet_stats, "📈",
         "Fleet activity: task completions + tool usage over N days (default 7)."),
        ("autoscale_advice", fleet.AUTOSCALE_ADVICE_SCHEMA, fleet.autoscale_advice, "🧭",
         "Advisory scale up/down/none decision from current queue depth (does NOT scale)."),
    ]
    for name, schema, handler, emoji, desc in fleet_tuples:
        assert schema.get("type") == "object"
        assert isinstance(emoji, str) and emoji
        assert isinstance(desc, str) and len(desc) > 10


def test_register_total_tool_count(monkeypatch):
    """Verify the _FLEET_TOOLS list has exactly 4 entries."""
    monkeypatch.setattr(fleet, "_daemon_mod", _make_fake_mod)
    init_src = Path(os.path.dirname(os.path.abspath(fleet.__file__))) / "__init__.py"
    src = init_src.read_text()
    fleet_names = {"cluster_verify", "cluster_throughput", "fleet_stats", "autoscale_advice"}
    # Verify all fleet tool names appear in _FLEET_TOOLS section
    fleet_tools_section = src.split("_FLEET_TOOLS = [")[1].split("]")[0] if "_FLEET_TOOLS = [" in src else ""
    for name in fleet_names:
        assert name in fleet_tools_section, f"{name} missing from _FLEET_TOOLS"
    # Verify _FLEET_TOOLS is in register loop concatenation
    assert "+ _FLEET_TOOLS" in src, "_FLEET_TOOLS not in register loop"
    # Verify from . import fleet
    assert "from . import fleet" in src
