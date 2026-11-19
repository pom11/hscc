"""HSCC HTTP API — push device registration (``/v1/devices``, card N2).

The iOS/macOS apps register their APNs token here; the daemon's sender then
pushes every new alert in the feed (``/v1/alerts``) to every row. The roster and
its rules live in ``hscc_daemon.devices`` so the API, the sender and the tests
share one definition.

    POST /v1/devices        register or refresh one device
    GET  /v1/devices        the roster WITHOUT tokens
    POST /v1/devices/test   send the ``hscc notify test`` push

Every route sits behind the server's single bearer-token gate; there is no
unauthenticated device path. Both POSTs sit behind the API's confirm gate as
well — ``"confirm": true`` in the body or the answer is ``409``, the same
contract every other app POST keeps (see ``_require_confirm`` below for why
these two are mutations and not exempt). Registration is idempotent per
``(platform, token)`` — the app calls it on launch, and a rebuilt app id or a
sandbox -> production move refreshes the row instead of duplicating it.

The response never contains the device token. The public ``id`` is a derived
stable identifier (a hash prefix) the operator can use to tell rows apart.
"""

from __future__ import annotations

import json
import re

from api_server import ApiError, ROUTES


# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch these in tests)
# --------------------------------------------------------------------------- #

def _backing_devices():
    from hscc_daemon import devices
    return devices


def _backing_apns():
    from hscc_daemon import apns
    return apns


def _store_dir(ctx):
    return getattr(ctx, "hscc_dir", None) or None


def _bad_request(message, speak=None):
    return ApiError(400, "bad_request", message, speak or message)


def _parse_body(body):
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise _bad_request("request body must be JSON")
    if not isinstance(data, dict):
        raise _bad_request("body must be a JSON object")
    return data


def _require_confirm(data: dict, what: str) -> None:
    """Return None (pass) if ``confirm`` is exactly true, else raise 409.

    The confirm gate is the Actions contract's whole point, and it is enforced
    in BOTH directions by ``hscc-api/tests/test_contract_swift_routes.py``:
    ``test_every_mutating_post_carries_confirm`` proves the iOS/macOS client
    sends ``"confirm": true``, and
    ``test_every_client_post_server_handler_is_confirm_gated`` proves this
    module's handlers actually CHECK it. Both routes the apps POST to are
    mutations, so both are gated:

      * ``/v1/devices`` writes a durable roster row — it ENROLLS a recipient of
        every future alert push, and a caller holding the API token gets to
        choose which device that is;
      * ``/v1/devices/test`` fires a real APNs push at EVERY registered device,
        i.e. it buzzes the operator's phone and Mac.

    An exemption here would be a hole in a closed set: the gate's value is that
    it covers every client POST, and the first named exception is the last
    route the next author has to think about. Wording is module-specific (like
    every other ``_require_confirm`` in this package) because "destructive" is
    not what these two do — they mutate and they notify.
    """
    if data.get("confirm") is True:
        return
    raise ApiError(
        409, "confirm_required",
        f"this endpoint mutates state and requires \"confirm\": true in the "
        f"request body to {what}",
        f"Confirmation required to {what}.",
    )


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

def handle_register_device(server, ctx, query, body):
    """Register/refresh one device; 201 new, 200 refreshed, 409 without confirm.

    The confirm gate runs BEFORE the roster write, so an unconfirmed call cannot
    enroll anything — it answers the same 409 as every other app POST.
    """
    body = _parse_body(body)
    _require_confirm(body, "register a push device")
    try:
        result = _backing_devices().register(
            platform=body.get("platform"),
            token=body.get("token"),
            bundle_id=body.get("bundle_id"),
            env=body.get("env"),
            hscc_dir=_store_dir(ctx),
        )
    except ValueError as exc:
        raise _bad_request(str(exc), "The device was not registered.") from exc

    device = result.get("device") or {}
    created = bool(result.get("created"))
    speak = (
        f"Device {device.get('id')} registered."
        if created else
        f"Device {device.get('id')} refreshed."
    )
    return (201 if created else 200), {
        "device": device,
        "created": created,
        "count": result.get("count", 0),
        "speak": speak,
    }


def handle_list_devices(server, ctx, query, body):
    """The roster without tokens (operator/app diagnostic surface)."""
    result = _backing_devices().public_list(hscc_dir=_store_dir(ctx))
    count = result.get("count", 0)
    result["speak"] = (
        f"{count} device{'s' if count != 1 else ''} registered."
        if count else "No push devices registered."
    )
    return 200, result


def handle_test_push(server, ctx, query, body):
    """Send the test push through the same sender as a real alert.

    Delivery failure is not an HTTP failure: the request was understood and the
    answer says why nothing was sent (``no_config`` / ``no_devices`` /
    ``all_failed``), exactly like alert suppression. ``hscc notify test`` turns
    that into a non-zero exit for a human.

    The push itself is the reason this route is confirm-gated: it buzzes every
    registered device, so nothing is sent until the caller has said so with
    ``"confirm": true`` — which the app always does and which ``hscc notify
    test`` sends on the operator's behalf.
    """
    body = _parse_body(body)
    _require_confirm(body, "send a test push")
    result = _backing_apns().send_test(hscc_dir=_store_dir(ctx))
    if result.get("sent"):
        speak = f"Test push sent to {result.get('sent_count', 0)} device(s)."
    else:
        reason = result.get("reason") or "not_sent"
        detail = result.get("detail")
        speak = f"Test push not sent ({reason})" + (f": {detail}" if detail else "")
    result["speak"] = speak
    return 200, result


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("POST", re.compile(r"^/v1/devices$"), handle_register_device))
ROUTES.append(("GET", re.compile(r"^/v1/devices$"), handle_list_devices))
ROUTES.append(("POST", re.compile(r"^/v1/devices/test$"), handle_test_push))
