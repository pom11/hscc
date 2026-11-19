"""Contract tests for the alert feed routes (``/v1/alerts``, card N1).

The route is the only writer ``hscc notify`` uses, so these tests drive it the
way the CLI and the apps do: real loopback HTTP through the central auth gate,
with a tmp hscc root. The store's own rules (dedupe, rate limit, bounds) are
covered in ``hscc_daemon/tests/test_alerts.py``; here the contract is the
status codes, the response shape, paging, and what is refused.

Coverage the card requires:
  * auth required (no bearer -> 401);
  * dedupe (same key/content -> 200 ``deduped``, one row);
  * rate limit (global cap -> 200 ``rate_limited``, nothing stored);
  * since-paging (exclusive, ascending, ``has_more``);
  * ack (marks seen and clears the key), and 400/404 envelopes.
"""

import pytest

import api_server
import routes_alerts
from hscc_daemon import alerts as alert_store

from tests.test_api import RunningServer


@pytest.fixture
def running(tmp_path):
    srv = RunningServer(hscc_dir=str(tmp_path))
    yield srv
    srv.close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


def post(running, token, body):
    return running.request(method="POST", path="/v1/alerts", token=token,
                           body=body)


def get(running, token, query=""):
    return running.request(method="GET", path="/v1/alerts" + query,
                           token=token)


def ack(running, token, alert_id):
    return running.request(method="POST",
                           path=f"/v1/alerts/{alert_id}/ack", token=token)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

def test_every_alert_route_requires_a_token(running):
    status, payload = running.request(
        method="POST", path="/v1/alerts", token=None,
        body={"title": "no", "body": "auth"},
    )
    assert status == 401 and payload["error"]["code"] == "unauthorized"

    status, payload = get(running, None)
    assert status == 401 and payload["error"]["code"] == "unauthorized"

    status, payload = running.request(
        method="POST", path="/v1/alerts/1/ack", token=None)
    assert status == 401 and payload["error"]["code"] == "unauthorized"


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #

def test_post_creates_a_persisted_alert(running, token):
    status, payload = post(running, token, {
        "title": "Approve a LIVE publish",
        "body": "example-project wants to deploy",
        "level": "action",
        "key": "live-publish/example-project",
    })
    assert status == 201, payload
    assert payload["created"] is True
    assert payload["deduped"] is False
    assert payload["rate_limited"] is False
    assert payload["speak"]
    alert = payload["alert"]
    assert alert["id"] == 1
    assert alert["level"] == "action"
    assert alert["key"] == "live-publish/example-project"
    assert alert["acked"] is False and alert["acked_at"] is None
    assert alert["ts"].endswith("Z")
    assert "push" not in payload

    status, feed = get(running, token)
    assert status == 200
    assert feed["count"] == 1
    assert feed["alerts"][0]["id"] == 1
    assert feed["latest_id"] == 1
    assert feed["speak"]


def test_default_level_is_info_and_body_is_optional(running, token):
    status, payload = post(running, token, {"title": "FYI"})
    assert status == 201
    assert payload["alert"]["level"] == "info"
    assert payload["alert"]["body"] == ""


def test_invalid_fields_are_400_and_store_nothing(running, token):
    for body in (
        {"title": ""},
        {"title": "x", "level": "noisy"},
        {"title": 42},
        {"title": "x", "body": 42},
        {"title": "x", "key": "k" * 201},
    ):
        status, payload = post(running, token, body)
        assert status == 400, (body, payload)
        assert payload["error"]["code"] == "bad_request"
    assert get(running, token)[1]["count"] == 0


def test_non_object_body_is_400(running, token):
    status, payload = running.request(
        method="POST", path="/v1/alerts", token=token, body=["not", "an", "object"])
    assert status == 400
    assert payload["error"]["code"] == "bad_request"


# --------------------------------------------------------------------------- #
# Dedupe
# --------------------------------------------------------------------------- #

def test_same_key_and_content_does_not_create_a_second_alert(running, token):
    body = {"title": "Node down", "body": "dgx-2", "level": "critical",
            "key": "node-down/dgx-2"}
    status, first = post(running, token, body)
    assert status == 201

    status, again = post(running, token, body)
    assert status == 200
    assert again["created"] is False
    assert again["deduped"] is True
    assert again["alert"]["id"] == first["alert"]["id"]

    assert get(running, token)[1]["count"] == 1


def test_changed_content_alerts_again(running, token, monkeypatch):
    # The store's rate limit is a separate rule; make this test about content.
    monkeypatch.setattr(alert_store, "PER_KEY_MIN_INTERVAL_S", 0)
    body = {"title": "Node down", "body": "dgx-2", "key": "node-down/dgx-2"}
    assert post(running, token, body)[0] == 201

    body["body"] = "dgx-3"
    status, second = post(running, token, body)
    assert status == 201
    assert second["alert"]["id"] == 2
    assert get(running, token)[1]["count"] == 2


# --------------------------------------------------------------------------- #
# Rate limit
# --------------------------------------------------------------------------- #

def test_global_rate_limit_suppresses_without_storing(running, token, monkeypatch):
    monkeypatch.setattr(alert_store, "GLOBAL_MAX_PER_WINDOW", 2)
    for i in range(2):
        assert post(running, token, {"title": f"alert {i}"})[0] == 201

    status, payload = post(running, token, {"title": "one too many"})
    assert status == 200
    assert payload["rate_limited"] is True
    assert payload["reason"] == "rate_limited_global"
    assert payload["alert"] is None
    assert payload["retry_after_s"] > 0
    assert get(running, token)[1]["count"] == 2


def test_per_key_rate_limit_reports_the_wait(running, token):
    body = {"title": "Flapping", "body": "up", "key": "flap"}
    assert post(running, token, body)[0] == 201

    body["body"] = "down"          # a changed condition, but too soon
    status, payload = post(running, token, body)
    assert status == 200
    assert payload["rate_limited"] is True
    assert payload["reason"] == "rate_limited_key"
    assert 0 < payload["retry_after_s"] <= alert_store.PER_KEY_MIN_INTERVAL_S
    assert get(running, token)[1]["count"] == 1


# --------------------------------------------------------------------------- #
# since paging
# --------------------------------------------------------------------------- #

def test_since_paging_is_exclusive_and_ascending(running, token):
    for i in range(5):
        assert post(running, token, {"title": f"alert {i}"})[0] == 201

    status, page = get(running, token, "?since=2&limit=2")
    assert status == 200
    assert [a["id"] for a in page["alerts"]] == [3, 4]
    assert page["since"] == 2
    assert page["next_since"] == 4
    assert page["latest_id"] == 5
    assert page["has_more"] is True

    status, rest = get(running, token, "?since=4&limit=2")
    assert [a["id"] for a in rest["alerts"]] == [5]
    assert rest["has_more"] is False

    status, empty = get(running, token, "?since=5")
    assert empty["alerts"] == []
    assert empty["next_since"] == 5


def test_default_read_is_the_newest_page(running, token):
    for i in range(3):
        post(running, token, {"title": f"alert {i}"})
    status, page = get(running, token, "?limit=2")
    assert status == 200
    assert [a["id"] for a in page["alerts"]] == [2, 3]
    assert page["has_more"] is True


@pytest.mark.parametrize("query", ["?since=nope", "?since=-1", "?limit=0",
                                   "?limit=nope"])
def test_bad_paging_queries_are_400(running, token, query):
    status, payload = get(running, token, query)
    assert status == 400, (query, payload)
    assert payload["error"]["code"] == "bad_request"


# --------------------------------------------------------------------------- #
# Ack
# --------------------------------------------------------------------------- #

def test_ack_marks_seen_and_clears_the_dedupe_slot(running, token, monkeypatch):
    monkeypatch.setattr(alert_store, "PER_KEY_MIN_INTERVAL_S", 0)
    body = {"title": "Broken", "body": "the proxy", "key": "proxy-broken",
            "level": "critical"}
    created = post(running, token, body)[1]["alert"]

    status, payload = ack(running, token, created["id"])
    assert status == 200
    assert payload["acked"] is True
    assert payload["alert"]["acked"] is True
    assert payload["alert"]["acked_at"].endswith("Z")
    assert payload["speak"]

    # Seen and cleared: the same condition recurring is news again.
    status, again = post(running, token, body)
    assert status == 201
    assert again["alert"]["id"] == 2


def test_ack_unknown_id_is_404_with_the_error_envelope(running, token):
    status, payload = ack(running, token, 99)
    assert status == 404
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["speak"]


def test_ack_non_numeric_id_is_400(running, token):
    status, payload = ack(running, token, "not-an-id")
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
