import os
import yaml
import pytest
import rolelib
import generator


def test_compose_soul_has_all_three_layers():
    spec = {"name": "reviewer", "identity": "You review code.\n", "preload_skills": []}
    soul = generator.compose_soul(spec, base_identity="BASE-CHAR-MARKER")
    # Layer 1 base
    assert "BASE-CHAR-MARKER" in soul
    # Layer 2 role
    assert "You review code." in soul
    # Layer 3 operational (thin: mentions role + worktree/kanban)
    assert "reviewer" in soul
    assert "worktree" in soul.lower() or "kanban" in soul.lower()


def test_compose_soul_orchestrator_skips_worker_ops():
    spec = {"name": "orchestrator", "identity": "You orchestrate.\n", "preload_skills": []}
    soul = generator.compose_soul(spec, base_identity="BASE")
    # Orchestrator is not a kanban worker — must NOT claim to run in a worktree.
    assert "your own git worktree" not in soul.lower()
    # ...but it MUST get its own gateway/authority operational block.
    assert "gateway node" in soul.lower()


def test_generate_profile_writes_files(tmp_path, monkeypatch):
    # PROFILES_DIR covers the fallback path; HERMES_HOME covers the native API
    # path (create_profile reads HERMES_HOME, not PROFILES_DIR), so the test is
    # isolated in BOTH modes and never touches the real ~/.hermes/profiles.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": ["test-driven-development"]}
    changed = generator.generate_profile(spec, base_identity="BASE")
    pdir = os.path.join(str(tmp_path / "profiles"), "coder")
    assert os.path.isdir(pdir)
    assert os.path.exists(os.path.join(pdir, "SOUL.md"))
    with open(os.path.join(pdir, "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert "hscc-cluster" not in cfg["toolsets"]
    assert "hermes-cli" in cfg["toolsets"]
    with open(os.path.join(pdir, "profile.yaml")) as f:
        prof = yaml.safe_load(f)
    assert prof["description_auto"] is False
    assert changed is True  # first write reports changed


def test_generate_profile_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n", "preload_skills": []}
    generator.generate_profile(spec, base_identity="BASE")
    changed_second = generator.generate_profile(spec, base_identity="BASE")
    assert changed_second is False  # unchanged content → no rewrite


import subprocess
import sys


def test_cli_generate_all_runs(tmp_path):
    """End-to-end: `hscc.py generate` builds all 24 role profiles into a temp home."""
    env = dict(os.environ)
    env["HERMES_HOME"] = str(tmp_path)
    plugin_dir = os.path.dirname(os.path.abspath(generator.__file__))
    py = sys.executable  # use system python, works in sandboxed tests
    result = subprocess.run(
        [py, os.path.join(plugin_dir, "hscc.py"), "generate"],
        capture_output=True, text=True, env=env, cwd=plugin_dir,
    )
    assert result.returncode == 0, result.stderr
    for role in ("orchestrator", "architect", "coder", "reviewer", "qa"):
        assert os.path.exists(os.path.join(str(tmp_path), "profiles", role, "SOUL.md"))
    # Verify routing_description landed as description in profile.yaml
    import yaml
    coder_profile = os.path.join(str(tmp_path), "profiles", "coder", "profile.yaml")
    with open(coder_profile) as f:
        pdata = yaml.safe_load(f)
    assert "routing_description" in pdata or "description" in pdata
    # The decomposer-facing description IS the routing_description
    assert "Claim tasks" in str(pdata.get("description", ""))


# -- model_tier generator tests --


def test_model_tier_fast_uses_worker_proxy(tmp_path, monkeypatch):
    """Fast-tier (default) roles use the worker proxy endpoint."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "coder"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["base_url"] == "http://localhost:4000/v1"
    assert cfg["model"]["default"] == "worker-model"


def test_model_tier_strong_uses_orch_endpoint(tmp_path, monkeypatch):
    """Strong-tier roles use the orchestrator endpoint."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "architect", "identity": "You design.\n",
            "preload_skills": [], "model_tier": "strong"}
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "architect"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["base_url"] == "http://10.0.0.244:8000/v1"
    assert cfg["model"]["default"] == "orchestrator-model"


def test_model_tier_override_via_env(tmp_path, monkeypatch):
    """HSCC_STRONG_URL env var overrides the strong-tier endpoint."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.setattr(generator, "STRONG_URL", "http://custom:9999/v1")
    monkeypatch.setattr(generator, "STRONG_MODEL", "custom/model")
    spec = {"name": "designer", "identity": "You design.\n",
            "preload_skills": [], "model_tier": "strong"}
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "designer"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["base_url"] == "http://custom:9999/v1"
    assert cfg["model"]["default"] == "custom/model"


def test_model_tier_strong_idempotent(tmp_path, monkeypatch):
    """Generating a strong-tier role twice reports changed=False on second run."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "architect", "identity": "You design.\n",
            "preload_skills": [], "model_tier": "strong"}
    generator.generate_profile(spec, base_identity="BASE")
    changed_second = generator.generate_profile(spec, base_identity="BASE")
    assert changed_second is False


def test_model_tier_strong_still_has_compaction(tmp_path, monkeypatch):
    """Strong-tier roles still route compaction to the orchestrator."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "architect", "identity": "You design.\n",
            "preload_skills": [], "model_tier": "strong"}
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "architect"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    # Compaction auxiliary should still be present
    assert "auxiliary" in cfg
    assert "compression" in cfg["auxiliary"]


def test_short_desc_identity_no_double_period():
    """_short_desc_identity should not produce '..' when identity sentence already ends with '.'."""
    spec = {"name": "reviewer", "identity": "You review code.\n", "preload_skills": []}
    desc = generator._short_desc_identity(spec)
    assert not desc.endswith(".."), f"got: {desc!r}"
    assert desc == "You review code."


def test_write_if_changed_handles_non_utf8(tmp_path):
    """_write_if_changed should overwrite a non-UTF-8 file without raising."""
    path = tmp_path / "test.txt"
    path.write_bytes(b"\xff\xfe garbage binary \xff")
    content = "valid utf-8 content"
    changed = generator._write_if_changed(str(path), content)
    assert changed is True
    assert path.read_text() == content


# -- model_endpoint / model_name generator tests --


def test_model_endpoint_and_name_in_config(tmp_path, monkeypatch):
    """a) spec with model_endpoint+model_name -> config.yaml uses that base_url + model."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast",
            "model_endpoint": "http://coding:5000/v1",
            "model_name": "Qwen/Code-32B"}
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "coder"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["base_url"] == "http://coding:5000/v1"
    assert cfg["model"]["default"] == "Qwen/Code-32B"


def test_model_endpoint_only_falls_back_to_tier_model(tmp_path, monkeypatch):
    """b) spec with model_endpoint only -> uses that base_url, model falls back to tier default."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast",
            "model_endpoint": "http://coding:5000/v1"}
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "coder"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["base_url"] == "http://coding:5000/v1"
    assert cfg["model"]["default"] == "worker-model"


def test_model_endpoint_only_strong_falls_back_to_strong_model(tmp_path, monkeypatch):
    """b) spec with model_endpoint only on strong tier -> falls back to strong model."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "architect", "identity": "You design.\n",
            "preload_skills": [], "model_tier": "strong",
            "model_endpoint": "http://arch:6000/v1"}
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "architect"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["base_url"] == "http://arch:6000/v1"
    assert cfg["model"]["default"] == "orchestrator-model"


def test_no_override_uses_tier_logic(tmp_path, monkeypatch):
    """c) spec WITHOUT override -> identical to current tier behaviour."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "coder"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["base_url"] == "http://localhost:4000/v1"
    assert cfg["model"]["default"] == "worker-model"


def test_model_endpoint_idempotent(tmp_path, monkeypatch):
    """e) generation stays idempotent with an override present."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast",
            "model_endpoint": "http://coding:5000/v1",
            "model_name": "Qwen/Code-32B"}
    generator.generate_profile(spec, base_identity="BASE")
    changed_second = generator.generate_profile(spec, base_identity="BASE")
    assert changed_second is False


def test_model_endpoint_still_has_compaction(tmp_path, monkeypatch):
    """model_endpoint override does not remove compaction config."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast",
            "model_endpoint": "http://coding:5000/v1",
            "model_name": "Qwen/Code-32B"}
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "coder"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert "auxiliary" in cfg
    assert "compression" in cfg["auxiliary"]


# -- logical-alias model defaults (v1.5.1) --
#
# Constants above evaluate at import time, so these tests reload the module
# under a controlled HSCC_*_MODEL environment to assert both the alias defaults
# and the env-override path. Each reload test restores clean alias defaults at
# the end so the rest of the suite stays deterministic.
import importlib


_MODEL_ENV_VARS = ("HSCC_WORKER_MODEL", "HSCC_STRONG_MODEL", "HSCC_COMPACT_MODEL")


def _clear_model_env(monkeypatch):
    """Drop all model-alias env vars so constants evaluate to their defaults."""
    for var in _MODEL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _reload_with_model_env(monkeypatch, overrides):
    """Set env overrides (monkeypatched), reload generator so constants recompute."""
    for var, val in overrides.items():
        monkeypatch.setenv(var, val)
    return importlib.reload(generator)


def _restore_model_defaults(monkeypatch):
    """Clear env overrides and reload so the module is back to alias defaults."""
    _clear_model_env(monkeypatch)
    importlib.reload(generator)


def test_model_defaults_are_aliases_without_env(monkeypatch):
    """With no HSCC_*_MODEL env set, every model default is a logical alias."""
    _clear_model_env(monkeypatch)
    mod = importlib.reload(generator)
    assert mod.WORKER_MODEL == "worker-model"
    assert mod.STRONG_MODEL == "orchestrator-model"
    assert mod.COMPACT_MODEL == "orchestrator-model"


def test_worker_profile_model_and_compaction_are_aliases(tmp_path, monkeypatch):
    """A generated worker (fast) profile uses the worker-model alias for its
    model block and the orchestrator-model alias for compaction."""
    _clear_model_env(monkeypatch)
    mod = importlib.reload(generator)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(mod.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    mod.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "coder"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["default"] == mod.WORKER_MODEL == "worker-model"
    assert cfg["auxiliary"]["compression"]["model"] == \
        mod.COMPACT_MODEL == "orchestrator-model"


def test_strong_profile_model_is_alias(tmp_path, monkeypatch):
    """A generated strong-tier profile uses the orchestrator-model alias."""
    _clear_model_env(monkeypatch)
    mod = importlib.reload(generator)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(mod.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "architect", "identity": "You design.\n",
            "preload_skills": [], "model_tier": "strong"}
    mod.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "architect"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["default"] == mod.STRONG_MODEL == "orchestrator-model"


def test_env_override_strong_model_forces_concrete(monkeypatch):
    """HSCC_STRONG_MODEL env override forces a concrete id; worker alias stays."""
    mod = _reload_with_model_env(
        monkeypatch, {"HSCC_STRONG_MODEL": "deepseek-ai/DeepSeek-V4-Flash-0731"})
    assert mod.STRONG_MODEL == "deepseek-ai/DeepSeek-V4-Flash-0731"
    assert mod.WORKER_MODEL == "worker-model"  # untouched remains alias
    _restore_model_defaults(monkeypatch)


def test_env_override_worker_model_forces_concrete(monkeypatch):
    """HSCC_WORKER_MODEL env override forces a concrete id; strong alias stays."""
    mod = _reload_with_model_env(
        monkeypatch, {"HSCC_WORKER_MODEL": "Qwen/Qwen3.6-27B-FP8"})
    assert mod.WORKER_MODEL == "Qwen/Qwen3.6-27B-FP8"
    assert mod.STRONG_MODEL == "orchestrator-model"  # untouched remains alias
    _restore_model_defaults(monkeypatch)
