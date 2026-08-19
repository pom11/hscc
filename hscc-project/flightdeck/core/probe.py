"""probe.py — shared service probing for flightdeck.

A single place that knows how to check that a service is UP, without ever
probing an endpoint with a method it does not accept. This rule has now been
broken THREE times in this repo, each time producing a false "unreachable"
report against a healthy endpoint:

  - ``init`` bare-GET'd the Telegram MCP HTTP daemon (reported a healthy
    daemon as MISSING);
  - INST2 fixed that one with a real MCP handshake;
  - D2 reintroduced it by GETting a vLLM ``/v1/chat/completions`` URL — which
    is POST-only — so a healthy endpoint read as UNVERIFIED "unreachable".

The rules encoded here are shared by every flightdeck command that probes a
remote service:

  * An HTTP response of ANY status — 200, 404, 405, 422 — proves the endpoint
    is REACHABLE. Only a transport-level failure (connection refused, DNS
    lookup failure, timeout) means "unreachable". A 405 means "wrong method",
    which is a bug in the PROBE, never a fault in the endpoint.
  * GET a models URL; POST to a chat-completions URL. Never the other way
    round.

Two commands independently hand-rolling reachability probes is exactly how
this bug recurred; everything routes through this module so the one place
that knows how to classify up-vs-down is never duplicated.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

# A probe's verdict on whether the target node is up. UNREACHABLE is reserved
# for transport-level failure: nothing is listening, DNS cannot resolve, or a
# timeout. Any HTTP response at all — even 404/405/422 — is REACHABLE.
REACHABLE = "reachable"
UNREACHABLE = "unreachable"

# The trailing path segment that marks an OpenAI-compatible CHAT completions
# URL. Stripped (including the leading ``/``) and replaced with ``/models`` to
# get the models-list URL, which IS safe to GET. A chat-completions URL itself
# is POST-only and must never be GET'd.
_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"


class ProbeError(Exception):
    """A transport-level failure reaching the endpoint (not an HTTP error).

    Raised only when nothing answered: connection refused, DNS lookup failure,
    or a timeout. An HTTP error response (404/405/422/...) is a *successful*
    HTTP exchange and is never this error.
    """


def derive_models_url(url: str) -> str | None:
    """The /models URL serving a chat-completions ``url``, or None.

    Replaces the trailing ``/chat/completions`` with ``/models`` so the caller
    GETs the models-list endpoint (GET-safe) instead of GETting the POST-only
    chat-completions endpoint. Returns None when ``url`` does not match the
    expected chat-completions shape — the caller must then POST to ``url``
    directly, never GET it.
    """
    base = url.rstrip("/")
    if base.endswith(_CHAT_COMPLETIONS_SUFFIX):
        return base[: -len(_CHAT_COMPLETIONS_SUFFIX)] + "/models"
    return None


def is_connection_refused(exc: BaseException) -> bool:
    """True when the exception chain signals nothing is listening.

    Walks ``__cause__`` / ``__context__`` and recurses into exception groups
    so a connection-refused surfaced through an HTTP/transport layer (an
    ``httpx`` / ``httpcore`` error, or an ``ExceptionGroup`` wrapping the
    socket ``ConnectionRefusedError``) is still recognised as "nothing is
    listening" — the difference between [MISSING] and [UNVERIFIED].
    """
    seen: set[int] = set()

    def _walk(node: BaseException | None) -> bool:
        if node is None or id(node) in seen:
            return False
        seen.add(id(node))
        if isinstance(node, (ConnectionRefusedError, ConnectionError)):
            return True
        # Recurse into exception-group sub-exceptions.
        for sub in getattr(node, "exceptions", None) or ():
            if _walk(sub):
                return True
        return _walk(node.__cause__) or _walk(node.__context__)

    return _walk(exc)


def probe_http(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    timeout: float = 5.0,
    _urlopen=None,
) -> tuple[str, int | None, object | None]:
    """Issue one HTTP request and classify the endpoint's reachability.

    Returns ``(status, resp_status, payload)``:

      status:      ``REACHABLE`` when we received ANY HTTP response (any status
                   code, including 404/405/422); ``UNREACHABLE`` only on a
                   transport-level failure (connection refused / DNS / timeout).
      resp_status: the HTTP status code, or ``None`` when unreachable.
      payload:     the parsed JSON response body, or ``None`` when it was not
                   JSON / wasn't a response / unreachable.

    Never raises. ``_urlopen`` is injectable so tests never touch the network;
    it follows ``urllib.request.urlopen``'s contract (a context manager whose
    object exposes ``.status`` and ``.read()``). ``method`` is passed to the
    underlying request verbatim so the caller always controls the verb.
    """
    open_fn = _urlopen if _urlopen is not None else urllib.request.urlopen
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with open_fn(request, timeout=timeout) as resp:
            resp_status: int | None = getattr(resp, "status", None)  # type: ignore[assignment]
            if resp_status is None:
                getcode = getattr(resp, "getcode", None)
                if callable(getcode):
                    resp_status = getcode()  # type: ignore[assignment]
            payload: object | None = None
            try:
                payload = json.loads(resp.read().decode("utf-8"))
            except Exception:
                payload = None
            return REACHABLE, resp_status, payload
    except urllib.error.HTTPError as exc:
        # An HTTP error response is still a response — the endpoint is UP, and
        # a 405 means the PROBE used the wrong method, not that the endpoint is
        # down. Never collapse this into UNREACHABLE.
        return REACHABLE, exc.code, None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        # connection refused / DNS failure / timeout — nothing answered.
        return UNREACHABLE, None, None
