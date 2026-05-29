# Multi-Node Agent Provisioning Workflow

## When to use
Provision Qwen3.6 (or other models) across multiple cluster nodes for parallel agent work.

## Steps

1. **Register agents** in `~/.hscc/agents.json` before starting containers
   ```json
   {"dev-005": {"model": "Qwen/Qwen3.6-35B-A3B-FP8", "host": "192.0.2.11", ...}, ...}
   ```
   
2. **Pause idle monitor** during bulk provisioning to prevent false-positive kills
   ```
   cronjob action=pause job_id=<idle-monitor-job-id>
   ```

3. **IP format**: Always use dots (`192.0.2.11`), not dashes (`192-0-2-246`)
   - The idle monitor uses regex to match IP patterns
   - Dash-separated IPs won't be matched for lifecycle tracking

4. **Fix recipe before provisioning**:
   - Remove `--reasoning-parser qwen3` → causes `content: null` bug
   - Remove `--chat-template unsloth.jinja` → file doesn't exist in container
   - Commit: `git add -A && git commit -m "fix: ..."`

5. **Start containers**:
   ```bash
   python3 ~/.hermes/plugins/hscc-provision/hscc.py assign <agent> <recipe> <host>
   ```

6. **Wait for vLLM to load** (30-60 seconds for model loading)

7. **Verify readiness** before assigning tasks:
   ```bash
   python3 ~/.hermes/skills/hscc-provision/scripts/verify-vllm-ready.py 192.0.2.11 192.0.2.12
   ```

8. **Assign tasks** via agent coordinator:
   ```bash
   python3 ~/.hermes/plugins/hscc-agent-coordinator/hscc.py assign-task <agent> <task-id>
   ```

9. **Sync lifecycle state**: Ensure `~/.hscc/lifecycle.json` task_ids match `~/.hscc/projects.json`
   - Otherwise agent coordinator shows stale task IDs

10. **Resume idle monitor** after all containers are stable:
    ```
    cronjob action=resume job_id=<idle-monitor-job-id>
    ```

## Pitfalls

### Container launches ≠ model is ready
Sparkrun containers start successfully but vLLM may fail to load the model. Just checking `sparkrun status` is not enough — test the HTTP endpoint.

### Recipe YAML edits are ignored until committed
`~/.sparkrun-local/` is a git repo. Sparkrun resolves recipes from **git HEAD**, not the working copy. `git add -A && git commit -m "..."` is required after every recipe change.

### Cached recipes in ~/.cache/sparkrun/
Sparkrun clones remote registries into `~/.cache/sparkrun/registries/` under multiple paths. After fixing a recipe, clean the cache or verify with `sparkrun show @official/<recipe>`.

### Node failures are silent
A node (e.g., 248) may fail with CUDA errors (`libtorch_cuda.so`). Always verify each node individually — don't assume all succeed.

### Task state sync
The agent coordinator's `lifecycle.json` and `projects.json` may diverge. Always ensure `task_id` in lifecycle.json matches `id` in projects.json for the same agent, or the coordinator shows wrong state.