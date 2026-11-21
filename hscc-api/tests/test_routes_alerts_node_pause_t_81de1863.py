"""``POST /v1/alerts`` must not create a Needs-me row for a parked node
(t_81de1863).

The store's rules live in ``hscc_daemon/tests/test_alerts_node_pause_t_81de1863.py``;
this file is the HTTP CONTRACT of the same seam, driven the way ``hscc notify``
and the apps drive it — real loopback HTTP through the auth gate, tmp feed root.
It pins the two things only the route can get wrong: the STATUS (a refusal is
200 with ``paused``, not 201 with a half-built alert) and the fact that a
caller who does not name a node sees exactly yesterday's behaviour.

SAFETY: TEST-NET-1 hosts (RFC 5737), tmp feed root, and the node-pause overlay
root is sealed to a tmp directory by an assert-first fixture — this leg's
conftest redirects ``expanduser`` but ``node_pauses`` resolved its constant at
import time, so a test that wrote a pause at the default root here would hit
the operator's real ``~/.hscc``. The fixture refuses that before writing.
"""

import os

import pytest

import api_server
from hscc_daemon import node_pauses

from tests.test_api import RunningServer

# TEST-NET-1 (RFC 5737) — documentation space, never this fleet.
PAUSED_NODE = "192.0.2.10"
UNRELATED_NODE = "192.0.2.30"


@pytest.fixture
def overlay(tmp_path):
    """Seal the overlay root for this test. Assert FIRST, then write."""
    root = str(tmp_path / "pause-overlay")
    os.makedirs(root, exist_ok=True)
    live = os.path.realpath(os.path.expanduser("~/.hscc"))
    assert os.path.realpath(root) != live, (
        "refusing to write node pauses anywhere near the live root")
    original = node_pauses.HSCC_DIR
    node_pauses.HSCC_DIR = root
    yield root
    node_pauses.HSCC_DIR = original


@pytest.fixture
def running(tmp_path):
    srv = RunningServer(hscc_dir=str(tmp_path / "feed"))
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


# ── the refusal, over HTTP ─────────────────────────────────────────────────


def test_a_post_scoped_to_a_parked_node_is_refused(running, token, overlay):
    node_pauses.pause(PAUSED_NODE, reason="training run")
    status, payload = post(running, token, {
        "title": "worker down",
        "body": "192.0.2.10 is not answering",
        "level": "critical",
        "key": "node/down",
        "node": PAUSED_NODE,
    })
    assert status == 200, payload
    assert payload["created"] is False
    assert payload["paused"] is True
    assert payload["reason"] == "paused_node"
    assert payload["alert"] is None
    assert payload["retry_after_s"] is None
    assert payload["paused_node"] == PAUSED_NODE
    assert payload["paused_reason"] == "training run"
    assert PAUSED_NODE in payload["speak"] and "paused" in payload["speak"]

    # Nothing reached the feed the apps read.
    feed_status, feed = get(running, token)
    assert feed_status == 200 and feed["count"] == 0 and feed["alerts"] == []
    assert not os.path.exists(
        os.path.join(running.server.ctx.hscc_dir, "alerts.json"))


def test_refusing_twice_then_resuming_still_creates_one_row(running, token,
                                                            overlay):
    """The acceptance pair: silent while paused, and the FIRST post after
    ``hscc node resume`` is created — same key, same title, no rate wait, id 1.
    """
    body = {"title": "worker down", "body": "not answering", "level": "critical",
            "key": "node/down", "node": PAUSED_NODE}
    node_pauses.pause(PAUSED_NODE, reason="training run")
    for _ in range(2):
        status, payload = post(running, token, body)
        assert status == 200 and payload["paused"] is True

    assert node_pauses.clear(PAUSED_NODE) is not None
    status, payload = post(running, token, {k: v for k, v in body.items()
                                            if k != "node"})
    assert status == 201, payload
    assert payload["created"] is True and payload["paused"] is False
    assert payload["alert"]["id"] == 1


# ── what must still be created ────────────────────────────────────────────


def test_an_unscoped_post_is_unchanged_while_a_node_is_parked(running, token,
                                                               overlay):
    node_pauses.pause(PAUSED_NODE, reason="training run")
    status, payload = post(running, token, {
        "title": "Approve a LIVE publish", "body": "waiting", "level": "action",
        "key": "live-publish/example-project",
    })
    assert status == 201, payload
    assert payload["created"] is True and payload["paused"] is False
    assert payload["alert"]["id"] == 1


def test_a_post_scoped_to_an_unpaused_node_is_created(running, token, overlay):
    node_pauses.pause(PAUSED_NODE, reason="training run")
    status, payload = post(running, token, {
        "title": "disk filling", "body": "92%", "level": "action",
        "key": "disk/192-0-2-30", "node": UNRELATED_NODE,
    })
    assert status == 201 and payload["created"] is True


def test_a_blank_scope_is_unscoped_not_a_refusal(running, token, overlay):
    node_pauses.pause(PAUSED_NODE, reason="training run")
    status, payload = post(running, token, {
        "title": "Release", "body": "shipped", "level": "info", "node": "  ",
    })
    assert status == 201 and payload["created"] is True


@pytest.mark.parametrize("junk", [["192.0.2.10", "192.0.2.11"], 42, {"h": 1}])
def test_a_non_string_scope_is_a_bad_request(running, token, overlay, junk):
    """The store raises; the route turns it into 400 with the store's own
    sentence — never a silent 201 and never a guessed host."""
    status, payload = post(running, token, {
        "title": "T", "body": "B", "level": "info", "node": junk,
    })
    assert status == 400, payload
    assert payload["error"]["code"] == "bad_request"
    feed_status, feed = get(running, token)
    assert feed["count"] == 0


def test_a_malformed_overlay_fails_toward_alerting(running, token, overlay):
    """A broken pause document must never mute the operator's feed."""
    with open(os.path.join(overlay, node_pauses.OVERLAY_NAME), "w") as fh:
        fh.write("{ this is not json")
    status, payload = post(running, token, {
        "title": "worker down", "body": "not answering", "level": "critical",
        "node": PAUSED_NODE,
    })
    assert status == 201 and payload["created"] is True


def test_a_pause_recorded_through_the_span_sibling_refuses_too(running, token,
                                                               overlay):
    node_pauses.pause("192.0.2.11", reason="span parked as one unit",
                      span_nodes=[PAUSED_NODE, "192.0.2.11"])
    status, payload = post(running, token, {
        "title": "worker down", "body": "not answering", "level": "critical",
        "node": PAUSED_NODE,
    })
    assert status == 200 and payload["paused"] is True
    assert payload["paused_reason"] == "span parked as one unit"


def test_the_paused_post_does_not_disturb_an_existing_row(running, token,
                                                          overlay):
    """Dedupe identity survives the refusal: the parked post neither clears
    nor consumes the live row's slot."""
    status, first = post(running, token, {
        "title": "Disk filling", "body": "92%", "level": "action",
        "key": "disk/filling",
    })
    assert status == 201 and first["alert"]["id"] == 1
    node_pauses.pause(PAUSED_NODE, reason="training run")
    status, refused = post(running, token, {
        "title": "Disk filling", "body": "92%", "level": "action",
        "key": "disk/filling", "node": PAUSED_NODE,
    })
    assert status == 200 and refused["paused"] is True
    status, again = post(running, token, {
        "title": "Disk filling", "body": "92%", "level": "action",
        "key": "disk/filling",
    })
    assert status == 200 and again["deduped"] is True
    assert again["alert"]["id"] == 1
    assert again["alert"]["acked"] is False
    _, feed = get(running, token)
    assert feed["count"] == 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
