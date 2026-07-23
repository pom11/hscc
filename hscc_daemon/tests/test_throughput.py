"""Unit tests for throughput.py — vLLM metrics parsing and aggregation.

No network: all fetches are mocked or injected.
"""

import urllib.error

import pytest

from hscc_daemon.throughput import (
    parse_vllm_metrics,
    fetch_node_metrics,
    compute_throughput,
    format_throughput,
)


# --- Prometheus fixture with two label series, comments, and a malformed line ---
PROMETHEUS_FIXTURE = """\
# HELP vllm:prompt_tokens_total Total prompt tokens processed.
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total{engine="mp:0",model="llama"} 100.0
vllm:prompt_tokens_total{engine="mp:1",model="llama"} 200.0
# HELP vllm:generation_tokens_total Total generation tokens produced.
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{engine="mp:0",model="llama"} 500.0
vllm:generation_tokens_total{engine="mp:1",model="llama"} 300.0
# HELP vllm:num_requests_running Number of requests currently running.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="mp:0",model="llama"} 3.0
vllm:num_requests_running{engine="mp:1",model="llama"} 5.0
# HELP vllm:num_requests_waiting Number of requests waiting in queue.
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{engine="mp:0",model="llama"} 2.0
vllm:num_requests_waiting{engine="mp:1",model="llama"} 7.0
this is a completely malformed line with no metric name or value
some_other_metric{foo="bar"} 999.0
"""


class TestParseVllmMetrics:
    """parse_vllm_metrics sums across label series and tolerates noise."""

    def test_sums_two_series(self):
        result = parse_vllm_metrics(PROMETHEUS_FIXTURE)
        assert result["prompt_tokens"] == 300
        assert result["generation_tokens"] == 800
        assert result["running"] == 8
        assert result["waiting"] == 9

    def test_returns_int_when_whole(self):
        result = parse_vllm_metrics(PROMETHEUS_FIXTURE)
        assert isinstance(result["prompt_tokens"], int)
        assert isinstance(result["generation_tokens"], int)

    def test_returns_float_when_fractional(self):
        text = 'vllm:prompt_tokens_total{engine="0"} 10.5\n'
        result = parse_vllm_metrics(text)
        assert result["prompt_tokens"] == 10.5
        assert isinstance(result["prompt_tokens"], float)

    def test_missing_metrics_are_zero(self):
        text = "vllm:prompt_tokens_total{engine=\"0\"} 42\n"
        result = parse_vllm_metrics(text)
        assert result["prompt_tokens"] == 42
        assert result["generation_tokens"] == 0
        assert result["running"] == 0
        assert result["waiting"] == 0

    def test_empty_input(self):
        result = parse_vllm_metrics("")
        assert result == {
            "prompt_tokens": 0,
            "generation_tokens": 0,
            "running": 0,
            "waiting": 0,
        }

    def test_comment_only(self):
        result = parse_vllm_metrics("# just a comment\n# another\n")
        assert result["prompt_tokens"] == 0

    def test_malformed_line_skipped(self):
        text = (
            "vllm:prompt_tokens_total{engine=\"0\"} 10\n"
            "garbage without value\n"
            "vllm:prompt_tokens_total{engine=\"1\"} 20\n"
        )
        result = parse_vllm_metrics(text)
        assert result["prompt_tokens"] == 30

    def test_untracked_metric_ignored(self):
        text = "some_random_metric 999.0\n"
        result = parse_vllm_metrics(text)
        assert result["prompt_tokens"] == 0

    def test_metric_without_labels(self):
        text = "vllm:prompt_tokens_total 50.0\n"
        result = parse_vllm_metrics(text)
        assert result["prompt_tokens"] == 50


class TestFetchNodeMetrics:
    """fetch_node_metrics returns None on errors."""

    def test_returns_none_on_url_error(self, monkeypatch):
        def fake_urlopen(*args, **kwargs):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        result = fetch_node_metrics("http://unreachable:8000/metrics")
        assert result is None

    def test_returns_none_on_timeout(self, monkeypatch):
        import socket

        def fake_urlopen(*args, **kwargs):
            raise socket.timeout()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        result = fetch_node_metrics("http://slow:8000/metrics")
        assert result is None

    def test_parses_on_success(self, monkeypatch):
        class FakeResp:
            def read(self):
                return b"vllm:prompt_tokens_total{engine=\"0\"} 10\n"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: FakeResp())
        result = fetch_node_metrics("http://ok:8000/metrics")
        assert result is not None
        assert result["prompt_tokens"] == 10


class TestComputeThroughput:
    """compute_throughput aggregates across endpoints with injectable fetch."""

    def _fake_fetch_ok(self, url):
        return {
            "prompt_tokens": 100,
            "generation_tokens": 500,
            "running": 3,
            "waiting": 2,
        }

    def test_aggregates_two_endpoints_one_ok(self):
        endpoints = ["http://node1:8000/metrics", "http://node2:8000/metrics"]

        def selective_fetch(url):
            if "node1" in url:
                return self._fake_fetch_ok(url)
            return None  # node2 unreachable

        data = compute_throughput(endpoints, _fetch=selective_fetch)
        fleet = data["fleet"]
        assert fleet["nodes_total"] == 2
        assert fleet["nodes_ok"] == 1
        assert fleet["prompt_tokens"] == 100
        assert fleet["generation_tokens"] == 500
        assert fleet["running"] == 3
        assert fleet["waiting"] == 2
        assert len(data["by_node"]) == 1
        assert "http://node1:8000/metrics" in data["by_node"]

    def test_both_endpoints_ok(self):
        endpoints = ["http://a:8000/metrics", "http://b:8000/metrics"]

        data = compute_throughput(endpoints, _fetch=self._fake_fetch_ok)
        fleet = data["fleet"]
        assert fleet["nodes_ok"] == 2
        assert fleet["nodes_total"] == 2
        assert fleet["prompt_tokens"] == 200  # 100 * 2
        assert len(data["by_node"]) == 2

    def test_no_endpoints(self):
        data = compute_throughput([], _fetch=self._fake_fetch_ok)
        assert data["fleet"]["nodes_total"] == 0
        assert data["fleet"]["nodes_ok"] == 0
        assert data["by_node"] == {}

    def test_serving_derivation_empty_on_missing_serving(self, monkeypatch):
        """When endpoints=None and serving module raises, defaults to []."""
        # Patch serving.load_serving to raise so we exercise the fallback
        import hscc_daemon.serving as serving_mod

        original_load = serving_mod.load_serving

        def failing_load(*a, **kw):
            raise RuntimeError("simulated serving failure")

        monkeypatch.setattr(serving_mod, "load_serving", failing_load)

        # compute_throughput imports 'serving' at call time; reload to pick up
        import importlib
        import hscc_daemon.throughput as tp
        importlib.reload(tp)

        data = tp.compute_throughput()  # no endpoints arg
        assert data["fleet"]["nodes_total"] == 0
        assert data["fleet"]["nodes_ok"] == 0
        assert data["by_node"] == {}

    def test_returns_int_when_whole(self):
        endpoints = ["http://a:8000/metrics"]
        data = compute_throughput(endpoints, _fetch=self._fake_fetch_ok)
        assert isinstance(data["fleet"]["prompt_tokens"], int)


class TestFormatThroughput:
    """format_throughput produces readable text."""

    def test_nonempty(self):
        data = {
            "fleet": {
                "prompt_tokens": 300,
                "generation_tokens": 800,
                "running": 8,
                "waiting": 9,
                "nodes_ok": 1,
                "nodes_total": 2,
            },
            "by_node": {
                "http://node1:8000/metrics": {
                    "prompt_tokens": 300,
                    "generation_tokens": 800,
                    "running": 8,
                    "waiting": 9,
                }
            },
        }
        text = format_throughput(data)
        assert "Fleet throughput" in text
        assert "prompt=300" in text
        assert "generation=800" in text
        assert "running=8" in text
        assert "waiting=9" in text
        assert "Nodes: 1/2 reachable" in text
        assert "Per-node:" in text
        assert "queue_depth=9" in text

    def test_empty(self):
        data = {
            "fleet": {
                "prompt_tokens": 0,
                "generation_tokens": 0,
                "running": 0,
                "waiting": 0,
                "nodes_ok": 0,
                "nodes_total": 0,
            },
            "by_node": {},
        }
        text = format_throughput(data)
        assert "prompt=0" in text
        assert "Nodes: 0/0 reachable" in text
        assert "Per-node:" not in text

    def test_returns_string(self):
        data = {"fleet": {}, "by_node": {}}
        assert isinstance(format_throughput(data), str)
