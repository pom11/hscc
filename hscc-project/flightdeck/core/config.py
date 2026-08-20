"""config.py — flightdeck user connection settings.

Small, separate from ``registry.yaml``. Holds connection-level settings that
would otherwise leak private values into the public repo as source constants:
the Telegram group id and the MCP daemon URL.

Values are read from ``~/.flightdeck/config.yaml`` (overridable for tests via
a ``path`` argument), with environment variables taking precedence over the
file. Precedence (highest wins)::

    FLIGHTDECK_TELEGRAM_GROUP_ID   >  config.yaml telegram.group_id
    FLIGHTDECK_MCP_URL             >  config.yaml telegram.mcp_url
                                     >  default ``http://127.0.0.1:8787/mcp``

A missing config file is fine for everything EXCEPT the Telegram group id,
which has no safe, universally-valid default: publishing a baked-in id would
ship a private group in source. When it is unset, resolving it raises
:class:`MissingGroupIdError` with a clear, actionable message; callers that
touch Telegram surface that to the user, and commands that never touch
Telegram never need config at all.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml


class ConfigError(Exception):
    """Base class for flightdeck configuration errors."""


class MissingGroupIdError(ConfigError):
    """The Telegram ``group_id`` is not configured anywhere.

    Raised by :func:`telegram_group_id` when neither the config file nor the
    environment supplies a value. The message is actionable: it names the
    exact config key and env var to set, and how to find a group id.
    """


DEFAULT_CONFIG = "~/.flightdeck/config.yaml"

# The localhost default MCP URL is a sane, public-safe default that leaks
# nothing and matches the shared MCP daemon's own local binding.
DEFAULT_MCP_URL = "http://127.0.0.1:8787/mcp"

# Environment overrides; these win over the config file when both are set.
ENV_GROUP_ID = "FLIGHTDECK_TELEGRAM_GROUP_ID"
ENV_MCP_URL = "FLIGHTDECK_MCP_URL"

# Config-file keys, for the actionable error message.
_CONFIG_KEY_GROUP_ID = "telegram.group_id"
_CONFIG_KEY_MCP_URL = "telegram.mcp_url"


def config_path(path: str | None = None) -> Path:
    """The config file path, defaulting to ~/.flightdeck/config.yaml.

    A leading ``~`` is expanded. An explicitly-supplied path (tests, or a
    power user running from a different home) wins over the default.
    """
    return Path(os.path.expanduser(path if path is not None else DEFAULT_CONFIG))


def _telegram_mapping(path: str | None = None) -> dict:
    """Load the ``telegram:`` mapping from the config file.

    A missing file, an empty file, or a file without a ``telegram`` key all
    yield an empty mapping (no error — those are all "not configured", which
    is only fatal for ``group_id``). A present but malformed file raises rather
    than silently guessing, because an unparseable config is an unknown state.
    """
    p = config_path(path)
    if not p.exists():
        return {}
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse {p}: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping, got {type(raw).__name__}")
    tg = raw.get("telegram")
    if tg is None:
        return {}
    if not isinstance(tg, dict):
        raise ConfigError("config 'telegram' must be a mapping")
    return tg


def telegram_group_id(path: str | None = None, env: dict | None = None) -> str:
    """Resolve the Telegram group id: env override wins, else the config file.

    Raises :class:`MissingGroupIdError` when it is not set anywhere — there is
    deliberately NO default, because a baked-in fallback would again ship a
    private group id in the product. The error names the config key, the env
    var, and tells the user how to find their own group id, so the fix is
    unambiguous.
    """
    env_map = os.environ if env is None else env
    from_env = env_map.get(ENV_GROUP_ID)
    if from_env:
        return str(from_env).strip()

    value = _telegram_mapping(path).get("group_id")
    if value is None or str(value).strip() == "":
        raise MissingGroupIdError(
            "Telegram is not configured: no group id set. Set `"
            + _CONFIG_KEY_GROUP_ID
            + "` in "
            + str(config_path(path))
            + " (or export "
            + ENV_GROUP_ID
            + ") to point flightdeck at your Telegram group. To find a group "
            "id, add the group id to a private group's link on the Telegram "
            "desktop app (Settings > Advanced > Folder/chat folder ids), or "
            "ask a bot for the chat id. Flightdeck never guesses a group id."
        )
    return str(value).strip()


def telegram_mcp_url(path: str | None = None, env: dict | None = None) -> str:
    """Resolve the MCP daemon URL: env override wins, else config file, else
    the localhost default.

    Unlike ``group_id``, this HAS a sane default: ``http://127.0.0.1:8787/mcp``
    is the shared daemon's local binding and leaks nothing about the operator.
    """
    env_map = os.environ if env is None else env
    from_env = env_map.get(ENV_MCP_URL)
    if from_env:
        return str(from_env).strip()

    value = _telegram_mapping(path).get("mcp_url")
    if value is None or str(value).strip() == "":
        return DEFAULT_MCP_URL
    return str(value).strip()
