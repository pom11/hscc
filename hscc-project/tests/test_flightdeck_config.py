"""Tests for flightdeck.core.config — the Telegram group id / MCP URL loader.

These cover the public-release blocker: the operator's private Telegram group
id must come from configuration (env or ~/.flightdeck/config.yaml), never a
hardcoded source constant. All tests use a tmp_path config file and injected
env dicts — nothing touches real ~/.flightdeck, real env vars, or the network.
"""

from __future__ import annotations

import pytest

from flightdeck.core import config
from flightdeck.core.config import ENV_GROUP_ID, ENV_MCP_URL, MissingGroupIdError


def _write_config(tmp_path, **telegram_kwargs):
    """Write a config.yaml under tmp_path with the given telegram keys."""
    doc = {}
    if telegram_kwargs:
        doc["telegram"] = telegram_kwargs
    path = tmp_path / "config.yaml"
    if doc:
        import yaml

        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    else:
        path.write_text("", encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- #
# group_id — file vs env precedence, missing raises
# --------------------------------------------------------------------------- #


def test_group_id_reads_config_file_value(tmp_path):
    """A config-file telegram.group_id is used when no env var is set."""
    cfg = _write_config(tmp_path, group_id="-100111222333")
    assert config.telegram_group_id(path=cfg) == "-100111222333"


def test_group_id_env_overrides_config_file(tmp_path):
    """FLIGHTDECK_TELEGRAM_GROUP_ID wins over the config file."""
    cfg = _write_config(tmp_path, group_id="-100111222333")
    env = {ENV_GROUP_ID: "-100999888777"}
    assert config.telegram_group_id(path=cfg, env=env) == "-100999888777"


def test_public_release_blocker_missing_group_id_raises_actionable(tmp_path):
    """No group id anywhere -> a clear, actionable error naming the config key.

    Named for the public-release blocker: a missing id must never silently
    fall back to a baked-in group. The message names `telegram.group_id`, the
    config file path, and the env var, so the user knows exactly what to set.
    """
    cfg = _write_config(tmp_path)  # empty config, no telegram key
    with pytest.raises(MissingGroupIdError) as excinfo:
        config.telegram_group_id(path=cfg, env={})
    msg = str(excinfo.value)
    assert config._CONFIG_KEY_GROUP_ID in msg
    assert ENV_GROUP_ID in msg
    assert "never guesses" in msg


def test_group_id_rejects_blank_config_value(tmp_path):
    """A present-but-blank group_id is treated as unset (still raises)."""
    cfg = _write_config(tmp_path, group_id="   ")
    with pytest.raises(MissingGroupIdError):
        config.telegram_group_id(path=cfg, env={})


def test_group_id_env_wins_even_directly():
    """The env-only path works with no config file at all."""
    env = {ENV_GROUP_ID: "-100555444333"}
    assert config.telegram_group_id(path="/nonexistent/nowhere.yaml", env=env) == "-100555444333"


# --------------------------------------------------------------------------- #
# mcp_url — env > file > localhost default
# --------------------------------------------------------------------------- #


def test_mcp_url_defaults_to_localhost_with_no_config(tmp_path):
    """No config and no env -> the safe localhost default, never an error."""
    cfg = _write_config(tmp_path)
    assert config.telegram_mcp_url(path=cfg, env={}) == config.DEFAULT_MCP_URL
    assert config.DEFAULT_MCP_URL == "http://127.0.0.1:8787/mcp"


def test_mcp_url_reads_config_file_value(tmp_path):
    """A config-file telegram.mcp_url is used when no env var is set."""
    cfg = _write_config(tmp_path, mcp_url="http://10.0.0.5:9000/mcp")
    assert config.telegram_mcp_url(path=cfg, env={}) == "http://10.0.0.5:9000/mcp"


def test_mcp_url_env_overrides_config_file(tmp_path):
    """FLIGHTDECK_MCP_URL wins over the config file."""
    cfg = _write_config(tmp_path, mcp_url="http://10.0.0.5:9000/mcp")
    env = {ENV_MCP_URL: "http://10.1.1.1:8787/mcp"}
    assert config.telegram_mcp_url(path=cfg, env=env) == "http://10.1.1.1:8787/mcp"
