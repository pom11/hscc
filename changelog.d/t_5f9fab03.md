kind: Added
task: t_5f9fab03

- **`scripts/load_burners.sh` gained an opt-in stage trace for signal-stall
  forensics.** Setting `HSCC_LOAD_BURNERS_TRACE=<path>` appends one line per
  harness stage — `<pid> <tag> <stage> [detail]`, stages `start`, `in-wait`,
  `{int,term,hup}-trap`, `cleanup-entry`, `after-term-loop`, `after-sleep`,
  `after-kill-loop`, `reap-wait <pid>`/`reap-ok <pid>`, `cleanup-exit
  <status>` — which localized the t_f8e6fe38 SIGINT stall to the stage the
  parent card's decision table needs: four paired suite-traces show the
  stalled harness stopping at `in-wait` with ZERO `int-trap` markers while
  its burners keep running (watcher snapshot), so cleanup never ran and the
  reap loop is ruled out. Unset ⇒ `lb_trace` returns before any syscall:
  zero behaviour change for every shipped consumer (23 passed alone with it
  unset). Marker writes carry `2>/dev/null` *before* the append redirection;
  markers never touch stdout. The audit
  `docs/audits/load-burners-sigint-trace-stage_t_5f9fab03.md` records the
  measurements — including that a bounded-slice wait rewrite was tried and
  REJECTED (the stalled harness crossed ~105 command boundaries without
  running the trap: bash 3.2 never delivers that SIGINT to its handler) —
  and the two next probes. No shipped behaviour changed; the flake itself
  stays open with carded next steps.
