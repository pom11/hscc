"""Contract tests for push-device registration (``/v1/devices``, card N2).

Real loopback HTTP through the central auth gate, a tmp hscc root, and the
route's own backing seam for the test push. The roster rules are covered by
``hscc_daemon/tests/test_apns.py``; here the contract is auth, status codes,
idempotent refresh and "a token is never echoed".

Both POSTs are also confirm-gated (``_require_confirm`` in ``routes_devices``),
which the two directions of ``hscc-api/tests/test_contract_swift_routes.py``
enforce from the client side. The 409 half of that contract is pinned HERE,
server-side: an unconfirmed call must neither enroll a device nor send a push.
"""

import pytest

import api_server
import routes_devices

from tests.test_api import RunningServer

TOKEN_A = "ab" * 32


@pytest.fixture
def running(tmp_path):
    srv = RunningServer(hscc_dir=str(tmp_path))
    yield srv
    srv.close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


def post(running, token, body, path="/v1/devices"):
    return running.request(method="POST", path=path, token=token, body=body)


def get(running, token):
    return running.request(method="GET", path="/v1/devices", token=token)


def _device(**over):
    """A valid registration body. Carries ``confirm`` because the route is a
    mutation; every test that wants the *unconfirmed* shape drops or replaces
    that key explicitly (``confirm=False``, ``"confirm": "true"``) rather than
    relying on an implicit default."""
    body = {"confirm": True, "platform": "ios", "token": TOKEN_A,
            "bundle_id": "ro.parsec.hscc.ios", "env": "production"}
    body.update(over)
    return body


class FakeApns:
    """The test-push seam, with a call log so 'nothing was sent' is an
    assertion about the sender, not about the response body."""
    seen = []

    @staticmethod
    def send_test(hscc_dir=None):
        FakeApns.seen.append(hscc_dir)
        return {"sent": True, "reason": "sent", "sent_count": 2, "test": True}


@pytest.fixture
def fake_apns(monkeypatch):
    FakeApns.seen = []
    monkeypatch.setattr(routes_devices, "_backing_apns", lambda: FakeApns)
    yield FakeApns


def test_every_device_route_requires_a_token(running):
    status, payload = post(running, None, _device())
    assert status == 401 and payload["error"]["code"] == "unauthorized"

    status, payload = get(running, None)
    assert status == 401 and payload["error"]["code"] == "unauthorized"

    status, payload = post(running, None, {"confirm": True},
                           path="/v1/devices/test")
    assert status == 401 and payload["error"]["code"] == "unauthorized"


def test_register_then_refresh_is_idempotent_and_hides_the_token(running, token):
    status, first = post(running, token, _device())
    assert status == 201, first
    assert first["created"] is True
    assert first["count"] == 1
    device = first["device"]
    assert device["platform"] == "ios"
    assert device["bundle_id"] == "ro.parsec.hscc.ios"
    assert device["env"] == "production"
    assert device["id"] and first["speak"]
    assert TOKEN_A not in str(first)

    status, again = post(running, token, _device(env="development",
                                                 bundle_id="ro.parsec.hscc.dev"))
    assert status == 200, again
    assert again["created"] is False
    assert again["device"]["id"] == device["id"]
    assert again["device"]["env"] == "development"
    assert again["count"] == 1

    status, listing = get(running, token)
    assert status == 200
    assert listing["count"] == 1
    assert listing["devices"][0]["id"] == device["id"]
    assert TOKEN_A not in str(listing)
    assert listing["speak"]


def test_invalid_registration_is_400_and_stores_nothing(running, token):
    """Field validation runs AFTER the confirm gate (the ``_action_fields``
    order), so these bodies are all confirmed and still 400 on their field
    error. ``{"confirm": True}`` stays in the set: it proves the confirm flag
    alone is not what makes a body acceptable."""
    for body in (
        _device(platform="android"),
        _device(token="nope"),
        _device(bundle_id="not a bundle"),
        _device(env="staging"),
        {"confirm": True},
    ):
        status, payload = post(running, token, body)
        assert status == 400, (body, payload)
        assert payload["error"]["code"] == "bad_request"
    assert get(running, token)[1]["count"] == 0


def test_non_object_body_is_400(running, token):
    status, payload = running.request(
        method="POST", path="/v1/devices", token=token, body=["not", "object"])
    assert status == 400
    assert payload["error"]["code"] == "bad_request"


def test_registration_without_confirm_is_409_and_enrolls_nothing(running, token):
    """The server half of the confirm contract: the roster is untouched."""
    status, payload = post(running, token, _device(confirm=False))
    assert status == 409, payload
    assert payload["error"]["code"] == "confirm_required"
    assert "register a push device" in payload["error"]["message"]
    assert payload["error"]["speak"]

    listing = get(running, token)[1]
    assert listing["count"] == 0


def test_registration_confirm_must_be_the_boolean_true(running, token):
    """`confirm` is checked with ``is True``, so a truthy look-alike must not
    open the gate — the same strictness every other actions route pins."""
    for value in ("true", 1, None, {}):
        status, payload = post(running, token, _device(confirm=value))
        assert status == 409, (value, payload)
        assert payload["error"]["code"] == "confirm_required"
    assert get(running, token)[1]["count"] == 0


def test_test_push_without_confirm_is_409_and_sends_nothing(running, token,
                                                             fake_apns):
    """The worse half: a 409 here means the sender was never reached. Asserted
    on the seam's own call log, because a `sent: false` body could also mean
    'the sender ran and found no config'."""
    status, payload = post(running, token, {}, path="/v1/devices/test")
    assert status == 409, payload
    assert payload["error"]["code"] == "confirm_required"
    assert "send a test push" in payload["error"]["message"]
    assert payload["error"]["speak"]
    assert fake_apns.seen == []


def test_test_push_with_no_config_is_an_honest_200(running, token):
    status, payload = post(running, token, {"confirm": True},
                           path="/v1/devices/test")
    assert status == 200
    assert payload["sent"] is False
    assert payload["reason"] == "no_config"
    assert payload["test"] is True
    assert "not sent" in payload["speak"]


def test_test_push_uses_the_apns_seam_and_the_server_root(running, token,
                                                          fake_apns):
    status, payload = post(running, token, {"confirm": True},
                           path="/v1/devices/test")
    assert status == 200
    assert payload["sent"] is True and payload["sent_count"] == 2
    assert "2 device(s)" in payload["speak"]
    assert fake_apns.seen == [running.server.ctx.hscc_dir]
