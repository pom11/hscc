"""Hermetic tests for the profile-home config fallback (t_9e8732b8).

Hermes multiplexed profiles each run with a profile-scoped HERMES_HOME
(``~/.hermes/profiles/<name>``). ``_load_config(hermes_home)`` is called with
that profile home, but the single shared ``memori_byodb.json`` lives at the
default home ``~/.hermes``. Before this fix the lookup was strict, so for every
profile home ``_load_config`` returned None and ``initialize()`` raised —
the provider never activated (the root cause of zero capture since 2026-06-13).

These tests are hermetic: they patch the module-level ``_DEFAULT_HOME`` to a
temp dir and never read the real ``~/.hermes/memori_byodb.json`` or touch any
live DB.
"""

from __future__ import annotations

import json

from memori_byodb import MemoriBYODBMProvider, _load_config, _DEFAULT_HOME


def _write_config(home, *, entity_id="desac_hermes"):
    home.mkdir(parents=True, exist_ok=True)
    (home / "memori_byodb.json").write_text(
        json.dumps({"entityId": entity_id, "projectId": "cluster",
                    "processId": "gw",
                    "dbPath": str(home / "memori_byodb.db")}),
        encoding="utf-8",
    )


def test_load_config_falls_back_to_shared_default_home(tmp_path, monkeypatch):
    """A profile home with no config still resolves the shared default config."""
    default_home = tmp_path / "hermes"  # stands in for ~/.hermes
    _write_config(default_home, entity_id="desac_hermes")
    monkeypatch.setattr("memori_byodb._DEFAULT_HOME", default_home)

    profile_home = default_home / "profiles" / "hscc-orch"
    profile_home.mkdir(parents=True)

    cfg = _load_config(profile_home)
    assert cfg is not None
    assert cfg.entity_id == "desac_hermes"


def test_load_config_prefers_profile_home_when_present(tmp_path, monkeypatch):
    """A profile home with its OWN config wins over the shared default."""
    default_home = tmp_path / "hermes"
    _write_config(default_home, entity_id="shared_entity")
    monkeypatch.setattr("memori_byodb._DEFAULT_HOME", default_home)

    profile_home = default_home / "profiles" / "custom"
    _write_config(profile_home, entity_id="profile_entity")

    cfg = _load_config(profile_home)
    assert cfg is not None
    assert cfg.entity_id == "profile_entity"


def test_load_config_none_when_everywhere_absent(tmp_path, monkeypatch):
    default_home = tmp_path / "hermes"
    monkeypatch.setattr("memori_byodb._DEFAULT_HOME", default_home)
    profile_home = default_home / "profiles" / "empty"
    profile_home.mkdir(parents=True)

    assert _load_config(profile_home) is None


def test_initialize_succeeds_for_profile_home(tmp_path, monkeypatch):
    """initialize() must not raise for a profile-scoped home (regression: it
    used to raise RuntimeError -> provider never activated). Uses a fake client
    so no SQLite/SDK glue runs."""
    default_home = tmp_path / "hermes"
    _write_config(default_home, entity_id="desac_hermes")
    monkeypatch.setattr("memori_byodb._DEFAULT_HOME", default_home)

    captured = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("memori_byodb.MemoriBYODBClient", _FakeClient)

    provider = MemoriBYODBMProvider()
    provider.initialize("sess-1", hermes_home=str(default_home / "profiles" / "hscc-orch"))
    # The shared config was resolved -> the client was constructed from it.
    assert captured["entity_id"] == "desac_hermes"
    assert provider._project_id  # resolved from config projectId
