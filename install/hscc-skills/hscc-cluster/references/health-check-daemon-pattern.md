# Health Check Daemon Pattern

Design pattern for HSCC monitoring daemons: modular handlers, file-based state, Telegram alerts.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌──────────┐
│  Scheduler   │────▶│  Handlers    │────▶│Escalator │
│  (60s loop)  │◀────│ (SSH checks) │     │ (decides) │
└─────────────┘     └──────────────┘     └──────────┘
                              │                │
                     ┌────────┴──────┐  ┌─────┴──────┐
                     │ status.json   │  │ Telegram   │
                     │ alerts.jsonl  │  │ delivery   │
                     └───────────────┘  └────────────┘
```

## Config Schema (`~/.hscc/daemon/config.json`)

```json
{
  "poll_interval": 60,
  "handler_timeout": 10,
  "max_orchestrator_restarts": 1,
  "telegram_enabled": true,
  "handlers": ["vllm", "container", "gateway", "nas"]
}
```

## Handler Contract

Each handler is a function that returns:

```json
{"component": "vllm", "status": "healthy", "detail": {"uptime": "12h", "gpu_mem": "85%"}}
```

Or on failure:

```json
{"component": "vllm", "status": "unhealthy", "error": "HTTP 503 from :8000/health"}
```

Or on `unknown` (SSH timeout, network unreachable):

```json
{"component": "nas", "status": "unknown", "error": "SSH connection timed out after 10s"}
```

## Escalation Rules

| Handler result | Action |
|---|---|
| orchestrator = `unhealthy` | Auto-restart container (max 1/cycle) |
| orchestrator = `unknown` | Log warning |
| any other = `unhealthy` | Telegram alert |
| ALL = `unknown` | Telegram alert (system may be down) |
| mixed `unknown` + others | Log warning, don't alert |

## State Files

- `~/.hscc/daemon/status.json` — latest cycle's full HealthReport (overwritten each cycle)
- `~/.hscc/daemon/alerts.jsonl` — append-only alert log (one JSON object per line)

## Pitfalls

- **Never SSH-connect from macOS to cluster host for container commands** — always use `ssh spark@<host>` for SSH-only checks, then `ssh spark@<host> docker ps` for container inspection. Never run Docker CLI from macOS.
- **Handle stale SSH sessions** — the SSH connection from Hermes session may not carry SSH keys to remote hosts. Always verify agent's `~/.ssh/config` and SSH key existence before relying on passwordless SSH.
- **10s timeout is strict** — if a handler takes longer, it returns `unknown`, not `unhealthy`. This prevents a slow handler from blocking the entire cycle.
- **Never restart on `unknown`** — unknown means we can't tell if the service is down or just unreachable. Auto-restart could cause a restart storm if the network is flaky.
- **Append-only alerts** — `alerts.jsonl` is never overwritten. Use `tail -n 100` to view recent alerts, or parse the file for history.