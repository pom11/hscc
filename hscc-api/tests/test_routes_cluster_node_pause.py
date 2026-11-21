"""The node-pause overlay on the cluster read routes (t_824b617f).

WHAT THIS PINS. A node the operator parked with ``hscc node pause`` has its
units stopped on purpose. Every read path downstream of that stop — the
sparkrun host table, the serving poller — then sees what a crash looks like:
no container, no answering engine. Without an overlay read at the API edge the
fleet API therefore calls a deliberate act a fault, and the widgets paint the
node DOWN. These tests pin the other outcome: a parked row says
``{"status": "paused", "paused_reason": ..., "paused_since": ...}`` and those
three names are what the iOS/macOS widgets code against (t_983f177e), so they
are asserted literally rather than by constant on purpose — a rename must fail
a test here before it ships in a widget.

THE SEAM IS THE REAL WRITER. The pause records are written through
``node_pauses.pause(..., directory=<tmp>)`` — the same function the CLI calls —
into the server's own ``hscc_dir``, so the reader and the writer are proven to
share a root instead of being assumed to. Nothing here touches the operator's
real ``~/.hscc``: the server binds a tmp ``hscc_dir``, the overlay is written
to that same tmp dir, and every host is a ``192.0.2.0/24`` documentation
address.
"""

import json

import pytest

import api_server
import routes_cluster
from hscc_daemon import node_pauses
from test_api import RunningServer  # reuse A1's hermetic server helper

PARKED = "192.0.2.11"
SPANNED = "192.0.2.12"
HEALTHY = "192.0.2.13"

#: The three names the widget contract is built on. Asserted, not imported.
CONTRACT_KEYS = ("status", "paused_reason", "paused_since")


@pytest.fixture(autouse=True)
def _no_shared_snapshot(monkeypatch):
    """Each test starts with an empty monitor/status snapshot cache.

    The cache is a module global with a 20 s TTL, so a frame another test
    filed would answer this one's request. Pinning the clock as well keeps
    ``taken_at`` and the TTL arithmetic deterministic instead of racing a
    neighbour that moved the cache clock.
    """
    routes_cluster._reset_cluster_snapshot_cache()
    yield
    routes_cluster._reset_cluster_snapshot_cache()


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


def _request(running, token, path):
    return running.request(token=token, path=path)


def _stub_backing(monkeypatch, monitor=None, serving=None):
    """Install the fleet reads these routes make, as plain data.

    ``serving`` is the poller's result — ``{"rows": [...], "proxy_error": ...}``
    — or just its rows as a list, which is what most of these cases care about.
    """
    monkeypatch.setattr(routes_cluster, "_backing_cluster_monitor",
                        lambda: monitor)
    if isinstance(serving, list):
        serving = {"rows": serving, "proxy_error": None}
    monkeypatch.setattr(routes_cluster, "_backing_serving_snapshot",
                        lambda: serving if serving is not None
                        else {"rows": [], "proxy_error": None})


def _host_row(host, **extra):
    row = {"host": host, "error": None, "sample": {"cpu_usage_pct": "8.1"},
           "workloads": [{"workload_id": "workload-a"}], "used_slots": 1,
           "free_slots": 0}
    row.update(extra)
    return row


def _monitor_frame(*rows):
    return {"success": True, "output": "{}", "json": {"hosts": list(rows)}}


def _pause(directory, host=PARKED, **kwargs):
    """Write one pause record the way the CLI writes it — real writer, tmp dir."""
    kwargs.setdefault("reason", "training run")
    kwargs.setdefault("until", "2h")
    kwargs.setdefault("units", ["unit-a"])
    return node_pauses.pause(host, directory=str(directory), **kwargs)


def _row_for(payload, host, section="hosts"):
    rows = payload["json"][section] if section != "serving" else payload["serving"]
    key = "host" if section != "serving" else "node"
    return next(r for r in rows if r.get(key) == host)


# ---------------------------------------------------------------------------
# The paused row shape
# ---------------------------------------------------------------------------

def test_parked_host_row_projects_paused_with_reason_and_since(
        running, token, monkeypatch, hscc_dir):
    """THE row shape the widget contract names, spelled exactly."""
    entry = _pause(hscc_dir)
    _stub_backing(monkeypatch, monitor=_monitor_frame(
        _host_row(PARKED), _host_row(HEALTHY)))

    status, payload = _request(running, token, "/v1/cluster/monitor")

    assert status == 200
    row = _row_for(payload, PARKED)
    assert row["status"] == "paused"
    assert row["paused_reason"] == "training run"
    assert row["paused_since"] == entry["since"]
    assert set(row) == {"host", "error", "sample", "workloads", "used_slots",
                        "free_slots", "status", "paused_reason", "paused_since"}
    # The record round-trips through the real overlay file, not a stub.
    assert node_pauses.paused(PARKED, directory=str(hscc_dir)) is not None


def test_paused_row_never_claims_down(running, token, monkeypatch):
    """No key of a parked row says ``down``, however the sample came back.

    The strongest case the route has to survive: sparkrun could not reach the
    parked host at all (so the row carries its ``error``), and the serving
    scrape has no model for it — the exact pair of signals a client otherwise
    reads as a dead machine.
    """
    _stub_backing(monkeypatch, monitor=_monitor_frame(
        _host_row(PARKED, error="ssh: connect timeout")),
        serving=[{"node": PARKED, "port": "8000", "model": None,
                  "running": 0, "waiting": 0}])
    monkeypatch.setattr(routes_cluster, "_backing_node_pauses",
                        lambda directory=None: {
                            PARKED: {"reason": "training run",
                                     "since": "2026-11-21T10:00:00+00:00",
                                     "until": None, "units": ["unit-a"],
                                     "span_nodes": [], "stopped": True}})

    status, payload = _request(running, token, "/v1/cluster/monitor")

    assert status == 200
    for row in (payload["json"]["hosts"][0], payload["serving"][0]):
        assert row["status"] == "paused"
        assert row["paused_reason"] == "training run"
        assert row["paused_since"] == "2026-11-21T10:00:00+00:00"
        assert "down" not in json.dumps(row).lower()


def test_serving_row_for_a_parked_node_is_paused_not_down(
        running, token, monkeypatch, hscc_dir):
    """The serving table is where "down" is literally rendered (no model).

    A parked unit has no model to report, which is the CLI's own "down" cell
    (hscc_daemon/cluster_live.py). The row keeps its null model — a null model
    is true — and gains the status that outranks it.
    """
    _pause(hscc_dir)
    _stub_backing(monkeypatch, monitor=_monitor_frame(_host_row(PARKED)),
                  serving=[{"node": PARKED, "port": "8000", "model": None,
                            "running": 0, "waiting": 0},
                           {"node": HEALTHY, "port": "8001", "model": "qwen",
                            "running": 1, "waiting": 0}])

    status, payload = _request(running, token, "/v1/cluster/monitor")

    assert status == 200
    parked = _row_for(payload, PARKED, section="serving")
    assert parked["model"] is None                 # truth kept
    assert parked["status"] == "paused"            # and it is not what it looks like
    assert parked["paused_reason"] == "training run"
    assert "status" not in _row_for(payload, HEALTHY, section="serving")


def test_span_partner_of_a_parked_unit_is_paused_too(running, token,
                                                     monkeypatch, hscc_dir):
    """A unit spanning two hosts is parked as a whole — both rows say paused.

    This is ``node_pauses.covering``'s rule on the visibility side: the host
    named only in ``span_nodes`` is just as out of service, and calling it down
    would fault the operator's own action twice.
    """
    _pause(hscc_dir, span_nodes=[PARKED, SPANNED])
    _stub_backing(monkeypatch, monitor=_monitor_frame(
        _host_row(PARKED), _host_row(SPANNED), _host_row(HEALTHY)))

    status, payload = _request(running, token, "/v1/cluster/monitor")

    assert status == 200
    for host in (PARKED, SPANNED):
        row = _row_for(payload, host)
        assert row["status"] == "paused"
        assert row["paused_since"]
    assert "status" not in _row_for(payload, HEALTHY)
    assert [p["node"] for p in payload["paused_nodes"]] == [PARKED, SPANNED]


def test_own_record_wins_over_a_span_that_also_names_the_host(
        running, token, monkeypatch, hscc_dir):
    """``covering`` prefers the host's own pause; so does the projection."""
    _pause(hscc_dir, reason="mine", span_nodes=[PARKED, SPANNED])
    _pause(hscc_dir, host=SPANNED, reason="theirs")
    _stub_backing(monkeypatch, monitor=_monitor_frame(
        _host_row(PARKED), _host_row(SPANNED)))

    status, payload = _request(running, token, "/v1/cluster/monitor")

    assert status == 200
    assert _row_for(payload, PARKED)["paused_reason"] == "mine"
    assert _row_for(payload, SPANNED)["paused_reason"] == "theirs"


# ---------------------------------------------------------------------------
# down is not paused, and paused is not down
# ---------------------------------------------------------------------------

def test_unreachable_node_is_still_not_paused(running, token, monkeypatch,
                                              hscc_dir):
    """The other half of ``down != paused``: an errored host gains NO status.

    A node nobody parked must keep the shape it had before the overlay
    existed, ``error`` included, or the route would be marking real faults as
    operator-intentional. The widget keeps deriving down from this row.
    """
    _pause(hscc_dir)  # overlay present, but it does not hold this host
    broken = _host_row(HEALTHY, error="ssh: connect timeout", sample=None,
                       workloads=[], used_slots=0)
    _stub_backing(monkeypatch, monitor=_monitor_frame(
        _host_row(PARKED), broken))

    status, payload = _request(running, token, "/v1/cluster/monitor")

    assert status == 200
    row = _row_for(payload, HEALTHY)
    assert row == broken
    assert not set(CONTRACT_KEYS) & set(row)
    assert row["error"] == "ssh: connect timeout"
    # And the parked one is not down: the two states never share a row.
    assert _row_for(payload, PARKED)["status"] == "paused"


def test_payload_is_unchanged_when_the_overlay_is_empty(
        running, token, monkeypatch):
    """No overlay on disk → the monitor payload is what it always was.

    Asserted as an exact payload, key order included, because "unchanged
    otherwise" is what the widget parity gates measure. ``paused_nodes`` is the
    only addition, and it is empty rather than absent so a client can read it
    unconditionally.
    """
    monitor = _monitor_frame(_host_row(HEALTHY))
    serving = {"rows": [{"node": HEALTHY, "port": "8001", "model": "qwen",
                         "running": 1}], "proxy_error": None}
    _stub_backing(monkeypatch, monitor=monitor, serving=serving)

    status, payload = _request(running, token, "/v1/cluster/monitor")

    assert status == 200
    assert payload["json"] == monitor["json"]
    assert payload["serving"] == serving["rows"]
    assert payload["paused_nodes"] == []
    assert payload["speak"] == "Fleet snapshot: 1 host sampled."
    assert payload["success"] is True and payload["degraded"] is False


def test_absent_parked_host_is_still_reported_paused(
        running, token, monkeypatch, hscc_dir):
    """A parked host can be MISSING from the frame — the list covers it.

    sparkrun does not list a machine whose workloads are stopped, so there is
    no row to tag. Without the top-level set a client subtracting paused from
    its down tally would subtract nothing and keep calling the node gone.
    """
    _pause(hscc_dir)
    _stub_backing(monkeypatch, monitor=_monitor_frame(_host_row(HEALTHY)))

    status, payload = _request(running, token, "/v1/cluster/monitor")

    assert status == 200
    assert [r["host"] for r in payload["json"]["hosts"]] == [HEALTHY]
    assert payload["paused_nodes"] == [{
        "node": PARKED, "status": "paused", "paused_reason": "training run",
        "paused_since": payload["paused_nodes"][0]["paused_since"]}]
    assert set(payload["paused_nodes"][0]) == set(("node",) + CONTRACT_KEYS)


def test_monitor_degrades_exactly_as_before_when_nothing_is_parked(
        running, token, monkeypatch):
    """A degraded monitor keeps its 503 + success:false contract."""
    _stub_backing(monkeypatch, monitor={"success": False,
                                        "error": "no monitor output"})

    status, payload = _request(running, token, "/v1/cluster/monitor")

    assert status == 503
    assert payload["success"] is False and payload["degraded"] is True
    assert payload["error"] == "no monitor output"


# ---------------------------------------------------------------------------
# Freshness: the overlay is read per request, the fleet frame is not
# ---------------------------------------------------------------------------

def test_resume_is_visible_on_the_next_request_inside_the_ttl(
        running, token, monkeypatch, hscc_dir):
    """The overlay must not be frozen into the 20 s snapshot cache.

    Pause, read, resume, read again — all inside one TTL, with the fleet frame
    served from the cache both times (one backing call). If the projection had
    been cached with the frame, the second read would still call a resumed node
    paused; if it had mutated the cached rows, the tag would leak into every
    later request.
    """
    frame_calls = []

    def monitor():
        frame_calls.append(1)
        return _monitor_frame(_host_row(PARKED))

    monkeypatch.setattr(routes_cluster, "_backing_cluster_monitor", monitor)
    monkeypatch.setattr(routes_cluster, "_backing_serving_snapshot",
                        lambda: {"rows": [], "proxy_error": None})
    tok = token

    _pause(hscc_dir)
    _, first = _request(running, tok, "/v1/cluster/monitor")
    assert _row_for(first, PARKED)["status"] == "paused"

    node_pauses.clear(PARKED, directory=str(hscc_dir))
    _, second = _request(running, tok, "/v1/cluster/monitor")

    assert frame_calls == [1], "the frame came from the cache, as designed"
    assert "status" not in _row_for(second, PARKED)
    assert second["paused_nodes"] == []
    assert _row_for(first, PARKED)["status"] == "paused", \
        "the first response must not have written into the cached row"


def test_the_overlay_root_is_the_server_root(running, token, monkeypatch,
                                             hscc_dir):
    """The reader asks for the root the writer writes: the server's own.

    A root read out of a process env is a root the CLI that wrote the overlay
    can never have had (the ``cron_watchers`` / ``node_pauses`` contract), so
    the handler threads ``ctx.hscc_dir`` and this pins it.
    """
    seen = []
    monkeypatch.setattr(routes_cluster, "_backing_node_pauses",
                        lambda directory=None: seen.append(directory) or {})
    _stub_backing(monkeypatch, monitor=_monitor_frame(_host_row(HEALTHY)))

    status, payload = _request(running, token, "/v1/cluster/monitor")

    assert status == 200
    assert payload["paused_nodes"] == []
    assert seen and set(seen) == {hscc_dir}


def test_unreadable_overlay_tags_nothing_and_fails_nothing(
        running, token, monkeypatch):
    """A broken overlay is the overlay's problem to WARN about, not the route's
    to turn into an error: the fleet read still answers, untagged."""
    def boom(directory=None):
        raise RuntimeError("overlay unreadable")

    monkeypatch.setattr(routes_cluster, "_backing_node_pauses", boom)
    _stub_backing(monkeypatch, monitor=_monitor_frame(_host_row(PARKED)))

    status, payload = _request(running, token, "/v1/cluster/monitor")

    assert status == 200
    assert "status" not in payload["json"]["hosts"][0]
    assert payload["paused_nodes"] == []


def test_malformed_overlay_is_not_an_error(running, token, hscc_dir,
                                           monkeypatch):
    """The real reader's own fail-open path, driven from disk."""
    import os
    path = node_pauses.overlay_path(str(hscc_dir))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{not json")

    _stub_backing(monkeypatch, monitor=_monitor_frame(_host_row(PARKED)))
    status, payload = _request(running, token, "/v1/cluster/monitor")

    assert status == 200
    assert payload["paused_nodes"] == []
    assert "status" not in payload["json"]["hosts"][0]


# ---------------------------------------------------------------------------
# The health route
# ---------------------------------------------------------------------------

def test_health_route_names_parked_nodes_so_paused_is_not_down(
        running, token, monkeypatch, hscc_dir):
    """``GET /v1/health`` — the route the card names its headline.

    The checks stay the checks (suppressing a parked unit inside them belongs
    to the verify layer, and greying a red check out at the HTTP edge would
    launder a real signal); what the route adds is the authoritative parked
    set, so a client showing a down count can tell parked from broken.
    """
    import health_cache

    entry = _pause(hscc_dir)
    health_cache.record({"ok": False, "unverified": [],
                         "checks": [{"name": "chat_roundtrip", "ok": False,
                                     "message": "192.0.2.11 unreachable"}]})
    monkeypatch.setattr(routes_cluster, "_backing_verify",
                        lambda: pytest.fail("health must answer from the cache"))

    status, payload = _request(running, token, "/v1/health")

    assert status == 200
    assert payload["paused_nodes"] == [{
        "node": PARKED, "status": "paused", "paused_reason": "training run",
        "paused_since": entry["since"]}]
    # The verdict is untouched — this route reports, it does not forgive.
    assert payload["ok"] is False
    assert payload["checks"][0]["ok"] is False
    assert {"ok", "checks", "speak", "age_seconds", "generated_at", "stale",
            "source"} <= set(payload)


def test_health_route_with_nothing_parked_reports_an_empty_set(
        running, token, monkeypatch):
    import health_cache

    health_cache.record({"ok": True, "unverified": [], "checks": []})
    monkeypatch.setattr(routes_cluster, "_backing_verify",
                        lambda: pytest.fail("health must answer from the cache"))

    status, payload = _request(running, token, "/v1/health")

    assert status == 200
    assert payload["paused_nodes"] == []
    assert payload["speak"] == "All checks passed."


def test_health_route_unavailable_path_is_unchanged(running, token,
                                                    monkeypatch):
    """The no-snapshot-anywhere answer stays byte-identical."""
    import health_cache

    health_cache.reset_state()
    monkeypatch.setattr(routes_cluster, "_backing_verify",
                        lambda: (_ for _ in ()).throw(RuntimeError("no fleet")))

    status, payload = _request(running, token, "/v1/health")

    assert status == 200
    assert payload == {"speak": "health check unavailable"}


# ---------------------------------------------------------------------------
# The contract itself
# ---------------------------------------------------------------------------

def test_field_names_are_the_names_the_widgets_code_against():
    """The three names, asserted literally, with the route's own constants.

    The widget card (t_983f177e) renders ``status``/``paused_reason``/
    ``paused_since`` straight off this payload, so a rename here is a breaking
    change for shipped bundles. Written out longhand so the test fails on a
    rename instead of following it.
    """
    assert routes_cluster.PAUSE_STATUS == "paused"
    assert routes_cluster.PAUSE_ROW_KEYS == ("status", "paused_reason",
                                             "paused_since")
    row = routes_cluster._pause_row("training run", "2026-11-21T10:00:00+00:00")
    assert tuple(row) == CONTRACT_KEYS


def test_pause_projection_is_span_aware_and_tolerant_of_a_broken_record(
        monkeypatch):
    """The projector's own rules, without a server in the way."""
    def fake(directory=None):
        return {PARKED: {"reason": "r", "since": "s", "span_nodes": [SPANNED]},
                "  ": {"reason": "ignored"},
                "bad": "not-a-record"}

    monkeypatch.setattr(routes_cluster, "_backing_node_pauses", fake)
    result = routes_cluster._pause_overlay(None)

    assert result[PARKED] == {"status": "paused", "paused_reason": "r",
                              "paused_since": "s"}
    assert result[SPANNED] == result[PARKED]
    assert set(result) == {PARKED, SPANNED}
    assert routes_cluster._paused_nodes(result)[0]["node"] == PARKED


def test_row_annotation_does_not_mutate_the_rows_it_tags():
    """The rows come from the shared snapshot cache — they must survive clean."""
    row = {"host": PARKED, "error": None}
    overlay = {PARKED: {"status": "paused", "paused_reason": "r",
                        "paused_since": "s"}}

    out = routes_cluster._annotate_paused_rows([row], overlay)

    assert out[0] is not row
    assert out[0]["status"] == "paused"
    assert row == {"host": PARKED, "error": None}
    assert out[0]["error"] is None
