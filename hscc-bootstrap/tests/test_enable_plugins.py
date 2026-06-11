import json

import yaml

import enable_plugins


def _write(p, data):
    p.write_text(yaml.safe_dump(data))
    return str(p)


# ── plugins.enabled ──────────────────────────────────────────────────────────

def test_adds_missing_plugins(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"model": {"default": "X"},
                   "plugins": {"enabled": ["hscc-cluster"]},
                   "toolsets": ["hermes-cli", "hscc-cluster", "sparkrun"]})
    res = enable_plugins.enable(path)
    assert res["plugins"] == ["hscc-commands", "sparkrun-hermes"]
    cfg = yaml.safe_load(open(path))
    assert cfg["plugins"]["enabled"] == [
        "hscc-cluster", "hscc-commands", "sparkrun-hermes"]
    assert cfg["model"]["default"] == "X"          # rest preserved


def test_creates_enabled_when_absent(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"toolsets": ["hermes-cli", "hscc-cluster", "sparkrun"]})
    res = enable_plugins.enable(path)
    assert set(res["plugins"]) == {
        "hscc-cluster", "hscc-commands", "sparkrun-hermes"}


# ── toolsets ─────────────────────────────────────────────────────────────────

def test_adds_missing_toolset_to_list(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": [
                      "hscc-cluster", "hscc-commands", "sparkrun-hermes"]},
                   "toolsets": ["hermes-cli", "hscc-cluster", "kanban"]})
    res = enable_plugins.enable(path)
    assert res["toolsets"] == ["sparkrun", "delegation"]
    cfg = yaml.safe_load(open(path))
    assert cfg["toolsets"] == [
        "hermes-cli", "hscc-cluster", "kanban", "sparkrun", "delegation"]


def test_toolsets_json_string_normalized_to_list(tmp_path):
    # config may store toolsets as a JSON-string; we normalize to a YAML list.
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": [
                      "hscc-cluster", "hscc-commands", "sparkrun-hermes"]},
                   "toolsets": json.dumps(["hermes-cli", "kanban"])})
    res = enable_plugins.enable(path)
    assert set(res["toolsets"]) == {"hscc-cluster", "sparkrun", "delegation"}
    cfg = yaml.safe_load(open(path))
    assert isinstance(cfg["toolsets"], list)
    assert all(t in cfg["toolsets"]
               for t in ("hscc-cluster", "sparkrun", "delegation"))


def test_toolsets_absent_seeds_default_plus_hscc(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": [
                      "hscc-cluster", "hscc-commands", "sparkrun-hermes"]}})
    res = enable_plugins.enable(path)
    cfg = yaml.safe_load(open(path))
    assert cfg["toolsets"] == [
        "hermes-cli", "hscc-cluster", "sparkrun", "delegation"]
    assert set(res["toolsets"]) == {"hscc-cluster", "sparkrun", "delegation"}


# ── idempotency + guards ─────────────────────────────────────────────────────

def _fully_wired_cfg():
    return {
        "plugins": {"enabled": ["hscc-cluster", "hscc-commands", "sparkrun-hermes"]},
        "toolsets": ["hermes-cli", "hscc-cluster", "kanban", "sparkrun",
                     "delegation"],
        "kanban": {"default_assignee": enable_plugins.DEFAULT_ASSIGNEE,
                   "max_in_progress": enable_plugins.MAX_IN_PROGRESS,
                   "max_in_progress_per_profile":
                       enable_plugins.MAX_IN_PROGRESS_PER_PROFILE},
        "delegation": {"base_url": enable_plugins.WORKER_PROXY_URL,
                       "model": enable_plugins.WORKER_MODEL,
                       "provider": "custom",
                       "api_key": enable_plugins.WORKER_PROXY_KEY,
                       "max_concurrent_children":
                           enable_plugins.MAX_CONCURRENT_CHILDREN},
    }


def test_fully_wired_is_noop(tmp_path):
    path = _write(tmp_path / "config.yaml", _fully_wired_cfg())
    before = open(path).read()
    res = enable_plugins.enable(path)
    assert res == {"plugins": [], "toolsets": [], "kanban": [], "delegation": []}
    assert open(path).read() == before              # no rewrite, no backup churn


def test_missing_config_noop(tmp_path):
    res = enable_plugins.enable(str(tmp_path / "nope.yaml"))
    assert res == {"plugins": [], "toolsets": [], "kanban": [], "delegation": []}


# ── fleet routing (kanban + delegation) ──────────────────────────────────────

def test_routing_filled_on_fresh_config(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": [
                      "hscc-cluster", "hscc-commands", "sparkrun-hermes"]},
                   "toolsets": ["hermes-cli", "hscc-cluster", "kanban", "sparkrun"]})
    res = enable_plugins.enable(path)
    assert "default_assignee" in res["kanban"]
    assert set(res["delegation"]) == {
        "base_url", "model", "provider", "api_key", "max_concurrent_children"}
    cfg = yaml.safe_load(open(path))
    assert cfg["kanban"]["default_assignee"] == enable_plugins.DEFAULT_ASSIGNEE
    assert cfg["kanban"]["max_in_progress"] == enable_plugins.MAX_IN_PROGRESS
    assert cfg["delegation"]["base_url"] == enable_plugins.WORKER_PROXY_URL
    assert cfg["delegation"]["max_concurrent_children"] == \
        enable_plugins.MAX_CONCURRENT_CHILDREN


def test_routing_preserves_operator_choices(tmp_path):
    # An operator-set default_assignee + a LARGER cap + a custom delegation
    # endpoint must all be kept.
    cfg = _fully_wired_cfg()
    cfg["kanban"]["default_assignee"] = "my-special-worker"
    cfg["kanban"]["max_in_progress"] = 99           # larger than default
    cfg["delegation"]["base_url"] = "http://my-proxy:9000/v1"
    path = _write(tmp_path / "config.yaml", cfg)
    res = enable_plugins.enable(path)
    assert res == {"plugins": [], "toolsets": [], "kanban": [], "delegation": []}
    out = yaml.safe_load(open(path))
    assert out["kanban"]["default_assignee"] == "my-special-worker"
    assert out["kanban"]["max_in_progress"] == 99   # not lowered
    assert out["delegation"]["base_url"] == "http://my-proxy:9000/v1"


def test_caps_raised_when_too_low(tmp_path):
    cfg = _fully_wired_cfg()
    cfg["kanban"]["max_in_progress"] = 2            # below default -> raise
    cfg["kanban"]["max_in_progress_per_profile"] = 1
    path = _write(tmp_path / "config.yaml", cfg)
    res = enable_plugins.enable(path)
    assert "max_in_progress" in res["kanban"]
    out = yaml.safe_load(open(path))
    assert out["kanban"]["max_in_progress"] == enable_plugins.MAX_IN_PROGRESS


def test_bad_plugins_shape_does_not_clobber(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"plugins": "weird-string",
                   "toolsets": ["hermes-cli", "hscc-cluster", "sparkrun"]})
    res = enable_plugins.enable(path)
    assert res["plugins"] == []
    assert yaml.safe_load(open(path))["plugins"] == "weird-string"


def test_bad_toolsets_shape_does_not_clobber(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": [
                      "hscc-cluster", "hscc-commands", "sparkrun-hermes"]},
                   "toolsets": {"weird": "dict"}})
    res = enable_plugins.enable(path)
    assert res["toolsets"] == []
    assert yaml.safe_load(open(path))["toolsets"] == {"weird": "dict"}
