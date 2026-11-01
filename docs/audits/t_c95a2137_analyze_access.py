#!/usr/bin/env python3
"""Reconstruct per-connection structure of the uvicorn access capture.

The parent audit only used path-gap analysis. Every line also carries the
client's ephemeral source port, which identifies the *connection* a request
was served on (HTTP/1.1 keep-alive reuses a connection for sequential
requests). That lets us test per-client / per-connection hypotheses.

Read-only. Prints a report; nothing is written.
"""
import re
import sys
import collections

DEFAULT_LOG = "/Users/desac/fleet-check/proxy-stall-2026-10-31/litellm.log"
LOG = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LOG

LINE = re.compile(
    r'^INFO:\s+(?P<client>\S+):(?P<port>\d+) - "(?P<method>[A-Z]+) (?P<path>\S+) '
    r'HTTP/[0-9.]+" (?P<status>\d+)(?: (?P<reason>.*))?$'
)

rows = []
unparsed = collections.Counter()
with open(LOG, encoding="utf-8", errors="replace") as fh:
    for i, raw in enumerate(fh, 1):
        raw = raw.rstrip("\n")
        m = LINE.match(raw)
        if not m:
            if raw.strip():
                unparsed[raw[:60]] += 1
            continue
        rows.append((i, int(m["port"]), m["method"], m["path"], m["status"]))

print(f"total lines={i} parsed={len(rows)} unparsed_kinds={len(unparsed)}")
for k, v in unparsed.most_common(8):
    print(f"  unparsed x{v}: {k!r}")

# ---- per-client-port profiles -------------------------------------------
by_port = collections.defaultdict(list)
for pos, port, method, path, status in rows:
    by_port[port].append((pos, method, path, status))

print(f"\ndistinct client ports = {len(by_port)}")
sizes = collections.Counter(len(v) for v in by_port.values())
print("lines-per-port histogram (size: count), top:",
      dict(sorted(sizes.items())[:12]))
print("ports with >1 line:", sum(1 for v in by_port.values() if len(v) > 1))
print("top ports by line count:")
for port, v in sorted(by_port.items(), key=lambda kv: -len(kv[1]))[:12]:
    paths = collections.Counter(p for _, _, p, _ in v)
    print(f"  :{port} lines={len(v)} span={v[0][0]}..{v[-1][0]} "
          f"paths={dict(paths.most_common(4))}")

# ---- which clients issue which paths ------------------------------------
print("\npath -> distinct client ports (top 4 paths by volume):")
paths = collections.Counter(r[3] for r in rows)
for p, c in paths.most_common(6):
    ports = set(r[1] for r in rows if r[3] == p)
    print(f"  {p:28s} lines={c:5d} ports={len(ports)}")

# ---- /v1/models issuers: find the 60s daemon clock ----------------------
print("\n/v1/models 200 issuers (line positions; cadence proxy):")
vm = [(pos, port) for pos, port, m, p, s in rows if p == "/v1/models" and s == "200"]
per = collections.defaultdict(list)
for pos, port in vm:
    per[port].append(pos)
for port, poss in sorted(per.items(), key=lambda kv: -len(kv[1]))[:14]:
    gaps = [b - a for a, b in zip(poss, poss[1:])]
    med = sorted(gaps)[len(gaps) // 2] if gaps else 0
    print(f"  :{port:6d} n={len(poss):5d} median_gap_lines={med:6d} "
          f"max_gap={max(gaps) if gaps else 0}")
