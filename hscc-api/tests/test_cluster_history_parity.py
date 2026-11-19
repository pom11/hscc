"""Parity: GET /v1/cluster/history returns EXACTLY what the recorder holds.

The card's rule is that this route may not re-implement the aggregation. So
every test here drives the recorder's OWN write path to build the fixture
(:meth:`metrics_history.MetricsRecorder.tick`, which rolls tier 0 -> 1 -> 2 with
its own ``rollup``), then reads the same store back through the recorder's OWN
read API (:meth:`MetricsStore.host_rows` / :meth:`engine_rows`,
:meth:`MetricsRecorder.tier0_points`) and compares that against what the route
put on the wire. The oracle is the recorder, never a re-derived mean: where the
response is a projection (``?metrics=``) the comparison filters the oracle's own
keys and nothing else.

One fixture, both sides: the same ``hscc_dir`` is the server's and the
recorder's, so the two cannot be looking at different data.

Read-only is a requirement, not a style note: a request here must not sample the
fleet. Three tests guard it — the live backing seam poisoned, an AST sweep of
the handler, and a missing store that must still be missing afterwards.

Fixtures are TEST-NET-1 addresses (192.0.2.x) and one placeholder tailnet
address; no real host, tenant or token is read.
"""

import ast
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

import api_server
import routes_cluster
from test_api import RunningServer

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hscc_daemon import metrics_history as mh          # noqa: E402

HOST_A = "192.0.2.10"                                   # TEST-NET-1, never a node
HOST_B = "192.0.2.11"
TAILNET = "100.64.0.1"                                  # placeholder, doc example only
PORT = "8000"
MODEL = "org/Qwen-Test"

#: aligned to 2 s, 1 min and 5 min, so every bucket boundary in the fixture is
#: a real one; the route's clock is pinned to this same instant.
NOW = 1_795_000_000 - (1_795_000_000 % 300)
TICKS = 360                     # 12 minutes at 2 s: 11 closed minutes, 2 tier-2 buckets


# ── the recorder's own input shapes (sparkrun's names, CSV strings) ────────

def sample(ts, host=HOST_A, **over):
    s = {"timestamp": str(int(ts)), "hostname": host,
         "cpu_usage_pct": "6.2", "cpu_temp_c": "54.0",
         "gpu_util_pct": "94", "gpu_temp_c": "71", "gpu_power_w": "160.0",
         "mem_used_pct": "31.5", "swap_used_mb": "0", "swap_total_mb": "8192",
         "swap_used_pct": "", "gpu_mem_used_mb": "12000",
         "gpu_clock_mhz": "1450", "net_recv_bytes_s": "1024",
         "net_send_bytes_s": "2048", "sparkrun_jobs": "1"}
    s.update(over)
    return s


def monitor_frame(ts):
    """A ``sparkrun cluster monitor --json`` frame: one host answers normally,
    one never reports its GPU (the ``None`` path must survive the round trip)."""
    return {"hosts": [
        {"host": HOST_A, "error": None,
         "sample": sample(ts, gpu_power_w=str(100.0 + (ts % 60)))},
        {"host": HOST_B, "error": None,
         "sample": sample(ts, host=HOST_B, gpu_temp_c="", gpu_power_w="",
                          gpu_util_pct="0")},
    ]}


def serving_rows(ts):
    """A ``ServingPoller.poll()`` row list — one backend on HOST_A."""
    return [{"node": HOST_A, "port": PORT, "model": MODEL,
             "running": 2, "waiting": 1, "kv": 12.5,
             "gen_tps": 40.0, "prompt_tps": 200.0,
             "prompt_total": 900000.0, "gen_total": 250000.0}]


@pytest.fixture
def store_dir(tmp_path):
    """The recorder's real store, filled by the recorder's own code path.

    Nothing in this file calls ``write_rollup`` or ``rollup`` by hand: ticks go
    in through :meth:`MetricsRecorder.tick` (the 2 s path the daemon uses), and
    the roll-ups that produce tiers 1 and 2 are the recorder's own.
    """
    rec = mh.MetricsRecorder(hscc_dir=str(tmp_path))
    for i in range(TICKS):
        ts = NOW - (TICKS * mh.TIER0_INTERVAL_S) + i * mh.TIER0_INTERVAL_S
        rec.tick(monitor_frame(ts), serving_rows(ts), ts)
    rec.tick(monitor_frame(NOW), serving_rows(NOW), NOW)     # closes the last minute
    assert rec.store.count(1) > 0, "fixture produced no tier-1 rows"
    assert rec.store.count(2) > 0, "fixture produced no tier-2 rows"
    rec.store.close()
    return tmp_path


@pytest.fixture
def running(store_dir):
    """The live server, serving the SAME hscc_dir the recorder just wrote."""
    srv = RunningServer(hscc_dir=str(store_dir))
    yield srv
    srv.close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


@pytest.fixture
def pinned_clock(running):
    """Pin the route's clock to the fixture instant, so `since`/`until` are
    reproducible and the window edge cannot drift between the two sides."""
    original = routes_cluster._history_now
    routes_cluster._history_now = lambda: float(NOW)
    yield NOW
    routes_cluster._history_now = original


class FakeStream:
    """Stands in for ``cluster_live.HostStream`` without spawning anything.

    The route must never build a Sampler (that spawns a
    ``sparkrun cluster monitor``), so the ring test installs one that was handed
    fakes — the same stand-ins the recorder's own suite uses.
    """

    def __init__(self, interval, cluster=None):
        self.interval, self.cluster = interval, cluster
        self.frame: dict | None = None
        self.error: str | None = None

    def close(self):
        pass


class FakePoller:
    """Stands in for ``cluster_live.ServingPoller``."""

    def poll(self):
        return []

    def close(self):
        pass


@pytest.fixture
def ring(store_dir):
    """A sampler resident IN this process, owning the tier-0 ring.

    The deployed API is a separate process and cannot own it — that case has its
    own test. Here the route must serve the ring the recorder really holds.
    """
    rec = mh.MetricsRecorder(hscc_dir=str(store_dir))
    for i in range(3):
        rec.tick(monitor_frame(NOW - 4 + i * 2), serving_rows(NOW - 4 + i * 2),
                 NOW - 4 + i * 2)
    mh.set_sampler(mh.Sampler(host_stream=FakeStream(mh.TIER0_INTERVAL_S),
                              serving=FakePoller(), recorder=rec))
    yield rec
    mh.close_sampler()


def get(running, token, path):
    status, payload = running.request(token=token, path=path)
    return status, payload


# ── the parity gates: one per tier, oracle = the recorder's own read API ────

@pytest.mark.parametrize("window,tier,resolution", [
    ("1h", 1, "60s"),
    ("6h", 1, "60s"),
    ("24h", 1, "60s"),
    ("7d", 2, "300s"),
])
def test_durable_windows_return_the_recorders_rows_verbatim(
        running, token, pinned_clock, store_dir, window, tier, resolution):
    """Every durable window: the route's rows == ``host_rows``/``engine_rows``
    for the (tier, since, until, node) the route itself reported."""
    status, api = get(running, token, f"/v1/cluster/history?window={window}")
    assert status == 200, api
    assert api["tier"] == tier and api["resolution"] == resolution
    assert api["window"] == window and api["window_s"] == {
        "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}[window]

    store = mh.MetricsStore(hscc_dir=str(store_dir))
    try:
        want_hosts = store.host_rows(tier, since=api["since"],
                                     until=api["until"], host=None)
        want_engines = store.engine_rows(tier, since=api["since"],
                                         until=api["until"], node=None)
    finally:
        store.close()
    assert want_hosts and api["hosts"] == want_hosts
    assert want_engines and api["engines"] == want_engines
    # The response must never leave a client guessing which tier it is looking at.
    assert api["resolution_s"] == (60 if tier == 1 else 300)
    assert api["max_fields"] == [c for c in mh.HOST_COLUMNS if c.endswith("_max")]
    for row in api["hosts"]:
        assert set(api["max_fields"]) <= set(row)
    assert api["speak"] and window in api["speak"] and resolution in api["speak"]


def test_a_missing_reading_stays_none_and_never_becomes_zero(
        running, token, pinned_clock, store_dir):
    """HOST_B never reported a GPU. Its stored None must arrive as null: a 0.0
    here would draw a node that never answered as an idle one."""
    status, api = get(running, token,
                      f"/v1/cluster/history?window=1h&node={HOST_B}")
    assert status == 200, api
    rows = [r for r in api["hosts"] if r["host"] == HOST_B]
    assert rows
    assert api["engines"] == [] and api["counts"]["engines"] == 0
    store = mh.MetricsStore(hscc_dir=str(store_dir))
    try:
        want = store.host_rows(1, since=api["since"], until=api["until"],
                               host=HOST_B)
    finally:
        store.close()
    assert api["hosts"] == want
    for row in rows:
        assert row["gpu_power_w"] is None
        assert row["gpu_power_w_max"] is None


def test_peak_column_is_the_recorders_peak_not_a_rederived_one(
        running, token, pinned_clock, store_dir):
    """The reason tier 1/2 store a peak at all: the spike the average erased.
    The route carries the recorder's column, byte for byte."""
    status, api = get(running, token,
                      f"/v1/cluster/history?window=1h&node={HOST_A}"
                      "&metrics=gpu_power_w,gpu_power_w_max")
    assert status == 200, api
    store = mh.MetricsStore(hscc_dir=str(store_dir))
    try:
        want = store.host_rows(1, since=api["since"], until=api["until"],
                               host=HOST_A)
    finally:
        store.close()
    assert api["hosts"], api
    for got, src in zip(api["hosts"], want):
        assert got["gpu_power_w"] == src["gpu_power_w"]
        assert got["gpu_power_w_max"] == src["gpu_power_w_max"]
        assert got["gpu_power_w_max"] >= (src["gpu_power_w"] or 0)
        assert set(got) == {"tier", "ts", "host", "n", "error",
                            "gpu_power_w", "gpu_power_w_max"}


def test_window_15m_serves_the_ring_this_process_owns(
        running, token, pinned_clock, ring):
    """Tier 0 parity: the rows == ``tier0_points()`` transposed, values taken
    from the recorder's own read (never recomputed here)."""
    status, api = get(running, token, "/v1/cluster/history?window=15m")
    assert status == 200, api
    assert api["tier"] == 0 and api["resolution"] == "2s"
    assert api["store"]["tier0_resident"] is True
    assert api["max_fields"] == []                  # raw samples have no peak
    points = ring.tier0_points()
    assert len(points) == 3
    # per-node: one row per (point, host), keyed by the recorder's own host map
    assert len(api["hosts"]) == 3 * 2
    by_point = {p["ts"]: p for p in points}
    for row in api["hosts"]:
        src = by_point[row["ts"]]["hosts"][row["host"]]
        for key, value in src.items():
            assert row[key] == value, (row["ts"], row["host"], key)
        assert row["n"] == by_point[row["ts"]]["n"]
        assert row["tier"] == 0 and row["res"] == by_point[row["ts"]]["res"]
    # per-engine: same identity the recorder keys them by, (node, port)
    assert len(api["engines"]) == 3
    for row in api["engines"]:
        src = next(e for e in by_point[row["ts"]]["engines"]
                   if (e["node"], e["port"]) == (row["node"], row["port"]))
        assert row["model"] == src["model"]
        assert row["run"] == src["run"] and row["wait"] == src["wait"]
        assert row["kv"] == src["kv"] and row["gen_tps"] == src["gen_tps"]
        assert row["tier"] == 0 and row["n"] == by_point[row["ts"]]["n"]


def test_window_15m_says_the_ring_is_not_here_rather_than_nothing_happened(
        running, token, pinned_clock, store_dir):
    """The deployed topology: the API is its own process, so the daemon's ring
    is unreachable. An empty list must never be reported as if it were data."""
    mh.close_sampler()                              # nothing resident in this process
    status, api = get(running, token, "/v1/cluster/history?window=15m")
    assert status == 200, api
    assert api["tier"] == 0 and api["resolution_s"] == 2
    assert api["hosts"] == [] and api["engines"] == []
    assert api["store"]["tier0_resident"] is False
    assert "daemon" in api["note"] and "window=1h" in api["note"]
    assert "1-minute" in api["speak"]
    # ... while the durable tiers, from the same file, are still served.
    status2, api2 = get(running, token, "/v1/cluster/history?window=1h")
    assert status2 == 200 and api2["hosts"] and api2["store"]["exists"] is True


def test_a_durable_tier_never_claims_a_tier0_residency_it_did_not_measure(
        running, token, pinned_clock, store_dir):
    """The flag is a measurement of tier 0, or it is silent — never a default.

    It used to be initialised to `True` and written only inside the tier-0
    branch, so every durable window answered `tier0_resident: true` in a process
    that hosts no recorder at all, while `window=15m` in the same process answered
    `false`. A tier this route never read must not speak for it: `None` is the
    only honest answer, and the key stays on the wire so the shape is unchanged.
    """
    mh.close_sampler()                              # the deployed topology
    assert mh.resident_recorder() is None           # ... really, nothing resident
    for window, tier in (("1h", 1), ("6h", 1), ("24h", 1), ("7d", 2)):
        status, api = get(running, token, f"/v1/cluster/history?window={window}")
        assert status == 200, api
        assert api["tier"] == tier
        assert "tier0_resident" in api["store"], "key must stay on the wire"
        assert api["store"]["tier0_resident"] is None, (window, api["store"])
        # The tier it DID read is still stated as a measurement: served, present,
        # and not smuggling a gap into an answer that has none.
        assert api["store"]["exists"] is True and api["hosts"]
        assert api["note"] == "" and "ring" not in api["speak"]
    # And tier 0 in the same process still measures it: `false`, not unknown.
    status, api = get(running, token, "/v1/cluster/history?window=15m")
    assert status == 200 and api["tier"] == 0
    assert api["store"]["tier0_resident"] is False


# ── metrics= is a projection of the recorder's rows, not a second query ────

def test_metrics_projection_keeps_identity_and_the_named_fields_only(
        running, token, pinned_clock, store_dir):
    status, api = get(running, token,
                      "/v1/cluster/history?window=7d&metrics=gpu_util_pct,gen_tps")
    assert status == 200, api
    store = mh.MetricsStore(hscc_dir=str(store_dir))
    try:
        want_hosts = store.host_rows(2, since=api["since"], until=api["until"])
        want_engines = store.engine_rows(2, since=api["since"], until=api["until"])
    finally:
        store.close()
    assert api["metrics"] == ["gpu_util_pct", "gen_tps"]
    assert api["hosts_included"] is True and api["engines_included"] is True
    for got, src in zip(api["hosts"], want_hosts):
        assert got == {k: v for k, v in src.items()
                       if k in ("tier", "ts", "host", "n", "error",
                                "gpu_util_pct")}
    for got, src in zip(api["engines"], want_engines):
        assert got == {k: v for k, v in src.items()
                       if k in ("tier", "ts", "node", "port", "model", "n",
                                "error", "gen_tps")}


def test_a_metrics_filter_that_names_no_engine_metric_excludes_the_series(
        running, token, pinned_clock, store_dir):
    """Excluded is said out loud: an empty `engines` list with no flag would read
    as 'the fleet served nothing'."""
    status, api = get(running, token,
                      "/v1/cluster/history?window=1h&metrics=gpu_util_pct")
    assert status == 200, api
    assert api["engines"] == [] and api["engines_included"] is False
    assert api["hosts"] and api["hosts_included"] is True
    status, api = get(running, token,
                      "/v1/cluster/history?window=1h&metrics=run")
    assert status == 200, api
    assert api["hosts"] == [] and api["hosts_included"] is False
    assert api["engines"] and api["engines_included"] is True


# ── validation: a clear 400 naming the rule, never a silent empty answer ───

@pytest.mark.parametrize("path,needle", [
    ("/v1/cluster/history?window=30m",
     "window must be one of 15m, 1h, 6h, 24h, 7d"),
    ("/v1/cluster/history?window=1h&metrics=bogus", "unknown metric 'bogus'"),
    ("/v1/cluster/history?window=1h&metrics=%2C%2C", "at least one field"),
    ("/v1/cluster/history?window=15m&metrics=gpu_temp_c_max",
     "not stored for a 15 m window"),
    ("/v1/cluster/history?node=%20", "must be 'all' or one host name"),
    ("/v1/cluster/history?node=two%20hosts", "is not a host name"),
    ("/v1/cluster/history?node=a/b", "is not a host name"),
    ("/v1/cluster/history?tier=1", "'tier' is not supported"),
    ("/v1/cluster/history?host=192.0.2.10", "'host' is not supported"),
])
def test_every_parameter_is_validated_with_an_actionable_400(
        running, token, pinned_clock, store_dir, path, needle):
    status, api = get(running, token, path)
    assert status == 400, (path, status, api)
    assert api["error"]["code"] == "bad_request", path
    assert needle in api["error"]["message"], (path, api["error"])
    assert api["error"]["speak"], path


def test_a_blank_param_is_the_same_request_as_no_param(running, token,
                                                       pinned_clock, store_dir):
    """The dispatcher's `parse_qs` drops blank values before any handler sees
    them, so `?node=` and `?metrics=` cannot mean anything but the default.
    Said out loud here because a client that sends them expects a filter."""
    status, api = get(running, token,
                      "/v1/cluster/history?window=1h&node=&metrics=")
    assert status == 200, api
    assert api["node"] == "all"
    assert set(api["metrics"]) == set(mh.HOST_COLUMNS) | set(mh.ENGINE_KEYS)
    assert api["hosts"] and api["engines"]


def test_the_unknown_metric_error_names_the_recorders_own_vocabulary(
        running, token, pinned_clock, store_dir):
    """The list comes from the recorder module, so a rename there cannot leave
    this route advertising a column that no longer exists."""
    status, api = get(running, token,
                      "/v1/cluster/history?window=1h&metrics=nope")
    assert status == 400
    words = {w.strip(",;") for w in api["error"]["message"].split()}
    assert set(mh.HOST_COLUMNS) | set(mh.ENGINE_KEYS) <= words


def test_a_well_formed_node_with_no_history_is_an_empty_answer_not_an_error(
        running, token, pinned_clock, store_dir):
    """A node that never reported is a fact about the window, not a bad request;
    the payload says which (a 400 here would be wrong for a node that was simply
    down for the whole window)."""
    status, api = get(running, token,
                      f"/v1/cluster/history?window=1h&node={TAILNET}")
    assert status == 200, api
    assert api["hosts"] == [] and api["engines"] == []
    assert api["node"] == TAILNET and api["counts"] == {"hosts": 0, "engines": 0}
    assert "unavailable" not in str(api)


# ── read-only: this route must never sample the fleet ──────────────────────

def test_the_handler_names_no_process_socket_or_monitor_seam():
    """An AST sweep, the same guard the recorder's own suite uses: the cheapest
    way for a read route to start ssh-ing is to reuse a sibling's backing call."""
    src = inspect.getsource(routes_cluster.handle_cluster_history)
    tree = ast.parse(src)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    forbidden = {"subprocess", "Popen", "run_cmd", "socket", "connect",
                 "HostStream", "ServingPoller", "cmd_monitor",
                 "cmd_cluster_status", "_backing_cluster_monitor",
                 "_backing_serving_snapshot", "_backing_cluster_status",
                 "_load_cluster_engine", "get_sampler", "ssh"}
    assert not (names & forbidden), names & forbidden


def test_a_request_never_reaches_a_live_backing_or_a_process(
        running, token, pinned_clock, store_dir, monkeypatch):
    """Every live seam in the module (and the exec layer under it) raises if
    touched, while all five windows are requested.

    ssh/sparkrun can only reach the fleet through `subprocess`/`os.exec*`, so
    that is the layer poisoned, together with the module's own live backings and
    the recorder's sampler constructors. Patching `socket` itself is deliberately
    NOT done: this fixture's loopback client shares the process and would be
    broken by it, which would prove nothing — the AST sweep above is what covers
    socket use here.
    """
    def poison(*args, **kwargs):
        raise AssertionError("history route touched a live backing")
    for name in ("_backing_cluster_monitor", "_backing_cluster_status",
                 "_backing_cluster_hosts", "_backing_cluster_jobs",
                 "_backing_cluster_info", "_backing_serving_snapshot",
                 "_load_cluster_engine"):
        monkeypatch.setattr(routes_cluster, name, poison)
    monkeypatch.setattr(mh, "get_sampler", poison)
    monkeypatch.setattr(mh, "Sampler", poison)
    monkeypatch.setattr(mh, "host_stream_cls", poison)
    monkeypatch.setattr(mh, "serving_poller_cls", poison)
    monkeypatch.setattr(subprocess, "run", poison)
    monkeypatch.setattr(subprocess, "Popen", poison)
    monkeypatch.setattr(os, "system", poison)
    monkeypatch.setattr(os, "popen", poison)
    monkeypatch.setattr(os, "posix_spawn", poison)
    for window in ("15m", "1h", "6h", "24h", "7d"):
        status, api = get(running, token, f"/v1/cluster/history?window={window}")
        assert status == 200, (window, api)
        assert api.get("error") is None, (window, api)


def test_a_read_does_not_create_the_store(tmp_path):
    """A widget poll against a fresh install must not leave an empty database
    behind: the recorder owns that file, and an empty one looks like a source."""
    srv = RunningServer(hscc_dir=str(tmp_path))
    try:
        tok = api_server.load_token(srv.server.ctx.hscc_dir)
        db = mh.db_path(hscc_dir=str(tmp_path))
        status, api = srv.request(token=tok,
                                  path="/v1/cluster/history?window=24h")
        assert status == 200, api
        assert api["hosts"] == [] and api["store"]["exists"] is False
        assert "not written its store yet" in api["note"]
        assert not os.path.exists(db), f"the read created {db}"
        assert not os.path.exists(os.path.dirname(db))
    finally:
        srv.close()


def test_the_route_is_registered_and_answered_by_the_history_handler():
    """The register half of the widget contract (the Swift-side half is
    test_contract_swift_routes.py, which covers the path once the extension
    client can ask for it)."""
    handlers = {(p.pattern, h) for (m, p, h) in api_server.ROUTES
                if m == "GET" and p.pattern == r"^/v1/cluster/history$"}
    assert handlers == {(r"^/v1/cluster/history$",
                         routes_cluster.handle_cluster_history)}


def test_every_window_states_the_resolution_and_the_peaks_it_carries(
        running, token, pinned_clock, store_dir, ring):
    """The amendment's rule: a client must never guess whether it is looking at
    2-second, 1-minute or 5-minute points."""
    seen = {}
    for window in ("15m", "1h", "6h", "24h", "7d"):
        status, api = get(running, token, f"/v1/cluster/history?window={window}")
        assert status == 200, (window, api)
        for key in ("tier", "resolution_s", "resolution", "tier_summary",
                    "max_fields", "since", "until", "window_s"):
            assert key in api, (window, key)
        assert api["window"] == window
        assert api["since"] == api["until"] - api["window_s"]
        seen[window] = (api["tier"], api["resolution_s"], api["max_fields"])
    assert seen["15m"] == (0, 2, [])
    assert seen["1h"][0] == seen["6h"][0] == seen["24h"][0] == 1
    assert seen["1h"][1] == 60 and seen["7d"] == (2, 300, seen["1h"][2])
