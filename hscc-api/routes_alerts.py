"""HSCC HTTP API — the alert feed (``/v1/alerts``).

The main orchestrator reaches the operator through one feed (design:
``docs/design/2026-11-19-main-orchestrator-design.md`` §10). This module is the
transport-neutral HTTP face of it; the store, dedupe and rate limits live in
``hscc_daemon.alerts`` so ``hscc notify`` and the apps see exactly the same
rules and the same durable record.

    POST /v1/alerts                  create / dedupe / rate-limit one alert
    GET  /v1/alerts?since=<id>       paging read for the apps
    POST /v1/alerts/<id>/ack         mark seen; clears its dedupe slot

Every route is behind the server's single bearer-token gate (``_authorize`` in
``api_server``) like every other /v1 route; there is no unauthenticated alert
path. The response never contains credentials, device tokens or transport
secrets — the feed is title/body/level/key and the receiver's state.

CONTRACT NOTES
==============
* **Create answers 201; suppression answers 200.** A deduplicated or
  rate-limited post is not a failure — the feed did what it promised — so the
  caller must branch on ``created`` / ``deduped`` / ``rate_limited``, not on
  the status alone. ``retry_after_s`` is present only when rate-limited.
* **The alert shape is stable.** ``id`` is a monotonic integer, ``ts`` is
  UTC ``...Z``, ``level`` is one of ``info|action|critical``, ``acked`` is a
  boolean and ``acked_at`` is null until acknowledged. ``key`` is the dedupe
  identity or null.
* **``since`` is exclusive and ascending.** A client advances ``next_since``;
  ``has_more`` says whether to ask again. ``latest_id`` is the newest id the
  store holds, not merely the newest in this page.
* **No invented data.** A missing/corrupt store answers the empty feed with an
  honest ``speak``; validation failures are 400 with the store's own sentence;
  an unknown ack id is 404.

Test seam: every backing call goes through ``_backing_*`` so route tests never
write the operator's live ``~/.hscc/alerts.json``.
"""

from __future__ import annotations

import re

from api_server import ApiError, ROUTES


# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch these in tests)
# --------------------------------------------------------------------------- #

def _backing_alerts():
    from hscc_daemon import alerts
    return alerts


def _store_dir(ctx):
    """The server's hscc root — the same directory its token lives in."""
    return getattr(ctx, "hscc_dir", None) or None


def _bad_request(message, speak=None):
    return ApiError(400, "bad_request", message, speak or message)


def _parse_body(body):
    """Decode the dispatcher's raw body bytes into a JSON object.

    ``ApiHandler._dispatch`` hands handlers the RAW bytes (it reads at most
    1 MiB); parsing is the route's job, the same shape ``routes_kanban`` uses.
    An empty body is an empty object so ``{"title": ...}`` is not required to
    carry a second framing layer.
    """
    import json

    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise _bad_request("request body must be JSON")
    if not isinstance(data, dict):
        raise _bad_request("body must be a JSON object")
    return data


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

def handle_post_alert(server, ctx, query, body):
    """Create one alert (or report why it was deduplicated/rate-limited)."""
    body = _parse_body(body)

    title = body.get("title")
    text = body.get("body", "")
    level = body.get("level")
    key = body.get("key")

    try:
        result = _backing_alerts().post_alert(
            title=title,
            body=text,
            level=level,
            key=key,
            hscc_dir=_store_dir(ctx),
        )
    except ValueError as exc:
        raise _bad_request(str(exc), "The alert was not accepted.") from exc

    alert = result.get("alert") or {}
    if result.get("created"):
        status = 201
        speak = f"Alert {alert.get('id')} posted."
    elif result.get("deduped"):
        status = 200
        speak = f"Alert {alert.get('id')} is unchanged; nothing new to send."
    else:
        status = 200
        speak = "Alert suppressed by the rate limit."

    payload = {
        "alert": result.get("alert"),
        "created": bool(result.get("created")),
        "deduped": bool(result.get("deduped")),
        "rate_limited": bool(result.get("rate_limited")),
        "reason": result.get("reason"),
        "retry_after_s": result.get("retry_after_s"),
        "speak": speak,
    }
    if result.get("push") is not None:
        payload["push"] = result["push"]
    return status, payload


def handle_list_alerts(server, ctx, query, body):
    """Paging read: ``?since=<id>&limit=<n>`` (exclusive, ascending)."""
    since = query.get("since")
    if since is not None and not str(since).strip():
        since = None
    limit = query.get("limit", "").strip() or "100"

    try:
        result = _backing_alerts().list_alerts(
            since=since, limit=limit, hscc_dir=_store_dir(ctx),
        )
    except ValueError as exc:
        raise _bad_request(str(exc), "The alert feed query was not valid.") from exc

    count = result.get("count", 0)
    result["speak"] = (
        f"{count} alert{'s' if count != 1 else ''}."
        if count else "No new alerts."
    )
    return 200, result


def handle_ack_alert(server, ctx, query, body):
    """Mark one alert acknowledged (idempotent); clears its dedupe slot."""
    raw = query.get("id")
    # The regex route param is delivered as ``id`` by the dispatcher's query
    # merge; fall back to the raw path match when a caller invokes the handler
    # directly (tests do, and a missing id must be a 400, never a 500).
    if raw is None:
        raise _bad_request("alert id is required")

    try:
        alert = _backing_alerts().ack_alert(raw, hscc_dir=_store_dir(ctx))
    except ValueError as exc:
        raise _bad_request(str(exc), "The alert id was not valid.") from exc

    if alert is None:
        raise ApiError(
            404, "not_found",
            f"no alert with id {raw} in the feed",
            "That alert is not in the feed.",
        )
    return 200, {
        "alert": alert,
        "acked": True,
        "speak": f"Alert {alert.get('id')} acknowledged.",
    }


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("POST", re.compile(r"^/v1/alerts$"), handle_post_alert))
ROUTES.append(("GET", re.compile(r"^/v1/alerts$"), handle_list_alerts))
ROUTES.append((
    "POST",
    re.compile(r"^/v1/alerts/(?P<id>[^/]+)/ack$"),
    handle_ack_alert,
))
