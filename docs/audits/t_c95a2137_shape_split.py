#!/usr/bin/env python3
"""Shape-split evidence for the :4000 stall audit (question (a) follow-up).

Read-only over the operator capture. Answers three things the parent audit
could not, using only data already on disk:

 1. Which path families come from *long-lived* client connections vs from a
    fresh connection per request (uvicorn logs the client's ephemeral source
    port, which identifies the HTTP/1.1 keep-alive connection).
 2. Whether that long-lived client is still connected at the 15:54 capture
    (correlate against tcp_states.txt) -> identifies the 4 loopback holders.
 3. A "response backlog" trace: if the loop kept serving, a slow request's
    access line lands after many lines from *other* ports. Count the lines
    from other ports between consecutive lines of one long-lived connection.

No writes; report to stdout.
"""
import collections
import re
import sys

DEFAULT_LOG = "/Users/desac/fleet-check/proxy-stall-2026-10-31/litellm.log"
DEFAULT_TCP = "/Users/desac/fleet-check/proxy-stall-2026-10-31/tcp_states.txt"
LOG = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LOG
TCP = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_TCP

LINE = re.compile(
    r'^INFO:\s+\S+:(?P<port>\d+) - "(?P<method>[A-Z]+) (?P<path>\S+) '
    r'HTTP/[0-9.]+" (?P<status>\d+)'
)

rows = []
with open(LOG, encoding="utf-8", errors="replace") as fh:
    for pos, raw in enumerate(fh, 1):
        m = LINE.match(raw.rstrip("\n"))
        if m:
            rows.append((pos, int(m["port"]), m["method"], m["path"], m["status"]))

by_port = collections.defaultdict(list)
for pos, port, method, path, status in rows:
    by_port[port].append((pos, method, path, status))

POLLER = {"/version", "/props", "/api/tags", "/api/v1/models", "/v1/props"}

# ---- 1. path family -> connection-reuse profile --------------------------
print("== connection-reuse profile per path ==")
for path in sorted({r[3] for r in rows}, key=lambda p: -sum(1 for r in rows if r[3] == p)):
    ev = [r for r in rows if r[3] == path]
    ports = collections.Counter(r[1] for r in ev)
    multi = [n for n in ports.values() if n > 1]
    print(f"  {path:24s} lines={len(ev):5d} conns={len(ports):5d} "
          f"max_reqs_per_conn={max(ports.values()):3d} conns_with>1={len(multi):5d}")

# ---- 2. the poller: connection lifetime ---------------------------------
print("\n== 5-path poller connection structure ==")
pol = [r for r in rows if r[3] in POLLER]
pp = collections.Counter(r[1] for r in pol)
print(f"  poller lines={len(pol)} distinct client ports={len(pp)} "
      f"max lines per port={max(pp.values())}")
# does a poller connection ever carry a NON-poller request?
shared = 0
for port, n in pp.items():
    paths = {p for _, _, p, _ in by_port[port]}
    if paths - POLLER:
        shared += 1
print(f"  poller ports that also served another path: {shared} "
      f"(non-zero => shared client, e.g. one tool reusing its pool)")

# ---- 3. correlate final-poller ports with tcp_states.txt ----------------
print("\n== poller connections still alive at the 15:54 tcp capture ==")
tcp = open(TCP, encoding="utf-8", errors="replace").read()
alive = set(int(m) for m in re.findall(r"127\.0\.0\.1:4000->127\.0\.0\.1:(\d+)", tcp))
print(f"  tcp_states loopback peer ports to :4000 = {sorted(alive)}")
last_pos = max(pos for pos, _, _, _, _ in pol)
recent = [(port, by_port[port][-1][0]) for port in pp if by_port[port][-1][0] >= last_pos - 200]
hits = [p for p, _ in recent if p in alive]
print(f"  poller ports within the last 200 lines: {len(recent)}; "
      f"of those, still ESTABLISHED at capture: {hits}")
for port, lp in sorted(recent, key=lambda kv: -kv[1])[:8]:
    paths = collections.Counter(p for _, _, p, _ in by_port[port])
    print(f"    :{port} last_line={lp:6d} nlines={len(by_port[port]):3d} "
          f"paths={dict(paths)} {'<== IN tcp_states' if port in alive else ''}")

# ---- 4. backlog trace per long-lived connection -------------------------
print("\n== interposed-line (backlog) gaps on multi-request connections ==")
print("   (lines from OTHER ports between two requests of the same connection)")
import bisect
all_pos = [r[0] for r in rows]  # already sorted by line position
worst = []
for port, ev in by_port.items():
    if len(ev) < 2:
        continue
    own = [e[0] for e in ev]
    for (a, _, pa, _), (b, _, pb, _) in zip(ev, ev[1:]):
        between = bisect.bisect_left(all_pos, b) - bisect.bisect_right(all_pos, a)
        own_between = bisect.bisect_left(own, b) - bisect.bisect_right(own, a)
        worst.append((between - own_between, port, pa, pb, a, b))
worst.sort(reverse=True)
print(f"  total consecutive-pairs on reused connections: {len(worst)}")
for others, port, pa, pb, a, b in worst[:10]:
    print(f"    :{port} {pa} -> {pb} lines={a}..{b} interposed_from_others={others}")
vals = sorted(w[0] for w in worst)
n = len(vals)
print(f"  distribution: median={vals[n//2]} p90={vals[int(n*0.9)]} p99={vals[int(n*0.99)]} max={vals[-1]}")
print(f"  pairs with >50 interposed lines: {sum(1 for v in vals if v > 50)}")

# ---- 5. per-status sanity -------------------------------------------------
print("\n== status mix ==")
print(" ", collections.Counter((r[3], r[4]) for r in rows).most_common(10))
