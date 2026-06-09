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
    cfg = yaml.safe_load(open(os.path.join(pdir, "config.yaml")))
    assert "hscc-cluster" not in cfg["toolsets"]
    assert "hermes-cli" in cfg["toolsets"]
    prof = yaml.safe_load(open(os.path.join(pdir, "profile.yaml")))
    assert prof["description_auto"] is False
    assert changed is True  # first write reports changed


def test_generate_profile_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n", "preload_skills": []}
    generator.generate_profile(spec, base_identity="BASE")
    changed_second = generator.generate_profile(spec, base_identity="BASE")
    assert changed_second is False  # unchanged content → no rewrite
