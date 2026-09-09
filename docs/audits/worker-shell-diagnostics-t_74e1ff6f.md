# Report: Kanban worker shell diagnostics (task t_74e1ff6f)

Task: Report (1) the `backend-engineer` profile's configured toolset, (2) last 60
lines of `~/.hermes/kanban/boards/hscc/logs/t_e8a61c8e.log` (case 1), (3) last 60
lines of `t_61f27161.log` and `t_9e66d919.log` (case 2 cards), and (4) the files in
`~/.hermes/profiles/backend-engineer/` plus the contents of its `config.yaml`.

Gathered fully; contents printed verbatim in the delivery message. Key findings
below.

## (1) Toolset of the `backend-engineer` profile

From `~/.hermes/profiles/backend-engineer/config.yaml`:

```
toolsets:
- hermes-cli
- kanban
- web
- browser
- terminal
- file
- code_execution
- vision
- skills
- todo
- memory
- session_search
- clarify
- delegation
- cronjob
- messaging
skills:
  preload:
  - test-driven-development
  - verification-before-completion
model:
  default: worker-model
  provider: custom
  base_url: http://localhost:4000/v1
  api_key: «redacted»
```

**Key discrepancy:** the config DECLARES `terminal`, `file`, and `code_execution`
toolsets, but every worker session logs that it has NO usable shell/file/execution.
This config-vs-reality mismatch is the crux of the bug.

## (2) t_e8a61c8e.log (case 1) — core finding

Worker blocked with `kanban_block(kind=capability)`. Only execution path was
`browser_exec`, whose harness daemon never starts ("browser-harness: daemon
default didn't come up"). `computer_use` keyboard input to iTerm2 required an
approval that timed out. Deliverable files written on disk but uncommitted
(no shell to git-commit). Full contents reported verbatim in delivery message.

## (3) t_61f27161.log + t_9e66d919.log (case 2 cards) — core finding

Both blocked with `kanban_block(kind=capability)`. Same infrastructure wall:
- `browser_exec` harness daemon fails to start on every call.
- `execute_code` blocked in single-query mode ("runs arbitrary local Python ...
  Single-query mode (-q) runs without a user present").
- `computer_use` input actions require interactive approval that times out.
- No standalone read_file/write_file/patch/terminal tool in the session toolset.
- A `delegate_task` child context cannot touch the board either ("Children cannot
  call delegate_task, clarify, memory, cronjob, kanban tools").

The card also logged that a child context cannot restore shell access because the
toolset/harness is shared and broken at the infrastructure level.

## (4) Profile directory + config.yaml

Files in `~/.hermes/profiles/backend-engineer/`:
`.env`, `.skills_prompt_snapshot.json`, `.update_check`, `SOUL.md`,
`SOUL.md.bak-20260801-083710`, `audio_cache/`, `auth.lock`, `bin/`, `cache/`,
`config.yaml`, `config.yaml.bak-20260801-083710`, `config.yaml.bak-dsv4sw`,
`config.yaml.bak.1786021714`, `config.yaml.bak.1786266504`,
`config.yaml.bak.1787724050`, `config.yaml.bak.1788892503`, `cron/`,
`gateway_state.json`, `hooks/`, `image_cache/`, `logs/`, `lsp/`, `memories/`,
`pairing/`, `platforms/`, `plugins/`, `processes.json`, `profile.yaml`,
`sandboxes/`, `sessions/`, `skills/`, `state.db` (+ `state.db-shm`, `state.db-wal`),
`terminal-sessions/`, `update-check.yaml`, `verification_evidence.db`.

`config.yaml` (39 lines) printed verbatim in the delivery message; reproduced here
(api_key redacted by config):
```yaml
toolsets:
- hermes-cli
- kanban
- web
- browser
- terminal
- file
- code_execution
- vision
- skills
- todo
- memory
- session_search
- clarify
- delegation
- cronjob
- messaging
skills:
  preload:
  - test-driven-development
  - verification-before-completion
model:
  default: worker-model
  provider: custom
  base_url: http://localhost:4000/v1
  api_key: «redacted»
compression:
  threshold: 0.8
  threshold_tokens: 100000
auxiliary:
  compression:
    provider: custom
    model: orchestrator-model
    base_url: http://100.64.0.1:8000/v1
    api_key: «redacted»
    timeout: 90
onboarding:
  seen:
    tool_progress_prompt: true
```

## Terminal-state blocker (this run)

This reporting run is itself dispatched as a `delegate_task` CHILD context. The
kanban kernel hard-rejects board access from child contexts:
`"delegate_task child contexts cannot mutate Kanban tasks or boards"` — this
applies to `kanban show`/`complete`/`block` alike, so no board terminal action is
possible from here. This is the same constraint the worker logs reference. The
deliverable (this report) is complete and written to disk here; the board task
itself cannot be completed/blocked from this child context and must be resolved by
the parent/orchestrator or an operator session.
