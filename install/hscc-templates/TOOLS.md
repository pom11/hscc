# TOOLS.md — HSCC Infrastructure

## sparkrun Architecture

sparkrun is a CLI tool installed ONLY on the Mac. It manages DGX Spark nodes remotely over SSH.
- NEVER install sparkrun, pip, uv, or any Python tooling on DGX nodes.
- NEVER SSH into a node to install software.
- ALL sparkrun commands run locally on the Mac.

## DGX Spark Cluster

| Node | IP | User | Role | GPU | VRAM |
|------|-----|------|------|-----|------|
| Spark 1 | {{dgxIP}} | spark | Primary | GB10 | 128GB unified |
| Spark 2 | 192.168.1.202 | spark | Worker | GB10 | 128GB unified |

- Cluster name: `hscc`
- SSH auth: key-based (no passwords)
- Networking: Realtek ethernet only
- Each node runs independently — ethernet is too slow for multi-node tensor parallelism

## Services

| Service | Address |
|---------|---------|
| Hermes Gateway | localhost:18789 |
| PostgreSQL | localhost:5432 (hermes/hermes) |
| mem0 | localhost:8090 |
| mem0-mcp | localhost:9200 |
| Ollama | localhost:11434 |

## SSH Diagnostics

For things sparkrun doesn't cover:
```bash
ssh spark@{{dgxIP}} 'nvidia-smi'
ssh spark@{{dgxIP}} 'df -h /home'
ssh spark@{{dgxIP}} 'docker ps'
ssh spark@{{dgxIP}} 'docker logs sparkrun_vllm --tail 50'
```
