"""The byte budget: `?max_bytes=` answers with coarser buckets, never fewer.

Why this file exists: t_25fcf502 measured `GET /v1/cluster/history` unable to
answer under the widget contract's <= 16 KiB-per-surface ceiling at ANY window —
even its narrowest possible 7 d read (one node, one metric) is 119,531 B, 7.3x
over. The rows are the cost, so the only lever left over an unbounded axis is to
serve FEWER BUCKETS. That is a dangerous thing to add to a route whose whole
contract is that `nil` is a gap: a fold that averages an excursion away, or that
coerces a `None`, or that quietly drops the newest points, would break §5.6 while
sizing it correctly. So every test here attacks one of those three failure modes,
and the oracle is the recorder's own stored rows — never a re-derived mean.

The strongest assertion in the file is `test_the_budget_is_met_on_the_wire`: it
counts the BYTES of the body a real client reads off a real socket, because the
number the route compared and the number `Content-Length` names have to be the
same number or the budget is a claim and not a property.

Fixtures are TEST-NET-1 addresses (192.0.2.x); no real host, tenant or token is
read. The store is built by the recorder's own `tick`, and the server and the
recorder share one `hscc_dir`.
"""

import http.client
import json
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
PORT = "8000"
MODEL = "org/Qwen-Test"

#: aligned to 2 s, 1 min and 5 min, so every bucket edge in the fixture is a
#: real one; the route's clock is pinned to the same instant.
NOW = 1_795_000_000 - (1_795_000_000 % 300)
MINUTES = 30                     # the span before the gap
GAP_MINUTES = 6                  # the recorder heard NOTHING for this long
#: One 2 s reading far above its own minute's mean, placed 10 minutes back
#: from the pinned clock: inside the ticked span (the gap starts 6 minutes
#: back) and on an even second, which is where the 2 s cadence actually puts
#: a sample. A minute-of-hour rule would depend on where NOW lands.
SPIKE_TS = NOW - 10 * 60
SPIKE_C = 98.75
TICKS = MINUTES * 60 // mh.TIER0_INTERVAL_S


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
    """HOST_A answers everything; HOST_B never reports its GPU temperature.

    HOST_B is the `nil`-is-a-gap case: whatever the fold does, a field the node
    never reported must come back `None` and never as `0`.
    """
    spike = {"cpu_temp_c": str(SPIKE_C)} if ts == SPIKE_TS else {}
    return {"hosts": [
        {"host": HOST_A, "error": None,
         "sample": sample(ts, gpu_power_w=str(100.0 + (ts % 60)), **spike)},
        {"host": HOST_B, "error": None,
         "sample": sample(ts, host=HOST_B, gpu_temp_c="", gpu_power_w="",
                          gpu_util_pct="0")}]}


def serving_rows(ts):
    return [{"node": HOST_A, "port": PORT, "model": MODEL,
             "running": 2, "waiting": 1, "kv": 12.5,
             "gen_tps": 40.0, "prompt_tps": 200.0,
             "prompt_total": 900000.0, "gen_total": 250000.0}]


@pytest.fixture(scope="module")
def filled_dir(tmp_path_factory):
    """A tier-1 store with MINUTES closed minutes, one spike, and a real gap.

    Module-scoped on purpose: the route only reads this store (three tests in
    `test_cluster_history_parity.py` guard that), and 900 recorder ticks per test
    would make the leg about the fixture instead of about the fold.
    """
    root = tmp_path_factory.mktemp("budget_store")
    rec = mh.MetricsRecorder(hscc_dir=str(root))
    # The ticks stop GAP_MINUTES before the pinned clock, so those buckets were
    # never written at all — a gap the fold must leave alone.
    start = NOW - (MINUTES + GAP_MINUTES) * 60
    for i in range(TICKS):
        ts = start + i * mh.TIER0_INTERVAL_S
        rec.tick(monitor_frame(ts), serving_rows(ts), ts)
    rec.tick(monitor_frame(NOW - GAP_MINUTES * 60),
             serving_rows(NOW - GAP_MINUTES * 60), NOW - GAP_MINUTES * 60)
    # ...then silence: nothing is ticked for GAP_MINUTES, so those buckets were
    # never written at all. A fold must not fill them in.
    assert rec.store.count(1) > 0, "fixture produced no tier-1 rows"
    rec.store.close()
    return root


@pytest.fixture(scope="module")
def running(filled_dir):
    srv = RunningServer(hscc_dir=str(filled_dir))
    yield srv
    srv.close()


@pytest.fixture(scope="module")
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


@pytest.fixture
def pinned_clock(running):
    original = routes_cluster._history_now
    routes_cluster._history_now = lambda: float(NOW)
    yield NOW
    routes_cluster._history_now = original


def get(running, token, path):
    return running.request(token=token, path=path)


def raw_get(running, token, path):
    """(status, raw_body_bytes). Parsed JSON cannot answer a byte question."""
    conn = http.client.HTTPConnection(running.host, running.port, timeout=10)
    conn.request("GET", path, headers={"Authorization": "Bearer " + token})
    resp = conn.getresponse()
    body = resp.read()
    status = resp.status
    length = resp.getheader("Content-Length")
    conn.close()
    return status, body, length


def stored_hosts(root, tier=1):
    """The recorder's own rows: the oracle underneath every assertion below.

    Takes the SAME `hscc_dir` the running server was handed, so the route and the
    oracle cannot be looking at two different stores.
    """
    store = mh.MetricsStore(hscc_dir=str(root))
    try:
        return store.host_rows(tier)
    finally:
        store.close()


# ── the budget is a property of the wire, not of the route's arithmetic ────

def test_the_budget_is_met_on_the_wire(running, token, pinned_clock):
    """The bytes a client reads off the socket fit the budget it asked for.

    This is the claim the card was raised for, so it is tested against the
    socket rather than against the payload dict: `Content-Length`, the raw body
    length and the requested budget must all agree.
    """
    budget = 4096
    status, body, length = raw_get(
        running, token, f"/v1/cluster/history?window=1h&max_bytes={budget}")
    assert status == 200, body
    assert len(body) <= budget, f"{len(body)} B served for a {budget} B budget"
    assert length is not None, "a JSON body must carry a Content-Length"
    assert int(length) == len(body)
    served = json.loads(body)
    assert served["budget_met"] is True
    assert served["fold"] > 1, "a 4 KiB budget over this fixture must fold"
    assert served["max_bytes"] == budget


def test_an_unbudgeted_read_is_unchanged(running, token, pinned_clock,
                                          filled_dir):
    """Nobody asked for a budget, so nothing happened: the stored rows verbatim.

    The fold is new code on the path of every history read. This is the pin that
    it is inert until asked — the same rows the recorder holds, at the stored
    resolution, with no verdict claimed about a budget nobody set.
    """
    status, plain = get(running, token, "/v1/cluster/history?window=1h")
    assert status == 200, plain
    assert plain["hosts"] == stored_hosts(filled_dir), \
        "the route stopped serving what it stores"
    assert plain["fold"] == 1 and plain["max_bytes"] is None
    assert plain["budget_met"] is None, "a verdict was claimed about no budget"
    assert plain["resolution_s"] == plain["native_resolution_s"] == 60


# ── what a fold must never do ──────────────────────────────────────────────

def test_the_fold_serves_the_whole_span_and_keeps_the_tail(running, token,
                                                           pinned_clock):
    """Folding coarsens the series; it does not shorten it.

    The newest point is the "now" a widget headlines, so a fold that trimmed the
    tail to hit a byte count would be the loudest possible regression: it would
    satisfy the size and break §5.6's rule at the same time.
    """
    status, plain = get(running, token, "/v1/cluster/history?window=1h")
    status, folded = get(running, token,
                         "/v1/cluster/history?window=1h&max_bytes=4096")
    assert status == 200, folded
    assert folded["counts"]["hosts"] < plain["counts"]["hosts"], \
        "the budget did not actually reduce anything"
    step = folded["resolution_s"]
    assert step == 60 * folded["fold"]
    served = sorted(r["ts"] for r in folded["hosts"])
    for label, stored in (("newest", max(r["ts"] for r in plain["hosts"])),
                          ("oldest", min(r["ts"] for r in plain["hosts"]))):
        assert served[0] <= stored < served[-1] + step, (
            f"the fold dropped the {label} bucket: {stored} is not covered by "
            f"{served[0]}..{served[-1] + step}")


def test_a_folded_peak_is_the_true_peak_not_the_peak_of_averages(
        running, token, pinned_clock, filled_dir):
    """The one property the `*_max` columns exist for survives the fold.

    One 2 s reading inside minute 7 sits far above that minute's mean. Folded
    across five minutes, the served peak must still be that reading — the max of
    the STORED per-minute maxima, which is the max of the whole folded span. An
    implementation that averaged and then took the max of the averages would
    report a peak the fleet never reached, which is exactly how a thermal
    excursion gets hidden by the endpoint that was written to expose it.
    """
    want = max(r["cpu_temp_c_max"] for r in stored_hosts(filled_dir)
               if r["cpu_temp_c_max"] is not None)
    # The spike is asserted to be IN the stored data before it is asserted
    # to survive the fold: a fixture that lost it would make the next
    # assertion pass for the wrong reason.
    assert want == pytest.approx(SPIKE_C), \
        "the fixture's spike did not survive the recorder's own roll-up"
    for path in (f"/v1/cluster/history?window=1h&max_bytes=4096",
                 f"/v1/cluster/history?window=1h&max_bytes=4096"
                 f"&metrics=cpu_temp_c_max"):
        status, served = get(running, token, path)
        assert status == 200, served
        host_a = [r for r in served["hosts"] if r["host"] == HOST_A]
        assert host_a, served
        got = max(r["cpu_temp_c_max"] for r in host_a)
        assert got == pytest.approx(want), (
            f"{path}: folded peak {got} is not the stored peak {want} — the "
            "excursion was averaged away")
        mean = max((r.get("cpu_temp_c") or 0) for r in host_a)
        assert got > mean, "a peak equal to the mean proves nothing here"


def test_a_field_the_node_never_reported_folds_to_none_not_zero(
        running, token, pinned_clock, filled_dir):
    """`nil` is the recorder's "not reported", and a fold must not launder it.

    HOST_B never reports a GPU temperature. Folded, it must still be `None`: a
    `0` here draws a node that never answered as an idle one, which §5.6 names as
    the failure this payload exists to prevent.
    """
    assert all(r["gpu_temp_c"] is None for r in stored_hosts(filled_dir)
               if r["host"] == HOST_B), "fixture lost the None path"
    status, served = get(running, token,
                         "/v1/cluster/history?window=1h&max_bytes=4096")
    assert status == 200, served
    rows = [r for r in served["hosts"] if r["host"] == HOST_B]
    assert rows, served
    assert all(r["gpu_temp_c"] is None for r in rows), \
        "a fold turned 'not reported' into a number"


def test_a_fold_invents_no_row_for_a_span_that_held_nothing(
        running, token, pinned_clock):
    """A gap stays a gap: no coarse bucket is fabricated over the silent span.

    Truncation drops the tail; the other way to shrink a series is to fill the
    holes, and that is worse — it turns "nobody was answering" into a flat line.
    """
    gap_start = NOW - GAP_MINUTES * 60
    status, served = get(running, token,
                         "/v1/cluster/history?window=1h&max_bytes=4096")
    assert status == 200, served
    step = served["resolution_s"]
    for row in served["hosts"]:
        assert not (gap_start <= row["ts"] < NOW), (
            f"a row was served for the silent span at ts={row['ts']}, step {step}")


# ── the response has to say what it did ────────────────────────────────────

def test_the_folded_resolution_is_stated_honestly(running, token, pinned_clock):
    """`resolution_s` names the step SERVED; `native_resolution_s` the stored one.

    A chart that labelled 15-minute points as 1-minute data would not be wrong by
    accident — every axis, every "samples/min" figure and every gap rule downstream
    would inherit the lie. Both numbers are on the response so neither has to be
    inferred from a timestamp gap.
    """
    status, served = get(running, token,
                         "/v1/cluster/history?window=1h&max_bytes=4096")
    assert status == 200, served
    assert served["native_resolution_s"] == 60, "tier 1 stores 1-minute buckets"
    assert served["resolution_s"] == 60 * served["fold"]
    assert served["resolution"] == f"{served['resolution_s']}s"
    assert served["fold"] > 1
    assert "folded" in served["note"], served["note"]
    assert "folded" in served["speak"], served["speak"]


def test_a_budget_that_cannot_be_met_is_reported_not_obeyed_by_lying(
        running, token, pinned_clock):
    """Too small a budget is answered with the coarsest fold and `budget_met: false`.

    The alternative is cutting rows until the number fits. The contract forbids
    that, so the route has to be able to say "I could not fit it" — and say it as
    a code, not only in prose.
    """
    status, tiny = get(running, token,
                       "/v1/cluster/history?window=1h&max_bytes=64")
    assert status == 200, tiny
    assert tiny["budget_met"] is False
    assert tiny["fold"] == max(mh.fold_factors(60))
    # Too small a budget is met with the COARSEST fold, not with fewer rows:
    # the span still runs the whole way, so the series is complete and large
    # rather than truncated and small.
    step = tiny["resolution_s"]
    assert step == 60 * tiny["fold"]
    status, full = get(running, token, "/v1/cluster/history?window=1h")
    served = sorted(r["ts"] for r in tiny["hosts"])
    newest = max(r["ts"] for r in full["hosts"])
    oldest = min(r["ts"] for r in full["hosts"])
    assert served[0] <= oldest and newest < served[-1] + step, (
        "an unmet budget truncated the series instead of coarsening it")
    assert "over the requested budget" in tiny["note"], tiny["note"]
    assert len(json.dumps(tiny).encode()) > 64


# ── parameter handling ─────────────────────────────────────────────────────

def test_a_budget_the_stored_resolution_already_fits_is_served_unfolded(
        running, token, pinned_clock):
    """Enough budget must buy the stored data, not a coarser answer.

    The fold ladder is only honest if it stops at the finest rung that fits: a
    route that always answered at the coarsest step would pass every "was the
    fold stated" test above while quietly throwing resolution away on requests
    that never needed it. So ask for a budget the stored answer already fits and
    demand `fold == 1` with the stored rows verbatim — and note the budget grows
    monotonically with resolution, which is the same property seen from the other
    side.

    The +128 is not slack invented to make the assertion pass: echoing
    `max_bytes` as an integer is a handful of bytes longer than the `null` the
    unbudgeted body carries, so a budget of exactly the measured size would be
    spent by the echo, not by a data row.
    """
    status, plain = get(running, token, "/v1/cluster/history?window=1h")
    size = len(json.dumps(plain).encode("utf-8"))
    status, roomy = get(running, token,
                        f"/v1/cluster/history?window=1h&max_bytes={size + 128}")
    assert status == 200, roomy
    assert roomy["fold"] == 1, "a budget the stored answer fits bought a fold"
    assert roomy["budget_met"] is True
    assert roomy["hosts"] == plain["hosts"]

    status, tight = get(running, token,
                        f"/v1/cluster/history?window=1h&max_bytes=4096")
    assert status == 200, tight
    assert tight["fold"] > roomy["fold"], (
        f"a {size + 128} B budget bought fold {roomy['fold']} and a 4096 B one "
        f"bought {tight['fold']} — resolution is not bought with budget")


def test_max_bytes_is_refused_on_the_2s_tier(running, token, pinned_clock):
    """Tier 0 is the 2 s ring: already bounded, and folding raw samples would
    mint the `*_max` columns tier 0 does not store."""
    status, body = get(running, token,
                       "/v1/cluster/history?window=15m&max_bytes=4096")
    assert status == 400, (status, body)
    assert body["error"]["code"] == "bad_request"
    assert "max_bytes is not supported on a 15 m window" in \
        body["error"]["message"], body


@pytest.mark.parametrize("raw,needle", [
    ("abc", "max_bytes must be a whole number of bytes"),
    ("0", "max_bytes=0 is not a budget"),
    ("-5", "max_bytes=-5 is not a budget"),
])
def test_a_bad_budget_is_a_400_naming_the_rule(running, token, pinned_clock,
                                               raw, needle):
    status, body = get(running, token,
                       f"/v1/cluster/history?window=1h&max_bytes={raw}")
    assert status == 400, (raw, status, body)
    assert needle in body["error"]["message"], body


def test_the_fold_projects_requested_metrics_after_folding(running, token,
                                                           pinned_clock,
                                                           filled_dir):
    """Fold THEN project — the order is a correctness rule, not a style choice.

    `_fold_hosts` falls back to the plain average when a `<k>_max` column is
    absent from the rows it is handed. So projecting first and folding second
    would report the average AS the peak, silently, on exactly the requests that
    ask for a peak. The same peak must therefore survive a projected folded read
    as an unprojected one.
    """
    status, plain = get(running, token,
                        "/v1/cluster/history?window=1h&max_bytes=4096")
    status, proj = get(running, token,
                       "/v1/cluster/history?window=1h&max_bytes=4096"
                       "&metrics=cpu_temp_c,cpu_temp_c_max")
    assert status == 200, (plain, proj)
    # A projected body is cheaper, so it may afford a finer fold — that is the
    # budget working, not drifting. What must be invariant is what a peak MEANS:
    # each served row's `cpu_temp_c_max` is the maximum of the STORED maxima in
    # the span it covers. Project-first would put that row's average there.
    assert proj["fold"] <= plain["fold"]
    stored = [r for r in stored_hosts(filled_dir) if r["host"] == HOST_A]
    step = proj["resolution_s"]
    checked = 0
    for row in proj["hosts"]:
        if row["host"] != HOST_A:
            continue
        want_keys = ({k for k in (stored[0].keys()
                                  & set(routes_cluster._HISTORY_HOST_IDENTITY))}
                     | {"cpu_temp_c", "cpu_temp_c_max"})
        assert set(row) == want_keys, (sorted(row), sorted(want_keys))
        want = [r["cpu_temp_c_max"] for r in stored
                if row["ts"] <= r["ts"] < row["ts"] + step
                and r["cpu_temp_c_max"] is not None]
        assert want, f"no stored bucket under served ts={row['ts']}"
        assert row["cpu_temp_c_max"] == max(want), (
            f"ts={row['ts']}: served peak {row['cpu_temp_c_max']} is not the "
            f"stored peak {max(want)} of the folded span — the fold ran after "
            "the projection and reported an average as a peak")
        checked += 1
    assert checked >= 2, f"only {checked} row(s) compared; the fixture is too thin"
