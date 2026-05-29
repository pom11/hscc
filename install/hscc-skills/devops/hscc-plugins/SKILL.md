---
name: hscc-plugins
description: Building Hermes plugins for cluster control, agent management, and project tracking.
category: devops
---

# HSCC Plugins

Building custom Python plugins for Hermes to manage DGX Spark cluster, agents, and projects.

## When to use

- Building a new `hscc-*` component (hscc-cluster, hscc-projects, hscc-agents, etc.)
- Wrapping CLI tools as structured JSON plugins for the agent
- Any task involving Python scripts in `~/.hermes/plugins/`

## File Structure

```
~/.hermes/plugins/hscc-<name>/hscc.py     # Python plugin
~/.hermes/skills/hscc-<name>/SKILL.md     # Instructions for agent
```

The agent calls the plugin directly:
```bash
python3 ~/.hermes/plugins/hscc-cluster/hscc.py <command> [args]
```

No wrapper scripts, no symlinks, no TypeScript compilation.

## Language Preference

**Use Python, not TypeScript.** The user explicitly prefers Python plugins for zero-compilation simplicity.

## Command Pattern

Each command is a function returning `{"success": True/False, "output": "...", ...}`:

```python
def cmd_<name>():
    return run_cmd([SPARKRUN, "command", "args"], timeout=15)
```

The `run_cmd` helper wraps subprocess with JSON parsing:
```python
def run_cmd(args, timeout=30):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return {"success": result.returncode == 0, "output": result.stdout.strip(), ...}
```

## Common Gotchas

### sparkrun monitor streams
`sparkrun cluster monitor --json` streams continuously. Always wrap with `timeout`:
```python
result = subprocess.run(
    "timeout 3 sparkrun cluster monitor --simple --json",
    shell=True, capture_output=True, text=True, timeout=10
)
first_line = result.stdout.strip().split("\n")[0]
return {"success": True, "json": json.loads(first_line)}
```

### SSH with quotes
When SSH commands contain quotes, use double-quote escaping carefully or pass as separate args.

### Structured output
Always return JSON the agent can parse. Don't return raw text for commands the agent needs to programmatically inspect.

## Naming Convention

All components use the `hscc-*` prefix (Hermes Spark Cluster Control):
- `hscc-cluster` — sparkrun status, hosts, monitoring, workloads
- `hscc-projects` — kanban boards, tasks, roadmaps
- `hscc-agents` — agent fleet management
- `hscc-orchestrator` — soul files, dispatch rules
- `hscc-events` — notifications, lifecycle, triggers

## Anti-Patterns

- Don't create wrapper scripts or symlinks — agent calls Python directly
- Don't use TypeScript — no compilation, just Python
- Don't let commands stream — always return a single parseable response
- Don't use hardcoded IPs — read from `~/.hscc/` or `~/.config/sparkrun/clusters/`
- **Don't delete plugins before checking `install/hscc-plugins/` templates** — those are the authoritative source. If you need to "clean" a plugin, extract from templates first, then modify in place.
