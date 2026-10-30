# Media serving support matrix — GB10 (aarch64, sm_121)

Status: DRAFT (skeleton — being filled in on task t_165c208f). Date: 2026-10-30.

Scope: which candidate image/video models from the 2026-10-30 shortlist run on OUR
hardware — GB10 (DGX Spark: aarch64, compute capability 12.1 / `sm_121`) — under which
server, in which weight format. Media units are model servers with an OpenAI
images/videos API; no ComfyUI on the nodes (operator decision 2026-10-30 15:0x).

Servers evaluated:
1. **SGLang Diffusion** (`sglang` multimodal/diffusion serving path — sparkrun already
   ships an sglang runtime family).
2. **vLLM-Omni** (`vllm-project/vllm-omni`).
3. Third server only if it clearly beats both on GB10.

Weight formats in scope: `diffusers` (BF16/FP8 safetensors layout), ModelOpt **NVFP4**,
**FP8** (e4m3fn). ComfyUI single-file checkpoints are out of scope.

## Hardware constraints that drive every row

(placeholder — filled by research below)

## Matrix

| model | server | weight format on GB10/sm_121 | evidence | verdict | notes |
|---|---|---|---|---|---|
| (fill) | | | | | |

## Shortlist recommendation

(placeholder)

## UNVERIFIED claims

(placeholder)
