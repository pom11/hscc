"""Test hscc.py CLI commands."""
import io
import json
import os
import sys
import pytest
import hscc
import rolelib


def test_cmd_generate_continues_past_bad_spec(tmp_path, monkeypatch):
    """cmd_generate wraps each spec in try/except; bad specs are recorded but
    remaining specs still generate, and return code is non-zero."""
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    (roles_dir / "good.yaml").write_text(
        "name: good\nidentity: Does good.\nrouting_description: Good stuff.\n"
    )
    # bad spec — missing required fields
    (roles_dir / "bad.yaml").write_text(
        "name:\nidentity:\nrouting_description:\n"
    )
    profiles_dir = tmp_path / "profiles"

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(rolelib, "ROLES_DIR", str(roles_dir))
    monkeypatch.setattr(rolelib, "PROFILES_DIR", str(profiles_dir))

    # Patch _base_identity so we don't need the real file
    monkeypatch.setattr(hscc, "_base_identity", lambda: "BASE\n")

    ret = hscc.cmd_generate()
    assert ret == 1, "Expected non-zero return when there are failures"

    # The good role should still have been generated
    assert os.path.exists(str(profiles_dir / "good" / "SOUL.md"))


def test_cmd_generate_emits_valid_json(tmp_path, monkeypatch):
    """cmd_generate stdout must be parseable JSON (not a Python dict repr)."""
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    (roles_dir / "tester.yaml").write_text(
        "name: tester\nidentity: Test role.\nrouting_description: Testing.\n"
    )
    profiles_dir = tmp_path / "profiles"

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(rolelib, "ROLES_DIR", str(roles_dir))
    monkeypatch.setattr(rolelib, "PROFILES_DIR", str(profiles_dir))
    monkeypatch.setattr(hscc, "_base_identity", lambda: "BASE\n")

    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    hscc.cmd_generate()

    stdout = captured.getvalue()
    parsed = json.loads(stdout)
    assert isinstance(parsed, dict)
    assert "generated" in parsed
    assert len(parsed["generated"]) == 1
    assert parsed["generated"][0]["role"] == "tester"
