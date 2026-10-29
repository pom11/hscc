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
