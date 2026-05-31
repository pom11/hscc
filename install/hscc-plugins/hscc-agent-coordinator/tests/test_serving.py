#!/usr/bin/env python3
"""Unit tests for serving.json routing + capacity (coordinator pure core).

Run: cd hscc-agent-coordinator && python3 -m unittest tests.test_serving -v
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

# Isolate state (bridge.json) under a temp HSCC_HOME before importing the module,
# so capacity tests never touch the live ~/.hscc/bridge.json.
_TMP_HOME = tempfile.mkdtemp(prefix="hscc-test-")
os.environ["HSCC_HOME"] = _TMP_HOME


def _load():
    p = Path(__file__).resolve().parent.parent / "hscc.py"
    spec = importlib.util.spec_from_file_location("coord_hscc_test", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


H = _load()


class TestParseWorkerUnits(unittest.TestCase):
    def test_basic_filters_to_workers(self):
        s = {"version": 1, "port": 8000, "units": [
            {"id": "orch-244", "role": "orchestrator",
             "nodes": ["192.0.2.10"], "recipe": "r", "model": "m"},
            {"id": "worker-246", "role": "worker",
             "nodes": ["192.0.2.11"], "recipe": "r", "model": "m",
             "max_workers": 3}]}
        wu = H.parse_worker_units(s)
        self.assertEqual([u["id"] for u in wu], ["worker-246"])
        self.assertEqual(wu[0]["head"], "192.0.2.11")
        self.assertEqual(wu[0]["max_workers"], 3)
        self.assertEqual(wu[0]["recipe"], "r")

    def test_default_cap_is_4(self):
        s = {"units": [{"id": "w", "role": "worker",
                        "nodes": ["192.0.2.12"], "recipe": "r", "model": "m"}]}
        self.assertEqual(H.parse_worker_units(s)[0]["max_workers"], 4)

    def test_skip_units_without_nodes(self):
        s = {"units": [{"id": "w", "role": "worker", "nodes": [],
                        "recipe": "r", "model": "m"}]}
        self.assertEqual(H.parse_worker_units(s), [])

    def test_empty_or_none(self):
        self.assertEqual(H.parse_worker_units(None), [])
        self.assertEqual(H.parse_worker_units({}), [])


class TestPickUnit(unittest.TestCase):
    WU = [{"id": "worker-246", "head": "192.0.2.11", "max_workers": 4},
          {"id": "worker-247", "head": "192.0.2.12", "max_workers": 4},
          {"id": "worker-248", "head": "192.0.2.13", "max_workers": 4}]

    def test_most_free_capacity(self):
        self.assertEqual(
            H.pick_unit(self.WU, {"worker-246": 3, "worker-247": 1, "worker-248": 2}),
            "worker-247")

    def test_tie_break_lowest_octet(self):
        self.assertEqual(H.pick_unit(self.WU, {}), "worker-246")

    def test_spread_2_2_1(self):
        # 5 sequential picks across 3 cap-4 units -> 2/2/1, never 4/1/0.
        load = {}
        picks = []
        for _ in range(5):
            u = H.pick_unit(self.WU, load)
            picks.append(u)
            load[u] = load.get(u, 0) + 1
        counts = sorted(load.values())
        self.assertEqual(counts, [1, 2, 2])

    def test_all_full_returns_none(self):
        self.assertIsNone(H.pick_unit(
            [{"id": "w", "head": "192.0.2.11", "max_workers": 2}], {"w": 2}))

    def test_empty_units_none(self):
        self.assertIsNone(H.pick_unit([], {}))


class TestLoadServing(unittest.TestCase):
    def test_missing_returns_none(self):
        self.assertIsNone(H.load_serving(path="/nonexistent/serving.json"))

    def test_malformed_returns_none(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not json")
            bad = f.name
        try:
            self.assertIsNone(H.load_serving(path=bad))
        finally:
            os.unlink(bad)

    def test_valid_roundtrip(self):
        data = {"version": 1, "port": 8000, "units": [
            {"id": "worker-246", "role": "worker",
             "nodes": ["192.0.2.11"], "recipe": "r", "model": "m"}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            good = f.name
        try:
            got = H.load_serving(path=good)
            self.assertEqual(got["units"][0]["id"], "worker-246")
        finally:
            os.unlink(good)


class TestCapacityAccounting(unittest.TestCase):
    """Capacity is DERIVED from bridge task statuses via reconcile_unit_load."""

    def setUp(self):
        H.save_bridge({"tasks": {}})

    def test_reconcile_counts_only_live_entries(self):
        H.save_bridge({"tasks": {
            "t1": {"unit_id": "worker-246", "status": "released"},
            "t2": {"unit_id": "worker-246", "status": "held"},
            "t3": {"unit_id": "worker-247", "status": "released"},
            "t4": {"unit_id": "worker-247", "status": "cancelled"},
            "t5": {"unit_id": "worker-247", "status": "done"},
        }, "_unit_load": {"worker-246": 99}})  # stale value must be overwritten
        load = H.reconcile_unit_load()
        self.assertEqual(load.get("worker-246"), 2)
        self.assertEqual(load.get("worker-247"), 1)  # cancelled+done excluded
        # Stored cache matches the recomputed truth.
        self.assertEqual(H.load_bridge().get("_unit_load"), load)

    def test_terminal_status_frees_capacity(self):
        # held -> counts; flip to done -> no longer counts (the decrement path).
        H.save_bridge({"tasks": {"t1": {"unit_id": "worker-246", "status": "held"}}})
        self.assertEqual(H.reconcile_unit_load().get("worker-246"), 1)
        b = H.load_bridge()
        b["tasks"]["t1"]["status"] = "done"
        H.save_bridge(b)
        self.assertEqual(H.reconcile_unit_load().get("worker-246", 0), 0)

    def test_pick_respects_existing_held_entries(self):
        # Derived capacity: a held entry on 246 steers the next pick to 247.
        H.save_bridge({"tasks": {
            "t1": {"unit_id": "worker-246", "status": "held"}}})
        wu = [{"id": "worker-246", "head": "192.0.2.11", "max_workers": 4},
              {"id": "worker-247", "head": "192.0.2.12", "max_workers": 4}]
        self.assertEqual(H.pick_unit(wu, H.reconcile_unit_load()), "worker-247")


class TestServingActive(unittest.TestCase):
    def test_active_with_worker_unit(self):
        # serving_active reads the live SERVING_JSON path; craft one in temp.
        import unittest.mock as mock
        s = {"units": [{"id": "w", "role": "worker",
                        "nodes": ["192.0.2.11"], "recipe": "r", "model": "m"}]}
        with mock.patch.object(H, "load_serving", return_value=s):
            self.assertTrue(H.serving_active())

    def test_inactive_when_absent(self):
        import unittest.mock as mock
        with mock.patch.object(H, "load_serving", return_value=None):
            self.assertFalse(H.serving_active())


if __name__ == "__main__":
    unittest.main()
