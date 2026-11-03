kind: Added
task: t_fc6926fe

- **The daemon now detects and heals HELD-STREAM PROXY STALLS (the
  2026-11-02 incident) on its own.** Two stalls (12:27, 12:45) left LiteLLM
  holding worker streams on :4000 while ALL four nodes sat idle answering
  direct requests in <0.5 s (faulthandler proof: the frozen worker blocked in
  openai `_streaming` `iter_bytes`); `check_proxy` could not see the shape —
  its probes only ask whether the proxy accepts NEW work, which a stalled
  proxy still does — and each recovery was a manual `proxy stop` → TERM/KILL
  the lingering litellm → `proxy start`. New stream `proxy_stall`
  (`daemon_ops.PERIODIC_INTERVALS["proxy_stall"] = 30` + a `CHECKS` row — the
  cadence SOT, never a private sleep loop): a stall is declared only when ALL
  THREE operator-specified conditions hold — (a) ≥1 ESTABLISHED socket on the
  proxy port (local `lsof`, allowed: the proxy is THIS host's service), (b)
  sum of vLLM `num_requests_running + num_requests_waiting` == 0 across every
  serving endpoint from serving.json (via the shared
  `throughput.fetch_node_metrics` — never ssh, never local `docker ps`)
  sustained ≥120 s (4 consecutive 30 s ticks; streak in monotonic time), and
  (c) a 5-token chat probe through :4000 still SUCCEEDS. Any UNKNOWN read
  (lsof failure, unreachable node /metrics, probe transport failure) fails
  OPEN with a named degradation and resets the streak; an intentional
  autodown (`autodown.classify == expected_down`, the shared table — no
  parallel rule) makes the tick inert. Remediation on a confirmed stall is
  the operator's recipe THROUGH the existing proxy-control path
  (`health._proxy_repair_orphan`: stop → cmdline-vetted TERM/KILL → plain
  start → port-owner verification, every leg a bounded subprocess) under the
  heal lease, then one re-probe; at most ONE restart per 10 min, cooldown
  persisted in `proxy_stall.json` (epoch stamps, survive a daemon restart,
  carried forward on every stamp so a healthy tick cannot wipe the window),
  and a suppressed remediation logs + raises a `proxy.stall.suppressed`
  event instead of restarting. `hscc verify` gains the stream for free via
  the cadence SOT (staleness floor `max(600, 2*30+90)`).
---
kind: Verified
task: t_fc6926fe
order: 1

- `hscc_daemon/tests/test_proxy_stall.py`: 0 -> **24** cases, green. Fakes
  only through injected seams (socket-count / node-metrics / probe / restart
  / intentional / state / event writers — the `pipeline_watchdog`
  `check_dgx_fn` precedent); the run's sparkrun fence confirms zero live
  sparkrun execs. Covers the card's full matrix: confirmed stall restarts
  exactly once with the cooldown written and events recorded; fleet-busy,
  probe-negative and socket-idle ticks never restart; UNKNOWN reads fail
  open named; cooldown suppresses a second stall inside 10 min AND a failed
  repair still spends the window; intentional autodown inert before any
  probe runs; `stall_since` resets when any condition clears. Mutation
  proofs: replacing each of `_cond_a`/`_cond_b`/`_cond_c`/`_cond_cooldown`
  fails exactly its own scenario (a-removal → socket-idle restarts; b →
  fleet-busy restarts; c → probe-negative restarts; cooldown → bounce-kill
  at t=270) and the other three guards still hold in every mutant.
