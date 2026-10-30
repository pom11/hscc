# Media serving on the Sparks — design for operator review (M0)

**Date:** 2026-10-30
**Status:** Approved (operator ruling 2026-10-30 22:33) — design only. No
recipes, no code, no fleet action in this card. §8 carries the inline rulings.
**Card:** t_13c64d9b (media workstream M0).
**Consumes:** the support-matrix answer of parent card t_165c208f — committed
on branch `wt/t_165c208f` as `docs/design/media-serving-support-matrix.md`
(PR #117, not yet merged, hence absent from this branch's tree). This doc does
not restate its evidence table; it builds on its verdicts (SGLang Diffusion =
fleet media server; per-tier picks; NVFP4 kernel caveats C1–C5).
**Operator rulings this doc is written under (2026-10-30):** media units are
**pure model servers on the Sparks** — no ComfyUI on nodes; ComfyUI exists only
as the operator's Mac-side client UI via the later node pack (M5). vLLM-Omni is
framed as an **optional second backend**, not a build dependency.

Sections: 1 runtime · 2 facade API contract · 3 LiteLLM registration ·
4 template schema · 5 VRAM budget · 6 NAS/staging · 7 security boundary ·
8 open questions.

Conventions: `org/project#NNNN` = upstream issue/PR (same citation style as the
matrix); `file.py:NNN` = this repo; `10.0.0.x` = LAN-node placeholder.
Anything not verifiable from a cited source is flagged **UNVERIFIED**.

---

## 1. Runtime per model

**Chosen server: SGLang Diffusion (`sglang serve`) on every media unit**, per
the matrix cross-cutting verdict: the sglang runtime family already ships in
sparkrun, the GB10 kernel validation exists (sgl#30705), and it is the only
working NVFP4 loading story on sm_121. vLLM-Omni (`vllm serve --omni`) stays an
optional second backend per operator — blocked behind its aarch64 packaging
gap (`fa3-fwd`, vllm-omni#1121); nothing in this design requires it.

Per-tier runtime/format pin (from the matrix "shortlist recommendation"; the
operator re-picks the shortlist itself — §8 Q1):

| tier | unit model | server | weight format | GB10 notes |
|---|---|---|---|---|
| image gen | `unsloth/Qwen-Image-2.1-FP8` (same architecture as `Qwen/Qwen-Image-2.1`) | SGLang Diffusion | **FP8 default** — operator standing rule: standard models are NVFP4 or FP8 native, never plain BF16; plain BF16 (`Qwen/Qwen-Image-2.1`) is a documented fallback only if FP8 kernels misbehave on sm_121 | the published DGX Spark row (35.4 s/gen, all-resident) was measured BF16; FP8 halves the DiT footprint (§5), buys memory headroom, not speed on bandwidth-bound GB10 |
| image gen (speed backup) | `FLUX.2-klein-9b-nvfp4` | SGLang Diffusion | ModelOpt NVFP4 | UNVERIFIED load test needed; ~6 GB DiT; FlashInfer cudnn FP4 backend only (matrix C1) |
| image edit | `lmsys/qwen-image-edit-2511-modelopt-nvfp4-sglang` | SGLang Diffusion | ModelOpt NVFP4, `--model-path` direct | canonical loader path; fall back to BF16 + `convrot_int8` (matrix C3, explicitly GB10-capable) if FP4 kernels misbehave |
| video | `Wan-AI/Wan2.2-T2V-A14B-Diffusers` + lmsys ModelOpt FP8 transformer override | SGLang Diffusion | FP8 transformer (lmsys override; NVFP4 transformer variant = opt-in, cudnn backend); `transformer_2` stays BF16 — **forced exception** to the never-plain-BF16 rule: the MoE dual-transformer ships an FP8 override for one expert only, no published FP8/NVFP4 build of the second (docs); ~80 GiB working set fits unified memory |
| video (alt) | `Lightricks/LTX-2.5-Diffusers` 🔒 | SGLang Diffusion | BF16/FP8 — **never NVFP4** | no NVFP4 kernel path on either server (matrix) |

Runtime shape per unit (design contract for M1's recipe; flags per
docs.sglang.io diffusion API + quantization guide):

- one `sglang serve` process per unit, `--num-gpus 1`, one model per process
  (diffusion pipelines have no multiplexing equivalent to vLLM batching across
  models);
- `--model-path` = the node-local NVMe cache path (§6), `--served-model-name` =
  the alias (§4 — same "concrete alias" pair that LLM units already use,
  `cluster_template.py:437`);
- NVFP4 units pin `SGLANG_DIFFUSION_FLASHINFER_FP4_GEMM_BACKEND=flashinfer_cudnn`
  in the recipe env — never trtllm on sm_121 (matrix C1);
- memory-shed flags available per unit as declared recipe knobs, not template
  fields: `--text-encoder-cpu-offload`, `--pin-cpu-memory`,
  `--dit-layerwise-offload` / `--layerwise-offload-components` (used by
  upstream recipes on smaller cards; on a 128 GB GB10 they exist to make
  co-tenancy, §5, possible, not to make boot possible);
- weight format on disk is always a **diffusers-layout directory** (safetensors
  + components) or a full ModelOpt repo — ComfyUI single-file checkpoints are
  out per the operator ruling; HF repo layout, so the same local-cache
  convention as LLM units applies (§6).

**Why not the other candidates** (one line each, detail in the matrix):
vLLM-Omni — no aarch64 wheel, DIY install then hope (matrix C4);
TensorRT-LLM diffusion — no GB10/aarch64 diffusion validation, per-arch engine
re-export for NVFP4; community `spark-vllm-docker` torchao path — real GB10
numbers but vLLM-based, community-maintained, first-pass memory spike.

## 2. Facade API contract per unit

**Headline finding (drives the M2 gate): the chosen server already speaks the
whole contract natively.** SGLang Diffusion's HTTP server implements an
OpenAI-compatible surface for exactly the endpoints this workstream needs —
verified against the upstream OpenAI-API doc (docs.sglang.io/docs/sglang-diffusion/api/openai_api)
on 2026-10-30:

| contract endpoint | native? | notes |
|---|---|---|
| `GET /v1/models` | yes | mode-tagged: each entry carries `task_type` (e.g. `T2V`) + `supported_task_types` + `dit_precision`/`vae_precision`/`pipeline_class` — the mode tag for §3/§4 comes free |
| `POST /v1/images/generations` | yes | JSON body; OpenAI fields (`prompt`, `size`, `n`, `response_format=b64_json|url`) + SGLang extensions (`task_type`, `quality`, `enhance_prompt`, model-specific fields top-level or via `extra_body`); `b64_json` or relative URL `/v1/images/<id>/content` |
| `GET /v1/images/{id}/content` | yes | download for `response_format=url` |
| `POST /v1/images/edits` | yes | multipart (`image=@file`, `prompt`, `size`, `response_format`) |
| `POST /v1/videos` | yes | **async**: returns a job object (`id`, `status`) — no bytes in the create response; JSON or multipart (`input_reference=@file`, `reference_url`) |
| `GET /v1/videos/{id}` | yes | status poll: `queued → in_progress → completed|failed`, `progress` 0–100 (denoise estimate, best-effort) |
| `GET /v1/videos/{id}/content` | yes | MP4 download when `completed` |
| `GET /v1/videos` | yes | job list, paginated (`limit` 1–100, `after`, `order`) |
| `DELETE /v1/videos/{id}` | yes | removes record + best-effort cancellation (cancellation is single-node-monolithic only) |

**Facade scope ruling proposed to the operator (§8 Q3): archive M2.** There is
no missing endpoint to translate. The facade card was pre-emptively scoped to
"whatever the chosen server lacks"; per the table above that set is empty. If
the operator still wants a facade, the honest residual value is three things
the server deliberately does *not* do (each spec'd below, none requiring a new
service today): durable job ownership across restarts, queue admission caps,
and output retention. All three are listed by upstream as "require an external
serving layer" concerns — the recommendation is to accept the process-local job
store for cycle 1 and revisit only if it actually bites.

Contract details the fleet commits to regardless (each is what clients —
LiteLLM, Hermes, the M5 node pack — may rely on; all satisfied natively):

- **Async job semantics for video**: create → job id → poll → content. An
  accepted job survives client disconnect; polling is cheap GETs. Per the
  operator ruling (§8 Q5) there is **no client-side deadline and no
  client-imposed size/length caps** — the client polls until the job reaches
  `completed`/`failed`; the only client pacing rule is a sane polling interval
  (~5 s recommended, M6 default).
- **Sticky-job constraint (real, and the reason for §3's registration shape):**
  video job metadata is **process-local** — it does not survive an HTTP server
  restart, and when load balancing independent HTTP servers, *poll and content
  requests must route to the server that accepted the job*. Independent servers
  do not share a job store. Consequence: **a media model alias must have
  exactly one healthy backend** in LiteLLM's model group (§3). This is a design
  rule, not a preference — LiteLLM `simple-shuffle` would otherwise round-robin
  polls across replicas and 404 them.
- **b64_json/url exactness**: image responses match OpenAI (`data[].b64_json`
  or `data[].url`, URL form = relative `/v1/images/<id>/content` when cloud
  storage is unconfigured). Clients must accept relative URLs (proxy-side base
  resolution, §3).
- **Error shapes**: the server returns OpenAI-style errors on bad task/size/
  conditioning (HTTP 400 for unsupported task, wrong output type for endpoint,
  missing conditioning). LiteLLM adds its own `Invalid model name`-class 400s
  upstream of the backend. No error translation needed.
- **Timeouts / sizing caps** (design values; enforcement point in parens):
  image sync request ≤ **300 s** end-to-end (proxy per-deployment timeout, §3;
  measured native is ~35–45 s, the margin covers cold path + queue); no video
  client deadline and **no client-side size/seconds caps** (§8 Q5 ruling — the
  server validates requested size/length against model-supported values; M6
  ships request defaults, not ceilings); image `n ≤ 2` stays a client-side
  courtesy default in M6, not a cap. Server-side bounded
  scheduler wait: `--scheduler-rpc-timeout` per recipe (marks a job `failed`,
  does not cancel GPU work — documented caveat).
- **Output retention**: `DELETE` does not remove generated files and job
  records never expire on their own (unlike OpenAI's `expires_at`); the recipe
  pins `--output-path` to a bounded scratch dir on the node and cleanup is the
  daemon's domain, not this design's (flagged §8 Q5).

## 3. LiteLLM registration

Live facts this section is written against (orchestrator-verified 2026-10-30,
same-day re-reads where marked): the proxy is **LiteLLM 1.82.6 on :4000**
(pinned by sparkrun's launch argv). Both `/v1/images/generations` and
`/v1/videos` **routes EXIST and answer `Invalid model name`** — i.e. the routes
are registered, only backends are missing. Re-verified today against the
1.82.6 wheel: `LiteLLMRoutes.openai_routes` carries
`/v1/images/generations`, `/v1/images/edits`, `/v1/videos`,
`/v1/videos/{video_id}`, `/v1/videos/{video_id}/content`,
`/v1/videos/{video_id}/remix` (litellm/proxy/_types.py:275–291), and
`video_generation` is a first-class route name alongside `image_generation`.

**Registration mechanism: sparkrun autodiscovery, no hand-edits.** Per the
standing doctrine (sparkrun owns the proxy; `litellm_config.yaml` is
regenerated by sparkrun and "the only way sparkrun mutates the proxy's model
list"; runtime `/model/new` needs a DB and answers 500), media units register
the same way LLM units do today:

1. The unit launches with `--served-model-name "<concrete> <alias>"` (the
   existing space-separated pair, `cluster_template.py:437`), so SGLang's
   `/v1/models` advertises both names.
2. sparkrun autodiscovery polls each endpoint's `/v1/models`, emits one
   `model_list` entry per advertised name with
   `litellm_params.model: openai/<name>`, `api_base: http://<node>:<port>/v1`
   (`build_litellm_config`, sparkrun `src/sparkrun/plugins/litellm/engine.py:148+`).
3. Stable aliases additionally go through `~/.config/sparkrun/proxy.yaml`'s
   existing `aliases:` block (alias → target model), which sparkrun expands to
   extra model_list entries — the same path `worker-model` uses.

**Mode tag — `model_info.mode`.** LiteLLM's `ModelInfo.mode` defaults to
`chat` (litellm/types/router.py; 1.82.6's `ModelInfo` is `extra="allow"`, and
the router reads `mode` for capability routing, `router.py:7682`). The images/
videos route dispatch itself is by URL, not by mode — but `mode` is what makes
`/v1/models`, key-scope checks, and cost tracking tell the truth about a media
model, and a `chat`-mode tag on a diffusion model is exactly the kind of
lie that surfaces later as "proxy listed it under chat". Two-step plan:

- **Cycle 1 (works today, zero sparkrun change):** nothing blocks media
  traffic; the entries exist and the routes dispatch. The mode tag is absent
  (sparkrun emits `model_info.max_input_tokens` only, and media units report
  `max_model_len: null`).
- **Smallest sparkrun addition (proposed, upstream):** in
  `build_litellm_config`, when a discovered endpoint's `/v1/models` entry
  carries `task_type`/`supported_task_types` (which SGLang Diffusion does,
  §2 — this is the *mode-tagged* `/v1/models` the contract asked for), emit
  `model_info: {mode: image_generation|video_generation}` mapped from the task
  types (`*2I|i2i`→`image_generation`, `*2V|i2v|t2v`→`video_generation`). This
  is the same shape as the per-deployment `model_info` extras contract already
  spec'd as pending in `docs/design/2026-10-15-template-declarations.md` §4.3 —
  media gives that pending hook a concrete consumer. Do **not** hand-edit
  `litellm_config.yaml` and do not add a second config source.

**One backend per media alias (load-balancing rule).** From §2's sticky-job
constraint: a media model name must resolve to exactly one healthy deployment.
Consequences for templates (§4): a media family's `workers:` is the count of
*distinct media models* (or idle spares), never N replicas of one model;
sparkrun's `least-in-flight` balancer and `simple-shuffle` both fan a model
group across replicas per request, which breaks video poll routing. If a media
unit dies, its jobs die with it (process-local store) — self-heal relaunches
the unit, clients re-submit; that is the accepted cycle-1 failure semantics.

**What the proxy does NOT get:** model-group fan-out for media (above),
`api_base` exposure past the LAN (§7), or key-scope changes. The master-key
auth model is unchanged; media requests carry the same bearer as chat.

## 4. hscc template schema extension

Additive, byte-identity-preserving (the schema-v3 pattern: new keys default to
sentinel and `to_dict()` omits them, `template_intent.py:30-38`):

- **`kind: chat|image|video` on `ModelIntent`** (`template_intent.py:64`),
  default `chat`. `to_dict()` emits `kind` only when ≠ `chat`, so every
  existing template round-trips byte-identically (the test the v3 additions
  pass — `tests/_v2_parsed_golden.json`). `chat` is spelled out (rather than
  `None`) because "is this a media unit" is a question three consumers ask,
  and an explicit default beats absence-checking at every call site.
- **`ResolvedUnit.kind`** (`template_intent.py:334`) — plumbed through
  `resolve()` unchanged; media placement rules (§5) read it.
- **serving.json**: `kind` is written into a unit record **only when
  non-chat** (mirrors the `uniform_pool` convention, `template_intent.py:715-
  718`) so existing consumers (keepalive, watchdog, engine-wedge) see a
  byte-identical file until someone declares a media unit.
- **serve-cmd rendering is UNCHANGED.** `_render_serve_cmd`
  (`cluster_template.py:422`) is already model-agnostic: it renders
  `sparkrun run <recipe> --cluster --hosts --port --served-model-name … --tp
  [--gpu-mem]` and all model-specific argv (diffusion flags, sampler knobs,
  quantization env) lives in the **recipe** (M1). "No chat sampler knobs" is
  the flip side of this: the template surface carries no sampler knobs for any
  unit today (no `max_model_len`, no `temperature`), so media needs no
  *subtraction* — the media recipe simply ships diffusion knobs
  (`--num-gpus`, offload flags, FP4 backend env) where a chat recipe ships
  `--max-model-len`/KV flags.
- **Drift compare: zero impact by construction.** serve-cmd normalization
  (`_serve_cmd_flags`, `serve_cmds_equivalent`, `cluster_template.py:455+`)
  compares rendered argv; `kind` does not appear in argv, so declaring or
  changing `kind` on an already-provisioned unit produces no phantom drift.
  Changing the *recipe* is (and must remain) real drift. The one new rule:
  a `kind` flip (`image`→`video`) without a recipe change is incoherent —
  `validate` rejects it (same pipeline that rejects out-of-range
  `gpu_memory_utilization` at parse time, `_gpu_mem_from`).
- **One serving-template/workflow file per alias.** New location
  `hscc-cluster/templates/media/<alias>.yaml` — a **client-facing defaults
  file, not an apply input**. Contents: the OpenAI-call shape this fleet
  promises for that alias — `kind`, default `size`, default `steps`,
  default `seed` (`null` = random), `n` cap, video `seconds`/fps tier, plus
  the request `model` id to send to `:4000`. Consumers: Hermes `image_gen`/
  `video_gen` config recipe (M6), the M5 ComfyUI node pack, humans. Apply
  never reads it; it carries no IPs/ports (topology-free like every template);
  drift-compare never reads it. Its hash is *not* part of unit identity —
  defaults changes are client policy, not fleet state (deliberate: otherwise
  every prompt-default tweak reads as fleet drift).
- **validate/doctor legs (design, built later):** validate = `kind` coherent
  with recipe (above) + media units obey §5 placement rules + `workers:` count
  vs distinct models (one-backend-per-alias, §3). doctor = probe the unit's
  `/v1/models` and warn when advertised `supported_task_types` disagree with
  the declared `kind` (mapping is best-effort; flagged **UNVERIFIED** until
  the first live unit shows the exact enum set — the doc example shows `T2V`
  but the image-side enums are not enumerated upstream).

## 5. GPU memory budget vs LLM units on the same node

Mechanism: the existing per-unit `gpu_memory_utilization:`
(`ModelIntent`, v3) → `--gpu-mem` → recipe → engine flag. No new knob. The
media-specific content is the **sizing rules**, because diffusion memory
behavior differs from LLM serving in one way that matters: **there is no
pre-allocated KV reservation.** A diffusion unit holds its weights resident and
spikes with working buffers during denoise; `gpu_memory_utilization` on
SGLang-Diffusion caps what the engine will *claim*, so the number a media unit
needs is `weights + measured peak activations + margin`, not `weights + KV`.

Co-location rules (extend the templates/README sizing rule — each unit's share
must exceed its per-GPU need, and the sum across co-located units stays ≤ ~0.8;
on GB10 unified memory the remainder is *not* slack — the flash-next template
header says why):

1. **Media units co-locate freely — no video-exclusive-node rule** (§8 Q6
   ruling). Video and image units are placed like any other unit: unpinned,
   free co-tenancy with LLM units, no dedicated media node from the start.
   The single hard rule is the **fraction sum ≤ 0.8** across units on a node,
   and `validate` enforces it (rule 3 below); placement stays the balancer's
   job. The Wan2.2 working set (§5 rule 3's declared fraction) is what keeps
   a video unit from being co-scheduled into a starving node — not exclusivity.
2. **Image units may co-locate with one LLM unit** at declared fractions:
   the image unit gets `gpu_memory_utilization: 0.25–0.35` (Qwen-Image-2.1
   FP8 DiT + T5 + VAE: ≈ 55–60 GB of *claimed* memory at BF16, ~half the DiT
   in FP8; comfortable
   at 0.4 on a 119 GiB-usable card — the FP8 default (§1) is what makes 0.25–0.35 work), the LLM
   partner keeps its own fraction, **sum ≤ 0.8**. The whisper recipe
   (`~/.sparkrun-local/recipes/local-fixed/whisper-large-v3-vllm.yaml`,
   `gpu_memory_utilization: 0.1` side-by-side replicas) is the fleet precedent
   for a small model server sharing a node.
3. **The VRAM sum check at resolve must count media units.** The existing
   colocated-models check sums weights + KV; for a media unit the weight sum
   must come from the unit's declared tier (recipe metadata — M1's recipe
   header carries the number the same way LLM recipes do) and the check's
   known blind spot stays honestly documented: reservation fraction ≠ weight
   sum (templates/README: "VRAM check does not catch it and cannot"). Media
   units are checked against BOTH: declared fraction ≥ declared weight+peak
   estimate, and fraction sum ≤ 0.8.
4. **Watchdog denominator:** media units appear in `serving.json` (§4) so the
   daemon's engine-wedge/keepalive math sees them; the streaming-probe concept
   does not apply to a diffusion unit (no tokens) — a media unit's liveness
   probe is `GET /v1/models` reachability, not a generated token. (Daemon-side
   work is a later card; recorded here so nobody "fixes" the wedge check with a
   chat probe against a diffusion port.)

## 6. NAS / staging paths and the local-NVMe rule

- **Source of truth**: the NAS. Mac mount `/Volumes/NAS` (nfs), node-side
  `/mnt/nas`; the operator's staging script (operator-owned, runs at :00
  cadence) populates `/mnt/nas/hub/models--<org>--<repo>` HF-cache layout.
  Media checkpoints land there in exactly the HF `hub/` layout LLM models use,
  because `sglang serve --model-path` consumes diffusers directories / ModelOpt
  repos from disk. **Mac-side staging caveat (measured 2026-10-30):** the Mac
  (uid 501) cannot write `/mnt/nas/hub` (uid-1000-owned over NFS), so
  Mac-staged repos land in `/Volumes/NAS/hub-mac/` and need a uid-1000 `mv`
  into `hub/` on the NAS before any node can consume them.
- **The flash-next lesson, applied without re-litigating: the model must be on
  LOCAL NVMe before the unit boots.** The measured NFS numbers (4 KiB random
  5.72 ms — the per-token-read killer documented in
  `hscc-cluster/templates/4node/flash-next-all.yaml:17-45`) were about a
  per-token disk-read pattern, but the general rule is what the template
  states: **"NAS is a STAGING source only."** For media the concrete risk is
  boot time and re-serve latency: a cold `sglang serve` reading a 60–100 GB
  checkpoint over NFS at 224 MB/s sequential is 5–8 minutes per boot *before*
  warmup, and every self-heal relaunch pays it; from local NVMe the same load
  is tens of seconds. Media units therefore use the same container convention
  as the LLM units: `distribution.model.enabled: false` (M1 recipe) and the
  model present at the node-local `~/.cache/huggingface/hub/…` (mounted
  `/cache/huggingface`, `HF_HUB_CACHE`) on every node that serves it.
- **Fan-out** is the flash-next recipe verbatim, size-adjusted (an image DiT
  is 6–60 GB vs flash-next's 98.6 GB — most image tiers fit the 10G link in
  minutes): pull once on a 10G node, node→node over the CX7 fabric; on the
  slow-link nodes, still pull from the NAS but expect the measured link
  penalty. Staging itself stays operator-side (the 16:00-style runs); hscc
  builds no staging code in this workstream.
- **Cache eviction policy**: media checkpoints are large and node-local disk
  is finite (the gateway boot-disk-full warning in `templates/4node/balanced.yaml`
  is the cautionary precedent). Rule: **one model set per unit slot, no LRU
  by the agent.** Deleting a staged checkpoint is operator action, same
  standing rule as every other cache dir. `validate` (design-time) refuses a
  template asking for the same model on more nodes than the operator has
  staged it on **only when** the check is cheap — cycle 1: doctor *warns* on
  "unit wants local model X, `sparkrun show` reports X absent on node N";
  apply-time hard-fail waits for the M1 recipe to prove the failure mode.

## 7. Security boundary

- **Media unit ports are LAN-only, never exposed past the node.** Same posture
  as every vLLM port today: bind inside the LAN, reachable from the gateway
  and the Mac, never routed onward. Nothing in this design adds a listener
  outside that perimeter: the unit's HTTP port (8000+ / recipe default 30000
  remapped by the recipe, not by the template), its upload dir, and its
  `--output-path` scratch dir all live on the node.
- **`proxy :4000 remains the single entry point** for all media traffic,
  exactly as chat: bearer master-key auth at the proxy, `api_key: not-needed`
  node-side (sparkrun's standard posture), no per-unit keys, no DB. Clients
  (Hermes, ComfyUI node pack) talk to `:4000/v1/…` and never to a node address
  — which is also what makes the one-backend rule (§3) invisible to clients.
- **LoRA/admin routes stay inside the LAN consequence**: SGLang exposes
  `/v1/set_lora`, `/v1/merge_lora_weights` etc. on the *same* port as
  generation. They are not proxied (LiteLLM surfaces only the OpenAI route
  set; these paths get 404/403 at the proxy — same as today's
  `/server_info`-class paths) and LAN-only + master-key-at-proxy means the
  only party that can call them is someone already inside the perimeter.
  Recorded so an M5/M6 card doesn't "helpfully" proxy them.
- **No-auth ComfyUI applies to the MAC-side app only.** ComfyUI runs on the
  operator's Mac as a client UI (M5 node pack); its notorious no-auth posture
  is a workstation concern — it connects out to `:4000`, it is not a fleet
  service, and no node serves it. This doc's "no auth on media units" never
  refers to ComfyUI on nodes because that thing does not exist (operator
  ruling).
- **Upload handling**: image/video input uploads are written under the
  server's upload directory with sanitized filenames (native SGLang behavior);
  no NAS write-back — generated media is delivered to the client
  (b64/URL/job-content download), and the node-side copies follow §2's
  retention point.
- **No secrets in play**: media models are open-weight (or 🔒-gated but
  locally-staged), so unlike future API-backed tiers there is no key material
  in recipes, templates, or `model_list` entries. `api_key: not-needed` stays
  literal, and the address-guard rule (placeholders only — `10.0.0.x`) applies
  to every committed artifact this workstream produces.

## 8. Open questions for the operator — RULED (operator, 2026-10-30 22:33)

1. **Shortlist re-pick.** The matrix recommends per tier: image gen
   Qwen-Image-2.1 in FP8 (operator never-plain-BF16 rule; BF16 fallback only
   on kernel trouble), edit lmsys Qwen-Image-Edit-2511 NVFP4, video
   Wan2.2 + lmsys FP8 transformer; LTX-2.5 only in BF16/FP8. FLUX.2-dev stays
   runs-with-work, and FLUX.3 Image Klein (FP8+NVFP4, SGLang cookbook) is a
   plausible late addition. Which models get M1 recipes + template slots first?
   (M1 is blocked on nothing except this ruling + §1.)
   **RULING Q1 — first tier locked:** image gen = **Qwen-Image-2.1 FP8**
   (diffusers layout; BF16 documented as fallback only, never the default);
   speed backup for image gen = **FLUX.2-klein-9b NVFP4**; image edit =
   **lmsys/qwen-image-edit-2511-modelopt-nvfp4-sglang**; video =
   **Wan2.2-T2V-A14B + lmsys FP8 transformer** (the `transformer_2` BF16
   component is the one *forced exception* to the never-BF16 rule — fp32
   master-weights module, documented in M1's recipe); video alternative =
   **LTX-2.5 FP8**. M1 recipes + M4 template slots cover exactly this set.
2. **vLLM-Omni as second backend.** Framed here as optional and tracked, not
   built (its aarch64 packaging fix vllm-omni#1121 is open). Confirm: no M-card
   invests in vLLM-Omni until upstream ships aarch64, or want a spike card the
   moment it does?
   **RULING Q2 — watch line only:** no M-card invests in vLLM-Omni. It stays
   one tracked watch line (vllm-omni#1121); re-open only when upstream ships
   aarch64. No spike card before then.
3. **M2 facade: archive?** §2's table says the chosen server natively covers
   every contract endpoint including async `/v1/videos`. Recommendation:
   archive the conditional M2 card; keep the three residuals (durable jobs,
   queue caps, retention) as cycle-2 watch items if real usage demands them.
   This is the explicit human gate on that card — ruling requested.
   **RULING Q3 — archive M2.** Native SGLang-Diffusion covers every endpoint
   (§2 table); the facade card is archived. Durable jobs / queue caps /
   retention stay cycle-2 watch items, revisited only if real usage bites.
4. **sparkrun `model_info.mode` addition** (§3): approve proposing the
   smallest sparkrun-side patch — map discovered `task_type`/
   `supported_task_types` from `/v1/models` to `model_info.mode`
   `image_generation`/`video_generation` in `build_litellm_config`. Cycle 1
   works without it; with it, `/v1/models` at `:4000` stops lying about media
   models. Same hook family as template-declarations §4.3.
   **RULING Q4 — yes via upstream PR route.** A dedicated card files the
   smallest focused diff in `build_litellm_config` on our sparkrun fork
   (branch off upstream main, conventional commit, unit tests incl. a
   chat-unchanged regression, zero behavior change for LLM endpoints). No
   push / no PR open until the operator reviews the diff. This stays the ONLY
   sanctioned sparkrun edit; hscc cards never touch sparkrun.
5. **Video polling caps + retention.** Proposed client deadline 1800 s /
   720p tier (upstream cookbook's own value), poll every 5 s; `--output-path`
   scratch dir per unit with who-cleans-it left to a daemon-side card. Accept,
   or pick different numbers — these become M6 defaults.
   **RULING Q5 — no client caps.** The 1800 s deadline, the 720p tier cap and
   the ≤ 8 s length cap are DELETED from this design (see §2). M6 gets a sane
   polling interval only (~5 s); no size/seconds/deadline caps in defaults.
   `--output-path` scratch dir + daemon-side cleanup stays as written.
6. **Co-tenancy ruling** (§5): confirm cycle-1 policy — video units exclusive
   node, image units shareable at fraction sum ≤ 0.8 — and whether any node is
   designated media-dedicated from the start or media units place anywhere
   (unpinned like `flash-next-all`).
   **RULING Q6 — free co-tenancy, no video-exclusive rule.** §5 rule 1 is
   rewritten accordingly: no video-exclusive-node rule, no media-dedicated
   node, media units unpinned like `flash-next-all`. The only hard rule is
   fraction sum ≤ 0.8 per node and `validate` enforces it.
