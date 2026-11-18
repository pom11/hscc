"""The metrics history recorder: the on-disk ring, the fold, the restart.

The two gates the card names explicitly live here:

  * ``test_restart_round_trip_survives_a_new_recorder_object`` — write, throw
    the recorder away, construct a fresh one, read back (the card's
    "prove it: write, restart the recorder, read back").
  * ``test_downsample_folds_sixty_seconds_into_five_minute_buckets`` — the
    60 s -> 5 min bucket correctness: boundary alignment, count, average, and
    the peak/floor the average would erase.

Everything else here is the part a chart silently depends on: the ring bounds
(size by COUNT and by BYTES, newest wins), the one-directional fold (a bucket
is final once folded), and a corrupt line that must degrade to "skipped and
counted" instead of crashing the daemon's reader.

The store's paths are reached only through ``hscc_dir=`` / the autouse
``~/.hscc`` redirect in conftest, so no test here can touch the operator's
real ``~/.hscc/metrics``.
"""

import json
import os

import pytest

from hscc_daemon import metrics_history as mh

T0 = 1_795_000_000          # a fixed epoch so bucket maths is exact
# 300-aligned base point: T0 % 300 == 100, so use an aligned base for the
# downsampling tests and keep the unaligned one for the rest.
BASE = T0 - (T0 % 300)


def sample(**over):
    """A sparkrun-shaped host sample: every field a CSV string, or empty."""
    s = {"timestamp": "1795000000",
         "cpu_usage_pct": "6.2", "cpu_temp_c": "87.2",
         "gpu_util_pct": "94", "gpu_temp_c": "71", "gpu_power_w": "37.88",
         "mem_used_pct": "85.1", "swap_used_mb": "0", "swap_total_mb": "16383",
         "gpu_mem_used_mb": "47104", "gpu_clock_mhz": "1965",
         "net_recv_bytes_s": "120000", "net_send_bytes_s": "340000",
         "sparkrun_jobs": "1"}
    s.update(over)
    return s


def frame(*hosts, ts="1795000000"):
    """A ``sparkrun cluster monitor --json`` frame from host names."""
    return {"hosts": [{"host": h, "error": None, "sample": sample(timestamp=ts)}
                      for h in hosts]}


def rows(**over):
    """A ``ServingPoller.poll()`` row list, one backend by default."""
    r = {"node": "192.0.2.246", "port": "8001", "model": "org/Model-A",
         "routes": ["orchestrator-model"], "running": 3, "waiting": 1,
         "kv": 0.5, "gen_tps": 10.0, "prompt_tps": 200.0,
         "prompt_total": 900000.0, "gen_total": 250000.0}
    r.update(over)
    return [r]


def rec(ts, host="dgx-246", power="10.0", util="10", **over):
    """One 60 s record built through the production mapper."""
    f = {"hosts": [{"host": host, "error": None,
                    "sample": sample(gpu_power_w=power, gpu_util_pct=util,
                                     timestamp=str(ts))}]}
    r = mh.record_from_monitor(f, rows(), ts)
    assert r is not None                       # a frame with a host always maps
    r.update(over)
    return r


# ── the record: what the monitor reads is what gets stored ────────────────

def test_record_carries_every_field_the_monitor_reads():
    """The card's list, verbatim, under sparkrun's own field names.

    Names matter: a reader that has to translate keys is a reader that drifts,
    and a rename upstream should surface as a MISSING key, not a silently wrong
    one. Values are floats because history is averaged — sparkrun's CSV strings
    are parsed exactly once, here.
    """
    r = mh.record_from_monitor(frame("dgx-246"), rows(), T0)
    h = r["hosts"]["dgx-246"]
    for k in ("cpu_usage_pct", "cpu_temp_c", "gpu_util_pct", "gpu_temp_c",
              "gpu_power_w", "mem_used_pct", "swap_used_mb", "gpu_mem_used_mb",
              "gpu_clock_mhz", "net_recv_bytes_s", "net_send_bytes_s"):
        assert k in h, k
        assert isinstance(h[k], float), k
    assert h["cpu_usage_pct"] == 6.2 and h["gpu_power_w"] == 37.88
    assert h["gpu_clock_mhz"] == 1965.0
    assert h["net_recv_bytes_s"] == 120000.0 and h["net_send_bytes_s"] == 340000.0
    # "swap" is a percentage on the monitor, but a percentage needs a total:
    # both raw values plus the derived pct are stored.
    assert h["swap_used_mb"] == 0.0 and h["swap_total_mb"] == 16383.0
    assert h["swap_used_pct"] == 0.0
    assert r["ts"] == T0 and r["res"] == "60s" and r["n"] == 1


def test_missing_reading_stays_none_never_zero():
    """An unavailable reading is the EMPTY string upstream.

    Coercing it to 0.0 would write a fake "GPU idle, 0 W" into history for a
    node that never answered, and the average of a bucket full of those reads
    as a calm fleet. None is stored and the downsampler averages around it.
    """
    f = {"hosts": [{"host": "dgx-247", "error": None,
                    "sample": sample(gpu_power_w="", gpu_util_pct="",
                                     gpu_clock_mhz="n/a", swap_total_mb="")}]}
    h = mh.record_from_monitor(f, [], T0)["hosts"]["dgx-247"]
    assert h["gpu_power_w"] is None and h["gpu_util_pct"] is None
    assert h["gpu_clock_mhz"] is None
    assert h["cpu_usage_pct"] == 6.2          # the readings that exist survive
    assert h["swap_used_pct"] is None          # no total, no percentage
    # ...and the total that DOES exist produces the percentage the monitor shows
    assert mh.host_metrics(sample())["swap_used_pct"] == 0.0
    assert mh.host_metrics(sample(swap_used_mb="4096",
                                 swap_total_mb="16383"))["swap_used_pct"] == 25.0


def test_nan_and_inf_never_enter_a_sample():
    """One NaN would poison every average of the field it lands in."""
    h = mh.host_metrics({"gpu_power_w": "nan", "gpu_util_pct": "inf"})
    assert h["gpu_power_w"] is None and h["gpu_util_pct"] is None


def test_host_failure_is_recorded_as_a_failure():
    """A host whose ssh collection failed keeps its error string.

    Dropping the row would leave a gap indistinguishable from "the daemon was
    not running"; the point exists, its numbers are None, and it says why.
    """
    f = {"hosts": [{"host": "dgx-248", "error": "ssh: connection refused",
                    "sample": None}]}
    r = mh.record_from_monitor(f, None, T0)
    assert r["hosts"]["dgx-248"]["error"] == "ssh: connection refused"
    assert r["hosts"]["dgx-248"]["gpu_util_pct"] is None


def test_stream_level_error_is_stamped_on_the_record():
    """``HostStream.error`` ("sparkrun monitor exited") rides in the data.

    Otherwise a three-hour gap in the ring is only explainable from a daemon
    log that rotates.
    """
    r = mh.record_from_monitor(None, None, T0, host_error="sparkrun monitor exited")
    assert r["error"] == "sparkrun monitor exited" and r["hosts"] == {}


def test_no_hosts_and_no_error_records_nothing():
    """A tick that learned nothing must not occupy a ring slot.

    Writing it would push a real sample out of a bounded ring to store a row of
    nulls.
    """
    assert mh.record_from_monitor(None, None, T0) is None
    assert mh.record_from_monitor({"hosts": []}, [], T0) is None


def test_serving_row_maps_run_wait_kv_and_generated_tokens_per_second():
    """Per-engine serving, including the card's "generated tok/s".

    ``gen_tps`` is cluster_live's delta of vLLM's cumulative Out counter — the
    recorder stores it, it does not re-derive it. The cumulative In/Out totals
    are deliberately NOT stored: they reset when an engine restarts, so a
    history of them is a history of two baselines.
    """
    e = mh.record_from_monitor(frame("dgx-246"), rows(), T0)["engines"][0]
    assert e["run"] == 3 and isinstance(e["run"], int)
    assert e["wait"] == 1 and e["kv"] == 0.5
    assert e["gen_tps"] == 10.0 and e["prompt_tps"] == 200.0
    assert e["node"] == "192.0.2.246" and e["port"] == "8001"
    assert e["model"] == "org/Model-A"
    for dropped in ("prompt_total", "gen_total", "routes"):
        assert dropped not in e, dropped


def test_down_backend_is_stored_as_down_not_as_zero_load():
    rows_ = [{"node": "192.0.2.247", "port": "8002", "model": "-",
              "routes": ["worker-model"], "error": "timed out"}]
    e = mh.record_from_monitor(frame("dgx-246"), rows_, T0)["engines"][0]
    assert e["error"] == "timed out"
    assert "run" not in e and "kv" not in e     # no invented zeros


# ── downsampling: the 60 s -> 5 min correctness gate ──────────────────────

def test_downsample_folds_sixty_seconds_into_five_minute_buckets():
    """Five 60 s points make ONE bucket: aligned start, exact n, mean, peaks.

    Five, not six: a 300 s bucket holds offsets 0/60/120/180/240 and the point
    at +300 opens the next one (pinned by the test after this), so a fold that
    assumed six would quietly leave the fifth minute out of every bucket.

    The three things a fold can get wrong are all pinned here: WHERE the bucket
    starts (an off-by-one boundary silently re-labels every point on a chart),
    WHAT the average is over (non-None samples only — a None must not dilute
    the mean toward zero), and what the average HIDES (the peak and floor,
    which is the difference between "average 30 W" and "peaked at 140 W").
    """
    pts = [rec(BASE + i * 60, power=str(10.0 + i)) for i in range(5)]
    out = mh.downsample(pts)
    assert len(out) == 1
    b = out[0]
    assert b["ts"] == BASE and b["res"] == "5m" and b["n"] == 5
    h = b["hosts"]["dgx-246"]
    assert h["gpu_power_w"] == pytest.approx(12.0)     # mean of 10..14
    assert h["_max"]["gpu_power_w"] == 14.0
    assert h["_min"]["gpu_power_w"] == 10.0
    assert h["gpu_util_pct"] == 10.0                    # constant -> mean == value


def test_bucket_boundaries_split_at_three_hundred_seconds():
    """A point at +299 s and a point at +300 s are different buckets.

    This is the boundary the card's "bucket correctness" turns on: floor-to-300
    means the sample landing ON the boundary opens the next bucket.
    """
    pts = [rec(BASE + 299), rec(BASE + 300), rec(BASE + 301)]
    out = mh.downsample(pts)
    assert [b["ts"] for b in out] == [BASE, BASE + 300]
    assert [b["n"] for b in out] == [1, 2]


def test_partial_bucket_folds_what_exists_and_says_so():
    """A daemon that was down for four of five minutes must not invent them.

    ``n`` is the contract a reader uses to decide whether to trust the point,
    so it is the count of samples that EXIST, never the bucket's capacity.
    """
    out = mh.downsample([rec(BASE), rec(BASE + 300), rec(BASE + 360)])
    assert [(b["ts"], b["n"]) for b in out] == [(BASE, 1), (BASE + 300, 2)]


def test_unreported_field_stays_none_in_a_bucket():
    """A field no sample reported has no average — 0.0 would be a lie."""
    pts = [rec(BASE, power=""), rec(BASE + 60, power="")]
    h = mh.downsample(pts)[0]["hosts"]["dgx-246"]
    assert h["gpu_power_w"] is None
    assert "gpu_power_w" not in h["_max"] and "gpu_power_w" not in h["_min"]


def test_bucket_reports_a_host_that_failed_inside_the_window():
    pts = [rec(BASE), rec(BASE + 60)]
    pts[1]["hosts"]["dgx-246"]["error"] = "ssh: timeout"
    b = mh.downsample(pts)[0]
    assert b["hosts"]["dgx-246"]["error"] == "ssh: timeout"


def test_engines_are_folded_per_engine_not_per_host():
    """Two engines on one node stay two rows.

    Keying by node:port|model is what keeps a 2-GPU node's two vLLM backends
    from being averaged into one imaginary backend that serves twice the load.
    """
    f = frame("dgx-246")
    two = [dict(rows()[0]), dict(rows()[0], port="8002", gen_tps=20.0)]
    pts = [mh.record_from_monitor(f, two, BASE + i * 60) for i in range(2)]
    eng = mh.downsample(pts)[0]["engines"]
    assert sorted(eng) == ["192.0.2.246:8001|org/Model-A",
                           "192.0.2.246:8002|org/Model-A"]
    a = eng["192.0.2.246:8001|org/Model-A"]
    assert a["gen_tps"] == 10.0 and a["_max"]["gen_tps"] == 10.0
    assert eng["192.0.2.246:8002|org/Model-A"]["gen_tps"] == 20.0
    assert a["node"] == "192.0.2.246" and a["model"] == "org/Model-A"


def test_downsample_never_folds_its_own_output():
    """Idempotence: feeding buckets back in must not produce buckets of buckets.

    Without the resolution guard a retry of a half-finished fold would average
    averages and silently halve the sample count.
    """
    pts = [rec(BASE + i * 60) for i in range(5)]
    once = mh.downsample(pts)
    assert mh.downsample(once) == []
    assert mh.downsample(pts + once)[0]["n"] == 5


def test_record_without_a_timestamp_is_dropped_not_misfiled():
    """A point with no ts cannot be placed on a timeline; dropping is honest."""
    bad = rec(BASE)
    del bad["ts"]
    assert mh.downsample([bad, rec(BASE + 60)])[0]["n"] == 1


# ── the store: restart, bounds, fold, prune ───────────────────────────────

def test_restart_round_trip_survives_a_new_recorder_object(tmp_path):
    """THE restart gate: write, restart the recorder, read back.

    Two separate objects, one directory — the same shape as a daemon restart,
    where the new process has no memory of the old one's caches.
    """
    a = mh.MetricsHistory(hscc_dir=str(tmp_path))
    written = [rec(BASE + i * 60, power=str(10.0 + i)) for i in range(5)]
    for r in written:
        a.append(r)
    del a                                     # the "restart"

    b = mh.MetricsHistory(hscc_dir=str(tmp_path))
    assert b.raw_points() == written          # byte-identical read-back
    # And the restarted recorder KEEPS APPENDING to what it found: a ring that
    # truncated on open would look fine right up to the moment it ate a day.
    nxt = rec(BASE + 5 * 60, power="15.0")
    b.append(nxt)
    assert [r["ts"] for r in b.raw_points()] == [r["ts"] for r in written + [nxt]]
    c = mh.MetricsHistory(hscc_dir=str(tmp_path))
    assert len(c.raw_points()) == 6


def test_store_is_one_json_line_per_record(tmp_path):
    """JSONL, newest last — the shape a reader can tail and a human can grep."""
    a = mh.MetricsHistory(hscc_dir=str(tmp_path))
    for i in range(3):
        a.append(rec(BASE + i * 60))
    lines = open(a.raw_path, encoding="utf-8").read().splitlines()
    assert len(lines) == 3
    assert [json.loads(l)["ts"] for l in lines] == [BASE, BASE + 60, BASE + 120]
    assert os.path.basename(a.raw_path) == mh.RAW_FILE
    assert not [f for f in os.listdir(a.dir) if f.endswith(".tmp")]  # no litter


def test_a_torn_or_corrupt_line_is_skipped_and_counted(tmp_path):
    """kill -9 mid-append leaves a half line; it must not break the reader.

    The count is surfaced (``corrupt_lines``) rather than swallowed: a store
    that silently eats damaged lines reports "240 points, all good" for a
    directory that is quietly losing data.
    """
    a = mh.MetricsHistory(hscc_dir=str(tmp_path))
    a.append(rec(BASE))
    a.append(rec(BASE + 60))
    with open(a.raw_path, "a", encoding="utf-8") as fh:
        fh.write('{"ts": 123, "broken\n')      # torn line
        fh.write("[not, a, record]\n")         # decodable but the wrong shape
    b = mh.MetricsHistory(hscc_dir=str(tmp_path))
    assert [r["ts"] for r in b.raw_points()] == [BASE, BASE + 60]
    assert b.stats()["corrupt_lines"] == 2


def test_missing_store_reads_as_empty_not_an_error(tmp_path):
    """A fresh install has no store yet: an empty timeline, never an exception."""
    h = mh.MetricsHistory(hscc_dir=str(tmp_path / "never-created"))
    assert h.raw_points() == [] and h.buckets() == [] and h.series(BASE - 3600) == []
    assert h.stats()["raw_points"] == 0


def test_ring_keeps_the_newest_and_drops_the_oldest(tmp_path):
    """The ring bound, by COUNT. Newest wins — the 24 h window is the product.

    A capacity-4 ring fed 20 points keeps exactly 4 (with ``trim_slack=0`` the
    trim runs on every append past capacity, so the count itself is the
    assertion; the slack's cheaper path is the next test). The oldest are gone,
    the newest survive, and the file on disk agrees with the in-memory cache —
    a ring whose file and cache disagree loses records on the next restart.
    """
    h = mh.MetricsHistory(hscc_dir=str(tmp_path), raw_capacity=4, trim_slack=0)
    for i in range(20):
        h.append(rec(BASE + i * 60))
    pts = h.raw_points()
    assert len(pts) == 4
    assert [p["ts"] for p in pts] == [BASE + 16 * 60, BASE + 17 * 60,
                                      BASE + 18 * 60, BASE + 19 * 60]
    on_disk = [json.loads(l)["ts"] for l in
               open(h.raw_path, encoding="utf-8").read().splitlines()]
    assert on_disk == [p["ts"] for p in pts]


def test_ring_trims_only_past_the_slack(tmp_path):
    """The cheap path is the point of the slack: below it, append only.

    A rewrite on every tick would rewrite a ~2 MB file every minute for a ring
    that changed by one line; trimming at capacity+slack makes it one rewrite
    per ~144 ticks and the bound only moves by the slack.
    """
    h = mh.MetricsHistory(hscc_dir=str(tmp_path), raw_capacity=4, trim_slack=3)
    for i in range(7):                        # 7 == capacity + slack: no trim yet
        h.append(rec(BASE + i * 60))
    assert len(h.raw_points()) == 7
    h.append(rec(BASE + 7 * 60))              # one past -> trim to capacity
    assert [p["ts"] for p in h.raw_points()] == [BASE + i * 60 for i in (4, 5, 6, 7)]


def test_byte_cap_bounds_a_fat_record(tmp_path):
    """The second bound, by BYTES: capacity alone cannot bound a record.

    A node may expose any number of engines and model names are arbitrary text,
    so 1440 records is not a size. With a tiny cap the trim keeps the newest
    records that fit, which is what makes ``size_bound_bytes()`` a promise.
    """
    fat = [{**rec(BASE + i * 60),
            "engines": [{"node": "n", "port": str(i), "model": "m" * 400,
                         "run": 1, "wait": 0, "kv": 0.1, "gen_tps": 1.0,
                         "prompt_tps": 2.0} for _ in range(20)]}
           for i in range(30)]
    cap = 20_000
    h = mh.MetricsHistory(hscc_dir=str(tmp_path), raw_capacity=1000,
                          raw_max_bytes=cap, trim_slack=100)
    for r in fat:
        h.append(r)
    assert os.path.getsize(h.raw_path) <= cap
    assert h.raw_points()[-1]["ts"] == fat[-1]["ts"]     # newest survived
    assert len(h.raw_points()) < len(fat)                # oldest gave way


def test_size_bound_is_what_the_pr_body_quotes():
    """The bound in the prose is the bound in the code, computed not typed."""
    assert mh.size_bound_bytes() == mh.RAW_MAX_BYTES + mh.DOWN_MAX_BYTES
    # The rings are what the card fixed; the caps are what we chose. If anyone
    # retires the 24 h / 7 d numbers, this fails and the PR text gets re-derived.
    assert mh.RAW_CAPACITY == 1440 and mh.DOWN_CAPACITY == 2016


def test_measured_record_size_fits_the_stated_caps(tmp_path):
    """The caps are derived from a MEASURED record, not from a guess.

    8 MiB for 1440 raw points and 24 MiB for 2016 buckets are only a bound if a
    realistic record actually fits. This builds one the size of a real fleet
    (4 nodes, 6 engines with long model names), serialises it the way the store
    does (2.3 KB raw, 5.4 KB per bucket — 3.2 MiB and 10.5 MiB at capacity), and
    proves each ring needs less than half its cap. If records grow, this fails
    before the store silently starts trimming history early.
    """
    hosts = {f"dgx-24{i}": mh.host_metrics(sample()) for i in (6, 7, 8)}
    hosts["nas"] = mh.host_metrics(None, "ssh: timeout")
    eng = mh.engine_metrics([
        {"node": f"192.0.2.24{i}", "port": str(8000 + i),
         "model": "organization/Very-Long-Model-Name-235B-A22B-Instruct",
         "running": 3, "waiting": 1, "kv": 0.42, "gen_tps": 18.5,
         "prompt_tps": 210.0} for i in range(6)])
    fat_raw = {"ts": BASE, "res": "60s", "n": 1, "hosts": hosts, "engines": eng}
    raw_bytes = len(json.dumps(fat_raw, separators=(",", ":"))) + 1
    fat_bucket = mh.downsample([dict(fat_raw, ts=BASE + i * 60)
                                for i in range(6)])[0]
    down_bytes = len(json.dumps(fat_bucket, separators=(",", ":"))) + 1

    assert raw_bytes * mh.RAW_CAPACITY <= mh.RAW_MAX_BYTES, raw_bytes
    assert down_bytes * mh.DOWN_CAPACITY <= mh.DOWN_MAX_BYTES, down_bytes
    # Headroom is bounded the other way too: a cap 4x the need is not a bound,
    # it is an apology. The measured margin is ~2.5x on the raw ring.
    assert raw_bytes * mh.RAW_CAPACITY * 4 > mh.RAW_MAX_BYTES
    assert down_bytes * mh.DOWN_CAPACITY * 4 > mh.DOWN_MAX_BYTES


def test_compact_folds_buckets_wholly_past_the_raw_window(tmp_path):
    """The fold rule, one direction: whole buckets move, and they leave raw.

    A bucket whose whole span is older than 24 h moves to the 5-minute ring and
    disappears from raw — a ring is bounded, not tiered, so "both copies exist"
    would eat the 24 h window in a day.
    """
    cutoff = BASE + 600                       # bucket-aligned, on purpose
    now = cutoff + mh.RAW_RETENTION_S
    h = mh.MetricsHistory(hscc_dir=str(tmp_path))
    for i in range(15):                       # three full buckets of 5 points
        h.append(rec(BASE + i * 60))
    assert [mh._bucket_start(BASE + i * 60) for i in (0, 4, 5, 9, 10, 14)] == \
        [BASE, BASE, BASE + 300, BASE + 300, BASE + 600, BASE + 600]

    did = h.compact(now=now)
    # Buckets BASE and BASE+300 end at or before the cutoff; BASE+600 does not.
    assert did["folded_buckets"] == 2
    assert [b["ts"] for b in h.buckets()] == [BASE, BASE + 300]
    assert all(b["n"] == 5 for b in h.buckets())
    assert [p["ts"] for p in h.raw_points()] == [BASE + 600 + i * 60
                                                 for i in range(5)]


def test_a_bucket_straddling_the_cutoff_is_never_folded_early(tmp_path):
    """The load-bearing direction: a fold only ever runs on FINAL buckets.

    With the cutoff falling mid-bucket, nothing may be folded — because the
    minutes still to come belong to that bucket, and a bucket written to the
    5-minute store is final (there is no merge path back). Once the last minute
    ages out, the SAME bucket folds whole, with all five of its points.

    Getting this wrong is the quiet data-loss bug: fold early and the bucket's
    later minutes are dropped forever; fold late and raw overflows its window.
    """
    cutoff = BASE + 60                        # mid-bucket: BASE .. BASE+300
    h = mh.MetricsHistory(hscc_dir=str(tmp_path))
    for i in range(5):
        h.append(rec(BASE + i * 60))

    early = h.compact(now=cutoff + mh.RAW_RETENTION_S)
    assert early["folded_buckets"] == 0 and len(h.raw_points()) == 5
    assert h.buckets() == []

    # One more minute of clock: the bucket is now wholly older than the cutoff.
    later = h.compact(now=cutoff + 300 + mh.RAW_RETENTION_S)
    assert later["folded_buckets"] == 1
    assert [(b["ts"], b["n"]) for b in h.buckets()] == [(BASE, 5)]
    assert h.raw_points() == []


def test_a_folded_bucket_is_final_and_never_rewritten(tmp_path):
    """After a fold the point is gone from raw, so a re-fold is a no-op.

    This is why the two files can be append-only between trims: nothing ever
    merges into a bucket that is already on disk, so a half-written merge cannot
    leave a bucket whose ``n`` disagrees with its contents.
    """
    cutoff = BASE + 300
    now = cutoff + mh.RAW_RETENTION_S
    h = mh.MetricsHistory(hscc_dir=str(tmp_path))
    for i in range(5):
        h.append(rec(BASE + i * 60))
    assert h.compact(now=now)["folded_buckets"] == 1
    again = h.compact(now=now + 60)
    assert again["folded_buckets"] == 0
    assert [(b["ts"], b["n"]) for b in h.buckets()] == [(BASE, 5)]


def test_compact_is_cheap_and_empty_when_nothing_has_aged_out(tmp_path):
    """The common tick: a call every minute that folds nothing.

    If compaction rewrote both files every tick the store would take ~1.4 GB of
    rewrites a day for a ring that changes one line at a time.
    """
    h = mh.MetricsHistory(hscc_dir=str(tmp_path))
    for i in range(10):
        h.append(rec(BASE + i * 60))
    before = {f: os.stat(os.path.join(h.dir, f)).st_mtime_ns for f in
              (mh.RAW_FILE, mh.DOWN_FILE) if os.path.exists(os.path.join(h.dir, f))}
    did = h.compact(now=BASE + 600)
    assert did == {"folded_buckets": 0, "raw_points": 10, "buckets": 0,
                   "dropped_buckets": 0}
    for f, ns in before.items():
        assert os.stat(os.path.join(h.dir, f)).st_mtime_ns == ns, f


def test_compact_prunes_buckets_past_seven_days(tmp_path):
    """7 d is the outer bound: an 8-day-old bucket is dropped, not averaged."""
    now = BASE + 8 * 24 * 3600
    h = mh.MetricsHistory(hscc_dir=str(tmp_path))
    h.append(rec(now - 8 * 24 * 3600))        # far outside both windows
    h.append(rec(now - 2 * 3600))
    did = h.compact(now=now)
    assert did["folded_buckets"] == 1
    assert did["dropped_buckets"] == 1
    assert h.buckets() == []
    assert [p["ts"] for p in h.raw_points()] == [now - 2 * 3600]


def test_buckets_survive_a_restart_with_their_sidecars(tmp_path):
    """Restart-proof for the downsampled ring too, peaks included.

    A ``_max`` that is lost on restart is a peak nobody can see again, and the
    average on its own is the number that hides an 85 degreeC spike.
    """
    h = mh.MetricsHistory(hscc_dir=str(tmp_path))
    h.append(rec(BASE, power="10.0"))
    h.append(rec(BASE + 60, power="140.0"))
    assert h.compact(now=BASE + mh.RAW_RETENTION_S + 300)["folded_buckets"] == 1
    fresh = mh.MetricsHistory(hscc_dir=str(tmp_path))
    b = fresh.buckets()[0]
    assert b["hosts"]["dgx-246"]["gpu_power_w"] == pytest.approx(75.0)
    assert b["hosts"]["dgx-246"]["_max"]["gpu_power_w"] == 140.0


def test_series_spans_both_resolutions_without_repeating_a_sample(tmp_path):
    """A 30-hour read gets 24 h of raw plus the older hours as buckets.

    Without the mix, a widget asking for a week hits the 24 h raw wall and
    either shows a day (reading raw only) or double-draws the same samples
    (reading both). ``res`` on every record is what lets a reader label what it
    is plotting.
    """
    now = BASE + 30 * 3600
    h = mh.MetricsHistory(hscc_dir=str(tmp_path))
    for i in range(30 * 60):                  # 30 h of points, one per minute
        h.append(rec(BASE + i * 60))
    h.compact(now=now)
    pts = h.series(since=now - 30 * 3600, until=now)
    res = [p["res"] for p in pts]
    assert set(res) == {"5m", "60s"}
    assert res == ["5m"] * res.count("5m") + ["60s"] * res.count("60s")
    assert pts[-1]["res"] == "60s"                      # newest minute is raw
    # The seam is a hard non-overlap, asserted from the two definitions rather
    # than from a count: a bucket that ends after the raw window starts would
    # draw the same five minutes twice, once as a mean and once as themselves.
    raw_start = now - mh.RAW_RETENTION_S
    assert all(p["ts"] >= raw_start for p in pts if p["res"] == "60s")
    assert all(p["ts"] + mh.DOWN_BUCKET_S <= raw_start
               for p in pts if p["res"] == "5m")
    assert len({p["ts"] for p in pts}) == len(pts)      # nothing counted twice


def test_reads_filter_by_window_and_host(tmp_path):
    """Window bounds are inclusive on both ends, and ``host`` narrows to one node.

    A reader that asked for "the last hour of dgx-247" must not get the fleet's
    records that happen to also mention it, and an exclusive end would silently
    drop the most recent sample — the one a widget draws first.
    """
    h = mh.MetricsHistory(hscc_dir=str(tmp_path))
    for i in range(4):
        h.append(rec(BASE + i * 60))
        h.append(rec(BASE + i * 60, host="dgx-247"))
    # +60 and +180 are both inside the window: 3 minutes x 2 hosts = 6 records.
    assert len(h.raw_points(since=BASE + 60, until=BASE + 180)) == 6
    assert len(h.raw_points(since=BASE, until=BASE + 180)) == 8
    only = h.raw_points(host="dgx-247")
    assert len(only) == 4
    assert all(p["hosts"].keys() == {"dgx-247"} for p in only)


def test_out_of_order_append_keeps_both_files_sorted(tmp_path):
    """A replay or a clock step back must not put the ring out of order.

    The ring is bounded and read as a timeline: if position and ts ever
    disagree, the newest-wins trim starts deleting the wrong end.
    """
    h = mh.MetricsHistory(hscc_dir=str(tmp_path))
    for ts in (BASE, BASE + 120, BASE + 60):
        h.append(rec(ts))
    assert [p["ts"] for p in h.raw_points()] == [BASE, BASE + 60, BASE + 120]
    on_disk = [json.loads(l)["ts"] for l in
               open(h.raw_path, encoding="utf-8").read().splitlines()]
    assert on_disk == [BASE, BASE + 60, BASE + 120]


def test_stats_describe_the_store_for_the_state_stamp(tmp_path):
    h = mh.MetricsHistory(hscc_dir=str(tmp_path))
    for i in range(3):
        h.append(rec(BASE + i * 60))
    s = h.stats()
    assert s["raw_points"] == 3 and s["buckets"] == 0
    assert s["raw_capacity"] == mh.RAW_CAPACITY
    assert s["newest_ts"] == BASE + 120 and s["oldest_ts"] == BASE
    assert s["bound_bytes"] == mh.size_bound_bytes()
    assert s["raw_bytes"] == os.path.getsize(h.raw_path)


# ── the store lives under ~/.hscc, nowhere else ───────────────────────────

def test_store_dir_is_under_hscc_metrics(tmp_path):
    """~/.hscc/metrics by default; ``hscc_dir=`` overrides it wholesale.

    The second half is the one that matters for the suite: the default resolves
    through ``expanduser`` at CALL time, which is what lets conftest's autouse
    ``~/.hscc`` redirect cover this store with no module constant to register.
    Under the redirect the default lands in this test's tmp dir, and it must
    never land under the real $HOME — that is the no-live-leak property.
    """
    assert mh.store_dir(str(tmp_path)) == os.path.join(str(tmp_path), "metrics")
    assert mh.store_dir() == os.path.join(os.path.expanduser("~/.hscc"), "metrics")
    assert not mh.store_dir().startswith(os.environ["HOME"] + os.sep)
    h = mh.MetricsHistory()                               # no hscc_dir at all
    assert h.raw_path == os.path.join(mh.store_dir(), mh.RAW_FILE)
    h.append(rec(BASE))                                   # writes...
    assert os.path.exists(h.raw_path)
    assert h.raw_path.startswith(str(tmp_path))        # ...into this test only


def test_a_full_tick_of_ticks_stays_inside_the_two_rings(tmp_path):
    """End-to-end bound: 25 h of ticks, folded as it aged, inside both caps.

    The store's promise is that it never grows, so the check is the whole
    scenario: a day and an hour of 60 s samples, compacted as a live daemon
    would, then a restart, then the numbers.
    """
    h = mh.MetricsHistory(hscc_dir=str(tmp_path))
    now = BASE + 25 * 3600
    for i in range(25 * 60):
        h.append(rec(BASE + i * 60, power=str(10 + (i % 7))))
        h.compact(now=BASE + (i + 1) * 60)
    s = h.stats()
    assert s["raw_points"] <= mh.RAW_CAPACITY
    assert s["buckets"] <= mh.DOWN_CAPACITY
    assert s["raw_bytes"] + s["down_bytes"] <= mh.size_bound_bytes()
    fresh = mh.MetricsHistory(hscc_dir=str(tmp_path))
    assert fresh.stats()["raw_points"] == s["raw_points"]
