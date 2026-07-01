import os
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


import yaml


def test_generate_profile_writes_files(tmp_path, monkeypatch):
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
