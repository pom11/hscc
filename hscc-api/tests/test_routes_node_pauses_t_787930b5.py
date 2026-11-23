"""`GET /v1/node-pauses` — the READ half of the pause trio (t_787930b5).

WHAT THIS PINS. The audit's D10/F9: pause records (reason/since/until/span and
the owning host) were reachable by the CLI and the ``node_paused`` agent tool
but by no API route, so the mobile app and dashboards could show a PAUSED
state (the monitor route's overlay projection) and never *why*, since when, or
until when. This route exposes the records themselves — and only the records:
``hscc node pause|resume`` and the confirm-gated agent tools remain the only
mutation doors, because the printed plan and the y/N gate are the safety
mechanism a plain POST cannot reproduce.

THE SEAM IS THE REAL STORE AND THE REAL WRITER. Records are written through
``node_pauses.pause(..., directory=<server hscc_dir>)`` — the same function the
CLI calls — so the route and the CLI are proven to share one root instead of
being assumed to. Every host is a documentation address (TEST-NET-1,
192.0.2.0/24); nothing here touches the operator's real ``~/.hscc``.
"""

import json
import os

import pytest

import api_server
import routes_cluster
from hscc_daemon import node_pauses
from test_api import RunningServer

PARKED = "192.0.2.21"
SPANNED = "192.0.2.22"
OTHER = "192.0.2.23"

#: The row keys the route promises. Written out, not imported, so a rename
#: fails here before it reaches a client.
ROW_KEYS = {"host", "owner", "units", "reason", "since", "until",
            "span_nodes", "stopped"}


@pytest.fixture
def running(tmp_path):
    srv = RunningServer(hscc_dir=str(tmp_path))
    yield srv
    srv.close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


@pytest.fixture
def hscc_dir(running):
    """The server's own root — the temp overlay dir every case writes into."""
    return running.server.ctx.hscc_dir


def _get(running, token, path="/v1/node-pauses"):
    return running.request(token=token, path=path)


def _pause(directory, host=PARKED, **kwargs):
    """Write one pause record the way the CLI writes it — real writer, tmp dir."""
    kwargs.setdefault("reason", "training run")
    kwargs.setdefault("until", "2h")
    kwargs.setdefault("units", ["unit-a"])
    return node_pauses.pause(host, directory=str(directory), **kwargs)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_nothing_paused_is_a_200_with_an_empty_list(running, token):
    status, payload = _get(running, token)

    assert status == 200
    assert payload == {"count": 0, "pauses": [], "speak": "No nodes paused."}


def test_one_pause_round_trips_through_the_real_writer(running, token,
                                                       hscc_dir):
    entry = _pause(hscc_dir, units=["unit-a", "unit-b"],
                   span_nodes=[PARKED, SPANNED], stopped=True)

    status, payload = _get(running, token)

    assert status == 200
    assert payload["count"] == 1
    assert payload["speak"] == "1 node paused."
    assert payload["pauses"] == [{
        "host": PARKED,
        "owner": PARKED,
        "units": ["unit-a", "unit-b"],
        "reason": "training run",
        "since": entry["since"],
        "until": entry["until"],
        "span_nodes": [PARKED, SPANNED],
        "stopped": True,
    }]
    # The row's values are the store's own, read from the server's root.
    assert node_pauses.paused(PARKED, directory=str(hscc_dir)) == entry


def test_rows_are_sorted_and_an_open_ended_pause_keeps_none(running, token,
                                                            hscc_dir):
    _pause(hscc_dir, host=PARKED)
    _pause(hscc_dir, host=OTHER, reason="", until=None, units=[],
           stopped=False)

    status, payload = _get(running, token)

    assert status == 200
    assert payload["count"] == 2
    assert payload["speak"] == "2 nodes paused."
    assert [row["host"] for row in payload["pauses"]] == [PARKED, OTHER]
    open_ended = payload["pauses"][1]
    assert open_ended["until"] is None
    assert open_ended["reason"] == ""
    assert open_ended["units"] == []
    assert open_ended["stopped"] is False


def test_a_span_is_reported_on_its_owner_record_not_as_a_second_pause(
        running, token, hscc_dir):
    """A node inside another pause's span is covered, not an owner.

    ``hscc node resume`` refuses to resume a spanned node on its own and
    points at the owner (docs/ops/node-pause.md). The route carries the same
    distinction: one row for the record, ``owner`` naming the keyed host, and
    ``span_nodes`` verbatim for a client that wants to paint every covered
    node.
    """
    _pause(hscc_dir, span_nodes=[PARKED, SPANNED])

    status, payload = _get(running, token)

    assert status == 200
    assert payload["count"] == 1
    row = payload["pauses"][0]
    assert row["host"] == row["owner"] == PARKED
    assert row["span_nodes"] == [PARKED, SPANNED]
    assert SPANNED not in [r["host"] for r in payload["pauses"]]


# ---------------------------------------------------------------------------
# The root and the read-only posture
# ---------------------------------------------------------------------------

def test_the_route_reads_the_servers_root_not_the_modules_default(
        running, token, hscc_dir, tmp_path, monkeypatch):
    """A second root must not leak into this server's answer."""
    elsewhere = tmp_path / "not-this-server"
    monkeypatch.setattr(node_pauses, "HSCC_DIR", str(elsewhere))

    _pause(elsewhere, host=OTHER, reason="somebody else's node")
    _pause(hscc_dir, host=PARKED)

    status, payload = _get(running, token)

    assert status == 200
    assert [row["host"] for row in payload["pauses"]] == [PARKED]


def test_there_is_no_mutation_route_and_post_changes_nothing(running, token,
                                                             hscc_dir):
    status, payload = running.request(method="POST", path="/v1/node-pauses",
                                      token=token, body={"host": PARKED})

    assert status == 405
    assert payload["error"]["code"] == "method_not_allowed"
    assert node_pauses.list_pauses(str(hscc_dir)) == {}


def test_a_request_without_a_token_is_rejected(running):
    status, payload = _get(running, None)

    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# Never a clean zero on a broken read
# ---------------------------------------------------------------------------

def test_a_broken_reader_is_degraded_with_its_reason(running, token,
                                                     monkeypatch):
    def boom(directory=None):
        raise RuntimeError("overlay unreadable")

    monkeypatch.setattr(routes_cluster, "_backing_node_pauses", boom)

    status, payload = _get(running, token)

    assert status == 200
    assert "count" not in payload and "pauses" not in payload
    assert payload["error"] == "RuntimeError: overlay unreadable"
    assert payload["speak"].startswith("Node pause list failed:")


def test_a_non_object_reader_result_is_degraded_not_zero(running, token,
                                                         monkeypatch):
    monkeypatch.setattr(routes_cluster, "_backing_node_pauses",
                        lambda directory=None: ["not", "a", "mapping"])

    status, payload = _get(running, token)

    assert status == 200
    assert "count" not in payload
    assert "list" in payload["error"]


def test_a_malformed_overlay_file_is_not_an_error(running, token, hscc_dir,
                                                  monkeypatch):
    """The underlying reader fails open on corruption (and WARNs about it)."""
    monkeypatch.setattr(node_pauses, "_warn", lambda msg: None)
    path = node_pauses.overlay_path(str(hscc_dir))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{not json")

    status, payload = _get(running, token)

    assert status == 200
    assert payload == {"count": 0, "pauses": [], "speak": "No nodes paused."}


# ---------------------------------------------------------------------------
# The contract itself
# ---------------------------------------------------------------------------

def test_row_key_contract_is_the_documented_set():
    assert routes_cluster.NODE_PAUSE_ROW_KEYS == (
        "host", "owner", "units", "reason", "since", "until", "span_nodes",
        "stopped")
    row = routes_cluster._node_pause_record(PARKED, {
        "reason": "r", "since": "s", "until": None, "units": ["u"],
        "span_nodes": [], "stopped": False})
    assert set(row) == ROW_KEYS


def test_json_body_is_the_same_shape_clients_parse(running, token, hscc_dir):
    """A raw-body decode, so the assertion does not depend on the test helper."""
    _pause(hscc_dir)

    import http.client
    conn = http.client.HTTPConnection(running.host, running.port, timeout=5)
    conn.request("GET", "/v1/node-pauses",
                 headers={"Authorization": "Bearer " + token})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()

    assert resp.status == 200
    body = json.loads(raw)
    assert body["count"] == 1
    assert set(body["pauses"][0]) == ROW_KEYS
