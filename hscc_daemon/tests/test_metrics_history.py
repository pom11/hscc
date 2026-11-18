"""The metrics history recorder: the 2 s sampler, the three tiers, the roll-ups.

The gates this card names, and what each one is really guarding:

  * ``test_tier1_and_tier2_survive_a_restart`` — the card's "write, restart the
    recorder, read back". Tiers 1 and 2 are the durable ones.
  * ``test_tier0_is_empty_right_after_a_restart`` — the half of that the
    amendment asks to be STATED rather than hidden: the 2 s ring is memory-only,
    so a restarted recorder starts empty and fills as samples arrive.
  * ``test_the_2s_to_1min_rollup_keeps_the_peak_it_averaged_away`` — the
    2 s -> 1 min fold, including the reason the ``*_max`` columns exist: an
    average alone reports a 500 W spike as a calm 113 W.
  * ``test_the_1min_to_5min_rollup_rolls_up_from_tier1_rows`` — the
    1 min -> 5 min fold reads the TIER BELOW's rows (never the raw samples) and
    its peak is exact, because the largest sample in a minute is that minute's
    stored max.

Everything else here is the part a chart depends on and never says out loud:
a missing reading must stay None and never become a zero (an average of zeros
looks like an idle fleet), an engine's identity must be its port (two GPUs on
one host are not one imaginary backend), the ring must be bounded in COUNT, the
disk must see one transaction a minute rather than one every 2 s, and the
sampler must be ``cluster_live``'s own — pinned by class identity plus an AST
sweep, because "it reuses the monitor" is the requirement most likely to be
quietly unmet by a second poller that works fine until it doubles the SSH fan-out.

Nothing here spawns anything: the sampler is always handed fakes, and the store
is reached only through ``hscc_dir=`` (plus conftest's autouse ``~/.hscc``
redirect), so no test can touch the operator's real ``~/.hscc/metrics``.
"""

import ast
import os
import sqlite3

import pytest

from hscc_daemon import cluster_live
from hscc_daemon import daemon_ops
from hscc_daemon import metrics_history as mh
from hscc_daemon import state

BASE = 1_795_000_000 - (1_795_000_000 % 300)   # aligned to 2 s, 1 min and 5 min
HOST = "192.0.2.246"                            # TEST-NET-1, never a real node


# ── fixtures + builders ────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_live_sampler():
    """Never let a test leave a sampler holding a real stream behind."""
    yield
    mh.close_sampler()


def sample(ts, **over):
    """A sparkrun-shaped host sample: every field a CSV string, as it arrives."""
    s = {"timestamp": str(int(ts)),
         "cpu_usage_pct": "6.2", "cpu_temp_c": "87.2",
         "gpu_util_pct": "94", "gpu_temp_c": "71", "gpu_power_w": "37.88",
         "mem_used_pct": "85.1", "swap_used_mb": "128", "swap_total_mb": "16383",
         "gpu_mem_used_mb": "47104", "gpu_clock_mhz": "1965",
         "net_recv_bytes_s": "120000", "net_send_bytes_s": "340000",
         "sparkrun_jobs": "1"}
    s.update(over)
    return s


def frame(ts, *hosts, **over):
    """A ``sparkrun cluster monitor --json`` frame for these host names."""
    hosts = hosts or (HOST,)
    return {"hosts": [{"host": h, "error": None, "sample": sample(ts, **over)}
                      for h in hosts]}


def engines(node=HOST, port="8001", model: str | None = "org/Model-A", **over):
    """A ``ServingPoller.poll()`` row list — one backend by default."""
    r = {"node": node, "port": port, "model": model, "routes": ["orchestrator"],
         "running": 3, "waiting": 1, "kv": 12.5, "gen_tps": 40.0,
         "prompt_tps": 200.0, "prompt_total": 900000.0, "gen_total": 250000.0}
    r.update(over)
    return [r]


class FakeStream:
    """Stands in for ``cluster_live.HostStream`` without spawning anything."""

    instances = []

    def __init__(self, interval, cluster=None):
        self.interval, self.cluster = interval, cluster
        self.frame: dict | None = None
        self.error: str | None = None
        self.closed = False
        FakeStream.instances.append(self)

    def close(self):
        self.closed = True


class FakePoller:
    """Stands in for ``cluster_live.ServingPoller``."""

    def __init__(self, rows=None, raises=None):
        self.rows, self.raises = rows if rows is not None else engines(), raises
        self.calls = 0

    def poll(self):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.rows

    def close(self):
        pass


def sampler(stream=None, poller=None, rec=None, tmp=None, **over):
    return mh.Sampler(host_stream=stream if stream is not None else FakeStream(2),
                      serving=poller if poller is not None else FakePoller(),
                      recorder=rec if rec is not None else mh.MetricsRecorder(
                          hscc_dir=str(tmp)),
                      clock=lambda: BASE, **over)


def feed(rec, minutes, start=BASE, per_minute=30, hosts=(HOST,), **over):
    """Tick ``minutes`` whole minutes through ``rec``; returns the next ts.

    Each minute is 30 samples at 2 s from its own aligned start, and the LAST
    minute stays open (nothing ticks in the minute after it), which is the point:
    a roll-up only happens once a bucket's span has passed.
    """
    for m in range(minutes):
        minute = start + m * 60
        for i in range(per_minute):
            t = minute + i * 2
            rec.tick(frame(t, *hosts), engines(), t, **over)
    return start + minutes * 60


# ── what a point carries ───────────────────────────────────────────────────

def test_every_field_the_card_asks_for_is_stored():
    rec = mh.record_from_monitor(frame(BASE, "dgx-a", "dgx-b"), engines(), BASE)
    for host in ("dgx-a", "dgx-b"):
        m = rec["hosts"][host]
        for f in ("cpu_usage_pct", "cpu_temp_c", "gpu_util_pct", "gpu_temp_c",
                  "gpu_power_w", "mem_used_pct", "swap_used_mb",
                  "swap_used_pct", "gpu_mem_used_mb", "gpu_clock_mhz",
                  "net_recv_bytes_s", "net_send_bytes_s"):
            assert m[f] is not None, f
    assert rec["hosts"]["dgx-a"]["gpu_power_w"] == 37.88     # parsed, not a string
    assert rec["hosts"]["dgx-a"]["swap_used_pct"] == 0.78    # 128 / 16383
    e = rec["engines"][0]
    assert (e["run"], e["wait"], e["kv"], e["gen_tps"]) == (3, 1, 12.5, 40.0)


def test_run_and_wait_use_the_monitors_names_not_the_pollers():
    """The poller returns ``running``/``waiting``; the stored row (and the
    monitor's own table) calls them ``run``/``wait``. A shared name would store
    None for every engine on a healthy fleet — the worst possible default."""
    e = mh.engine_metrics(engines(running=7, waiting=2))[0]
    assert (e["run"], e["wait"]) == (7, 2)
    assert "running" not in e and "waiting" not in e


def test_a_missing_reading_is_none_never_zero():
    """`""` -> 0.0 is the silent lie this whole module is built to avoid."""
    m = mh.host_metrics(sample(BASE, gpu_power_w="", gpu_temp_c=None,
                               cpu_usage_pct="n/a"))
    assert m["gpu_power_w"] is None and m["gpu_temp_c"] is None
    assert m["cpu_usage_pct"] is None


def test_nan_and_inf_never_enter_history():
    """One NaN poisons every mean it joins, and no widget can plot it."""
    m = mh.host_metrics(sample(BASE, gpu_util_pct="nan", gpu_power_w="inf"))
    assert m["gpu_util_pct"] is None and m["gpu_power_w"] is None


def test_swap_pct_is_derived_and_left_alone_when_unknown():
    assert mh.host_metrics(sample(BASE))["swap_used_pct"] == 0.78
    # A zero total (no swap configured) must not become a division or a zero-pct
    # reading — it is unknown.
    assert mh.host_metrics(sample(BASE, swap_total_mb="0"))["swap_used_pct"] is None


def test_a_failed_host_is_stored_with_its_error():
    """A host that did not answer is a DATA POINT, not a hole: dropping it would
    leave a gap indistinguishable from 'the daemon was not running'."""
    f = {"hosts": [{"host": "dgx-a", "error": "ssh timeout", "sample": None}]}
    rec = mh.record_from_monitor(f, [], BASE)
    assert rec["hosts"]["dgx-a"]["error"] == "ssh timeout"
    assert rec["hosts"]["dgx-a"]["gpu_util_pct"] is None


def test_an_unattributable_engine_row_is_dropped():
    """(node, port) is the primary key; a NULL there is an IntegrityError that
    would take the whole minute's roll-up down for one malformed row."""
    out = mh.engine_metrics([{"node": None, "port": "8001", "model": "m"},
                             {"node": HOST, "port": "", "model": "m"},
                             engines()[0]])
    assert len(out) == 1 and out[0]["node"] == HOST


def test_a_down_backend_is_recorded_as_down_not_idle():
    out = mh.engine_metrics([{"node": HOST, "port": "8001", "model": "m",
                              "error": "connect refused"}])[0]
    assert out["error"] == "connect refused"
    assert "run" not in out          # no load numbers to average


def test_the_stream_error_is_stamped_on_the_point():
    rec = mh.record_from_monitor(frame(BASE), engines(), BASE,
                                 host_error="sparkrun monitor exited")
    assert rec["error"] == "sparkrun monitor exited"


def test_a_tick_that_learned_nothing_is_not_recorded():
    """The ring is bounded: a row of nulls must not push a real sample out."""
    assert mh.record_from_monitor(None, None, BASE) is None
    assert mh.record_from_monitor({"hosts": []}, [], BASE) is None
    # ... but the same empty frame WITH a stream error IS the record we want.
    assert mh.record_from_monitor({"hosts": []}, [], BASE,
                                  host_error="exited") is not None


def test_a_stale_frame_is_fingerprinted_as_the_same_reading():
    f = frame(BASE)
    assert mh.frame_identity(f) == mh.frame_identity(frame(BASE))
    assert mh.frame_identity(f) != mh.frame_identity(frame(BASE + 2))


def test_freshness_is_declared_unknown_when_it_cannot_be_known():
    """No stamps -> None, so the caller records every tick instead of
    'detecting' staleness it has no way to detect."""
    assert mh.frame_identity(None) is None
    assert mh.frame_identity({"hosts": [{"host": "a", "sample": {}}]}) is None


# ── roll-up: 2 s -> 1 min (named gate) ─────────────────────────────────────

def test_the_2s_to_1min_rollup_keeps_the_peak_it_averaged_away():
    """Thirty 2 s samples: gpu_power sits at 100 W and spikes to 500 W once.

    The average MUST be 113.33 and the max MUST be 500. A fold that computed the
    max from averaged input — or dropped the peak column — reports this minute
    as a calm 113 W and the throttle event is invisible forever.
    """
    points = []
    for i in range(30):
        ts = BASE + i * 2
        over = {"gpu_power_w": "500"} if i == 17 else {"gpu_power_w": "100"}
        points.append(mh.record_from_monitor(frame(ts, **over), engines(), ts))
    host_rows, _eng = mh.rollup(points, 1, 60)
    assert len(host_rows) == 1
    r = host_rows[0]
    assert r["ts"] == BASE                       # aligned to the minute
    assert r["n"] == 30                          # every 2 s sample is counted
    assert r["gpu_power_w"] == pytest.approx(113.333333, abs=1e-4)
    assert r["gpu_power_w_max"] == 500.0
    assert r["gpu_temp_c_max"] == 71.0           # flat series: max == the value


def test_a_bucket_starts_on_the_minute_and_only_after_its_last_sample():
    """Samples at :01..:59 belong to the bucket starting at :00."""
    pts = [mh.record_from_monitor(frame(BASE + 1 + i * 2), engines(), BASE + 1 + i * 2)
           for i in range(30)]
    rows, _e = mh.rollup(pts, 1, 60)
    assert rows[0]["ts"] == BASE


def test_a_partial_minute_reports_its_count_and_invents_nothing():
    """A daemon down for 40 of 60 s rolls up the 10 samples it has, says n=10,
    and does not pad the minute with zeros."""
    pts = [mh.record_from_monitor(frame(BASE + i * 2), engines(), BASE + i * 2)
           for i in range(10)]
    rows, _e = mh.rollup(pts, 1, 60)
    assert rows[0]["n"] == 10
    assert rows[0]["gpu_power_w"] == 37.88


def test_a_bucket_of_nothing_rolls_up_to_nothing():
    assert mh.rollup([], 1, 60) == ([], [])


def test_no_number_is_invented_when_nothing_reported():
    """A minute where every host failed rolls up to None fields + the error —
    never to zeros, which a widget would draw as a perfectly idle fleet."""
    pts = [mh.record_from_monitor(
        {"hosts": [{"host": "dgx-a", "error": "unreachable", "sample": None}]},
        [], BASE + i * 2) for i in range(30)]
    rows, _e = mh.rollup(pts, 1, 60)
    assert rows[0]["gpu_util_pct"] is None and rows[0]["gpu_power_w_max"] is None
    assert rows[0]["error"] == "unreachable"
    assert rows[0]["n"] == 30          # the samples arrived; they said nothing


# ── roll-up: 1 min -> 5 min (named gate) ───────────────────────────────────

def _minute_row(ts, n, gpu_util, gpu_power_max, gpu_temp_max):
    """A tier-1 row as it comes back out of the store (flat, with n + peaks)."""
    return {"tier": 1, "ts": ts, "host": HOST, "n": n,
            "gpu_util_pct": gpu_util,
            "gpu_power_w": (gpu_util * 0.5) if gpu_util is not None else None,
            "gpu_util_pct_max": gpu_util,
            "gpu_power_w_max": gpu_power_max, "gpu_temp_c_max": gpu_temp_max,
            "cpu_temp_c_max": 40.0}


def test_the_1min_to_5min_rollup_rolls_up_from_tier1_rows():
    """Five 1-minute rows -> one 5-minute row, keyed on the minute's own peak."""
    rows_in = [_minute_row(BASE + i * 60, 30, 10.0 * (i + 1), 100.0 + i, 60.0 + i)
               for i in range(5)]
    rows, _e = mh.rollup(rows_in, 2, 300)
    assert len(rows) == 1 and rows[0]["ts"] == BASE
    assert rows[0]["tier"] == 2
    assert rows[0]["n"] == 150                   # 5 full minutes of 2 s samples
    assert rows[0]["gpu_util_pct"] == pytest.approx(30.0)
    # The peak is EXACT: the biggest 2 s reading inside a minute is that
    # minute's stored max, so the 5-minute max is the max of the minute maxes.
    assert rows[0]["gpu_power_w_max"] == 104.0
    assert rows[0]["gpu_temp_c_max"] == 64.0


def test_the_coarser_mean_is_weighted_by_the_finer_tiers_sample_counts():
    """One minute answered 30 times at 100% GPU, the next 3 times at 0%.

    The true 5-minute average is 90.9%, not 50%. An unweighted mean of minute
    averages is the plausible-looking wrong answer here, and it is the one a
    quick fold produces by default.
    """
    rows_in = [_minute_row(BASE, 30, 100.0, 300.0, 80.0),
               _minute_row(BASE + 60, 3, 0.0, 10.0, 40.0)]
    rows, _e = mh.rollup(rows_in, 2, 300)
    assert rows[0]["gpu_util_pct"] == pytest.approx(90.909091, abs=1e-4)
    assert rows[0]["n"] == 33


def test_a_model_with_no_samples_contributes_no_zero():
    rows_in = [_minute_row(BASE, 30, 100.0, 300.0, 80.0),
               {**_minute_row(BASE + 60, 30, None, None, None), "host": HOST}]
    rows, _e = mh.rollup(rows_in, 2, 300)
    assert rows[0]["gpu_util_pct"] == 100.0      # the silent minute is skipped


# ── per-engine roll-up ─────────────────────────────────────────────────────

def test_two_backends_on_one_host_stay_two_series():
    """Averaging a node's two vLLM ports into one imaginary backend would show
    twice the load on a model that is not serving it."""
    pts = [mh.record_from_monitor(
        frame(BASE + i * 2),
        engines(port="8001", model="org/Model-A", running=1)
        + engines(port="8002", model="org/Model-B", running=9), BASE + i * 2)
        for i in range(30)]
    _hosts, eng = mh.rollup(pts, 1, 60)
    assert len(eng) == 2
    by_port = {e["port"]: e for e in eng}
    assert by_port["8001"]["run"] == 1 and by_port["8002"]["run"] == 9
    assert by_port["8002"]["model"] == "org/Model-B"


def test_a_port_serving_nothing_yet_is_still_a_row():
    """The route map may not have resolved a model yet; the port is still the
    identity and its load is still real, so the row keeps a None model."""
    pts = [mh.record_from_monitor(frame(BASE),
                                  [engines(model=None)[0]], BASE)]
    _h, eng = mh.rollup(pts, 1, 60)
    assert eng[0]["model"] is None and eng[0]["run"] == 3


# ── the store ──────────────────────────────────────────────────────────────

def test_store_round_trip_preserves_types_and_nulls(tmp_path):
    store = mh.MetricsStore(hscc_dir=str(tmp_path))
    pts = [mh.record_from_monitor(frame(BASE, "dgx-a"), engines(), BASE)]
    host_rows, eng_rows = mh.rollup(pts, 1, 60)
    store.write_rollup(host_rows, eng_rows)
    back = store.host_rows(1)
    assert len(back) == 1 and back[0]["host"] == "dgx-a"
    assert back[0]["gpu_power_w"] == 37.88 and back[0]["tier"] == 1
    assert store.engine_rows(1)[0]["node"] == HOST
    # A host filter and a time window both have to work for the widget routes.
    assert store.host_rows(1, host="nobody") == []
    assert store.host_rows(1, since=BASE + 60) == []
    assert store.host_rows(1, since=BASE, until=BASE)[0]["host"] == "dgx-a"
    store.close()


def test_tier1_and_tier2_survive_a_restart(tmp_path):
    """The card's gate: write, throw the recorder away, build a fresh one, read
    it back — and keep appending from where the old one stopped."""
    store = mh.MetricsStore(hscc_dir=str(tmp_path))
    rec = mh.MetricsRecorder(store=store)
    feed(rec, 7)                                  # 7 minutes of 2 s samples
    before = (store.count(1), store.count(2))
    assert before[0] == 6                         # the 7th minute is still open
    assert before[1] == 1                         # one full 5-minute bucket
    store.close()

    again = mh.MetricsRecorder(hscc_dir=str(tmp_path))   # fresh object, same file
    assert again.store.count(1) == before[0]
    assert again.store.count(2) == before[1]
    assert again.tier0_points() == []              # the lost minute is tier 0's
    # ... and it goes on rolling up from where it stopped: the watermark is in
    # the file, so a new process continues instead of re-writing or skipping
    # buckets. (The minute that was OPEN when the daemon died is gone with tier
    # 0 — that is the documented, accepted cost of a memory-only 2 s tier.)
    feed(again, 2, start=BASE + 7 * 60)
    assert again.store.count(1) == before[0] + 1   # the next CLOSED minute landed
    assert again.store.watermark("tier1_closed_through") == BASE + 8 * 60


def test_tier0_is_empty_right_after_a_restart(tmp_path):
    """Stated, not hidden: tier 0 is memory-only by spec, so a restarted
    recorder has no 2 s points until the samples arrive again."""
    store = mh.MetricsStore(hscc_dir=str(tmp_path))
    rec = mh.MetricsRecorder(store=store)
    feed(rec, 3)
    assert len(rec.tier0) == 90                    # 3 minutes of 2 s ticks
    store.close()

    again = mh.MetricsRecorder(hscc_dir=str(tmp_path))
    assert again.tier0_points() == []              # empty, as documented
    assert again.stats()["tier0_points"] == 0
    again.tick(frame(BASE), engines(), BASE)       # ... and it fills immediately
    assert len(again.tier0) == 1


def test_the_tier0_ring_is_bounded_and_keeps_the_newest(tmp_path):
    rec = mh.MetricsRecorder(hscc_dir=str(tmp_path), tier0_points=5)
    for i in range(20):
        rec.tick(frame(BASE + i * 2), engines(), BASE + i * 2)
    assert len(rec.tier0) == 5
    assert [p["ts"] for p in rec.tier0] == [BASE + i * 2 for i in range(15, 20)]


def test_the_disk_sees_one_transaction_per_closed_minute(tmp_path):
    """30 ticks a minute must not mean 30 sqlite passes a minute.

    Counted at the store boundary: one minute of ticks writes nothing at all
    (the minute has not closed), the next minute writes exactly once.
    """
    class CountingStore(mh.MetricsStore):
        passes = 0

        def write_rollup(self, host_rows, engine_rows):
            CountingStore.passes += 1
            return super().write_rollup(host_rows, engine_rows)

    store = CountingStore(hscc_dir=str(tmp_path))
    rec = mh.MetricsRecorder(store=store)
    feed(rec, 1, per_minute=30)
    assert CountingStore.passes == 0               # still inside the open minute
    feed(rec, 1, start=BASE + 60, per_minute=30)
    assert CountingStore.passes == 1               # the closed minute, once
    feed(rec, 2, start=BASE + 120, per_minute=30)
    assert CountingStore.passes == 3               # one per closed minute after


def test_a_replayed_minute_is_not_written_twice(tmp_path):
    """Re-running a roll-up must be a no-op, not a duplicate — that is what lets
    a roll-up that died half-way simply be retried. And each closed bucket must
    land exactly once, not once per tick."""
    store = mh.MetricsStore(hscc_dir=str(tmp_path))
    rec = mh.MetricsRecorder(store=store)
    feed(rec, 2)
    assert store.count(1) == 1
    rec.rollups(BASE + 60)                          # same minute, replayed
    rec.rollups(BASE + 60)
    assert store.count(1) == 1
    rec.rollups(BASE + 120)                         # the next minute closes: once
    rec.rollups(BASE + 120)
    assert store.count(1) == 2


def test_each_tier_prunes_by_its_own_window(tmp_path):
    """24 h for tier 1, 7 d for tier 2 — a tier that prunes by the wrong window
    either fills the disk or throws away a week the widgets still read."""
    store = mh.MetricsStore(hscc_dir=str(tmp_path))
    now = BASE
    old1 = now - mh.TIER1_WINDOW_S - 60            # 24 h + 1 min old
    old2 = now - mh.TIER2_WINDOW_S - 300           # 7 d + 5 min old
    store.write_rollup([{"tier": 1, "ts": old1, "host": HOST, "n": 30},
                        {"tier": 1, "ts": now - 120, "host": HOST, "n": 30},
                        {"tier": 2, "ts": old2, "host": HOST, "n": 150},
                        {"tier": 2, "ts": now - 600, "host": HOST, "n": 150}], [])
    # Tier 2 starts caught up, so this test measures PRUNING and not the
    # catch-up roll-up (which would legitimately synthesise a tier-2 row out of
    # the tier-1 row above — see test_the_1min_to_5min_rollup_...).
    store.set_watermark("tier2_closed_through", now)
    mh.MetricsRecorder(store=store).rollups(now)
    assert [r["ts"] for r in store.host_rows(1)] == [now - 120]
    assert [r["ts"] for r in store.host_rows(2)] == [now - 600]
    store.close()


def test_watermarks_are_durable(tmp_path):
    store = mh.MetricsStore(hscc_dir=str(tmp_path))
    assert store.watermark("tier1_closed_through", 0) == 0
    store.set_watermark("tier1_closed_through", BASE + 120)
    store.close()
    assert mh.MetricsStore(hscc_dir=str(tmp_path)).watermark(
        "tier1_closed_through") == BASE + 120


def test_earliest_is_the_oldest_bucket_or_none(tmp_path):
    store = mh.MetricsStore(hscc_dir=str(tmp_path))
    assert store.earliest(1) is None
    store.write_rollup([{"tier": 1, "ts": BASE + 120, "host": HOST, "n": 30}], [])
    assert store.earliest(1) == BASE + 120
    store.close()


def test_a_store_failure_does_not_stop_the_recorder(tmp_path):
    """A sqlite error is reported and the sampler keeps going; tier 0 keeps
    filling so the next tick can retry the roll-up. Losing history is survivable,
    losing the sampler is not."""
    blocked = tmp_path / "blocked"
    (blocked / "metrics" / "history.db").mkdir(parents=True)   # the db path IS a dir
    store = mh.MetricsStore(hscc_dir=str(blocked))
    rec = mh.MetricsRecorder(store=store)
    rec.tick(frame(BASE), engines(), BASE)         # must not raise
    assert rec.last_error and "OperationalError" in rec.last_error
    assert len(rec.tier0) == 1                     # still recording in memory
    rec.tick(frame(BASE + 2), engines(), BASE + 2)
    assert len(rec.tier0) == 2


def test_the_store_stays_inside_the_stated_bound(tmp_path):
    """The bound the PR body quotes, measured — in both directions.

    A full week at the fleet's shape (4 nodes, 6 backends, 51-character model
    names) has to fit inside the stated 10 MiB target, and the bound must not be
    so loose that "bounded" means nothing: the same order of magnitude is the
    test.
    """
    store = mh.MetricsStore(hscc_dir=str(tmp_path))
    hosts = [f"dgx-{i}" for i in range(4)]
    ports = [(f"dgx-{i % 4}", str(8001 + i),
              f"org/Model-{i}-235B-A22B-Instruct") for i in range(6)]
    for tier, buckets, step in ((1, 1440, 60), (2, 2016, 300)):
        host_rows = [{"tier": tier, "ts": BASE + b * step, "host": h, "n": 30,
                      "cpu_usage_pct": 6.2, "cpu_temp_c": 87.2,
                      "gpu_util_pct": 94.0, "gpu_temp_c": 71.0,
                      "gpu_power_w": 37.88, "mem_used_pct": 85.1,
                      "swap_used_mb": 128.0, "swap_total_mb": 16383.0,
                      "swap_used_pct": 0.78, "gpu_mem_used_mb": 47104.0,
                      "gpu_clock_mhz": 1965.0, "net_recv_bytes_s": 120000.0,
                      "net_send_bytes_s": 340000.0, "sparkrun_jobs": 1.0,
                      "cpu_temp_c_max": 90.0, "gpu_temp_c_max": 74.0,
                      "gpu_power_w_max": 400.0}
                     for b in range(buckets) for h in hosts]
        eng_rows = [{"tier": tier, "ts": BASE + b * step, "node": n, "port": p,
                     "model": m, "n": 30, "run": 3.0, "wait": 1.0, "kv": 12.5,
                     "gen_tps": 40.0, "prompt_tps": 200.0}
                    for b in range(buckets) for (n, p, m) in ports]
        store.write_rollup(host_rows, eng_rows)
    measured = store.size_bytes()
    assert measured > 0
    assert measured < mh.size_bound_bytes(), (
        f"4-node store is {measured / 1e6:.1f} MB, bound is "
        f"{mh.size_bound_bytes() / 1e6:.1f} MB")
    assert mh.size_bound_bytes() <= 8 * measured, (
        "the stated bound is orders of magnitude looser than the data — "
        "'bounded' would be decorative")
    store.close()


def test_the_store_is_one_file_and_not_json_per_sample(tmp_path):
    """The amendment's shape requirement, checked where it is claimed: one file,
    sqlite's own header, and no -wal sidecar to grow next to it."""
    store = mh.MetricsStore(hscc_dir=str(tmp_path))
    store.write_rollup([{"tier": 1, "ts": BASE, "host": HOST, "n": 30}], [])
    assert os.path.basename(store.path) == mh.DB_FILE
    with open(store.path, "rb") as fh:
        assert fh.read(16) == b"SQLite format 3\x00"
    assert not [f for f in os.listdir(os.path.dirname(store.path))
                if f.endswith(("-wal", "-shm"))]
    assert mh.db_path(hscc_dir=str(tmp_path)) == store.path
    store.close()


# ── the sampler: ONE, and the monitor's own ────────────────────────────────

def test_the_sampler_reuses_the_monitors_own_classes():
    """The requirement most likely to be quietly unmet.

    Identity, not a duck-type: the sampler's sources must literally be
    ``cluster_live``'s classes, because "reuse the monitor's sampling path" is
    what keeps the fleet from being polled twice per tick over two sets of ssh
    channels that can disagree.
    """
    assert mh.host_stream_cls() is cluster_live.HostStream
    assert mh.serving_poller_cls() is cluster_live.ServingPoller


def test_the_module_opens_no_second_sampling_path():
    """No subprocess, no socket, no exec — the AST is the proof, not the prose.

    A second poller here would not fail a functional test; it would double the
    ssh fan-out per node per tick on the live cluster. So the ban is structural.
    """
    tree = ast.parse(open(mh.__file__).read())
    imports = set()
    attrs = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imports.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            if n.module:
                imports.add(n.module.split(".")[0])
        elif isinstance(n, ast.Attribute):
            attrs.add(n.attr)
    assert not imports & {"subprocess", "socket", "paramiko", "pexpect"}, imports
    banned = {"Popen", "system", "popen", "check_output", "check_call",
              "spawn", "execv", "execve", "fork", "ssh"}
    assert not attrs & banned, attrs & banned


def test_the_sampler_ticks_at_the_monitors_own_cadence():
    """2 s here is cluster_live's DEFAULT_INTERVAL, not a number invented to
    look responsive — and the daemon's SOT entry points at the same value."""
    assert mh.TIER0_INTERVAL_S == cluster_live.DEFAULT_INTERVAL == 2
    assert daemon_ops.PERIODIC_INTERVALS[mh.STREAM_NAME] == mh.TIER0_INTERVAL_S


def test_the_tier0_window_is_fifteen_minutes_at_450_points_per_node():
    assert mh.TIER0_POINTS == 450 == mh.TIER0_WINDOW_S // mh.TIER0_INTERVAL_S
    assert mh.TIER1_WINDOW_S == 86400 and mh.TIER1_BUCKET_S == 60
    assert mh.TIER2_WINDOW_S == 7 * 86400 and mh.TIER2_BUCKET_S == 300


def test_a_stale_frame_is_not_a_new_sample(tmp_path):
    """sparkrun wedged, the last frame is still readable. Recording it every 2 s
    would draw a flat, healthy-looking line for a dead poller."""
    stream = FakeStream(2)
    stream.frame = frame(BASE)
    poller = FakePoller()
    rec = mh.MetricsRecorder(hscc_dir=str(tmp_path))
    s = mh.Sampler(host_stream=stream, serving=poller, recorder=rec,
                   clock=lambda: BASE)
    assert s.tick() is not None
    assert s.tick() is None and s.tick() is None
    assert len(rec.tier0) == 1
    assert poller.calls == 1                       # not even re-polled


def test_a_lost_stream_is_recorded_once_then_gaps(tmp_path):
    """The tail of the history says "the sampler lost the fleet here" — once,
    not every tick, and never as a frozen copy of the last good reading."""
    stream = FakeStream(2)
    stream.frame = frame(BASE)
    stream.error = "sparkrun monitor exited"
    rec = mh.MetricsRecorder(hscc_dir=str(tmp_path))
    s = mh.Sampler(host_stream=stream, serving=FakePoller(), recorder=rec,
                   clock=lambda: BASE, restart_min_s=10_000)
    first = s.tick()
    assert first is not None and first.get("error") == "sparkrun monitor exited"
    assert first["hosts"] == {}                    # no stale numbers laundered in
    assert s.tick() is None and s.tick() is None
    assert len(rec.tier0) == 1


def test_stream_restart_is_bounded(tmp_path):
    """A permanent sparkrun failure must not fork a child every 2 s."""
    FakeStream.instances.clear()
    orig = mh.host_stream_cls
    mh.set_sampler(None)
    try:
        mh.host_stream_cls = lambda: FakeStream        # the CLASS, not an instance
        stream = FakeStream(2)
        stream.error = "sparkrun monitor exited"
        s = mh.Sampler(host_stream=stream, serving=FakePoller(),
                       recorder=mh.MetricsRecorder(hscc_dir=str(tmp_path)),
                       clock=lambda: BASE, restart_min_s=60)
        FakeStream.instances.clear()
        s.tick(now=BASE + 61)                      # due: a new stream is built
        assert len(FakeStream.instances) == 1
        assert stream.closed is True               # and the old one was closed
        s.tick(now=BASE + 62)                      # not due: no second child
        assert len(FakeStream.instances) == 1
    finally:
        mh.host_stream_cls = orig


def test_a_serving_failure_does_not_blank_the_host_numbers(tmp_path):
    """The two sources are independent: if /metrics is down, the node telemetry
    is still real telemetry."""
    stream = FakeStream(2)
    stream.frame = frame(BASE)
    rec = mh.MetricsRecorder(hscc_dir=str(tmp_path))
    s = mh.Sampler(host_stream=stream, serving=FakePoller(raises=OSError("nope")),
                   recorder=rec, clock=lambda: BASE)
    out = s.tick()
    assert out is not None and out["hosts"][HOST]["gpu_power_w"] == 37.88
    assert out["engines"] == []
    assert "serving poll failed" in s.error


def test_get_sampler_builds_one_sampler_from_the_monitors_classes(tmp_path):
    """The singleton is the "ONE sampler" requirement; built from cluster_live,
    at the monitor's cadence, with the cluster passed through."""
    FakeStream.instances.clear()
    orig_stream, orig_poller = mh.host_stream_cls, mh.serving_poller_cls
    try:
        mh.host_stream_cls = lambda: FakeStream          # the CLASS
        mh.serving_poller_cls = lambda: FakePoller       # the CLASS
        a = mh.get_sampler(hscc_dir=str(tmp_path), cluster="prod")
        b = mh.get_sampler(hscc_dir=str(tmp_path))
        assert a is b
        assert a.interval == mh.TIER0_INTERVAL_S and a.cluster == "prod"
        assert len(FakeStream.instances) == 1      # one child, not two
    finally:
        mh.host_stream_cls, mh.serving_poller_cls = orig_stream, orig_poller


def test_close_sampler_terminates_the_child_it_owns(tmp_path):
    """Daemon threads die with the process; a SUBPROCESS does not. Without this
    every daemon restart orphans a sparkrun child holding ssh channels open."""
    stream = FakeStream(2)
    mh.set_sampler(mh.Sampler(host_stream=stream, serving=FakePoller(),
                              recorder=mh.MetricsRecorder(hscc_dir=str(tmp_path))))
    mh.close_sampler()
    assert stream.closed is True
    assert mh._sampler is None                     # released, not left installed
    mh.close_sampler()                             # a second close is a no-op


# ── the daemon stream ──────────────────────────────────────────────────────

class BrokenSampler:
    def tick(self, now=None):
        raise RuntimeError("boom")

    def close(self):
        pass


def test_the_stream_is_registered_in_the_single_source_of_truth():
    """Cadence in PERIODIC_INTERVALS (what `hscc verify` staleness maths uses)
    AND a row in the loop's CHECKS table (what actually starts the thread) — a
    cadence without a check body is a stream that never runs, and a body without
    a cadence falls through to the flat staleness window."""
    assert daemon_ops.PERIODIC_INTERVALS[mh.STREAM_NAME] == 2
    src = open(daemon_ops.__file__).read()
    assert f'"{mh.STREAM_NAME}": check_metrics_history' in src


def test_the_tick_stamps_state_on_every_path(tmp_path):
    """A stream that goes dark is indistinguishable from one that died, so both
    paths stamp — including the one where the sampler raised."""
    mh.set_sampler(mh.Sampler(host_stream=FakeStream(2), serving=FakePoller(),
                              recorder=mh.MetricsRecorder(hscc_dir=str(tmp_path))))
    out = mh.check_metrics_history(now=BASE)
    assert out["ok"] is True and out["error"] is None
    assert state.read_state(mh.STREAM_NAME)["ok"] is True

    mh.set_sampler(BrokenSampler())
    bad = mh.check_metrics_history(now=BASE)
    assert bad["ok"] is False and "boom" in bad["error"]
    stamped = state.read_state(mh.STREAM_NAME)
    assert stamped["ok"] is False and "boom" in stamped["error"]


def test_the_tick_reports_the_store_it_wrote(tmp_path):
    """The state file is the only surface that shows whether history is being
    written at all, so it carries depth, row counts and the file's real size."""
    stream = FakeStream(2)
    mh.set_sampler(mh.Sampler(host_stream=stream, serving=FakePoller(),
                              recorder=mh.MetricsRecorder(hscc_dir=str(tmp_path))))
    # The frame must ADVANCE: an identical frame is the same reading, and the
    # recorder is correct to refuse it (see test_a_stale_frame_is_not_a_new_sample).
    for i in range(31):
        stream.frame = frame(BASE + i * 2)
        mh.check_metrics_history(now=BASE + i * 2)
    stream.frame = frame(BASE + 62)
    out = mh.check_metrics_history(now=BASE + 62)        # closes the first minute
    assert out["tier0_points"] == 32
    assert out["tier1_rows"] == 1 and out["tier2_rows"] == 0
    assert out["db_bytes"] > 0 and out["bound_bytes"] == mh.size_bound_bytes()
    assert os.path.exists(mh.db_path(hscc_dir=str(tmp_path)))


def test_a_broken_store_reports_itself_in_the_state(tmp_path):
    blocked = tmp_path / "blocked"
    (blocked / "metrics" / "history.db").mkdir(parents=True)   # the db path IS a dir
    stream = FakeStream(2)
    mh.set_sampler(mh.Sampler(host_stream=stream, serving=FakePoller(),
                              recorder=mh.MetricsRecorder(
                                  store=mh.MetricsStore(hscc_dir=str(blocked)))))
    for i in range(31):
        stream.frame = frame(BASE + i * 2)
        mh.check_metrics_history(now=BASE + i * 2)
    stream.frame = frame(BASE + 62)
    out = mh.check_metrics_history(now=BASE + 62)
    assert out["ok"] is False and "OperationalError" in out["error"]
    assert state.read_state(mh.STREAM_NAME)["error"]


# ── the cross-process read seam (t_c7f57783: GET /v1/cluster/history) ──────
#
# Tier 0 lives in the sampler's process and the API server is a different
# process, so the read route asks "does THIS process own the ring?" rather than
# "give me a sampler". These two tests are what keep that question answerable:
# the first pins that answering it never constructs a Sampler (constructing one
# spawns `sparkrun cluster monitor` — a read route that sampled the fleet would
# be the second poller this module's whole design exists to avoid), and the
# second pins the other answer, "resident, and empty right after a restart",
# which is a different fact from "not resident here".

def test_resident_recorder_answers_without_building_a_sampler(monkeypatch):
    def _no_construction(*args, **kwargs):
        raise AssertionError("resident_recorder() must never construct a Sampler")
    monkeypatch.setattr(mh, "Sampler", _no_construction)
    mh.close_sampler()
    assert mh.resident_recorder() is None
    # The distinction this seam exists for: get_sampler() DOES construct on
    # first use, which is exactly why a read route must not call it.
    with pytest.raises(AssertionError, match="must never construct"):
        mh.get_sampler()


def test_resident_recorder_is_the_owner_of_the_tier0_ring(tmp_path):
    rec = mh.MetricsRecorder(hscc_dir=str(tmp_path))
    mh.set_sampler(mh.Sampler(host_stream=FakeStream(2), serving=FakePoller(),
                              recorder=rec))
    assert mh.resident_recorder() is rec
    assert mh.resident_recorder().tier0_points() == []    # resident AND empty
    rec.tick(frame(BASE), engines(), BASE)
    assert [p["ts"] for p in mh.resident_recorder().tier0_points()] == [BASE]


def test_tier2_rolls_the_serving_series_up_with_the_host_series(tmp_path):
    """The 7 d tier is the only one a week-long chart can read, and the widgets
    draw their serving table from it — so per-engine rows have to roll up too,
    not stop at tier 1.

    The load-bearing number is the weighted mean: minutes 0-3 answered 30 times
    at 100 tok/s and minute 4 answered 6 times at 0. The true 5-minute value is
    95.24 tok/s; a plain mean of the minute averages says 80, and a fold that
    forgot the engine rows says nothing at all — which is what this line used to
    do (t_c7f57783's read-route parity test found it: `rollups()` read tier 1's
    engine rows into a local and then folded only the host rows, leaving
    `engine_rollup` empty for tier 2).
    """
    store = mh.MetricsStore(hscc_dir=str(tmp_path))
    rec = mh.MetricsRecorder(store=store)
    per_minute = [30, 30, 30, 30, 6, 30]
    gen_tps = [100.0, 100.0, 100.0, 100.0, 0.0, 100.0]
    for m, (count, tps) in enumerate(zip(per_minute, gen_tps)):
        minute = BASE + m * 60
        for i in range(count):
            t = minute + i * 2
            rec.tick(frame(t), engines(gen_tps=tps), t)
    t2 = store.engine_rows(2)
    assert len(t2) == 1, "the 5-minute bucket produced no engine row"
    row = t2[0]
    assert (row["tier"], row["ts"]) == (2, BASE)
    assert (row["node"], row["port"], row["model"]) == (HOST, "8001",
                                                        "org/Model-A")
    assert row["gen_tps"] == pytest.approx(12000 / 126, abs=1e-4)
    assert row["n"] == 126
    # The host series of the same bucket is still there — one fold, two series.
    assert store.count(2) == 1
    assert store.host_rows(2)[0]["ts"] == BASE
    store.close()
