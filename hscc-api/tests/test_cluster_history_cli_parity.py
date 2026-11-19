"""Parity: `hscc cluster history` prints EXACTLY what GET /v1/cluster/history serves.

Why this file exists: the CLI verb and the route are two surfaces on one read,
and the moment they disagree an operator reading the terminal is looking at
different numbers than the widget shows. So one fixture state drives both —
the same ``hscc_dir``, the same pinned clock — and the comparison is the
recorder's own read API on one side and the route's JSON on the other.

The card's parity idiom is the one ``test_cluster_history_parity.py`` already
uses for this route: build the fixture with the recorder's OWN write path
(:meth:`metrics_history.MetricsRecorder.tick`, which rolls tier 0 -> 1 -> 2),
then compare against the recorder's OWN read API. Nothing here re-derives a
mean, a peak or a window edge.

TWO AXES, ONE OF THEM PENDING. t_447cc7fc ruled ``/v1/cluster/history``
canonical and the CLI mirrors its parameters, but the route answers only
``?window=`` until t_b611b56e folds ``tier``/``since``/``until`` into it. The
window axis is compared here in full today; the tier axis is a `skip` whose
reason names t_b611b56e, and it is written to PROBE the route and start
running on its own the moment that fold lands — a leg that quietly disappears
is how a parity promise dies, and a leg nobody has to re-enable is how it
survives.

Fixtures are TEST-NET-1 addresses (192.0.2.x) and one placeholder tailnet
address; no real host, tenant or token is read.
"""

import ast
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
from hscc_daemon import metrics_history_cli as mhc     # noqa: E402

HOST_A = "192.0.2.10"                                   # TEST-NET-1, never a node
HOST_B = "192.0.2.11"
TAILNET = "100.64.0.1"                                  # placeholder, doc example only
PORT = "8000"
MODEL = "org/Qwen-Test"

#: aligned to 2 s, 1 min and 5 min so every bucket boundary in the fixture is a
#: real one; the route's clock and the CLI's are both pinned to this instant.
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
    """HOST_A answers everything; HOST_B never reports GPU temp or power — the
    ``None`` path must survive both surfaces unchanged."""
    return {"hosts": [
        {"host": HOST_A, "error": None,
         "sample": sample(ts, gpu_power_w=str(100.0 + (ts % 60)))},
        {"host": HOST_B, "error": None,
         "sample": sample(ts, host=HOST_B, gpu_temp_c="", gpu_power_w="",
                          gpu_util_pct="0")}]}


def serving_rows(ts):
    return [{"node": HOST_A, "port": PORT, "model": MODEL,
             "running": 1 + int(ts % 3), "waiting": 0, "kv": 0.42,
             "gen_tps": "118.5", "prompt_tps": "900"},
            {"node": HOST_B, "port": "8001", "model": None,
             "error": "backend unreachable"}]


@pytest.fixture
def store_dir(tmp_path):
    """The recorder's own write path, into a directory BOTH surfaces read."""
    rec = mh.MetricsRecorder(hscc_dir=str(tmp_path))
    for i in range(TICKS):
        ts = NOW - (TICKS * mh.TIER0_INTERVAL_S) + i * mh.TIER0_INTERVAL_S
        rec.tick(monitor_frame(ts), serving_rows(ts), ts)
    rec.tick(monitor_frame(NOW), serving_rows(NOW), NOW)
    assert rec.store.count(1) > 0, "fixture produced no tier-1 rows"
    assert rec.store.count(2) > 0, "fixture produced no tier-2 rows"
    rec.store.close()
    return tmp_path


@pytest.fixture
def running(store_dir):
    srv = RunningServer(hscc_dir=str(store_dir))
    yield srv
    srv.close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


@pytest.fixture
def pinned_clock(running):
    """Pin the route's clock, and use the same instant as the CLI's `now`, so
    the two window edges cannot drift apart by a tick between the calls."""
    original = routes_cluster._history_now
    routes_cluster._history_now = lambda: float(NOW)
    yield NOW
    routes_cluster._history_now = original


def get(running, token, path):
    return running.request(token=token, path=path)


def cli(argv, store_dir, now=NOW):
    """The CLI's own read, in process, on the fixture store and pinned clock."""
    opts, err = mhc.parse_history_args(argv)
    assert err is None, f"the CLI refused its own documented flags: {err}"
    payload = mhc.read_history(opts, hscc_dir=str(store_dir), now=now)
    # The human table must show what the JSON says, or `--json` is a second
    # answer rather than a second rendering of one answer.
    assert json.loads(mhc.history_json(payload)) == payload
    return payload


# ── the window axis: the route's rows, the CLI's rows, the recorder's rows ──

@pytest.mark.parametrize("window,tier,resolution", [
    ("1h", 1, "60s"),
    ("6h", 1, "60s"),
    ("24h", 1, "60s"),
    ("7d", 2, "300s"),
])
def test_the_cli_and_the_route_agree_on_every_window(
        running, token, pinned_clock, store_dir, window, tier, resolution):
    """One fixture, two surfaces, one set of rows — plus the recorder as the
    oracle underneath both, so a shared mistake cannot agree with itself."""
    status, api = get(running, token, f"/v1/cluster/history?window={window}")
    assert status == 200, api
    cli_payload = cli(["--window", window], store_dir)

    assert cli_payload["hosts"] == api["hosts"], "the CLI's node rows drifted"
    assert cli_payload["engines"] == api["engines"], "the CLI's engine rows drifted"
    assert cli_payload["counts"] == api["counts"]
    for key in ("window", "window_s", "tier", "resolution", "resolution_s",
                "tier_summary", "max_fields", "since", "until", "node",
                "metrics", "hosts_included", "engines_included"):
        assert cli_payload[key] == api[key], (
            f"{key}: CLI says {cli_payload[key]!r}, the route says "
            f"{api[key]!r} — the two surfaces now describe the same read "
            "differently")
    assert api["tier"] == tier and api["resolution"] == resolution

    store = mh.MetricsStore(hscc_dir=str(store_dir))
    try:
        want_hosts = store.host_rows(tier, since=api["since"],
                                    until=api["until"], host=None)
        want_engines = store.engine_rows(tier, since=api["since"],
                                        until=api["until"], node=None)
    finally:
        store.close()
    assert api["hosts"] == want_hosts and cli_payload["hosts"] == want_hosts
    assert api["engines"] == want_engines and cli_payload["engines"] == want_engines
    assert want_hosts, "a window that served nothing cannot prove parity"


def test_the_two_surfaces_expose_the_same_vocabulary(running, token,
                                                    pinned_clock, store_dir):
    """The payload keys themselves. `speak` is the route's voice and has no CLI
    counterpart; anything ELSE one surface has and the other lacks is a real
    divergence — and when the route grows a key (t_b611b56e), the CLI has to
    follow it, which is exactly what this assertion is for."""
    status, api = get(running, token, "/v1/cluster/history?window=6h")
    assert status == 200, api
    cli_payload = cli(["--window", "6h"], store_dir)
    assert set(api) - set(cli_payload) == {"speak"}, (
        f"the route answers keys the CLI has no equivalent for: "
        f"{sorted(set(api) - set(cli_payload) - {'speak'})}")
    assert set(cli_payload) - set(api) == set(), (
        f"the CLI invents keys the route does not have: "
        f"{sorted(set(cli_payload) - set(api))}")


def test_one_machine_filter_narrows_both_surfaces_alike(
        running, token, pinned_clock, store_dir):
    """`--node` is the one machine filter on both surfaces; there is no second
    flag for a second machine."""
    status, api = get(running, token,
                      f"/v1/cluster/history?window=24h&node={HOST_A}")
    assert status == 200, api
    cli_payload = cli(["--window", "24h", "--node", HOST_A], store_dir)
    assert cli_payload["node"] == api["node"] == HOST_A
    assert cli_payload["hosts"] == api["hosts"]
    assert cli_payload["engines"] == api["engines"]
    assert api["hosts"] and {r["host"] for r in api["hosts"]} == {HOST_A}
    assert {r["node"] for r in api["engines"]} == {HOST_A}


def test_the_projection_matches_field_for_field(running, token, pinned_clock,
                                               store_dir):
    """`--metrics` is not a display filter: the same fields survive, and the
    row's identity keys ride along on both surfaces."""
    path = "/v1/cluster/history?window=1h&metrics=gpu_util_pct,gpu_temp_c"
    status, api = get(running, token, path)
    assert status == 200, api
    cli_payload = cli(["--window", "1h", "--metrics", "gpu_util_pct,gpu_temp_c"],
                      store_dir)
    assert cli_payload["hosts"] == api["hosts"]
    assert cli_payload["engines"] == api["engines"]
    assert cli_payload["max_fields"] == api["max_fields"] == []
    for row in api["hosts"]:
        assert {"tier", "ts", "host"} <= set(row), "identity was projected away"
        assert set(row) <= set(mhc.HOST_IDENTITY) | {"gpu_util_pct",
                                                     "gpu_temp_c"}


def test_a_filter_naming_no_engine_field_excludes_the_series_on_both(
        running, token, pinned_clock, store_dir):
    status, api = get(running, token,
                      "/v1/cluster/history?window=1h&metrics=gpu_util_pct")
    assert status == 200, api
    cli_payload = cli(["--window", "1h", "--metrics", "gpu_util_pct"],
                      store_dir)
    assert api["hosts_included"] is cli_payload["hosts_included"] is True
    assert api["engines_included"] is cli_payload["engines_included"] is False
    assert api["engines"] == cli_payload["engines"] == []
    assert api["hosts"] == cli_payload["hosts"] and api["hosts"]


# ── the two emptiness cases, stated the same way by both surfaces ───────────

def test_window_15m_says_the_ring_is_not_here_on_both_surfaces(
        running, token, pinned_clock, store_dir):
    """Neither surface may answer "no 2 s points" in a way that reads as "the
    fleet was idle for 15 minutes"."""
    status, api = get(running, token, "/v1/cluster/history?window=15m")
    assert status == 200, api
    assert api["tier"] == 0
    assert api["store"]["tier0_resident"] is False
    assert api["hosts"] == [] and api["engines"] == []
    cli_payload = cli(["--window", "15m"], store_dir)
    assert cli_payload["store"]["tier0_resident"] is False
    assert cli_payload["hosts"] == [] and cli_payload["engines"] == []
    assert "ring" in cli_payload["note"] and "daemon" in cli_payload["note"], (
        "the CLI's tier-0 answer must say WHERE the ring is, as the route's does")


def test_a_store_that_does_not_exist_stays_missing_on_both(
        running, token, pinned_clock, tmp_path):
    fresh = tmp_path / "never-written"
    fresh.mkdir()
    srv = RunningServer(hscc_dir=str(fresh))
    try:
        tok = api_server.load_token(srv.server.ctx.hscc_dir)
        status, api = srv.request(token=tok, path="/v1/cluster/history?window=1h")
        assert status == 200, api
        assert api["store"]["exists"] is False
        assert "not written its store" in api["note"]
        cli_payload = cli(["--window", "1h"], fresh)
        assert cli_payload["store"]["exists"] is False
        assert cli_payload["note"] == api["note"]
        assert not Path(mh.db_path(str(fresh))).exists(), \
            "a read on one surface created the store the other was only looking at"
    finally:
        srv.close()


# ── the tier axis: pending t_b611b56e, and written to wake up by itself ─────

def test_the_tier_axis_matches_the_route_once_the_route_answers_it(
        running, token, pinned_clock, store_dir):
    """`--tier`/`--since`/`--until` parity.

    The canonical route takes only ``?window=`` until t_b611b56e folds the
    recorder's second axis into it, so today there is nothing on the route side
    to compare against. This leg PROBES the route rather than being commented
    out or deleted: if the route still refuses the parameters the leg skips,
    naming the PR that unblocks it, and the day that PR lands it starts running
    with no edit to this file. A parity leg that has to be remembered is a
    parity leg that gets forgotten.
    """
    since = NOW - 3 * mh.TIER1_BUCKET_S
    status, probe = get(running, token,
                        f"/v1/cluster/history?tier=1&since={since}&until={NOW}")
    if status == 400 and "not supported" in json.dumps(probe):
        pytest.skip(
            "t_b611b56e has not folded tier/since/until into "
            "/v1/cluster/history yet: the route answers only ?window=, so the "
            "CLI's --tier/--since/--until have no route-side counterpart to "
            "compare against. This leg activates itself when that PR lands.")
    assert status == 200, probe
    cli_payload = cli(["--tier", "1", "--since", str(since),
                       "--until", str(NOW)], store_dir)
    assert probe["hosts"] == cli_payload["hosts"], (
        "the tier-axis rows now differ between the route and the CLI")
    assert probe["engines"] == cli_payload["engines"]
    assert cli_payload["since"] == probe["since"]
    assert cli_payload["until"] == probe["until"]


# ── the CLI must be a reader, not a second sampler or an HTTP client ────────

def test_the_cli_module_touches_no_transport_and_no_sampler():
    """A read command that reached the fleet, or reconstructed the sampler, is
    the bug this whole surface is built to avoid: `get_sampler()` CONSTRUCTS a
    Sampler, which spawns a second `sparkrun cluster monitor` and a second
    writer on the roll-up watermarks, and going over HTTP would make the CLI a
    client of the route instead of a second surface on the same read.

    Swept from the SYNTAX TREE, not the prose: this module's own docstring
    names sparkrun and ssh to explain what it must never do, and a substring
    ban would either fail on that sentence or be loosened until it caught
    nothing. An import or a call is the thing that can actually shell out.
    """
    src = (_REPO_ROOT / "hscc_daemon" / "metrics_history_cli.py").read_text()
    tree = ast.parse(src)

    banned_modules = {"subprocess", "socket", "http", "http.client", "urllib",
                      "urllib.request", "api_server", "routes_cluster"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in banned_modules, (
                    f"the history CLI imports {alias.name} — it reads the "
                    "recorder's store, nothing live")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in banned_modules, (
                f"the history CLI imports from {node.module}")

    calls = {node.func.id for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    attrs = {node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    forbidden = {"get_sampler", "urlopen", "run_cmd", "check_output", "run",
                 "Popen", "system", "check_call", "spawn"}
    hit = (calls | attrs) & forbidden
    assert not hit, f"the history CLI reached a transport or sampler seam: {hit}"

    # And the read API it is required to use is really in there — a guard that
    # only forbids things passes just as happily when someone deletes the read.
    assert "host_rows" in src and "engine_rows" in src and "tier0_points" in src
    assert "resident_recorder" in src, "tier 0 must be read, not assumed present"
