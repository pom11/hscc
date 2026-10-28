"""`hscc cluster monitor --live`: parsers, the serving poller and the renderers.

Everything network-facing is faked; nothing here touches the fleet.
"""

import io
import json
import urllib.error
import urllib.request

import pytest
from rich.console import Console

from hscc_daemon import cli_theme
from hscc_daemon import cluster_live as cl
from hscc_daemon import hscc as unified

MODEL = "org/Model-A"

METRICS = f"""# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{{engine="0",model_name="{MODEL}"}} 3.0
vllm:num_requests_waiting{{engine="0",model_name="{MODEL}"}} 1.0
vllm:kv_cache_usage_perc{{engine="0",model_name="{MODEL}"}} 0.5
vllm:generation_tokens_total{{engine="0",model_name="{MODEL}"}} 1000.0
vllm:prompt_tokens_total{{engine="0",model_name="{MODEL}"}} 5000.0
vllm:num_preemptions_total{{engine="0",model_name="{MODEL}"}} 2.0
"""

MODEL_INFO = {"data": [
    {"model_name": MODEL, "litellm_params": {"api_base": "http://10.0.0.1:8000/v1"}},
    {"model_name": "worker-model", "litellm_params": {"api_base": "http://10.0.0.1:8000/v1"}},
    {"model_name": "worker-model", "litellm_params": {"api_base": "http://10.0.0.2:8001/v1"}},
]}


def _render(renderable):
    buf = io.StringIO()
    Console(file=buf, width=200, color_system=None,
            theme=cli_theme.theme_for("dark")).print(renderable)
    return buf.getvalue()


# ── parsing ────────────────────────────────────────────────────────────────

def test_parse_vllm_metrics_reads_load_gauges_and_counters():
    m = cl.parse_vllm_metrics(METRICS)[MODEL]
    assert m == {"running": 3.0, "waiting": 1.0, "kv": 0.5,
                 "gen_total": 1000.0, "prompt_total": 5000.0}


def test_parse_vllm_metrics_sums_engines_and_averages_kv():
    text = (f'vllm:num_requests_running{{engine="0",model_name="{MODEL}"}} 2\n'
            f'vllm:num_requests_running{{engine="1",model_name="{MODEL}"}} 3\n'
            f'vllm:gpu_cache_usage_perc{{engine="0",model_name="{MODEL}"}} 0.2\n'
            f'vllm:gpu_cache_usage_perc{{engine="1",model_name="{MODEL}"}} 0.4\n')
    m = cl.parse_vllm_metrics(text)[MODEL]
    assert m["running"] == 5
    assert abs(m["kv"] - 0.3) < 1e-9  # older vLLM gauge name, averaged


def test_parse_vllm_metrics_ignores_garbage():
    assert cl.parse_vllm_metrics("not prometheus\nvllm:num_requests_running{} nan?\n") == {}


def test_parse_model_info_groups_aliases_by_backend():
    assert cl.parse_model_info(MODEL_INFO) == {
        "10.0.0.1:8000": [MODEL, "worker-model"],
        "10.0.0.2:8001": ["worker-model"],
    }


def test_hostport():
    assert cl.hostport("http://10.0.0.3:8002/v1") == "10.0.0.3:8002"
    assert cl.hostport("https://example.test/v1") == "example.test:443"
    assert cl.hostport("") == ""


def test_rates_need_two_samples_and_survive_restart():
    cur = {"gen_total": 1100.0, "prompt_total": 5400.0}
    assert cl.rates(None, cur, 2) == (None, None)
    assert cl.rates({"gen_total": 1000.0, "prompt_total": 5000.0}, cur, 2) == (50.0, 200.0)
    # counters went backwards = engine restarted: no rate, not a negative one
    assert cl.rates({"gen_total": 9000.0, "prompt_total": 5000.0}, cur, 2) == (None, 200.0)


# ── poller ─────────────────────────────────────────────────────────────────

class FakeNet:
    def __init__(self):
        self.metrics = {"10.0.0.1:8000": METRICS, "10.0.0.2:8001": METRICS}
        self.proxy_ok = True
        self.calls = []

    def get(self, url, timeout):
        self.calls.append(url)
        if url.endswith("/v1/model/info"):
            if not self.proxy_ok:
                raise OSError("connection refused")
            return json.dumps(MODEL_INFO).encode()
        hp = url.split("/")[2]
        body = self.metrics.get(hp)
        if body is None:
            raise OSError("timed out")
        return body.encode()


def _poller(net, clock):
    return cl.ServingPoller(get=net.get, clock=lambda: clock[0])


def test_poller_rows_per_backend_and_rates_on_second_tick():
    net, clock = FakeNet(), [100.0]
    p = _poller(net, clock)
    rows = p.poll()
    assert [(r["node"], r["port"], r["running"], r["waiting"]) for r in rows] == [
        ("10.0.0.1", "8000", 3, 1), ("10.0.0.2", "8001", 3, 1)]
    assert rows[0]["routes"] == [MODEL, "worker-model"]
    assert rows[0]["gen_tps"] is None  # first sample has no rate yet

    net.metrics["10.0.0.1:8000"] = METRICS.replace("1000.0", "1020.0")
    clock[0] = 102.0
    rows = p.poll()
    assert rows[0]["gen_tps"] == 10.0
    assert rows[1]["gen_tps"] == 0.0
    p.close()


def test_poller_marks_a_dead_backend_instead_of_dropping_it():
    net, clock = FakeNet(), [0.0]
    net.metrics.pop("10.0.0.2:8001")
    rows = _poller(net, clock).poll()
    dead = [r for r in rows if r.get("error")]
    assert len(dead) == 1 and dead[0]["node"] == "10.0.0.2"


def test_poller_keeps_last_routes_when_proxy_blips_and_caches_them():
    net, clock = FakeNet(), [0.0]
    p = _poller(net, clock)
    p.poll()
    clock[0] = cl.BACKENDS_REFRESH_S + 1
    net.proxy_ok = False
    rows = p.poll()
    assert p.proxy_error and len(rows) == 2
    # within the refresh window the proxy is not asked again
    n = sum(u.endswith("/v1/model/info") for u in net.calls)
    clock[0] += 1
    p.poll()
    assert sum(u.endswith("/v1/model/info") for u in net.calls) == n


# ── rendering ──────────────────────────────────────────────────────────────

FRAME = {"hosts": [
    {"host": "10.0.0.1", "error": None, "sample": {
        "sparkrun_jobs": "1", "cpu_usage_pct": "6.2", "mem_used_pct": "85.1",
        "swap_total_mb": "16383", "swap_used_mb": "0", "gpu_util_pct": "94",
        "cpu_temp_c": "87.2", "gpu_temp_c": "71", "gpu_power_w": "37.88"},
     "workloads": [{"containers": [{"name": "should-not-render"}]}]},
    {"host": "10.0.0.2", "error": "ssh: connection refused", "sample": None},
]}


def test_host_table_matches_sparkrun_columns_without_containers():
    out = _render(cl.host_table(FRAME))
    for col in ("Host", "Jobs", "CPU%", "RAM%", "Swap%", "GPU%", "CPU Temp",
                "GPU Temp", "GPU Power"):
        assert col in out
    assert "37.88 W" in out and "87.2 C" in out and "0%" in out
    assert "should-not-render" not in out
    assert "connection refused" in out


def test_serving_table_shows_load_per_node_and_model_totals():
    rows = [
        {"node": "10.0.0.1", "port": "8000", "model": MODEL, "routes": [MODEL, "worker-model"],
         "running": 3, "waiting": 1, "kv": 0.5, "gen_tps": 10.0, "prompt_tps": 200.0},
        {"node": "10.0.0.2", "port": "8001", "model": MODEL, "routes": ["worker-model"],
         "running": 2, "waiting": 0, "kv": 0.25, "gen_tps": 5.0, "prompt_tps": None},
        {"node": "10.0.0.3", "port": "8002", "model": "-", "routes": ["worker-model"],
         "error": "timed out"},
    ]
    out = _render(cl.serving_table(rows))
    assert "Model-A" in out and "org/Model-A" not in out  # org prefix dropped
    assert "worker" in out and "worker-model" not in out  # -model suffix dropped
    assert "50.0" in out and "Prompt t/s" in out and "Routes" in out
    total = next(line for line in out.splitlines() if line.startswith("total"))
    assert "total (2)" in total and " 5 " in total and "15.0" in total
    assert "down" in out and "timed out" in out


def test_serving_table_narrow_drops_routes_and_prompt_rate():
    rows = [{"node": "10.0.0.1", "port": "8000", "model": MODEL, "routes": ["worker-model"],
             "running": 1, "waiting": 0, "kv": 0.1, "gen_tps": None, "prompt_tps": None}]
    out = _render(cl.serving_table(rows, width=80))
    assert "Routes" not in out and "Prompt" not in out and "Gen t/s" in out
    total = next(line for line in out.splitlines() if line.startswith("total"))
    assert total.rstrip().endswith("-")  # no rate yet, not a fake 0.0


def test_render_handles_no_frame_and_proxy_down():
    out = _render(cl.render(None, [], 2, proxy_error="refused", host_error="sparkrun monitor exited"))
    assert "0 host(s)" in out and "unreachable" in out and "sparkrun monitor exited" in out


# ── flags ──────────────────────────────────────────────────────────────────

def test_monitor_flags():
    ok = lambda *a: unified._parse_monitor_flags(["monitor", *a])
    assert ok() == ({"live": False, "interval": 2, "cluster": None, "help": False}, None)
    opts, err = ok("--live", "--interval", "5", "--cluster", "lab")
    assert err is None and opts["live"] and opts["interval"] == 5 and opts["cluster"] == "lab"
    assert ok("--help")[0]["help"]
    assert "unknown argument" in ok("--bogus")[1]
    assert "needs a value" in ok("--live", "--interval")[1]
    assert "whole number" in ok("--live", "--interval", "x")[1]
    assert "at least 1" in ok("--live", "--interval", "0")[1]
    assert "only apply with --live" in ok("--interval", "5")[1]


# ── errno 65 (macOS Local Network TCC) → /usr/bin/curl fallback ────────────
#
# A Homebrew keg / Framework python is adhoc-signed, so macOS TCC has no
# stable identity to bind the Local Network grant to and EVERY private-range
# connect fails `OSError(65, 'No route to host')` while the fleet is healthy
# and Apple-signed /usr/bin/curl reaches the same URL. Symptom this pins: the
# whole serving table rendering `down` on a live fleet. Nothing here touches
# the network or a real curl — urlopen and the curl seam are both stubbed.

import urllib.error
import urllib.request


class _CurlStub:
    """Records every argv the fallback hands the curl seam; returns canned rc."""

    def __init__(self, rc=0, out=b"curl-body", err=b""):
        self.rc, self.out, self.err = rc, out, err
        self.argvs = []
        self.timeouts = []

    def __call__(self, argv, timeout):
        self.argvs.append(list(argv))
        self.timeouts.append(timeout)
        return self.rc, self.out, self.err


def _urlopen_raises(exc):
    """A urlopen replacement that always fails with ``exc``."""
    def _fake(*a, **kw):
        raise exc
    return _fake


def _errno65(host="10.0.0.1"):
    return urllib.error.URLError(OSError(65, "No route to host", host))


@pytest.fixture
def curl_seam(monkeypatch):
    """Install a curl stub on the real seam and hand back a fresh notice ticket."""
    def _install(rc=0, out=b"curl-body", err=b""):
        stub = _CurlStub(rc, out, err)
        monkeypatch.setattr(cl, "_lan_curl_run", stub)
        cl._lan_curl_reset_notice()
        return stub
    return _install


def test_errno65_on_private_host_falls_back_to_curl_and_returns_body(curl_seam, monkeypatch):
    """The reported failure: errno 65 on a LAN backend now yields the body."""
    stub = curl_seam(out=METRICS.encode())
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(_errno65()))
    assert cl._http_get("http://10.0.0.1:8000/metrics", 3) == METRICS.encode()
    assert stub.argvs == [["/usr/bin/curl", "-sS", "--fail", "-L", "--max-time", "3",
                           "http://10.0.0.1:8000/metrics"]]
    assert stub.timeouts == [3]


def test_fallback_argv_is_a_list_never_a_shell(curl_seam, monkeypatch):
    """argv-list only: no shell=True, no string command, URL last, curl first."""
    stub = curl_seam()
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(_errno65()))
    cl._http_get("http://10.0.0.1:8000/metrics", 3)
    argv = stub.argvs[0]
    assert isinstance(argv, list) and argv[0] == "/usr/bin/curl"
    assert argv[-1] == "http://10.0.0.1:8000/metrics"
    for flag in ("--fail", "-L", "-sS", "--max-time"):
        assert flag in argv


@pytest.mark.parametrize("url", [
    "http://example.com:8000/metrics",   # public name: not the TCC story
    "http://8.8.8.8/metrics",            # public IP
    "http://100.128.0.1/metrics",        # just ABOVE the tailnet CGNAT /10
    "http://localhost:4000/v1/model/info",  # TCC does not gate loopback
    "http://127.0.0.1:4000/v1/model/info",
])
def test_errno65_off_a_private_host_propagates_unchanged(curl_seam, monkeypatch, url):
    """No silent curl detour for anything that is not a private host."""
    stub = curl_seam()
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(_errno65()))
    with pytest.raises(urllib.error.URLError) as ei:
        cl._http_get(url, 3)
    assert ei.value.reason.errno == 65
    assert stub.argvs == []


def test_errno113_on_private_host_does_not_fall_back(curl_seam, monkeypatch):
    """Linux EHOSTUNREACH is 113 = genuinely no route; it must stay an error."""
    stub = curl_seam()
    exc = urllib.error.URLError(OSError(113, "No route to host", "10.0.0.1"))
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(exc))
    with pytest.raises(urllib.error.URLError) as ei:
        cl._http_get("http://10.0.0.1:8000/metrics", 3)
    assert ei.value.reason.errno == 113
    assert stub.argvs == []


def test_httperror_from_urlopen_is_untouched(curl_seam, monkeypatch):
    """A status is a probe answer, not a denial — no fallback on any python."""
    stub = curl_seam()
    exc = urllib.error.HTTPError("http://10.0.0.1:8000/metrics", 503,
                                 "down", None, None)
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(exc))
    with pytest.raises(urllib.error.HTTPError) as ei:
        cl._http_get("http://10.0.0.1:8000/metrics", 3)
    assert ei.value.code == 503
    assert stub.argvs == []


def test_timeout_from_urlopen_is_untouched(curl_seam, monkeypatch):
    stub = curl_seam()
    monkeypatch.setattr(urllib.request, "urlopen",
                        _urlopen_raises(TimeoutError("timed out")))
    with pytest.raises(TimeoutError):
        cl._http_get("http://10.0.0.1:8000/metrics", 3)
    assert stub.argvs == []


def test_curl_exit_22_raises_the_httperror_family(curl_seam, monkeypatch):
    """`--fail` exit 22 must come back as HTTPError, not 'unreachable'."""
    curl_seam(rc=22, err=b"curl: (22) The requested URL returned error: 404\n")
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(_errno65()))
    with pytest.raises(urllib.error.HTTPError) as ei:
        cl._http_get("http://10.0.0.1:8000/metrics", 3)
    assert ei.value.code == 404
    assert isinstance(ei.value, urllib.error.URLError)


def test_curl_failure_of_another_kind_keeps_the_original_error(curl_seam, monkeypatch):
    """curl could not reach it either → it really is down (errno 65 stands)."""
    curl_seam(rc=7, err=b"curl: (7) Couldn't connect to server\n")
    exc = _errno65()
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(exc))
    with pytest.raises(urllib.error.URLError) as ei:
        cl._http_get("http://10.0.0.1:8000/metrics", 3)
    assert ei.value.reason.errno == 65


def test_curl_missing_surfaces_original_error_with_hint(curl_seam, monkeypatch):
    """No usable /usr/bin/curl: the errno-65 error still surfaces, + a hint."""
    def no_curl(argv, timeout):
        raise FileNotFoundError(2, "No such file or directory", argv[0])
    monkeypatch.setattr(cl, "_lan_curl_run", no_curl)
    cl._lan_curl_reset_notice()
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(_errno65()))
    with pytest.raises(urllib.error.URLError) as ei:
        cl._http_get("http://10.0.0.1:8000/metrics", 3)
    msg = str(ei.value)
    assert "No route to host" in msg            # original message kept, not replaced
    assert "curl fallback unavailable" in msg   # one-line hint appended


def test_notice_appears_exactly_once_across_many_fallbacks(curl_seam, monkeypatch,
                                                            capsys):
    """The monitor re-renders per tick; the notice must not repeat."""
    stub = curl_seam()
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(_errno65()))
    for i in range(5):
        cl._http_get(f"http://10.0.0.{i}:8000/metrics", 3)
    assert len(stub.argvs) == 5
    assert capsys.readouterr().err.count(cl.LAN_CURL_FALLBACK_NOTICE) == 1


@pytest.mark.parametrize("host", ["10.0.0.1", "192.0.2.1", "172.20.3.4",
                                 "100.64.0.3", "100.64.0.254",
                                 "169.254.9.9", "node-1.local", "gateway.local",
                                 "fd00::1234", "fe80::1"])
def test_private_host_classification_covers_lan_tailnet_and_local(host):
    """The ranges the Local Network gate can deny — incl. the whole tailnet
    CGNAT block, which Python does NOT classify as private on its own."""
    assert cl._lan_private_host(host) is True


@pytest.mark.parametrize("host", ["8.8.8.8", "1.1.1.1", "example.com",
                                  "api.example.local.example", "100.128.0.1",
                                  "100.63.255.1", "2001:4860::8888",
                                  "127.0.0.1", "localhost", "localhost.localdomain",
                                  "", None, "::1", "  "])
def test_public_loopback_and_unparseable_hosts_are_not_private(host):
    """Public hosts, loopback and junk never take the fallback: TCC only gates
    local traffic, and a public errno 65 is a real firewall/DNS problem."""
    assert cl._lan_private_host(host) is False


def test_poller_renders_backends_up_under_errno65(curl_seam, monkeypatch):
    """End-to-end for the reported symptom: the table must show `up`, not `down`.

    Realistic shape: the proxy is on loopback (TCC does not gate it, so urllib
    serves it and the route map loads) while the LAN backends are denied — which
    is exactly how the incident read: routes present, every backend `down`.
    """
    class _Resp:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/v1/model/info"):     # loopback proxy: urllib is fine
            return _Resp(json.dumps(MODEL_INFO).encode())
        raise _errno65()                        # LAN backend: denied
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    curl_urls = []

    def fake_curl(argv, timeout):
        curl_urls.append(argv[-1])
        return 0, METRICS.encode(), b""
    monkeypatch.setattr(cl, "_lan_curl_run", fake_curl)
    cl._lan_curl_reset_notice()

    p = cl.ServingPoller(clock=lambda: 100.0)
    try:
        rows = p.poll()
    finally:
        p.close()
    assert not [r for r in rows if r.get("error")], rows
    assert {(r["node"], r["port"], r["running"]) for r in rows} == {
        ("10.0.0.1", "8000", 3), ("10.0.0.2", "8001", 3)}
    # the fallback ran for the two LAN backends only — never for the loopback proxy
    assert sorted(curl_urls) == ["http://10.0.0.1:8000/metrics",
                                 "http://10.0.0.2:8001/metrics"]


def test_poller_rows_carry_cumulative_token_counters():
    net, clock = FakeNet(), [0.0]
    rows = _poller(net, clock).poll()
    assert (rows[0]["prompt_total"], rows[0]["gen_total"]) == (5000.0, 1000.0)


def test_serving_table_shows_token_totals_per_node_and_summed_even_narrow():
    rows = [
        {"node": "10.0.0.1", "port": "8000", "model": MODEL, "routes": ["worker-model"],
         "running": 1, "waiting": 0, "kv": 0.1, "gen_tps": None, "prompt_tps": None,
         "prompt_total": 110_862_221.0, "gen_total": 1_446_156.0},
        {"node": "10.0.0.2", "port": "8001", "model": MODEL, "routes": ["worker-model"],
         "running": 1, "waiting": 0, "kv": 0.1, "gen_tps": None, "prompt_tps": None,
         "prompt_total": 900_000_000.0, "gen_total": 999.0},
    ]
    out = _render(cl.serving_table(rows, width=80))
    head = next(line for line in out.splitlines() if line.startswith("Node"))
    assert " In " in head and " Out " in head
    assert "110.9M" in out and "1.4M" in out and "999" in out
    total = next(line for line in out.splitlines() if line.startswith("total"))
    assert "1.01G" in total and "1.4M" in total


def test_fmt_tokens():
    assert [cl.fmt_tokens(x) for x in (None, 999, 1_500, 202_000, 18_000_000,
                                       1_234_000_000)] == [
        "-", "999", "2k", "202k", "18.0M", "1.23G"]
