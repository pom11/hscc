"""repl_delivery — deliver a message into the project's live REPL (CLI path).

The iOS/WS write path (hscc-api/routes_ws) is tmux-first via repl_inject;
`hscc project message send <project> "..." --apply` uses the SAME module
from the CLI, so both doors type into the SAME pane the operator sees. No
second driver, no Telegram-era fiction.

Pane discovery lives in hscc-api/repl_inject.py. This package is a sibling
of hscc-api both in the repo (<base>/hscc-project and <base>/hscc-api) and
in an installed tree (~/.hermes/plugins/hscc-project and .../hscc-api), so
the loader resolves that sibling and puts it on sys.path — the same trick
api_cli._load_api_server uses for its own modules.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _repl_inject():
    """Import the sibling hscc-api/repl_inject module (or None).

    parents[3] of this file: repo root when running from a checkout,
    plugins dir when installed. hscc-api must sit beside hscc-project in
    either layout — it does (install_payload.DEFAULT_PAYLOAD ships both).
    """
    base = Path(__file__).resolve().parents[3]
    api_dir = base / "hscc-api"
    if not (api_dir / "repl_inject.py").is_file():
        return None
    if str(api_dir) not in sys.path:
        sys.path.insert(0, str(api_dir))
    import repl_inject  # noqa: F401
    return repl_inject


def deliver(project_name: str, message: str) -> dict:
    """Inject ``message`` into the project's orchestrator REPL pane.

    Profile resolution is the lifecycle SSoT convention: the project's
    orchestrator is ``<name>-orch`` (flightdeck project_lifecycle step 6 —
    the same profile the CLI chat opens). The pane must run
    ``hermes -p <name>-orch chat``; repl_inject refuses to guess among
    several, and refuses ambiguity entirely.

    Returns a receipt dict on success. Raises ReplDeliveryError(kind,
    message) — kinds: module_missing, no_panes, ambiguous, tmux, busy.
    """
    from ..core.project_lifecycle import _orch_profile_name
    ri = _repl_inject()
    if ri is None:
        raise ReplDeliveryError(
            "module_missing",
            "hscc-api/repl_inject.py not found beside hscc-project — "
            "is the hscc-api plugin installed?")
    profile = _orch_profile_name(project_name)
    try:
        # resolve_pane enforces exactly-one itself: InjectError kind
        # 'no_panes' for zero matches, 'ambiguous' for more than one.
        pane = ri.resolve_pane(profile)
    except ri.InjectError as exc:
        if exc.kind == "no_panes":
            raise ReplDeliveryError(
                "no_panes",
                f"no tmux pane runs the REPL for '{project_name}' "
                f"(hermes -p {profile} chat). Open it in tmux and retry, "
                "or use the iOS app — the API may fall back to its serve path."
            ) from exc
        raise ReplDeliveryError(exc.kind, exc.message) from exc
    try:
        return ri.deliver(pane, message)
    except ri.InjectError as exc:
        raise ReplDeliveryError(exc.kind, exc.message) from exc


class ReplDeliveryError(RuntimeError):
    """A delivery refusal with a machine-readable kind."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message
