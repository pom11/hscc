---
name: hscc-model-onboard
description: Bring a NEW model online across the DGX Spark cluster end-to-end — author/validate a sparkrun recipe, populate the offline NAS->node HF cache, wire every HSCC config layer, launch vLLM, and verify. Use when switching the cluster to a new model or quant (e.g. FP8 -> NVFP4).
category: hscc
domain: cluster control, model onboarding, cutover
platform: macOS CLI
version: 1.0.0
license: MIT
metadata.hermes.tags: []
---

# hscc-model-onboard

One-command onboarding for a **new** model or quant across the whole cluster.
Where the hscc-cluster toolset's `provision_model` tool runs *existing* recipes,
this skill covers the steps *before* that: getting a recipe right, getting the
weights onto every node from NAS **offline**, and wiring the 7 config layers
that route the fleet — then launching and verifying.

Use it for a full cutover (e.g. Qwen3.6-35B-A3B **FP8 -> NVFP4**) or to add a
brand-new model.

## The tool

`scripts/onboard.py` — stdlib-only Python, phase-based. Topology is **read from
`~/.hscc/config.json`** (gateway + workers + nas_ip + ssh_user + vllm_port), never
hardcoded, so it can't drift from the real cluster.

```
python3 ~/.hermes/skills/hscc-model-onboard/scripts/onboard.py <phase> ...
```

| Phase | Args | Mutates | What it does |
|-------|------|---------|--------------|
| `plan`   | `<model> <recipe>` | no | Print topology, recipe model check, NAS + per-node cache state, and the config changes `wire` would make. **Always run first.** |
| `cache`  | `<model> [recipe]` | nodes' HF cache (additive) | Verify model on NAS; rsync NAS->each serving node's local cache. Skips nodes already complete. If `recipe` is given and missing, scaffolds a starter and stops. |
| `wire`   | `<model> <recipe> --yes` | HSCC config | Backup, then update serving.json, models.json, provision.json, agents.json, hermes config.yaml + worker profiles. |
| `launch` | `<recipe> --yes` | cluster | `sparkrun run <recipe> --hosts <node>` on every serving node. |
| `verify` | `<model> [--wait]` | no | `/health` + a coherent `/v1/chat/completions` on every serving node. `--wait` polls up to 5 min for slow loads. |
| `all`    | `<model> <recipe> --yes` | all of the above | plan -> cache -> wire -> launch -> verify. |

**Mutating phases require `--yes`.** Without it they print what they would do and stop.

## Standard workflow (cutover)

```bash
S=~/.hermes/skills/hscc-model-onboard/scripts/onboard.py
M=nvidia/Qwen3.6-35B-A3B-NVFP4
R=~/.sparkrun-local/recipes/local-fixed/qwen3.6-35b-a3b-nvfp4-vllm.yaml

python3 $S plan   $M $R          # 1. inspect — no changes
python3 $S cache  $M $R          # 2. offline NAS -> every node (idempotent)
python3 $S wire   $M $R --yes    # 3. wire config (auto-backup)
python3 $S launch $R --yes       # 4. start vLLM on all nodes
python3 $S verify $M --wait      # 5. health + completion
```

`all` does 1-5 in one shot; prefer the step-by-step form for a first cutover so
each phase is inspectable.

## What `wire` changes (and what it preserves)

| File | Change | Preserved |
|------|--------|-----------|
| `~/.hscc/serving.json` | every unit `model` + `recipe` | `nodes`, `role`, `max_workers` |
| `~/.hscc/models.json` | `primary_model`, `models[0].name`, status=cached | `base_url` (gateway), provider |
| `~/.hscc/provision.json` | active `mappings[*].recipe` | `history` (audit log left intact) |
| `~/.hscc/agents.json` | only the `<model>` part of each `vllm-<ip>/<model>` | **per-agent routing IP** |
| `~/.hermes/config.yaml` | `model.default` line only | everything else incl. **secrets** |
| `~/.hermes/profiles/worker-*/config.yaml` | `model.default` | per-node `base_url` |

Backup is written to `~/.hscc/_onboard-backup-<timestamp>/` before any write.

## After wiring: services

`wire` does NOT restart anything. The gateway + daemon keep the **old** model in
memory until restarted. Containers can be up and healthy (verify passes) while
the fleet still routes to the old config. Restart only on explicit go:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.hermes.gateway.plist
```

## Recipe authoring

`onboard.py` validates that the recipe's `model:` matches and (for a missing
recipe) scaffolds a starter from the canonical NVFP4 recipe with a REVIEW banner.
It does **not** invent quant/mods — that needs a human. See
`references/quant-recipes.md` for the mods that make NVFP4 / FP8 load on DGX
Spark (notably `mods/exp-w4a16` for NVFP4 `lm_head`).

## NAS + offline cache

The cluster runs **offline** (`HF_HUB_OFFLINE=1`). Weights live on NAS at
`/mnt/nas/hub/<repo>/` and are rsynced to each node's
`/home/spark/.cache/huggingface/hub/<repo>/`. sparkrun's own "distribution" step
does an **online** `hf download` into node-local cache — pre-populating from NAS
turns that into an instant cache-hit and avoids a ~22 GB internet pull per node.
See `references/nas-cache.md`.

## Key constraints honored

- **No `rm -rf`** — cache sync is `rsync -a` (no `--delete`) into a `mkdir -p` parent.
- **Secrets safe** — `~/.hermes/config.yaml` is line-edited (model.default only), never round-tripped or printed.
- **Topology from config.json** — fixes the stale-IP bug in the older `model-check.py`.
- **No yaml module** — YAML edits are line-based (system python3 lacks `yaml`).
- **Gated** — every cluster/config mutation needs `--yes`; restart is never automatic.

## References

- `references/quant-recipes.md` — NVFP4/FP8 recipe mods, DGX Spark env, exp-w4a16.
- `references/nas-cache.md` — offline NAS->node cache, rsync-from-gateway pattern, pitfalls.

## Related skills

- `hscc-cluster` — cluster status / host inspection; `provision_model`/`stop_model`/`restart_model` to run existing recipes once onboarded.
- `hscc` — umbrella router.
