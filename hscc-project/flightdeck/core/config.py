"""config.py — flightdeck user connection settings.

Small, separate from ``registry.yaml``. Holds connection-level settings.
Kept minimal now that the Telegram surface has been removed. The MCP daemon
URL that used to serve Telegram is no longer resolved here; flightdeck no
longer reaches any daemon for messaging.

Values are read from ``~/.flightdeck/config.yaml`` (overridable for tests via
a ``path`` argument), with environment variables taking precedence over the
file.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml


class ConfigError(Exception):
    """Base class for flightdeck configuration errors."""


DEFAULT_CONFIG = "~/.flightdeck/config.yaml"


def config_path(path: str | None = None) -> Path:
    """The config file path, defaulting to ~/.flightdeck/config.yaml.

    A leading ``~`` is expanded. An explicitly-supplied path (tests, or a
    power user running from a different home) wins over the default.
    """
    return Path(os.path.expanduser(path if path is not None else DEFAULT_CONFIG))
