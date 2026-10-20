import os, sys

# Put the plugin dir on sys.path so `import clusterlib` resolves without
# importing the plugin package (whose hyphenated dir name isn't importable).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import clusterlib


@pytest.fixture(autouse=True)
def _pin_serving_json_to_tmp(monkeypatch, tmp_path):
    """Defense-in-depth: redirect cluster_template.SERVING_JSON to a per-test
    tmp file so NO cluster-template test can write fixture data into the
    operator's LIVE ~/.hscc/serving.json (t_501fb7f1 — the fourth live-state
    leak in this project).

    ``_provision_models`` stamps a unit's serve_cmd into SERVING_JSON whenever a
    launch returns 0. Several test helpers call it directly with mock subprocess
    and, historically, did NOT redirect SERVING_JSON — leaking fixture
    serve_cmds (e.g. recipe ~/recipes/orch.yaml → /Users/desac/recipes/orch.yaml,
    hosts 10.0.0.x, port 9000) into the real serving.json. Tests that need a
    SPECIFIC serving.json still override it themselves (monkeypatch.setattr);
    this fixture only guarantees the default is never the live path.
    """
    import cluster_template as ct
    monkeypatch.setattr(ct, "SERVING_JSON", tmp_path / "serving.json")


@pytest.fixture(autouse=True)
def _stub_sparkrun_proxy_write(monkeypatch):
    """Defense-in-depth (same doctrine as _pin_serving_json_to_tmp): NO test may
    drive the real sparkrun proxy.yaml write path.

    ``_apply_proxy_declaration``'s default writer shells into sparkrun's venv
    and merges into ``~/.config/sparkrun/proxy.yaml`` — the operator's LIVE
    balancer config. On this host sparkrun IS on PATH, so an apply-integration
    test on a uniform_pool template would otherwise write the real file. Here the
    default writer is a no-op recorder that reports "already in effect" (changed
    False — no restart_required, apply stays successful); a test that wants a
    different outcome monkeypatches ``ct._sparkrun_proxy_write`` (or passes
    ``_write=``) itself, which runs after this fixture and wins.
    """
    import cluster_template as ct
    calls = []

    def _stub(regions):
        calls.append(regions)
        return {"ok": True, "error": None, "mode": "direct", "changed": False,
                "before": None, "after": None}

    monkeypatch.setattr(ct, "_sparkrun_proxy_write", _stub)
    monkeypatch.setattr(ct, "_SPARKRUN_WRITE_CALLS", calls)


@pytest.fixture(autouse=True)
def _pin_topology(monkeypatch):
    """Pin cluster topology to fixed example IPs so tests are independent of
    the machine's live ~/.hscc/cluster.json (which clusterlib resolves from at
    import). Tests assert against these documentation-range addresses."""
    monkeypatch.setattr(clusterlib, "HEAD", "192.0.2.10", raising=False)
    monkeypatch.setattr(clusterlib, "NODES", ["192.0.2.11", "192.0.2.12", "192.0.2.13"], raising=False)
    monkeypatch.setattr(clusterlib, "NAS_HOST", "192.0.2.20", raising=False)
