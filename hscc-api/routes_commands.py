"""HSCC API — slash-command catalog (read-only).

Exposes the slash commands the HSCC chat client can offer in its palette.
The list is NOT maintained here: it is sourced from the authoritative
registration in the ``hscc-commands`` plugin (``hscc-commands/__init__.py``,
``register(ctx)``), which is where Hermes/HSCC actually registers every slash
command via ``ctx.register_command(name=..., description=..., args_hint=...)``.

So a command that is registered there appears here automatically, and a
command removed there disappears here automatically — there is no second
hand-written list to rot (the bug this card prevents).

Read-only by construction:
  * ``register(ctx)`` only RECORDS declarations onto the given ``ctx``; it
    never executes a command handler. We call it with a recording stub
    ``ctx`` so nothing is run — this endpoint performs NO side effects and
    shells out to nothing.

Design note: the commands exposed by this endpoint are the operator slash
commands the HSCC daemon/gateway registers for the cluster (global — not
scoped per project). The route therefore takes no project path segment and
returns the whole registered set. If per-project command sets are added
upstream later, this module can accept an optional ``name`` path segment;
today the authoritative registration is single-scope.

Conventions (design §A, shared):
  * handler is ``(server, ctx, query, body) -> (status, payload_dict)``;
  * ``speak`` is ALWAYS present on a read response (design §B);
  * if the plugin can't be imported/read, degrade to a 200 with an honest
    ``speak`` (never fabricate a command list, never crash).
"""

from __future__ import annotations

import importlib
import os
import re
import sys
from pathlib import Path

from api_server import ROUTES  # noqa: E402

# The hscc-commands plugin sits as a sibling package of hscc-api in the HSCC
# checkout (``<repo>/hscc-commands/``). Its package name contains a hyphen, so
# it must be loaded via importlib (``import hscc-commands`` is not valid
# syntax) with the repo root on sys.path, mirroring how hscc_daemon discovers
# the plugin beside it. We resolve the plugin dir the same way routes_project
# resolves flightdeck — from the live checkout, falling back to the deployed
# plugins dir so the endpoint works whether running in-repo or installed.
_PLUGIN_NAME = "hscc-commands"


def _plugin_dir() -> Path | None:
    """Resolve the directory holding the ``hscc-commands`` package.

    Precedence: sibling checkout next to ``hscc-api`` in a repo checkout
    (``<parent>/hscc-commands``), then the deployed HSCC plugins dir
    (``~/.hscc/plugins``), then ``~/.hermes/plugins``. Returns None when no
    candidate holds the plugin, so the endpoint can degrade honestly.
    """
    here = Path(__file__).resolve().parent          # .../hscc-api
    candidates = [
        here.parent / "hscc-commands",              # repo checkout sibling
        Path.home() / ".hscc" / "plugins" / "hscc-commands",
    ]
    for c in candidates:
        if (c / "__init__.py").is_file():
            return c.parent
    return None


def _recorded_commands():
    """Return the registered slash commands by invoking register() against a
    recording stub ctx — the SINGLE authoritative source.

    Returns a list of dicts ``{name, description, takes_args}`` (``takes_args``
    derived from the presence of ``args_hint`` on the registration), or ``None``
    when the plugin is unavailable so the caller can degrade honestly.
    """
    plugin_dir = _plugin_dir()
    if plugin_dir is None:
        return None
    added = []
    try:
        if str(plugin_dir) not in sys.path:
            sys.path.insert(0, str(plugin_dir))
        plugin = importlib.import_module(_PLUGIN_NAME)
        if not callable(getattr(plugin, "register", None)):
            return None
        records = []

        class _RecordingCtx:
            """Stub ctx that only records declarations — never executes."""

            def register_command(self, **kw):
                records.append(kw)

        plugin.register(_RecordingCtx())
        for rec in records:
            name = rec.get("name")
            if not name:
                continue
            added.append({
                "name": name,
                "description": rec.get("description", ""),
                "takes_args": bool(rec.get("args_hint")),
            })
    except Exception:
        return None
    return added


def handle_commands(server, ctx, query, body):
    """GET /v1/commands — the slash commands available to the chat client.

    Read-only: sources the list from the authoritative ``hscc-commands``
    plugin registration (no second hand-written list), and never executes a
    command. Each item carries its ``name``, one-line ``description``, and
    whether it ``takes_args``. A single ``speak`` field summarizes.
    """
    commands = _recorded_commands()
    if commands is None:
        return 200, {"speak": "Slash-command list unavailable."}
    payload = {"commands": commands}
    n = len(commands)
    payload["speak"] = (
        f"{n} slash command{'s' if n != 1 else ''} available."
        if n
        else "No slash commands registered."
    )
    return 200, payload


ROUTES.append(("GET", re.compile(r"^/v1/commands$"), handle_commands))
