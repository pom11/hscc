---
name: hscc-agent-coordinator
description: Agent lifecycle FSM, worktree management, recovery, auto-provisioning, and the executor bridge that dispatches HSCC tasks to Hermes kanban workers in isolated git worktrees
category: hscc
domain: agent coordination, lifecycle management
platform: macOS CLI
---

# hscc-agent-coordinator

Manages agent lifecycle state machine, git worktrees, orphan detection, and auto-provisioning of model containers.

## When to use

- Assign a task to an agent (`assign-task`)
- List agents with lifecycle state (`list-agents`)
- Move an agent between states (`update-task`)
- Move a task between agents (`move-task`)
- Detect orphans with no sparkrun container (`detect-orphans`)
- Diagnose and recover failed agents (`attempt-recovery`)
- **Actually run agent work** on a task — dispatch it to a Hermes kanban worker in an isolated git worktree (`dispatch-task` → `release-task`)
- Inspect/stop running work (`task-status`, `cancel-task`, `send-message`)
- Manage worktrees end-to-end (`merge-worktree`, `remove-worktree`, `check-collisions`, `detect-stale`, `green-check`)

## State files

- **Lifecycle**: `~/.hscc/lifecycle.json` — agent states, history
- **Worktrees**: `~/.hscc/worktrees.json` — git worktree assignments
- **Recovery**: `~/.hscc/recovery.json` — immutable recovery ledger
- **Events**: `~/.hscc/events.jsonl` — all lifecycle events (appended by this plugin)
- **Bridge**: `~/.hscc/bridge.json` — HSCC-task → kanban-card dispatch mapping

## Commands

```bash
python3 ~/.hermes/plugins/hscc-agent-coordinator/hscc.py <command> [args]
```

### `assign-task <agent_id> <task_id> [project_id] [branch_slug]`

Assign a task to an agent with full FSM validation, worktree creation, and **auto-provisioning**.

**Flow:**
1. Validates agent exists and is enabled
2. FSM transition: `idle → spawning → running`
3. **Auto-provisions model container** — queries `sparkrun status`, spins up on idle host if needed
4. Wires agent's `model` and `endpoint` in `agents.json`
5. Creates git worktree for the task
6. Marks task in-progress in `projects.json`

**Example:**
```bash
python3 ~/.hermes/plugins/hscc-agent-coordinator/hscc.py assign-task dev-001 task-001
```

### `update-task <agent_id> <state>`

Force-transition an agent to a lifecycle state.

```bash
python3 ~/.hermes/plugins/hscc-agent-coordinator/hscc.py update-task dev-002 idle
```

### `list-agents`

List all agents with lifecycle state summary.

### `move-task <task_id> <new_agent_id>`

Reassign a running task to a different agent.

### `detect-orphans`

Find agents in `running` state with no corresponding sparkrun container.

### `attempt-recovery <agent_id>`

Diagnose and auto-recover failed agents.

### `recovery-log`

View immutable recovery ledger.

### `list-worktrees`

List active git worktrees for agent tasks.

## Executor bridge — actually running agent work

HSCC is bookkeeping; the real execution engine is the **Hermes kanban worker**
(`hermes -p <profile> chat -q`, dispatched by the in-gateway kanban dispatcher).
The executor bridge connects an HSCC project task to that engine, running the
worker inside a pre-created git worktree so every agent works on its own
isolated checkout of the project repo.

**Prerequisite:** the project must have a git repo. Projects created via
`hscc-projects create` are auto-provisioned with a git repo at
`~/.hscc/projects/<id>` and a matching kanban board (`boardSlug`). When there is
agent work to do, **create an HSCC project first**, add the task, then dispatch.

### Guarded two-step flow (default)

```bash
# 1. Mirror the HSCC task as a BLOCKED kanban task + pre-create its worktree.
#    Nothing runs yet — this is the guard.
python3 ~/.hermes/plugins/hscc-agent-coordinator/hscc.py dispatch-task <task_id> [project_id] [profile]

# 2. Explicit "go": unblock the mirror so the dispatcher spawns a worker
#    whose cwd is the isolated worktree.
python3 ~/.hermes/plugins/hscc-agent-coordinator/hscc.py release-task <task_id>
```

`dispatch-task` creates the worktree (`~/.hscc/worktrees/<project>/<agent>-<task>`)
on branch `task/<task_id>-<title-slug>`, mirrors the task onto the project's
kanban board with `--workspace worktree:<path> --initial-status blocked`, and
records the mapping in `~/.hscc/bridge.json`. Because the worktree dir already
exists, the kanban worker's cwd lands inside it. `release-task` then `unblock`s
the card and nudges the dispatcher.

Each worktree gets a `.claw-base` marker file recording the base commit at
creation time; `check-collisions`/`detect-stale` diff against it to find changed
files. `remove-worktree` reaps the bridge entry and (only if the branch was
merged) deletes the `task/*` branch so re-dispatch starts clean.

### Inspect / control running work

```bash
python3 .../hscc.py task-status <task_id> [log_lines]   # kanban status + worker log tail
python3 .../hscc.py send-message <task_id> <message...>  # post a comment the worker can read
python3 .../hscc.py cancel-task <task_id>                # reclaim + block the card, agent → idle
python3 .../hscc.py list-dispatched                      # all bridged tasks
```

### Worktree lifecycle

```bash
python3 .../hscc.py merge-worktree <project_id> <task_id> [--no-ff]  # merge branch into repo default; reports conflicts (never auto-aborts)
python3 .../hscc.py green-check <project_id> <task_id> [-- cmd...]    # run verifier in worktree (verify.sh / make test / npm test / pytest)
python3 .../hscc.py remove-worktree <project_id> <task_id> [--force] # refuses if uncommitted changes unless --force
python3 .../hscc.py check-collisions [project_id]                    # files touched by >1 active worktree
python3 .../hscc.py detect-stale [project_id] [--hours N]            # abandoned worktrees (no commits + old, or agent finished/idle)
```

**Recommended sequence to land work:** `green-check` → `merge-worktree` →
`remove-worktree`. Profiles default to `default` unless the agent record has a
`profile` field or you pass one explicitly.

## Lifecycle FSM

```
idle     → spawning, ready, running, finished, failed, disabled
spawning → ready, running, failed, idle, disabled
ready    → ready, running, failed, idle, disabled
running  → finished, failed, idle, disabled
finished → idle, spawning, running, disabled
failed   → idle, spawning, disabled
disabled → idle
```

## Auto-Provisioning Integration

When `assign-task` reaches the "running" state, it automatically:

1. Calls `sparkrun status` to check if the recipe's container is already running
2. **If running**: extracts the host IP from the sparkrun output, wires the agent to it
3. **If not running**: provisions a new container on the first idle host via `sparkrun run`
4. **If no idle hosts**: reports error, agent assigned but provisioning failed
5. Updates `agents.json` with the correct `model`, `endpoint`, and `status`

**Recipe matching**: Uses the agent's `model` field if set (not "auto"), otherwise defaults to `qwen3.6-35b-a3b-fp8-vllm`.

**Model endpoint format**: `vllm-192-0-2-244/Qwen/Qwen3.6-35B-A3B-FP8` with endpoint `http://192.0.2.10:8000`.

## Guard Rules

- **max_running**: Maximum 3 concurrent running agents
- **no_duplicate_task**: Reject if another agent already has this task_id running
- **FSM validation**: Only valid state transitions are allowed
- **Agent must be idle**: Cannot assign to an agent in `running`, `failed`, etc.

## Pitfalls

- **FSM state re-read**: After `set_lifecycle()` in `assign-task`, MUST re-read `lifecycle.json` to get the updated state. The in-memory copy from before the transition is stale.
- **Sparkrun status parsing**: Never parse host IPs by counting dots in "Job:" lines — file paths like `.sparkrun-local/recipes/official/qwen3.6-35b-a3b-fp8-vllm.yaml` contain dots and will be falsely identified as IPs. Always parse hosts from the "Idle hosts" section only, and extract IPs from the line AFTER the "Job:" line (the `solo IP Up ...` line).
- **Path expansion**: Always use `os.path.expanduser()` for `~` paths in subprocess calls. Shell won't expand `~` inside Python subprocess arguments.
- **Failed agent state**: Agents transition to "failed" state and become ineligible for new assignments. Must explicitly `update-task <id> idle` before reassignment.
- **Legacy state fallback**: If `~/.hscc/lifecycle.json` has no agents, the code falls back to `~/.hscc/plugin-state/hscc-lifecycle.json`.
- **vLLM content null (reasoning-parser bug)**: If Qwen3.6 vLLM returns `content: null` with output in `reasoning` field, the sparkrun recipe has `--reasoning-parser qwen3` set. This intercepts ALL output. Fix: remove `--reasoning-parser` from recipe defaults/command.
- **Idle monitor kills unassigned containers**: The `model-idle-monitor.py` daemon (cron every 5m) stops sparkrun containers that have no assigned task. When provisioning new agent containers, **pause the idle monitor first** (`cronjob action=pause`), then register agents in `agents.json`, then provision. Otherwise unassigned containers get killed within 5 minutes. Resume the cron after all agents are assigned.

- **Model idle monitor**: Daemon at `~/.hermes/plugins/install/hscc-plugins/hscc-agent-coordinator/scripts/model-idle-monitor.py` automatically stops idle models every 5 min. Protected: MTP gateway container. Configurable timeout: `HSCC_IDLE_TIMEOUT_MINUTES` (default 30). Cron job `9508e87f9729` runs scans continuously.

- **Recipe YAML changes require full restart**: Patching a sparkrun recipe file does NOT affect running containers. The container was built with the old command at startup time. After modifying ANY recipe field (command, defaults, env), MUST stop the old container (`sparkrun stop <id>`) and restart it fresh. Otherwise it continues running with the old parameters.

- **agents.json model field IP format**: The `model` field in agents.json uses format `vllm-<IP>/<model-name>`. IP addresses MUST use dots (e.g. `vllm-192.0.2.11`), NOT dashes (`vllm-192-0-2-246`). The idle monitor regex `r"vllm-(\d+\.\d+\.\d+\.\d+)"` expects dots and will NOT match dash-separated IPs. This causes containers to be treated as orphans and killed.

- **Multi-node provisioning sequence**: When provisioning containers for multiple agents:
  1. **Pause idle monitor cron** first (`cronjob action=pause`)
  2. Provision all containers (`sparkrun run <recipe> -H <host>`)
  3. Assign tasks to agents (`assign-task <id> <task>`)
  4. Verify API endpoints return correct `content` field
  5. **Resume idle monitor cron** last
  Steps 2 and 3 must be done quickly between idle monitor scans, or containers will be killed as "orphans" (no agent references yet).

- **Sparkrun `mods:` section with missing directories**: Recipe YAML may reference local `mods/` directories that don't exist on the control node. These are silently ignored by sparkrun CLI but the recipe should be kept clean. If mod files are needed, they must be created in `~/.sparkrun-local/recipes/<registry>/mods/` relative to the recipe's base path.

- **Container startup timing**: vLLM containers take 1-3 minutes to load models and become API-ready. After `sparkrun run`, the container shows as "Up" in `sparkrun status` immediately (that's just docker container uptime). MUST verify API is actually serving (`curl http://<ip>:8000/v1/chat/completions`) before testing or assigning tasks. Check container logs for "Application startup complete" or use `docker exec <container> tail -10 /tmp/sparkrun_serve.log`.

## Full Lifecycle Test Pattern

When testing a new agent+model combination end-to-end:

1. **Provision model** — `hscc-provision run <recipe> [host]`
2. **Verify endpoint** — `hscc-provision health [host]`
3. **Update lifecycle** — `update-task <agent_id> idle` then `spawning`
4. **Assign task** — `assign-task <agent_id> <task_id>`
5. **Monitor state** — `list-agents` to confirm `running → finished → idle`
6. **Stop model** — `hscc-provision stop <container_id>`
7. **Clean agent state** — agent auto-transitions to `idle` on task completion

**Output retrieval:** If `content: null` appears on the API response, diagnose the `--reasoning-parser` flag in the sparkrun recipe before falling back to manual generation. See `references/vllm-qwen36-response-quirks.md`.

## Cross-references

- **hscc-provision** — auto-provisioning uses `hscc-provision` plugin to spin up containers. Always verify vLLM is serving before assigning tasks.
- **hscc-cluster** — cluster status provides idle host information
- **hscc-orchestrator** — agent definitions come from the same `agents.json` source
- **hscc-events** — all lifecycle events are emitted to the event bus

## References

- `references/multi-node-provisioning.md` — Full multi-node provisioning workflow with pitfalls
- `references/vllm-qwen36-response-quirks.md` — Qwen3.6 reasoning parser bug
- **references/vllm-qwen36-response-quirks.md** — `--reasoning-parser qwen3` bug: recipe flag causes `content: null`; fix is to remove the flag. Includes diagnostic script.
- **references/vllm-reasoning-parser-bug.md** — Detailed troubleshooting for the vLLM reasoning-parser bug: symptom, fix, verification, and critical container restart requirement.
- **references/multi-node-provisioning.md** — Complete 7-step sequence for multi-node vLLM provisioning: pause monitor → register agents → provision → verify API → assign tasks → resume monitor.
