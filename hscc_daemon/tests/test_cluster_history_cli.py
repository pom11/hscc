"""`hscc cluster history` — the CLI verb over the recorder's stored series.

Two things these tests exist to keep true:

1. THE SAME ROWS AS THE ROUTE. The fixture is built by the recorder's OWN write
   path (:meth:`MetricsRecorder.tick`, which rolls tier 0 -> 1 -> 2) and the
   expectation is the recorder's OWN read API (:meth:`MetricsStore.host_rows` /
   :meth:`engine_rows`) — never a re-derived mean. A rename in the recorder
   moves both sides together; a field the CLI silently dropped fails here. The
   route side of the same fixture is
   ``hscc-api/tests/test_cluster_history_cli_parity.py``.

2. THE SAME FLAGS AS THE CANONICAL ROUTE (t_447cc7fc ruling). ``--window`` and
   ``--tier`` are the route's two axes and mutually exclusive; ``--since`` /
   ``--until`` need ``--tier``; ``--node`` is the ONE machine filter (there is
   no ``--host``, because `host_rollup.host` and `engine_rollup.node` are one
   machine); neither axis is ``--window 1h``, the route's default. Every one of
   those is refused the way the route refuses it — a CLI that quietly picked a
   third meaning for a flag would be a second contract nobody keeps in sync.

The read-only rule is a requirement, not a style note: a history read must not
sample the fleet. Two guards here — ``metrics_history.get_sampler`` poisoned so
constructing a Sampler raises, and a recording ``sparkrun`` canary on PATH that
must stay untouched.

Fixtures are TEST-NET-1 addresses (192.0.2.x); no real host or token is read.
"""

import json
import os
import re
import stat
import sys

import pytest

import hscc_daemon.hscc as unified
from hscc_daemon import metrics_history as mh
from hscc_daemon import metrics_history_cli as mhc

HOST_A = "192.0.2.10"                       # TEST-NET-1, never a node
HOST_B = "192.0.2.11"
PORT_A, PORT_B = "8000", "8001"
MODEL = "org/Qwen-Test"
NOW = 1_795_000_000 - (1_795_000_000 % 300)
TICKS = 360                                 # 12 minutes at 2 s


# ── the fixture: the recorder's own write path, nothing hand-written ───────

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
    """HOST_A answers everything; HOST_B never reports GPU temp or power, so
    the ``None`` path has to survive both the roll-up and the render."""
    return {"hosts": [
        {"host": HOST_A, "error": None,
         "sample": sample(ts, gpu_power_w=str(100.0 + (ts % 60)))},
        {"host": HOST_B, "error": None,
         "sample": sample(ts, host=HOST_B, gpu_temp_c="", gpu_power_w="",
                          gpu_util_pct="0")}]}


def serving_rows(ts):
    return [{"node": HOST_A, "port": PORT_A, "model": MODEL,
             "running": 1 + int(ts % 3), "waiting": 0, "kv": 0.42,
             "gen_tps": "118.5", "prompt_tps": "900"},
            {"node": HOST_B, "port": PORT_B, "model": None,
             "error": "backend unreachable"}]


@pytest.fixture
def store_dir(tmp_path):
    rec = mh.MetricsRecorder(hscc_dir=str(tmp_path))
    for i in range(TICKS):
        ts = NOW - (TICKS * mh.TIER0_INTERVAL_S) + i * mh.TIER0_INTERVAL_S
        rec.tick(monitor_frame(ts), serving_rows(ts), ts)
    rec.tick(monitor_frame(NOW), serving_rows(NOW), NOW)   # close the last minute
    assert rec.store.count(1) > 0 and rec.store.count(2) > 0, "fixture produced no rows"
    rec.store.close()
    return tmp_path


@pytest.fixture(autouse=True)
def _no_engine(monkeypatch):
    """Any history path that reaches the cluster engine (and therefore sparkrun)
    is a bug, so the loader raises. Help and dispatch both must stay inert."""
    def boom(*a, **k):
        raise AssertionError("cluster history loaded/ran the cluster engine")
    monkeypatch.setattr(unified, "_load_cluster_engine", boom)


@pytest.fixture(autouse=True)
def _no_sampler_construction(monkeypatch):
    """``get_sampler()`` CONSTRUCTS a Sampler — a second monitor and a second
    roll-up watermark writer. The CLI must never call it."""
    def never(*a, **k):
        raise AssertionError("cluster history called metrics_history.get_sampler()")
    monkeypatch.setattr(mh, "get_sampler", never)


@pytest.fixture(autouse=True)
def _no_sparkrun(monkeypatch, tmp_path):
    """A recording ``sparkrun`` canary: a history read must not shell out."""
    bin_dir = tmp_path / "canary-bin"
    bin_dir.mkdir()
    marker = tmp_path / "sparkrun-ran"
    fake = bin_dir / "sparkrun"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return marker


def run_argv(argv, monkeypatch, hscc_dir=None, now=NOW):
    """``hscc cluster <argv>`` in process -> (rc, stdout). The clock is pinned
    to the fixture instant, exactly as the parity test pins the route's."""
    import io
    from contextlib import redirect_stdout
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setattr(sys, "argv", ["hscc", "cluster", *argv])
    if hscc_dir is not None:
        import hscc_daemon.metrics_history_cli as mod
        # A test that runs the CLI twice (table then --json) patches this twice;
        # unwrap to the pristine function so the pin is applied once, not nested.
        original = getattr(mod.read_history, "_pristine", mod.read_history)

        def pinned(opts):
            return original(opts, hscc_dir=str(hscc_dir), now=now)
        pinned._pristine = original
        monkeypatch.setattr(mod, "read_history", pinned)
    out = io.StringIO()
    with redirect_stdout(out):
        code = unified._handle_cluster()
    return code, out.getvalue()


def run_history(argv, monkeypatch, hscc_dir=None, now=NOW):
    """``hscc cluster history <argv>`` in process -> (rc, stdout)."""
    return run_argv(["history", *argv], monkeypatch=monkeypatch,
                    hscc_dir=hscc_dir, now=now)


# ── the flag surface IS the route's (t_447cc7fc) ────────────────────────────

def canon(text):
    """Collapse whitespace, the way ``tests/test_cluster_group_help.py`` does.

    A refusal is wrapped to the cluster help width (<=78), so a fragment of it
    can straddle a line break; comparing canonically is what lets the test pin
    WORDING without pinning where the lines happen to break.
    """
    return re.sub(r"\s+", " ", text).strip()


@pytest.mark.parametrize("argv,fragment", [
    (["--window", "2h"], "is not a window"),
    (["--window"], "'--window' needs one of"),
    (["--window", "1h", "--tier", "1"], "may not both be given"),
    (["--tier", "0", "--window", "15m"], "may not both be given"),
    (["--tier", "7"], "out of range"),
    (["--tier", "gpu"], "is not a tier"),
    (["--tier"], "'--tier' needs a value"),
    (["--tier", "--json"], "'--tier' needs a value"),
    (["--since", "1794999000"], "need --tier"),
    (["--until", "1794999000"], "need --tier"),
    (["--window", "1h", "--since", "1794999000"], "need --tier"),
    (["--tier", "1", "--since", "1h"], "not epoch seconds"),
    (["--tier", "1", "--since", "5", "--until", "1"], "runs the wrong way"),
    (["--node", "bad host"], "is not a host name"),
    (["--node", ""], "is not a host name"),
    (["--node"], "'--node' needs a host name"),
    (["--host", HOST_A], "is not a 'hscc cluster history' flag"),
    (["--metrics"], "needs at least one field name"),
    (["--metrics", ","], "needs at least one field name"),
    (["--metrics", "gpu_colour"], "unknown metric"),
    (["--tier", "0", "--metrics", "gpu_temp_c_max"], "not stored for tier 0"),
    (["--bogus"], "is not a 'hscc cluster history' flag"),
])
def test_a_bad_request_is_refused_rather_than_guessed(argv, fragment, store_dir,
                                                      monkeypatch):
    rc, out = run_history(argv, hscc_dir=store_dir, monkeypatch=monkeypatch)
    assert rc == mhc.USAGE_ERROR_RC, out
    assert fragment in canon(out), out
    assert out.startswith("Error: "), out
    # A refusal reads as a refusal: it says nothing was read, and it never
    # prints a table the operator might mistake for an answer. (Not "the word
    # cpu_usage_pct is absent" — an unknown-metric refusal legitimately names
    # the fields the recorder does store.)
    assert "Nothing was read." in canon(out), out
    assert not any(ln.startswith("ts ") for ln in out.splitlines()), out


def test_nothing_is_shelled_out_for_a_refusal(store_dir, monkeypatch, tmp_path):
    rc, _out = run_history(["--tier"], hscc_dir=store_dir,
                           monkeypatch=monkeypatch)
    assert rc == 1
    assert not (tmp_path / "sparkrun-ran").exists(), \
        "a refused history read shelled out to sparkrun"


def test_the_equals_form_is_the_same_request(store_dir):
    a, err_a = mhc.parse_history_args(["--tier", "1", "--node", HOST_A])
    b, err_b = mhc.parse_history_args(["--tier=1", f"--node={HOST_A}"])
    assert err_a is None and err_b is None
    assert a == b


def test_no_axis_is_the_routes_default_window(store_dir):
    bare, err = mhc.parse_history_args([])
    explicit, err2 = mhc.parse_history_args(["--window", "1h"])
    assert err is None and err2 is None
    assert bare == explicit
    assert bare["window"] == mhc.DEFAULT_WINDOW == "1h" and bare["tier"] == 1


def test_each_window_serves_the_tier_the_recorder_defines(store_dir):
    for window, (tier, _bucket, span) in mhc.history_windows(mh).items():
        opts, err = mhc.parse_history_args(["--window", window])
        assert err is None, window
        payload = mhc.read_history(opts, hscc_dir=str(store_dir), now=NOW)
        assert payload["tier"] == tier and payload["window_s"] == span
        assert payload["since"] == NOW - span and payload["until"] == NOW
        assert payload["resolution"] == mhc.resolution_label(mh, tier)


def test_tier_alone_answers_its_own_retention(store_dir):
    for tier in (1, 2):
        opts, err = mhc.parse_history_args(["--tier", str(tier)])
        assert err is None
        payload = mhc.read_history(opts, hscc_dir=str(store_dir), now=NOW)
        assert payload["window"] is None, "the tier axis has no window preset"
        assert payload["window_s"] == mhc.tier_span(mh, tier)
        assert payload["since"] == NOW - mhc.tier_span(mh, tier)


def test_explicit_bounds_are_inclusive_bucket_starts(store_dir):
    store = mh.MetricsStore(hscc_dir=str(store_dir))
    try:
        rows = store.host_rows(1)
    finally:
        store.close()
    pick = rows[3]["ts"]
    opts, err = mhc.parse_history_args(
        ["--tier", "1", "--since", str(pick), "--until", str(pick)])
    assert err is None
    payload = mhc.read_history(opts, hscc_dir=str(store_dir))
    assert payload["hosts"], "an inclusive single-bucket window came back empty"
    assert {r["ts"] for r in payload["hosts"]} == {pick}


# ── the rows: verbatim the recorder's, names and all ─────────────────────────

@pytest.mark.parametrize("tier", [1, 2])
def test_rows_are_the_recorders_rows_verbatim(store_dir, tier):
    opts, err = mhc.parse_history_args(["--tier", str(tier)])
    assert err is None
    payload = mhc.read_history(opts, hscc_dir=str(store_dir), now=NOW)
    store = mh.MetricsStore(hscc_dir=str(store_dir))
    try:
        assert payload["hosts"] == store.host_rows(
            tier, since=payload["since"], until=payload["until"], host=None)
        assert payload["engines"] == store.engine_rows(
            tier, since=payload["since"], until=payload["until"], node=None)
    finally:
        store.close()
    assert payload["counts"] == {"hosts": len(payload["hosts"]),
                                 "engines": len(payload["engines"])}
    assert payload["max_fields"] == [f"{k}_max" for k in mh.MAX_KEYS]


def test_the_one_node_filter_narrows_both_tables(store_dir):
    """`host_rollup.host` and `engine_rollup.node` are one machine, so `--node`
    filters both tables — there is no second flag to ask for a second machine."""
    opts, err = mhc.parse_history_args(["--window", "24h", "--node", HOST_A])
    assert err is None
    payload = mhc.read_history(opts, hscc_dir=str(store_dir), now=NOW)
    assert {r["host"] for r in payload["hosts"]} == {HOST_A}
    assert {r["node"] for r in payload["engines"]} == {HOST_A}
    assert payload["node"] == HOST_A


def test_engine_identity_is_node_and_port_and_model_rides_along(store_dir):
    opts, _err = mhc.parse_history_args(["--tier", "1"])
    payload = mhc.read_history(opts, hscc_dir=str(store_dir), now=NOW)
    keys = {(r["node"], r["port"]) for r in payload["engines"]}
    assert keys == {(HOST_A, PORT_A), (HOST_B, PORT_B)}
    by_key = {(r["node"], r["port"]): r for r in payload["engines"]}
    assert by_key[(HOST_A, PORT_A)]["model"] == MODEL
    assert by_key[(HOST_B, PORT_B)]["model"] is None, \
        "an unresolved model is not a key and must not be invented"


def test_metrics_keeps_identity_and_the_named_fields_only(store_dir):
    opts, err = mhc.parse_history_args(
        ["--window", "1h", "--metrics", "gpu_util_pct,gpu_temp_c"])
    assert err is None
    payload = mhc.read_history(opts, hscc_dir=str(store_dir), now=NOW)
    for row in payload["hosts"]:
        assert set(row) <= set(mhc.HOST_IDENTITY) | {"gpu_util_pct",
                                                     "gpu_temp_c"}
        assert {"tier", "ts", "host"} <= set(row), "identity was projected away"
    assert payload["max_fields"] == []


def test_a_filter_that_names_no_engine_field_excludes_the_series(store_dir):
    """Reported as excluded, not as an empty list: "no engine points" must not
    be readable as "the fleet served nothing"."""
    opts, err = mhc.parse_history_args(["--window", "1h",
                                        "--metrics", "gpu_util_pct"])
    assert err is None
    payload = mhc.read_history(opts, hscc_dir=str(store_dir), now=NOW)
    assert payload["hosts"] and payload["hosts_included"] is True
    assert payload["engines"] == [] and payload["engines_included"] is False
    text = mhc.render_history(payload)
    assert "excluded by --metrics" in text and "not an empty series" in text


# ── the human table: peaks beside averages, None never 0 ─────────────────────

def test_the_host_table_prints_every_column_the_store_holds(store_dir,
                                                            monkeypatch):
    _rc, out = run_history(["--tier", "1"], hscc_dir=store_dir,
                           monkeypatch=monkeypatch)
    header = next(ln for ln in out.splitlines() if ln.startswith("ts "))
    printed = header.split()
    missing = [c for c in mh.HOST_COLUMNS if c not in printed]
    assert not missing, f"stored columns the table cannot show: {missing}"
    assert {"host", "n", "error"} <= set(printed)


def test_a_peak_prints_beside_the_average_it_is_the_peak_of(store_dir,
                                                            monkeypatch):
    _rc, out = run_history(["--tier", "1"], hscc_dir=store_dir,
                           monkeypatch=monkeypatch)
    cols = next(ln for ln in out.splitlines()
                if ln.startswith("ts ")).split()
    for key in mh.MAX_KEYS:
        assert cols.index(f"{key}_max") == cols.index(key) + 1, \
            f"{key}_max is not beside {key}: {cols}"
    # And the peak is really the peak: the fixture makes HOST_A's power spike
    # above its minute average, so a table that folded them would show one.
    cells = _row_cells(out, HOST_A)
    avg = cells[cols.index("gpu_power_w")]
    peak = cells[cols.index("gpu_power_w_max")]
    assert avg != peak, f"peak and average printed identically: {avg}"
    assert float(peak) > float(avg), f"peak {peak} below average {avg}"


def _row_cells(out, host):
    """The first host-table row whose machine cell is `host`, as its cells.

    One column is one token (the ts cell carries no space — see
    :func:`metrics_history_cli._ts_cell`), so the cells index 1:1 against the
    header the test passed them with.
    """
    for ln in out.splitlines():
        cells = ln.split()
        if len(cells) > 3 and cells[1] == host and cells[0][:2].isdigit():
            return cells
    raise AssertionError(f"no printed row for {host}")


def test_a_field_the_node_never_reported_prints_an_em_dash_never_zero(
        store_dir, monkeypatch):
    _rc, out = run_history(["--tier", "1"], hscc_dir=store_dir,
                           monkeypatch=monkeypatch)
    cols = next(ln for ln in out.splitlines()
                if ln.startswith("ts ")).split()
    cells = _row_cells(out, HOST_B)
    for key in ("gpu_temp_c", "gpu_temp_c_max", "gpu_power_w", "gpu_power_w_max"):
        cell = cells[cols.index(key)]
        assert cell == mhc.MISSING, (
            f"{key} for the host that never reported it printed {cell!r}; "
            "a blank must not become a 0")
    # ...and the same cell in the machine contract is null, not 0.0.
    _rc, raw = run_history(["--tier", "1", "--json"], hscc_dir=store_dir,
                           monkeypatch=monkeypatch)
    payload = json.loads(raw)
    row_b = next(r for r in payload["hosts"] if r["host"] == HOST_B)
    assert row_b["gpu_temp_c"] is None and row_b["gpu_power_w"] is None
    assert row_b["gpu_temp_c_max"] is None and row_b["gpu_power_w_max"] is None


def test_json_carries_the_routes_vocabulary_and_preserves_the_nulls(
        store_dir, monkeypatch):
    _rc, raw = run_history(["--window", "6h", "--json"], hscc_dir=store_dir,
                           monkeypatch=monkeypatch)
    payload = json.loads(raw)
    assert payload["window"] == "6h" and payload["tier"] == 1
    assert payload["window_s"] == 6 * 3600
    assert payload["resolution"] == "60s" and payload["resolution_s"] == 60
    assert payload["tier_summary"].startswith("1-minute averages")
    assert payload["metrics"] == list(mh.HOST_COLUMNS) + list(mh.ENGINE_KEYS)
    assert any(v is None for r in payload["hosts"] for v in r.values())


# ── emptiness is stated, not implied ────────────────────────────────────────

@pytest.mark.parametrize("argv", [["--tier", "0"], ["--window", "15m"]])
def test_tier_zero_says_the_ring_is_not_here_rather_than_an_empty_table(
        argv, store_dir, monkeypatch):
    rc, out = run_history(argv, hscc_dir=store_dir, monkeypatch=monkeypatch)
    assert rc == 0, "a stated 'not readable here' is not a command failure"
    assert "note: tier 0 is the sampler's in-memory ring" in out
    assert "--tier 1" in out, "the answer must say what to ask for instead"
    assert "cpu_usage_pct" not in out, \
        "a table with zero rows would read as an empty history"
    assert "not an empty history" in out


def test_tier_zero_json_states_residency_and_reason(store_dir):
    opts, _err = mhc.parse_history_args(["--tier", "0"])
    payload = mhc.read_history(opts, hscc_dir=str(store_dir), now=NOW)
    assert payload["store"]["tier0_resident"] is False
    assert payload["note"] and "--tier 1" in payload["note"]
    assert payload["hosts"] == [] and payload["engines"] == []
    assert payload["max_fields"] == [], "tier 0 has no roll-up, so no peaks"


def test_tier_zero_prints_the_ring_when_this_process_owns_it(store_dir):
    """The other tier-0 answer: a process that DOES sample and holds points."""
    rec = mh.MetricsRecorder(hscc_dir=str(store_dir))
    for i in range(3):
        ts = NOW - 4 + i * 2
        rec.tick(monitor_frame(ts), serving_rows(ts), ts)

    class _Stream:
        def close(self):
            pass

    class _Poller:
        def poll(self):
            return []

        def close(self):
            pass

    sampler = mh.Sampler(host_stream=_Stream(), serving=_Poller(), recorder=rec)
    mh.set_sampler(sampler)
    try:
        opts, _err = mhc.parse_history_args(["--window", "15m"])
        payload = mhc.read_history(opts, hscc_dir=str(store_dir), now=NOW)
    finally:
        mh.close_sampler()
    assert payload["store"]["tier0_resident"] is True
    assert {r["host"] for r in payload["hosts"]} == {HOST_A, HOST_B}
    assert all("gpu_temp_c_max" not in r for r in payload["hosts"])


def test_a_missing_store_is_reported_missing_and_stays_missing(tmp_path,
                                                              monkeypatch):
    empty = tmp_path / "fresh"
    empty.mkdir()
    opts, _err = mhc.parse_history_args(["--window", "1h"])
    payload = mhc.read_history(opts, hscc_dir=str(empty), now=NOW)
    assert payload["store"]["exists"] is False
    assert payload["counts"] == {"hosts": 0, "engines": 0}
    assert "has not written its store yet" in payload["note"]
    assert not os.path.exists(mh.db_path(str(empty))), \
        "a read created the store it was only supposed to look at"
    rc, out = run_history(["--window", "1h"], hscc_dir=empty,
                          monkeypatch=monkeypatch)
    assert rc == 0
    assert "no such file" in out
    assert "has not written its store yet" in out


# ── help is inert ───────────────────────────────────────────────────────────

def test_verb_help_prints_usage_and_runs_nothing(monkeypatch):
    rc, out = run_argv(["history", "--help"], monkeypatch=monkeypatch)
    assert rc == 0
    assert out.startswith("Usage: hscc cluster history [--window W | --tier N]")
    for token in ("--window", "--tier", "--since", "--until", "--node",
                  "--metrics", "--json"):
        assert token in out, f"{token} undocumented"
    assert "15m|1h|6h|24h|7d" in out


def test_group_help_lists_the_new_verb(monkeypatch):
    rc, out = run_argv(["--help"], monkeypatch=monkeypatch)
    assert rc == 0
    assert "  hscc cluster history [--window W|--tier N]" in out.splitlines()


def test_an_unknown_cluster_verb_is_still_refused(monkeypatch):
    rc, out = run_argv(["histor"], monkeypatch=monkeypatch)
    assert rc == 1
    assert "unknown cluster subcommand" in out
