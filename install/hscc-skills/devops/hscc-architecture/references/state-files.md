# HSCC State Files Reference

State files HSCC components read and write.

## File Locations

Live state is under `~/.hscc/` (override with the `HSCC_HOME` env var):

| File | Format | Contents | Used By |
|------|--------|----------|---------|
| `cluster.json` | JSON | Gateway, workers, NAS device definitions | hscc-cluster, hscc_daemon |
| `network.json` | JSON | Network config, SSH settings | hscc-cluster |
| `events.jsonl` | JSON Lines | Event log (one JSON object per line) | hscc_daemon |
| `state/*.json` | JSON | Daemon polling/check state snapshots | hscc_daemon |
| `cluster-manifest.json` | JSON | Cluster deployment manifest | hscc-cluster |

> **Stale state (consumers archived 2026-06-08).** `agents.json`, `projects.json`, `session-snapshot.json`, `setup.json`, `lifecycle.json` and `plugin-state/` were written by the old agent pipeline (hscc-agents/hscc-projects/hscc-gateway/hscc-agent-coordinator). Agent dispatch and task tracking are now **native Hermes kanban**; these files are no longer authoritative — the paused idle monitor still keys off the stale `agents.json`, which is why it must not be resumed. The `agents.json` schema below is kept for history.

## Agent Structure (agents.json — historical)

```json
{
  "agents": {
    "dev-001": {
      "id": "dev-001",
      "name": "Builder",
      "role": "developer",
      "model": "Qwen/Qwen3.6-35B-A3B-FP8",
      "workspace": "~/projects/example-repo",
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
