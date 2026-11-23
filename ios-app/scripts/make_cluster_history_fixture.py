#!/usr/bin/env python3
"""Generate the committed `v1_cluster_history*.json` decode fixtures from the ROUTE.

Why this exists: the decode fixtures for `GET /v1/cluster/history` must not be a
hand-typed guess. A fixture written from the doc table proves only that the model
matches the doc, so a drift between the model and the handler stays invisible
while every gate is green. This script produces the bytes instead:

  1. it fills a real store through the recorder's OWN write path
     (`metrics_history.MetricsRecorder.tick`, the same 2 s path the daemon uses,
     which rolls tiers 1 and 2 with its own `rollup`), and
  2. it calls `routes_cluster.handle_cluster_history` directly for each window,
     then writes the payload the handler returned.

Nothing here re-implements aggregation, and no key name is typed by hand: the
field names on the wire are the recorder's (`HOST_KEYS` / `HOST_COLUMNS` /
`ENGINE_KEYS`). The instant fixture is pinned to `NOW` by monkey-patching the
route's own clock seam (`_history_now`), which is what
`hscc-api/tests/test_cluster_history_parity.py` does, so `since`/`until` and the
bucket starts are reproducible.

Sanitisation (the ONLY rewrite applied to handler output):
  * `store.path`  -> `/Users/example/.hscc/metrics/history.db`
    (the generator's temp dir would otherwise leak a machine-specific path;
    `ClusterHistoryStore` deliberately does not declare `path`, so the model
    never decodes it — the key stays on the wire on purpose so that refusal is
    itself proven).
  * host addresses are TEST-NET-1 `192.0.2.x` by construction (never a real node).

Usage (no server, no network, no ssh; the interpreter must have PyYAML, because
importing the handler pulls in api_server — the same interpreter
`hscc-api/tests` runs under works):

    python3 ios-app/scripts/make_cluster_history_fixture.py [--out DIR]

Default `--out` is `ios-app/scripts/model_decode_check/fixtures`, i.e. it
regenerates the committed fixtures in place. Re-run it after any change to the
recorder's columns or the route's projection and commit the diff: a change in
the recorder shows up here as a changed fixture, which is the point.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (str(REPO_ROOT), str(REPO_ROOT / "hscc-api")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import routes_cluster                                               # noqa: E402
from hscc_daemon import metrics_history as mh                       # noqa: E402

HOST_A = "192.0.2.10"                       # TEST-NET-1 — never a real node
HOST_B = "192.0.2.11"
PORT = "8000"
MODEL = "org/Qwen-Test"
PATH_PLACEHOLDER = "/Users/example/.hscc/metrics/history.db"

#: aligned to 2 s, 1 min and 5 min so every bucket boundary is a real one.
NOW = 1_795_000_000 - (1_795_000_000 % 300)
TICKS = 360                                 # 12 minutes at 2 s -> 11 closed minutes


def sample(ts: float, host: str = HOST_A, **over) -> dict:
    """One `sparkrun cluster monitor --json` host sample, in sparkrun's names."""
    s = {"timestamp": str(int(ts)), "hostname": host,
         "cpu_usage_pct": "6.2", "cpu_temp_c": "54.0",
         "gpu_util_pct": "94", "gpu_temp_c": "71", "gpu_power_w": "160.0",
         "mem_used_pct": "31.5", "swap_used_mb": "0", "swap_total_mb": "8192",
         "swap_used_pct": "", "gpu_mem_used_mb": "12000",
         "gpu_clock_mhz": "1450", "net_recv_bytes_s": "1024",
         "net_send_bytes_s": "2048", "sparkrun_jobs": "1"}
    s.update(over)
    return s


def monitor_frame(ts: float) -> dict:
    """HOST_B never reports its GPU — the `None` path must survive the round trip."""
    return {"hosts": [
        {"host": HOST_A, "error": None,
         "sample": sample(ts, gpu_power_w=str(100.0 + (ts % 60)))},
        {"host": HOST_B, "error": None,
         "sample": sample(ts, host=HOST_B, gpu_temp_c="", gpu_power_w="",
                          gpu_util_pct="0")},
    ]}


def serving_rows(ts: float) -> list:
    """A `ServingPoller.poll()` row list. `kv` is vLLM's raw
    `vllm:kv_cache_usage_perc` gauge — a 0-1 FRACTION, which is why
    `cluster_live.py`'s own table cell multiplies it by 100."""
    return [{"node": HOST_A, "port": PORT, "model": MODEL,
             "running": 2, "waiting": 1, "kv": 0.125,
             "gen_tps": 40.0, "prompt_tps": 200.0,
             "prompt_total": 900000.0, "gen_total": 250000.0}]


def build_store(hscc_dir: str) -> None:
    rec = mh.MetricsRecorder(hscc_dir=hscc_dir)
    for i in range(TICKS):
        ts = NOW - (TICKS * mh.TIER0_INTERVAL_S) + i * mh.TIER0_INTERVAL_S
        rec.tick(monitor_frame(ts), serving_rows(ts), ts)
    rec.tick(monitor_frame(NOW), serving_rows(NOW), NOW)   # closes the last minute
    if rec.store.count(1) == 0:
        raise SystemExit("fixture produced no tier-1 rows — the recorder changed")
    if rec.store.count(2) == 0:
        raise SystemExit("fixture produced no tier-2 rows — the recorder changed")
    rec.store.close()


class Ctx:
    """Only `hscc_dir` is read by the handler."""

    def __init__(self, hscc_dir: str):
        self.hscc_dir = hscc_dir


def sanitize(payload: dict) -> dict:
    store = payload.get("store")
    if isinstance(store, dict) and "path" in store:
        store["path"] = PATH_PLACEHOLDER
    return payload


def serve(hscc_dir: str, query: dict) -> dict:
    status, payload = routes_cluster.handle_cluster_history(
        None, Ctx(hscc_dir), query, None)
    if status != 200:
        raise SystemExit(f"{query} answered {status}: {payload}")
    return sanitize(payload)


CASES = (
    # file name                                    query
    ("v1_cluster_history_6h.json",                 {"window": "6h"}),
    ("v1_cluster_history_7d.json",                 {"window": "7d"}),
    ("v1_cluster_history_6h_engine_only.json",     {"window": "6h",
                                                     "metrics": "gen_tps,run"}),
    ("v1_cluster_history_15m.json",                {"window": "15m"}),
    # The `tier` axis (t_b611b56e): an explicit span rather than a preset
    # anchored to `now`. Committed so the decoder is exercised against the bytes
    # the new axis really returns — `axis: "tier"`, and a `window` that is a
    # derived span label instead of a preset name the request never used.
    ("v1_cluster_history_tier_axis.json",          {"tier": "1",
                                                    "since": str(NOW - 900),
                                                    "until": str(NOW - 300)}),
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(REPO_ROOT / "ios-app" / "scripts" /
                                        "model_decode_check" / "fixtures"))
    args = ap.parse_args()

    work = tempfile.mkdtemp(prefix="hscc-history-fixture-")
    original_now = routes_cluster._history_now
    try:
        build_store(work)
        routes_cluster._history_now = lambda: float(NOW)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for name, query in CASES:
            payload = serve(work, dict(query))
            text = json.dumps(payload, indent=1, sort_keys=True) + "\n"
            (out / name).write_text(text)
            counts = payload.get("counts", {})
            print(f"wrote {name}: window={payload['window']} tier={payload['tier']} "
                  f"resolution={payload['resolution']} "
                  f"hosts={counts.get('hosts')} engines={counts.get('engines')}")
    finally:
        routes_cluster._history_now = original_now
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
