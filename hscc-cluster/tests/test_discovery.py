"""Tests for discovery.py — the single topology source (WS2/D8)."""

import json
import pytest

import discovery as d

#: The REAL `_write_cache`, captured at import time. tests/conftest.py's
#: autouse `_pin_discovery_io` replaces it with a recorder, so a test that must
#: prove what actually lands on disk has to reach around that fixture.
_REAL_WRITE_CACHE = d._write_cache


SPARKRUN_FIXTURE = json.dumps([{
    "name": "hscc",
    "hosts": ["192.0.2.10", "192.0.2.11", "192.0.2.12", "192.0.2.13"],
    "user": "spark", "cache_dir": "/mnt/nas", "default": True,
}])

CLUSTER_JSON_FIXTURE = {
    "name": "hscc",
    "gateway": {"ip": "192.0.2.10", "name": "GX10 Gateway", "sshUser": "spark", "id": "gw"},
    "workers": [
        {"ip": "192.0.2.11", "name": "GX10 #1", "id": "w1"},
        {"ip": "192.0.2.12", "name": "GX10 #2", "id": "w2"},
    ],
    "nasDevices": [{"ip": "192.0.2.20", "name": "qnap"}],
}


class TestParseSparkrun:
    def test_picks_default(self):
        raw = json.dumps([
            {"name": "a", "hosts": ["1.1.1.1"], "default": False},
            {"name": "b", "hosts": ["2.2.2.2"], "default": True},
        ])
        assert d.parse_sparkrun_clusters(raw)["name"] == "b"

    def test_only_one(self):
        assert d.parse_sparkrun_clusters(SPARKRUN_FIXTURE)["name"] == "hscc"

    def test_garbage(self):
        assert d.parse_sparkrun_clusters("not json") is None
        assert d.parse_sparkrun_clusters("[]") is None


class TestTopologyFromSparkrun:
    def test_first_host_is_gateway(self):
        topo = d.topology_from_sparkrun(d.parse_sparkrun_clusters(SPARKRUN_FIXTURE))
        assert topo.orchestrator.ip == "192.0.2.10"
        assert topo.orchestrator.role == "gateway"
        assert topo.worker_ips == ["192.0.2.11", "192.0.2.12", "192.0.2.13"]
        assert topo.source == "live"

    def test_enrichment_adds_names(self):
        topo = d.topology_from_sparkrun(
            d.parse_sparkrun_clusters(SPARKRUN_FIXTURE), enrich=CLUSTER_JSON_FIXTURE)
        assert topo.orchestrator.name == "GX10 Gateway"
        assert topo.orchestrator.id == "gw"
        # NAS ip comes from the enrich cluster.json (cache_dir is just a mount)
        assert topo.nas and topo.nas.ip == "192.0.2.20"

    def test_no_hosts_raises(self):
        with pytest.raises(d.DiscoveryError):
            d.topology_from_sparkrun({"hosts": []})


class TestTopologyFromClusterJson:
    def test_full(self):
        topo = d.topology_from_cluster_json(CLUSTER_JSON_FIXTURE)
        assert topo.orchestrator.ip == "192.0.2.10"
        assert topo.worker_ips == ["192.0.2.11", "192.0.2.12"]
        assert topo.nas.ip == "192.0.2.20"
        assert topo.source == "cache"

    def test_no_gateway_raises(self):
        with pytest.raises(d.DiscoveryError):
            d.topology_from_cluster_json({"workers": []})


class TestCacheRoundTrip:
    def test_to_cluster_json_then_back(self):
        topo = d.topology_from_sparkrun(
            d.parse_sparkrun_clusters(SPARKRUN_FIXTURE), enrich=CLUSTER_JSON_FIXTURE)
        round = d.topology_from_cluster_json(d.to_cluster_json(topo))
        assert round.orchestrator.ip == topo.orchestrator.ip
        assert round.worker_ips == topo.worker_ips
        assert round.nas.ip == topo.nas.ip


class TestNvidiaSmi:
    def test_parse(self):
        # name, memory.total(MiB), memory.free(MiB), power.draw(W)
        caps = d.parse_nvidia_smi("NVIDIA GB10, 124416, 119000, 12.5")
        assert caps["gpu_model"] == "NVIDIA GB10"
        assert caps["vram_total_gb"] == pytest.approx(121.5, abs=0.5)
        assert caps["vram_free_gb"] == pytest.approx(116.2, abs=0.5)
        assert caps["power_draw_w"] == 12.5

    def test_garbage(self):
        assert d.parse_nvidia_smi("") == {}
        assert d.parse_nvidia_smi("only,three,fields") == {}


class TestClassifyIdle:
    def test_idle_by_power_not_util(self):
        assert d.classify_idle(12.0) is True      # low power = idle
        assert d.classify_idle(60.0) is False     # high power = busy
        assert d.classify_idle(None) is None      # unknown


class TestDiscoverPrecedence:
    def _patch_run(self, monkeypatch, *, live_ok, raw=""):
        def fake_run(args, timeout=20):
            if args[:3] == [d.SPARKRUN, "cluster", "list"]:
                return {"ok": live_ok, "stdout": raw, "stderr": ""}
            return {"ok": False, "stdout": "", "stderr": ""}
        monkeypatch.setattr(d, "_run", fake_run)

    def test_live_wins(self, monkeypatch, tmp_path):
        self._patch_run(monkeypatch, live_ok=True, raw=SPARKRUN_FIXTURE)
        monkeypatch.setattr(d, "CLUSTER_JSON", str(tmp_path / "cluster.json"))
        monkeypatch.setattr(d, "_read_cluster_json", lambda: CLUSTER_JSON_FIXTURE)
        topo = d.discover()
        assert topo.source == "live"
        assert topo.orchestrator.ip == "192.0.2.10"

    def test_cache_when_live_fails(self, monkeypatch):
        self._patch_run(monkeypatch, live_ok=False)
        monkeypatch.setattr(d, "_read_cluster_json", lambda: CLUSTER_JSON_FIXTURE)
        topo = d.discover()
        assert topo.source == "cache"
        assert topo.orchestrator.ip == "192.0.2.10"

    def test_fail_loud_when_both_absent(self, monkeypatch):
        self._patch_run(monkeypatch, live_ok=False)
        monkeypatch.setattr(d, "_read_cluster_json", lambda: None)
        with pytest.raises(d.DiscoveryError):
            d.discover()

    def test_never_returns_fake_ip(self, monkeypatch):
        """Regression: the old clusterlib silently fell back to 192.0.2.x docs
        IPs. discovery must raise instead — assert no fake topology is returned."""
        self._patch_run(monkeypatch, live_ok=False)
        monkeypatch.setattr(d, "_read_cluster_json", lambda: None)
        try:
            topo = d.discover()
        except d.DiscoveryError:
            return  # correct
        pytest.fail(f"discover() returned {topo} instead of raising")


class TestNasStatus:
    def test_no_nas(self, monkeypatch):
        topo = d.topology_from_sparkrun(d.parse_sparkrun_clusters(SPARKRUN_FIXTURE))
        monkeypatch.setattr(d, "discover", lambda **k: topo)  # no enrich → no nas
        res = d.nas_status()
        assert res["ok"] is True and res["nas"] is None

    def test_nas_mounted(self, monkeypatch):
        topo = d.topology_from_sparkrun(
            d.parse_sparkrun_clusters(SPARKRUN_FIXTURE), enrich=CLUSTER_JSON_FIXTURE)
        monkeypatch.setattr(d, "discover", lambda **k: topo)
        monkeypatch.setattr(d.sparkrun_bridge, "remote_cmd",
                            lambda host, command, timeout=30: {"ok": True, "stdout": "ok", "stderr": "", "code": 0})
        res = d.nas_status()
        assert res["nas"] == "192.0.2.20" and res["mounted"] is True

    def test_nas_not_mounted(self, monkeypatch):
        topo = d.topology_from_sparkrun(
            d.parse_sparkrun_clusters(SPARKRUN_FIXTURE), enrich=CLUSTER_JSON_FIXTURE)
        monkeypatch.setattr(d, "discover", lambda **k: topo)
        monkeypatch.setattr(d.sparkrun_bridge, "remote_cmd",
                            lambda host, command, timeout=30: {"ok": True, "stdout": "fail", "stderr": "", "code": 0})
        res = d.nas_status()
        assert res["mounted"] is False

    def test_single_probe_no_fanout(self, monkeypatch):
        """Staging constraint: NAS health is ONE probe, never a per-worker fan-out."""
        topo = d.topology_from_sparkrun(
            d.parse_sparkrun_clusters(SPARKRUN_FIXTURE), enrich=CLUSTER_JSON_FIXTURE)
        monkeypatch.setattr(d, "discover", lambda **k: topo)
        calls = []
        monkeypatch.setattr(d.sparkrun_bridge, "remote_cmd",
                            lambda host, command, timeout=30: calls.append(host) or {"ok": True, "stdout": "ok", "stderr": "", "code": 0})
        d.nas_status()
        assert len(calls) == 1   # exactly one sparkrun remote probe


class TestAutoAdopt:
    def test_live_membership_authoritative(self, monkeypatch):
        """A host added to the sparkrun cluster appears even if the cache
        (cluster.json) predates it; a removed host drops out."""
        raw = json.dumps([{
            "name": "hscc",
            "hosts": ["192.0.2.10", "192.0.2.11", "192.0.2.99"],  # .99 is new, .12 gone
            "user": "spark", "default": True,
        }])

        def fake_run(args, timeout=20):
            if args[:3] == [d.SPARKRUN, "cluster", "list"]:
                return {"ok": True, "stdout": raw, "stderr": ""}
            return {"ok": False, "stdout": "", "stderr": ""}
        monkeypatch.setattr(d, "_run", fake_run)
        monkeypatch.setattr(d, "_read_cluster_json", lambda: CLUSTER_JSON_FIXTURE)
        monkeypatch.setattr(d, "_write_cache", lambda topo: None)
        topo = d.discover()
        assert "192.0.2.99" in topo.worker_ips      # adopted
        assert "192.0.2.12" not in topo.worker_ips  # dropped


class TestCachePreservesOperatorKeys:
    """t_7c3ec484: `_write_cache` used to write `to_cluster_json(topo)` and
    nothing else, so every hand-added top-level key in ~/.hscc/cluster.json
    silently evaporated on the next discovery sweep — including the
    ``node_recovery`` opt-out blocks that install_autoreboot /
    install_notifier_mask / install_gpu_clock_cap / node_recovery read and that
    bootstrap.sh tells operators to write there. One sweep deleted an opt-out
    and the feature silently re-enabled."""

    OPERATOR_KEYS = {
        # the documented opt-out block (watchdog + autoreboot subkeys)
        "node_recovery": {"watchdog": False, "autoreboot": False},
        # the shape t_32f97d20's allowlist would have had if it lived here
        "target_profiles": ["backend-engineer", "reviewer"],
        # a key discovery has never heard of — ownership, not an allowlist
        "some_future_thing": {"nested": [1, 2, 3]},
    }

    def _seed(self, tmp_path, extra=None):
        """cluster.json as discovery last wrote it, plus operator keys."""
        data = dict(CLUSTER_JSON_FIXTURE)
        data.update(extra if extra is not None else self.OPERATOR_KEYS)
        path = tmp_path / "cluster.json"
        path.write_text(json.dumps(data))
        return path

    def _live_discovery(self, monkeypatch):
        """sparkrun answers live (the conftest fixture already does) and the
        REAL cache writer runs, so the assertion is about the file on disk."""
        monkeypatch.setattr(d, "_write_cache", _REAL_WRITE_CACHE)

    def test_operator_keys_survive_live_discovery(self, monkeypatch, tmp_path):
        """THE regression: seed node_recovery + a target_profiles-shaped key,
        run discover() with sparkrun stubbed live, both keys still there."""
        path = self._seed(tmp_path)
        self._live_discovery(monkeypatch)

        topo = d.discover()
        assert topo.source == "live"          # discovery really went live
        assert topo.worker_ips == ["192.0.2.11", "192.0.2.12", "192.0.2.13"]

        on_disk = json.loads(path.read_text())
        assert on_disk["node_recovery"] == self.OPERATOR_KEYS["node_recovery"]
        assert on_disk["target_profiles"] == self.OPERATOR_KEYS["target_profiles"]
        assert on_disk["some_future_thing"] == self.OPERATOR_KEYS["some_future_thing"]
        # and the topology itself is still the live one, not the seeded one
        assert [w["ip"] for w in on_disk["workers"]] == topo.worker_ips

    def test_opt_out_reader_still_reads_the_preserved_block(self, monkeypatch, tmp_path):
        """Honesty rule (same as t_32f97d20's reader): a preserved key must be
        readable by the ORIGINAL reader, unchanged. Here: the real
        node_recovery reader, pointed at the file discovery just rewrote."""
        import node_recovery as nr
        path = self._seed(tmp_path)
        self._live_discovery(monkeypatch)
        d.discover()

        cfg = nr.node_recovery_config(str(path))
        assert cfg["watchdog"] is False and cfg["autoreboot"] is False
        assert cfg["config_error"] == ""

    def test_owned_keys_stay_live_authoritative(self, monkeypatch, tmp_path):
        """Preservation must NOT resurrect a removed node: owned keys always
        come from the live topology, even though the old file lists .12."""
        raw = json.dumps([{"name": "hscc",
                           "hosts": ["192.0.2.10", "192.0.2.11", "192.0.2.99"],
                           "user": "spark", "default": True}])

        def fake_run(args, timeout=20):
            if args[:3] == [d.SPARKRUN, "cluster", "list"]:
                return {"ok": True, "stdout": raw, "stderr": ""}
            return {"ok": False, "stdout": "", "stderr": ""}
        monkeypatch.setattr(d, "_run", fake_run)
        path = self._seed(tmp_path)
        self._live_discovery(monkeypatch)

        topo = d.discover()
        on_disk = json.loads(path.read_text())
        assert "192.0.2.12" not in [w["ip"] for w in on_disk["workers"]]
        assert "192.0.2.99" in [w["ip"] for w in on_disk["workers"]]
        assert on_disk["node_recovery"] == self.OPERATOR_KEYS["node_recovery"]
        assert topo.source == "live"

    def test_absent_old_file_is_quiet(self, monkeypatch, tmp_path, capsys):
        """First run (no cluster.json yet) writes the topology and says nothing
        — the warning below is only for a file that exists but cannot be read."""
        self._live_discovery(monkeypatch)
        d.discover()
        assert json.loads((tmp_path / "cluster.json").read_text())["name"] == "hscc"
        assert capsys.readouterr().err == ""

    def test_unreadable_old_file_is_reported_not_silent(self, monkeypatch, tmp_path,
                                                        capsys):
        """Present-but-corrupt means operator state is about to be overwritten.
        Discovery must still succeed (cache write is best-effort) but must say
        out loud what it is about to lose."""
        path = tmp_path / "cluster.json"
        path.write_text("{ this is not json")
        self._live_discovery(monkeypatch)

        topo = d.discover()
        assert topo.source == "live"                       # never fails discovery
        err = capsys.readouterr().err
        assert "node_recovery" in err and "LOST" in err    # names the damage
        assert json.loads(path.read_text())["name"] == "hscc"  # file repaired

    def test_merge_preserved_keys_is_pure_and_ownership_based(self):
        """The pure function: fresh wins on owned keys, everything unknown is
        carried verbatim, and a non-dict `previous` is 'nothing to preserve'."""
        fresh = {"name": "hscc", "gateway": {"ip": "192.0.2.10"},
                 "workers": [{"ip": "192.0.2.11"}]}
        prev = {"name": "stale", "gateway": {"ip": "198.51.100.1"},
                "workers": [], "nasDevices": [{"ip": "192.0.2.20"}],
                "nas_devices": [{"ip": "192.0.2.20"}],
                "node_recovery": {"watchdog": False}, "x": 1}
        out = d.merge_preserved_keys(prev, fresh)
        assert out["name"] == "hscc"                     # fresh, not stale
        assert out["gateway"]["ip"] == "192.0.2.10"      # fresh, not stale
        assert "nasDevices" not in out and "nas_devices" not in out
        assert out["node_recovery"] == {"watchdog": False} and out["x"] == 1
        assert d.merge_preserved_keys(None, fresh) == fresh
        assert d.merge_preserved_keys(["nope"], fresh) == fresh
        # fresh is never mutated
        assert "x" not in fresh
        # ownership list covers exactly what to_cluster_json can write
        topo = d.topology_from_cluster_json(CLUSTER_JSON_FIXTURE)
        assert set(d.to_cluster_json(topo)) <= set(d.OWNED_CACHE_KEYS)


class TestProbeNodeRealPort:
    """_probe_node reads serving.json for real ports instead of hardcoding 8000."""

    # Facts that sparkrun would fetch for a node's GPU capability.
    NVIDIA_OK = {"ok": True, "stdout": "GB10, 124416, 119000, 12.5", "stderr": "", "code": 0}

    def _patch_sparkrun_seam(self, monkeypatch, result=NVIDIA_OK):
        """Inject a fake at the sparkrun seam — tests never ssh anywhere."""
        monkeypatch.setattr(d.sparkrun_bridge, "remote_cmd",
                            lambda host, command, timeout=30: dict(result))

    def test_fallback_8000_when_no_serving_json(self, monkeypatch, tmp_path):
        """When serving.json is missing, probe port 8000."""
        monkeypatch.setattr(d, "SERVING_JSON", str(tmp_path / "serving.json"))
        self._patch_sparkrun_seam(monkeypatch)
        call_args = []
        def fake_run(args, timeout=20):
            call_args.append(args)
            return {"ok": True, "stdout": '{"data":[]}', "stderr": ""}
        monkeypatch.setattr(d, "_run", fake_run)

        node = d.Node(ip="192.0.2.5", role="worker")
        d._probe_node(node)

        assert node.gpu_model == "GB10"
        assert node.vram_total_gb == 121.5
        assert node.vram_free_gb == 116.21
        assert node.vllm_healthy is True
        curl_calls = [a for a in call_args if a[0] == "curl"]
        assert len(curl_calls) == 1
        assert ":8000" in curl_calls[0][-1]
        # No raw ssh reaches _run — that route lives at the sparkrun seam now.
        assert all(a[0] != "ssh" for a in call_args)

    def test_probes_real_port_from_serving_json(self, monkeypatch, tmp_path):
        """When serving.json declares a non-8000 port, probe that port."""
        serving_path = tmp_path / "serving.json"
        serving_path.write_text(json.dumps({
            "units": [{
                "id": "unit-1",
                "nodes": ["192.0.2.5"],
                "port": 8001,
            }]
        }))
        monkeypatch.setattr(d, "SERVING_JSON", str(serving_path))
        self._patch_sparkrun_seam(monkeypatch)

        call_args = []
        def fake_run(args, timeout=20):
            call_args.append(args)
            if ":8000" in args[-1]:
                return {"ok": False, "stdout": "", "stderr": "timeout"}
            return {"ok": True, "stdout": '{"data":[]}', "stderr": ""}
        monkeypatch.setattr(d, "_run", fake_run)

        node = d.Node(ip="192.0.2.5", role="worker")
        d._probe_node(node)

        assert node.vllm_healthy is True
        assert node.gpu_model == "GB10"
        curl_calls = [a for a in call_args if a[0] == "curl"]
        assert len(curl_calls) == 2
        assert ":8000" in curl_calls[0][-1]
        assert ":8001" in curl_calls[1][-1]

    def test_stops_at_first_healthy_port(self, monkeypatch, tmp_path):
        """If port 8000 responds, don't waste time probing extra ports."""
        serving_path = tmp_path / "serving.json"
        serving_path.write_text(json.dumps({
            "units": [{
                "id": "unit-1",
                "nodes": ["192.0.2.5"],
                "port": 8001,
            }]
        }))
        monkeypatch.setattr(d, "SERVING_JSON", str(serving_path))
        self._patch_sparkrun_seam(monkeypatch)

        call_args = []
        def fake_run(args, timeout=20):
            call_args.append(args)
            return {"ok": True, "stdout": '{"data":[]}', "stderr": ""}
        monkeypatch.setattr(d, "_run", fake_run)

        node = d.Node(ip="192.0.2.5", role="worker")
        d._probe_node(node)

        assert node.vllm_healthy is True
        curl_calls = [a for a in call_args if a[0] == "curl"]
        assert len(curl_calls) == 1

    def test_ignores_units_on_different_nodes(self, monkeypatch, tmp_path):
        """A serving.json unit on another node's IP should not affect this probe."""
        serving_path = tmp_path / "serving.json"
        serving_path.write_text(json.dumps({
            "units": [{
                "id": "unit-1",
                "nodes": ["192.0.2.99"],
                "port": 8001,
            }]
        }))
        monkeypatch.setattr(d, "SERVING_JSON", str(serving_path))
        self._patch_sparkrun_seam(monkeypatch)

        call_args = []
        def fake_run(args, timeout=20):
            call_args.append(args)
            return {"ok": True, "stdout": '{"data":[]}', "stderr": ""}
        monkeypatch.setattr(d, "_run", fake_run)

        node = d.Node(ip="192.0.2.5", role="worker")
        d._probe_node(node)

        assert node.gpu_model == "GB10"
        curl_calls = [a for a in call_args if a[0] == "curl"]
        assert len(curl_calls) == 1
        assert ":8000" in curl_calls[0][-1]

    def test_healthy_false_when_all_ports_fail(self, monkeypatch, tmp_path):
        """Mark vllm_healthy=False when every probed port fails."""
        serving_path = tmp_path / "serving.json"
        serving_path.write_text(json.dumps({
            "units": [{
                "id": "unit-1",
                "nodes": ["192.0.2.5"],
                "port": 8001,
            }]
        }))
        monkeypatch.setattr(d, "SERVING_JSON", str(serving_path))
        self._patch_sparkrun_seam(monkeypatch)

        def fake_run(args, timeout=20):
            return {"ok": False, "stdout": "", "stderr": "connection refused"}
        monkeypatch.setattr(d, "_run", fake_run)

        node = d.Node(ip="192.0.2.5", role="worker")
        d._probe_node(node)

        assert node.vllm_healthy is False
