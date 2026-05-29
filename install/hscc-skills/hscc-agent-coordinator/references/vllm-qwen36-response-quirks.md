# vLLM Qwen3.6 `--reasoning-parser` Bug

## The Problem

When Qwen3.6-35B-A3B-FP8 is served with the sparkrun recipe flag `--reasoning-parser qwen3`, ALL model output is intercepted and placed into the `reasoning` field in the API response. The `content` field is left as `null`.

**Symptoms:**
```json
{
  "choices": [{
    "message": {
      "content": null,
      "reasoning": "Full model output text here...",
      "role": "assistant"
    }
  }]
}
```

Any automation reading `message.content` gets `None` — silently failing.

## Root Cause

The `--reasoning-parser qwen3` vLLM argument tells vLLM to parse and route output into the structured `reasoning` field. This was added to the recipe for structured reasoning output, but it captures ALL output, leaving nothing for `content`.

## Fix

**Remove `--reasoning-parser` from the sparkrun recipe.** The fixed recipe is at:
`~/.sparkrun-local/recipes/qwen3.6-35b-a3b-fp8-vllm.yaml`

In the recipe YAML, remove these lines from `defaults` and `command`:
```yaml
# Remove:
reasoning_parser: qwen3
--reasoning-parser {reasoning_parser} \
```

**Verification script:**
```python
import urllib.request, json
url = "http://<host>:8000/v1/chat/completions"
payload = json.dumps({
    "model": "Qwen/Qwen3.6-35B-A3B-FP8",
    "messages": [{"role": "user", "content": "say hi"}],
    "max_tokens": 50
}).encode()
req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
resp = urllib.request.urlopen(req, timeout=30)
data = json.loads(resp.read())
msg = data["choices"][0]["message"]
print("content:", repr(msg.get("content")))  # Should have text now
print("reasoning:", msg.get("reasoning"))   # Should be None
```

## When Reasoning Parser IS Needed

If structured reasoning output (CoT/analysis before response) is required, keep `--reasoning-parser qwen3` and read from the `reasoning` field instead of `content`. But for standard tool-use/agent workflows, it causes more problems than it solves.

## Affected Deployments

- Recipe `@official/qwen3.6-35b-a3b-fp8-vllm` (original, contains `reasoning_parser: qwen3`)
- Any sparkrun recipe with `reasoning_parser: qwen3` in defaults

## Other Models

This is specific to Qwen3.6 on this vLLM version. Other models/recipes may behave differently. Always test with a short completion before relying on `content` field.
