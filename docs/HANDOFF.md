# HANDOFF — resume the iOS app work

**Written:** 2026-09-01 19:00 · **origin/main at writing:** `ebc3bde`

If a session ended mid-flight, start here. The cluster keeps working without a
Claude session — kanban workers are independent processes driven by the Hermes
gateway dispatcher. What stops is REVIEW: cherry-picking finished cards,
verifying them by execution, and shipping. That is the job to resume.

## Resume in one command

```
cd ~/dev/hscc
python3 - <<'P'
import sqlite3, os
c = sqlite3.connect(os.path.expanduser('~/.hermes/kanban/boards/hscc/kanban.db'))
for r in c.execute("select id,status,assignee,title from tasks "
                   "where status not in ('done','archived') order by priority desc"):
    print(r)
P
```

Anything `done` since the last ship needs verifying and merging.

## The seven cards dispatched 2026-09-01 ~18:55

| id | what |
|---|---|
| `t_3d0b33d7` | guard: every profile base_url must be an endpoint serving.json actually serves |
| `t_2d9937d5` | live route sweep — prove every endpoint the app calls answers |
| `t_3e152ac7` | audit: client sends the params each server handler requires |
| `t_d61e9cbd` | harness: decode REAL live API responses through the Swift models |
| `t_b55400ea` | audit: every screen's empty / loading / error state |
| `t_cf296e48` | decide + act on the five never-referenced Swift types |
| `t_15a88458` | review widget + both Live Activity targets |

## How to verify a finished card — non-negotiable

This project's failure mode is **code that compiles clean and is wrong at
runtime**. Compile-green proves almost nothing here.

1. `MB=$(git merge-base dev <branch>)` then cherry-pick **every** commit in
   `$MB..<branch>`. Branch names are not always `wt/<id>`.
2. Re-run the worker's OWN evidence yourself. Three times during the audit a
   "CLEAN" claim was true on the worker's base and false on current dev.
3. Check the LOAD-BEARING claim in the code, not the summary.
4. iOS: `build_check.sh` (fails on warnings now), plus `check_sources`,
   `model_decode_check`, `chat_state_check`, `streaming_check`,
   `session_activity_check`, `first_run_check`, `reconnect_check`, `check_theme`.
5. Python: `HSCC_TEST_PY=/Users/desac/miniconda3/envs/p313/bin/python bash scripts/run_tests.sh`
   — **run it backgrounded**, the Bash tool caps at 10 minutes.
6. Leak-check COMMITTED blobs before pushing, never the working tree:
   `for f in $(git diff origin/main..dev --name-only); do git show dev:"$f" | grep -qE '100\.115\.[0-9]+\.[0-9]+|192\.168\.88\.[0-9]+' && echo LEAK: $f; done`
7. Confirm `git rev-parse --abbrev-ref HEAD` is `dev` before any git write — a
   worker can check its branch out in the main checkout.
8. Push `dev:main`, confirm with `git ls-remote origin main`.
9. Deploy only if runtime behaviour changed: `bash hscc-bootstrap/bootstrap.sh --yes`.

## Live smoke test — is chat actually working?

The single most useful check. It found the outage that made the app look dead:

```
TOK=$(cat ~/.hscc/api-token | tr -d '\n')
curl -s -X POST http://100.115.243.3:8788/v1/orchestrator/chat \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"project":"hscc","prompt":"Reply with exactly: alive","confirm":true}'
# then poll /v1/orchestrator/chat/<job_id> until status is done
```

Expect `queued -> running -> done` with a real reply in ~30s. If it sits at
`running` forever, check where the hermes process is connecting:
`lsof -nP -p $(pgrep -f 'hermes -p .*-orch chat' | head -1) | grep TCP` — a
`SYN_SENT` to a host that is not in `~/.hscc/serving.json` is the bug.

## Known state

- Fleet: DeepSeek-V4-Flash-0731, orchestrator `.244/.246`, worker `.247/.248`.
  **No vision** — confirmed by the engine: *"is not a multimodal model"* (400).
  The Vision-Exp variant is not usable: vLLM has no native support yet.
- Invariants: autodown ARMED 120 · kanban caps 6/3 ·
  `max_concurrent_children` 9 · 40 profiles all `threshold_tokens: 100000`.
- Do NOT raise kanban width past 6 — all workers share one worker-model on one
  TP pair, so more width only deepens the vLLM queue.
- `~/.hermes/profiles-backup-20260901-183205` is the pre-repair profile backup.

## Traps that have each cost real time

- **Merging is not deploying, and deploying is not running.** The daemon runs the
  installed payload under `~/.hermes/plugins`, not the repo.
- **Never hardcode a host in a test.** A repo-wide address scrub rewrote a
  default and its test expectation together; the suite stayed green while every
  orchestrator profile pointed at a dead host and chat hung for days.
- A card that wedges 3+ times with no commits is a **card-scoping** failure.
  Split it or do its highest-value part by hand — that is how the twelve
  escaped-interpolation bugs were found.
- A `blocked` card usually holds good work. Read the branch, not the status.
- `git worktree list` has 200+ entries — always grep for the task id.

## What the operator still has to do

There is no iOS runtime or simulator on this host. Device build/install,
ActivityKit, App Intents, extension runtime, real network behaviour, dark mode,
accessibility, camera capture and on-device Keychain are all UNPROVEN.
`ios-app/docs/DEVICE-SMOKE-CHECKLIST.md` (54 items) is the gate.
