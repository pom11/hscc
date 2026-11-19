"""Tests for GET /v1/metrics/history — the three-tier history read (t_5ae8cfcf).

The contract this file exists to hold is the one a chart silently breaks on:
the route passes the recorder's field names through UNRENAMED and its ``None``s
UNCOALESCED, and it states which tier-0 state it is in rather than letting an
empty window read as "nothing happened". Every row assertion therefore compares
the route's rows with what ``MetricsStore`` / ``MetricsRecorder`` return for the
same query, in the same call, on the same fixture state — the route-equals-the-
library rule the widget routes live by (``test_widget_route_parity.py``).

Hermetic: the store lives under the per-test tmp ``~/.hscc`` (the conftest's
autouse ``expanduser`` redirect covers ``metrics_history.db_path``, which
resolves at call time), the tier-0 ring is a recorder installed with
``set_sampler`` and torn down by the fixture, and ``get_sampler`` is fenced so a
regression that lets a GET start a ``sparkrun cluster monitor`` fails here
instead of on the operator's machine.
"""

import datetime
import json
import os
import types

import pytest

import api_server
import routes_metrics


HOST_A, HOST_B = "192.0.2.10", "192.0.2.11"
PORT = "8000"


# --------------------------------------------------------------------------- #
# Fixture state — built by the recorder's own writers, not hand-written rows
# --------------------------------------------------------------------------- #

def _frame(ts, *, host=HOST_A, cpu=10.0, gpu_temp=60.0, gpu_power=120.0,
           error=None, sample=True):
    """A sparkrun monitor frame shaped like ``HostStream.frame``."""
    entry = {"host": host, "error": error, "sample": None,
             "workloads": [], "used_slots": 0, "free_slots": 1}
    if sample:
        # The collector's CSV names, verbatim, because that is what
        # ``host_metrics`` reads (empty string = "not reported", NOT a zero).
        entry["sample"] = {
            "timestamp": str(ts),
            "cpu_usage_pct": str(cpu), "cpu_temp_c": "44",
            "gpu_util_pct": "80", "gpu_temp_c": str(gpu_temp),
            "gpu_power_w": str(gpu_power), "mem_used_pct": "30",
            "swap_used_mb": "100", "swap_total_mb": "1000",
            "gpu_mem_used_mb": "40000", "gpu_clock_mhz": "1800",
            "net_recv_bytes_s": "1000", "net_send_bytes_s": "2000",
            "sparkrun_jobs": "1",
        }
    return {"timestamp": float(ts), "hosts": [entry]}


def _serving_rows(ts):
    return [{"node": HOST_A, "port": PORT, "model": "qwen-test",
             "running": 2, "waiting": 1, "kv": 0.4,
             "gen_tps": 31.5, "prompt_tps": 12.0}]


def _recorder():
    """A recorder on the CALL-TIME ``~/.hscc`` — i.e. the conftest's redirect.

    No ``hscc_dir=``: both sides of every assertion here (the writer and the
    route's reader) must resolve the same path, and the store resolves it at
    call time precisely so the suite's autouse ``expanduser`` redirect covers
    it. Handing the fixture a tmp dir instead would build the rows somewhere the
    route never looks, and every read assertion would pass on an empty store.
    """
    from hscc_daemon import metrics_history as mh
    return mh.MetricsRecorder()


def _fake_sampler(recorder):
    """A stand-in for the process-wide ``Sampler``.

    Only ``.recorder`` and ``.close()`` are needed — the route reads, it never
    ticks — and ``close_sampler()`` calls ``close()``, so the fake needs one or
    teardown raises.
    """
    return types.SimpleNamespace(recorder=recorder, close=lambda: None)


@pytest.fixture
def mh():
    from hscc_daemon import metrics_history
    return metrics_history


@pytest.fixture
def live_store(tmp_path):
    """A real store at the redirected ``~/.hscc/metrics/history.db``."""
    store = routes_metrics._backing_store()
    yield store
    store.close()


@pytest.fixture
def populated(tmp_path, live_store):
    """Two real minutes of samples, rolled up by the recorder itself.

    The shape is chosen so each assertion has something to bite on:

    * host A answers TWICE inside the :60 bucket at 60 °C then 90 °C, so the
      bucket's average (75) and its peak (90) are different numbers — an
      assertion that only checked the average could not tell a peak column from
      a copied one;
    * host B answers once at :00 and then FAILS in the :120 bucket, so its
      second row is all-``None`` + an ``error`` string rather than zeros;
    * the tick at 200 s closes both buckets (a bucket is only rolled once its
      whole span has passed), so the rows on disk came through
      ``MetricsRecorder.tick`` -> ``rollup`` -> ``write_rollup`` — the
      production writer, not a hand-written INSERT.
    """
    rec = _recorder()
    rec.tick(_frame(100), _serving_rows(100), 100)
    rec.tick(_frame(100, host=HOST_B, cpu=20.0, gpu_temp=70.0,
                    gpu_power=140.0), None, 100)
    rec.tick(_frame(118, gpu_temp=90.0, gpu_power=200.0), _serving_rows(118),
             118)
    # A host that answered nothing this tick: an error row, not a zero row.
    rec.tick(_frame(130, host=HOST_B, error="collect failed", sample=False),
             None, 130)
    rec.tick(_frame(200), _serving_rows(200), 200)     # closes :60 and :120
    return {"recorder": rec, "store": live_store}


@pytest.fixture
def ring_recorder(tmp_path, populated):
    """Install the populated recorder as this process's sampler."""
    from hscc_daemon import metrics_history as mh
    fake = _fake_sampler(populated["recorder"])
    mh.set_sampler(fake)
    yield populated
    mh.close_sampler()


@pytest.fixture(autouse=True)
def no_sampler_construction(monkeypatch):
    """Fence the one call that would let a GET spawn a second fleet poller.

    ``metrics_history.get_sampler()`` CONSTRUCTS a Sampler on first use — and a
    Sampler spawns ``sparkrun cluster monitor`` plus a second writer on the
    roll-up watermarks. A read route that reaches for the recorder must READ it,
    so any call here is a bug: raise with a message that names the bug.
    """
    from hscc_daemon import metrics_history as mh

    def _forbidden(*a, **kw):
        raise AssertionError(
            "GET /v1/metrics/history called metrics_history.get_sampler() — a "
            "read must never construct the Sampler (it spawns sparkrun cluster "
            "monitor and a second roll-up writer)")

    monkeypatch.setattr(mh, "get_sampler", _forbidden, raising=False)
    yield
    mh.close_sampler()


def _query(**kw):
    return {k: str(v) for k, v in kw.items() if v is not None}


def _get(**kw):
    return routes_metrics.handle_metrics_history(None, None, _query(**kw), b"")


# --------------------------------------------------------------------------- #
# The route exists
# --------------------------------------------------------------------------- #

def test_route_is_registered():
    hits = [(m, p.pattern) for (m, p, _h) in api_server.ROUTES
            if p.match("/v1/metrics/history")]
    assert hits == [("GET", "^/v1/metrics/history$")], hits


def test_tier_is_required_400():
    with pytest.raises(api_server.ApiError) as exc:
        routes_metrics.handle_metrics_history(None, None, {}, b"")
    assert exc.value.status == 400
    assert exc.value.code == "bad_request"
    assert "tier" in exc.value.message


@pytest.mark.parametrize("raw", ["3", "-1", "one", "1.5", " "])
def test_bad_tier_400(raw):
    with pytest.raises(api_server.ApiError) as exc:
        _get(tier=raw)
    assert exc.value.status == 400
    assert "tier" in exc.value.message


@pytest.mark.parametrize("param", ["since", "until"])
def test_bad_epoch_400(param):
    with pytest.raises(api_server.ApiError) as exc:
        _get(tier=1, **{param: "yesterday"})
    assert exc.value.status == 400
    assert param in exc.value.message


# --------------------------------------------------------------------------- #
# Tiers 1 and 2 == the store's own read API, row for row
# --------------------------------------------------------------------------- #

def test_tier1_rows_are_the_stores_rows_verbatim(populated, live_store):
    """The rename/coercion gate: the route's rows ARE ``host_rows``'s rows."""
    status, payload = _get(tier=1)
    assert status == 200
    direct = live_store.host_rows(1)
    assert payload["hosts"] == direct
    assert payload["engines"] == live_store.engine_rows(1)
    assert payload["hosts"], "fixture must have produced a rolled minute"


def test_tier2_rows_come_from_the_stores_tier2_query(populated, live_store):
    """Tier 2 is read through the same reader; the route does not re-fold."""
    from hscc_daemon import metrics_history as mh
    one_minute = live_store.host_rows(1)
    assert one_minute
    live_store.write_rollup(*mh.rollup(one_minute, 2, mh.TIER2_BUCKET_S))
    _status, payload = _get(tier=2)
    assert payload["hosts"] == live_store.host_rows(2)
    assert payload["engines"] == live_store.engine_rows(2)
    assert payload["tier_meta"]["resolution_s"] == mh.TIER2_BUCKET_S
    assert payload["tier_meta"]["window_s"] == mh.TIER2_WINDOW_S


def test_field_names_are_sparkruns_own_names(populated, live_store):
    """A rename is the bug class: the chart reads None and draws a flat line."""
    from hscc_daemon import metrics_history as mh
    _status, payload = _get(tier=1)
    row = payload["hosts"][0]
    for name in mh.HOST_COLUMNS:
        assert name in row, f"{name} missing or renamed"
    assert row["host"] == HOST_A
    engine = payload["engines"][0]
    for name in ("node", "port", "model") + mh.ENGINE_KEYS:
        assert name in engine, f"{name} missing or renamed"


def _host_row(payload, host, ts=None):
    """Pick one node row by (host, bucket) — never by index.

    ``host_rows`` orders by ``ts`` only, so two hosts in the same bucket come
    back in sqlite's primary-key order, which is the store's business and not
    something a test should depend on.
    """
    rows = [r for r in payload["hosts"]
            if r["host"] == host and (ts is None or r["ts"] == ts)]
    assert rows, f"no row for {host} at {ts}: {payload['hosts']}"
    return rows[0]


def test_peaks_are_exposed_alongside_the_averages(populated):
    """Averages are averages: a chart of temperature needs BOTH halves."""
    from hscc_daemon import metrics_history as mh
    _status, payload = _get(tier=1)
    row = _host_row(payload, HOST_A, 60)
    # The :60 bucket holds host A at 60 °C then 90 °C -> mean 75, peak 90.
    assert row["gpu_temp_c"] == 75.0
    assert row["gpu_temp_c_max"] == 90.0
    assert row["gpu_power_w"] == 160.0
    assert row["gpu_power_w_max"] == 200.0
    assert payload["fields"]["host_peaks"] == [f"{k}_max" for k in mh.MAX_KEYS]
    assert set(payload["fields"]["host_peaks"]) <= set(row)


def test_none_stays_none_and_never_becomes_zero(populated, live_store):
    """A node that never answered must not render as an idle one."""
    _status, payload = _get(tier=1)
    rows = {r["ts"]: r for r in payload["hosts"] if r["host"] == HOST_B}
    broken = rows[max(rows)]                      # the "collect failed" tick
    assert broken["error"] == "collect failed"
    for name in ("cpu_usage_pct", "gpu_temp_c", "gpu_power_w"):
        assert broken[name] is None, f"{name} was coerced to {broken[name]!r}"
    assert broken == [r for r in live_store.host_rows(1, host=HOST_B)
                      if r["ts"] == broken["ts"]][0]
    # And it survives the wire as null, not 0.
    assert "null" in json.dumps(broken)


def test_host_and_node_filter_their_own_tables(populated):
    """``host`` is host_rollup's key, ``node`` is engine_rollup's."""
    _status, only_a = _get(tier=1, host=HOST_A)
    assert {r["host"] for r in only_a["hosts"]} == {HOST_A}
    assert only_a["engines"], "host= must not blank the engine half"
    _status, only_b = _get(tier=1, host="192.0.2.99")
    assert only_b["hosts"] == []
    assert only_b["counts"]["host_rows"] == 0
    _status, by_node = _get(tier=1, node=HOST_A)
    assert {e["node"] for e in by_node["engines"]} == {HOST_A}
    _status, wrong_node = _get(tier=1, node=HOST_B)
    assert wrong_node["engines"] == []


def test_since_and_until_are_inclusive_bucket_starts(populated, live_store):
    from hscc_daemon import metrics_history as mh
    _status, payload = _get(tier=1, since=60, until=60)
    assert [r["ts"] for r in payload["hosts"]] == [60, 60]     # both hosts
    assert payload["since"] == 60 and payload["until"] == 60
    _status, second = _get(tier=1, since=120)
    assert [r["ts"] for r in second["hosts"]] == [120]
    _status, all_of_it = _get(tier=1)
    assert len(all_of_it["hosts"]) == len(live_store.host_rows(1))
    assert payload["tier_meta"]["resolution_s"] == mh.TIER1_BUCKET_S


def test_rows_come_back_oldest_first(populated, live_store):
    from hscc_daemon import metrics_history as mh
    live_store.write_rollup(*mh.rollup(live_store.host_rows(1), 2,
                                       mh.TIER2_BUCKET_S))
    _status, payload = _get(tier=2)
    ts = [r["ts"] for r in payload["hosts"]]
    assert ts == sorted(ts) and len(set(ts)) < len(ts)


def test_engine_identity_is_node_and_port_not_model(populated):
    from hscc_daemon import metrics_history as mh
    _status, payload = _get(tier=1)
    assert payload["fields"]["engine_key"] == list(mh.ENGINE_KEY_COLUMNS)
    assert "model" not in mh.ENGINE_KEY_COLUMNS
    engine = payload["engines"][0]
    assert engine["model"] == "qwen-test"          # an attribute, present
    assert (engine["node"], engine["port"]) == (HOST_A, PORT)


# --------------------------------------------------------------------------- #
# Tier 0: the response states WHICH empty it is
# --------------------------------------------------------------------------- #

def test_tier0_serves_the_ring_when_this_process_holds_it(ring_recorder):
    _status, payload = _get(tier=0)
    assert payload["source"] == "recorder_ring"
    assert payload["tier0"]["readable_here"] is True
    assert payload["tier0"]["points"] == 5          # five recorded ticks
    assert len(payload["hosts"]) == payload["counts"]["host_rows"] > 0
    assert payload["hosts"][0]["res"] == "2s"


def test_tier0_host_filter_applies_to_the_ring(ring_recorder):
    _status, payload = _get(tier=0, host=HOST_B)
    assert {r["host"] for r in payload["hosts"]} == {HOST_B}
    assert payload["engines"] == []                 # B never had an engine


def test_tier0_fresh_recorder_says_the_ring_is_empty(tmp_path):
    """Empty right after a restart is BY DESIGN — the response must say so."""
    from hscc_daemon import metrics_history as mh
    empty = mh.MetricsRecorder()
    mh.set_sampler(_fake_sampler(empty))
    try:
        _status, payload = _get(tier=0)
    finally:
        mh.close_sampler()
    assert payload["tier0"]["points"] == 0
    assert payload["tier0"]["capacity"] == mh.TIER0_POINTS
    assert payload["tier0"]["readable_here"] is True
    assert "restart" in payload["tier0"]["reason"]
    assert payload["hosts"] == []
    assert "empty" in payload["speak"]


def test_tier0_from_another_process_reports_the_depth(monkeypatch):
    """The deployed shape: no ring here, but the daemon's stamp has the count."""
    from hscc_daemon import state as state_mod
    state_mod.write_state("metrics_history", {
        "ok": True, "tier0_points": 437, "tier1_rows": 12, "tier2_rows": 4,
        "db_bytes": 4096, "bound_bytes": 10485760, "error": None})
    _status, payload = _get(tier=0)
    assert payload["source"] == "recorder_ring_absent"
    assert payload["tier0"]["points"] == 437
    assert payload["tier0"]["readable_here"] is False
    assert payload["tier0"]["source"] == "daemon_stream_stamp"
    assert "daemon" in payload["tier0"]["reason"]
    assert payload["tier0"]["stamp_age_s"] is not None
    assert payload["hosts"] == []                   # no rows invented
    assert payload["counts"]["host_rows"] == 0


def test_tier0_without_a_stamp_is_not_reported_as_zero(tmp_path, monkeypatch):
    """Missing stamp = the daemon never ran. Unknown, not zero."""
    from hscc_daemon import state as state_mod
    monkeypatch.setattr(state_mod, "STATE_DIR", str(tmp_path / "no-state"))
    _status, payload = _get(tier=0)
    assert payload["tier0"]["points"] is None
    assert payload["tier0"]["source"] == "no_stream_stamp"
    assert payload["tier0"]["readable_here"] is False


def test_stamp_age_is_honest_about_a_stale_daemon(monkeypatch, tmp_path):
    """A 2 s stream whose stamp is an hour old is a dead daemon, not a gap."""
    from hscc_daemon import state as state_mod
    old = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(hours=1)).isoformat()
    state_mod.write_state("metrics_history", {"tier0_points": 0})
    monkeypatch.setattr(routes_metrics, "_backing_stream_state",
                        lambda: {"tier0_points": 0, "timestamp": old})
    _status, payload = _get(tier=0)
    assert 3500 < payload["tier0"]["stamp_age_s"] < 3700
    monkeypatch.setattr(routes_metrics, "_backing_stream_state",
                        lambda: {"tier0_points": 0, "timestamp": "not-a-date"})
    _status, payload = _get(tier=0)
    assert payload["tier0"]["stamp_age_s"] is None


# --------------------------------------------------------------------------- #
# The read must not write, and must not sample
# --------------------------------------------------------------------------- #

def test_history_route_never_creates_the_store(mh):
    """``MetricsStore.connect()`` creates the file — a GET must not.

    The store opens with ``os.makedirs`` + ``sqlite3.connect``, so a reader that
    reached for it on a fleet that had never recorded would leave a schema-only
    database in the operator's state dir. ``source`` says which case it is.
    """
    store = routes_metrics._backing_store()
    assert not os.path.exists(store.path)
    _status, payload = _get(tier=1)
    assert payload["source"] == "sqlite_store_absent"
    assert payload["hosts"] == [] and payload["engines"] == []
    assert payload["stats"]["exists"] is False
    assert payload["stats"]["bound_bytes"] == mh.size_bound_bytes()
    assert not os.path.exists(store.path), (
        "a GET created ~/.hscc/metrics/history.db")


def test_history_route_never_constructs_a_sampler(ring_recorder):
    """The autouse fence fails if this ever calls get_sampler(); passing is the
    assertion. It runs with a recorder installed AND without one."""
    _status, payload = _get(tier=0)
    assert payload["tier0"]["readable_here"] is True
    _status, payload = _get(tier=1)
    assert payload["source"] == "sqlite_store"


def test_store_failure_degrades_in_the_payload_not_a_500(monkeypatch):
    """A broken store says so in ``error`` + ``speak``; the widget keeps its
    snapshot rather than seeing a 500 it cannot distinguish from an outage."""
    def boom():
        raise RuntimeError("disk gone")
    monkeypatch.setattr(routes_metrics, "_backing_store", boom)
    status, payload = _get(tier=1)
    assert status == 200
    assert "RuntimeError: disk gone" == payload["error"]
    assert "disk gone" in payload["speak"]


# --------------------------------------------------------------------------- #
# The response shape the widgets decode
# --------------------------------------------------------------------------- #

def test_response_shape_is_stable_across_tiers(populated):
    """One decoder serves all three tiers; a missing key breaks the widget."""
    keys = None
    for tier in (0, 1, 2):
        _status, payload = _get(tier=tier)
        assert payload["tier"] == tier
        assert payload["tier_meta"]["tier"] == tier
        assert set(payload) == {
            "tier", "tier_meta", "since", "until", "host", "node", "source",
            "hosts", "engines", "counts", "fields", "tier0", "stats",
            "error", "speak"}
        assert set(payload["counts"]) == {"host_rows", "engine_rows"}
        assert set(payload["tier0"]) == {
            "points", "capacity", "readable_here", "source", "reason",
            "sampler_error", "store_error", "stamp_age_s"}
        if keys is None:
            keys = set(payload)
        assert set(payload) == keys
    assert payload["speak"].startswith("History tier 2")


def test_resolution_and_window_come_from_the_recorder(populated):
    from hscc_daemon import metrics_history as mh
    for tier, (res, win) in {0: (mh.TIER0_INTERVAL_S, mh.TIER0_WINDOW_S),
                             1: (mh.TIER1_BUCKET_S, mh.TIER1_WINDOW_S),
                             2: (mh.TIER2_BUCKET_S, mh.TIER2_WINDOW_S)}.items():
        _status, payload = _get(tier=tier)
        assert payload["tier_meta"]["resolution_s"] == res
        assert payload["tier_meta"]["window_s"] == win
