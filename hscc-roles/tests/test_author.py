import os
import rolelib
import author


def test_create_writes_valid_spec(tmp_path, monkeypatch):
    monkeypatch.setattr(author.rolelib, "ROLES_DIR", str(tmp_path / "roles"))
    path = author.create_role("financial-analyst",
                              "Analyzes budgets, models cash flow, reports risk.")
    assert os.path.exists(path)
    spec = rolelib.load_spec(path)  # must be loadable/valid
    assert spec["name"] == "financial-analyst"
    assert "cash flow" in spec["identity"]
    assert isinstance(spec["preload_skills"], list)


def test_create_rejects_bad_name(tmp_path, monkeypatch):
    monkeypatch.setattr(author.rolelib, "ROLES_DIR", str(tmp_path / "roles"))
    import pytest
    with pytest.raises(ValueError, match="invalid role name"):
        author.create_role("Bad Name!", "desc")


def test_create_refuses_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(author.rolelib, "ROLES_DIR", str(tmp_path / "roles"))
    author.create_role("coder", "Builds code.")
    import pytest
    with pytest.raises(ValueError, match="already exists"):
        author.create_role("coder", "Builds code again.")
