# rc=143 mid-suite SIGTERM — root-cause investigation (t_6bb29d46)

**RESOLVED.** There is no reaper. All three rc=143 aborts were kills issued by the
agent sessions themselves — two by `pkill -f "run_tests.sh"` typed by the worker into
its own terminal tool to restart its verification legs, one by the harness
`process.kill` the reviewer used to serialize its gate legs. Each incident is closed
with primary evidence below. The landed guard makes any future rc=143 self-attribute
in the log within the same suite run, and this doc carries the forensics recipe that
names the sender in minutes.

## Incident timeline (2026-10-06, EEST)

| # | ~time  | victim | context |
|---|--------|--------|---------|
| 1 | 03:38  | p313 final-suite run1 | t_163fa09f implementer worker |
| 2 | 03:38  | py311 final-suite run1 | same worker (re-ran later green) |
| 3 | 07:00  | GATE_PY311 (~20% in) | t_163fa09f reviewer landing gate (re-run green) |

## Root cause — incidents #1 + #2: the worker's own `pkill`

Primary evidence (read-only queries against profile state DBs + harness logs):

1. `~/.hermes/profiles/backend-engineer/state.db`, the t_163fa09f implementer session
   (backend-engineer profile, started 2026-10-05 22:26), message **58989**,
   03:38:55.056, assistant text:
   *"Two runs are in flight; I need to fold in a design cleanup first (it changes
   conftest for every test). Killing and restarting after the edit."* — tool_calls on
   the same row carry the command, verbatim:
   `pkill -f "run_tests.sh" 2>/dev/null; pkill -f "pytest -q /Users/desac/.hermes/kanban/boards/hscc/workspaces/t_163fa09f" 2>/dev/null; sleep 2; pgrep -fl "run_tests.sh" | head -3; echo "stopped"`
2. Its tool result (msg **58990**, 03:38:57.393) shows the post-kill `pgrep`: the two
   `/bin/zsh -lic ... for i in 1 2; ... timeout 2400 bash scripts/run_tests.sh ...`
   wrappers **alive** and the inner `timeout 2400 bash scripts/run_tests.sh` listed —
   the signature of the sweep having landed (see mechanics below).
   `agent.log` times the call at 2.30 s completing 03:38:57,392 ⇒ execution ≈03:38:55.1.
3. Wrapper process-results `proc_3eaf4d0b12bb.json` / `proc_dc13af937397.json`
   (backend-engineer logs/process-results): `termination_source: ""`,
   `completion_reason: "exited"`, exit 0, captured stdout
   `p313 run1 rc=143 ... p313 run2 rc=0 ... ALL P313 DONE` (py311 same shape).
   The registry never killed these; they completed naturally after the sweep.
4. The worker itself later noticed: session DB msg 58995 reasoning (03:40:33):
   *"Wait — why did I kill the runs? My message said ... I need to actually decide
   what that cleanup is."* The kill was intentional (restart both legs after a
   conftest refactor), and the same worker's own follow-up reasoning shows it had
   already lost the plot on it — which is exactly why every observer downstream read
   rc=143 as "mystery reaper".

### Kill mechanics (why exactly run1 died and run2 ran)

The launched shape was `zsh -lic '... for i in 1 2; ... timeout 2400 bash
scripts/run_tests.sh > log; done'` inside the harness background-registry wrapper:

- `pkill -f "run_tests.sh"` matches **every** process whose full argv contains that
  string: the `timeout` parent and the inner `bash scripts/run_tests.sh`. macOS sends
  SIGTERM per-pid to each match.
- `timeout` forwards TERM to `bash scripts/run_tests.sh` → that shell dies → the
  `run_tests.sh` log ends mid-suite with rc 128+15 = **143** echoed by the surviving
  loop (`p313 run1 rc=143`).
- The `zsh -lic` wrapper's argv also matches — but **interactive zsh ignores
  SIGTERM** (same documented behaviour hermes' `process_registry` relies on for
  `bash -lic`), so the wrapper survives and immediately starts run 2. That survival
  is what made the pair look like "the infra killed run1 and let run2 through".
- Log mtimes (03:38:44 / 03:38:52) predate the pkill because the logs were written
  through block-buffered redirects: the mtime records the last buffer flush, not the
  moment of death. The pkill remains the only actor in the window.

The card's "sleep/loop repro" was done as a controlled mechanism repro instead of a
passive 20-min watch: `scripts/audits/rc143_guard_selftest.sh` **Test B** re-launches
the real `run_tests.sh` (pytest shadowed by a sleep stub — zero tests, safe next to
t_9462260b) in the exact incident shape and issues an incident-shaped
`pkill -f "bash <path>"`. Result: inner leg rc=143, wrapper survives and reports it —
byte-for-byte the incident behaviour (PASS, this session, 2026-10-06 ~08:4x).

## Root cause — incident #3: reviewer's own `process.kill` (harness-mediated, marked)

`~/.hermes/profiles/hscc-orch/logs/process-results/proc_6819ed006111.json` — owner is
the hscc-orch reviewer run for t_163fa09f (session_key field in that JSON):
`exit_code: -15`, `completion_reason: "killed"`, **`termination_source: "process.kill"`**,
started 06:59:31, finished 07:00:53. This matches the reviewer's own t_163fa09f
comment: "I first launched both legs concurrently — my own anti-perturbation rule for
this card — killed the py311 leg at rc=143 and re-ran it after p313 finished". Same
clear pattern at 07:36:59 for a t_9462260b verify leg (`proc_ef3f0286fb45.json`, exit
-15, `process.kill`; recorded in the BACKEND-ENGINEER profile's process-results — the
leg ran as a background job of the t_9462260b implementer session with
`HERMES_HOME=<backend-engineer>`). Harness-mediated kills set
`termination_source`; the 03:38 pair has it empty — different mechanism, and both
explained.

## Ruled-out matrix (all checked read-only on 2026-10-06)

| Candidate | Verdict | Evidence |
|---|---|---|
| macOS sleep / App Nap | ruled out | `pmset -g log` 03:20–03:49: no sleep entries; machine awake (audio assertion active until 03:43) |
| launchd session/system teardown | ruled out | `log show` 03:30–03:45: only unrelated AppKit sudden-termination + unilog SIGKILLs; no kill of our tree; host up |
| hermes exit funnel `kill_live_foreground_processes` | ruled out | victims were BACKGROUND registry procs; worker parent never exited in the window (registry shows wrappers exited 0) |
| hermes registry kills (`process.kill`, `agent_close`, abandoned-turn reap) | ruled out for #1/#2 | `termination_source` empty + `completion_reason: "exited"` on both victims; the same-day kills that ARE registry kills are clearly marked (incident #3) |
| kanban dispatcher reclaim / HSCC kill_switch | ruled out | both SIGTERM the *worker pid*; supervisor confirmed worker pid 72995 alive 6h16m across the window; no `terminal_worker_reaped` event in 03:38 |
| HSCC daemon (pid 24940) watchdogs | ruled out | `~/.hscc` has no logs dir; daemon-side kill primitives (kill_switch.py) target worker pids of running cards, not children, and were not invoked (no matching board events) |
| repo scripts (worker_health, worker_progress, flake_hunt, escalate/depr watchers, watchdogs) | ruled out | grep: none contains kill/pkill/SIGTERM/killpg |
| cron jobs / LaunchAgents | ruled out | crontab empty; non-Apple LaunchAgents are hermes gateway / hscc_daemon / tg-mcp / model-shim / display only; hermes cron holds dep-watcher + hygiene-supervisor only |
| hygiene-supervisor (active 03:38:36–56) | ruled out | its cron-run session's tool_calls in the window: two kanban_comments + a 1.09 s `git worktree remove` (cwd /private/tmp/ow_landed_check). No kill verb. Its "landed check" was a `pytest test_doctor.py` run (targeted, disjoint command lines) |
| gateway inactivity/turn reaper (`_reap_gateway_turn_processes`) | ruled out | logs zero "Reaped"/interrupt events for the session all day; also only targets registry-tracked procs created by an abandoned turn |
| OOM / jetsam | ruled out | zero failures in output to the abort point; machine load normal; no jetsam entries in the window |
| `timeout 2400` firing | ruled out | budget 2400 s, death at ~430 s; `timeout` with TERM-forwarding would report differently and only 1 leg per interpreter |
| operator `pkill` from a real terminal | ruled out | `~/.zsh_history` contains no kill/pkill commands; the only pkill actor in every state.db in the window is the worker session itself (msg 58989) |

## Guard landed (deliverable 2)

`scripts/run_tests.sh` now traps SIGTERM: the log itself prints
`!! run_tests.sh RECEIVED SIGTERM at <time> — elapsed Ns, during suite: <X> ...
This is an EXTERNAL KILL (shell rc=143), NOT a test failure` plus pid/ppid/parent
command, then exits 143. A POSIX shell cannot read the sender pid from the signal
(macOS delivers no si_pid to shell traps); sender identity is recovered via the
recipe below, and the note now tells the reader to look there instead of at
infra. Verification:

- `scripts/audits/rc143_guard_selftest.sh` (sleep-based only; safe while t_9462260b
  verifies) Test A: per-pid kill ⇒ note + rc 143. Test B: incident-shaped
  `pkill -f "bash <path>"` through `zsh -lic` + `timeout 2400` ⇒ inner rc=143,
  note in log, wrapper survives. Both PASS this session.
- `scripts/audits/ws_relay_final_verify.sh` (the stamped wrapper) calls
  `run_tests.sh` and records its rc per leg ⇒ it inherits the guard; a killed leg
  now stamps "RECEIVED SIGTERM ... rc: 143" inside the commit-stamped log.

## Forensics recipe — naming a killer in minutes (next occurrence)

1. `logs/process-results/proc_*.json` in the owning profile: `termination_source`
   non-empty ⇒ harness-mediated kill, session_id names the issuer. Empty + victim is
   a registry child ⇒ external signal; go to 2.
2. Read-only query every profile's `state.db`:
   `sqlite3 -readonly <db> "SELECT id,session_id,timestamp,substr(tool_calls,1,400)
   FROM messages WHERE role='assistant' AND tool_calls LIKE '%pkill%' AND
   timestamp BETWEEN <lo> AND <hi>"` — sweeps announce themselves; check the
   assistant `content` for the "why".
3. Cross-check the victim wrapper's captured stdout (`run$i rc=143` vs
   `ALL ... DONE`) to see what survived and continued.

## Operational note (prevents the next incident — documented, not enforced)

`pkill -f "run_tests.sh"` is unsafe on this host: it hits **other cards' concurrent
suite legs** (the 03:38 sweep would have murdered t_267f9d88's legs had any been
running) and hits the harness registry's wrappers too. To stop your own suite: prefer
the harness `process.kill` (leaves `termination_source` behind), or `kill <pid>` on
the specific registry pid / `timeout` pid from `pgrep -fl "<full worktree path>"`.
Name-based sweeps on generic script names should be treated as cross-session
sabotage, not hygiene.

## Cost recap

~1 worker-hour per occurrence was re-run time; the bigger cost was board-wide
misdiagnosis ("reaper" theory, supervisor warnings, this card itself). The trap note
plus the recipe above make the next rc=143 self-explaining in one log read.
