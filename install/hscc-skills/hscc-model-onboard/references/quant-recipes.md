# Quant recipes for DGX Spark (GB10 / sm_121a)

How to author a sparkrun recipe that actually loads a quantized Qwen3.6-35B-A3B
checkpoint on the DGX Spark GPU (NVIDIA GB10, Blackwell, compute `sm_121a`),
running fully **offline** against the NAS-populated cache.

Canonical working recipe:
`~/.sparkrun-local/recipes/local-fixed/qwen3.6-35b-a3b-nvfp4-vllm.yaml`.

## NVFP4 (NVIDIA modelopt 4-bit)

Model: `nvidia/Qwen3.6-35B-A3B-NVFP4`. The checkpoint is **MIXED_PRECISION** —
vLLM detects `quantization=modelopt_mixed` and serves `lm_head` as W4A16_NVFP4.

### The one thing that makes it load: `mods/exp-w4a16`

A stock vLLM build crashes loading this checkpoint with a missing
`lm_head.input_scale` (the W4A16 path isn't wired for the mixed-precision head).
The fix is the eugr mod **`mods/exp-w4a16`**, which applies upstream vLLM PRs
**#42124, #42566, #42546** at container start (`git apply`, idempotent — it
`--reverse --check`es first). Put it **first** in the recipe's `mods:` list:

```yaml
mods:
  - mods/exp-w4a16          # <-- enables W4A16 NVFP4 lm_head; must be present
  - mods/fix-qwen3-coder-next
  - mods/fix-qwen3.5-chat-template
```

> The mod `curl`s the PR diffs at run time, so the **head node needs internet
> when the container first launches**. Weights stay offline; only the patch
> fetch is online.

### Required defaults / env (DGX Spark)

```yaml
defaults:
  quantization: modelopt
  kv_cache_dtype: fp8
  moe_backend: marlin
  attention_backend: flashinfer
  gpu_memory_utilization: 0.8
  max_model_len: 262144

env:
  CUTE_DSL_ARCH: sm_121a               # Blackwell GB10 arch
  VLLM_USE_FLASHINFER_MOE_FP4: "0"
  VLLM_FP8_MOE_BACKEND: flashinfer_cutlass
  FLASHINFER_DISABLE_VERSION_CHECK: "1"
  VLLM_MARLIN_USE_ATOMIC_ADD: "1"
  HF_HOME: /cache/huggingface          # bind-mounted from /home/spark/.cache/huggingface
  HF_HUB_OFFLINE: "1"                   # never re-download at serve time
  TRANSFORMERS_OFFLINE: "1"
```

Serve flags that matter: `--quantization modelopt --kv-cache-dtype fp8
--moe-backend marlin --attention-backend flashinfer --trust-remote-code
--chat-template unsloth.jinja`.

## FP8

Model: `Qwen/Qwen3.6-35B-A3B-FP8`. Loads on stock vLLM (no `exp-w4a16` needed).
Same DGX Spark env block; `quantization` is auto-detected (or `fp8`). Keep the
qwen3 chat-template + coder mods.

## Pitfalls (learned the hard way)

- **`--chat-template unsloth.jinja` must exist INSIDE the container.** vLLM
  validates the path at startup and dies with "appears path-like, but doesn't
  exist!" if it's missing. Check: `docker exec <ctr> find / -name unsloth.jinja`.
- **`HF_HUB_OFFLINE=1` only stops the *container* re-downloading** at serve time.
  It does NOT make sparkrun's distribution step offline — that runs `hf download`
  on the host. Pre-populate node caches from NAS first (see `nas-cache.md`).
- **VRAM estimator prints "Unknown dtype 'modelopt'"** — harmless; it can't size
  modelopt weights, the model still fits (35B-A3B is ~23 GB of weights).
- **Don't edit the canonical sparkrun recipes.** Author quant variants as NEW
  files under `~/.sparkrun-local/recipes/local-fixed/` and reference mods; never
  mutate shipped recipes.
- **Recipe must declare `model:` exactly** matching the HF repo id — `onboard.py`
  and the NAS cache lookup both key off it (`org/model` -> `models--org--model`).
