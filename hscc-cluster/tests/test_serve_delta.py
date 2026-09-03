"""Tests for cluster_template._diff_serving_delta — the serve_delta preview.

These are pure unit tests of the read-only diff that names which serving
units START, STOP, and MOVE (relocate/rescale) and which nodes are affected.
They run without a cluster or file I/O, driving the function with synthetic
serving-unit dicts. Keeping them in their own module avoids touching the large
apply-integration file and keeps the delta contract legible.
"""

import sys
from pathlib import Path
from dataclasses import dataclass

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from cluster_template import _diff_serving_delta  # noqa: E402


def _unit(uid, role, model, nodes, port, tp=1, pp=1, family=None):
    u = {"id": uid, "role": role, "model": model, "nodes": list(nodes),
         "port": port, "tp": tp, "pp": pp}
    if family is not None:
        u["family"] = family
    return u


class TestServeDelta:
    """preview_template's serve_delta — the honest per-unit WHAT WILL CHANGE.

    ``_diff_serving_delta`` diffs the LIVE serving.json units against the
    resolved NEW serving units. It names START / STOP / MOVE (relocate or
    rescale) and the affected nodes so the operator never applies a template
    blind. Every unit id/port here is synthetic and node IPs are the documented
    10.0.0.x LAN placeholders — never real cluster addresses.
    """

    def test_no_change_reports_all_unchanged_and_empty_delta(self):
        cur = {"units": [
            _unit("orch", "orchestrator", "deepseek-v3", ["10.0.0.1"], 8000),
            _unit("a1", "worker", "llama-3.1", ["10.0.0.2"], 8001),
        ]}
        new = {"units": [
            _unit("orch", "orchestrator", "deepseek-v3", ["10.0.0.1"], 8000),
            _unit("a1", "worker", "llama-3.1", ["10.0.0.2"], 8001),
        ]}
        d = _diff_serving_delta(cur, new)
        assert d["start"] == [] and d["stop"] == [] and d["move"] == []
        assert len(d["unchanged"]) == 2
        # Nothing actually changes — no node is affected by an apply.
        assert d["affected_nodes"] == []

    def test_grow_is_a_start(self):
        cur = {"units": [_unit("a1", "worker", "llama-3.1", ["10.0.0.2"], 8001)]}
        new = {"units": [
            _unit("a1", "worker", "llama-3.1", ["10.0.0.2"], 8001),
            _unit("a2", "worker", "llama-3.1", ["10.0.0.3"], 8002),
        ]}
        d = _diff_serving_delta(cur, new)
        assert len(d["start"]) == 1
        assert d["start"][0]["id"] == "a2"
        assert d["stop"] == [] and d["move"] == []
        assert d["affected_nodes"] == ["10.0.0.3"]

    def test_shrink_is_a_stop(self):
        cur = {"units": [
            _unit("a1", "worker", "llama-3.1", ["10.0.0.2"], 8001),
            _unit("a2", "worker", "llama-3.1", ["10.0.0.3"], 8002),
        ]}
        new = {"units": [_unit("a1", "worker", "llama-3.1", ["10.0.0.2"], 8001)]}
        d = _diff_serving_delta(cur, new)
        assert len(d["stop"]) == 1
        assert d["stop"][0]["id"] == "a2"
        assert d["start"] == [] and d["move"] == []
        assert d["affected_nodes"] == ["10.0.0.3"]

    def test_relocate_is_a_move_not_stop_plus_start(self):
        cur = {"units": [_unit("a1", "worker", "llama-3.1", ["10.0.0.2"], 8001)]}
        new = {"units": [_unit("a1", "worker", "llama-3.1", ["10.0.0.4"], 8001)]}
        d = _diff_serving_delta(cur, new)
        assert len(d["move"]) == 1
        m = d["move"][0]
        assert m["from_nodes"] == ["10.0.0.2"] and m["to_nodes"] == ["10.0.0.4"]
        assert d["start"] == [] and d["stop"] == []
        assert d["affected_nodes"] == ["10.0.0.2", "10.0.0.4"]

    def test_rescale_is_a_move(self):
        cur = {"units": [_unit("a1", "worker", "llama-3.1", ["10.0.0.2"], 8001, tp=1)]}
        new = {"units": [_unit("a1", "worker", "llama-3.1", ["10.0.0.2"], 8001, tp=2)]}
        d = _diff_serving_delta(cur, new)
        assert len(d["move"]) == 1
        assert d["move"][0]["from_tp"] == 1 and d["move"][0]["to_tp"] == 2

    def test_absent_current_every_thing_is_a_start(self):
        d = _diff_serving_delta(None, {"units": [_unit("a1", "worker", "llama-3.1",
                                                       ["10.0.0.2"], 8001)]})
        assert len(d["start"]) == 1
        assert d["stop"] == [] and d["move"] == []
        assert d["affected_nodes"] == ["10.0.0.2"]

    def test_malformed_units_are_ignored_not_crash(self):
        d = _diff_serving_delta({"units": ["garbage", None, 5]},
                                {"units": [_unit("a1", "worker", "llama-3.1",
                                                ["10.0.0.2"], 8001)]})
        assert len(d["start"]) == 1
        assert d["stop"] == [] and d["move"] == []


# ── preview_template emits serve_delta on a real resolution ──────────────────

@dataclass
class _FakeNode:
    ip: str
    vram_free_gb: float = 120.0


@dataclass
class _FakeTopo:
    orchestrator: _FakeNode
    workers: list


def _integration_topology(n=3):
    return _FakeTopo(_FakeNode("10.0.0.1"),
                     [_FakeNode(f"10.0.0.{2 + i}") for i in range(n)])


class TestPreviewServeDeltaPresent:
    """preview_template always carries a serve_delta key (even \"no change\")
    so the app is never guessing whether to render the section."""

    def test_preview_always_has_serve_delta(self, monkeypatch):
        import cluster_template as ct
        import template_intent as ti
        import recipe_cost as rc
        monkeypatch.setattr(ct, "_discover", lambda probe=False: _integration_topology(3))
        monkeypatch.setattr(ti._rc, "recipe_cost",
                            lambda r: rc.RecipeCost(r, per_gpu_total_gb=30, fits=True))
        monkeypatch.setattr(ti, "Path_isfile", lambda r: True)
        monkeypatch.setattr(ct.Path, "is_file", lambda self: True)

        res = ct.preview_template("single-family")
        assert "serve_delta" in res
        sd = res["serve_delta"]
        for key in ("start", "stop", "move", "unchanged", "affected_nodes"):
            assert key in sd
        # The resolve produced at least the orchestrator → observable units.
        assert len(sd["start"]) + len(sd["unchanged"]) + len(sd["move"]) >= 1