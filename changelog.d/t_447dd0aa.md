kind: Docs
task: t_447dd0aa

- **Root-cause audit for the 2026-10-30/31 LiteLLM proxy `:4000` stalls**
  (`docs/audits/2026-10-31-proxy-stalls.md`). Investigation only, no fixes.
  Three findings, each with timestamped-log or `file:line` evidence: (a) the
  stall is inside the gateway but **not** an event-loop freeze — a five-path
  external poller stays evenly interleaved across all 17,376 access lines
  (max gap 46 lines), and autodiscover never restarted the router (0
  `reconciled` lines in 1,586 sweeps); (b) the daemon repair cannot recover
  because `_proxy_needs_restart` withholds `--restart` on a *timed-out* probe
  (so sparkrun answers `Proxy is already running (PID 91693)`), strict-consecutive
  streak math resets the escalation ladder on an intermittent healthy tick
  (17× "attempt 1 of 3", 0× "attempt 3"), and repair verification demands
  `port owner == tracked pid`, which a `uvx` wrapper launch can never satisfy
  (`grep -c "proxy repaired on" ~/.hscc/daemon.log` = **0**); (c) `FLEET
  VISIBILITY LOST` is a **symptom, not an independent fault** — neither sparkrun
  status path touches `:4000` (both SSH-sweep the nodes), so it must not be
  escalated as a second failure. Eight atomic fix proposals (FIX-1..FIX-8) with
  repo + `file:line` + acceptance + risk; sparkrun-side items are upstream
  proposals only.
---
kind: Verified
order: 1

- Docs-only change; `bash scripts/run_tests.sh` and
  `python3 scripts/address_guard.py --tracked` run at completion.
