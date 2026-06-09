import json
import detect


def test_parse_default_cluster():
    raw = json.dumps([
        {"name": "hscc", "hosts": ["10.0.0.1", "10.0.0.2"], "user": "spark",
         "cache_dir": "/mnt/nas", "default": True},
    ])
    c = detect.parse_clusters(raw)
    assert c["name"] == "hscc"
    assert c["hosts"] == ["10.0.0.1", "10.0.0.2"]
    assert c["user"] == "spark"
    assert c["nas"] == "/mnt/nas"


def test_picks_default_among_many():
    raw = json.dumps([
        {"name": "a", "hosts": ["1.1.1.1"], "user": "x", "cache_dir": "", "default": False},
        {"name": "b", "hosts": ["2.2.2.2"], "user": "y", "cache_dir": "", "default": True},
    ])
    assert detect.parse_clusters(raw)["name"] == "b"


def test_single_cluster_no_default_flag():
    raw = json.dumps([{"name": "solo", "hosts": ["9.9.9.9"], "user": "u", "cache_dir": ""}])
    c = detect.parse_clusters(raw)
    assert c["name"] == "solo"
    assert c["nas"] is None


def test_no_clusters_returns_none():
    assert detect.parse_clusters("[]") is None
    assert detect.parse_clusters("") is None
    assert detect.parse_clusters("not json") is None
