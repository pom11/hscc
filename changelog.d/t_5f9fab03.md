kind: Fixed
task: t_5f9fab03

- **The `load_burners.sh` harness no longer awaits its command in one
  unbounded `wait`, closing a measured bash 3.2 signal race.** With a sibling
  `scripts/tests` suite sharing the host, `test_signal_to_the_harness_takes_
  every_burner_with_it[2-130]` (SIGINT) could time out at
  `test_load_burners.py:110` while the harness slept inside
  `wait "$COMMAND_PID"` with both burners still RUNNING at 100 % CPU: the
  stage trace added this card caught the stalled harness at `in-wait` with
  ZERO `int-trap` markers in four suite-traces (and the parent's own TERM
  never produced `term-trap` either) — bash 3.2 queued the trap and never
  dispatched it, so no test-side timeout could bound the stall
  (t_f8e6fe38 measured the healthy path at 0.22–0.48 s of the 10 s budget;
  the stalled path had no internal end). The command is now awaited in
  bounded foreground slices (`while kill -0 "$pid"; do sleep 0.1; done`,
  then a final non-blocking `wait` for the status): a trap deferred by a
  foreground command is the deferral POSIX shells reliably flush at the
  command boundary — the hazard this script's own header had already
  measured (TERM held for a 30 s foreground `sleep`, then RUN) — so
  worst-case trap latency is one 0.1 s poll by construction, on bash 3.2 and
  dash alike. Exit statuses (130/143/129/124/`$?`), the group-kill
  guarantee, and the silence contract are unchanged.

- **`scripts/load_burners.sh` gained an opt-in stage trace for signal-stall
  forensics.** Setting `HSCC_LOAD_BURNERS_TRACE=<path>` appends one line per
  harness stage — `<pid> <tag> <stage> [detail]`, stages `start`, `in-wait`,
  `{int,term,hup}-trap`, `cleanup-entry`, `after-term-loop`, `after-sleep`,
  `after-kill-loop`, `reap-wait <pid>`/`reap-ok <pid>`, `cleanup-exit
  <status>` — which is what localized this stall to the trap dispatch
  instead of the reap loop. Unset ⇒ `lb_trace` returns before any syscall:
  zero behaviour change for every shipped consumer. Marker writes carry
  `2>/dev/null` *before* the append redirection, so a broken trace path
  cannot print what the harness contract forbids; markers never touch
  stdout.
