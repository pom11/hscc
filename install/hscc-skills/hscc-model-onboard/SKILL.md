---
name: hscc-model-onboard
description: Bring a NEW model online across the DGX Spark cluster end-to-end — author/validate a sparkrun recipe, populate the offline NAS->node HF cache, wire every HSCC config layer, launch vLLM, and verify. Use when switching the cluster to a new model or quant (e.g. FP8 -> NVFP4).
category: hscc
domain: cluster control, model onboarding, cutover
platform: macOS CLI
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
| `plan`   | `<model> <recipe>` | no | Print topology (parked nodes marked), recipe model check, NAS + per-node cache state, and the config changes `wire` would make. **Always run first.** |
| `cache`  | `<model> [recipe]` | nodes' HF cache (additive) | Verify model on NAS; rsync NAS->each serving node's local cache. Skips nodes already complete **and nodes the pause overlay holds** (no probe, no push). If `recipe` is given and missing, scaffolds a starter and stops. |
| `wire`   | `<model> <recipe> --yes` | HSCC config | Backup, then update serving.json, models.json, provision.json, agents.json, hermes root config.yaml + every profile config.yaml (model.default). |
| `launch` | `<recipe> --yes` | cluster | Bring the serving layer up through the door: `hscc cluster down` then `hscc cluster up`. Never a hand-built `sparkrun run` (see Pause-aware onboarding). Without `--yes` it runs the door's `--dry-run` and stops nothing. |
| `verify` | `<model> [--wait]` | no | `/health` + a coherent `/v1/chat/completions` on every serving node. A node the overlay holds reads `PAUSED — expected-absent`, not `DOWN`. `--wait` polls up to 5 min for slow loads. |
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
| `~/.hscc/serving.json` | every unit `model` + `recipe`, and the recorded `serve_cmd` re-rendered from the fleet plan builder | `nodes`, `role`, `port`, `tp`, `max_workers` |
| `~/.hscc/models.json` | `primary_model`, `models[0].name`, status=cached | `base_url` (gateway), provider |
| `~/.hscc/provision.json` | active `mappings[*].recipe` | `history` (audit log left intact) |
| `~/.hscc/agents.json` | only the `<model>` part of each `vllm-<ip>/<model>` | **per-agent routing IP** |
| `~/.hermes/config.yaml` | `model.default` line only | everything else incl. **secrets** |
| `~/.hermes/profiles/<name>/config.yaml` (every profile under the resolved root) | `model.default` | per-node `base_url` |

The hermes **root** (not `$HERMES_HOME`) is the mirror target: Hermes exports
`HERMES_HOME=<root>/profiles/<name>` for profile sessions, so joining it would
aim at `<profile>/profiles` and skip every profile. Resolution is lazy and goes
through the shared homes ladder (exported home → `profiles/` grandparent →
`~/.hermes`); set `HSCC_HERMES_ROOT` to name the root explicitly. The chosen
root is printed by `wire`.

Backup is written to `~/.hscc/_onboard-backup-<timestamp>/` before any write
(root `config.yaml` + every profile `config.yaml`, so the set matches what
`wire` writes).

## After wiring: services

`wire` does NOT restart anything. The gateway + daemon keep the **old** model in
memory until restarted. Containers can be up and healthy (verify passes) while
the fleet still routes to the old config. Restart only on explicit go:

```bash
# Sanctioned door: checks the label is loaded, uses the gui/<uid>/... slash form,
# and returns a structured result instead of a silent non-zero.
python3 ~/.hermes/hermes-agent/hscc-cluster/gateway_restart.py        # --json for machines
```

If the door is not installed on this machine, the raw kick is
`launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway` — the **slash** form; the
dotted `gui.$(id -u)` form silently fails every kickstart. Use
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.hermes.gateway.plist`
only when `launchctl list ai.hermes.gateway` says the label is not loaded at all
(bootstrap loads; it does not restart). Then `verify` again — the point is that
the fleet routes the new model, not that the gateway restarted cleanly.

## Pause-aware onboarding (post-2.17.3 rules)

`hscc node pause <node> --reason ...` parks a node **without editing anything the
cluster is built from** — `serving.json` still lists its units, and that is the
point: the pause has to be reversible in one command. So a tool that reads only
`config.json`/`serving.json` sees a parked machine as broken and "repairs" it —
relaunching vLLM on a node the operator took out of the fleet for a training
run. The overlay is `~/.hscc/node_pauses.json` and `hscc_daemon/node_pauses.py`
is its one reader; the three rules below are quoted from that module, because
four other seams (health/auto-heal, node_pause_exec, autodown, proxy drift) each
got this wrong before and were each fixed to ask the same question.

1. Ask **`covering` / `paused_nodes`**, never `paused()`. A pause record is keyed
   by host; a tensor-parallel unit's `span_nodes` peers are just as parked. A
   per-host check skips the machine you looked at and relaunches the other.
2. **Expiry does not release a node.** A record whose `until` has passed is still
   a pause until the daemon's expiry pass or `hscc node resume` ends it. A seam
   that read `until < now` as free would relaunch the unit seconds before the
   daemon resumed it, then resume on top of its own launch.
3. An unreadable overlay **fails open and says so**: nothing is treated as
   parked, and the run prints that the overlay was unreadable — a pause you
   cannot read is exactly the one the operator must hear about.

| Situation | What onboarding does |
|---|---|
| `cache` on a parked node | `PARKED — rsync skipped`. No SSH probe, no multi-GB push. Resume, then re-run `cache`; resume does not depend on this pass having run. |
| `launch` with a parked unit | Delegated: `hscc cluster up` skips it, prints `PAUSED <node>`, and reports `result: all-paused` (plus a `resume:` hint) when every unit is parked. |
| `verify` on a parked node | `PAUSED — expected-absent`, no probe, **not** a failure. A parked host running nothing is the pause working. |
| parked backend missing from the proxy pool | Expected-absent — dropped from BOTH drift directions before any proxy repair. A pause that pages you about a proxy you caused costs a 300 s cooldown on a live entrypoint. |
| `autodown` armed during a cutover | Check `hscc autodown status` first: an idle teardown between `wire` and `launch` leaves half the fleet on the old model. Parked nodes are excluded from its stop AND its `autoup`, so it will not wake them for you either. |
| keepalive unit dies mid-cutover | The daemon relaunches it from `serving.json` — after `wire` that is the NEW recipe. Another reason `down` then `up` is one tight sequence, not a coffee break. |

Preview before committing: `hscc cluster up --dry-run` prints exactly what the
door would start, parked nodes already partitioned out — the same code path as
the real call.

**Never stop a serving workload by hand.** `hscc node pause` (it reuses the
supervisor's own stop intent) or `hscc cluster down` — not a raw `docker stop`.
And never hand-write a unit's `serve_cmd`: the field is written by
`hscc cluster template apply` (or re-rendered by `wire`) or it is absent and
derived from `model`/`recipe`/`port`/`tp`. The resume path *prefers* that record
and replays it with only `--served-model-name` re-normalized and the `o`/
`enable-prefix-caching` pair pruned — so a hand-written `--tp` or `--max-num-seqs`
that disagrees with the unit's own fields is not corrected, it is launched, and
the serve_cmd drift canary then complains on every daemon tick about a mismatch
you created.

Why the launch is a door call and not a `sparkrun run`: sparkrun identifies a
workload by an **intent hash** of the recipe plus `--port`, `--served-model-name`
and `--tp`. An argv that drops or reorders one of those starts a workload no
later stop can match (`t_8ad06a53` — a pause that printed "stopped" and changed
nothing, and the node was still serving), and under `--ensure` the divergence is
answered with a **parallel duplicate** on the same port (`t_e1ff0e8e`). There is
exactly one builder of that argv, in `hscc_daemon/serving.py`; `onboard.py` has
no sparkrun constant on purpose.

## Recipe authoring

`onboard.py` validates that the recipe's `model:` matches and (for a missing
recipe) scaffolds a starter from the canonical NVFP4 recipe with a REVIEW banner.
It does **not** invent quant/mods — that needs a human. See
`references/quant-recipes.md` for the mods that make NVFP4 / FP8 load on DGX
Spark (notably `mods/exp-w4a16` for NVFP4 `lm_head`).

## NAS + offline cache

The cluster runs **offline** (`HF_HUB_OFFLINE=1`). Weights live on NAS at
`/mnt/nas/hub/<repo>/` and are rsynced to each node's
`/home/<ssh_user>/.cache/huggingface/hub/<repo>/`. sparkrun's own "distribution" step
does an **online** `hf download` into node-local cache — pre-populating from NAS
turns that into an instant cache-hit and avoids a ~22 GB internet pull per node.
See `references/nas-cache.md`.

## Key constraints honored

- **No `rm -rf`** — cache sync is `rsync -a` (no `--delete`) into a `mkdir -p` parent.
- **Secrets safe** — `~/.hermes/config.yaml` is line-edited (model.default only), never round-tripped or printed.
- **Topology from config.json** — fixes the stale-IP bug in the older `model-check.py`.
- **Pause is authoritative** — a parked node is never probed, synced, launched or
  reported DOWN; the overlay is read through `hscc_daemon.node_pauses`.
- **One launch builder** — `launch` shells `hscc cluster down|up`, never
  `sparkrun` directly, so `--served-model-name`/`--tp` can never drift from the
  intent the next stop will look for.
- **No `serve_cmd` left behind** — a model swap re-renders the recorded argv from
  the fleet plan builder, or clears it and says so.
- **No yaml module** — YAML edits are line-based (system python3 lacks `yaml`).
- **Gated** — every cluster/config mutation needs `--yes`; restart is never automatic.

## References

- `references/quant-recipes.md` — NVFP4/FP8 recipe mods, DGX Spark env, exp-w4a16.
- `references/nas-cache.md` — offline NAS->node cache, rsync-from-gateway pattern, pitfalls.

## Related skills

- `hscc-cluster` — cluster status / host inspection; `provision_model`/`stop_model`/`restart_model` to run existing recipes once onboarded.
- `hscc` — umbrella router.
