# rc=143 mid-suite SIGTERM — root-cause investigation (t_6bb29d46)

Status: IN PROGRESS (committed incrementally per checkpoint discipline; findings land as evidenced).

## Incident timeline (from the card + supervisor comments)

All times 2026-10-06, EEST.

| # | ~time  | victim | context |
|---|--------|--------|---------|
| 1 | 03:38  | p313 final-suite run1 | t_163fa09f implementer worker |
| 2 | 03:38  | py311 final-suite run1 | same worker (re-ran later green) |
| 3 | 07:00  | GATE_PY311 (~20% in) | t_163fa09f reviewer landing gate (re-run green) |

## Findings so far (evidence)

### E1. Incident #3 is explained: reviewer's own deliberate `process.kill`

`~/.hermes/profiles/hscc-orch/logs/process-results/proc_6819ed006111.json` (session
`20261006_063332_4d7f3d` = the reviewer run):

```
exit_code = -15, completion_reason = "killed", termination_source = "process.kill",
started_at 06:59:31, mtime 07:00:53, command: export HERMES_HOME=...; cd .../gate...
```

This matches the reviewer's own comment on t_163fa09f: "I first launched both legs
concurrently — my own anti-perturbation rule for this card — killed the py311 leg at
rc=143 and re-ran it after p313 finished". Same pattern at 07:36:59 for the t_9462260b
gate leg (`proc_ef3f0286fb45.json`, exit -15, `process.kill`, session 20261006_032327_2b3047).
=> Incident #3 and the t_946 leg were harness-mediated kills (agent called the process
tool / close), NOT the mystery reaper.

### E2. Incidents #1/#2 were NOT harness-mediated

`~/.hermes/profiles/backend-engineer/logs/process-results/`:

- `proc_3eaf4d0b12bb.json` — wrapper started 03:31:30: `for i in 1 2; do ... HSCC_TEST_PY=$HOME/miniconda3/envs/p313/bin/python timeout 2400 bash scripts/run_tests.sh > /tmp/p313_final_run$i.log ...; done`
- `proc_dc13af937397.json` — same shape for py311, started 03:31:54.

Both wrappers: `exit_code: 0`, `completion_reason: "exited"`, `termination_source: ""` —
the registry never killed anything. Their captured stdout reads
`p313 run1 rc=143 ... p313 run2 rc=0 ... ALL P313 DONE` (and py311 likewise).
So the OUTER wrapper survived; only the inner `timeout 2400 bash scripts/run_tests.sh`
received SIGTERM at 03:38:44 / 03:38:52 (log mtimes) — ~430 s into otherwise-green runs.

Mechanics that narrow the killer:

- `run_tests.sh` aggregates suite failures to rc=1, never 143. rc=143 = the bash process
  itself (or its `timeout` parent) died by SIGTERM (128+15), not a pytest failure.
- The wrapper survived a signal that killed its child ⇒ classic **process-group signal**
  (SIGTERM to the group; the interactive shell wrapper ignores SIGTERM — hermes' own
  process_registry comment documents "the interactive `bash -lic` wrapper" ignoring
  SIGTERM), or a `pkill -f` that matched `bash scripts/run_tests.sh` / `timeout ... run_tests.sh`
  command lines but not the for-loop wrapper line.
- NOT `timeout` firing: budget was 2400 s, death at ~430 s.
- NOT the gateway/registry paths: any registry kill sets termination_source
  ("process.kill"/"agent_close") and marks completion_reason "killed" — absent here.

### E3. Candidates ruled out so far (read-only checks)

| Candidate | Verdict | Evidence |
|---|---|---|
| macOS sleep/App Nap | ruled out | `pmset -g log` 03:20–03:49: no sleep entries; machine awake (audio assertion active 03:13–03:43) |
| launchd system teardown/reboot | ruled out | `log show` 03:30–03:45: only unrelated AppKit sudden-termination + unilog SIGKILLs; no SIGTERM of our tree; host up continuously |
| hermes `kill_live_foreground_processes` (exit funnel) | ruled out for #1/#2 | those were BACKGROUND registry procs whose parent (worker) never exited; registry shows wrapper exited naturally |
| hermes registry kills (`process.kill`, `agent_close`, abandoned-turn reap) | ruled out for #1/#2 | registry JSON shows `termination_source: ""` + `completion_reason: "exited"`; the two same-day incidents that ARE registry kills are clearly marked (E1) |
| kanban dispatcher reclaim / kill_switch | ruled out | would SIGTERM the worker pid itself; supervisor confirmed worker pid 72995 alive across the window (its 04:43 tick: "alive 6h16m") |
| HSCC repo scripts (worker_health, worker_progress, flake_hunt, escalate_watcher, recover_worker_work, watchdogs) | ruled out | grep: no kill/pkill/SIGTERM in any of them |
| hermes lsp reaper | ruled out | agent.log 03:40:37 reaped only an idle pyright LSP client (600s idle policy) |

### E4. What WAS alive at the kill instant (correlation, not yet causation)

- worker session `20261005_222644_216d50` (backend-engineer): polls its own background
  legs; receives `/steer` "New note on your kanban task from the operator" at 03:38:57
  (the hscc-orch supervision-tick comment) — text-only injection, cannot signal.
- hscc-orch `cron_f568ed163281_20261006_020225` ("hscc-hygiene-supervisor"):
  03:38:36 posts two kanban_comments, then at 03:38:56 opens a fresh terminal env
  (cwd=/private/tmp/ow_landed_check) and runs a 1.09s command — content not yet identified.
  Kill instants were 03:38:44 / 03:38:52, i.e. bracketed by the supervisor's activity.

## Open questions / next steps

1. Identify the exact sender of the two group-SIGTERMs at 03:38:44/03:38:52.
   Leading hypothesis: a `pkill -f`-style sweep by an agent-run command (hygiene-supervisor
   landed-check or a watchdog), matching `run_tests.sh` in the inner command line.
   Check: supervisor scratch scripts, `ow_landed_check` command content, hscc-orch cron job
   body, any `pkill` in agent-session transcripts around 03:38.
2. Cheap repro (sleep/loop, NOT suite-based): monitored long child with SIGTERM trap
   logging pid/ppid/kill-time + `ps -o ppid` of sender via kqueue/`log stream`; per card
   constraint, no full suites while t_9462260b verifies.
3. Minimal guard: SIGTERM trap + killer-identity capture in `scripts/run_tests.sh`
   (trap writes pid/ppid/parent-cmd/time to the log), so the next occurrence names the killer.

## Guard

(pending — will land in scripts/ + this doc)
