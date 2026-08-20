"""Shared test fixtures for hscc-api.

Puts the plugin dir on sys.path so ``import api_server`` works when this dir's
tests are run in isolation by scripts/run_tests.sh (the plugin dir name is
hyphenated and not an importable package name).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import api_server  # noqa: E402


@pytest.fixture
def hscc_dir(tmp_path):
    """A fresh, isolated ~/.hscc stand-in for each test."""
    return str(tmp_path)
