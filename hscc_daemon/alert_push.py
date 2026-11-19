"""Push sender seam for the alert feed — intentionally a no-op today.

``hscc_daemon.alerts`` persists every created alert and then offers it here,
once, after the write. This module is the ONE place that knows how to reach
the operator's devices; card N2 (APNs sender + device registration) fills it in.
Keeping the seam separate means the feed, the CLI and the API never grow a
second delivery path, and N2 can change transport without touching the store.

Contract for the implementation that replaces :func:`send`:

* input is the public alert dict (``id``, ``ts``, ``title``, ``body``,
  ``level``, ``key``, ``acked``, ``acked_at``) — treat it as read-only;
* return a JSON-able dict describing what happened, at least
  ``{"sent": bool, "reason": str}`` (plus transport detail as useful);
* never raise for a delivery failure and never retry inside this call: a
  failed push must not fail the alert request, and the feed is still the
  durable record. Queueing/retry belongs to the sender's own state;
* credentials (APNs key, team id, device tokens) live outside the repo —
  ``~/.hscc`` or the keychain — never in this file.

The current answer is deliberately explicit: ``sent: false`` with
``reason: "no_transport"``. An empty ``{}`` would let a response claim push
was attempted when nothing was.
"""

from __future__ import annotations

__all__ = ["send", "SENDER_READY"]

#: False until N2 lands a real transport. Surfaces can say "no push transport"
#: from this flag instead of guessing from a response shape.
SENDER_READY = False


def send(alert: dict) -> dict:
    """No-op push: the alert is already persisted by the caller."""
    return {
        "sent": False,
        "reason": "no_transport",
        "detail": "push sender not configured (card N2)",
    }
