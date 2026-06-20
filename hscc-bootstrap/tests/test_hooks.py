"""Tests for cluster-guard hook installation via enable_plugins."""
import json
import os
import stat
from pathlib import Path

import yaml

import enable_plugins


def _write(p, data):
    p.write_text(yaml.safe_dump(data))
    return str(p)


def _minimal_cfg():
    """Config with plugins + toolsets already wired so enable() only touches hooks."""
    return {
        "plugins": {"enabled": ["hscc-cluster", "hscc-commands", "sparkrun-hermes"]},
        "toolsets": ["hermes-cli", "hscc-cluster", "sparkrun", "delegation"],
    }


# ── _ensure_hooks_file ──────────────────────────────────────────────────────

def test_hook_file_installed_from_source(tmp_path, monkeypatch):
    """cluster-guard.py is copied from hooks_source to ~/.hermes/hooks/."""
    hooks_src = tmp_path / "hooks_src"
    hooks_src.mkdir()
    (hooks_src / "cluster-guard.py").write_text("# hook script\n")

    hooks_dst = tmp_path / "hooks_dst"
    monkeypatch.setattr(enable_plugins, "HOOKS_DIR", str(hooks_dst))
    monkeypatch.setattr(enable_plugins, "CLUSTER_GUARD_DST",
                       str(hooks_dst / "cluster-guard.py"))

    res = enable_plugins._ensure_hooks_file(str(hooks_src))
    assert res["installed"] is True
    assert (hooks_dst / "cluster-guard.py").is_file()
    assert (hooks_dst / "cluster-guard.py").read_text() == "# hook script\n"


def test_hook_file_backup_existing(tmp_path, monkeypatch):
    """Existing cluster-guard.py is backed up before overwrite."""
    hooks_src = tmp_path / "hooks_src"
    hooks_src.mkdir()
    (hooks_src / "cluster-guard.py").write_text("# new version\n")

    hooks_dst = tmp_path / "hooks_dst"
    hooks_dst.mkdir()
    (hooks_dst / "cluster-guard.py").write_text("# old version\n")

    monkeypatch.setattr(enable_plugins, "HOOKS_DIR", str(hooks_dst))
    monkeypatch.setattr(enable_plugins, "CLUSTER_GUARD_DST",
                       str(hooks_dst / "cluster-guard.py"))

    res = enable_plugins._ensure_hooks_file(str(hooks_src))
    assert res["installed"] is True
    assert res["backed_up"] is not None
    assert "bak-" in res["backed_up"]
    assert (hooks_dst / "cluster-guard.py").read_text() == "# new version\n"
    # Backup exists
    backup = Path(res["backed_up"])
    assert backup.is_file()
    assert backup.read_text() == "# old version\n"


def test_hook_file_missing_source(tmp_path, monkeypatch):
    """No crash when source cluster-guard.py doesn't exist."""
    hooks_src = tmp_path / "hooks_src"
    hooks_src.mkdir()  # dir exists but no cluster-guard.py

    hooks_dst = tmp_path / "hooks_dst"
    monkeypatch.setattr(enable_plugins, "HOOKS_DIR", str(hooks_dst))
    monkeypatch.setattr(enable_plugins, "CLUSTER_GUARD_DST",
                       str(hooks_dst / "cluster-guard.py"))

    res = enable_plugins._ensure_hooks_file(str(hooks_src))
    assert res["installed"] is False
    assert "not found" in res.get("reason", "")


def test_hook_file_sets_executable(tmp_path, monkeypatch):
    """Installed hook file gets 0o755 permissions."""
    hooks_src = tmp_path / "hooks_src"
    hooks_src.mkdir()
    (hooks_src / "cluster-guard.py").write_text("# hook\n")

    hooks_dst = tmp_path / "hooks_dst"
    monkeypatch.setattr(enable_plugins, "HOOKS_DIR", str(hooks_dst))
    monkeypatch.setattr(enable_plugins, "CLUSTER_GUARD_DST",
                       str(hooks_dst / "cluster-guard.py"))

    enable_plugins._ensure_hooks_file(str(hooks_src))
    mode = (hooks_dst / "cluster-guard.py").stat().st_mode
    assert mode & stat.S_IXUSR          # user execute
    assert mode & stat.S_IXGRP          # group execute
    assert mode & stat.S_IXOTH          # other execute


# ── _ensure_hooks (config wiring) ──────────────────────────────────────────

def test_hooks_wired_on_fresh_config(tmp_path):
    """All three hook types are added when hooks section is absent."""
    cfg = _minimal_cfg()
    changed = enable_plugins._ensure_hooks(cfg)
    assert set(changed) == {"pre_tool_call", "post_tool_call", "on_session_start"}
    hooks = cfg["hooks"]
    # pre_tool_call
    assert len(hooks["pre_tool_call"]) == 1
    pre = hooks["pre_tool_call"][0]
    assert pre["matcher"] == "hscc-cluster"
    assert pre["command"].endswith("cluster-guard.py")
    assert pre["timeout"] == 10
    # post_tool_call
    assert len(hooks["post_tool_call"]) == 1
    post = hooks["post_tool_call"][0]
    assert post["matcher"] == "hscc-cluster"
    assert post["timeout"] == 5
    # on_session_start
    assert len(hooks["on_session_start"]) == 1
    sess = hooks["on_session_start"][0]
    assert "command" in sess
    assert sess["command"].endswith("cluster-guard.py")
    assert sess["timeout"] == 5


def test_hooks_idempotent_when_already_wired(tmp_path):
    """Re-running _ensure_hooks does not duplicate entries."""
    cfg = _minimal_cfg()
    enable_plugins._ensure_hooks(cfg)
    first_changed = enable_plugins._ensure_hooks(cfg)
    assert first_changed == []
    # Still only one entry per hook type
    assert len(cfg["hooks"]["pre_tool_call"]) == 1
    assert len(cfg["hooks"]["post_tool_call"]) == 1
    assert len(cfg["hooks"]["on_session_start"]) == 1


def test_hooks_preserves_existing_hooks(tmp_path):
    """Operator-added hooks are not removed; cluster-guard is appended."""
    cfg = _minimal_cfg()
    cfg["hooks"] = {
        "pre_tool_call": [{"matcher": "other-toolset", "command": "echo hi"}],
        "post_tool_call": [],
        "on_session_start": [],
    }
    changed = enable_plugins._ensure_hooks(cfg)
    assert "pre_tool_call" in changed
    # Original hook preserved, cluster-guard appended
    assert len(cfg["hooks"]["pre_tool_call"]) == 2
    assert cfg["hooks"]["pre_tool_call"][0]["matcher"] == "other-toolset"
    assert cfg["hooks"]["pre_tool_call"][1]["matcher"] == "hscc-cluster"


def test_hooks_skips_non_dict_hooks_config(tmp_path):
    """Bad hooks shape (string) does not crash; returns []."""
    cfg = _minimal_cfg()
    cfg["hooks"] = "bad"
    changed = enable_plugins._ensure_hooks(cfg)
    assert changed == []


# ── enable() integration ────────────────────────────────────────────────────

def test_enable_returns_hooks_key(tmp_path):
    """enable() return dict includes 'hooks' key."""
    path = _write(tmp_path / "config.yaml", _minimal_cfg())
    res = enable_plugins.enable(path, hooks_source=str(tmp_path / "no-hooks"))
    assert "hooks" in res


def test_enable_hooks_noop_when_wired(tmp_path):
    """enable() does not re-add hooks when they're already wired."""
    cfg = _minimal_cfg()
    cfg["hooks"] = {
        "pre_tool_call": [{"matcher": "hscc-cluster",
                           "command": "/fake/cluster-guard.py",
                           "timeout": 10}],
        "post_tool_call": [{"matcher": "hscc-cluster",
                            "command": "/fake/cluster-guard.py",
                            "timeout": 5}],
        "on_session_start": [{"command": "/fake/cluster-guard.py", "timeout": 5}],
    }
    path = _write(tmp_path / "config.yaml", cfg)
    res = enable_plugins.enable(path, hooks_source=str(tmp_path / "no-hooks"))
    assert res["hooks"] == []


def test_enable_hook_file_installed(tmp_path, monkeypatch):
    """enable() installs the hook file to disk alongside config wiring."""
    hooks_src = tmp_path / "hooks_src"
    hooks_src.mkdir()
    (hooks_src / "cluster-guard.py").write_text("# from repo\n")

    hooks_dst = tmp_path / "hooks_dst"
    monkeypatch.setattr(enable_plugins, "HOOKS_DIR", str(hooks_dst))
    monkeypatch.setattr(enable_plugins, "CLUSTER_GUARD_DST",
                       str(hooks_dst / "cluster-guard.py"))

    path = _write(tmp_path / "config.yaml", _minimal_cfg())
    enable_plugins.enable(path, hooks_source=str(hooks_src))

    assert (hooks_dst / "cluster-guard.py").is_file()
    assert (hooks_dst / "cluster-guard.py").read_text() == "# from repo\n"