# Multi-Node Provisioning Troubleshooting

When provisioning Qwen3.6 (or similar vLLM models) across multiple cluster nodes, this reference covers known failure modes and diagnosis steps.

## Failure Mode A: Container Shows "Up" But Endpoint Refused
- **Cause**: vLLM model loading in progress (5-15+ minutes for Qwen3.6-35B FP8)
- **Diagnosis**: `ssh spark@<ip> "docker logs <container> 2>&1 | grep -i load"`
- **Check for**: "Starting to load model", "model.py:617", "Using max model len"

## B. Container Running But No vLLM Process
- **Cause**: sparkrun command execution failed → fallback to `sleep infinity`
- **Diagnosis**: `ssh spark@<ip> "docker exec <container> ps aux | grep -i vllm"`
- **If only `sleep infinity` found**: sparkrun's primary command failed silently
- **Check**: Container entrypoint + command: `docker inspect <container> --format '{{.Config.Cmd}}'`
- **Fallback indicator**: Command is `bash -c printf %s c2xlZXAgaW5maW5pdHk= | base64 -d -- | bash` — decodes to `sleep infinity`
- **Possible causes**: Recipe syntax error, missing env vars, GPU unavailable, disk full

## C. Recipe Drift Between Nodes
- **Symptom**: Different nodes running different flags/versions
- **Check**: `ssh spark@<ip> "docker exec <container> cat /proc/1/cmdline | tr '\\0' ' '"`
- **Legacy recipe (244)**: Has `--chat-template unsloth.jinja --reasoning-parser qwen3`
- **Fixed recipe (246/247)**: Removed those flags
- **Fix**: Verify all nodes use the same recipe version; use `sparkrun show <recipe>` to inspect

## Diagnostic Checklist (run in order)

1. `sparkrun status` — confirm container count per host
2. `ssh spark@<ip> "docker ps --format '{{.ID}} {{.Names}} {{.Status}}'"` — check container running
3. `ssh spark@<ip> "docker exec <container> ps aux | grep -i vllm"` — verify process
4. `ssh spark@<ip> "docker logs <container> 2>&1 | tail -40"` — check for errors
5. `ssh spark@<ip> "docker inspect <container> --format '{{.Config.Cmd}}'"` — confirm command
6. `ssh spark@<ip> "docker exec <container> ls /cache/huggingface/hub/models--Qwen--Qwen3.6-35B-A3B-FP8/snapshots/"` — verify weights
7. `ssh spark@<ip> "nvidia-smi"` — confirm GPU visible on host
8. `ssh spark@<ip> "cat /etc/docker/daemon.json"` — verify nvidia runtime configured

## Pitfalls

- **`sparkrun logs <job_id>` hangs** on large outputs (20s+). Use direct `docker logs` via SSH.
- **`sparkrun status` shows "Up" ≠ vLLM ready** — only checks container uptime, not service health.
- **Model weights on host ≠ inside container** — container must have volume mount to `/cache/huggingface`.
- **NVIDIA container runtime** must be in `/etc/docker/daemon.json` with `nvidia` runtime entry.
- **Base64 fallback**: `c2xlZXAgaW5maW5pdHk=` = `sleep infinity` — sparkrun's health-check when command fails.

## Session Record: May 28, 2026 — Qwen3.6 Multi-Node Test

| Node | Recipe | vLLM Running | Status |
|------|--------|-------------|--------|
| 244 | Legacy (MTP, chat-template, reasoning-parser) | ✅ | Up 13h |
| 246 | Fixed | ✅ | Up ~5 min load |
| 247 | Fixed | ✅ | Up ~10 min load |
| 248 | Fixed | ❌ | Container running, only `sleep infinity` |

**248 findings**:
- Model weights present inside container at `/cache/huggingface/hub/models--Qwen--Qwen3.6-35B-A3B-FP8/snapshots/95a723d08a9490559dae23d0cff1d9466213d989`
- GPU detected (GB10) on host and in container
- Container entrypoint: `/opt/nvidia/nvidia_entrypoint.sh`
- Command: `sleep infinity` (fallback)
- No error logs: `/tmp/sparkrun_serve.log` doesn't exist inside container
- Root cause: sparkrun's vLLM command execution silently failed, never produced logs