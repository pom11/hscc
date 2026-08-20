"""Tests for flightdeck.core.probe — the shared service-probing helper.

The SINGLE place that knows how to check whether a service is UP and how to
classify reachability without ever probing an endpoint with a method it does
not accept. These tests are the regression guard for a bug that shipped THREE
times (init -> bare-GET of the Telegram MCP daemon, INST2 -> real handshake,
then D2 -> GET of a vLLM chat-completions URL): a healthy endpoint misreported
as "unreachable" because the probe used the wrong HTTP method.

Nothing here touches the network: urllib is injected via ``_urlopen`` fakes.
"""

from __future__ import annotations

import json
import urllib.error

try:
    from exceptiongroup import ExceptionGroup  # PEP 654 backport on Python 3.10
except ModuleNotFoundError:  # stdlib/builtin from Python 3.11
    pass

from flightdeck.core import probe


# --------------------------------------------------------------------------- #
# Fakes for urllib.request.urlopen (the ``_urlopen`` injection point)
# --------------------------------------------------------------------------- #

class FakeResponse:
    """A urllib-style response object exposing ``status`` and ``read()``."""

    def __init__(self, status: int = 200, payload: bytes = b"{}"):
        self.status = status
        self._payload = payload

    def getcode(self):
        return self.status

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _urlopen_returning(payload_dict, status=200):
    """A fake ``_urlopen`` returning a JSON payload with the given status."""
    body = json.dumps(payload_dict).encode("utf-8")

    def _fake(request, timeout=5.0):  # noqa: ARG001 - mirrors urlopen signature
        return FakeResponse(status=status, payload=body)

    return _fake


def _urlopen_raising_http(status):
    """A fake ``_urlopen`` raising HTTPError — urllib's real behaviour for 4xx/5xx."""

    def _fake(request, timeout=5.0):  # noqa: ARG001 - mirrors urlopen signature
        raise urllib.error.HTTPError(request.full_url, status, "boom", {}, None)

    return _fake


def _urlopen_refused():
    """A fake ``_urlopen`` raising a connection-refused URLError (nothing listening)."""

    def _fake(request, timeout=5.0):  # noqa: ARG001 - mirrors urlopen signature
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    return _fake


class _RecordingURL:
    """Captures the method + URL of the request the probe actually issued."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request, timeout=5.0):  # noqa: ARG001
        self.calls.append((request.method, request.full_url))
        return FakeResponse(status=200, payload=b"{}")


# --------------------------------------------------------------------------- #
# Models URL derivation
# --------------------------------------------------------------------------- #

def test_models_url_derived_from_chat_completions_url():
    """The /models URL is derived by replacing the trailing /chat/completions."""
    assert probe.derive_models_url(
        "http://10.0.0.244:8000/v1/chat/completions"
    ) == "http://10.0.0.244:8000/v1/models"


def test_models_url_derivation_handles_trailing_slash():
    assert probe.derive_models_url(
        "http://host:8000/v1/chat/completions/"
    ) == "http://host:8000/v1/models"


def test_models_url_cannot_be_derived_from_non_chat_shape():
    """A URL that is not a chat-completions URL yields None — the caller must
    then POST to it, never GET it."""
    assert probe.derive_models_url("http://host:8000/v1/models") is None
    assert probe.derive_models_url("http://host:8000/other") is None
    assert probe.derive_models_url("http://host:8000/v1/embeddings") is None


# --------------------------------------------------------------------------- #
# Reachability classification — the heart of the third-occurrence fix
# --------------------------------------------------------------------------- #

def test_post_only_endpoint_405_to_get_is_reachable_never_unreachable():
    """THIRD-OCCURRENCE regression guard.

    A POST-only endpoint answering 405 to a GET proves it is UP — it must be
    classified REACHABLE, never UNREACHABLE. A 405 means the PROBE used the
    wrong method (a bug in the probe), not a fault in the endpoint. Collapsing
    it into "unreachable" is exactly the false negative that shipped twice.
    """
    status, resp_status, payload = probe.probe_http(
        "http://host:8000/v1/chat/completions",
        method="GET",
        _urlopen=_urlopen_raising_http(405),
    )
    assert status == probe.REACHABLE
    assert resp_status == 405
    assert payload is None


def test_any_http_error_status_proves_reachable():
    """404 / 422 / any HTTP response means the endpoint is up — not unreachable."""
    for code in (404, 422, 500):
        status, resp_status, _ = probe.probe_http(
            "http://host:8000/x", method="GET", _urlopen=_urlopen_raising_http(code)
        )
        assert status == probe.REACHABLE
        assert resp_status == code


def test_connection_refused_is_unreachable():
    """Only a transport-level failure (nothing listening) is UNREACHABLE."""
    status, resp_status, payload = probe.probe_http(
        "http://host:8000/x", method="GET", _urlopen=_urlopen_refused()
    )
    assert status == probe.UNREACHABLE
    assert resp_status is None
    assert payload is None


def test_probe_http_parses_json_payload():
    status, resp_status, payload = probe.probe_http(
        "http://host:8000/v1/models",
        method="GET",
        _urlopen=_urlopen_returning({"data": [{"id": "gpt-4o"}, {"id": "other"}]}),
    )
    assert status == probe.REACHABLE
    assert resp_status == 200
    assert payload == {"data": [{"id": "gpt-4o"}, {"id": "other"}]}


def test_probe_http_passes_method_and_data_to_urllib():
    """The exact verb/body the caller asks for is forwarded verbatim, so the
    caller (not the helper) decides GET vs POST and the helper never invents a
    method."""
    rec = _RecordingURL()
    probe.probe_http(
        "http://host:8000/v1/chat/completions",
        method="POST",
        data=b'{"probe":true}',
        _urlopen=rec,
    )
    assert rec.calls == [("POST", "http://host:8000/v1/chat/completions")]


# --------------------------------------------------------------------------- #
# connection-refused classification (moved from init so it is SHARED)
# --------------------------------------------------------------------------- #

def test_is_connection_refused_walks_cause_and_context():
    err = RuntimeError("wrapped")
    err.__cause__ = ConnectionRefusedError("refused")
    assert probe.is_connection_refused(err) is True


def test_is_connection_refused_recurse_exception_group():
    group = ExceptionGroup(
        "mcp transport", [ConnectionRefusedError("refused")]
    )
    assert probe.is_connection_refused(group) is True


def test_is_connection_refused_false_for_protocol_error():
    assert probe.is_connection_refused(RuntimeError("400 Bad Request")) is False
