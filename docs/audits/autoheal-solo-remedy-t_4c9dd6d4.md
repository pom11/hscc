# Auto-heal remedy spawns a SOLO instead of restoring the TP pair — t_4c9dd6d4

**Date:** 2026-09-10
**Status:** Diagnosis + fix (committed on `wt/autoheal-remedy-tp`)
**Branch:** wt/autoheal-remedy-tp

## Summary

The damaging SOLO is **not** produced by the force-recreate template-apply path.
It is produced by the daemon's **gentle relaunch** inside `check_workers`:

    hscc_daemon/health.py:1535-1541

The gentle relaunch runs `sparkrun run <recipe> --cluster hscc --hosts <node>
--port <port> --no-follow --ensure` where `<node>` is the unit's **primary node
only** and there is **no `--tp`**. For a TP=2 unit (span `[head, worker]`) this
is exactly the `sparkrun_<cid>_..._SOLO` container observed on `.247`.

The force-recreate path (`_default_autoheal_worker` → `apply_template
--force-recreate` → `_provision_models`) restores the whole span correctly
(`--hosts .247,.248 --tp 2`). `hscc cluster up` / `fleet_up_plan` (serving.py)
also render the correct full-span + `--tp` command. Only the daemon's gentle
relaunch creates a SOLO.

## Environment note (base-identity)

All hosts referenced are redacted to placeholders. `.247`/`.248` in this
document stand for the reasoning-family span head + worker from the observed
fleet (real addresses never recorded).

## Why the template (says 4node/dual) yields a SOLO on one host

The template (`hscc-cluster/templates/4node/dual-dsv4.yaml`) deliberately
declares NO explicit `nodes:` (comment lines 18-22): placement is inferred by
the resolver (`workers: remaining`). On the observed fleet the resolver
correctly produced the TP pair — the original container
`sparkrun_79d915fa1d2c3348_..._node_0` is the head of a pair cluster, not a
solo. So the SOLO is not a resolver/placement output at all.

The SOLO appears from the daemon's **per-node keepalive model**:

1. `serving.keepalive_units()` (serving.py:186-217) emits ONE entry PER NODE of
   the span, each carrying only `{node, port, recipe, id}` — the unit's `tp`
   and full `nodes` list are dropped (serving.py:203-210).
2. In `check_workers`, `_tp_peer_nodes()` (health.py:455-475) correctly keeps
   the NON-primary peer (`.248`) from being checked/relaunched as a solo
   (health.py:1440-1445).
3. But the span **primary** (`.247`) is treated like an ordinary single-node
   worker. When its `/health` probe fails, the gentle relaunch (health.py:1522-
   1541) issues a **single-host, no-`--tp`** `sparkrun run --hosts .247 --port
   8000`. sparkrun labels a single-node job with the `_SOLO` suffix.

That is the observed `sparkrun_1e5b762a694eaee6_086606fbaf71_SOLO` on `.247`
— created by the gentle relaunch, not the template apply.

## The exact offending command

Built at health.py:1536-1538 (inside the `if streak >= DEBOUNCE` else-gentle
branch, reached on the first/second down checks before the debounce reaches 3):

    sparkrun run <recipe> --cluster hscc --hosts 10.0.0.247 --port 8000 --no-follow --ensure

Contrast the correct TP command (health.py's own `fleet_up_plan`, serving.py:383-398):

    sparkrun run <recipe> --cluster hscc --hosts 10.0.0.247,10.0.0.248 --port 8000 --no-follow --ensure --tp 2

The gentle relaunch drops the second host and the `--tp` flag. That is the bug.

## Why the SOLO wedges the unit (requirement #3)

Under host networking the SOLO and the surviving `node_0` both bind `:8000` on
`.247` and both contend for the GPU. `node_0` never finishes loading; neither
new container can serve. That is the ~40-minute wedge documented in the task
body. The gentle relaunch's preceding stop (health.py:1525) runs
`sparkrun stop <recipe> --hosts .247` — it stops .247's existing container, but
because sparkrun scopes the stop to the single host in the arg, the old
`node_0`/pair state on `.248` is not coordinated, and a SOLO relaunch recreates
a single-host job that fights what remains.

## The SOLO-storm loop

The gentle relaunch has NO coordination with the force-recreate cooldown
(`_worker_last_autoheal`). Every down check that is not inside the debounce
window / relaunch grace can fire a gentle relaunch. So after a force-recreate
restores the pair but the unit is still down on the next check, the gentle
relaunch fires again — issuing another SOLO. The observed end-state (SOLO
alongside node_0) is one member of this loop.

## Fix (this branch)

The gentle relaunch must restore the unit AS DEFINED in serving.json — the
full TP span, with `--tp`, not a solo on the head.

1. `serving.keepalive_units()` — carry the full span (`nodes`) and `tp` on
   every per-node entry (backward-compatible extra keys; `node`/`port`/`recipe`
   /`id` unchanged). Per-node emission is preserved for tp=1 co-located units.
2. `check_workers` gentle relaunch (health.py:1522-1541) — build the command
   from `nodes` (full span) + `--tp`, exactly like `fleet_up_plan` (serving.py:
   383-398), and stop the whole span before relaunch. So a TP unit is relaunched
   as a whole group, not a solo.

Requirement #3 (never two workloads on the same host:port): the stop-before-
relaunch is widened to the full span, so a healed unit stops the existing
span members first and then re-provisions the pair.

## Test approach

No force-recreate against the live fleet. A fake heal seam (monkeypatched
`subprocess.Popen` / `run_cmd`, as the existing suite does) asserts the gentle
relaunch now renders the full-span `--tp` command for a tp=2 unit and a
plain single-node command for tp=1. `keepalive_units` contract test updated for
the added keys.

## What this fix does NOT cover — the cooldown-persistence gap (reported, arg inline)

The operator's temporary stopgap for the SOLO-storm loop was
`HSCC_WORKER_AUTOHEAL_COOLDOWN_MINUTES=1440`. That stopgap is BLUNT and the
card says our fix should make it removable. Does it?

The core fix above breaks the destructive cycle at its source: after a
force-recreate restores the pair, a still-down unit used to get a gentle-relaunch
SOLO on the next check; that SOLO wedged the unit and re-triggered load → grace
expiry → another force-recreate. Now the gentle relaunch re-provisions the WHOLE
span with `--tp`, so a still-loading pair is re-issued correctly rather than
poisoned with a competitor for :8000. So the primary avalanche is removed and
the 1440-minute cooldown is far less load-bearing.

BUT the cooldown mechanism itself is still not trustworthy across restarts, and
I do not fully endorse removing the stopgap until this is addressed. The
debounce/cooldown bookkeeping (`_worker_down_streak`, `_worker_last_autoheal`,
health.py:72-74) is IN-MEMORY and 'resets on daemon start'. Applying an env var
requires restarting the daemon, which wipes the very cooldown history the env
var enforces — the observed force-recreate burst 10 → 12 within ~29 minutes of
setting the var. This is the third instance of the same class of bug:

  1. debounce reset across restarts → a still-loading unit got a fresh 3-strike
     countdown every restart (the original loop)
  2. container age resolved LOCALLY → None on this fleet → grace failed open
     (t_cd6a895a fixed this by persisting container state)
  3. cooldown reset by the restart that applied it (this one)

t_cd6a895a persisted container state to ~/.hscc/worker_container_state.json; the
debounce/cooldown bookkeeping sitting right next to it in the same file was left
in-memory. Persisting `_worker_last_autoheal` (and ideally `_worker_down_streak`)
alongside that state so 'how recently did we heal this unit' survives a restart
is the clean, small follow-up that makes the cooldown trustworthy and lets the
operator remove the 1440-minute stopgap without risk. Auto-heal is a destructive
action gated entirely on state that evaporates whenever the process bounces —
and the process bounces often (58 'Started autodown thread' entries in one log).
Filed as a follow-up task; deliberately NOT scope-crept into this card, whose
core is the SOLO remedy.

## Separate question (reported, NOT fixed here)

Why did `.247` stop serving ~5 minutes after coming up healthy? Ruled out:
startup grace (correctly did NOT run — has_served=True). Ruled out: the heal
itself at 16:17 (that happened AFTER the stop). Not conclusively diagnosed from
code alone — the likeliest candidates (engine OOM, `.248` peer eviction pulling
the span, an operator action) require live fleet introspection that is out of
scope for this headless card. Filed as a follow-up.
