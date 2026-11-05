#!/usr/bin/env python3
"""Sample vLLM /metrics on the engines over a window; report per-second advancement.

Fleet hosts are deliberately NOT hard-coded (this repo is public). Pass them via env, using the
documented placeholders as the shape of the argument — LAN engines `10.0.0.x`:

    ENGINE_METRICS="10.0.0.244:8000,10.0.0.246:8003,10.0.0.247:8001,10.0.0.248:8002" \
        python3 t_22b546e3_metrics_watch.py 150

The port per engine differs by deployment; `sparkrun status` is the source of truth for both.
Purpose: the discriminator against an "engine wedge" verdict — counters that advance while agents
are silent prove the engine is healthy and the loss is upstream of it.
"""
import os
import sys
import time
import urllib.request

_spec = os.environ.get("ENGINE_METRICS", "").strip()
if not _spec:
    sys.exit("set ENGINE_METRICS='host:port,host:port,...' (see docstring; placeholders 10.0.0.x)")
HOSTS = {}
for item in _spec.split(","):
    item = item.strip()
    if item:
        HOSTS[item] = f"http://{item}/metrics"
WANT = ("vllm:num_requests_running", "vllm:num_requests_waiting",
        "vllm:num_preemptions_total", "vllm:prompt_tokens_total",
        "vllm:generation_tokens_total")

def grab(url):
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            body = r.read().decode()
    except Exception as e:
        return None, repr(e)
    out = {}
    for ln in body.splitlines():
        for w in WANT:
            if ln.startswith(w + "{"):
                out[w] = float(ln.rsplit(" ", 1)[1])
    return out, None

window = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
step = 10.0
prev = None
prev_t = None
t0 = time.time()
while time.time() - t0 < window:
    now = time.time()
    row = {}
    for h, u in HOSTS.items():
        m, err = grab(u)
        row[h] = m if m else err
    if prev is None:
        print("t=%.0fs first sample: running/waiting per host" % (now - t0))
        for h, m in row.items():
            if isinstance(m, dict):
                print("  %s run=%s wait=%s preempt=%s ptok=%.0e gtok=%.4e" % (
                    h, m.get("vllm:num_requests_running"), m.get("vllm:num_requests_waiting"),
                    m.get("vllm:num_preemptions_total"), m.get("vllm:prompt_tokens_total", 0),
                    m.get("vllm:generation_tokens_total", 0)))
            else:
                print("  %s ERR %s" % (h, m))
    else:
        dt = now - prev_t
        for h, m in row.items():
            p = prev[h]
            if isinstance(m, dict) and isinstance(p, dict):
                dp = m["vllm:prompt_tokens_total"] - p["vllm:prompt_tokens_total"]
                dg = m["vllm:generation_tokens_total"] - p["vllm:generation_tokens_total"]
                print("t=%.0fs %s run=%s wait=%s d_prompt=%.0f d_gen=%.0f (%.1f tok/s gen) preempt=%s" % (
                    now - t0, h, m.get("vllm:num_requests_running"), m.get("vllm:num_requests_waiting"),
                    dp, dg, dg / dt if dt else 0, m.get("vllm:num_preemptions_total")))
            else:
                print("t=%.0fs %s ERR %s" % (now - t0, h, m))
    prev = row
    prev_t = now
    time.sleep(step)
print("done window=%.0fs" % (time.time() - t0))
