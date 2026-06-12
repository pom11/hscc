"""Tests for discovery.py — the single topology source (WS2/D8)."""

import json
import pytest

import discovery as d


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
        monkeypatch.setattr(d, "_run",
                            lambda args, timeout=20: {"ok": True, "stdout": "ok", "stderr": ""})
        res = d.nas_status()
        assert res["nas"] == "192.0.2.20" and res["mounted"] is True

    def test_nas_not_mounted(self, monkeypatch):
        topo = d.topology_from_sparkrun(
            d.parse_sparkrun_clusters(SPARKRUN_FIXTURE), enrich=CLUSTER_JSON_FIXTURE)
        monkeypatch.setattr(d, "discover", lambda **k: topo)
        monkeypatch.setattr(d, "_run",
                            lambda args, timeout=20: {"ok": True, "stdout": "fail", "stderr": ""})
        res = d.nas_status()
        assert res["mounted"] is False

    def test_single_probe_no_fanout(self, monkeypatch):
        """Staging constraint: NAS health is ONE probe, never a per-worker fan-out."""
        topo = d.topology_from_sparkrun(
            d.parse_sparkrun_clusters(SPARKRUN_FIXTURE), enrich=CLUSTER_JSON_FIXTURE)
        monkeypatch.setattr(d, "discover", lambda **k: topo)
        calls = []
        monkeypatch.setattr(d, "_run",
                            lambda args, timeout=20: calls.append(args) or {"ok": True, "stdout": "ok", "stderr": ""})
        d.nas_status()
        assert len(calls) == 1   # exactly one ssh probe


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
