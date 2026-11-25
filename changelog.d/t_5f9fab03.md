kind: Added
task: t_5f9fab03

- **`scripts/load_burners.sh` gained an opt-in stage trace for signal-stall
  forensics.** Setting `HSCC_LOAD_BURNERS_TRACE=<path>` appends one line per
  harness stage — `<pid> <tag> <stage> [detail]`, stages `start`, `in-wait`,
  `{int,term,hup}-trap`, `cleanup-entry`, `after-term-loop`, `after-sleep`,
  `after-kill-loop`, `reap-wait <pid>`/`reap-ok <pid>`, `cleanup-exit
  <status>` — so a stalled run can be pinned to the stage it reached instead
  of inferred from outside (the t_f8e6fe38 sibling-suite SIGINT stall looks
  identical to pgrep whether the trap never ran or a reap is blocking).
  Unset ⇒ `lb_trace` returns before any syscall: zero behaviour change for
  every shipped consumer (`loadstress_leg.py`, `run_tests.sh`,
  `test_load_burners.py`, `test_loadstress_leg.py` — 23 passed alone with the
  variable unset). Every marker write carries `2>/dev/null` *before* the
  append redirection, so a broken trace path cannot print what the harness
  contract forbids; markers never touch stdout.
