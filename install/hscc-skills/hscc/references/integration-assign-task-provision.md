# HSCC Provisioning Integration — assign-task ↔ sparkrun

When `hscc agent-coordinator assign-task <agent_id> <task_id>` runs:

1. **FSM guard** — validates agent exists, checks lifecycle transition (idle → spawning → running)
2. **Provision check** — calls `sparkrun status`, scans for existing container with matching recipe
3. **Auto-provision** — if no container, invokes provision plugin via subprocess:
   ```bash
   python3 ~/.hermes/plugins/hscc-provision/hscc.py run @official/<recipe> <idle_host>
   ```
4. **Wire agent** — updates `agents.json` with correct `model`, `endpoint`, `status`

## Critical Pitfalls

### Sparkrun Output Parsing
`sparkrun status` output has:
```
Job: /path/to/recipe.yaml  (tp=1, pp=1)  [1b6e77192e59]
  solo       192.0.2.10   Up 15 hours   sparkrun-eugr-vllm:latest
...
Idle hosts (no sparkrun containers):
  192.0.2.11
```

**NEVER scan all lines for IP patterns** — file paths like `/Users/desac/.sparkrun-local/...` contain dots that match `count(".") == 3`.

**Correct approach:**
- Match "Job:" line → read NEXT line for host IP
- Only parse idle hosts in "Idle hosts" section
- Use `in_idle_section` flag

### Subprocess Path Expansion
`~` is NOT expanded in subprocess args:
```python
# WRONG
sub.run(["python3", "~/.hermes/plugins/hscc-provision/hscc.py", ...])
# ERROR: can't open file '/Users/desac/~/.hermes/...'

# CORRECT
path = os.path.expanduser("~/.hermes/plugins/hscc-provision/hscc.py")
sub.run(["python3", path, "run", recipe, host])
```

## Recipe Resolution
- Agent with `model="auto"` → uses default `qwen3.6-35b-a3b-fp8-vllm`
- Agent with specific model → extracts recipe from model string (last path segment)
- Provision plugin resolves `@official/<recipe>` to local YAML path

## State Files
- `~/.hscc/lifecycle.json` — agent lifecycle state (idle/spawning/running/failed)
- `~/.hscc/agents.json` — agent model/endpoint config (wired on success)
- `~/.hscc/worktrees.json` — git worktree state per task
- `~/.hscc/events.jsonl` — immutable event log
