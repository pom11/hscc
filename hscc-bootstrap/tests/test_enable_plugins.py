import yaml

import enable_plugins


def _write(p, data):
    p.write_text(yaml.safe_dump(data))
    return str(p)


def test_adds_missing(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"model": {"default": "X"}, "plugins": {"enabled": ["hscc-cluster"]}})
    added = enable_plugins.enable(path)
    assert added == ["hscc-commands", "sparkrun-hermes"]
    cfg = yaml.safe_load(open(path))
    assert cfg["plugins"]["enabled"] == [
        "hscc-cluster", "hscc-commands", "sparkrun-hermes"]
    assert cfg["model"]["default"] == "X"      # rest of config preserved


def test_idempotent(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": [
                      "hscc-cluster", "hscc-commands", "sparkrun-hermes"]}})
    assert enable_plugins.enable(path) == []


def test_creates_enabled_when_absent(tmp_path):
    path = _write(tmp_path / "config.yaml", {"model": {"default": "X"}})
    added = enable_plugins.enable(path)
    assert set(added) == {"hscc-cluster", "hscc-commands", "sparkrun-hermes"}


def test_missing_config_noop(tmp_path):
    assert enable_plugins.enable(str(tmp_path / "nope.yaml")) == []


def test_bad_plugins_shape_does_not_clobber(tmp_path):
    path = _write(tmp_path / "config.yaml", {"plugins": "weird-string"})
    assert enable_plugins.enable(path) == []
    assert yaml.safe_load(open(path))["plugins"] == "weird-string"
