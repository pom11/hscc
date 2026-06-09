import os, sys

# Put the plugin dir on sys.path so `import clusterlib` resolves without
# importing the plugin package (whose hyphenated dir name isn't importable).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import clusterlib


@pytest.fixture(autouse=True)
def _pin_topology(monkeypatch):
    """Pin cluster topology to fixed example IPs so tests are independent of
    the machine's live ~/.hscc/cluster.json (which clusterlib resolves from at
    import). Tests assert against these documentation-range addresses."""
    monkeypatch.setattr(clusterlib, "HEAD", "192.0.2.10", raising=False)
    monkeypatch.setattr(clusterlib, "NODES", ["192.0.2.11", "192.0.2.12", "192.0.2.13"], raising=False)
    monkeypatch.setattr(clusterlib, "NAS_HOST", "192.0.2.20", raising=False)
