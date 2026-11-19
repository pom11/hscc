import os
import sys

# Make the plugin modules importable as top-level (mirrors hscc-cluster tests).
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


import pytest


@pytest.fixture(autouse=True)
def _no_real_reasoning_policy(tmp_path, monkeypatch):
    """Never read the machine's applied reasoning policy from a test: the
    generator would otherwise emit agent/delegation blocks depending on what
    the last real `hscc template apply` recorded."""
    import rolelib
    monkeypatch.setattr(rolelib, "REASONING_POLICY_PATH",
                        str(tmp_path / "no-reasoning-policy.json"))


@pytest.fixture(autouse=True)
def _isolated_main_orchestrator_state(tmp_path, monkeypatch):
    """Never read the machine's stored main-orchestrator name from a test.

    The resolver falls back to ``general`` when nothing is stored — deterministic
    only when ``HSCC_DIR`` points at a throwaway root, never the operator's
    ``~/.hscc``.
    """
    monkeypatch.setenv("HSCC_DIR", str(tmp_path / "hscc-state"))
