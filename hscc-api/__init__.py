"""HSCC HTTP API server (pure stdlib).

Brings the HSCC CLI to other apps over HTTP — a bearer-token-authenticated,
Tailscale-optional, JSON API for cluster state and (later, in A2/A3/A4)
project/kanban dispatch.

This plugin dir (hscc-api) holds ONLY the server + auth + error contract
(Phase A1). Endpoints beyond the API's own liveness check (``/v1/ping``) are
added by later cards registering against the route table in
``api_server.ROUTES``.
"""

__version__ = "0.1.0"

__all__ = ["api_server"]
