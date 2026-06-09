# HSCC Monitoring Daemon

Continuous monitoring daemon for the DGX Spark cluster. Runs a 60s cycle of health checks, auto-restarts the vLLM orchestrator if dead, and alerts the user via Telegram for other failures.

## Installation

The daemon lives in `~/.hermes/plugins/hscc_daemon/`. No dependencies beyond Python 3.9 stdlib.

## Configuration

Create `~/.hscc/daemon/config.json`:

```json
{
  "poll_interval_sec": 60,
  "handlers": {
    "vllm": {"url": "http://localhost:8000/health"},
    "gateway": {"url": "http://localhost:18789/health"},
    "container": {"id": "hscc-orchestrator"},
    "nas": {"host": "nas.local", "path": "/", "key_path": null}
  },
  "telegram": {
    "chat_id": "YOUR_CHAT_ID",
    "bot_token": "YOUR_BOT_TOKEN",
    "max_restarts": 1,
    "max_alerts_per_60s": 5
  }
}
```

## Commands

| Command | Description |
|---------|-------------|
| `hscc_daemon start` | Start daemon in foreground |
| `hscc_daemon start --dry-run` | Run checks without executing actions |
| `hscc_daemon stop` | Graceful shutdown (sends SIGTERM) |
| `hscc_daemon status` | Show latest health report |
| `hscc_daemon alerts` | List pending alerts |
| `hscc_daemon check` | Run self-diagnostic |

## Health Status Categories

| Status | Meaning | Action |
|--------|---------|--------|
| `healthy` | Explicit success | No action |
| `unhealthy` | Explicit failure | Auto-restart orchestrator / Telegram alert |
| `unknown` | Timeout / connection refused | Warn only (no restart) |

## File Layout

```
~/.hermes/plugins/hscc_daemon/
├── daemon.py          # Core loop, escalator, CLI
├── README.md          # This file
├── tests/
│   └── test_daemon.py # Unit + integration tests
└── handlers/
    ├── __init__.py
    ├── base.py        # AbstractHandler + timeout runner
    ├── vllm.py        # vLLM HTTP health (:8000)
    ├── container.py   # Docker container lifecycle
    ├── gateway.py     # Hermes gateway HTTP (:18789)
    └── nas.py         # NAS disk space via SSH

~/.hscc/daemon/
├── config.json        # User configuration
├── status.json        # Latest cycle report (overwritten each cycle)
├── alerts.jsonl       # Persistent alert log (appended)
└── daemon.pid         # PID file (present when running)
```

## Safety Guarantees

1. **Max 1 restart per cycle** — prevents restart loops
2. **10s timeout per handler** — no blocking on slow checks
3. **unknown ≠ unhealthy** — timeouts don't trigger restarts
4. **Telegram rate-limited** — max 5 alerts per 60s
5. **Handler exceptions caught** — one handler crash doesn't kill the daemon
6. **Graceful shutdown** — current cycle finishes before exit

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `config.json not found` | Create it with defaults + your values |
| `docker: not found` | Ensure docker CLI is in PATH |
| `SSH to NAS fails` | Check host, SSH key, and network |
| `Telegram not sending` | Verify chat_id and bot_token in config |
| `Daemon won't start` | Run `hscc_daemon check` for self-diagnostic |
