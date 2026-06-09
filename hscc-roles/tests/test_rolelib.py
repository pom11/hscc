import os
import pytest
import rolelib


def test_full_toolset_excludes_cluster():
    ts = rolelib.role_toolsets()
    assert "hscc-cluster" not in ts
    assert "hermes-cli" in ts
    assert "kanban" in ts


def test_paths_are_under_plugin_dir():
    assert rolelib.ROLES_DIR.endswith("hscc-roles/roles")
    assert rolelib.BASE_IDENTITY_PATH.endswith("hscc-roles/base-identity.md")


def test_load_spec_valid(tmp_path):
    spec_file = tmp_path / "coder.yaml"
    spec_file.write_text(
        "name: coder\n"
        "identity: |\n"
        "  You build things.\n"
        "preload_skills: [test-driven-development]\n"
    )
    spec = rolelib.load_spec(str(spec_file))
    assert spec["name"] == "coder"
    assert "You build things." in spec["identity"]
    assert spec["preload_skills"] == ["test-driven-development"]


def test_load_spec_missing_name_raises(tmp_path):
    spec_file = tmp_path / "bad.yaml"
    spec_file.write_text("identity: hi\n")
    with pytest.raises(ValueError, match="missing required field 'name'"):
        rolelib.load_spec(str(spec_file))


def test_load_spec_missing_identity_raises(tmp_path):
    spec_file = tmp_path / "bad.yaml"
    spec_file.write_text("name: x\n")
    with pytest.raises(ValueError, match="missing required field 'identity'"):
        rolelib.load_spec(str(spec_file))


def test_load_spec_defaults_preload_skills_empty(tmp_path):
    spec_file = tmp_path / "min.yaml"
    spec_file.write_text("name: x\nidentity: hi\n")
    spec = rolelib.load_spec(str(spec_file))
    assert spec["preload_skills"] == []
