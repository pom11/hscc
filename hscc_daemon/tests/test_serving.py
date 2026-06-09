#!/usr/bin/env python3
"""Unit tests for serving.json reconcile helpers (daemon pure core).

Run: cd hscc_daemon && python3 -m unittest tests.test_serving -v
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


def _load():
    import sys
    import importlib
    # Add plugins/ to sys.path so hscc_daemon package imports resolve
    plugins_dir = str(Path(__file__).resolve().parent.parent.parent)
    if plugins_dir not in sys.path:
        sys.path.insert(0, plugins_dir)
    # Import as proper package module so relative imports work
    if "hscc_daemon.hscc" not in sys.modules:
        import hscc_daemon.hscc
    return sys.modules["hscc_daemon.hscc"]


H = _load()


class TestLoadServing(unittest.TestCase):
    def test_missing_returns_none(self):
        self.assertIsNone(H.load_serving(path="/nonexistent/serving.json"))

    def test_malformed_returns_none(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{bad json")
            bad = f.name
        try:
            self.assertIsNone(H.load_serving(path=bad))
        finally:
            os.unlink(bad)


class TestOrchestratorNodes(unittest.TestCase):
    SERVING = {"version": 1, "port": 8000, "units": [
        {"id": "orch-244", "role": "orchestrator",
         "nodes": ["192.0.2.10"], "recipe": "r", "model": "m"},
        {"id": "worker-246", "role": "worker",
         "nodes": ["192.0.2.11"], "recipe": "r", "model": "m"}]}

    def test_single_orchestrator(self):
        self.assertEqual(H.orchestrator_nodes(self.SERVING), {"192.0.2.10"})
        self.assertEqual(H.orchestrator_head(self.SERVING), "192.0.2.10")

    def test_multi_node_orchestrator_union(self):
        s = {"units": [{"id": "orch", "role": "orchestrator",
                        "nodes": ["192.0.2.10", "192.0.2.11"],
                        "recipe": "r", "model": "m"}]}
        self.assertEqual(H.orchestrator_nodes(s),
                         {"192.0.2.10", "192.0.2.11"})
        # head is nodes[0] — the single serving endpoint of the multi-node unit.
        self.assertEqual(H.orchestrator_head(s), "192.0.2.10")

    def test_none_serving(self):
        self.assertEqual(H.orchestrator_nodes(None), set())
        self.assertIsNone(H.orchestrator_head(None))

    def test_orchestrator_recipe(self):
        s = {"units": [{"id": "o", "role": "orchestrator", "nodes": ["192.0.2.10"],
                        "recipe": "~/r/a3b.yaml", "model": "m"}]}
        self.assertEqual(H.orchestrator_recipe(s),
                         os.path.expanduser("~/r/a3b.yaml"))

    def test_orchestrator_recipe_none_when_absent(self):
        self.assertIsNone(H.orchestrator_recipe(None))
        self.assertIsNone(H.orchestrator_recipe(
            {"units": [{"role": "worker", "nodes": ["192.0.2.11"], "recipe": "r"}]}))
        # orchestrator unit but no recipe key -> None (caller keeps fallback)
        self.assertIsNone(H.orchestrator_recipe(
            {"units": [{"role": "orchestrator", "nodes": ["192.0.2.10"]}]}))

    def test_no_orchestrator_unit(self):
        s = {"units": [{"id": "w", "role": "worker",
                        "nodes": ["192.0.2.11"], "recipe": "r", "model": "m"}]}
        self.assertEqual(H.orchestrator_nodes(s), set())
        self.assertIsNone(H.orchestrator_head(s))


class TestComputeBaseUrlChange(unittest.TestCase):
    OLD = "http://192.0.2.10:8000/v1"
    NEW = "http://192.0.2.12:8000/v1"

    def test_follows_old_orchestrator_endpoint(self):
        # A profile pointed at the OLD orchestrator endpoint follows to NEW.
        self.assertEqual(H.compute_base_url_change(self.OLD, self.OLD, self.NEW),
                         self.NEW)

    def test_worker_endpoint_untouched(self):
        # A worker profile (its own node) never equals the orchestrator endpoint,
        # so it is never rewritten — the model split is preserved.
        worker = "http://192.0.2.11:8000/v1"
        self.assertIsNone(H.compute_base_url_change(worker, self.OLD, self.NEW))

    def test_noop_when_already_new(self):
        self.assertIsNone(H.compute_base_url_change(self.NEW, self.OLD, self.NEW))

    def test_noop_when_endpoint_unchanged(self):
        self.assertIsNone(H.compute_base_url_change(self.OLD, self.OLD, self.OLD))


class TestServingPortEndpoint(unittest.TestCase):
    def test_port_default(self):
        self.assertEqual(H.serving_port({}), 8000)
        self.assertEqual(H.serving_port(None), 8000)

    def test_port_explicit(self):
        self.assertEqual(H.serving_port({"port": 9001}), 9001)

    def test_port_garbage_falls_back(self):
        self.assertEqual(H.serving_port({"port": "nope"}), 8000)

    def test_endpoint(self):
        s = {"port": 8000, "units": [{"id": "o", "role": "orchestrator",
             "nodes": ["192.0.2.10"], "recipe": "r", "model": "m"}]}
        self.assertEqual(H.orchestrator_endpoint(s),
                         "http://192.0.2.10:8000/v1")

    def test_endpoint_none_without_orchestrator(self):
        self.assertIsNone(H.orchestrator_endpoint({"units": []}))


class TestUpdateOrchestratorFollowers(unittest.TestCase):
    import unittest.mock as _mock

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hscc-prof-")
        self.state = os.path.join(self.tmp, ".orch_endpoint")
        self.profiles = os.path.join(self.tmp, "profiles")
        os.makedirs(self.profiles)
        self._p_dir = H.PROFILES_DIR
        self._p_state = H.ORCH_ENDPOINT_STATE
        H.PROFILES_DIR = self.profiles
        H.ORCH_ENDPOINT_STATE = self.state

    def tearDown(self):
        H.PROFILES_DIR = self._p_dir
        H.ORCH_ENDPOINT_STATE = self._p_state
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _profile(self, name, base_url):
        d = os.path.join(self.profiles, name)
        os.makedirs(d)
        with open(os.path.join(d, "config.yaml"), "w") as f:
            f.write("model:\n  provider: custom\n  base_url: %s\n" % base_url)

    def _read(self, name):
        with open(os.path.join(self.profiles, name, "config.yaml")) as f:
            return f.read()

    def _serving(self, head):
        return {"port": 8000, "units": [{"id": "o", "role": "orchestrator",
                "nodes": [head], "recipe": "r", "model": "m"}]}

    def test_first_run_records_baseline_no_rewrite(self):
        self._profile("default", "http://192.0.2.10:8000/v1")
        with self._mock.patch.object(H, "load_serving",
                                     return_value=self._serving("192.0.2.10")):
            H.update_orchestrator_followers()
        # No prior endpoint -> baseline only, profile untouched.
        self.assertIn("192.0.2.10", self._read("default"))
        self.assertEqual(H._read_prev_orch_endpoint(),
                         "http://192.0.2.10:8000/v1")

    def test_remap_follows_old_endpoint(self):
        self._profile("default", "http://192.0.2.10:8000/v1")
        self._profile("worker-246", "http://192.0.2.11:8000/v1")
        H._write_prev_orch_endpoint("http://192.0.2.10:8000/v1")
        with self._mock.patch.object(H, "load_serving",
                 return_value=self._serving("192.0.2.12")), \
             self._mock.patch.object(H, "_endpoint_healthy", return_value=True):
            H.update_orchestrator_followers()
        # default tracked OLD orchestrator -> follows to NEW.
        self.assertIn("http://192.0.2.12:8000/v1", self._read("default"))
        # worker pinned to its own node -> untouched (model split preserved).
        self.assertIn("http://192.0.2.11:8000/v1", self._read("worker-246"))
        self.assertEqual(H._read_prev_orch_endpoint(),
                         "http://192.0.2.12:8000/v1")

    def test_unhealthy_new_endpoint_defers(self):
        self._profile("default", "http://192.0.2.10:8000/v1")
        H._write_prev_orch_endpoint("http://192.0.2.10:8000/v1")
        with self._mock.patch.object(H, "load_serving",
                 return_value=self._serving("192.0.2.12")), \
             self._mock.patch.object(H, "_endpoint_healthy", return_value=False):
            H.update_orchestrator_followers()
        # Unhealthy candidate -> no rewrite, baseline unchanged.
        self.assertIn("http://192.0.2.10:8000/v1", self._read("default"))
        self.assertEqual(H._read_prev_orch_endpoint(),
                         "http://192.0.2.10:8000/v1")

    def test_fallback_mode_noop(self):
        self._profile("default", "http://192.0.2.10:8000/v1")
        with self._mock.patch.object(H, "load_serving", return_value=None):
            H.update_orchestrator_followers()
        self.assertIn("http://192.0.2.10:8000/v1", self._read("default"))

    def test_partial_write_failure_does_not_advance_endpoint(self):
        # Two profiles track the OLD endpoint; one write fails. The persisted
        # endpoint must stay OLD so the next tick retries the stranded profile.
        self._profile("default", "http://192.0.2.10:8000/v1")
        self._profile("orch-mirror", "http://192.0.2.10:8000/v1")
        H._write_prev_orch_endpoint("http://192.0.2.10:8000/v1")
        real_replace = os.replace

        def flaky_replace(src, dst):
            if dst.endswith(os.path.join("orch-mirror", "config.yaml")):
                raise OSError("simulated write failure")
            return real_replace(src, dst)

        with self._mock.patch.object(H, "load_serving",
                 return_value=self._serving("192.0.2.12")), \
             self._mock.patch.object(H, "_endpoint_healthy", return_value=True), \
             self._mock.patch.object(H.os, "replace", side_effect=flaky_replace):
            H.update_orchestrator_followers()
        # The healthy profile followed; the failed one did not.
        self.assertIn("http://192.0.2.12:8000/v1", self._read("default"))
        self.assertIn("http://192.0.2.10:8000/v1", self._read("orch-mirror"))
        # Endpoint NOT advanced -> next tick re-detects old!=new and retries.
        self.assertEqual(H._read_prev_orch_endpoint(),
                         "http://192.0.2.10:8000/v1")


class TestLiveDispatchHosts(unittest.TestCase):
    import unittest.mock as _mock

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hscc-bridge-")
        self.bridge = os.path.join(self.tmp, "bridge.json")
        self._orig = H.BRIDGE_FILE
        H.BRIDGE_FILE = self.bridge

    def tearDown(self):
        H.BRIDGE_FILE = self._orig
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, tasks):
        with open(self.bridge, "w") as f:
            json.dump({"tasks": tasks}, f)

    def test_missing_bridge_empty(self):
        self.assertEqual(H.live_dispatch_hosts(), set())

    def test_legacy_entries_ignored(self):
        # No unit_id -> legacy roster dispatch, protected elsewhere, not here.
        self._write({"t1": {"worker_host": "192.0.2.11", "status": "released",
                            "board": "b", "kanban_id": "k"}})
        with self._mock.patch.object(H, "_kanban_task_status",
                                     return_value=("running", None)):
            self.assertEqual(H.live_dispatch_hosts(), set())

    def test_held_unit_entry_protected(self):
        # held = provisioning / awaiting release -> protect without kanban call.
        self._write({"t1": {"unit_id": "worker-246", "worker_host": "192.0.2.11",
                            "status": "held"}})
        self.assertEqual(H.live_dispatch_hosts(), {"192.0.2.11"})

    def test_released_running_protected(self):
        self._write({"t1": {"unit_id": "worker-247", "worker_host": "192.0.2.12",
                            "status": "released", "board": "b", "kanban_id": "k"}})
        with self._mock.patch.object(H, "_kanban_task_status",
                                     return_value=("running", None)):
            self.assertEqual(H.live_dispatch_hosts(), {"192.0.2.12"})

    def test_released_finished_not_protected(self):
        self._write({"t1": {"unit_id": "worker-248", "worker_host": "192.0.2.13",
                            "status": "released", "board": "b", "kanban_id": "k"}})
        with self._mock.patch.object(H, "_kanban_task_status",
                                     return_value=("done", None)):
            self.assertEqual(H.live_dispatch_hosts(), set())

    def test_released_unknown_kanban_protected_failsafe(self):
        # No board/kid -> cannot prove finished -> protect (never reap on doubt).
        self._write({"t1": {"unit_id": "worker-246", "worker_host": "192.0.2.11",
                            "status": "released"}})
        self.assertEqual(H.live_dispatch_hosts(), {"192.0.2.11"})


class TestWorkerRecipeFor(unittest.TestCase):
    """Test _worker_recipe_for resolves per-node worker recipes from serving.json."""

    def test_worker_node_returns_recipe(self):
        from hscc_daemon import serving as S
        serving = {
            "units": [
                {"role": "worker", "nodes": ["10.0.0.1"], "recipe": "~/recipes/w.yaml"},
            ]
        }
        r = S._worker_recipe_for("10.0.0.1", serving)
        self.assertTrue(r.endswith("recipes/w.yaml"))

    def test_orchestrator_node_returns_none(self):
        from hscc_daemon import serving as S
        serving = {
            "units": [
                {"role": "orchestrator", "nodes": ["10.0.0.2"], "recipe": "~/recipes/o.yaml"},
            ]
        }
        self.assertIsNone(S._worker_recipe_for("10.0.0.2", serving))

    def test_unknown_node_returns_none(self):
        from hscc_daemon import serving as S
        serving = {
            "units": [
                {"role": "worker", "nodes": ["10.0.0.1"], "recipe": "~/recipes/w.yaml"},
            ]
        }
        self.assertIsNone(S._worker_recipe_for("10.0.0.99", serving))

    def test_none_serving_returns_none(self):
        from hscc_daemon import serving as S
        self.assertIsNone(S._worker_recipe_for("10.0.0.1", None))
        self.assertIsNone(S._worker_recipe_for("10.0.0.1", {}))

    def test_worker_no_recipe_returns_none(self):
        from hscc_daemon import serving as S
        serving = {
            "units": [
                {"role": "worker", "nodes": ["10.0.0.1"]},  # no recipe key
            ]
        }
        self.assertIsNone(S._worker_recipe_for("10.0.0.1", serving))

    def test_multi_node_worker(self):
        from hscc_daemon import serving as S
        serving = {
            "units": [
                {"role": "worker", "nodes": ["10.0.0.1", "10.0.0.2"], "recipe": "~/recipes/multi.yaml"},
            ]
        }
        r1 = S._worker_recipe_for("10.0.0.1", serving)
        r2 = S._worker_recipe_for("10.0.0.2", serving)
        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)
        self.assertTrue(r1.endswith("recipes/multi.yaml"))
        self.assertTrue(r2.endswith("recipes/multi.yaml"))


if __name__ == "__main__":
    unittest.main()
