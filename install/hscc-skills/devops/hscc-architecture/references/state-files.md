# HSCC State Files Reference

State files HSCC components read and write.

## File Locations

Live state is under `~/.hscc/` (override with the `HSCC_HOME` env var):

| File | Format | Contents | Used By |
|------|--------|----------|---------|
| `cluster.json` | JSON | Gateway, workers, NAS device definitions | hscc-cluster |
| `agents.json` | JSON | 21 agents (dev-001..dev-020, merge-001) with roles, status, tasks | hscc-agents |
| `projects.json` | JSON | Projects, tasks, kanban board state | hscc-projects |
| `network.json` | JSON | Network config, SSH settings | hscc-cluster |
| `events.jsonl` | JSON Lines | Event log (one JSON object per line) | hscc-events |
| `session-snapshot.json` | JSON | Current session state snapshot | hscc-projects |
| `setup.json` | JSON | Setup wizard state | hscc-gateway |
| `cluster-manifest.json` | JSON | Cluster deployment manifest | hscc-cluster |
| `plugin-state/` | dir | Per-plugin state (enabled/disabled, config) | hscc-gateway |

## Agent Structure (agents.json)

```json
{
  "agents": {
    "dev-001": {
      "id": "dev-001",
      "name": "Builder",
      "role": "developer",
      "model": "Qwen/Qwen3.6-35B-A3B-FP8",
      "workspace": "/Users/desac/projects/example-repo",
      "node": "192.0.2.11",
      "current_task_id": "task-001",
      "status": "idle",
      "created_at": "2024-...",
      "enabled": true
    }
  },
  "next_ids": { "dev": 21 }
}
```

Agent roles: orchestrator, architect, developer, frontendDev, backendDev, reviewer, securityEngineer, sre, devops, qaEngineer, dataScientist, workflowArchitect, technicalWriter, custom

## Cluster Structure (cluster.json)

```json
{
  "gateway": { "role": "gateway", "ip": "192.0.2.10", "sshUser": "spark", ... },
  "workers": [
    { "role": "worker", "ip": "192.0.2.11", "sshUser": "spark", ... },
    { "role": "worker", "ip": "192.0.2.12", "sshUser": "spark", ... },
    { "role": "worker", "ip": "192.0.2.13", "sshUser": "spark", ... }
  ],
  "nasDevices": [
    { "role": "nas", "ip": "192.0.2.20", "sshUser": "spark", ... }
  ]
}
```
