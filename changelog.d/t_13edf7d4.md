kind: Fixed
task: t_13edf7d4

- **Every heal path could destroy the fleet it was protecting (2026-10-14 incident).** Six defects fixed together: a shared, durable heal lease so only one repair runs at a time (F2); auto-heal recreates ONLY the wedged unit — the old whole-template `--force-recreate` tore down healthy units mid-load (F3); proxy health now compares the LIVE router to actually-healthy units instead of trusting sparkrun's state-file PID, and repairs an orphan listener behind a cmdline vet (F4); a durable per-node heal circuit breaker stops a repair storm after 6 actions in 15 min, with `hscc heal-clear` (F5); `hscc cluster up/down` now REFUSES unknown flags (`--help` used to start the fleet) and `hscc start` detaches stdio (F6); and a launch guard refuses to start a vLLM on a host whose GPU already holds a workload outside the declared span — refuse + alert, never preempt, and 'status unavailable' is also a refusal (A2).
---
kind: Fixed
task: t_13edf7d4

- **Dispatcher false 'GENUINE STALL' pages.** `kanban.max_in_progress` is a FLEET-WIDE cap, but the wedge detector computed roominess per board — with 3 tasks running across two boards a correctly-backlogged gateway was paged as wedged for an hour. Roominess now requires global headroom too, the healthy reason names which cap bound (`at_capacity (global)`), and recovery's gave-up latch clears when the stream is confirmed green (A1).
