"""GAP-F-8: the verify plugins row must assert DEPLOYED cluster tools.

Audit F GAP-F-8: ``check_plugins`` printed ✓ while the deployed
``~/.hermes/plugins/hscc-cluster/`` lacked ``orch_message.py`` entirely — the
row grepped only hscc-commands for three strings and asserted nothing about
the tool registry the plugin actually serves. The row now unions the repo-side
``hscc-cluster/__init__.py`` registry tables (the source of truth until the
plugin.yaml manifest reconciliation lands — t_19dbb9de) with the plugin.yaml
``provides_tools`` manifest, and requires every declared tool AND its handler
module file to exist in the DEPLOYED tree. Missing entries are named verbatim
under the explicit category "missing vs deployed payload" — a pending-release
gap (GAP-A-5, closes with the 2.15.0 bootstrap) is reported, never silently
✓, and never conflated with a different failure class.

Every fixture is a tmp tree — present vs absent — and the check only ever
reads files.
"""
import json
import os

import pytest


# A minimal repo-side registry shaped exactly like hscc-cluster/__init__.py:
# five-tuple tables whose first element is the tool name and whose third is
# ``module.handler``. orch_message is the tool the deployed tree lacks today.
REPO_INIT = '''
"""hscc-cluster toolset. Entry: register(ctx)."""
from . import ops
from . import orch_message

_READ_TOOLS = [
    ("cluster_status", S.CLUSTER_STATUS_SCHEMA, ops.cluster_status, "x", "d"),
]
_OPS_TOOLS = [
    ("provision_model", S.PROVISION_MODEL_SCHEMA, ops.provision_model, "x", "d"),
]
_ORCH_TOOLS = [
    ("orch_message", orch_message.ORCH_MESSAGE_SCHEMA,
     orch_message.orch_message, "x", "d"),
]

def register(ctx):
    for name, schema, handler, emoji, desc in (_READ_TOOLS + _OPS_TOOLS
                                               + _ORCH_TOOLS):
        ctx.register_tool(name=name, toolset="hscc-cluster", schema=schema)
'''

# The 2.14.0 deployed shape: same tables MINUS _ORCH_TOOLS and minus the
# orch_message import (the real deployed __init__.py has 6 tables, no
# _ORCH_TOOLS; orch_message.py is absent from the payload dir).
DEPLOYED_INIT_NO_ORCH = '''
"""hscc-cluster toolset. Entry: register(ctx)."""
from . import ops

_READ_TOOLS = [
    ("cluster_status", S.CLUSTER_STATUS_SCHEMA, ops.cluster_status, "x", "d"),
]
_OPS_TOOLS = [
    ("provision_model", S.PROVISION_MODEL_SCHEMA, ops.provision_model, "x", "d"),
]

def register(ctx):
    for name, schema, handler, emoji, desc in (_READ_TOOLS + _OPS_TOOLS):
        ctx.register_tool(name=name, toolset="hscc-cluster", schema=schema)
'''

DEPLOYED_INIT_FULL = REPO_INIT

CORE_COMMANDS = ('register("workers-up")\n'
                 'register("cluster-restart")\n'
                 'register("template")\n')


def _plant_core(plugins):
    core = plugins / "hscc-commands"
    core.mkdir(parents=True)
    (core / "__init__.py").write_text(CORE_COMMANDS)


def _plant_repo(root, init=REPO_INIT, manifest=True):
    repo = root / "repo"
    cl = repo / "hscc-cluster"
    cl.mkdir(parents=True)
    (cl / "__init__.py").write_text(init)
    (cl / "ops.py").write_text("def cluster_status(): ...\n")
    (cl / "orch_message.py").write_text("def orch_message(): ...\n")
    if manifest:
        (cl / "plugin.yaml").write_text(
            "name: hscc-cluster\nprovides_tools:\n  - cluster_status\n"
            "  - provision_model\n  - orch_message\n")
    return repo


def _plant_deployed(plugins, init, *, with_orch_module=True,
                    manifest=True):
    _plant_core(plugins)
    cl = plugins / "hscc-cluster"
    cl.mkdir(parents=True)
    (cl / "__init__.py").write_text(init)
    (cl / "ops.py").write_text("def cluster_status(): ...\n")
    if with_orch_module:
        (cl / "orch_message.py").write_text("def orch_message(): ...\n")
    if manifest:
        (cl / "plugin.yaml").write_text(
            "name: hscc-cluster\nprovides_tools:\n  - cluster_status\n"
            "  - provision_model\n")
    return cl


def _plugins_row(result):
    return next(c for c in result["checks"] if c["name"] == "plugins")


class TestCheckPluginsClusterTools:
    """The GAP-F-8 acceptance pair: absent -> named RED, present -> green."""

    def test_missing_orch_message_named_and_red(self, tmp_path):
        """GAP-F-8 shape: deployed tree lacks orch_message entirely -> the row
        goes ok=False and names BOTH the tool and the module file under the
        explicit 'missing vs deployed payload' category."""
        from hscc_daemon.verify import check_plugins
        repo = _plant_repo(tmp_path)
        _plant_deployed(tmp_path / "plugins", DEPLOYED_INIT_NO_ORCH,
                        with_orch_module=False)
        r = check_plugins(plugins_dir=str(tmp_path / "plugins"),
                          repo_root=str(repo))
        assert r["ok"] is False
        d = r["detail"]
        # Explicit category so a KNOWN-pending-release gap reads as exactly
        # that and nothing else.
        assert "missing vs deployed payload" in d
        # Named by name — both the tool and the file the card demands.
        assert "orch_message" in d
        assert "orch_message.py" in d

    def test_all_tools_deployed_stays_green(self, tmp_path):
        from hscc_daemon.verify import check_plugins
        repo = _plant_repo(tmp_path)
        _plant_deployed(tmp_path / "plugins", DEPLOYED_INIT_FULL)
        r = check_plugins(plugins_dir=str(tmp_path / "plugins"),
                          repo_root=str(repo))
        assert r["ok"] is True
        # Core-commands wording is preserved (existing consumers grep it).
        assert "all core commands found" in r["detail"]
        assert "missing vs deployed payload" not in r["detail"]

    def test_registered_but_module_file_absent_is_named(self, tmp_path):
        """A deployed __init__ that registers orch_message while the module
        file was dropped from the payload is the same class of un-deployed
        drift and must be named by file."""
        from hscc_daemon.verify import check_plugins
        repo = _plant_repo(tmp_path)
        _plant_deployed(tmp_path / "plugins", DEPLOYED_INIT_FULL,
                        with_orch_module=False)
        r = check_plugins(plugins_dir=str(tmp_path / "plugins"),
                          repo_root=str(repo))
        assert r["ok"] is False
        assert "missing vs deployed payload" in r["detail"]
        assert "orch_message.py" in r["detail"]

    def test_manifest_only_tool_is_expected(self, tmp_path):
        """provides_tools entries the registry tables do not carry are still
        demanded (manifest union — the post-t_19dbb9de case): a tool named by
        the repo manifest with no deployed counterpart is RED."""
        from hscc_daemon.verify import check_plugins
        repo = _plant_repo(tmp_path)
        (repo / "hscc-cluster" / "plugin.yaml").write_text(
            "name: hscc-cluster\nprovides_tools:\n  - cluster_status\n"
            "  - provision_model\n  - orch_message\n  - fleet_stats\n")
        # Deployed side is full for the registry tools and even declares the
        # module file exists — but neither registers nor ships fleet_stats.
        cl = _plant_deployed(tmp_path / "plugins", DEPLOYED_INIT_FULL)
        assert not (cl / "fleet_stats.py").exists()
        r = check_plugins(plugins_dir=str(tmp_path / "plugins"),
                          repo_root=str(repo))
        assert r["ok"] is False
        assert "fleet_stats" in r["detail"]
        assert "missing vs deployed payload" in r["detail"]

    def test_registry_beats_underdeclared_manifest(self, tmp_path):
        """Registry is the source of truth: the repo manifest under-declares
        (today's 13-name reality, GAP-A-7) yet the registry still demands
        orch_message. The check must not inherit the manifest's hole."""
        from hscc_daemon.verify import check_plugins
        repo = _plant_repo(tmp_path, manifest=False)
        (repo / "hscc-cluster" / "plugin.yaml").write_text(
            "name: hscc-cluster\nprovides_tools:\n  - cluster_status\n")
        _plant_deployed(tmp_path / "plugins", DEPLOYED_INIT_NO_ORCH,
                        with_orch_module=False)
        r = check_plugins(plugins_dir=str(tmp_path / "plugins"),
                          repo_root=str(repo))
        assert r["ok"] is False
        assert "orch_message" in r["detail"]


class TestCheckPluginsClusterToolsNeverAssertsFalsely:
    """Unassertive cases keep the row exactly as it was — the new assertion
    can only ADD signal, never fabricate a fail or a pass."""

    def test_no_repo_cluster_dir_is_unassertive(self, tmp_path):
        from hscc_daemon.verify import check_plugins
        _plant_core(tmp_path / "plugins")
        _plant_deployed(tmp_path / "plugins", DEPLOYED_INIT_NO_ORCH,
                        with_orch_module=False)
        # repo tree exists but has no hscc-cluster dir (hermetic test roots).
        (tmp_path / "repo").mkdir()
        r = check_plugins(plugins_dir=str(tmp_path / "plugins"),
                          repo_root=str(tmp_path / "repo"))
        assert r["ok"] is True
        assert "all core commands found" in r["detail"]

    def test_no_deployed_cluster_dir_is_unassertive(self, tmp_path):
        from hscc_daemon.verify import check_plugins
        repo = _plant_repo(tmp_path)
        _plant_core(tmp_path / "plugins")  # hscc-cluster NOT deployed
        r = check_plugins(plugins_dir=str(tmp_path / "plugins"),
                          repo_root=str(repo))
        assert r["ok"] is True
        assert "all core commands found" in r["detail"]

    def test_in_place_same_tree_is_unassertive(self, tmp_path):
        """repo_root == plugins_dir compares a tree with itself — vacuous.
        The row must stay on its core-commands verdict, not print a fake ✓
        for the cluster assertion."""
        from hscc_daemon.verify import check_plugins
        same = tmp_path / "tree"
        _plant_deployed(same, DEPLOYED_INIT_NO_ORCH, with_orch_module=False)
        # repo-side registry inside the SAME tree would demand orch_message;
        # a self-comparison must not be reported as either pass or drift.
        (same / "hscc-cluster" / "plugin.yaml").write_text(
            "name: hscc-cluster\nprovides_tools:\n  - orch_message\n")
        r = check_plugins(plugins_dir=str(same), repo_root=str(same))
        assert r["ok"] is True
        assert "orch_message" not in r["detail"]

    def test_unreadable_deployed_init_is_unassertive(self, tmp_path):
        from hscc_daemon.verify import check_plugins
        repo = _plant_repo(tmp_path)
        cl = _plant_deployed(tmp_path / "plugins", DEPLOYED_INIT_NO_ORCH,
                             with_orch_module=False)
        (cl / "__init__.py").write_text("this is not : valid python {{{\n")
        r = check_plugins(plugins_dir=str(tmp_path / "plugins"),
                          repo_root=str(repo))
        # A broken deployed __init__ is NOT this row's verdict (the payload
        # sha-diff owns content); the cluster leg stays unassertive.
        assert r["ok"] is True

    def test_empty_registry_tables_is_unassertive(self, tmp_path):
        from hscc_daemon.verify import check_plugins
        repo = _plant_repo(tmp_path, init="def register(ctx):\n    pass\n")
        _plant_deployed(tmp_path / "plugins", DEPLOYED_INIT_NO_ORCH,
                        with_orch_module=False)
        r = check_plugins(plugins_dir=str(tmp_path / "plugins"),
                          repo_root=str(repo))
        assert r["ok"] is True
        assert "all core commands found" in r["detail"]

    def test_core_commands_fail_still_fails(self, tmp_path):
        """The original legs are untouched: missing core strings is still RED
        naming the strings, with the cluster leg reported alongside."""
        from hscc_daemon.verify import check_plugins
        repo = _plant_repo(tmp_path)
        _plant_deployed(tmp_path / "plugins", DEPLOYED_INIT_FULL)
        (tmp_path / "plugins" / "hscc-commands" / "__init__.py").write_text(
            'register("workers-up")\n')
        r = check_plugins(plugins_dir=str(tmp_path / "plugins"),
                          repo_root=str(repo))
        assert r["ok"] is False
        assert "cluster-restart" in r["detail"]
        assert "template" in r["detail"]


class TestRunAllCarriesTheRow:
    """run_all must keep ONE plugins row (the 13-row count guard) and it must
    carry the cluster verdict; the hermetic overrides keep it unassertive."""

    def test_run_all_single_plugins_row_hermetic(self, tmp_path, monkeypatch):
        from hscc_daemon.verify import run_all
        plugins = tmp_path / "plugins"
        _plant_core(plugins)
        _plant_deployed(plugins, DEPLOYED_INIT_NO_ORCH,
                        with_orch_module=False)  # drift planted...
        repo = _plant_repo(tmp_path)
        result = run_all(plugins_dir=str(plugins),
                         repo_root=str(tmp_path / "empty-repo"))  # ...but the
        # hermetic repo_root override has NO hscc-cluster tree, so the row
        # stays unassertive and the suite never inherits live deploy drift.
        rows = [c for c in result["checks"] if c["name"] == "plugins"]
        assert len(rows) == 1
        assert rows[0]["ok"] is True

    def test_run_all_surfaces_live_shape_drift(self, tmp_path):
        from hscc_daemon.verify import run_all
        plugins = tmp_path / "plugins"
        _plant_deployed(plugins, DEPLOYED_INIT_NO_ORCH,
                        with_orch_module=False)
        repo = _plant_repo(tmp_path)
        result = run_all(plugins_dir=str(plugins), repo_root=str(repo))
        row = _plugins_row(result)
        assert row["ok"] is False
        assert "orch_message.py" in row["detail"]
        assert result["ok"] is False
