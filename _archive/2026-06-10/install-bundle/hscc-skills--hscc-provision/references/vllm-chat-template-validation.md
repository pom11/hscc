# vLLM Chat Template Validation Pitfall

## Symptom
Container starts but vLLM fails immediately with:
```
ValueError: The supplied chat template string (unsloth.jinja) appears path-like, 
but doesn't exist! Tried: unsloth.jinja and 
/usr/local/lib/python3.12/dist-packages/vllm/transformers_utils/chat_templates/unsloth.jinja
```

## Root Cause
The `--chat-template unsloth.jinja` flag expects that file to exist **inside the container**
filesystem at runtime, NOT in the local recipes directory. vLLM validates this at startup
and fails with `ValueError: ...appears path-like, but doesn't exist!`.

## Debug Steps
```bash
# Check if the template exists in the container
ssh 192.0.2.XXX "docker exec <container> find / -name 'unsloth.jinja' 2>/dev/null"

# Check what templates ARE available
ssh 192.0.2.XXX "docker exec <container> ls /usr/local/lib/python3.12/dist-packages/vllm/transformers_utils/chat_templates/"

# Check the model's own template
ssh 192.0.2.XXX "docker exec <container> cat /home/spark/.cache/huggingface/hub/models--*/snapshots/*/chat_template.jinja 2>/dev/null | head -5"
```

## Fixes (choose one)
1. **Remove `--chat-template` flag** from recipe — vLLM uses the model's built-in template
2. **Use a built-in template name** — e.g., `chatml`, `basic` (available via `ls chat_templates/`)
3. **Copy file into the image** — rebuild sparkrun image with the template baked in
4. **Use model's own template** — the snapshot's `chat_template.jinja` can sometimes be used via path

## Prevention
When creating or modifying sparkrun recipes, always verify the chat template file exists
inside the target container image before relying on it. The model snapshot's
`chat_template.jinja` (if present) is the safest source for template content.

## Session Reference
- 2026-05-28: Hit this on 3 nodes (246/247/248) with `unsloth.jinja` — recipe had flag but
  container image had no such file. Model snapshot had `chat_template.jinja` but vLLM
  didn't use it (expects exact path match).
