# HSCC Project Management

Manage projects, roadmaps, sub-projects, and tasks via the `hscc-projects` Python plugin.

## When to use

- User wants to create or manage projects, tasks, or roadmaps
- User asks for task status, progress, or kanban view
- User wants to assign tasks to agents or search tasks

## Commands

```bash
python3 ~/.hermes/plugins/hscc-projects/hscc.py <command> [args]
```

### `list`
List all projects and show which is active.

```bash
python3 ~/.hermes/plugins/hscc-projects/hscc.py list
```

### `create <name> <description>`
Create a new project. Sets it as active. **Auto-provisions a git repo and a
kanban board** for the project:

- Git repo at `~/.hscc/projects/<id>` (git init + `.gitignore` for `.worktrees/`
  + `README.md` + initial commit). Stored as `gitRepoPath`.
- A Hermes kanban board (slug `hscc-<8hex>`, bound to the repo as its default
  workdir). Stored as `boardSlug`.

This is the foundation the executor bridge needs: agent work on this project's
tasks runs in git worktrees of this repo, dispatched to this board. **When there
is agent work to be done, create a project first**, then add tasks and dispatch
them via `hscc-agent-coordinator dispatch-task`.

```bash
python3 ~/.hermes/plugins/hscc-projects/hscc.py create "My Project" "Project description"
```

The return includes `repo_status` (created/exists/error) and `board` (slug,
created, detail). Board provisioning never blocks project creation — if the
gateway/kanban is unavailable it is recorded and skipped.

### `show`
Show full details of the active project including all roadmaps, sub-projects, and tasks.

```bash
python3 ~/.hermes/plugins/hscc-projects/hscc.py show
```

### `status`
Quick summary of all task statuses in the active project.

```bash
python3 ~/.hermes/plugins/hscc-projects/hscc.py status
```

Returns: total tasks, count by status (backlog, inProgress, done, cancelled).

### `list-projects`
List all projects with task counts.

```bash
python3 ~/.hermes/plugins/hscc-projects/hscc.py list-projects
```

### `add-roadmap <name> <description>`
Add a roadmap to the active project.

```bash
python3 ~/.hermes/plugins/hscc-projects/hscc.py add-roadmap "Development" "Core development roadmap"
```

### `add-subproject <roadmap> <name> <description>`
Add a sub-project to a roadmap.

```bash
python3 ~/.hermes/plugins/hscc-projects/hscc.py add-subproject "Development" "Backend" "Backend tasks"
```

### `add-task <roadmap> <subproject> <title> <description>`
Add a task to a sub-project.

```bash
python3 ~/.hermes/plugins/hscc-projects/hscc.py add-task "Development" "Backend" "Setup API" "Create REST API routes"
```

### `update-task <task_id> <field> <value>`
Update a task field (status, priority, labels, assignedAgent).

```bash
python3 ~/.hermes/plugins/hscc-projects/hscc.py update-task <task_id> priority high
python3 ~/.hermes/plugins/hscc-projects/hscc.py update-task <task_id> status inProgress
```

### `move-task <task_id> <status>`
Quick shortcut to move a task to a new status.

```bash
python3 ~/.hermes/plugins/hscc-projects/hscc.py move-task <task_id> inProgress
```

Valid statuses: `backlog`, `inProgress`, `done`, `cancelled`, `review`

### `assign-task <task_id> <agent_id>`
Assign a task to an agent.

```bash
python3 ~/.hermes/plugins/hscc-projects/hscc.py assign-task <task_id> dev-001
```

### `list-agents`
List all agents from `~/.hscc/agents.json` with their current task assignments.

```bash
python3 ~/.hermes/plugins/hscc-projects/hscc.py list-agents
```

### `search <query>`
Search tasks by title or description.

```bash
python3 ~/.hermes/plugins/hscc-projects/hscc.py search "API"
```

### `repo-path <project_id>`
Print the git repo path provisioned for a project.

```bash
python3 ~/.hermes/plugins/hscc-projects/hscc.py repo-path <project_id>
```

### `delete <project_id>`
Remove a project from `projects.json` (non-destructive — the git repo is **left
on disk** for manual removal; this command never deletes files).

```bash
python3 ~/.hermes/plugins/hscc-projects/hscc.py delete <project_id>
```

## Tips

- State lives in `~/.hscc/projects.json`
- Each project has: roadmaps → subProjects → tasks (3-level hierarchy)
- Task statuses: `backlog` → `inProgress` → `review` → `done`
- Agents are sourced from `~/.hscc/agents.json`
- Task IDs are UUIDs — use the full ID when referencing tasks
- Each project also owns a git repo (`gitRepoPath`) and a kanban board
  (`boardSlug`) used by the executor bridge in `hscc-agent-coordinator`
