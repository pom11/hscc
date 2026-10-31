# Recipe staging (local overlay sources)

Sparkrun recipes this repo **stages** for the fleet's local overlay. These
files are *sources*: `bootstrap.sh` reads `~/.sparkrun-local/recipes`
(`hscc-bootstrap/bootstrap.sh:19`, `RECIPES_DIR`) — it never copies from here,
and nothing here touches `~/.sparkrun-local` automatically. Installing a
staged recipe is an **operator action**:

```bash
mkdir -p ~/.sparkrun-local/recipes/media
cp hscc-cluster/recipes/media/*.yaml ~/.sparkrun-local/recipes/media/
sparkrun recipe validate ~/.sparkrun-local/recipes/media/<file>.yaml   # errors = fatal
sparkrun recipe show     ~/.sparkrun-local/recipes/media/<file>.yaml
```

Rules this directory lives by (fleet precedent: the `local-fixed/` overlay
entries, `patches/MANIFEST.md:65`):

- **Local overlay only.** Never edit upstream/official registry recipes
  (`~/.cache/sparkrun/registries/**`). A fix or a fleet-specific pin gets its
  own file here, exactly like `local-fixed/` does for LLM recipes.
- **Pin everything.** Image by `@sha256:` digest (the tag is documentation
  only), model weights by exact HF repo id, ports explicitly.
- **Model on local NVMe.** NAS is a staging source only — the flash-next
  lesson (`hscc-cluster/templates/4node/flash-next-all.yaml:17-45`): measured
  NFS random-read latency makes disk-resident weights unusable; every staged
  repo must be rsync'd to each serving node's `~/.cache/huggingface/hub/`
  **before** the unit boots (`distribution_config.models.enabled: false` makes
  sparkrun skip download/distribution and serve from the local cache mount).
- **Staging lists are contracts.** Each recipe header carries the exact HF
  repo ids the operator stages to NAS (`/mnt/nas/hub/models--<org>--<repo>`).
  The worker/agent downloads NOTHING; staging is operator-side.

## media/

SGLang Diffusion media units per the operator's locked tier picks
(`docs/design/media-serving.md` §1 + §8 Q1 ruling): image gen
`qwen-image-gen`, image gen speed backup `flux-klein`, image edit
`qwen-image-edit`, video `wan-t2v`, video alternative `ltx`. Design rationale
(read it before editing): `docs/design/media-serving.md` (§1 runtime, §2
facade contract, §5 VRAM budget, §6 staging, §7 security).

Validation without a cluster (offline schema check, the acceptance gate for
these files): `hscc-cluster/tests/test_media_recipes.py` — it loads every
yaml in `media/` and asserts the sparkrun recipe contract that
`sparkrun recipe validate` exercises (parse-time keys, the runtime's readiness
rules, the runtime argv parser's `{model}`/`{model_dir}` convention). CI has
no sparkrun install; the operator-side `sparkrun recipe validate` above stays
the authoritative gate.
