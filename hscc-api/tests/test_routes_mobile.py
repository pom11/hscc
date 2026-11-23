"""Tests for GET /v1/mobile/summary — the compact board-status read.

The route delegates to ``hscc_daemon.kanban_live.collect(registered_only=True)``
— the SAME pass ``hscc kanban monitor`` renders — and returns
``board_status_counts`` for each registered project board. Hermetic: the
``_backing_collect`` seam is monkeypatched, so no test scans or mutates a live
kanban DB. Requests go over real loopback HTTP (port 0) so the central auth
gate and the router are exercised end-to-end.
"""

import http.client
import json
import threading
import types

import pytest

import api_server
import routes_mobile


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _card(board, status, idx):
    return {
        "id": f"t_{board}{idx}",
        "board": board,
        "title": f"card {idx} on {board}",
        "status": status,
        "assignee": "ios-engineer",
    }


def _fleet(board_names, per_status=2, extra_status=None):
    statuses = list(routes_mobile.COUNT_STATUSES)
    if extra_status:
        statuses.append(extra_status)
    cards = []
    for board in board_names:
        for status in statuses:
            for i in range(per_status):
                cards.append(_card(board, status, len(cards) + i))
    return cards


def _collect_from(state, board=None):
    """Shape cards like ``kanban_live.collect()`` board entries."""
    entries = []
    for slug in state["boards"]:
        if slug in state["unreadable"]:
            continue
        cards = [c for c in state["cards"] if c["board"] == slug]
        counts = {}
        for card in cards:
            status = str(card.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        if board is not None and slug != board:
            continue
        entries.append({"name": slug, "counts": counts,
                        "done_24h": counts.get("done", 0)})
    errors = [f"board {b!r} unreadable" for b in state["unreadable"]]
    return {"boards": entries, "running": [], "ready": [], "errors": errors,
            "caps": state.get("caps", (None, None))}


@pytest.fixture
def fleet(monkeypatch):
    calls = []
    state = {
        "boards": ["hscc", "radio", "default"],
        "cards": _fleet(["hscc", "radio", "default"], per_status=2,
                        extra_status="triage"),
        "boom": False,
        "unreadable": set(),
        "calls": calls,
        "caps": (6, 2),
    }

    def collect(board=None):
        calls.append(board)
        if state["boom"]:
            raise RuntimeError("board db locked")
        return _collect_from(state, board=board)

    monkeypatch.setattr(routes_mobile, "_backing_collect", collect)
    return state


@pytest.fixture
def running(tmp_path, fleet):
    srv = types.SimpleNamespace()
    srv.server = api_server.create_server(hscc_dir=str(tmp_path),
                                          addr=("127.0.0.1", 0))
    srv.host, srv.port = srv.server.server_address[:2]
    thread = threading.Thread(target=srv.server.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.server.shutdown()
    srv.server.server_close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


def _raw(running, token, path, method="GET"):
    conn = http.client.HTTPConnection(running.host, running.port, timeout=8)
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    conn.request(method, path, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    status = resp.status
    conn.close()
    return status, data


def _get(running, token, path):
    status, data = _raw(running, token, path)
    return status, json.loads(data or b"{}"), data


# --------------------------------------------------------------------------- #
# Read-only, one GET, guarded by the ONE central auth gate
# --------------------------------------------------------------------------- #

def test_route_registered_as_get_only():
    matches = [(m, h) for (m, p, h) in api_server.ROUTES
               if p.pattern == r"^/v1/mobile/summary$"]
    assert [m for m, _ in matches] == ["GET"], matches
    assert matches[0][1] is routes_mobile.handle_mobile_summary


def test_post_is_rejected_405(running, token, fleet):
    status, _, _ = _get(running, token, "/v1/mobile/summary")
    assert status == 200
    before = len(fleet["calls"])
    conn = http.client.HTTPConnection(running.host, running.port, timeout=8)
    conn.request("POST", "/v1/mobile/summary",
                 body=json.dumps({"confirm": True}).encode(),
                 headers={"Authorization": "Bearer " + token,
                          "Content-Type": "application/json"})
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 405, body
    assert body["error"]["code"] == "method_not_allowed"
    assert len(fleet["calls"]) == before


def test_auth_required_401_without_token(running):
    status, data = _raw(running, None, "/v1/mobile/summary")
    assert status == 401
    assert json.loads(data)["error"]["code"] == "unauthorized"
    status, data = _raw(running, "wrong-token", "/v1/mobile/summary")
    assert status == 401


# --------------------------------------------------------------------------- #
# Counts come from the collector's board view, not a client-side derivation
# --------------------------------------------------------------------------- #

def test_every_board_carries_all_status_keys(running, token, fleet):
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert status == 200
    assert [b["board"] for b in payload["boards"]] == [
        "default", "hscc", "radio"]
    entry = payload["boards"][0]
    for key in routes_mobile.COUNT_STATUSES:
        assert isinstance(entry["counts"][key], int)
    assert entry["counts"]["done"] == 2           # done in the last 24 h
    assert entry["other"] == 2, entry
    assert entry["other_statuses"] == {"triage": 2}, entry
    # total = open + archived + done-today + other, so a client's
    # total - done - archived is the open card count. Two cards per status,
    # six statuses + triage = 14.
    assert entry["total"] == 14


def test_done_is_the_collectors_done_24h(running, token, fleet):
    fleet["cards"] = [
        _card("hscc", "done", 1),
        _card("hscc", "todo", 2),
    ]
    status, payload, _ = _get(running, token, "/v1/mobile/summary?boards=hscc")
    assert status == 200
    assert payload["boards"][0]["counts"]["done"] == 1
    assert payload["boards"][0]["counts"]["todo"] == 1


def test_done_zero_is_present_and_distinguishable(running, token, fleet):
    fleet["cards"] = [c for c in fleet["cards"]
                      if c["board"] != "hscc" or c["status"] != "done"]
    status, payload, _ = _get(running, token, "/v1/mobile/summary?boards=hscc")
    entry = payload["boards"][0]
    assert entry["counts"]["done"] == 0
    assert entry["total"] > 0


def test_unknown_status_is_reported_not_dropped(running, token, fleet):
    fleet["cards"] = [_card("hscc", "triage", 1), _card("hscc", "triage", 2),
                      _card("hscc", "todo", 3)]
    status, payload, _ = _get(running, token, "/v1/mobile/summary?boards=hscc")
    entry = payload["boards"][0]
    assert entry["other"] == 2
    assert entry["other_statuses"] == {"triage": 2}
    assert entry["total"] == 3


# --------------------------------------------------------------------------- #
# One collection for every board; ?boards= narrows it
# --------------------------------------------------------------------------- #

def test_default_is_one_collect_call_for_all_boards(running, token, fleet):
    fleet["boards"] = [f"b{i}" for i in range(5)]
    fleet["cards"] = _fleet(fleet["boards"], per_status=1)
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert status == 200
    assert payload["board_count"] == 5
    assert fleet["calls"] == [None]


def test_boards_param_narrows_dedups_and_sorts(running, token, fleet):
    status, payload, _ = _get(
        running, token, "/v1/mobile/summary?boards=radio,hscc,radio")
    assert payload["board_count"] == 2
    assert [b["board"] for b in payload["boards"]] == ["hscc", "radio"]
    assert payload["total_boards"] == 2


def test_unregistered_board_404_with_error_envelope(running, token, fleet):
    status, payload, _ = _get(
        running, token, "/v1/mobile/summary?boards=not-registered")
    assert status == 404
    assert payload["error"]["code"] == "not_found"
    assert "registered project board" in payload["error"]["message"]


def test_unimplemented_blocks_are_400_not_silently_ignored(running, token,
                                                           fleet):
    for name in ("rows", "with"):
        status, payload, _ = _get(
            running, token, f"/v1/mobile/summary?{name}=1")
        assert status == 400, name
        assert payload["error"]["code"] == "bad_request"


@pytest.mark.parametrize("raw", ["../etc", "a/b", "a:b", "a.1"])
def test_malformed_board_name_400(running, token, raw):
    status, payload, _ = _get(
        running, token, f"/v1/mobile/summary?boards={raw}")
    assert status == 400
    assert payload["error"]["code"] == "bad_request"


def test_too_many_boards_400(running, token, fleet):
    many = ",".join(f"b{i}" for i in range(routes_mobile.MAX_REQUESTED_BOARDS + 1))
    status, payload, _ = _get(
        running, token, f"/v1/mobile/summary?boards={many}")
    assert status == 400
    assert payload["error"]["code"] == "bad_request"


# --------------------------------------------------------------------------- #
# Budget and structural redaction
# --------------------------------------------------------------------------- #

def test_budget_ceiling_with_50_boards(running, token, fleet):
    fleet["boards"] = [f"board{i:02d}" for i in range(50)]
    fleet["cards"] = _fleet(fleet["boards"], per_status=10)
    status, payload, raw = _get(running, token, "/v1/mobile/summary")
    assert status == 200
    assert len(raw) <= routes_mobile.MAX_RESPONSE_BYTES
    assert payload["truncated"] is True
    assert payload["omitted_boards"] > 0
    assert "omitted for size" in payload["speak"]


def test_no_path_address_or_token_can_reach_the_payload(running, token, fleet):
    fleet["boards"] = ["hscc", "100.64.0.1", "10.0.0.x",
                       "/tmp/secret", "tok_live.abcdef"]
    fleet["cards"] = []
    status, payload, raw = _get(running, token, "/v1/mobile/summary")
    assert status == 200
    assert [b["board"] for b in payload["boards"]] == ["hscc"]
    assert payload["unreadable_boards"] == 4
    text = raw.decode()
    for forbidden in ("100.64.0.1", "10.0.0.x", "/tmp/secret",
                      "tok_live.abcdef"):
        assert forbidden not in text


def test_unreadable_slug_count_is_exact(running, token, fleet):
    fleet["boards"] = ["ok", "bad slug", "10.0.0.x", "/tmp/x", ""]
    fleet["cards"] = []
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert payload["board_count"] == 1
    assert payload["unreadable_boards"] == 4
    assert "bad slug" not in json.dumps(payload)
    assert "/tmp/x" not in json.dumps(payload)


# --------------------------------------------------------------------------- #
# Honest degradation
# --------------------------------------------------------------------------- #

def test_board_listing_failure_omits_boards_entirely(running, token, fleet):
    fleet["boom"] = True
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert status == 200
    assert "boards" not in payload
    assert "counts" not in json.dumps(payload)
    assert payload["speak"] == "Board listing unavailable."
    assert payload["generated_at"]


def test_no_boards_at_all_says_so(running, token, fleet):
    fleet["boards"] = []
    fleet["cards"] = []
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert status == 200
    assert payload["boards"] == []
    assert payload["speak"] == "No boards."


# --------------------------------------------------------------------------- #
# speak + freshness stamp
# --------------------------------------------------------------------------- #

def test_speak_matches_the_contract_example_math(running, token, fleet):
    fleet["boards"] = ["hscc"]
    fleet["cards"] = ([_card("hscc", "todo", i) for i in range(17)]
                      + [_card("hscc", "ready", i) for i in range(2)]
                      + [_card("hscc", "running", i) for i in range(3)]
                      + [_card("hscc", "blocked", i) for i in range(1)]
                      + [_card("hscc", "done", i) for i in range(5)])
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert payload["speak"] == "hscc: 23 open, 3 running, 5 done."


def test_speak_names_the_omitted_boards_but_keeps_true_totals(running, token,
                                                              fleet):
    fleet["boards"] = [f"board{i:02d}" for i in range(50)]
    fleet["cards"] = _fleet(fleet["boards"], per_status=10)
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert "board" in payload["speak"]
    assert "omitted for size" in payload["speak"]


def test_speak_present_and_generated_at_is_utc_z(running, token, fleet):
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert isinstance(payload["speak"], str) and payload["speak"]
    assert payload["generated_at"].endswith("Z")


def test_worker_slot_cap_comes_from_the_server_config(running, token, fleet):
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert status == 200
    assert payload["max_in_progress"] == 6


def test_worker_slot_cap_is_absent_when_config_is_unset(running, token, fleet):
    fleet["caps"] = (None, None)
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert status == 200
    assert "max_in_progress" not in payload
