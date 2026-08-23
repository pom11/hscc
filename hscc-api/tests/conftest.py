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


@pytest.fixture(autouse=True)
def _isolate_hscc(tmp_path, monkeypatch):
    """Redirect hscc-api's ~/.hscc write path to a per-test tmp dir.

    Belt-and-braces, mirroring hscc_daemon/tests/conftest.py::_isolate_hscc.
    An authenticated request stamps autodown activity via
    ``state.write_state(\"activity\", ...)`` (api_server._do_stamp_http_activity),
    which writes to ``~/.hscc/state/activity.json`` through ``state.STATE_DIR``.
    If an api test ever exercises an authenticated request without pinning
    hscc_dir, it would write the activity file into the operator's real
    ~/.hscc. This autouse fixture makes that impossible by redirecting
    ``state.STATE_DIR`` off the live home dir for every test.
    """
    base = str(tmp_path / "hscc")
    # Do NOT eagerly mkdir — a test might create the same dir without
    # exist_ok=True; writes that need a parent create it themselves.
    from hscc_daemon import state
    monkeypatch.setattr(state, "STATE_DIR", os.path.join(base, "state"),
                        raising=False)
