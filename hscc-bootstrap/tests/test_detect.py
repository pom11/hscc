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


def test_recipe_model_reads_top_level_field(tmp_path):
    r = tmp_path / "qwen.yaml"
    r.write_text("model: Qwen/Qwen3.6-27B-FP8\nruntime: vllm\n")
    assert detect.recipe_model(str(r)) == "Qwen/Qwen3.6-27B-FP8"


def test_recipe_model_missing_returns_none(tmp_path):
    r = tmp_path / "x.yaml"
    r.write_text("runtime: vllm\n")
    assert detect.recipe_model(str(r)) is None


def test_list_recipes_finds_yaml(tmp_path):
    (tmp_path / "a.yaml").write_text("model: A\n")
    sub = tmp_path / "local-fixed"
    sub.mkdir()
    (sub / "b.yaml").write_text("model: B\n")
    found = detect.list_recipes(str(tmp_path))
    names = {r.rsplit("/", 1)[-1] for r in found}
    assert {"a.yaml", "b.yaml"}.issubset(names)
