"""Tests for the node-pause agent tools (card t_c28f4f14).

The tools delegate to ``hscc_daemon.hscc``'s own verb functions, so these
tests are mostly about the DELEGATION contract: the dry-run default survives
the trip through the tool surface, ``confirm`` is the agent's spelling of
``--apply``, the refusal reaches the agent as ``refused`` (not as a silent
empty result), and there is no argument combination by which a tool call
mutates something the CLI would refuse.

Hermetic: 192.0.2.x placeholders, and ``serving.load_serving`` /
``node_pause_exec.fetch_cluster_status`` / ``util.run_cmd`` all patched — no
subprocess, no fleet. Unlike the ``hscc_daemon`` suite, THIS suite has no
autouse ``~/.hscc`` isolation (it lives in another package), so the fixture
below seals the overlay and the heal ledger itself: without it, a passing
"pause a node" test would write a real pause into the operator's
``~/.hscc/node_pauses.json`` and the daemon would start skipping a live node.
"""

import json
import os

import pytest

import node_pause_tools as npt
from hscc_daemon import heal_breaker, node_pause_exec as npe
from hscc_daemon import node_pauses, serving, util

ORCH = "192.0.2.11"
W1 = "192.0.2.12"
W2 = "192.0.2.13"

SERVING = {
    "port": 8000,
    "units": [
        {"id": "orch", "role": "orchestrator", "nodes": [ORCH], "port": 8000,
         "recipe": "/srv/recipes/orchestrator.yaml",
         "model": "meta-llm/Orch-8B", "tp": 1},
        {"id": "w1", "role": "worker", "nodes": [W1], "port": 8100,
         "recipe": "/srv/recipes/worker.yaml",
         "model": "meta-llm/Work-8B", "tp": 1},
        {"id": "wspan", "role": "worker", "nodes": [W1, W2], "port": 8200,
         "recipe": "/srv/recipes/worker-tp.yaml",
         "model": "meta-llm/Work-8B", "tp": 2},
    ],
}


def _group(cid, recipe, hosts, port, model="meta-llm/Work-8B", served=None,
           tp=None):
    meta = {"recipe": recipe, "hosts": list(hosts), "port": port,
            "model": model}
    if served:
        meta["served_model_name"] = served
    if tp:
        meta["tensor_parallel"] = tp
    return {cid: {"meta": meta,
                  "containers": [{"host": h, "role": "worker",
                                  "status": "running", "image": "img"}
                                 for h in hosts]}}


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, timeout=None, **kw):
        self.calls.append([str(c) for c in cmd])
        return {"ok": True, "output": "", "stderr": ""}


@pytest.fixture(autouse=True)
def _seal_hscc_state(tmp_path, monkeypatch):
    """This suite has no ~/.hscc isolation of its own — seal what these tools
    write before any test can reach the operator's real files."""
    monkeypatch.setattr(node_pauses, "HSCC_DIR", str(tmp_path / "hscc"))
    monkeypatch.setattr(heal_breaker, "HEAL_BREAKER_FILE",
                        str(tmp_path / "hscc" / "heal-breaker.json"))
    return tmp_path


@pytest.fixture
def fleet(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(serving, "load_serving", lambda path=None: SERVING)
    monkeypatch.setattr(npe, "fetch_cluster_status", lambda: ({
        "groups": {
            **_group("cid_orch", "/srv/recipes/orchestrator.yaml", [ORCH],
                     8000, model="meta-llm/Orch-8B",
                     served="meta-llm/Orch-8B orchestrator-model"),
            **_group("cid_w1", "/srv/recipes/worker.yaml", [W1], 8100,
                     served="meta-llm/Work-8B worker-model"),
            **_group("cid_span", "/srv/recipes/worker-tp.yaml", [W1, W2],
                     8200, tp=2, served="meta-llm/Work-8B worker-model"),
        },
        "solo_entries": [], "errors": {}, "idle_hosts": [],
        "preparing_hosts": [], "pending_ops": [], "total_containers": 0,
        "host_count": 0,
    }, None))
    monkeypatch.setattr(util, "run_cmd", rec)
    return rec


# ── the dry-run default survives the tool surface ──────────────────────────
class TestDryRunDefault:
    def test_pause_without_confirm_previews_and_changes_nothing(self, fleet):
        out = npt.node_pause({"host": W1})
        assert out["executed"] is False
        assert out["preview"] is True
        assert fleet.calls == [], "a preview must not stop anything"
        assert node_pauses.paused(W1) is None
        assert "confirm=true" in out["note"]
        # The plan itself is what the operator would have seen.
        assert "w1" in out["report"] and W2 in out["report"]

    def test_resume_without_confirm_previews_and_changes_nothing(self, fleet):
        node_pauses.pause(W1, units=["w1", "wspan"], span_nodes=[W1, W2])
        out = npt.node_resume({"host": W1})
        assert out["executed"] is False and out["preview"] is True
        assert fleet.calls == []
        assert node_pauses.paused(W1) is not None
        assert "WOULD RESUME" in out["report"]

    def test_confirm_is_the_agents_spelling_of_apply(self, fleet):
        out = npt.node_pause({"host": W1, "span": True, "confirm": True})
        assert out["executed"] is True and out["ok"] is True
        assert len(fleet.calls) == 2, fleet.calls
        entry = node_pauses.paused(W1)
        assert entry is not None and entry["stopped"] is True
        assert entry["units"] == ["w1", "wspan"]


class TestNoAgentEscapeHatch:
    """Every CLI refusal must arrive as a refusal here too."""

    def test_orchestrator_node_is_refused_without_force(self, fleet):
        out = npt.node_pause({"host": ORCH, "confirm": True})
        assert out["refused"] is True
        assert out["executed"] is False and out["ok"] is False
        assert "orchestrator model" in out["report"]
        assert fleet.calls == []
        assert node_pauses.paused(ORCH) is None

    def test_force_overrides_exactly_as_it_does_on_the_cli(self, fleet):
        out = npt.node_pause({"host": ORCH, "confirm": True, "force": True})
        assert out["executed"] is True
        assert node_pauses.paused(ORCH) is not None

    def test_span_unit_needs_span_even_with_confirm(self, fleet):
        out = npt.node_pause({"host": W1, "confirm": True})
        assert out["refused"] is True
        assert fleet.calls == []
        assert node_pauses.paused(W1) is None

    def test_bad_until_is_refused_rather_than_parking_forever(self, fleet):
        out = npt.node_pause({"host": W1, "span": True, "confirm": True,
                              "until": "2days"})
        assert out["ok"] is False and out["executed"] is False
        assert "not a duration" in out["report"]
        assert node_pauses.paused(W1) is None


class TestResumeTool:
    def test_confirmed_resume_clears_pause_and_strikes(self, fleet):
        # STRIKES FIRST, PARK SECOND: note_heal() accrues NOTHING while the
        # pause overlay holds the node (#568, heal_breaker.note_heal), so the
        # old order left this resume clearing a slate that was never written and
        # `suspended()` was 0.0 for the whole test. Same law the daemon-side
        # twin pins, and for the same reason.
        for _ in range(7):
            heal_breaker.note_heal(W1)
        assert heal_breaker.suspended(W1) > 0.0, \
            "the fixture must trip the breaker for this to mean anything"
        node_pauses.pause(W1, units=["w1", "wspan"], span_nodes=[W1, W2])
        out = npt.node_resume({"host": W1, "confirm": True})
        assert out["executed"] is True and out["ok"] is True
        assert all("--ensure" in " ".join(c) for c in fleet.calls)
        assert node_pauses.paused(W1) is None
        assert heal_breaker.strike_count(W1) == 0

    def test_span_sibling_is_pointed_at_the_owner(self, fleet):
        node_pauses.pause(W1, units=["w1", "wspan"], span_nodes=[W1, W2])
        out = npt.node_resume({"host": W2, "confirm": True})
        assert out["ok"] is False
        assert W1 in out["report"]
        assert fleet.calls == []


class TestPausedTool:
    def test_returns_the_overlay_as_structured_data(self, fleet):
        assert npt.node_paused({}) == {"ok": True, "rc": 0, "count": 0,
                                       "pauses": []}
        node_pauses.pause(W1, reason="disk swap", units=["w1"],
                          span_nodes=[W1, W2], stopped=True)
        out = npt.node_paused({})
        assert out["count"] == 1
        row = out["pauses"][0]
        assert row["host"] == W1
        assert row["reason"] == "disk swap"
        assert row["units"] == ["w1"]
        assert row["span_nodes"] == [W1, W2]

    def test_is_read_only(self, fleet):
        npt.node_paused({})
        assert fleet.calls == []


class TestDelegationIsTheWholeDesign:
    """The tools add no policy of their own — assert it structurally, so a
    future edit that re-implements a guard here is caught, not admired."""

    def test_report_is_the_cli_itsown_bytes(self, fleet):
        from hscc_daemon import hscc as unified
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = unified._node_pause([W1])
        out = npt.node_pause({"host": W1})
        assert out["report"] == buf.getvalue().strip()
        assert out["rc"] == rc

    def test_the_module_holds_no_executor_call_of_its_own(self):
        """No second path to the fleet: this module must not import or call
        the executor / node_pauses — everything goes through the CLI."""
        import inspect
        src = inspect.getsource(npt)
        for forbidden in ("node_pause_exec", "node_pauses.", "run_cmd",
                          "stop_units", "resume_units", "sparkrun"):
            assert forbidden not in src, (
                f"{forbidden} in node_pause_tools.py means a second "
                f"implementation of a policy the CLI already owns")

    def test_schemas_default_confirm_false_and_forbid_extra_keys(self):
        for schema in (npt.NODE_PAUSE_SCHEMA, npt.NODE_RESUME_SCHEMA):
            assert schema["properties"]["confirm"]["default"] is False
            assert schema["additionalProperties"] is False
            assert schema["required"] == ["host"]
        assert npt.NODE_PAUSED_SCHEMA["additionalProperties"] is False

    def test_manifest_declares_the_three_tools(self):
        """plugin.yaml is what `hermes tools` advertises. The drift test
        enforces full two-way equality with the registry; this pins that these
        three specifically are declared (a missing row there means the fleet
        never learns the tool exists, and verify.check_plugins cannot see it
        either)."""
        import re
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "plugin.yaml")
        with open(path) as f:
            text = f.read()
        for name in ("node_pause", "node_resume", "node_paused"):
            assert re.search(rf"^\s+- {name}$", text, re.MULTILINE), name
