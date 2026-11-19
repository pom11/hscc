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


# ── the codes a client branches on: ONE closed set, not two vocabularies ─────

#: t_447cc7fc ruled these strings, so they are a contract and not a local
#: spelling: a client branches on `source` (and on `axis`) and reads `note` for
#: the sentence. Each surface defines them as its OWN module constants — copied
#: verbatim, because the daemon leg runs without the API package, the same
#: copy-not-import rule as the node-name rule above.
#:
#: The VALUES are pinned here as well as compared across the two modules, and
#: that is what this section is for: `test_the_two_surfaces_expose_the_same_
#: vocabulary` above is key for key, so rewording a value passes it — both
#: payloads still carry `source`, and a script comparing "sqlite_store_absent"
#: quietly takes the store-is-there branch for every answer that came from the
#: other surface. Pinning the ruled literals also closes the pair-drift: a code
#: reworded on BOTH surfaces still fails here, because changing a wire code is a
#: breaking change for every client that branches on it and has to be a decision
#: made in this file, not a reword in one module's constants.
SOURCE_CODES = {
    "SOURCE_RING": "recorder_ring",
    "SOURCE_RING_ABSENT": "recorder_ring_absent",
    "SOURCE_STORE": "sqlite_store",
    "SOURCE_STORE_ABSENT": "sqlite_store_absent",
}

#: The other code a client branches on, copied across the same seam and with
#: the same hole: `axis` says which axis answered, and on the tier axis
#: `window` is null, so a reader that inferred it from the keys would infer a
#: preset name that was never sent.
AXIS_CODES = {"AXIS_WINDOW": "window", "AXIS_TIER": "tier"}


def _surface_codes(module, group, ruled):
    """Every ``SOURCE_*`` / ``AXIS_*`` constant one surface defines.

    A vanished or an extra constant NAME is a failure of its own rather than an
    AttributeError: the name is part of the copy rule (it is how a reader finds
    the code next to its meaning), and a fifth code on one surface only is a
    code some client can receive and no client can name.
    """
    prefix = group.upper() + "_"
    defined = {name: value for name, value in vars(module).items()
               if name.startswith(prefix) and isinstance(value, str)}
    assert set(defined) == set(ruled), (
        f"{module.__name__}'s {group} codes are {sorted(defined)}, the ruled "
        f"set is {sorted(ruled)} — one surface states a code the other cannot "
        "name, or lost one a client still branches on")
    return defined


def _same_code_on_both_surfaces(state, api, cli_payload, want):
    """Both surfaces name `state` with the same ruled code.

    The three failures this rules out are three different bugs: a surface that
    stopped stating the key at all, a surface inventing a code, and the pair
    disagreeing — the last one invisible to a payload comparison, because two
    payloads can share every key and mean different things by their values.
    """
    for label, payload in (("the route", api), ("the CLI", cli_payload)):
        assert "source" in payload, (
            f"{label} no longer states `source` for {state}: a client that "
            "branches on the code is left reading a key it never sees, and "
            "every other key of the payload is unchanged so nobody notices")
        assert payload["source"] in set(SOURCE_CODES.values()), (
            f"{label} answered {state} with {payload['source']!r}, which is "
            f"not one of the ruled codes {sorted(set(SOURCE_CODES.values()))} "
            "— a client can only branch on a code it can name")
    assert api["source"] == cli_payload["source"], (
        f"for {state} the route says {api['source']!r} and "
        f"`hscc cluster history` says {cli_payload['source']!r}. One read, two "
        "surfaces, two answers: a script that branches on the route's code "
        "mis-files every read it took from the CLI, and neither payload says "
        "so")
    assert api["source"] == want, (
        f"{state} answered {api['source']!r} on both surfaces, not the ruled "
        f"{want!r} — the state and the code for it have come apart")


@pytest.mark.parametrize("group,ruled", [
    pytest.param("source", SOURCE_CODES, id="source"),
    pytest.param("axis", AXIS_CODES, id="axis")])
def test_the_two_surfaces_share_one_closed_set_of_codes(group, ruled):
    """Same constant names, same value per name, same closed set — route and CLI.

    Three assertions, for the three drifts that are actually possible: a NAME
    vanishes on one surface (the copy rule broke), a VALUE moves on one surface
    (clients branch wrong for every read taken from there), or the set moves on
    BOTH (the ruling was reworded without being re-made). The key-for-key
    vocabulary test above cannot see any of them: it compares payloads, and a
    payload with a reworded value carries the same keys.
    """
    route_codes = _surface_codes(routes_cluster, group, ruled)
    cli_codes = _surface_codes(mhc, group, ruled)
    for name, ruled_value in ruled.items():
        assert route_codes[name] == cli_codes[name], (
            f"{name} is {route_codes[name]!r} on the route and "
            f"{cli_codes[name]!r} in `hscc cluster history`. The payloads "
            "still match key for key, so nothing else notices: a client "
            f"comparing {route_codes[name]!r} takes the wrong branch for every "
            f"{group} answer that came from the CLI")
        assert route_codes[name] == ruled_value, (
            f"the route's {name} is {route_codes[name]!r}, not the ruled "
            f"{ruled_value!r} (t_447cc7fc). A wire code is what clients branch "
            "on: rewording it breaks them all, so it is a decision recorded "
            "here, not a reword inside one module")
        assert cli_codes[name] == ruled_value, (
            f"the CLI's {name} is {cli_codes[name]!r}, not the ruled "
            f"{ruled_value!r} (t_447cc7fc). The route's clients branch on "
            f"{ruled_value!r}; the CLI's own output would name the same state "
            "by a code no client knows")


def test_a_code_never_means_two_different_things():
    """`window` is the axis and nothing else, on either surface.

    Two vocabularies copied across two modules can collide without either
    payload looking wrong, and a code that means an axis on one read and a
    source on the next cannot be branched on at all.
    """
    for module in (routes_cluster, mhc):
        source = set(_surface_codes(module, "source", SOURCE_CODES).values())
        axis = set(_surface_codes(module, "axis", AXIS_CODES).values())
        assert not (source & axis), (
            f"{module.__name__} uses {sorted(source & axis)} as both a "
            "`source` and an `axis` code")


def test_both_surfaces_answer_with_the_same_code_for_the_same_state(
        running, token, pinned_clock, store_dir, monkeypatch, tmp_path):
    """The constants agree; these are the four states actually going on the wire.

    A surface is free to write a literal where its constant sits, and a payload
    comparison is as blind to that as it is to a reworded constant — so each
    state is asked of both surfaces and both answers must be the same ruled
    code. Three of the four come out of the one fixture; the fourth needs a
    directory nobody has ever written, which needs its own server.
    """
    # ── the store answered (the window axis, and the tier axis below) ───────
    status, api = get(running, token, "/v1/cluster/history?window=1h")
    assert status == 200, api
    window_cli = cli(["--window", "1h"], store_dir)
    _same_code_on_both_surfaces("a 1 h window served from the store", api,
                                window_cli, SOURCE_CODES["SOURCE_STORE"])
    assert api["axis"] == window_cli["axis"] == AXIS_CODES["AXIS_WINDOW"], (
        "the window axis was named differently by the two surfaces")

    # ── tier 0 with no ring here: the deployed case on both surfaces ────────
    status, api = get(running, token, "/v1/cluster/history?window=15m")
    assert status == 200, api
    _same_code_on_both_surfaces(
        "a tier-0 window with no ring in this process", api,
        cli(["--window", "15m"], store_dir), SOURCE_CODES["SOURCE_RING_ABSENT"])

    # ── the store is not there at all ───────────────────────────────────────
    missing = tmp_path / "never-written"
    missing.mkdir()
    srv = RunningServer(hscc_dir=str(missing))
    try:
        tok = api_server.load_token(srv.server.ctx.hscc_dir)
        status, api = srv.request(token=tok,
                                  path="/v1/cluster/history?window=1h")
        assert status == 200, api
        _same_code_on_both_surfaces(
            "a store that has never been written", api,
            cli(["--window", "1h"], missing),
            SOURCE_CODES["SOURCE_STORE_ABSENT"])
    finally:
        srv.close()

    # ── tier 0 WITH the ring resident: the one state no cross-surface test
    # had ever compared, because neither surface owns a ring in a test process
    # unless one is put there. Both surfaces ask exactly one question about
    # residency — `metrics_history.resident_recorder()` — so one recorder in
    # this process drives both; whether a process is ALLOWED to claim residency
    # is pinned by test_cluster_history_parity.py's `ring` fixture.
    ring = mh.MetricsRecorder(hscc_dir=str(store_dir))
    for i in range(3):
        ts = NOW - 4 + i * 2
        ring.tick(monitor_frame(ts), serving_rows(ts), ts)
    monkeypatch.setattr(mh, "resident_recorder", lambda: ring)
    status, api = get(running, token, "/v1/cluster/history?tier=0")
    assert status == 200, api
    ring_cli = cli(["--tier", "0"], store_dir)
    _same_code_on_both_surfaces("a tier-0 read with the ring resident", api,
                                ring_cli, SOURCE_CODES["SOURCE_RING"])
    assert api["axis"] == ring_cli["axis"] == AXIS_CODES["AXIS_TIER"], (
        "the tier axis was named differently by the two surfaces")
    assert api["hosts"] == ring_cli["hosts"] and api["hosts"], (
        "the resident ring is transposed into rows differently by the route "
        "and the CLI — the code agrees, the data it describes does not")
    assert api["engines"] == ring_cli["engines"] and api["engines"], (
        "the resident ring's engine rows differ between the two surfaces")


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
