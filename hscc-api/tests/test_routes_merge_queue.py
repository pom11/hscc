"""GET /v1/merge-queue — read-only rows, exactly the kanban monitor's view."""

import api_server

from test_api import RunningServer

from hscc_daemon import merge_queue as mq


def _request(running, token, path):
    return running.request(token=token, path=path)


def _seed(state_path):
    doc = mq.add_to_queue("alpha", 7, card="t_a", branch="wt/t_a",
                          path=state_path)
    doc = mq.add_to_queue("alpha", 8, card="t_b", branch="wt/t_b",
                          path=state_path)
    ps = mq._project_state(doc, "alpha")
    ps["queue"].pop()
    ps["in_flight"] = {
        "pr": 8, "card": "t_b", "branch": "wt/t_b", "stage": "gating",
        "enqueued_at": "2026-11-19T00:00:00+00:00", "attempts": 0}
    mq.save_state(doc, state_path)


def test_merge_queue_route_serves_the_cli_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(mq, "load_projects", lambda path=None: [])
    state_path = mq.state_file()
    _seed(state_path)

    srv = RunningServer(hscc_dir=str(tmp_path))
    try:
        token = api_server.load_token(srv.server.ctx.hscc_dir)
        status, payload = _request(srv, token, "/v1/merge-queue")
    finally:
        srv.close()

    expected = mq.view(state_path=state_path)
    assert status == 200
    for row in expected["rows"]:
        row.pop("age_s", None)
    for row in payload["rows"]:
        row.pop("age_s", None)
    for project in list(expected["projects"].values()) + list(
            payload["projects"].values()):
        for row in project["rows"]:
            row.pop("age_s", None)
    assert payload["rows"] == expected["rows"]
    assert payload["projects"] == expected["projects"]
    assert payload["count"] == 2 and payload["queued"] == 1
    assert [r["state"] for r in payload["rows"]] == ["queued", "gating"]
    assert isinstance(payload["speak"], str) and "1 queued" in payload["speak"]


def test_merge_queue_route_board_filter_and_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(mq, "load_projects", lambda path=None: [{
        "name": "alpha", "repo": "/repo/alpha", "board": "b-alpha",
        "merge_queue": {"enabled": True}}])
    state_path = mq.state_file()
    _seed(state_path)
    mq.add_to_queue("beta", 9, card="t_c", branch="wt/t_c", path=state_path)

    srv = RunningServer(hscc_dir=str(tmp_path))
    try:
        token = api_server.load_token(srv.server.ctx.hscc_dir)
        status, payload = _request(
            srv, token, "/v1/merge-queue?board=b-alpha")
    finally:
        srv.close()

    assert status == 200
    assert {r["project"] for r in payload["rows"]} == {"alpha"}

    monkeypatch.setattr(mq, "load_projects", lambda path=None: [])
    empty = mq.view(state_path=str(tmp_path / "missing.json"))
    assert empty["rows"] == []
    assert empty["count"] == 0
