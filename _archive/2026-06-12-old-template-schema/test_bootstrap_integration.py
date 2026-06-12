"""Tests for bootstrap_integration module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, mock_open

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(PLUGIN_DIR))

from bootstrap_integration import (
    should_apply_template,
    bootstrap_default_template,
)


class TestShouldApplyTemplate:
    """Test bootstrap trigger logic."""

    def test_returns_true_when_no_serving_json(self):
        with patch.object(Path, "exists", return_value=False):
            result = should_apply_template()
            assert result is True

    def test_returns_true_when_few_units(self):
        mock_data = {"units": []}
        with patch.object(Path, "exists", return_value=True):
            with patch("builtins.open", mock_open(read_data=json.dumps(mock_data))):
                result = should_apply_template()
                assert result is True

    def test_returns_false_when_enough_units(self):
        mock_data = {"units": [{"id": "orch"}, {"id": "worker"}]}
        with patch.object(Path, "exists", return_value=True):
            with patch("builtins.open", mock_open(read_data=json.dumps(mock_data))):
                result = should_apply_template()
                assert result is False

    def test_handles_corrupt_json(self):
        with patch.object(Path, "exists", return_value=True):
            with patch("builtins.open", mock_open(read_data="not json")):
                result = should_apply_template()
                assert result is True  # Falls back to True on error


class TestBootstrapDefaultTemplate:
    """Test template name detection from cluster config."""

    def test_returns_basic_1_node_when_no_cluster_json(self):
        with patch.object(Path, "exists", return_value=False):
            result = bootstrap_default_template()
            assert result == "basic-1-node"

    def test_returns_basic_2_node_with_1_worker(self):
        mock_data = {"workers": [{"ip": "192.168.88.246"}]}
        with patch.object(Path, "exists", return_value=True):
            with patch("builtins.open", mock_open(read_data=json.dumps(mock_data))):
                result = bootstrap_default_template()
                assert result == "basic-2-node"

    def test_returns_basic_3_node_with_2_workers(self):
        mock_data = {
            "workers": [
                {"ip": "192.168.88.246"},
                {"ip": "192.168.88.247"},
            ]
        }
        with patch.object(Path, "exists", return_value=True):
            with patch("builtins.open", mock_open(read_data=json.dumps(mock_data))):
                result = bootstrap_default_template()
                assert result == "basic-3-node"

    def test_returns_basic_4_node_with_3_workers(self):
        mock_data = {
            "workers": [
                {"ip": "192.168.88.246"},
                {"ip": "192.168.88.247"},
                {"ip": "192.168.88.248"},
            ]
        }
        with patch.object(Path, "exists", return_value=True):
            with patch("builtins.open", mock_open(read_data=json.dumps(mock_data))):
                result = bootstrap_default_template()
                assert result == "basic-4-node"

    def test_returns_basic_4_node_with_many_workers(self):
        mock_data = {
            "workers": [
                {"ip": f"192.168.88.{i}"} for i in range(246, 252)
            ]  # 6 workers
        }
        with patch.object(Path, "exists", return_value=True):
            with patch("builtins.open", mock_open(read_data=json.dumps(mock_data))):
                result = bootstrap_default_template()
                assert result == "basic-4-node"  # Capped at 4

    def test_returns_basic_1_node_on_corrupt_json(self):
        with patch.object(Path, "exists", return_value=True):
            with patch("builtins.open", mock_open(read_data="not json")):
                result = bootstrap_default_template()
                assert result == "basic-1-node"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
