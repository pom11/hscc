# Gentle relaunch creates a duplicate workload serving.json never declares — t_e1ff0e8e

**Date:** 2026-09-11
**Status:** Root-caused + fixed (commit `ce7a028` on `wt/duplicate-workload`)
**Branch:** wt/duplicate-workload

## Summary

The daemon's gentle relaunch (health.check_workers) built its `sparkrun run`
command **independently** from `fleet_up_plan` and **dropped
`--served-model-name`**. sparkrun folds `served_model_name` into the workload's
**intent hash**, so the relaunch derived a DIFFERENT cluster_id for the same
hosts. `--ensure` then found nothing running under that derived id and launched
a **PARALLEL DUPLICATE** of the worker instead of re-issuing the declared
workload. That duplicate could not bind the port the declared worker already
held, so its vLLM command failed and sparkrun fell back to `sleep infinity` —
the idle, untracked third workload.

## Root cause chain (all evidence-backed)

sparkrun's cluster_id is `sparkrun_<intent_id>_<placement_token>`
(job_metadata.generate_cluster_id). The two segments:

- `intent_id = sha256(recipe.runtime + recipe.model + port + served_model_name
  + every non-default parallelism dim)` — hosts are NOT hashed
  (orchestration/job_metadata.py:221-253 `generate_intent_id`).
- `placement_token = sha256(sorted(hosts))` — identical for the same host set
  (job_metadata.py:267-278 `derive_placement_token_from_hosts`); the greedy
  first-fit scheduler sets `deterministic_placement=True` (core/scheduler.py:
  301-311), so api.run derives this same deterministic token.

Because both workloads land on the same two hosts (.247/.248), the placement
tokens are equal; the ids differ ONLY because the **intents differ**.

### Why the intents differ

- The DECLARED worker was created by `fleet_up_plan` (serving.py), which passes
  `--served-model-name <concrete> worker-model` (serving.py `_served_model_name`
  → `fleet_up_plan` command). This fed `served_model_name` into the intent hash
  → intent `79d915fa1d2c3348`.
- The pre-fix gentle relaunch (health.py) built its command from
  `keepalive_units()`' `{node, port, recipe, id, nodes, tp}` and **never added
  `--served-model-name`** (serving.py:186-231 did not carry `model`; health.py
  did not call `_served_model_name`). The intent therefore hashed WITHOUT the
  `name=` component → a different intent `1e5b762a694eaee6`.

### Empirical proof (sparkrun's REAL functions, placeholder addresses)

`/tmp/prove_intent.py` drives sparkrun's own `generate_intent_id` +
`derive_placement_token_from_hosts` + `generate_cluster_id`:

    declared intent : 783b1cc511b6ccdc     (with --served-model-name)
    relaunch intent : c96e641ddf2261be     (without — the pre-fix relaunch)
    same placement  : 5b0360aa00f5
    declared cid    : sparkrun_783b1cc511b6ccdc_5b0360aa00f5
    relaunch cid    : sparkrun_c96e641ddf2261be_5b0360aa00f5
    intents differ  : True
    cluster_ids differ => --ensure sees no declared cid running, launches a NEW workload: True

The `--ensure` path (cli/_run.py:328-343) computes exactly
`derive_cluster_id(recipe, host_list, overrides)` and exits 0 only if THAT id is
already running. Since the relaunch derived a different id, `--ensure` saw
nothing running and launched a duplicate. Same mechanism produces the observed
`79d9...` (declared) vs `1e5b...` (duplicate) on this fleet.

## Answers to the task's questions

1. **Why `1e5b762a694eaee6` instead of `79d915fa1d2c3348`?** The relaunch dropped
   `--served-model-name`, which sparkrun hashes into the intent id. Different
   intent + same hosts ⇒ different cluster_id. Deterministic because the hash
   inputs (recipe, port, tp, and the ABSENCE of served_model_name) are stable
   across recreations.

2. **Does the relaunch consult serving.json?** Yes — via `keepalive_units()`
   (serving.py:186-231). But it re-derives the unit from a SUBSET of the unit's
   fields (`node/port/recipe/id/nodes/tp`) and reconstructed an incomplete
   command, so it landed on a different intent. It did not consult the
   `model`/`serve_cmd` fields `fleet_up_plan` uses. The fix makes it feed the
   SAME shared command builder as `fleet_up_plan`, so both derive the identical
   cluster_id.

3. **Should anything reap an existing untracked duplicate?** **No — deliberately
   not adding a reaper.** The fix prevents NEW duplicates at the source (the
   relaunch now re-issues the declared id, so `--ensure` exits 0 when the
   declared workload is up). Reaping is a separate, riskier concern: a reaper
   that stops "untracked" workloads could stop a REAL unit if serving.json is
   stale/mid-write or a legitimate workload is declared under a different id.
   The duplicate is idle (sleep infinity, 500 MiB RAM, no GPU, no port binding),
   so its cost is near zero — it is untracked state, not an outage cause, and
   the card explicitly says not to build heavyweight machinery around it. The
   `sleep infinity` fallback is sparkrun's signal that a launch FAILED; the
   correct lean action is to prevent the duplicate launch (done) rather than
   police leftovers. If the operator wants the existing duplicate cleaned up,
   `sparkrun stop` against the known duplicate cluster id (or a one-time prune)
   is the targeted lever — not an automatic daemon reaper that risks killing a
   real workload.

## The fix (commit ce7a028)

- **serving.py `_unit_run_cmd(unit)`** — NEW single source of truth for the
  `sparkrun run` launch command, carrying every intent-hash input (recipe,
  port, `--served-model-name`, `--tp`). Used by BOTH `fleet_up_plan` and the
  gentle relaunch, so the two can never drift again.
- **serving.py `keepalive_units`** — now also carries `model` and `role` per
  per-node entry so the relaunch can rebuild the served-model-name the unit was
  declared with.
- **health.py gentle relaunch** — uses `serving._unit_run_cmd(u)` instead of an
  inline command, so a TP worker is relaunched with the full span, `--tp`, AND
  `--served-model-name` — byte-identical to `fleet_up_plan`. `--ensure` now
  finds the declared workload and re-issues it instead of duplicating it.

## Tests

- `test_relaunch_reuses_declared_cluster_id` (test_health.py) — a TP=2 worker
  relaunch command equals `fleet_up_plan`'s command for the same unit
  (`--hosts <span>`, `--tp 2`, `--served-model-name W worker-model`).
- `keepalive_units` contract test updated for the new `model`/`role` keys.

## What this task did NOT do (and why)

- **No reaper** for existing untracked duplicates (argued above).
- **No live-fleet mutation** — `hscc doctor`/bootstrap not run; no Telegram;
  no POSTs. All verification is code-level / unit-test-level; the intent-hash
  proof uses placeholder addresses (never real hosts) via a temp script under
  /tmp, not committed.
- **No sparkrun change** — sparkrun's id derivation is correct; the bug was
  hscc handing it a different command. No upstream addition needed.

## Full-suite note

`scripts/run_tests.sh` reports `✗ hscc-roles`: one test,
`hscc-roles/tests/test_generator.py::test_every_hscc_owned_worker_profile_has_role_spec`,
fails with `FileNotFoundError: /Users/desac/.hermes/profiles/devops-engineer/
profiles`. That is an environment-dependent test scanning a Hermes profiles
directory that does not exist in this (devops-engineer) profile environment. It
is unrelated to this change: my commit touches only `hscc_daemon/` (that dir
`✓` green in the same run), and `hscc-roles` does not import `hscc_daemon`.
Pre-existing/env-gated, not a regression from this card.

