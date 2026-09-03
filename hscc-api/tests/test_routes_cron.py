"""Unit tests for hscc-api — GET /v1/cron/list (read-only scheduled-jobs roster).

The endpoint's contract (see ios-app/docs/cron-view-gap.md): a read-only list
of ALL Hermes cron jobs — active and paused — each mapped 1:1 from the
on-disk `~/.hermes/cron/jobs.json` fields, with fields per the card
minimum contract:

  { id, name, schedule_display, enabled, state, next_run_at, last_run_at,
    last_status, last_error }

The suite is hermetic:
  * the real ``running``/``token`` fixtures drive real loopback HTTP so auth
    and the route dispatcher are exercised end-to-end;
  * every backing call is stubbed via monkeypatch on the ``_backing_*`` module
    function, so NO test ever reads the operator's live jobs.json;
  * the source-of-truth regression test proves the endpoint reflects whatever
    the backing returns verbatim (no second hand-written list, no re-deriving);
  * the degradation test forces the store to be unreadable and asserts the
    endpoint degrades to a 200 with an honest ``speak`` (never a crash, never
    a fabricated job list).

Coverage required by the card:
  * GET /v1/cron/list -> 200 + ``jobs`` array with the full per-job contract +
    a non-empty ``speak``;
  * auth enforced (401 without / with wrong token);
  * ALL jobs are returned, active and paused alike, with enabled/state intact;
  * 1:1 mapping onto the backing store (source of truth) — read-only.
"""

import http.client
import json
import threading
import types

import pytest

import api_server
import routes_cron


# --------------------------------------------------------------------------- #
# Hermetic backing fakes (mirrors test_routes_autodown.py)
# --------------------------------------------------------------------------- #

@ pytest.fixture
def fakes(monkeypatch):
    """Stub every ``routes_cron._backing_*`` call so no test touches the
    operator's live jobs.json."""
    state = {"calls": 0}

    def _fake_all_cron_jobs():
        state["calls"] += 1
        return [
            {
                "id": "bdf1af7e169e",
                "name": "hscc-dep-watcher",
                "schedule_display": "0 8 * * *",
                "enabled": True,
                "state": "scheduled",
                "next_run_at": "2026-09-04T08:00:00+03:00",
                "last_run_at": "2026-09-03T08:00:44.775538+03:00",
                "last_status": "ok",
                "last_error": None,
            },
            {
                "id": "c67ce27a1f36",
                "name": "X feed — timeline + news",
                "schedule_display": "45 5 */2 * *",
                "enabled": False,
                "state": "paused",
                "next_run_at": "2026-06-19T05:45:00+03:00",
                "last_run_at": "2026-06-17T05:45:41.203100+03:00",
                "last_status": "ok",
                "last_error": None,
            },
        ]

    b = {"list_all_cron_jobs": _fake_all_cron_jobs}
    _install(monkeypatch, b)
    return state


def _install(monkeypatch, backing: dict):
    for name, fn in backing.items():
        monkeypatch.setattr(routes_cron, f"_backing_{name}", fn)


@pytest.fixture
def running(tmp_path, fakes):
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


def _req(running, token, path="/v1/cron/list", method="GET"):
    conn = http.client.HTTPConnection(running.host, running.port, timeout=8)
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    conn.request(method, path, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        payload: dict = json.loads(raw) if raw else {}
    except ValueError:
        payload = {"raw": raw}
    return resp.status, payload


# --------------------------------------------------------------------------- #
# GET /v1/cron/list — read-only
# --------------------------------------------------------------------------- #

def test_cron_list_200_shape(running, token):
    """200 + a ``jobs`` array with the full per-job contract, both active and
    paused jobs present, and a non-empty ``speak``."""
    status, payload = _req(running, token, "/v1/cron/list")
    assert status == 200
    jobs = payload["jobs"]
    assert isinstance(jobs, list) and len(jobs) == 2
    for j in jobs:
        # Full minimum contract — every key present.
        for key in ("id", "name", "schedule_display", "enabled", "state",
                    "next_run_at", "last_run_at", "last_status", "last_error"):
            assert key in j, f"missing {key} in {j}"
    # BOTH active and paused jobs are returned, with enabled/state intact.
    by_id = {j["id"]: j for j in jobs}
    assert by_id["bdf1af7e169e"]["enabled"] is True
    assert by_id["bdf1af7e169e"]["state"] == "scheduled"
    assert by_id["c67ce27a1f36"]["enabled"] is False
    assert by_id["c67ce27a1f36"]["state"] == "paused"
    assert isinstance(payload["speak"], str) and payload["speak"]


def test_cron_list_auth_401_without_token(running):
    status, payload = _req(running, None, "/v1/cron/list")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


def test_cron_list_auth_401_wrong_token(running):
    status, payload = _req(running, "bad-token", "/v1/cron/list")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


def test_cron_list_mapping_is_1to1_from_backing(running, token, monkeypatch):
    """The endpoint reflects whatever the backing returns VERBATIM — the
    source-of-truth guarantee (no hand-written list, no re-deriving, nothing
    invented). Any field the backing emits appears in the payload exactly."""
    sample = [
        {
            "id": "ab12cd34ef56",
            "name": "custom-watcher",
            "schedule_display": "every 5m",
            "enabled": True,
            "state": "scheduled",
            "next_run_at": "2026-09-05T01:00:00Z",
            "last_run_at": "2026-09-04T01:00:00Z",
            "last_status": "error",
            "last_error": "boom",
        },
    ]
    _install(monkeypatch, {"list_all_cron_jobs": lambda: sample})
    status, payload = _req(running, token, "/v1/cron/list")
    assert status == 200
    assert payload["jobs"] == sample  # exact 1:1 pass-through


def test_cron_list_empty_roster(running, token, monkeypatch):
    """No jobs -> 200 with an empty ``jobs`` array and an honest speak."""
    _install(monkeypatch, {"list_all_cron_jobs": lambda: []})
    status, payload = _req(running, token, "/v1/cron/list")
    assert status == 200
    assert payload["jobs"] == []
    assert "speak" in payload and payload["speak"]


def test_cron_list_degrades_when_store_unreadable(running, token, monkeypatch):
    """Backing returns CRON_UNREADABLE -> 200 + honest speak, NO fabricated
    ``jobs`` list."""
    _install(monkeypatch, {"list_all_cron_jobs": lambda: "<unreadable>"})
    status, payload = _req(running, token, "/v1/cron/list")
    assert status == 200
    assert "jobs" not in payload          # never fabricate a list
    assert "unavailable" in payload["speak"].lower()
    assert payload["speak"]


def test_cron_list_backing_exception_degrades(running, token, monkeypatch):
    """An unexpected backing exception degrades to an honest speak, never 500."""
    def _boom():
        raise RuntimeError("unexpected")

    _install(monkeypatch, {"list_all_cron_jobs": _boom})
    status, payload = _req(running, token, "/v1/cron/list")
    assert status == 200
    assert "jobs" not in payload
    assert payload["speak"]


# --------------------------------------------------------------------------- #
# Get-only: POST -> 405, never the handler
# --------------------------------------------------------------------------- #

def test_cron_list_post_is_405(running, token, fakes):
    status, payload = _req(running, token, "/v1/cron/list", method="POST")
    assert status == 405
    assert payload["error"]["code"] == "method_not_allowed"
    # Never reached the handler (read-only contract enforced at the router).
    assert fakes["calls"] == 0


# --------------------------------------------------------------------------- #
# Backing reader unit tests (hermetic, against a real temp jobs.json)
# --------------------------------------------------------------------------- #

def test_list_all_cron_jobs_returns_all_and_paused(monkeypatch):
    """list_all_cron_jobs returns active AND paused jobs with the full
    contract, in file order, mapped 1:1 from a real jobs.json on disk."""
    import json as _json
    import os
    import tempfile

    from hscc_daemon import autodown

    data = {
        "jobs": [
            {
                "id": "aaa",
                "name": "active-job",
                "schedule_display": "0 8 * * *",
                "enabled": True,
                "state": "scheduled",
                "next_run_at": "2026-09-04T08:00:00Z",
                "last_run_at": "2026-09-03T08:00:00Z",
                "last_status": "ok",
                "last_error": None,
            },
            {
                "id": "bbb",
                "name": "paused-job",
                "schedule_display": "45 5 */2 * *",
                "enabled": False,
                "state": "paused",
                "next_run_at": "2026-06-19T05:45:00Z",
                "last_run_at": "2026-06-17T05:45:41Z",
                "last_status": "error",
                "last_error": "deliver failed",
            },
        ]
    }
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            _json.dump(data, f)
        result = autodown.list_all_cron_jobs(jobs_file=path)
    finally:
        os.unlink(path)

    assert isinstance(result, list) and len(result) == 2
    assert result[0]["id"] == "aaa" and result[0]["enabled"] is True
    assert result[0]["state"] == "scheduled"
    assert result[1]["id"] == "bbb" and result[1]["enabled"] is False
    assert result[1]["state"] == "paused"
    assert result[1]["last_status"] == "error"
    assert result[1]["last_error"] == "deliver failed"
    # Full contract present on every job.
    for j in result:
        for key in ("id", "name", "schedule_display", "enabled", "state",
                    "next_run_at", "last_run_at", "last_status", "last_error"):
            assert key in j


def test_list_all_cron_jobs_fail_closed_on_missing(monkeypatch):
    """Absent jobs file -> CRON_UNREADABLE, never an empty list."""
    from hscc_daemon import autodown
    result = autodown.list_all_cron_jobs(jobs_file="/nonexistent/jobs.json")
    assert result == autodown.CRON_UNREADABLE
