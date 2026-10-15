"""`hscc cluster monitor --live`: parsers, the serving poller and the renderers.

Everything network-facing is faked; nothing here touches the fleet.
"""

import io
import json

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
