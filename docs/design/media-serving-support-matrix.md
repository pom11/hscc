# Media serving support matrix — GB10 (aarch64, sm_121)

Status: research answer for the 2026-10-30 media workstream (kanban t_165c208f).
Date: 2026-10-30. Sources are public upstream docs/issues/PRs, linked per row.

Scope: which candidate image/video models from the 2026-10-30 shortlist run on OUR
hardware — GB10 (DGX Spark: aarch64, compute capability 12.1 / `sm_121`, 128 GB
unified, ~273 GB/s) — under which server, in which weight format. Media units are
model servers with an OpenAI images/videos API; no ComfyUI on the nodes (operator
decision 2026-10-30). Weight formats in scope: `diffusers` (BF16/FP8 safetensors),
ModelOpt **NVFP4**, **FP8** (e4m3fn). ComfyUI single-file checkpoints are out of
scope. Model naming mirrors the public shortlist.

Servers evaluated:

1. **SGLang Diffusion** — `sglang serve --model-path …` (OpenAI `/v1/images/*`,
   `/v1/videos`). sparkrun already ships an sglang runtime family, so deployment
   friction is lowest here.
2. **vLLM-Omni** — `vllm serve <model> --omni` (OpenAI images API). Same CLI shape
   as vLLM for LLMs.
3. **Third servers** — TensorRT-LLM diffusion was checked (supports Qwen-Image-Edit-
   2511, Wan2.2, LTX-2) but is not shortlisted: its diffusion path targets
   sm_100-class parts, needs per-arch engine re-export for NVFP4, and no GB10/aarch64
   diffusion validation was found. The community `eugr/spark-vllm-docker` stack is
   vLLM-based (not a third server) but carries the one measured FLUX.2-on-GB10
   result — recorded in the FLUX.2-dev row.

## Hardware constraints that drive every row

- **C1 — NVFP4 FP4 GEMM on SM12.x.** FlashInfer's TensorRT-LLM FP4 backend
  (`flashinfer_trtllm`, the B200 default) rejects capability 120/121. Merged fix
  sgl-project/sglang#35739 makes the diffusion side default to `auto` (FlashInfer's
  sm_12x NVFP4 kernel) on SM120, and its gate `is_sm120()` tests *capability major
  == 12*, which covers sm_121 (sgl-project/sglang#30705 validation comment states
  this explicitly for GB10). On the LLM side, FlashInfer 0.6.18 `b12x` is enabled
  for SM121 too (sgl-project/sglang#38170). Practical rule: **NVFP4 GEMM works on
  GB10 only via the cudnn/cutlass/b12x FlashInfer backends, never trtllm**; set
  `SGLANG_DIFFUSION_FLASHINFER_FP4_GEMM_BACKEND=flashinfer_cudnn` where the default
  is wrong (docs.sglang.io quantization guide "Notes").
- **C2 — GB10-validated FP4 numbers exist.** The only fully GB10-validated FP4
  evidence in SGLang is kernel-level (sgl-project/sglang#30705, ran on GB10:
  cudnn FP4 GEMM 2.0–2.9× over BF16; caveat: cudnn *regresses below BF16 without a
  FlashInfer autotune warmup pass on sm_121*). No upstream end-to-end NVFP4
  image/video benchmark on GB10 was found → every NVFP4 model row below carries
  UNVERIFIED-on-GB10 unless a forum/PR says otherwise.
- **C3 — the flashiest working quant on GB10 today is ConvRot INT8.** SGLang's
  online `convrot_int8` JIT ops explicitly support "CC 9.0, 10.0 and 12.0 / 12.1
  (… DGX Spark)" with quality-validated models Qwen-Image, Qwen-Image-Edit,
  MiniMax-H3 (docs.sglang.io quantization guide). FP8 tensor cores work on all
  Blackwell (no caveat).
- **C4 — aarch64 wheels.** sglang: works on GB10/aarch64 (sgl-kernel 0.4.2.post2
  aarch64 wheel + torch 2.11/2.13 + flashinfer 0.6.11+, per #30705/#46390 reports;
  image `lmsysorg/sglang:v0.5.22-cu130` runs on 4× Spark, issue #46390). vLLM core:
  official aarch64 cu130 wheels exist (vllm release pages). **vllm-omni: no
  aarch64 wheel — docker build fails on `fa3-fwd` (x86_64-only wheel), issue
  vllm-project/vllm-omni#1121; community installs it manually from source.**
  Treat "vLLM-Omni on Spark" as DIY-then-test everywhere below.
- **C5 — memory.** 128 GB unified (~119–121 GiB usable) is generous: video models
  that "realistically need 80 GiB VRAM" per the vLLM-Omni quant RFC #1854 fit; the
  Qwen-Image-2.1 SGLang cookbook runs with all weights resident on Spark. No
  shortlist model OOMs a single Spark at its native resolution tier.

## Matrix

Verdicts: **runs** = upstream-verified on GB10/sm_12x; **runs-with-work** = format
+ arch supported upstream but no GB10 validation, or install/layout work needed;
**blocked** = upstream evidence it does not work. "Server: SGLD" = SGLang Diffusion,
"VO" = vLLM-Omni.

| model | server | weight format on GB10/sm_121 | evidence | verdict | notes |
|---|---|---|---|---|---|
| black-forest-labs/FLUX.2-dev-NVFP4 (mixed) | SGLD | ModelOpt NVFP4 (`--transformer-weights-path`, base FLUX.2-dev kept for BF16 TE+VAE) | quantization guide `modelopt-nvfp4` row (FLUX.2 + official repo, weights-path flow): docs.sglang.io/docs/sglang-diffusion/quantization.md; merged sgl#20137; Blackwell correctness sgl#23625; C1/C2 above | runs-with-work | UNVERIFIED on GB10 (docs validate B200/5090). Needs BF16 text encoder + VAE (~48 GB sidecar) — SGLang keeps them from the base repo automatically. trtllm backend must NOT be selected (C1). |
| black-forest-labs/FLUX.2-dev-NVFP4 (mixed) | VO | NVFP4 checkpoint path blocked; online FP8 in flight | vllm-omni RFC #1854: FLUX.2-dev NvFP4 "⏳ #7835 (blocked)" — SM120 test failed (`NVFP4 requires 2D tensor, got 3D` + packed weights never moved to GPU); FP8 D is open PR #3027 | blocked (NVFP4) / not-yet (FP8) | The RFC's own RTX-5090 test = closest arch to ours and it failed. Do not plan on FLUX.2 via vLLM-Omni this cycle. |
| black-forest-labs/FLUX.2-dev-NVFP4 (mixed) | vLLM (spark-vllm-docker) | torchao W4A4 NVFP4 quantized **on the fly** (not the official NVFP4 ckpt) | forums.developer.nvidia.com t376106 (measured on GB10 sm_121a): ~3× over BF16, 28 steps 1024² ≈ 45 s; steady ~66 GB vs ~112 GB BF16; PR eugr/spark-vllm-docker#313 | runs (community) | GB10-MEASURED. But: official prequantized `modelopt_fp4` path fails on sm_121a (diffusers unpack bug — forum + RFC agree); needs `mslk` dep; CUDA 13. First quant pass spikes to BF16 size. |
| black-forest-labs/FLUX.2-klein-9b-nvfp4 | SGLD | NVFP4 raw export — same loader family as FLUX.2-dev NVFP4 | SGLang supports FLUX.2-Klein family (compatibility matrix lists FLUX.2-Klein, docs.sglang.io/diffusion/compatibility_matrix.html); modelopt-nvfp4 row covers "FLUX.2"; C1/C2 | runs-with-work (likely) | UNVERIFIED: the ModelOpt published-checkpoint table covers FLUX.2-dev, not klein-9b-nvfp4 specifically; klein's NVFP4 repo layout needs a load test. 9B DiT (~6 GB NVFP4) = fastest SGLang image option. |
| black-forest-labs/FLUX.2-klein-9b-nvfp4 | VO | GGUF verified (out of format rules); NVFP4 untracked | RFC #1854 rows: FLUX.2-klein GGUF ✅ #1285; no NVFP4 row | blocked-for-our-use | GGUF is excluded by the shortlist's own standard-model rule. Adjacent: FLUX.3 Image Klein ships FP8(`fp8r`)+NVFP4 variants with an SGLang cookbook (docs.sglang.io/cookbook/diffusion/FLUX/FLUX.3.md) — candidate for re-pick. |
| unsloth/Qwen-Image-2.1-FP8 | SGLD | FP8 (serialized component or online `--component-quantizations.transformer fp8`); BF16 native fully Spark-validated | Qwen-Image-2.1 cookbook (docs.sglang.io/cookbook/diffusion/Qwen-Image/Qwen-Image-2.1): **DGX Spark 128 GB row, resident weights, Torch SDPA, gen 35.36 s / edit 42.23 s**, verified with torch 2.13.0+cu130; FP8/NVFP4 exports "checked on B200" | runs (BF16) / runs-with-work (FP8) | The only shortlist tier with a published GB10 latency row at native precision. FP8 on Spark halves DiT memory; speed gain unlikely on a bandwidth-bound GB10 (cookbook/PR #3700 both note FP8 buys headroom, not speed). |
| unsloth/Qwen-Image-2.1-FP8 | VO | FP8 online verified for Qwen-Image family | RFC #1854: Qwen-Image FP8 D ✅ #1034 (`img_mlp` skip recommended); Qwen-Image-2512 same arch; docs.vllm.ai vllm-omni online-quantization page (SM100+ table) | runs-with-work | Qwen-Image-**2.1** is newer than the 2512 rows — same family, but no explicit 2.1 row = UNVERIFIED on VO at any arch; plus C4 install work. |
| Bedovyy/Qwen-Image-Edit-2511-NVFP4 | SGLD | ModelOpt NVFP4, full-repo `--model-path` | quantization guide published-checkpoint table row "NVFP4 / Qwen/Qwen-Image-Edit-2511 / **lmsys/qwen-image-edit-2511-modelopt-nvfp4-sglang** / BF16-vs-NVFP4 B200 comparison" | runs-with-work | Prefer the **lmsys** repo over Bedovyy's third-party NVFP4 (same weights, self-describing ModelOpt config, documented loader path). UNVERIFIED on GB10 (C2); use cudnn FP4 backend (C1). |
| Bedovyy/Qwen-Image-Edit-2511-NVFP4 | VO | BF16 edit API works; FP8 D supported; NVFP4 untracked | vllm-omni Image Edit API doc shows `vllm serve Qwen/Qwen-Image-Edit-2511 --omni`; RFC #1854 Qwen-Image-Edit FP8 ✅ #1034; NvFP4 S rows are "Blackwell only (sm_100)" per RFC hardware section | runs-with-work (FP8) | NVFP4 on VO: no row at all = not supported. NVFP4 checkpoint itself loads as diffusers — but no VO kernel path. |
| T5B/Z-Image-Turbo-FP8 (e4m3fn) | SGLD | FP8 (online `fp8` validated for Z-Image-Turbo; offline component folder supported) | quantization guide `fp8`/online row "Z-Image-Turbo (validated)"; Z-Image-Turbo cookbook docs.sglang.io/cookbook/diffusion/Z-Image/Z-Image-Turbo.md | runs-with-work | Tiny DiT (6 GB) — fastest T2I tier; FP8 works everywhere FP8 tensor cores do. T5B repo = community diffusers FP8 conversion; UNVERIFIED (any server), needs layout check vs official `Tongyi-MAI/Z-Image-Turbo`. |
| T5B/Z-Image-Turbo-FP8 (e4m3fn) | VO | FP8 online ✅ for Z-Image | RFC #1854: Z-Image FP8 D ✅ #1034; online-quantization docs (Qwen-Image and Z-Image validated) | runs-with-work | C4 install work; T5B checkpoint layout UNVERIFIED. |
| lmsys/qwen-image-edit-2511-modelopt-nvfp4-sglang | SGLD | ModelOpt NVFP4 full repo, `--model-path` direct (canonical flow) | quantization guide table (row above) + "Direct `--model-path` loading is the canonical path for full Qwen Image ModelOpt NVFP4 repos"; B200 CI case for the 2512 sibling | runs-with-work | **Top NVFP4 edit candidate.** Same GB10 caveats C1/C2; keep `--dit-layerwise-offload` available, SGLang supports it for ModelOpt. sparkrun already has the sglang runtime → smallest deployment delta. |
| lmsys/qwen-image-edit-2511-modelopt-nvfp4-sglang | VO | not supported (NVFP4 not wired for this pipeline) | RFC #1854 has no Qwen-Image-Edit NVFP4 row; NvFP4 S limited to Blackwell sm_100 rows | blocked | Use SGLD for this checkpoint. |
| Wan-AI/Wan2.2-T2V-A14B-Diffusers (+ nvidia/Wan2.2-T2V-A14B-Diffusers-NVFP4) | SGLD | diffusers BF16 ✅; ModelOpt FP8 transformer override ✅ (`lmsys/wan22-t2v-a14b-modelopt-fp8-sglang-transformer`); NVFP4 override ✅ (`…-nvfp4-sglang-transformer`, primary transformer only, transformer_2 stays BF16) | compatibility matrix (Wan2.2 T2V A14B, 480/720p); quantization guide ModelOpt table rows FP8+NVFP4 Wan2.2; NVFP4 row notes default backend `flashinfer_trtllm` → MUST override to cudnn/cutlass on GB10 (C1) | runs-with-work | UNVERIFIED on GB10 (docs benchmark B200). nvidia/…-Diffusers-NVFP4 = ModelOpt-format repo, same family → likely loads via `--component-paths.transformer`; UNVERIFIED, prefer the lmsys transformer override + base Wan repo. MoE dual-transformer: keep `transformer_2` BF16 per docs. |
| Wan-AI/Wan2.2-T2V-A14B-Diffusers | VO | BF16 supported ✅; ModelOpt FP8 S merged (#3305); online FP8 in flight (#2920/#7825 text-encoder) | vllm-omni supported-models table (`WanPipeline`: Wan2.2-T2V-A14B, TI2V-5B) + recipe Wan2.2-T2V.md; RFC #1854 Wan 2.2 rows | runs-with-work | C4 install work; no GB10 validation found. Video ≈ 80 GiB working set → fits unified memory (C5). |
| Lightricks/LTX-2.5 🔒 (NVFP4) | SGLD | online `--quantization fp8` supported ✅; **NVFP4 not in supported list** | LTX-2.5 cookbook (docs.sglang.io/cookbook/diffusion/LTX/LTX2.5.md: `Lightricks/LTX-2.5-Diffusers`, fp8 halves DiT, −18 GB peak); modelopt-nvfp4 supported-models list has no LTX entry | runs-with-work (FP8) / blocked (NVFP4) | The shortlist's LTX-2.5 NVFP4 variant has no SGLang kernel path today. Serve BF16/FP8 from `LTX-2.5-Diffusers` instead; NVFP4 = watch upstream. |
| Lightricks/LTX-2.5 🔒 (NVFP4) | VO | `LTX2Pipeline` supported ✅ (LTX-2.5-Diffusers); online FP8 ✅ for LTX-2 (DiT #3700, Gemma3 encoder PR #7832); NVFP4 no row | vllm-omni supported-models (`LTX2Pipeline` + recipes/LTX/LTX-2.5.md); RFC #1854 LTX-2 FP8 ✅ + encoder-FP8 validation tables | runs-with-work (FP8) / blocked (NVFP4) | Same conclusion as SGLD: FP8 is the format for LTX on either server. |

Row count: 18 (8 shortlist models × {SGLD, VO} + 2 extra rows recording the
GB10-measured vLLM/torchao FLUX.2 route and the FLUX.2-klein VO row).

## Shortlist recommendation

Per tier, top pick given GB10 reality (server + format):

- **Image generation: `Qwen/Qwen-Image-2.1` (native BF16 or FP8 components) under
  SGLang Diffusion.** The only row with a published DGX Spark number (35.4 s/image
  at native precision, all-resident, verified on the exact arch/OS). FP8 halves
  DiT memory for co-tenancy; do not expect FP8 speedup on a bandwidth-bound GB10.
  Backup speed pick: FLUX.2-klein NVFP4 on SGLang (~6 GB DiT) — UNVERIFIED, needs
  a load test but sits entirely inside supported loader + kernel paths (C1/C2).
- **Image editing: `lmsys/qwen-image-edit-2511-modelopt-nvfp4-sglang` under SGLang
  Diffusion** (`--model-path`, cudnn FP4 backend). Official, self-describing NVFP4
  repo with the canonical loader path; supersedes the Bedovyy third-party NVFP4
  (same base weights, fewer format gambles). If the first GB10 smoke test trips on
  FP4 kernels, fall back to BF16 + `convrot_int8` (C3) — explicitly GB10-capable.
  FLUX.2-dev stays "runs-with-work" via the weights-path flow; only the
  torchao-on-the-fly variant has a real GB10 number (45 s, community image).
- **Video: `Wan-AI/Wan2.2-T2V-A14B-Diffusers` + lmsys ModelOpt FP8 (or NVFP4 with
  cudnn backend) transformer override under SGLang Diffusion.** Most-documented
  video row on both servers; fits unified memory. FP8 is the safer quant than
  NVFP4 on sm_121 until someone measures. **LTX-2.5 must be served from
  `LTX-2.5-Diffusers` in BF16/FP8, not the NVFP4 build** — no NVFP4 path exists on
  either server for LTX.

Cross-cutting: **SGLang Diffusion is the media server for the fleet.** It has (a)
the sglang runtime already in sparkrun, (b) the published GB10 validation (kernel
+ one model recipe), (c) the only working NVFP4 loading story on sm_121. vLLM-Omni
stays a credible second source once `fa3-fwd`/aarch64 packaging (its #1121) is
resolved upstream — track it, don't build on it yet.

## UNVERIFIED claims (explicit flags)

1. Every NVFP4 model row (FLUX.2-dev, FLUX.2-klein, Qwen-Image-Edit-2511 NVFP4,
   Wan2.2 NVFP4) — no upstream end-to-end GB10/sm_121 image/video NVFP4 benchmark
   exists; verified claims stop at B200/RTX-PRO-6000(kernel level on GB10).
   GB10 readiness rests on C1's arch-gate argument + #30705's GB10 kernel numbers.
2. `is_sm120()` covering sm_121 is per #30705's author comment + #38170's
   "capability major == 12" wording — not re-read from current main in this card.
3. FLUX.2-klein-9b-nvfp4 loader compatibility (repo layout vs `--transformer-weights-path`).
4. nvidia/Wan2.2-T2V-A14B-Diffusers-NVFP4 loadability as a ModelOpt component repo.
5. T5B/Z-Image-Turbo-FP8 checkpoint layout vs SGLang serialized-FP8 expectations.
6. unsloth/Qwen-Image-2.1-FP8 under vLLM-Omni (2.1 not in any VO row).
7. vLLM-Omni functioning at all on GB10 after the manual #1121 workaround (a user
   reported successful install, not a model run).
8. vLLM/torchao FLUX.2 GB10 numbers are a community forum post + open PR
   (eugr/spark-vllm-docker#313), not upstream vLLM.
9. TRT-LLM exclusion is absence-of-evidence on GB10 diffusion, not a documented
   failure.
