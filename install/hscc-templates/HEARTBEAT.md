# HEARTBEAT.md — HSCC Periodic Checks (every 30 min)

## CRITICAL: You are an ORCHESTRATOR — NEVER edit source files
- NEVER use read/write/edit tools on .swift, .ts, .js, .py, .sh files
- NEVER touch files in ~/.hermes/plugins/Sources/ or ~/.hscc/workspaces/
- Your ONLY job: health check → project scan → dispatch workers via hscc_* tools
- ALL coding work goes to worker agents on Spark 2

## Step 1: Health Check (fast, <30s)
- `curl -sf http://{{dgxIP}}:8000/v1/models` — Spark 1 serving?
- `curl -sf http://192.168.1.202:8000/v1/models` — Spark 2 serving?
- If a node is down: restart container via SSH, redeploy model
- `sparkrun cluster status --cluster hscc` — full cluster health
- NEVER install anything on DGX nodes — sparkrun manages from Mac

## Step 2: Project Scan
- `hscc_list_projects` — find all active projects
- `hscc_get_tasks_by_status status=inProgress` — check for stuck tasks
- `hscc_get_tasks_by_status status=backlog` — count remaining work

## Step 3: Check Worker Status — DO NOT dispatch if workers are running
- `hscc_fleet_status` — check if ANY agent is in running/spawning state
- **If a worker is running: DO NOTHING. Log "Worker active, skipping dispatch" and go to Step 4.**
- **CRITICAL: 1 worker at a time.** Each Spark has 1 inference slot. Concurrent workers thrash KV cache.
- Do NOT create new agents — fleet is pre-registered (dev-001 to dev-020 + merge-001).
- If NO workers running AND backlog tasks exist:
  - `hscc_dispatch_task` with create_worktree=true, repo_path=~/.hermes/plugins — dispatch ONE task only
  - `hscc_check_task_output` — poll until done
  - Review result by checking task output ONLY — do NOT read source files
  - After completion: dispatch merge-001 to squash-merge, then dispatch next task
- If worker is stuck (running >30 min with no message progress): investigate but do NOT dispatch another

## Step 4: If No Backlog
- Log "Heartbeat OK — N nodes up, 0 backlog" and stop

## If Something Breaks
1. Diagnose: `sparkrun cluster status --cluster hscc` and check container logs
2. Try auto-fix: restart container via SSH
3. If fix fails after 2 attempts: notify user with error details
4. Document the incident in today's memory note
