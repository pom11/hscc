# HSCC Bootstrap Installer — Design

**Date:** 2026-06-09
**Status:** Approved design → ready for implementation planning
**Scope:** Rewrite `hscc-bootstrap` into a preflight-gated, topology-detecting, minimal-interview installer that makes a machine HSCC-ready.

## Context

The current `hscc-bootstrap/bootstrap.sh` is a stale health-check: it validates state files from the archived agent pipeline (`lifecycle.json`, `projects.json`, `cooldowns.json` — all gone), hardcodes our private topology (192.0.2.x, QNAP NAS at .249), and doesn't know about the post-refactor world (role profiles, `serving.json` keep-alive units, the 27B model, the `sdlc-review` skill, autonomy).

This repo is a **private installation package** other users (or a fresh machine) install. Bootstrap must therefore work on **any sparkrun setup** — different IPs, different node counts, with or without a NAS — not just our cluster. It should detect what it can and ask only what it can't.

**Goal:** one command — `hscc-bootstrap` — that checks prerequisites, then installs everything HSCC needs onto the detected cluster, asking the minimum.

## Principle

**Detect facts, ask intent, default the rest.** Node IPs, cluster name, NAS path, and SSH user are *facts* sparkrun already knows. Which node is the orchestrator and which recipe to serve are *intent* only the user has. Everything else has a safe default the user can change later in `serving.json`.

## Prerequisites (hard gate)

Before installing anything, bootstrap verifies — and **hard-stops with clear guidance** if either fails:

1. **A sparkrun cluster is configured.** Check `sparkrun cluster list --json` returns at least one cluster. If not → stop: "No sparkrun cluster configured. Run `sparkrun cluster add ...` first."
2. **Hermes is present.** Check the Hermes CLI / `~/.hermes/hermes-agent` exists (and report whether the gateway is running). If Hermes isn't installed → stop: "Hermes not found at ~/.hermes/hermes-agent. Install Hermes first."

Rationale: HSCC is useless without a sparkrun cluster + Hermes. Installing into a broken base produces a half-working setup that's harder to debug than a clean stop.

## Detection (no questions)

From `sparkrun cluster list --json` (verified shape):
```json
[{"name": "hscc", "hosts": ["…"], "user": "spark", "cache_dir": "/mnt/nas", "default": true}]
```
- **cluster name** ← `name` (of the default cluster, or the only one)
- **node IPs** ← `hosts`
- **node count** ← `len(hosts)`
- **SSH user** ← `user`
- **NAS / cache dir** ← `cache_dir` (empty/absent = no NAS)
- **Hermes gateway running?** ← process / health probe (informational)

## Interview (minimal — only what has no safe default)

Ask at most **3** questions; each has a default so a `--yes` / non-interactive run takes all defaults:

1. **Orchestrator node** — which host runs the always-on model + Hermes gateway.
   Default: the host already serving a model (probe `:8000/v1/models`), else `hosts[0]`. Single-node cluster → no question.
2. **Recipe to serve** — which sparkrun recipe the orchestrator (and workers) run.
   Default: the sole recipe in `~/.sparkrun-local/recipes/` (incl. `local-fixed/`); if multiple, list and ask; if none, warn + leave serving.json model blank.
3. **NAS confirmation** — only asked if `cache_dir` is ambiguous. If `sparkrun` reports a `cache_dir`, use it silently; if absent, default to "no NAS" (local cache). Effectively zero questions when sparkrun already knows.

**Defaulted, never asked:** workers = all non-orchestrator hosts; keep-alive = on; vLLM port = 8000; autonomy = off; Operations notifications = off (configure later); model name = read from the chosen recipe; SSH user = from sparkrun.

Single-node cluster with one recipe and a sparkrun-known cache_dir → **zero interactive questions**, fully automatic.

## Install steps (after prereqs pass + interview)

All idempotent (safe to re-run). NO vLLM model provisioning — bringing models up is a separate explicit action (`provision_model` / `sparkrun run`), not part of bootstrap.

1. **Skills + templates** — `hscc-skills install` (bundled skills incl. `sdlc-review`, templates → `~/.hermes/skills`).
2. **Role profiles** — `hscc-roles generate` (build all 22 role profiles → `~/.hermes/profiles`).
3. **`~/.hscc` state + serving.json** — create `~/.hscc/`; write `serving.json` from the detected cluster + interview answers: one `orchestrator` unit (the chosen node), one `keepalive` `worker` unit per other node, each with the chosen recipe + model + port; seed `autonomy` = off. Don't clobber an existing serving.json without `--force` (back it up).
4. **Daemon** — install + start the launchd job `com.hermes.hscc_daemon` (monitoring + keep-alive). Verify it comes up.

## Output

A staged, readable report (keep the current bootstrap's nice staged UI): each stage PASS/WARN/FAIL, a final summary, exit 0 on success. `--json` for machine-readable output. The report ends with the live cluster picture + next step ("models are not running yet — provision with: …").

## Flags

- `--yes` / `-y` — non-interactive; take all interview defaults.
- `--force` — overwrite an existing serving.json (back it up first).
- `--skip-skills`, `--skip-roles`, `--skip-daemon` — skip individual install steps.
- `--json` — machine-readable output.
- `--verbose` / `-v`, `--help` / `-h`.

## Components / file structure

- `hscc-bootstrap/bootstrap.sh` — orchestrates: prereq gate → detect → interview → install steps → report. Keep it bash (matches current), but push the non-trivial logic (sparkrun parsing, serving.json generation) into small Python helpers invoked from the script so it's testable.
- `hscc-bootstrap/detect.py` — parse `sparkrun cluster list --json` → a normalized dict (name, hosts, user, cache_dir). Pure, testable.
- `hscc-bootstrap/serving_gen.py` — given detected cluster + answers (orchestrator, recipe, model, port, keepalive), produce the `serving.json` structure. Pure, testable.
- `hscc-bootstrap/tests/` — unit tests for `detect.py` (parse various sparkrun outputs incl. no-NAS, single-node, multiple clusters) and `serving_gen.py` (orchestrator + worker unit shaping, keep-alive flag).

The bash script stays the thin orchestrator + UI; the two Python helpers hold the logic worth testing. This keeps each unit small and lets the risky parts (detection, serving.json shape) be verified without a real cluster.

## Testing strategy

- `detect.py`: feed canned `sparkrun cluster list --json` strings (with/without cache_dir, 1 host, N hosts, multiple clusters w/ a default) → assert normalized dict.
- `serving_gen.py`: given a detected cluster + chosen orchestrator + recipe → assert serving.json has one orchestrator unit (right node) + keepalive worker units for the rest, correct recipe/model/port.
- Bootstrap script: a `--dry-run`/`--json` invocation on the live machine that exercises detect + interview-defaults + serving.json generation WITHOUT installing, asserting the planned actions. Full install verified manually on this machine (idempotent re-run).

## Out of scope

- vLLM model provisioning / cluster bring-up (separate explicit action).
- Installing sparkrun or Hermes themselves (prerequisites — bootstrap checks, doesn't install).
- Multi-cluster management (operate on the default cluster).
- Operations-topic Telegram setup beyond seeding it off (configure later).
