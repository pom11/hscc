"""Tests for per-project orchestrators (resolver + idempotent generator).

These tests never touch the real ~/.hermes/profiles or ~/.flightdeck/registry.
The generator root is redirected to a tmp home via HERMES_HOME / PROFILES_DIR,
and the registry via a tmp fixture (path argument / HSCC_REGISTRY env).
"""
import os
import yaml
import pytest

import rolelib
import generator
import orchestrators


@pytest.fixture
def registry_file(tmp_path):
    """A minimal-but-real fixture registry with a few projects."""
    doc = {
        "projects": [
            {"name": "ecofire-app", "repo": "/tmp/ecofire", "board": "ecofire-app"},
            {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
            # A project intentionally WITHOUT a board (edge case for resolver)
            {"name": "noboard", "repo": "/tmp/noboard"},
        ],
        "ignored_topics": [140],
    }
    p = tmp_path / "registry.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return str(p)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point the generator at a tmp profile root (BOTH native + fallback paths)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    # orchestrators imports generator as a module-level reference; ensure it
    # picks up the monkeypatched PROFILES_DIR by using the same rolelib object.
    return str(tmp_path / "profiles")


# -- resolver --


def test_resolver_real_project(registry_file):
    got = orchestrators.resolve_orchestrator("hscc", path=registry_file)
    assert got == {
        "project": "hscc",
        "profile": "hscc-orch",
        "session": "hscc",
        "board": "hscc",
        "repo": "/tmp/hscc",
    }


def test_resolver_uses_project_board_not_name(registry_file):
    """repo and board come from the registry; session is the project name."""
    got = orchestrators.resolve_orchestrator("ecofire-app", path=registry_file)
    assert got["profile"] == "ecofire-app-orch"
    assert got["session"] == "ecofire-app"
    assert got["board"] == "ecofire-app"
    assert got["repo"] == "/tmp/ecofire"


def test_resolver_general_sentinel(registry_file):
    got = orchestrators.resolve_orchestrator("general", path=registry_file)
    assert got == {
        "project": "general",
        "profile": "general-orch",
        "session": "general",
        "board": "default",
        "repo": None,
    }


def test_resolver_general_is_default_when_none(registry_file):
    """No project specified -> the general orchestrator (repo=None)."""
    got = orchestrators.resolve_orchestrator(None, path=registry_file)
    assert got["profile"] == "general-orch"
    assert got["session"] == "general"
    assert got["board"] == "default"
    assert got["repo"] is None


def test_resolver_unknown_project_raises(registry_file):
    with pytest.raises(orchestrators.UnknownProjectError):
        orchestrators.resolve_orchestrator("bogus-project", path=registry_file)


def test_resolver_repo_none_for_general(registry_file):
    got = orchestrators.resolve_orchestrator(path=registry_file)
    assert got["repo"] is None


def test_resolver_default_registry_path_used_without_arg():
    """Without a path arg the resolver targets the real registry default — but
    we never read it here; this just proves the default resolves to the
    documented path (expanduser, no crash)."""
    resolved_path = orchestrators._registry_path(None)
    assert resolved_path == os.path.expanduser("~/.flightdeck/registry.yaml")


def test_resolver_missing_board_raises_clear_error(registry_file):
    with pytest.raises(orchestrators.OrchestratorError, match="no 'board'"):
        orchestrators.resolve_orchestrator("noboard", path=registry_file)


def test_resolver_env_registry_override(registry_file, monkeypatch):
    """HSCC_REGISTRY env var overrides the default registry path."""
    monkeypatch.setenv("HSCC_REGISTRY", registry_file)
    got = orchestrators.resolve_orchestrator("hscc")
    assert got["profile"] == "hscc-orch"


# -- generator (idempotent, empty memory, no worker repoint) --


def test_ensure_orchestrator_creates_profile_with_empty_memory(
    registry_file, isolated_home, monkeypatch
):
    got = orchestrators.ensure_orchestrator("hscc", base_identity="BASE-IDENTITY",
                                             path=registry_file)
    pdir = os.path.join(isolated_home, "hscc-orch")
    assert os.path.isdir(pdir)
    assert os.path.exists(os.path.join(pdir, "SOUL.md"))
    # One post-condition that matters most: the generated profile must carry an
    # EMPTY memory store (no inherited/cloned memory from a shared role).
    memories = os.path.join(pdir, "memories")
    if os.path.isdir(memories):
        entries = os.listdir(memories)
        # MEMORY.md / USER.md may be auto-created empty by the scaffold; the
        # point is no *contentful* inherited memory files like role memory.
        assert entries == []
    assert got["changed"] is True

    # No worker-proxy repoint: project orchestrator is gateway-node, like the
    # cluster-wide orchestrator → config.yaml has NO model block / compaction.
    with open(os.path.join(pdir, "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert "model" not in cfg
    assert "auxiliary" not in cfg
    # ...but it MUST carry the kanban + delegation toolsets an orchestrator needs.
    toolsets = cfg["toolsets"]
    assert "kanban" in toolsets
    assert "delegation" in toolsets


def test_ensure_orchestrator_idempotent(registry_file, isolated_home, monkeypatch):
    orchestrators.ensure_orchestrator("hscc", base_identity="BASE-ID",
                                       path=registry_file)
    got2 = orchestrators.ensure_orchestrator("hscc", base_identity="BASE-ID",
                                              path=registry_file)
    assert got2["changed"] is False  # second run changes nothing


def test_ensure_orchestrator_does_not_clobber_existing_memory(
    registry_file, isolated_home, monkeypatch
):
    """If the profile already exists WITH memory, re-generation must not wipe it."""
    pdir = os.path.join(isolated_home, "hscc-orch")
    os.makedirs(os.path.join(pdir, "memories"), exist_ok=True)
    with open(os.path.join(pdir, "memories", "MEMORY.md"), "w") as f:
        f.write("# project-memory-that-must-survive\n")

    orchestrators.ensure_orchestrator("hscc", base_identity="BASE-ID",
                                       path=registry_file)
    with open(os.path.join(pdir, "memories", "MEMORY.md")) as f:
        assert "project-memory-that-must-survive" in f.read()


def test_ensure_orchestrator_soul_states_project_repo_board(
    registry_file, isolated_home, monkeypatch
):
    orchestrators.ensure_orchestrator("hscc", base_identity="BASE-ID",
                                       path=registry_file)
    with open(os.path.join(isolated_home, "hscc-orch", "SOUL.md")) as f:
        soul = f.read()
    assert "hscc" in soul            # which project
    assert "/tmp/hscc" in soul       # repo path
    assert "hscc" in soul and "board" in soul
    # gateway-node ops, not worker worktree ops
    assert "your own git worktree" not in soul.lower()


def test_ensure_orchestrator_general_repo_none(registry_file, isolated_home):
    """general orchestrator produces a profile with no repo in its SOUL."""
    got = orchestrators.ensure_orchestrator(None, base_identity="BASE-ID",
                                             path=registry_file)
    assert got["repo"] is None
    assert got["profile"] == "general-orch"
    pdir = os.path.join(isolated_home, "general-orch")
    assert os.path.isdir(pdir)
    with open(os.path.join(pdir, "SOUL.md")) as f:
        soul = f.read()
    assert "general" in soul
    # No repo to state; the ops block must still mark it a project-less orchestrator
    assert "worktree" not in soul.lower()


# -- generator.compose_soul integration --


def test_compose_soul_project_orch_not_worker(tmp_path):
    spec = {"name": "ecofire-app-orch", "identity": "You orchestrate.\n",
            "preload_skills": []}
    soul = generator.compose_soul(spec, base_identity="BASE")
    assert "your own git worktree" not in soul.lower()
    assert "gateway node" in soul.lower()
    # project-orch must NOT hold authority over the physical cluster
    # (that phrase may appear only to NEGATE it — the SOP grants authority
    # over the project's board, not the cluster).
    assert "physical cluster" not in soul.lower() or \
        "do not hold authority over the physical cluster" in soul.lower()


def test_compose_soul_real_orchestrator_still_cluster_authority():
    spec = {"name": "orchestrator", "identity": "You orchestrate.\n",
            "preload_skills": []}
    soul = generator.compose_soul(spec, base_identity="BASE")
    assert "physical cluster" in soul.lower()
    assert "gateway node" in soul.lower()


# -- CLI (hscc.py orch) --


def test_cli_orch_creates_profile(registry_file, tmp_path, monkeypatch):
    """`hscc.py orch <project>` provisions the orchestrator into a tmp home."""
    import subprocess
    import sys
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HSCC_REGISTRY", registry_file)
    plugin_dir = os.path.dirname(os.path.abspath(generator.__file__))
    result = subprocess.run(
        [sys.executable, os.path.join(plugin_dir, "hscc.py"), "orch", "hscc"],
        capture_output=True, text=True, env=dict(os.environ), cwd=plugin_dir,
    )
    assert result.returncode == 0, result.stderr
    import json as _json
    out = _json.loads(result.stdout)
    assert out["profile"] == "hscc-orch"
    assert out["session"] == "hscc"
    assert out["board"] == "hscc"
    assert os.path.isdir(os.path.join(str(tmp_path), "profiles", "hscc-orch"))


def test_cli_orch_unknown_project(registry_file, tmp_path, monkeypatch):
    """`hscc.py orch <unknown>` fails cleanly (exit 1) without partial write."""
    import subprocess
    import sys
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HSCC_REGISTRY", registry_file)
    plugin_dir = os.path.dirname(os.path.abspath(generator.__file__))
    result = subprocess.run(
        [sys.executable, os.path.join(plugin_dir, "hscc.py"), "orch", "nope"],
        capture_output=True, text=True, env=dict(os.environ), cwd=plugin_dir,
    )
    assert result.returncode == 1
    assert "unknown project" in result.stdout
    # nothing rescued into the profile tree
    assert not os.path.exists(os.path.join(str(tmp_path), "profiles", "nope-orch"))
