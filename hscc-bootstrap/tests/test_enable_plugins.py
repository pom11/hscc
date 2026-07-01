"""Tests for enable_plugins._ensure_multiplex — gateway multiplex config."""
import json
import os
import sys
import tempfile

import pytest

# Ensure the hscc-bootstrap package is importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from enable_plugins import _ensure_multiplex, enable


class TestEnsureMultiplex:
    """_ensure_multiplex enables gateway.multiplex_profiles idempotently."""

    def test_creates_multiplex_when_absent(self):
        cfg: dict = {}
        changed = _ensure_multiplex(cfg)
        assert changed == ["multiplex_profiles"]
        assert cfg["gateway"]["multiplex_profiles"] is True

    def test_does_not_clobber_operator_true(self):
        cfg = {"gateway": {"multiplex_profiles": True}}
        changed = _ensure_multiplex(cfg)
        assert changed == []
        assert cfg["gateway"]["multiplex_profiles"] is True

    def test_does_not_clobber_operator_false(self):
        cfg = {"gateway": {"multiplex_profiles": False}}
        changed = _ensure_multiplex(cfg)
        assert changed == []
        assert cfg["gateway"]["multiplex_profiles"] is False

    def test_does_not_clobber_string_value(self):
        cfg = {"gateway": {"multiplex_profiles": "true"}}
        changed = _ensure_multiplex(cfg)
        assert changed == []
        assert cfg["gateway"]["multiplex_profiles"] == "true"

    def test_gateway_non_dict_returns_empty(self):
        cfg = {"gateway": "not-a-dict"}
        changed = _ensure_multiplex(cfg)
        assert changed == []

    def test_preserves_other_gateway_keys(self):
        cfg = {"gateway": {"some_other_key": "value"}}
        changed = _ensure_multiplex(cfg)
        assert changed == ["multiplex_profiles"]
        assert cfg["gateway"]["some_other_key"] == "value"
        assert cfg["gateway"]["multiplex_profiles"] is True

    def test_idempotent_second_call(self):
        cfg: dict = {}
        _ensure_multiplex(cfg)
        changed = _ensure_multiplex(cfg)
        assert changed == []
        assert cfg["gateway"]["multiplex_profiles"] is True

    def test_return_empty_list_when_already_set(self):
        cfg = {"gateway": {"multiplex_profiles": True}}
        assert _ensure_multiplex(cfg) == []


class TestEnableIntegratesMultiplex:
    """enable() return dict includes 'multiplex' key."""

    def test_enable_return_has_multiplex_key(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("model:\n  default: test\n")
        result = enable(str(config_file))
        assert "multiplex" in result

    def test_enable_sets_multiplex_when_absent(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("{}\n")
        result = enable(str(config_file))
        assert result["multiplex"] == ["multiplex_profiles"]

    def test_enable_preserves_multiplex_when_set(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("gateway:\n  multiplex_profiles: false\n")
        result = enable(str(config_file))
        assert result["multiplex"] == []