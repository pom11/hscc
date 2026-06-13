---
name: hscc-provision
description: Manage dynamic lifecycle of ML model containers on the DGX Spark cluster. Auto-discover sparkrun recipes, verify models on NAS, spin up containers, wire Hermes agents.
category: hscc
domain: cluster control, dynamic provisioning
platform: macOS CLI
version: 1.0.0
license: MIT
metadata.hermes.tags: []
---

# hscc-provision

Manages the dynamic lifecycle of ML model containers on the DGX Spark cluster.
Auto-discovers available sparkrun recipes, verifies models exist on NAS
(pre-downloaded only), spins up containers, and wires Hermes agents to running
containers.

**CRITICAL:** Only uses models pre-downloaded on NAS. Blocks any recipe that
would trigger a model download. Runs with `HF_HUB_OFFLINE=1` to enforce.

## Local Recipes Registry

A local recipe registry is maintained at `~/.sparkrun-local/` with recipes from:
- **official:** Spark-Arena official recipes (from `@official` registry)
- **transitional:** Sparkrun transitional recipes (from `@sparkrun-transitional` registry)

The `hscc-provision` plugin automatically resolves remote recipe names (e.g.,
`@official/qwen3.6-35b-a3b-fp8-vllm`) to local file paths when available.

See `references/registry-manifest.md` for the `.sparkrun/registry.yaml` manifest format and full setup workflow.
- `references/vllm-chat-template-validation.md` — vLLM `--chat-template` validation pitfalls and fixes.

## Commands

### `hscc-provision recipes`
List all available sparkrun recipes with their target models and `has_nas` flag.
Only recipes with `has_nas: true` are safe to use.
```
python3 ~/.hermes/plugins/hscc-provision/hscc.py recipes
```

### `hscc-provision list`
Show all running containers.
```
python3 ~/.hermes/plugins/hscc-provision/hscc.py list
```

### `hscc-provision run <recipe> [host]`
Spin up a sparkrun container.
- `recipe`: sparkrun recipe (e.g. `@official/qwen3.6-35b-a3b-fp8-vllm`)
- `host` (optional): specific host IP — auto-selects idle host if omitted
- **Blocks if model not on NAS**
- **Prefers local recipes** if available

### `hscc-provision stop <container_id>`
Stop a running container.

### `hscc-provision assign <agent_id> <recipe> [host]`
Wire an agent to a recipe. Spins up the container if not already running.

### `hscc-provision unassign <agent_id>`
Clear an agent's model assignment, set it back to `auto`.

### `hscc-provision health [host_ip]`
Check vLLM health endpoint. Without host, checks all running containers.

### `hscc-provision cleanup`
Stop containers whose recipes have no active agent assignments.

### `hscc-provision status`
Combined fleet summary (agents, containers, idle hosts, recipes).

### `hscc-provision registry <list|sync|add|remove>`
Manage the local hscc recipe registry.
- **list**: Show all local recipes with their details and NAS status
- **sync**: Sync recipes from remote registries (sparkrun, spark-arena)
- **add**: Add a new recipe source
- **remove**: Remove recipes from a source

## Workflow

1. **Discover recipes**: `hscc-provision recipes` — look for `has_nas: true`
2. **Spin up**: `hscc-provision run <recipe> [host]`
3. **Wire agent**: `hscc-provision assign <agent_id> <recipe> [host]`
4. **Agent ready**: agent now has the recipe in its `model` field
5. **Cleanup**: `hscc-provision cleanup`

## Pitfalls

- **No yaml module**: The system Python does not have the `yaml` module. To parse
  recipe YAML files, use `sparkrun show <path_to_yaml>` instead of Python's yaml parser.
- **Duplicate main dispatch**: The hscc.py file has TWO `if __name__ == "__main__"`
  sections (line ~582 and line ~810). Keep only the last one (line ~810). Remove
  the first section plus any dangling function definitions between them.
- **`sparkrun list -q` doesn't work**: Use `sparkrun list` without the `-q` flag.
- **git sparse-checkout for registry recipes**: After cloning remote registries,
  recipe files may be missing. Run `git read-tree --reset -u HEAD` to populate them.
- **Always check has_nas**: Before running any recipe, verify the model exists on NAS.
  The plugin blocks downloads, but verify manually too.
- **No model downloads**: Enforce `HF_HUB_OFFLINE=1`. Never trust sparkrun to respect
  cache-only mode without the env var.

## Model Check Script

A reusable script at `scripts/model-check.py` provides the full model management
workflow: check availability, sync from NAS, fix recipe env vars, and bulk download.

```bash
python3 ~/.sparkrun-local/scripts/model-check.py check          # Check model availability on hosts
python3 ~/.sparkrun-local/scripts/model-check.py list           # List all recipes
python3 ~/.sparkrun-local/scripts/model-check.py fix-recipes    # Add HF env vars to recipes missing them
python3 ~/.sparkrun-local/scripts/model-check.py sync <model>   # Sync model from NAS to all hosts
python3 ~/.sparkrun-local/scripts/model-check.py download <model> <host-ip>
python3 ~/.sparkrun-local/scripts/model-check.py download-all   # Sync all NAS models to all hosts
```

## Pitfalls

- **rsync remote-to-remote fails on macOS**: The system rsync (2.6.9 compatible) does
  not support `rsync source:/path dest:/path`. Always run rsync FROM the NAS host
  via SSH. See `references/rsync-remote-to-remote-macos.md` for details.
- **No associative arrays on macOS**: macOS ships bash 3.2 which lacks `declare -A`.
  Use temp files (mktemp + grep) instead of associative arrays for lookups.
- **`set -euo pipefail` breaks SSH conditionals**: SSH failures in bash 3.2 cause
  unexpected exits when used in conditionals. Omit `set -e` in scripts with SSH calls.
- **HuggingFace repo naming**: `org/model` → `models--org--model`. The `hf_repo_name()`
  function in model-check.py handles this.
- **sparkrun auto-mounts HF cache**: The recipe env `HF_HOME=/cache/huggingface` maps
  to host's `/home/spark/.cache/huggingface` via bind mount.
- **sparkrun status parsing**: When parsing `sparkrun status` output for host IPs, NEVER count dots in "Job:" lines — file paths like `.sparkrun-local/recipes/official/qwen3.6-35b-a3b-fp8-vllm.yaml` contain dots and will be falsely identified as IPs. Always extract IPs from the line AFTER "Job:" (the `solo IP Up ...` line). For idle hosts, only parse from within the "Idle hosts" section, not globally.
- **Empty dirs confuse checks**: Always count files (`find ... -type f | wc -l`)
  rather than checking directory existence when detecting cached models.
- **Host `.245` may be unreachable**: Some nodes may be down. Scripts should handle
  SSH failures gracefully.

## Multi-Node Provisioning

### Always verify vLLM is serving before assigning tasks
Sparkrun containers launch successfully but vLLM may fail to load the model (GPU OOM, CUDA errors, etc.). Before assigning agents, verify the endpoint responds:
```bash
# Quick smoke test — wait for model to load
sleep 30
python3 -c "
import urllib.request, json
r = urllib.request.urlopen('http://<host>:8000/v1/chat/completions',
    json.dumps({'model':'Qwen/Qwen3.6-35B-A3B-FP8','messages':[{'role':'user','content':'one word'}],'max_tokens':5}).encode(),
    timeout=30)
print(json.loads(r.read())['choices'][0]['message']['content'][:30])
"
```
If it fails or hangs, check logs: `ssh <host> "docker logs sparkrun_<id>_solo | tail -30"`

### Recipe edits only take effect after `git commit`
The `~/.sparkrun-local/` directory is a **git repository**. Sparkrun resolves recipes from
**git HEAD**, not the working copy. Editing a YAML file has NO effect until you run
`git add -A && git commit -m "message"`. Always verify with `git show HEAD:<path>` after
editing.

### Sparkrun caches recipes in multiple locations
Sparkrun clones remote registries into `~/.cache/sparkrun/registries/` under many
different paths (by URL hash, by registry name, etc.). Changing a local working copy
doesn't automatically update these cached copies. Use:
```python
import glob, os, re
cache_dir = os.path.expanduser("~/.cache/sparkrun/registries")
for path in glob.glob(f"{cache_dir}/**/*.yaml", recursive=True):
    # check and fix
```
To fix ALL cached copies at once. After changing, verify with `sparkrun show <recipe>`.

### vLLM `--chat-template` filenames must exist INSIDE the container
The `--chat-template unsloth.jinja` flag expects that file to exist inside the container
filesystem at runtime, NOT in the local recipes directory. vLLM validates this at startup
and fails with `ValueError: ...appears path-like, but doesn't exist!`.

To check: `docker exec <container> find / -name 'unsloth.jinja'`. If missing, either
remove the flag, use a built-in template name (e.g., `chatml`), or copy the file into
the container's image.

## Key details

- All cluster hosts share NAS at `/mnt/nas` (via gateway `192.0.2.10`)
- SSH configured in `~/.ssh/config` for `User spark` on `192.0.2.*`
- Agent model path format: `<recipe_name>` (e.g. `@official/qwen3.6-35b-a3b-fp8-vllm`)
- Provision state persisted in `~/.hscc/provision.json`
- Local recipes directory: `~/.sparkrun-local/recipes/{official,transitional}/`
- Local registry registered with sparkrun via `.sparkrun/registry.yaml`
- Registry manifest format reference: `references/registry-manifest.md`
- Model-check script reference: `scripts/model-check.py`