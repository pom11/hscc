# vLLM Qwen3.6 `--reasoning-parser` Bug

## Symptom
API requests return `content: null` in choices.message, with ALL model output routed to `reasoning` field instead.

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": null,
      "reasoning": "Here's a thinking process:\n\n1. ..."
    }
  }]
}
```

## Root Cause
The sparkrun recipe command includes `--reasoning-parser qwen3` flag in the `vllm serve` command. This flag causes vLLM to intercept all completions and route them to the `reasoning` field, leaving `content` null.

## Fix
Remove `--reasoning-parser` from the recipe:

**File**: `~/.sparkrun-local/recipes/official/qwen3.6-35b-a3b-fp8-vllm.yaml`

Before:
```yaml
command: |
  vllm serve {model} \
    ...
    --reasoning-parser qwen3 \
    ...
```

After (no `--reasoning-parser` line):
```yaml
command: |
  vllm serve {model} \
    ...
    --kv-cache-dtype {kv_cache_dtype} \
    ...
```

Also check `defaults:` section for any `reasoning_parser` key — remove it.

## Verification
```bash
curl -X POST http://<ip>:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3.6-35B-A3B-FP8","messages":[{"role":"user","content":"say hi"}],"max_tokens":10}'
```
Response must have `message.content` populated (not null).

## Critical: Container Restart Required
Patch the recipe YAML → then STOP and RESTART the container:
```bash
sparkrun stop <container-id>
sparkrun run @local-official/qwen3.6-35b-a3b-fp8-vllm -H <host>
```

The running container was built with the OLD command. Patching YAML alone has NO effect on live containers.

## Multi-Node Scale Fix
When provisioning N containers with the fixed recipe:
1. Pause idle monitor cron first
2. Kill any old containers (they have wrong command)
3. Provision fresh on all hosts
4. Verify API on each before assigning tasks
