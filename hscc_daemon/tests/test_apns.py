"""APNs sender + device roster (card N2).

Hermetic: a fake transport stands in for APNs, a fake signer stands in for
openssl, and every config/roster lives under tmp_path. The one test that runs
the real ES256 signer generates a throwaway P-256 key and verifies the signature
with the system openssl — it never reads the operator's key.

Coverage the card requires:
  * JWT shape and refresh (reuse inside the window, new ``iat`` after it);
  * 410 / BadDeviceToken pruning, and non-prunable failures keeping the row;
  * no secrets (device token, JWT) in results or logs;
  * alert-only payload: title/body, sound for action/critical, thread-id = key;
  * registration is an idempotent refresh and validates its fields.
"""

import base64
import json
import logging
import os
import shutil
import subprocess

import pytest

from hscc_daemon import apns, devices

KEY_ID = "ABCDE12345"
TEAM_ID = "TEAM123456"
TOKEN_A = "ab" * 32
TOKEN_B = "cd" * 32

#: Captured before the autouse fake-signer fixture can replace it, so the real
#: ES256 test still exercises openssl.
REAL_SIGN_ES256 = apns._sign_es256


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.content = json.dumps(payload or {}).encode()

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class FakeTransport:
    """The minimal httpx.Client surface the sender uses."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def post(self, url, headers=None, content=None):
        self.calls.append({"url": url, "headers": dict(headers or {}),
                           "content": content})
        item = self.responses.pop(0) if self.responses else FakeResponse()
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


def _b64json(segment):
    raw = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(raw))


def _write_config(root, *, key_path=None, **over):
    key = root / "AuthKey.p8"
    key.write_text("-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n")
    cfg = {
        "key_id": KEY_ID,
        "team_id": TEAM_ID,
        "key_path": str(key_path or key),
    }
    cfg.update(over)
    (root / "apns.json").write_text(json.dumps(cfg))
    return cfg


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "hscc"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def _fake_signer(monkeypatch):
    """Never shell out to openssl from the sender tests."""
    monkeypatch.setattr(apns, "_sign_es256",
                        lambda key, data: b"\x05" * 64)


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #

def test_jwt_shape_and_refresh_window():
    cfg = {"key_id": KEY_ID, "team_id": TEAM_ID}
    clock = [1_800_000_000.0]
    signed = []
    provider = apns.TokenProvider(
        cfg,
        signer=lambda key, data: signed.append(data) or b"\x07" * 64,
        key_loader=lambda _cfg: b"key",
        now=lambda: clock[0],
    )

    token = provider.token()
    header_b64, claims_b64, sig_b64 = token.split(".")
    assert _b64json(header_b64) == {"alg": "ES256", "kid": KEY_ID}
    claims = _b64json(claims_b64)
    assert claims == {"iss": TEAM_ID, "iat": int(clock[0])}
    assert len(base64.urlsafe_b64decode(sig_b64 + "==")) == 64
    assert signed[-1] == f"{header_b64}.{claims_b64}".encode()

    # Reused inside APNs' one-hour window (and not refreshed early).
    clock[0] += 60
    assert provider.token() == token
    clock[0] = 1_800_000_000.0 + apns.JWT_REFRESH_S - 1
    assert provider.token() == token

    # Refreshed after the reuse window, with a new iat and signature input.
    clock[0] = 1_800_000_000.0 + apns.JWT_REFRESH_S
    fresh = provider.token()
    assert fresh != token
    assert _b64json(fresh.split(".")[1])["iat"] == int(clock[0])
    assert signed[-1] != signed[0]


def test_real_es256_signature_verifies_with_openssl(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl is not available")
    key = tmp_path / "key.p8"
    pub = tmp_path / "pub.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "EC",
         "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(key)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub)],
        check=True, capture_output=True,
    )

    message = b"eyJhbGciOiJFUzI1NiJ9.eyJpc3MiOiJURUFNMTIzNDU2In0"
    raw = REAL_SIGN_ES256(key.read_bytes(), message)
    assert len(raw) == 64

    # Re-encode R||S as DER so openssl's verifier can consume it.
    def _int(value):
        body = value.to_bytes(32, "big").lstrip(b"\x00") or b"\x00"
        if body[0] & 0x80:
            body = b"\x00" + body
        return b"\x02" + bytes([len(body)]) + body

    der = _int(int.from_bytes(raw[:32], "big")) + _int(
        int.from_bytes(raw[32:], "big"))
    der = b"\x30" + bytes([len(der)]) + der
    sig_file = tmp_path / "sig.der"
    sig_file.write_bytes(der)
    data_file = tmp_path / "msg"
    data_file.write_bytes(message)
    result = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(pub),
         "-signature", str(sig_file), str(data_file)],
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------------------------------------- #
# Device roster
# --------------------------------------------------------------------------- #

def test_register_is_an_idempotent_refresh(root):
    first = devices.register("ios", TOKEN_A, "ro.parsec.hscc.ios",
                             "development", hscc_dir=str(root), now=100)
    assert first["created"] is True
    assert first["count"] == 1
    device = first["device"]
    assert device["id"] and device["platform"] == "ios"
    assert "token" not in device

    again = devices.register("IOS", TOKEN_A.upper(), "ro.parsec.hscc.ios",
                             "production", hscc_dir=str(root), now=200)
    assert again["created"] is False
    assert again["count"] == 1
    assert again["device"]["id"] == device["id"]
    assert again["device"]["env"] == "production"
    assert devices.load(str(root))[0]["registered_at"] == device["registered_at"]
    assert devices.load(str(root))[0]["updated_at"] != device["registered_at"]


@pytest.mark.parametrize("kwargs", [
    {"platform": "android", "token": TOKEN_A, "bundle_id": "ro.parsec.hscc",
     "env": "production"},
    {"platform": "ios", "token": "not-hex", "bundle_id": "ro.parsec.hscc",
     "env": "production"},
    {"platform": "ios", "token": TOKEN_A, "bundle_id": "not a bundle",
     "env": "production"},
    {"platform": "ios", "token": TOKEN_A, "bundle_id": "ro.parsec.hscc",
     "env": "staging"},
])
def test_register_refuses_bad_fields(root, kwargs):
    with pytest.raises(ValueError):
        devices.register(hscc_dir=str(root), **kwargs)
    assert devices.load(str(root)) == []


def test_prune_removes_exactly_the_named_tokens(root):
    devices.register("ios", TOKEN_A, "ro.parsec.hscc.ios", "production",
                     hscc_dir=str(root))
    devices.register("macos", TOKEN_B, "ro.parsec.hscc.mac", "production",
                     hscc_dir=str(root))
    assert devices.prune_tokens([TOKEN_A], hscc_dir=str(root)) == 1
    kept = devices.load(str(root))
    assert [d["token"] for d in kept] == [TOKEN_B]
    assert all("token" not in d for d in devices.public_list(str(root))["devices"])


# --------------------------------------------------------------------------- #
# Sender
# --------------------------------------------------------------------------- #

def _register_both(root):
    devices.register("ios", TOKEN_A, "ro.parsec.hscc.ios", "development",
                     hscc_dir=str(root))
    devices.register("macos", TOKEN_B, "ro.parsec.hscc.mac", "production",
                     hscc_dir=str(root))


def test_send_pushes_alert_only_payload_to_every_device(root):
    _write_config(root)
    _register_both(root)
    transport = FakeTransport([FakeResponse(200), FakeResponse(200)])
    alert = {"id": 3, "title": "Approve LIVE publish",
             "body": "example-project wants to deploy", "level": "info",
             "key": "live/example-project"}

    result = apns.send_alert(alert, hscc_dir=str(root), transport=transport)

    assert result["sent"] is True
    assert result["sent_count"] == 2 and result["pruned"] == 0
    assert len(transport.calls) == 2
    by_token = {}
    for call in transport.calls:
        for token in (TOKEN_A, TOKEN_B):
            if token in call["url"]:
                by_token[token] = call
    assert by_token[TOKEN_A]["url"].startswith(
        apns.DEVELOPMENT_HOST + "/3/device/" + TOKEN_A)
    assert by_token[TOKEN_B]["url"].startswith(
        apns.PRODUCTION_HOST + "/3/device/" + TOKEN_B)
    for call in transport.calls:
        headers = call["headers"]
        assert headers["authorization"].startswith("bearer ")
        assert headers["apns-push-type"] == "alert"
        assert headers["apns-priority"] == "10"
        assert headers["apns-topic"].startswith("ro.parsec.hscc")
        aps = json.loads(call["content"])["aps"]
        assert aps["alert"] == {"title": "Approve LIVE publish",
                                "body": "example-project wants to deploy"}
        assert aps["thread-id"] == "live/example-project"
        assert "sound" not in aps


def test_action_and_critical_alerts_carry_sound(root):
    _write_config(root)
    devices.register("ios", TOKEN_A, "ro.parsec.hscc.ios", "production",
                     hscc_dir=str(root))
    transport = FakeTransport([FakeResponse(200)])
    apns.send_alert({"id": 1, "title": "Broken", "body": "proxy",
                     "level": "critical", "key": "proxy-broken"},
                    hscc_dir=str(root), transport=transport)
    aps = json.loads(transport.calls[0]["content"])["aps"]
    assert aps["sound"] == "default"
    assert aps["thread-id"] == "proxy-broken"


def test_410_and_bad_device_token_prune_the_registration(root):
    _write_config(root)
    _register_both(root)
    transport = FakeTransport([
        FakeResponse(200),
        FakeResponse(410, {"reason": "Unregistered"}),
    ])
    result = apns.send_alert({"id": 1, "title": "T", "body": "B"},
                             hscc_dir=str(root), transport=transport)
    assert result["sent"] is True and result["pruned"] == 1
    assert [d["token"] for d in devices.load(str(root))] == [TOKEN_A]

    transport = FakeTransport([FakeResponse(400, {"reason": "BadDeviceToken"})])
    result = apns.send_alert({"id": 2, "title": "T", "body": "B"},
                             hscc_dir=str(root), transport=transport)
    assert result["sent"] is False and result["pruned"] == 1
    assert devices.load(str(root)) == []


def test_transient_failure_keeps_the_registration(root):
    _write_config(root)
    devices.register("ios", TOKEN_A, "ro.parsec.hscc.ios", "production",
                     hscc_dir=str(root))
    transport = FakeTransport([FakeResponse(503, {"reason": "ServiceUnavailable"})])
    result = apns.send_alert({"id": 1, "title": "T", "body": "B"},
                             hscc_dir=str(root), transport=transport)
    assert result["sent"] is False and result["pruned"] == 0
    assert [d["token"] for d in devices.load(str(root))] == [TOKEN_A]


def test_no_config_and_no_devices_are_explicit_and_do_not_call_transport(root):
    transport = FakeTransport()
    assert apns.send_alert({"title": "T"}, hscc_dir=str(root),
                           transport=transport)["reason"] == "no_config"
    assert transport.calls == []

    _write_config(root)
    result = apns.send_alert({"title": "T"}, hscc_dir=str(root),
                             transport=transport)
    assert result["reason"] == "no_devices"
    assert transport.calls == []


def test_topics_config_controls_the_apns_topic(root):
    _write_config(root, topics={"ios": "ro.parsec.hscc.ios.push",
                                "macos": "ro.parsec.hscc.mac.push"})
    _register_both(root)
    transport = FakeTransport([FakeResponse(200), FakeResponse(200)])
    apns.send_alert({"title": "T", "body": "B"}, hscc_dir=str(root),
                    transport=transport)
    topics = {c["headers"]["apns-topic"] for c in transport.calls}
    assert topics == {"ro.parsec.hscc.ios.push", "ro.parsec.hscc.mac.push"}


def test_test_push_uses_the_same_sender(root):
    _write_config(root)
    devices.register("ios", TOKEN_A, "ro.parsec.hscc.ios", "production",
                     hscc_dir=str(root))
    transport = FakeTransport([FakeResponse(200)])
    result = apns.send_test(hscc_dir=str(root), transport=transport)
    assert result["test"] is True and result["sent"] is True
    aps = json.loads(transport.calls[0]["content"])["aps"]
    assert aps["alert"]["title"] == apns.TEST_ALERT["title"]
    assert aps["sound"] == "default"


def test_no_secret_is_logged_or_returned(root, caplog):
    _write_config(root)
    devices.register("ios", TOKEN_A, "ro.parsec.hscc.ios", "production",
                     hscc_dir=str(root))
    transport = FakeTransport([
        FakeResponse(400, {"reason": "BadDeviceToken"}),
    ])
    caplog.set_level(logging.DEBUG)
    result = apns.send_alert({"id": 9, "title": "T", "body": "B", "key": "k"},
                             hscc_dir=str(root), transport=transport)

    blob = json.dumps(result)
    for secret in (TOKEN_A, "eyJhbGciOiJFUzI1NiJ9", "bearer "):
        assert secret not in blob
        assert secret not in caplog.text
    assert "BadDeviceToken" in caplog.text  # the reason is useful, the token is not


# --------------------------------------------------------------------------- #
# Doctor
# --------------------------------------------------------------------------- #

def test_doctor_warns_with_the_one_time_steps_when_unconfigured(root):
    row = apns.doctor_check(hscc_dir=str(root))
    assert row["name"] == apns.CHECK_NAME
    assert row["ok"] is False and row["fatal"] is False
    assert "no APNs key configured" in row["detail"]
    assert "Apple developer portal" in row["fix"]
    assert "Keys" in row["fix"] and "APNs" in row["fix"]
    assert "ro.parsec.hscc.*" in row["fix"]


def test_doctor_reports_configured_key_and_missing_key_file(root):
    _write_config(root)
    row = apns.doctor_check(hscc_dir=str(root))
    assert row["ok"] is True
    assert "APNs key configured" in row["detail"]

    os.unlink(root / "AuthKey.p8")
    row = apns.doctor_check(hscc_dir=str(root))
    assert row["ok"] is False and row["fatal"] is False
    assert "unusable" in row["detail"]


def test_doctor_reports_malformed_config_without_crashing(root):
    (root / "apns.json").write_text("{ not json")
    row = apns.doctor_check(hscc_dir=str(root))
    assert row["ok"] is False and row["fatal"] is False
    assert "not valid JSON" in row["detail"]
