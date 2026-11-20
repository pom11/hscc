"""``_backing_serving_snapshot`` must report a current tok/s (t_d0a32549).

A fresh ``ServingPoller`` per request has no previous counter sample, so every
row's ``gen_tps`` was null and every widget read "— tok/s".
"""
from __future__ import annotations

import routes_cluster
from hscc_daemon import cluster_live


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def install(monkeypatch, clock):
    totals = {"gen_total": 0.0}

    def get(url, timeout):
        if url.endswith("/v1/model/info"):
            return b'{"data": [{"model_name": "m", "litellm_params": {"api_base": "http://192.0.2.10:8000/v1", "model": "hosted_vllm/m"}}]}'
        totals["gen_total"] += 40.0 * 2  # 40 tok/s over the 2 s window
        return (f'vllm:num_requests_running{{model_name="m"}} 1\n'
                f'vllm:generation_tokens_total{{model_name="m"}} {totals["gen_total"]}\n'
                f'vllm:prompt_tokens_total{{model_name="m"}} 0\n').encode()

    real = cluster_live.ServingPoller
    monkeypatch.setattr(cluster_live, "ServingPoller",
                        lambda: real(get=get, clock=clock))
    monkeypatch.setattr(routes_cluster, "_SERVING_POLLER", None)
    monkeypatch.setattr(routes_cluster, "_SERVING_LAST_AT", None)


def test_first_call_primes_and_reports_a_rate(monkeypatch):
    clock = Clock()
    install(monkeypatch, clock)
    out = routes_cluster._backing_serving_snapshot(clock=clock, sleep=clock.sleep)
    rows = [r for r in out["rows"] if "error" not in r]
    assert rows and all(r["gen_tps"] == 40.0 for r in rows)


def test_a_stale_previous_sample_is_reprimed_not_averaged(monkeypatch):
    clock = Clock()
    install(monkeypatch, clock)
    routes_cluster._backing_serving_snapshot(clock=clock, sleep=clock.sleep)
    clock.t += 1800  # a widget's 30-minute gap
    out = routes_cluster._backing_serving_snapshot(clock=clock, sleep=clock.sleep)
    rows = [r for r in out["rows"] if "error" not in r]
    assert all(r["gen_tps"] == 40.0 for r in rows)
