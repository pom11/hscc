"""Tests for flightdeck.core.config — the user connection settings loader.

Telegram has been removed from flightdeck: the group-id / MCP URL / enabled
loaders that lived here are gone (config.py shrank to a path helper + error
base). This file now covers only the surviving config surface.
"""

from __future__ import annotations

from flightdeck.core import config


def test_config_path_expands_home():
    """A leading ~ is expanded to an absolute path."""
    p = config.config_path("~/somewhere.yaml")
    assert str(p).startswith("/")
    assert str(p).endswith("somewhere.yaml")
    assert "~" not in str(p)


def test_config_path_explicit_plus_overrides_default():
    """An explicit path wins over the module default ~/.flightdeck/config.yaml."""
    p = config.config_path("custom/dir/config.yaml")
    assert str(p) == "custom/dir/config.yaml"


def test_config_path_default_is_flightdeck_config():
    """When no path is given, the default ~/.flightdeck/config.yaml is used."""
    p = config.config_path()
    assert str(p).endswith(".flightdeck/config.yaml")
