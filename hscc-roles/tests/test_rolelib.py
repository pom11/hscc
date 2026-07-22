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
        "routing_description: Claim code tasks. Do NOT claim architecture.\n"
        "preload_skills: [test-driven-development]\n"
    )
    spec = rolelib.load_spec(str(spec_file))
    assert spec["name"] == "coder"
    assert "You build things." in spec["identity"]
    assert spec["preload_skills"] == ["test-driven-development"]
    assert "Claim code tasks." in spec["routing_description"]


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
    spec_file.write_text("name: x\nidentity: hi\nrouting_description: Minimal.\n")
    spec = rolelib.load_spec(str(spec_file))
    assert spec["preload_skills"] == []
    assert spec["routing_description"] == "Minimal."


def test_list_spec_files_empty_when_no_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(rolelib, "ROLES_DIR", str(tmp_path / "nonexistent"))
    assert rolelib.list_spec_files() == []


def test_list_spec_files_returns_sorted_yaml(tmp_path, monkeypatch):
    monkeypatch.setattr(rolelib, "ROLES_DIR", str(tmp_path))
    (tmp_path / "z.yaml").touch()
    (tmp_path / "a.yaml").touch()
    (tmp_path / "ignore.txt").touch()
    files = rolelib.list_spec_files()
    assert len(files) == 2
    assert files[0].endswith("a.yaml")
    assert files[1].endswith("z.yaml")


def test_starter_specs_all_load():
    files = rolelib.list_spec_files()
    names = {rolelib.load_spec(f)["name"] for f in files}
    assert {"orchestrator", "architect", "coder", "reviewer", "qa"}.issubset(names)


def test_base_identity_exists_and_nonempty():
    assert os.path.exists(rolelib.BASE_IDENTITY_PATH)
    with open(rolelib.BASE_IDENTITY_PATH) as f:
        assert len(f.read().strip()) > 100


# -- model_tier tests --


def test_model_tier_fast_defaults(tmp_path):
    """When model_tier is absent, it defaults to 'fast'. Cant be a value error, but it should be a value."""
    spec_file = tmp_path / "min.yaml"
    spec_file.write_text(
        "name: x\n"
        "identity: hi\n"
        "routing_description: Minimal.\n"
    )
    spec = rolelib.load_spec(str(spec_file))
    assert spec["model_tier"] == "fast"


def test_model_tier_strong_when_set(tmp_path):
    """When model_tier: strong, spec returns 'strong'."""
    spec_file = tmp_path / "arch.yaml"
    spec_file.write_text(
        "name: architect\n"
        "identity: Designs systems.\n"
        "routing_description: Architecture.\n"
        "model_tier: strong\n"
    )
    spec = rolelib.load_spec(str(spec_file))
    assert spec["model_tier"] == "strong"


def test_model_tier_invalid_raises(tmp_path):
    """Invalid model_tier raises ValueError."""
    spec_file = tmp_path / "bad.yaml"
    spec_file.write_text(
        "name: x\n"
        "identity: hi\n"
        "routing_description: test.\n"
        "model_tier: quantum\n"
    )
    with pytest.raises(ValueError, match="model_tier must be one of"):
        rolelib.load_spec(str(spec_file))


def test_model_tier_case_insensitive(tmp_path):
    """model_tier is normalized to lowercase."""
    spec_file = tmp_path / "case.yaml"
    spec_file.write_text(
        "name: x\n"
        "identity: hi\n"
        "routing_description: test.\n"
        "model_tier: STRONG\n"
    )
    spec = rolelib.load_spec(str(spec_file))
    assert spec["model_tier"] == "strong"


def test_all_specs_load_with_model_tier():
    """All real spec files load and carry a valid model_tier."""
    files = rolelib.list_spec_files()
    for f in files:
        spec = rolelib.load_spec(f)
        assert spec["model_tier"] in ("fast", "strong")


def test_model_tier_null_defaults_to_fast(tmp_path):
    """Explicit YAML model_tier: (null) defaults to 'fast', not 'none'."""
    spec_file = tmp_path / "null_tier.yaml"
    spec_file.write_text(
        "name: x\n"
        "identity: hi\n"
        "routing_description: test.\n"
        "model_tier:\n"
    )
    spec = rolelib.load_spec(str(spec_file))
    assert spec["model_tier"] == "fast"
