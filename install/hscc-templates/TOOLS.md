# TOOLS.md — R2D2 Infrastructure

## CRITICAL: sparkrun Architecture

sparkrun is a CLI tool installed ONLY on the Mac. It manages DGX Spark nodes remotely over SSH.
- NEVER install sparkrun, pip, uv, or any Python tooling on DGX nodes.
- NEVER SSH into a node to install software. Nodes only need Docker + NVIDIA runtime (pre-installed).
- ALL sparkrun commands run locally on the Mac. sparkrun handles SSH internally.

## DGX Spark Cluster

| Node | IP | User | Role | GPU | VRAM |
|------|-----|------|------|-----|------|
| Spark 1 | {{dgxIP}} | spark | Primary | GB10 | 128GB unified |
| Spark 2 | 192.168.1.202 | spark | Worker | GB10 | 128GB unified |

- Cluster name: `r2d2`
- SSH auth: key-based (no passwords)
- Networking: Realtek ethernet only (no CX7/InfiniBand)
- Each node runs independently — ethernet is too slow for multi-node tensor parallelism

## Current Model Deployment
- Model: `{{modelRepo}}`
- Recipe: `{{recipe}}`
- Runtime: {{runtime}}
- Endpoint: http://{{dgxIP}}:8000/v1
- Hermes provider: `vllm-dgx`

### Launch / Stop Current Model
```bash
sparkrun run {{recipe}} --cluster r2d2 --no-follow
sparkrun stop {{recipe}} --cluster r2d2
```

## sparkrun CLI — Quick Reference

All commands run on the Mac. Use the `sparkrun_exec` tool or shell.

### Cluster Status
```bash
sparkrun cluster status --cluster r2d2          # human-readable
sparkrun cluster status --cluster r2d2 --json   # machine-readable
```

### Launch a Model
```bash
sparkrun run <recipe> --cluster r2d2 --no-follow         # deploy to cluster
sparkrun run <recipe> --hosts {{dgxIP}} --no-follow   # deploy to specific node
sparkrun run <recipe> --solo --no-follow                   # single-node mode
```

### Stop a Workload
```bash
sparkrun stop <recipe> --cluster r2d2
```

### Browse Recipes
```bash
sparkrun recipe list                    # all available recipes
sparkrun recipe list --json             # machine-readable
sparkrun recipe show <recipe>           # recipe details
sparkrun recipe search <query>          # search by keyword
```

### Model Sync (download model to nodes)
```bash
sparkrun setup model-sync --recipe <recipe> --cluster r2d2
```

### Container Logs
```bash
sparkrun logs <recipe> --cluster r2d2 --tail 50
```

### VRAM Check
```bash
sparkrun run <recipe> --dry-run   # preview VRAM usage without launching
```

## sparkrun Plugin (Hermes)

The `@sparkarena/sparkrun` plugin provides:
- **Tool:** `sparkrun_exec` — execute any sparkrun CLI command
- **Skills:** `run`, `setup`, `registry` — load these for detailed guidance

When doing sparkrun operations, load the appropriate skill first:
- Before running/stopping/monitoring: load the `run` skill
- Before cluster setup: load the `setup` skill
- Before managing registries: load the `registry` skill

## R2D2 Plugins (Hermes)

| Plugin | Tools | Purpose |
|--------|-------|---------|
| r2d2-agents | 15 | Agent CRUD, task dispatch (atomic guards), fleet coordination, auto-routing. Includes merge-001 agent for branch integration |
| r2d2-projects | 16 | Project/roadmap/subproject/task CRUD, status queries |
| r2d2-worktrees | 8 | Git worktree management, collision detection, green checks |
| r2d2-events | 7 | Event bus, snapshots, rotation, reset |
| r2d2-lifecycle | 4 | Agent lifecycle FSM (idle→spawning→ready→running→finished→failed→disabled) |
| r2d2-recovery | 3 | Failure diagnosis + auto-recovery |
| r2d2-permissions | 4 | Tool access control |
| r2d2-policy | 2 | Policy evaluation |
| r2d2-triggers | 2 | Trigger rule evaluation + listing |
| r2d2-gateway | 1 | Permission + policy pre-flight |
| r2d2-notifications | 1 | User notifications |
| r2d2-prompt-context | 1 | Build structured prompt context from worktree |
| @sparkarena/sparkrun | 1 | sparkrun CLI wrapper |

## mem0 MCP Tools

- `remember(content, user_id?)` — store a memory
- `recall(query, user_id?, limit?)` — semantic search
- `list_memories(user_id?)` — list all memories
- `update_memory(memory_id, content)` — update a memory
- `forget(memory_id)` — delete a memory
- `forget_all(user_id, confirm)` — delete ALL memories

Default user_id is "r2d2". Use agent name for workers.

## Mac Control — Screen, Keyboard & Mouse

### Screen Capture
```bash
screencapture /tmp/screen.png          # full screen
screencapture -R x,y,w,h /tmp/area.png # region
```

### Mouse Control (cliclick)
```bash
cliclick c:500,300      # click at x,y
cliclick dc:500,300     # double-click
cliclick rc:500,300     # right-click
cliclick m:500,300      # move cursor
cliclick dd:100,100 du:200,200  # drag from → to
```

### Keyboard Control (cliclick)
```bash
cliclick t:'Hello'             # type text
cliclick kp:return             # press key
cliclick kd:cmd kp:c ku:cmd    # Cmd+C (copy)
cliclick kd:cmd kp:v ku:cmd    # Cmd+V (paste)
```

### Permissions Required
These apps MUST be added to BOTH Screen Recording AND Accessibility:
- **node** (the Hermes/agent runtime)
- **R2D2 Control Center** (the Mac app)
- **Terminal / iTerm** (if running commands directly)

Path: System Settings → Privacy & Security → Screen Recording / Accessibility

## Services (managed by R2D2 app)

| Service | Address |
|---------|---------|
| Hermes Gateway | localhost:18789 |
| PostgreSQL | localhost:5432 (r2d2/r2d2local) |
| mem0 | localhost:8090 |
| mem0-mcp | localhost:9200 |
| Ollama | localhost:11434 |

## SSH Diagnostics (direct node access)

For things sparkrun doesn't cover, SSH directly:
```bash
ssh spark@{{dgxIP}} 'nvidia-smi'                        # GPU status
ssh spark@{{dgxIP}} 'df -h /home'                       # disk space
ssh spark@{{dgxIP}} 'docker ps'                         # running containers
ssh spark@{{dgxIP}} 'docker logs sparkrun_vllm --tail 50'  # container logs
```
