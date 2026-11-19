"""Contract tests for push-device registration (``/v1/devices``, card N2).

Real loopback HTTP through the central auth gate, a tmp hscc root, and the
route's own backing seam for the test push. The roster rules are covered by
``hscc_daemon/tests/test_apns.py``; here the contract is auth, status codes,
idempotent refresh and "a token is never echoed".
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
    body = {"platform": "ios", "token": TOKEN_A,
            "bundle_id": "ro.parsec.hscc.ios", "env": "production"}
    body.update(over)
    return body


def test_every_device_route_requires_a_token(running):
    status, payload = post(running, None, _device())
    assert status == 401 and payload["error"]["code"] == "unauthorized"

    status, payload = get(running, None)
    assert status == 401 and payload["error"]["code"] == "unauthorized"

    status, payload = post(running, None, {}, path="/v1/devices/test")
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
    for body in (
        _device(platform="android"),
        _device(token="nope"),
        _device(bundle_id="not a bundle"),
        _device(env="staging"),
        {},
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


def test_test_push_with_no_config_is_an_honest_200(running, token):
    status, payload = post(running, token, {}, path="/v1/devices/test")
    assert status == 200
    assert payload["sent"] is False
    assert payload["reason"] == "no_config"
    assert payload["test"] is True
    assert "not sent" in payload["speak"]


def test_test_push_uses_the_apns_seam_and_the_server_root(running, token,
                                                         monkeypatch):
    class FakeApns:
        seen = []

        @staticmethod
        def send_test(hscc_dir=None):
            FakeApns.seen.append(hscc_dir)
            return {"sent": True, "reason": "sent", "sent_count": 2,
                    "test": True}

    monkeypatch.setattr(routes_devices, "_backing_apns", lambda: FakeApns)
    status, payload = post(running, token, {}, path="/v1/devices/test")
    assert status == 200
    assert payload["sent"] is True and payload["sent_count"] == 2
    assert "2 device(s)" in payload["speak"]
    assert FakeApns.seen == [running.server.ctx.hscc_dir]
