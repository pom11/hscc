---
name: hscc
description: "Hermes Spark Cluster Control — thin cluster-physical layer for the DGX Spark cluster. Current CLI surface: node pause, autodown, merge queue, main orchestrator, templates, orch messaging. Agent dispatch is native Hermes kanban, NOT HSCC."
version: 4.0.0
author: Hermes Agent
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [HSCC, Cluster, Provisioning, Monitoring, CLI]
    related_skills: [hscc-cluster, hscc-model-onboard]
---

# Hermes Spark Cluster Control (HSCC)

HSCC is a thin cluster-PHYSICAL layer: it owns what runs on the GPU nodes.
Agent work (cards, dispatch, sessions) is **native Hermes kanban**, and the
agent-side machinery (sessions, boards, profiles, cron, delegation) belongs to
**hermes** — use its CLI or tools, never hand-edit `~/.hermes/**` state or
open `kanban.db`/`state.db` directly. The 2026-06-08 refactor archived the
duplicated agent pipeline (coordinator, projects, orchestrator, events,
governance, MCP server); anything naming `mcp_hscc_*`, `dispatch-task`,
`release-task` or `assign-task` is stale.

**The cluster ships two apps.** `ios-app/` is a slim widgets + Live Activities
app and `macos-app/` a menu bar app sharing the iOS widget sources; both release
through `scripts/release_apps.sh` on their own `apps-v*` tag namespace, separate
from the CLI's `VERSION`/`v*`/`Formula/hscc.rb` lane. Read
[references/apps-fleet.md](references/apps-fleet.md) before touching either app,
any app release, or anything under `gh-pages`.

## The iron rule: sparkrun owns the fleet

Every fleet fact (node, unit, container, workload) must come through
**sparkrun** — via the `hscc` CLI, the `hscc-cluster` tools, or the sanctioned
structured status query (`hscc_daemon/health.py` runs
`_SPARKRUN_STATUS_SCRIPT` under sparkrun's own venv python: `resolve_hosts` +
`query_cluster_status` + `to_dict()`). Never hand-roll `ssh` to a node and
never shell out to `docker` for fleet facts — a **local `docker ps` on the
Mac answers *empty*, not *error*** (the v1.15.0 failure class: the auto-heal
grace resolved container age with a local `docker ps`, got `None` every time,
failed open). Containers live on the DGX nodes; the daemon runs on the Mac.

The same no-second-implementation rule holds one level up: the agent tools for
a verb **call the unified CLI's own verb functions** (`hscc-cluster/node_pause_tools.py`
is the pattern — "there is no second implementation to fall out of step"), and
slash/API surfaces delegate the same way (`/cluster-down` delegates to
`cmd_cluster_down`, audit F5 fixed in #649). Probing services on THIS host
(the daemon's own docker, a local postgres) is local by definition and does not
go through sparkrun; fleet facts never qualify.

Cluster shape (documented placeholders, TEST-NET): gateway/orchestrator node
`192.0.2.10` (always-on vLLM serving all Hermes agents), worker GPU nodes
`.246 / .247 / .248` (provisioned on demand), NAS `.249`. State lives in
`~/.hscc/` (root override: `HSCC_DIR`; never paste a real tailnet/LAN address
into a doc or commit — the repo is public).

## The CLI surface

The authoritative verb list is `TOP_LEVEL_COMMANDS` in `hscc_daemon/hscc.py`;
`hscc nosuchverb` prints it. `hscc help [topic]` and `hscc <group> --help` are
the help surfaces (a known gap: the `hscc help kanban` topic omits the
`pause|resume|paused|enforce` family that `hscc kanban --help` lists — audit
F3; trust the group `--help`).

| group / verb | surface |
|---|---|
| `hscc cluster status\|hosts\|monitor\|jobs\|info\|stop <cid>\|up\|down\|history` | fleet reads + fleet power (`up`/`down` are operator-confirm-gated; `--dry-run` supported; `history --window/--tier` is the only place the metrics tiers are explained) |
| `hscc template list\|status\|preview <n>\|validate <n>\|apply <n> --confirm` | declared fleet layouts; see "Templates & the applied record" |
| `hscc autodown status\|enable [--idle-minutes n] [--force]\|disable\|wake\|cancel` | idle serving-layer down/up; **operator policy: stay OFF** |
| `hscc kanban blocked [--recover <id>]\|stale [--archive <id>]\|monitor\|pause <board>…\|resume\|paused\|enforce` | board hygiene + board brake; the pause family is **CLI-only by operator ruling (audit D6)** |
| `hscc cards monitor [--live] [--interval <s>] [--board <slug>] [--tools]` | interactive per-card transcript panel (keys tab/enter/t/p/q); TTY-only |
| `hscc merge-queue status\|list\|add\|remove\|pause\|resume\|enable\|disable` | local merge queue; see "Merge queue" |
| `hscc orch inject <profile> "<msg>" [--mode auto\|plain\|queue\|steer] [--dry-run] [--json] [--pane <t>]` \| `orch waker status\|enable\|disable` | live tmux-pane steering — **CLI-only**; the agent path is the `orch_message` tool — every `orch`-named capability is tabulated under "Naming traps" |
| `hscc main start\|status [--json]\|wakeups list\|set <name> <sched>\|enable\|disable <name>` | the main orchestrator's pane + daemon-fired wake-ups |
| `hscc node pause <host> [--reason <t>] [--until <dur>] [--force] [--yes] [--dry-run]` \| `node resume <host> [--yes] [--dry-run]` \| `node paused [--json]` | the node-pause overlay; rules below |
| `hscc verify [--json] [--chat]` | full compatibility/health smoke-test; `--chat` fires a real prompt (~600 s) |
| `hscc stats [days]` · `throughput` · `autoscale` · `escalate` | reads: kanban activity, vLLM tokens/queue depth, advisory scale decision (never scales), pending escalations (dry-run) |
| `hscc heal-status` · `heal-clear <node>` | heal-BREAKER strikes (read / clear). Different subsystem from the `/heal` slash command (audit F7 naming trap) — the whole `heal*` family is tabulated under "Naming traps" |
| `hscc notify <title> <body> [--level …] [--key …]` · `notify list` · `notify ack <id>` | operator alert feed (`~/.hscc/alerts.json`) |
| daemon/service: `start stop status check watch triggers log plist install uninstall bootstrap update start-daemon` | service-manager verbs; `status` = daemon + every health stream; `check --repo <path>` = address-guard posture of a checkout |
| `hscc api start\|stop\|status\|install-service\|uninstall-service` · `hscc profiles` · `hscc project <verb>` | API self-hosting (cannot stop itself over its own transport), per-profile running-card counts, flightdeck project commands (~25, delegated surface) |

Not agent-reachable **by ruling, not by omission** (audit verdicts D1-D11,
operator-signed): `kanban pause|resume|paused|enforce` (a worker that can pause
its board can pause the fleet's work — D6); `merge-queue` mutations (the queue
force-pushes and squash-merges; an agent-writable merge door is a self-merge
path — D8); `autodown enable|disable|wake|cancel` (fleet-power policy is
operator scope); `main start` (spawns the operator's orchestrator pane);
`cluster up|down` (no fleet-power tool — D3); `notify` (no tool — D7: an agent
that can page the operator trains the operator to ignore pages); `orch inject`
(live pane steering stays CLI — the agent path is `orch_message`).

## Naming traps: `heal*` and `orch*` each name several capabilities (audit F7)

Both prefixes are shared. Pick by what the operation **does** to the fleet, never
by the prefix — and if you are an agent, by which surface you are allowed to call
at all (CLI = operator terminal, chat surface = operator, tool = you, route = the
HTTP API). Every row's implementation is named in its first cell; the claims were
read off that code, not off a help text.

### The `heal` family — two verbs and one read tool about a ledger, three about running units

| capability (and where it lives) | what it actually does | what it does NOT do | often confused with |
|---|---|---|---|
| `hscc heal-status` (CLI, read-only; `hscc_daemon/cli.py::cmd_heal(status_only=True)`) and the `heal_status` agent tool (read-only; same state through `heal_breaker.snapshot()`) | prints/returns the daemon's heal-**breaker** ledger: repair actions recorded per node inside the window, their age, and whether that node is heal-suspended right now and for how long (`~/.hscc/heal-breaker.json`) | restarts, heals and clears nothing; a node parked by `hscc node pause` shows **no** strikes — the pause overlay suppresses accrual, so "empty" is not "healthy" | `/heal`; `hscc heal-clear` |
| `hscc heal-clear <node> [reason…]` (CLI, mutates; same function, `heal_breaker.clear` + `reset_alert`) | drops that one node's strikes and re-arms its alert latch, so the daemon's repair paths may act on it again immediately; the reason is kept under `_cleared` | touches no unit, no other node and no node pause. **Usually unnecessary**: a suspension self-expires as its strikes age out of the window — clearing is how you stop waiting after investigating, and the breaker's own alert names this verb | `hscc heal-status`; `node resume` (which clears the node's strikes as a side effect) |
| `/heal` → `/heal confirm` (operator chat surface; `hscc-commands/__init__.py::cmd_heal`) | probes the model endpoint of every worker unit in `serving.json`, lists the unhealthy ones, and on `confirm` stop+runs each of them (`cmdlib.restart_one`); if the orchestrator endpoint is also down it only *advises* `/cluster-restart confirm` | reads and clears no strike (the two are pinned apart by `hscc_daemon/tests/test_heal_cli_help.py`); never restarts the orchestrator unit (that is `/orch-restart`) or re-applies the template (`/cluster-restart`); no agent tool calls it | `hscc heal-status` / `heal-clear`; the `restart_model` tool (the same sparkrun stop+run, per-model, agent-callable) |
| the four **heal-category agent tools** — `restart_model`, `remount_nas`, `repair_nas_export`, `reap_orphans` (agent toolset; `hscc-cluster/__init__.py::_HEAL_TOOLS`) | four individual repair actions, each `confirm=true`-guarded and target-scoped (one model, one mount, one export set, one orphan sweep) | no health sweep and no "what is unhealthy" judgement (that is `/heal`, or `model_health` per node); no strike-ledger read (that is `heal_status`) and no clear | `/heal`; `hscc heal-*`; `heal_status` |
| the daemon's **auto-heal loop** (no verb; `hscc_daemon/health.py`, guarded by `heal_lease.py` + `heal_breaker.py`) | the unattended repair paths — worker relaunch, keepalive, proxy relaunch, engine-wedge recovery — serialised by the heal **lease** (`~/.hscc/heal-lease.json`, bounded TTL, one repair in flight) and bounded per node by the heal **breaker**: more than 6 actions on one node inside 900 s stops healing that node, logs one critical alert naming `hscc heal-clear <node>`, and waits | is not callable and has no verb of its own; skips paused nodes; does not reset itself on daemon restart (the ledger is durable on purpose) | `/heal` — an operator-initiated pass, not the loop |

### The `orch` family — five verbs on one prefix, plus the pane owner

| capability (and where it lives) | what it actually does | what it does NOT do | often confused with |
|---|---|---|---|
| `hscc orch inject <profile> "<msg>" [--mode auto\|plain\|queue\|steer] [--dry-run] [--pane <t>]` (CLI, operator-only; `hscc-cluster/hscc.py::cmd_orch_inject` over `hscc-api/repl_inject.py`) | tmux bracketed-paste into a **running** Hermes REPL pane for `<profile>`; exactly-one-pane (a missing *or* ambiguous pane is a refusal, never a guess); `auto` = `/queue` while the target is busy, plain when idle; `--dry-run` resolves the pane and sends nothing | cannot reach a profile with no pane; does not sign, rate-limit or audit the message; cannot restart or serve anything | `orch_message`; `hscc main start` (it owns the pane this types into) |
| `hscc orch waker status\|enable\|disable [--json]` (CLI, operator-only; `cmd_orch_waker` over `hscc_daemon/orch_waker.py`) | the daemon-resident orchestrator **waker** (one tick every 30 s): `status` prints enabled, last tick + its age, per-board last-id and pending count and the last 5 audit lines; `enable`/`disable` flip `orch_waker.enabled` through the daemon's own atomic writer | sends no message to anyone and wakes nothing on demand; it is not `hscc main wakeups` (a separate schedule of daemon-fired wake-ups) | `orch_message`; `hscc main wakeups` |
| `orch_message` (agent tool; `hscc-cluster/orch_message.py`) | the policed nudge channel between orchestrators: both ends must be `*-orch` or the main orchestrator resolved by `hscc-roles/main_orch.py`; queue-only (`mode=auto`, never `steer`); signed `[from <sender>]` plus `re: t_xxxx` with a ref card; ≤6/h per (sender, to) pair; every call — sent *and* refused — appends to `~/.hscc/state/orch_messages.jsonl`, and if that line cannot be written the message is **not** sent | never interrupts a running turn; refuses worker profiles and `default`; cannot target a pane of your choosing; cannot deliver to a profile with no pane | `hscc orch inject`; `/orch-restart`; `POST /v1/orchestrator/chat` |
| `/orch-restart` → `confirm` (operator chat surface; `hscc-commands/__init__.py::cmd_orch_restart`) | stops and relaunches the **one orchestrator vLLM unit** named in `serving.json` on its node (~5–7 min of serving downtime while the model loads) | has nothing to do with Hermes panes, profiles or messages despite the prefix; is not `/cluster-restart`, which re-applies the whole template | `hscc orch inject`; `/cluster-restart` |
| `POST /v1/orchestrator/chat` + `GET /v1/orchestrator/chat/{id}` + `POST …/{id}/stop` (HTTP; `hscc-api/routes_orchestrator.py`) | one async orchestrator **chat turn**: resolves project → orchestrator through `hscc-roles/orchestrators.py`, spawns `hermes -p <profile> chat -Q --continue <session>` on a background thread, answers 202 with a `job_id` at once, and the reply is polled (`queued`/`running`/`done`) or cancelled. It is a mutation, so it requires `"confirm": true` (409 otherwise) | does **not** type into the operator's live pane (it starts its own hermes process); no token streaming, by design; restarts nothing; the 600 s timeout is a wedge backstop, not a reply deadline | `orch_message` (a queued nudge that never runs a turn); `/orch-restart` |
| `hscc main start\|status [--json]\|wakeups list\|set <name> <sched>\|enable\|disable <name>` (CLI; `hscc_daemon/hscc.py`) | the **main orchestrator's** own lifecycle: `start` ensures exactly one pane runs `hermes -p <main> chat`, `status` reports name/board/pane/busy through the same `repl_inject` discovery `orch inject` uses, `wakeups` edits `~/.hscc/main_wakeups.json` | is not a message channel and not a restart: it creates the pane the other two talk to; `wakeups` ≠ `orch waker` | `hscc orch inject`; `hscc orch waker` |

Choosing, in one line each: to **say something** to a running orchestrator —
`orch_message` if you are an agent, `hscc orch inject` if you are the operator at
a terminal, the chat route if you want a whole turn that may dispatch real work.
To **restart serving** — `restart_model` if you are an agent, `/heal confirm` (a
sweep) or `/orch-restart` (the orchestrator unit) if you are the operator. To
know **why the daemon refuses to repair a node** — `hscc heal-status`; to stop
waiting on that node — `hscc heal-clear <node>`.

## Node pause (`hscc node pause|resume|paused`)

A pause is a **durable overlay** (`hscc_daemon/node_pauses.py`): the single
source of truth is `~/.hscc/node_pauses.json`, written atomically, no in-memory
cache (the CLI writes it, the daemon reads it — a cache reintroduces the
wrongly-stale-pause bug). The overlay writes **nothing else**: templates,
`serving.json`, the applied record, kanban caps, autodown and the proxy config
are the cluster's config truth, and every one of them has a watcher or apply
path that would fight an edit or restore the node. Repair paths (watchdog
auto-heal, keepalive, `cluster up`, autoscale, the heal breaker) consult the
overlay and skip paused nodes. A skip seam must ask `covering(node)` or
`paused_nodes()` — a unit can span several nodes (tensor-parallel), the record
is keyed by the host the pause was issued against and lists the rest in
`span_nodes`, so asking only `paused(host)` lets the other half of a span be
healed back to life.

Rules, all enforced in code (`node_pause_exec.py` + the CLI; executor is
dry-run by default — rule #478):

- **Single-node-only.** A node parks only when EVERY model on it runs on that
  node alone. A model spanning other nodes is refused BY NAME and **no flag
  overrides it** — not `--force`, not `--yes`. Stopping a TP unit "on this
  node" stops it on every node it spans, so the plan prints the full span
  (observed placement ∪ intent-recorded placement) before anything is asked.
- **`--force` is exactly one guard**: parking the applied template's declared
  orchestrator node, or a pause that would leave no healthy unit serving the
  orchestrator model. It unlocks nothing else.
- **Confirmation REPLACES `--apply`.** The verb prints the plan (host, every
  model it will stop, reason, deadline), then asks `Pause <host> and stop
  these N model(s)? [y/N]`. Only a literal `y`/`Y` proceeds — a bare Enter,
  `n` or `yes` stops and records nothing.
- **`--yes` is the non-interactive answer** — what a script or cron must pass
  to mean it; without a terminal AND without `--yes` the verb REFUSES.
- **`--dry-run` is prompt-free** — prints the plan, changes nothing, asks
  nothing.
- `--reason <text>` is recorded on the pause; `--until <dur>` (`90m`, `2h`,
  `1d3h`, `45` = seconds, or ISO) sets an auto-resume deadline — open-ended
  without it. An expired-but-unresumed record is STILL a pause (`paused()` is
  truth-accurate; only the daemon tick acts on expiry) and an unparseable
  `until` counts as open-ended, never "expired".
- **Resume replays the exact launch argv.** `node resume` re-issues each
  recorded unit through sparkrun's `--ensure` path with the SAME recipe and
  flags the unit was launched with (a different argv hashes to a different
  sparkrun intent id and would start a parallel duplicate of a live unit). A
  unit whose launch recipe cannot be rebuilt is REFUSED with a printed reason
  and the pause is kept — never restarted from a guess. Resume also clears that
  node's heal strikes.
- `node paused [--json]` reads the overlay file only — works with sparkrun
  unreachable.

Agent parity: the `node_pause`/`node_resume`/`node_paused` tools call the CLI's
own verb functions in-process; `confirm=true` maps to `--yes`, no `confirm`
maps to `--dry-run` — never to an unguarded mutation. There is no agent-only
escape hatch from the rules above. (Known wart: the `node_pause` registry blurb
mentions a `span=true` override that does not exist in the schema — multi-node
is refused with no override at all.)

## Orchestrators: `hscc main`, `orch inject`, `orch_message`

The cluster has **ONE main orchestrator**, named at bootstrap (default
`general`), persisted in `~/.hscc/main-orchestrator.json`. The identity resolver is
`hscc-roles/main_orch.py` (`main_name()/main_profile()/main_board()`) — the same
one `hscc main status` and `orch_message` read; never hardcode the name
(`hscc-roles/tests/test_main_orch.py` greps for retired identities even in
docstrings). It lives in a normal interactive Hermes REPL pane so the operator
can talk to it. `hscc main start` ensures EXACTLY ONE pane runs
`hermes -p <main> chat` (reuses a running one; never kills another pane or
window; detached session when no tmux server exists). `main status` reports
name, board, pane, busy/idle through the same `repl_inject` discovery that
`orch inject` uses. `main wakeups` edits the daemon-fired wake-up schedule
(`~/.hscc/main_wakeups.json`).

`hscc orch inject` (CLI-only) types a message into a running Hermes REPL pane
via `hscc-api/repl_inject`: exactly-one-pane rule (missing or ambiguous pane =
refusal, never a guess), `--mode auto|plain|queue|steer` (auto resolves to
`/queue` while the target is busy), `--dry-run` resolves the pane and sends
nothing.

`orch_message` (the agent tool) is the policed nudge channel: BOTH ends must be
`*-orch` profiles or the resolved main orchestrator (worker profiles and
`default` are refused); queue-only, never an interrupt; auto-signed
`[from <sender>] ` plus `re: t_xxxx` with a ref card; rate-limited per
(sender, to) pair (default 6/h, `orch_message.max_per_hour` in `cluster.json`,
env `HSCC_ORCH_MSG_MAX_PER_HOUR`); the reply loop guard refuses answering a
signed nudge with another `orch_message`; every call (sent and refused)
appends to `~/.hscc/state/orch_messages.jsonl`, and if the audit line cannot
be written the message is NOT sent (fail-closed). Naming trap (audit F7):
five different capabilities answer to `orch` — `orch inject`, `orch waker` (added
to the router after the audit was written), `orch_message`, `/orch-restart` and
`POST /v1/orchestrator/chat`. What each one does is tabulated once under "Naming
traps"; keep that table the single source instead of re-listing them here.

## Merge queue (`hscc merge-queue`)

A daemon-owned local FIFO per repo: one PR at a time in a dedicated worktree
under `~/.hscc/merge-queue/<project>/` (never the operator's checkout): fetch →
rebase onto the default branch → run the project gates → `push --force-with-lease`
→ `gh pr merge --squash --delete-branch`. A conflict or red gate aborts
cleanly, comments on the PR, hands the card back to its worker, drops the PR.
State survives daemon restart via `~/.hscc/merge-queue-state.json`. **On by
default** for a project with all three of: git checkout at its registry `repo`,
a `github.com` origin remote, declared (or hscc default) gates — `status`
prints why a project is off (`no repo` / `no GitHub remote` / `no gates`).
Plain git + `gh`, no GitHub org merge queue.

Read surface: `hscc merge-queue status|list` and the read-only
`merge_queue_status` agent tool (same payload as `status --json`).
**Mutations (`add/remove/pause/resume/enable/disable`) stay CLI-only** (D8).

## Kanban: native Hermes, with HSCC hygiene verbs

All project work runs on native Hermes kanban: create a card → triage →
auto-decompose → the gateway's embedded dispatcher (`dispatch_in_gateway:
true`, 60 s tick) runs each ready card in its own git worktree → review. Do
NOT hand-manage worktrees or dispatch. The machine-wide current board is the
ONE main orchestrator's board; a card without `board=` lands there.

**Kanban caps are owned by the serving template**: `caps:
{max_in_progress: N, max_in_progress_per_profile: M}` in the template YAML, and
`template apply` writes them into `kanban.*` in hermes config. The shipped
4-node template declares them; the pin
`hscc-cluster/tests/test_template_declarations.py::TestShippedTemplateCapsMatchSOT`
forces template and pinned SOT to move together. Operator live values at this
writing: **6 total / 2 per profile** (`hermes config` →
`kanban.max_in_progress: 6`, `max_in_progress_per_profile: 2`) — check before
quoting a number, the operator moves these.

HSCC adds board-hygiene verbs, load-bearing for autodown (ANY non-terminal
card anywhere is treated as work-in-progress and holds a teardown):
`hscc kanban blocked` / `stale` (default 7 days; `--archive <id>` archives
exactly one card, never bulk), `hscc kanban monitor` (read-only counts),
`hscc cards monitor --live` (TTY panel). `hscc kanban pause|resume|paused|enforce`
is the operator's board brake (reclaims running cards, blocks ready ones,
holds auto-decompose, interrupts + steers the board's orchestrator and pauses
its crons) — **CLI-only, D6**.

## Autodown — operator policy: stays OFF

`hscc autodown` is the idle serving-layer teardown/wake
(`~/.hscc/autodown.json`, owner `hscc_daemon/autodown.py`, teardown/autoup
interlock `autodown_interlock.json`). `status` shows armed-or-not, the idle
window, state reconciled against the live serving layer, and which interlock
(kanban work, PR/CI, watchdog block) is holding a teardown off.

**Operator policy: autodown must stay OFF** (`enabled: false` is the live,
intended state). Agents never enable it; there is deliberately no
enable/disable/wake/cancel tool (fleet-power policy is operator scope). The
read-only `autodown_status` tool / `GET /v1/autodown/status` route / CLI
`status` all show the same payload. If you find it armed unexpectedly, report
it — do not "fix" it by tearing the fleet down.

## Templates, `serving.json`, the applied record

Templates are declared fleet layouts (`hscc-cluster/templates/`, YAML v4
intent): topology, families, per-node request caps, kanban caps, balancer
(`proxy:`), reasoning-effort tiers. The chain is

- `hscc template list|status|preview <n>|validate <n>|apply <n> --confirm`
  (`status` answers which template is applied),
- `template validate` is the safe read — structural + placement layers, ONE
  implementation that `apply` also runs as its pre-flight gate before
  touching anything (a failed gate leaves the fleet untouched),
- `template apply --confirm` (HIGH-RISK — reshapes the whole fleet; without
  `confirm` you get a preview): resolves against the live cluster, writes
  `~/.hscc/serving.json` (the serving topology), models.json, routing/proxy
  config, kanban caps, provisions units, then records
  `~/.hscc/applied_template.json`,
- `~/.hscc/applied_template.json` is the record of WHICH template is live
  (`template`, `applied_at`, `orchestrator_node`, `families`, `units`, + the
  resolved v4 declaration block). Writers: exactly one
  (`hscc-cluster/cluster_template.py`); readers include the cluster-guard hook,
  `node_pause_exec` (the orchestrator-node guard reads `orchestrator_node`),
  `verify`, `declaration_agreement`, `hscc-commands/cmdlib`.

`~/.hscc/serving.json` has three writers (bootstrap's `serving_gen`,
`template apply`, `onboard.py`) — it is the definition of "orphan" for
`reap_orphans`, so a stale copy makes a live workload reapable: double-check
`serving.json` against reality before reaping anything (audit F13).

## The `hscc-cluster` agent tools (31)

Registered in `hscc-cluster/__init__.py` in seven families — reads:
`cluster_status`, `list_recipes`, `pick_node`, `discovery_status`,
`nas_status`, `node_paused`, `autodown_status`, `merge_queue_status`,
`doctor`, `heal_status`, `main_status` ·
ops: `provision_model`, `stop_model`, `model_health`, `node_pause`,
`node_resume` · debug: `vllm_logs`, `node_diagnostics`, `nas_diagnose` ·
heal: `restart_model`, `remount_nas`, `repair_nas_export`, `reap_orphans` ·
fleet: `cluster_verify`, `cluster_throughput`, `fleet_stats`,
`autoscale_advice` · template: `list_templates`, `preview_template`,
`apply_template` · orch: `orch_message`.

Every mutating tool is GUARDED: without `confirm=true` it returns a
preview/plan and touches nothing. `discovery_status` precedence is
live `sparkrun cluster list --json` → `~/.hscc/cluster.json` cache → defaults —
the precedence is the defence against a stale cache, never re-order it.
`fleet_stats` reads the kanban DB through Hermes (hermes owns it — never open
`state.db` yourself). Onboarding a NEW model/quant cluster-wide (recipe +
offline NAS→node cache + wire configs + launch + verify): use the
**`hscc-model-onboard`** skill.

## Model name must agree in 3 places

Clients query vLLM by exact model name; a stale name = 404 even when the
server is healthy. Keep these in sync with what `curl http://<orchestrator>:8000/v1/models`
reports (`data[0].id`):
1. `~/.hermes/config.yaml` `model.default` — THE big one (every agent + cron with
   `model:null` inherits it)
2. `~/.hscc/models.json` `primary_model` + `models[].name`
3. `~/.hscc/serving.json` unit `model`/`recipe`

`hermes status` shows the effective `Model:` live. Best practice: never
hardcode the quant suffix; resolve from `/v1/models`.

## Daemon (hscc_daemon)

The only continuous process: launchd `com.hermes.hscc_daemon`, live code
`~/.hermes/plugins/hscc_daemon/`. Polling streams write
`~/.hscc/state/<stream>.json`; a trigger engine evaluates `~/.hscc/events.jsonl`;
notifications are desktop + `events.jsonl` — the daemon posts to no chat
platform. Health check: `ps aux | grep hscc_daemon | grep -v grep` —
`launchctl list` exit codes are often STALE; trust the process list +
`hscc status`. One owner per state file is the invariant (the audit's §4
table is the map); `hscc triggers`, `hscc log`, `hscc status` read the
daemon's own streams.

## Convention

- Restart the gateway after any prompt/plugin/config edit:
  `launchctl kickstart -k gui/$UID/ai.hermes.gateway`.
- Dual-layout: when editing a live plugin/skill, sync its
  `install/hscc-skills/` (skills) or `install/hscc-plugins/` (plugins)
  template to avoid drift. `BUNDLED_SKILLS` in `hscc-skills/hscc.py` controls
  what bootstrap (re)seeds — and it only ever writes `$HERMES_HOME/skills`
  (the DEFAULT home); profile trees under `~/.hermes/profiles/` are curated
  separately and drift both ways (audit F11). A template edit is not done
  until the shipped file is committed AND installed.
- Never edit sparkrun recipes; switch to an alternative if one breaks. Never
  patch Hermes core.
- "Strip/archive" = MOVE to `_archive/<date>/`, never delete.
- Docs and commits carry placeholder addresses (`192.0.2.x` TEST-NET for the
  cluster; the fleet's documented placeholder token for a tailnet host),
  never the live ones — this repo is public.

**Archived 2026-06-08** (in `_archive/2026-06-08/`): hscc-agent-coordinator,
hscc-projects, hscc-orchestrator, hscc-events, hscc-governance, hscc-mcp.
**Archived 2026-06-10**: hscc-provision (its `provision_model` moved into
hscc-cluster), hscc-chat, hscc-optimizations. `hscc-commands` slash verbs
(`/cluster`, `/status`, `/orch-restart`, `/template`, `/heal`, `/workers-up`,
the `/cluster-*` family) are the operator's fast surface; note `/heal` heals
unhealthy workers while `hscc heal-status|heal-clear` are breaker strikes
(audit F7).

## References (live subsystems)

- `references/apps-fleet.md` — the two shipped apps, release lanes, gh-pages
- `references/event-driven-architecture.md` — daemon kqueue + launchd event system
- `references/monitoring-daemon-design.md` — daemon health checks, circuit breakers, graded alerting
- `references/multi-node-provisioning-troubleshooting.md` — multi-node vLLM provisioning failure modes
- `references/runtime-bump-verification.md` — runtime dep bump (hermes/sparkrun): recovery point first, never checkout main

> Dead-pipeline reference docs (orchestrator-tick-flow, task-creation-workflow,
> agents-data-model, permissions-reference, integration-assign-task-provision,
> kanban-sync-fix, stale-agent-cleanup, idle-monitor-integration, event-types,
> hscc-13-plugins-audit) describe the removed agent pipeline — treat any
> citation of them as historical; the pipeline was archived 2026-06-08.
