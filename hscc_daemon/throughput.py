"""vLLM throughput and queue-depth metrics from Prometheus /metrics endpoints.

Best-effort: never raises on missing or unreachable inputs.
"""

import urllib.request
import urllib.error


# Metric name -> key mapping
_METRIC_KEYS = {
    "vllm:prompt_tokens_total": "prompt_tokens",
    "vllm:generation_tokens_total": "generation_tokens",
    "vllm:num_requests_running": "running",
    "vllm:num_requests_waiting": "waiting",
}


def _int_if_whole(value):
    """Convert float to int when the value has no fractional part."""
    if value == int(value):
        return int(value)
    return value


def parse_vllm_metrics(text):
    """Parse Prometheus /metrics text and sum vLLM counters/gauges.

    Sums across all engine/model label series for each tracked metric.
    Ignores comment lines; tolerates missing metrics (returns 0);
    skips malformed lines.

    Returns:
        dict with keys: prompt_tokens, generation_tokens, running, waiting
    """
    result = {key: 0.0 for key in _METRIC_KEYS.values()}

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Find which metric this line belongs to
        matched_key = None
        matched_name = None
        for metric_name, key in _METRIC_KEYS.items():
            if line.startswith(metric_name):
                matched_key = key
                matched_name = metric_name
                break

        if matched_key is None:
            continue

        # matched_name is guaranteed bound here (set alongside matched_key)
        assert matched_name is not None
        rest = line[len(matched_name):]
        if rest.startswith("{"):
            brace_end = rest.find("}")
            if brace_end == -1:
                continue  # malformed — missing closing brace
            rest = rest[brace_end + 1:]

        rest = rest.strip()
        token = rest.split()[0] if rest else ""
        try:
            value = float(token)
        except (ValueError, IndexError):
            continue  # malformed — skip

        result[matched_key] += value

    # Convert floats to ints where whole
    return {key: _int_if_whole(val) for key, val in result.items()}


def fetch_node_metrics(url, timeout=4):
    """GET a vLLM /metrics endpoint and parse the response.

    Returns:
        dict from parse_vllm_metrics on success, None on any error.
    """
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        return parse_vllm_metrics(body)
    except Exception:
        return None


def compute_throughput(endpoints=None, _fetch=None):
    """Aggregate vLLM metrics across endpoints.

    Args:
        endpoints: list of metrics URLs. If None, derive from serving config.
        _fetch: optional override for fetch_node_metrics (testing).

    Returns:
        dict with 'by_node' (url -> metrics) and 'fleet' (totals + node counts).
    """
    fetch = _fetch if _fetch is not None else fetch_node_metrics

    # Derive endpoints from serving config if not provided
    if endpoints is None:
        try:
            from hscc_daemon import serving
            svc = serving.load_serving()
            units = serving.keepalive_units(svc)
            endpoints = [f"http://{u['node']}:{u['port']}/metrics" for u in units]
        except Exception:
            endpoints = []

    by_node = {}
    fleet = {
        "prompt_tokens": 0.0,
        "generation_tokens": 0.0,
        "running": 0.0,
        "waiting": 0.0,
        "nodes_ok": 0,
        "nodes_total": len(endpoints),
    }

    for url in endpoints:
        metrics = fetch(url)
        if metrics is not None:
            by_node[url] = metrics
            fleet["nodes_ok"] += 1
            for key in ("prompt_tokens", "generation_tokens", "running", "waiting"):
                fleet[key] += metrics.get(key, 0)
        # unreachable nodes counted in nodes_total, not nodes_ok

    # Convert fleet totals to int where whole
    for key in ("prompt_tokens", "generation_tokens", "running", "waiting"):
        fleet[key] = _int_if_whole(fleet[key])

    return {"by_node": by_node, "fleet": fleet}


def format_throughput(data):
    """Compact human-readable summary of throughput data.

    Args:
        data: dict from compute_throughput().

    Returns:
        str with fleet totals and per-node breakdown.
    """
    fleet = data.get("fleet", {})
    by_node = data.get("by_node", {})

    lines = []
    lines.append(
        f"Fleet throughput: "
        f"prompt={fleet.get('prompt_tokens', 0)}, "
        f"generation={fleet.get('generation_tokens', 0)}, "
        f"running={fleet.get('running', 0)}, "
        f"waiting={fleet.get('waiting', 0)}"
    )
    lines.append(
        f"Nodes: {fleet.get('nodes_ok', 0)}/{fleet.get('nodes_total', 0)} reachable"
    )

    if by_node:
        lines.append("Per-node:")
        for node, m in by_node.items():
            label = node
            lines.append(
                f"  {label}: prompt={m.get('prompt_tokens', 0)}, "
                f"gen={m.get('generation_tokens', 0)}, "
                f"running={m.get('running', 0)}, "
                f"queue_depth={m.get('waiting', 0)}"
            )

    return "\n".join(lines)
