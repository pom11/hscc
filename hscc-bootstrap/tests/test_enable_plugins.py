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
    assert res["toolsets"] == ["sparkrun"]
    cfg = yaml.safe_load(open(path))
    assert cfg["toolsets"] == ["hermes-cli", "hscc-cluster", "kanban", "sparkrun"]


def test_toolsets_json_string_normalized_to_list(tmp_path):
    # config may store toolsets as a JSON-string; we normalize to a YAML list.
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": [
                      "hscc-cluster", "hscc-commands", "sparkrun-hermes"]},
                   "toolsets": json.dumps(["hermes-cli", "kanban"])})
    res = enable_plugins.enable(path)
    assert set(res["toolsets"]) == {"hscc-cluster", "sparkrun"}
    cfg = yaml.safe_load(open(path))
    assert isinstance(cfg["toolsets"], list)
    assert "sparkrun" in cfg["toolsets"] and "hscc-cluster" in cfg["toolsets"]


def test_toolsets_absent_seeds_default_plus_hscc(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": [
                      "hscc-cluster", "hscc-commands", "sparkrun-hermes"]}})
    res = enable_plugins.enable(path)
    cfg = yaml.safe_load(open(path))
    assert cfg["toolsets"] == ["hermes-cli", "hscc-cluster", "sparkrun"]
    assert set(res["toolsets"]) == {"hscc-cluster", "sparkrun"}


# ── idempotency + guards ─────────────────────────────────────────────────────

def test_fully_wired_is_noop(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": [
                      "hscc-cluster", "hscc-commands", "sparkrun-hermes"]},
                   "toolsets": ["hermes-cli", "hscc-cluster", "kanban", "sparkrun"]})
    before = open(path).read()
    res = enable_plugins.enable(path)
    assert res == {"plugins": [], "toolsets": []}
    assert open(path).read() == before              # no rewrite, no backup churn


def test_missing_config_noop(tmp_path):
    res = enable_plugins.enable(str(tmp_path / "nope.yaml"))
    assert res == {"plugins": [], "toolsets": []}


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
