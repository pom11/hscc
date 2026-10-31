# hscc-cluster

The cluster-control plugin: the **orchestrator-only** toolset Hermes uses to see
and shape the DGX Spark cluster, plus the topology-free template engine and the
agentic work-flow helpers.

> Loaded by Hermes via `register(ctx)` in `__init__.py`. Worker roles never get
> this toolset — only the orchestrator can change cluster shape.

## When you touch it

New cluster/health tools, the template intent format, or the apply pipeline.
The template **format reference** is
[`templates/README.md`](templates/README.md), not this file.

## Tools (registered into the `hscc-cluster` toolset)

| Tool | What |
|------|------|
| `discovery_status` | Single source of truth for live topology (orchestrator/workers/NAS, `source: live\|cache`); `probe=true` adds per-node VRAM / power-draw idle / vLLM health. |
| `nas_status` | NAS node + a single mount probe (no fan-out — honors the NAS staging limit). |
| `cluster_status` | Running models, idle nodes, serving units (+ free-VRAM when probed). |
| `list_recipes` / `pick_node` | Browse sparkrun recipes / choose an idle worker. |
| `provision_model` / `stop_model` / `model_health` | Model lifecycle (guarded: `confirm=true`). Correct sparkrun invocation (`--cluster/--port/--ensure`). |
| `vllm_logs` / `node_diagnostics` / `nas_diagnose` | Debug a node / NAS. |
| `restart_model` / `remount_nas` / `repair_nas_export` / `reap_orphans` | Self-heal (guarded). |
| `orch_message` | Orchestrator-to-orchestrator **nudge** (queue-only, signed, audited) — see [Orchestrator-to-orchestrator messaging](#orchestrator-to-orchestrator-messaging). |

## Modules

- `discovery.py` — the keystone. `discover()` resolves topology **live → cache →
  fail-loud** (no silent fake-IP fallback). Idle is classified by **power draw**
  (≈<15 W on GB10), not util% (which reads ~96% idle). New sparkrun nodes are
  **auto-adopted**.
- `template_intent.py` + `templates/` — templates describe **intent only** (no
  IPs, no ports); at apply, `resolve()` maps them onto the live cluster
  (orchestrator→gateway, `workers: all|N|remaining` → real nodes) and
  auto-assigns ports. `recipe_cost.py` parses `sparkrun show`'s VRAM Estimation
  so placement only proposes layouts that **actually fit** (incl. 2 models
  co-located on one node).
- `cluster_template.py` — the apply pipeline: load yaml → `resolve(discover())`
  → `validate_resolved_plan` → write `serving.json`/`models.json`/`config.yaml`
  + proxies + provision. Transactional: snapshots to `~/.hscc/rollback/<ts>/`
  and **auto-rolls-back** on a failed apply; records the active template in
  `~/.hscc/applied_template.json`. Worker-facing model ids are
  **probe-before-write** — see
  [`docs/DESIGN-probe-before-write.md`](../docs/DESIGN-probe-before-write.md).
- `workflow.py` — the idempotent-resume probe (`probe_task_state`) + the
  `kanban_task_claimed` hook that posts a "resume, don't redo" note to a
  re-dispatched worker, built from its task branch's committed state.

## CLI

Merged into the main `hscc` CLI (this plugin also exposes the same engine as a
library the merged CLI imports):
- `hscc template <list|status|validate|preview|apply> [name] [--confirm]`
- `hscc cluster <status|hosts|monitor|jobs|info|stop|down|up>`

## Orchestrator-to-orchestrator messaging

Two surfaces, one engine: a CLI verb for a human at the terminal and a tool for
an orchestrator profile, both reaching the same pane-delivery module. Neither
one re-implements pane logic — `hscc-api/repl_inject.py` owns discovery, the
exactly-one-pane rule, the busy read and the mode→verb table, and this plugin
never runs `tmux` itself.

### The CLI verb (`cmd_orch_inject` in `hscc.py`)

```
hscc orch inject <profile> "<message>" \
    [--mode auto|plain|queue|steer] [--pane PANE] [--dry-run] [--json]

# engine entry point (works today; the unified `orch` group branch is pending —
# see the status note below, so use this spelling until it lands):
python3 hscc-cluster/hscc.py orch inject <profile> "<message>" --dry-run
```

It is a thin wrapper over `repl_inject.inject`: `--mode` goes straight through
(vocabulary `auto | plain | queue | steer`, plus the accepted legacy alias
`interrupt` == `plain`; `auto` = `/queue` while the target is busy, a plain
send when idle), and the CLI derives no prefix and reads no screen of its own —
so a dry run cannot disagree with a real send.

| Flag | Effect |
|------|--------|
| `--mode` | `auto` (default) / `plain` / `queue` / `steer`; passed straight to `repl_inject`. |
| `--pane` | Pin the target to a specific tmux pane id/target instead of resolving by profile. |
| `--dry-run` | Resolve + report the plan (target, busy, verb) and send **nothing**; adds the one-line `line` from `format_plan`. |
| `--json` | Machine path — the raw receipt dump (rendering stays in the entry point). |

Receipt keys: `pane`, `target`, `profile`, `busy`, `mode`, `verb`,
`interrupts`, `chars`, plus `text` on a dry run and `sent_text` on a real send.
A refusal **raises** — `InjectError` (`no_panes` / `ambiguous` / `tmux` /
`empty`) or `ValueError` for bad argv — and the entry point renders it (raw JSON
with `--json`, themed panel otherwise, exit 1). Registered as `orch` (group),
`orch-inject` and `inject` in `COMMANDS`; needs `hscc-api` deployed next to
`hscc-cluster`, since `repl_inject` lives there.

> **Unified-CLI status (as of this writing):** the verb dispatches from this
> plugin's entry point today. The `orch` group branch in the merged `hscc` CLI
> (`hscc_daemon/hscc.py`, the sibling of `cluster`/`template`) is owned by
> follow-up card `t_5da7fd56`; while that wiring is absent the unified spelling
> in the block above is rejected by the top-level dispatcher as an unknown
> command, so use the engine entry point shown with it. If `hscc help` now lists
> `orch`, that card has landed and this note (plus the fallback line above) is
> stale — drop both. Facts in this section are from the merged engine source,
> not from that pending wiring.

### The tool (`orch_message` in `orch_message.py`)

Registered in `__init__.py` into the `hscc-cluster` toolset as
`orch_message(to, message, ref_card=None)` — an orchestrator-to-orchestrator
**nudge**, not a general messaging API. Every rule is enforced in code:

1. **Both ends are orchestrators.** The sender is the *running* profile's
   identity (`HERMES_PROFILE`, falling back to the `profiles/<name>` leaf of
   `HERMES_HOME`, else `default`) — never an argument, so a sender cannot sign
   as someone else. Sender and target must match `*-orch`; worker profiles and
   `default` are refused, as is `to == sender`.
2. **Queue-only.** Delivery always asks `repl_inject` for `mode="auto"`, so a
   nudge queues behind a running turn; `steer` is never chosen by this tool.
3. **Auto-signed.** `[from <sender-orch>] ` always, plus `re: t_xxxx ` when
   `ref_card` names a kanban task (`t_` + hex).
4. **Rate + loop guards.** At most `max_per_hour` messages per `(sender, to)`
   pair — **default 6** — and a turn whose inbound text already starts with
   `[from ` may not be answered with another `orch_message` (the reply-loop
   guard, fed by the `user_task` context kwarg; without it the guard is a
   documented no-op).
5. **Audit.** Every call — sent *and* refused — appends one JSONL line to
   `~/.hscc/state/orch_messages.jsonl`: `ts`, `from`, `to`, `ref_card`, `chars`,
   `delivered`, `verb`, `reason`, `pane`, `body_preview` (first 200 chars; the
   full body is never stored) and `session_id` when the caller supplied one.
   `HSCC_DIR` relocates the base dir.

That log is the rate limiter's **only** state, so the tool is fail-closed: when
the audit log cannot be appended, the nudge is refused *before* a pane is
touched (an uncountable nudge is exactly what the guard exists to stop).

**Rate-cap config knob** (precedence: file → env → default 6; a non-integer or
a value below 1 falls through rather than disabling the channel or making it
unlimited):

```json
{ "orch_message": { "max_per_hour": 6 } }
```

…in `<hscc-dir>/cluster.json` (default `~/.hscc/cluster.json`), or
`HSCC_ORCH_MSG_MAX_PER_HOUR=6` in the environment.

Refusals are machine-readable `reason` values: `bad_args`, `empty_message`,
`bad_ref_card`, `sender_not_orch`, `target_not_orch`, `self_message`,
`loop_guard`, `rate_limited`, `no_pane`, `ambiguous_pane`, `transport_error`,
`repl_inject_missing`, `audit_unwritable` (`sent` on success). A refusal never
touches a pane.

The matching policy line is generated, not prose in this file: rule 11 of the
`## How you manage work` block in `hscc-roles/generator.py` puts it into every
`<project>-orch` SOUL — `orch_message` is the nudge ("look at t_xxx on <board>,
it's done/needs you"), never the work order; work itself always goes on a card
on the right board.

## Tests

`tests/` — run: `python -m pytest tests/ -q`. Integration tests assert
generated files / real git, not mocks.
