"""Push sender seam for the alert feed — APNs (card N2).

``hscc_daemon.alerts`` persists every created alert and then offers it here,
once, after the write. This module is the ONE place the feed reaches the
operator's devices; the transport itself lives in :mod:`hscc_daemon.apns`.

Contract
========
* input is the public alert dict (``id``, ``ts``, ``title``, ``body``,
  ``level``, ``key``, ``acked``, ``acked_at``) — treat it as read-only;
  ``hscc_dir`` is an optional keyword so a sandboxed API/test resolves the
  roster and APNs config under its own root instead of the live ``~/.hscc``;
* return a JSON-able dict describing what happened, at least
  ``{"sent": bool, "reason": str}`` (plus transport detail as useful);
* never raise for a delivery failure and never retry inside this call: a
  failed push must not fail the alert request, and the feed is still the
  durable record. Queueing/retry belongs to the sender's own state;
* credentials (APNs key, team id, device tokens) live outside the repo —
  ``~/.hscc`` or the keychain — never in this file.

With no APNs config or no registered device the answer is explicit
(``sent: false`` with ``reason: "no_config"`` / ``"no_devices"``), never an
empty ``{}`` that would let a response claim a push was attempted.
"""

from __future__ import annotations

__all__ = ["send", "SENDER_READY"]

#: The APNs transport is real as of card N2.
SENDER_READY = True


def send(alert: dict, hscc_dir=None) -> dict:
    """Push one created alert to the registered devices (never raises)."""
    try:
        from . import apns
        return apns.send_alert(alert, hscc_dir=hscc_dir)
    except Exception as exc:  # noqa: BLE001 - the feed must survive any sender
        return {
            "sent": False,
            "reason": "sender_error",
            "detail": type(exc).__name__,
        }
