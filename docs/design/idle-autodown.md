# HSCC Idle Autodown / Autoup — design for the serving layer

**Date:** 2026-08-23
**Status:** Draft — design doc only. No implementation code is produced by this card.
**Branch:** `dev`
**Authoritative reference:** every claim below cites the real file + line. If a
referenced function changes, re-verify before implementing.

## Purpose

Bring the HSCC **serving layer** (the GPU vLLM containers that serve the
orchestrator + worker models) DOWN when the cluster is idle for a configurable
window, freeing GPU memory and power, and bring it back UP automatically on the
next observable inbound activity. Opt-in, off by default.

The **serving layer** is precisely the set of sparkrun vLLM units defined in
`~/.hscc/serving.json` (`serving.load_serving()`, `hscc_daemon/serving.py:37`),
which are the units the watchdog and keep-alive health checks supervise:

- **orchestrator unit** (`role: orchestrator`) — currently spans nodes
  `10.0.0.244` (head) + `10.0.0.246`, port 8000. This is the model
  C1 calls "A3B on 10.0.0.244". Stopped by `serving.VLLM_STOP_CMD`
  (`sparkrun stop --hosts <primary>`, `serving.py:150`), started by
  `serving.VLLM_START_CMD` (`sparkrun run <recipe>...`, `serving.py:151`).
- **worker unit** (`role: worker`, `keepalive: true`) — currently spans nodes
  `10.0.0.247` + `10.0.0.248`, port 8000 (see C4 for how autodown
  treats keepalive units).

## Hard constraints (restated from the card, grounded in real code)

### C1 — the wake path must not depend on anything autodown turns off
The wake trigger and the idle timer **must** live in the always-on CPU-side
daemon (`hscc_daemon`), never in a model/agent prompt. Verified: the daemon
runs on the macOS gateway (its loop is `daemon_ops.run_daemon_loop()`,
`hscc_daemon/daemon_ops.py:167`); the Hermes gateway supervisor
(`ai.hermes.gateway` launchd job, `health.py:263`) is also CPU-side and stays
up. The only thing autodown turns off is the **GPU serving layer** (remote vLLM
containers). The daemon + gateway supervisor survive a teardown, so they *are*
available to observe the wake event. There is no dependency on a model in the
wake path.

### C2 — the watchdog will fight an intentional teardown
`pipeline_watchdog()` at `hscc_daemon/lifecycle.py:191` auto-heals the
pipeline. When not blocked, a failed DGX/gateway check auto-restarts vLLM
(`lifecycle.py:319-351`) and health-check threads (`check_workers`,
`health.py:791`) relaunch crashed keep-alive worker units, previously with
wrong, uncoordinated solo containers. An idle teardown therefore MUST set the
watchdog block (`blocked: true`) via `save_watchdog_block()`
(`lifecycle.py:141`) so the watchdog backs off instead of resurrecting, and
autoup MUST clear it. Distinguished from a crash-while-down in §7.

### C3 — never auto-down while there is work
Idle requires zero running/ready/review work AND an elapsed inactivity window.
Activity timestamp sources are defined precisely in §2.

### C4 — whole-fleet down, keepalive NOT exempt (operator decision)
`serving.keepalive_nodes()` / `serving.keepalive_units()` (`serving.py:162,172`)
designate worker units the health-check thread keeps alive. **Autodown now
takes the ENTIRE serving layer down — including keepalive units** (operator
decision, REVERSES the original C4 exemption). Powering everything down is the
point of the feature. Fleet down/up use the shared `serving.fleet_down_cmd()` /
`serving.fleet_up_plan()` builders, exposed to the operator as `hscc cluster
down` / `hscc cluster up`; autoup restores every unit (orchestrator AND
keepalive workers). See §3.

### C5 — opt-in, off by default
Defaults to disabled. An operator must explicitly `hscc autodown enable`. The
default for every mode/flag is non-acting. §7.

---

## 1. Idle definition + exact activity sources

"Idle" is a conjunction, per C3. Autodown MAY proceed only when **all** of the
following hold. Any single condition false ⇒ not idle ⇒ no teardown.

### 1a. No dispatched work in the kanban pipeline
The fleet drives work through the kanban DB. Autodown must see zero work in any
in-flight or about-to-be-picked-up state. The **authoritative source is the
kanban SQLite DB `~/.hermes/kanban.db`** (the shared board both orchestrators
and workers read/write). Inspect it via a read-only helper that reuses the same
logic path as the fleet's own `flightdeck/core/kanban.py` (`_load_kanban_db`) —
**do not** shell out to `hermes kanban ...` and parse text (same fragility the
HSCC codebase already removed elsewhere; see `hscc_daemon/hscc.py:76`
`_kanban_task_status`, which shells out — that is the legacy pattern we do NOT
copy).

The exact "no work" predicate (all required):
- zero tasks in `running`
- zero tasks in `ready` / `in_progress` awaiting dispatch
- zero tasks in `review` / `qa` that a reviewer/QA profile is about to pick up
  (matches the exclusion list already used by `live_dispatch_hosts()`,
  `hscc_daemon/hscc.py:90-114`: `done, review, archived, blocked` are the
  terminal/parked statuses that do NOT count as active work)

Helper shape (new): `autodown.hscc idle._has_active_work(kanban_db) -> bool`
mirroring the status vocabulary in `hscc.py:112`. Because the dispatcher can
claim `ready` cards at any moment, this predicate is (correctly) conservative:
any ambiguous state counts as "work".

### 1b. No agent currently executing
Beyond kanban, an agent may be mid-turn on a direct (non-kanban) request (e.g.
a Telegram DM conversation in progress). Source: `~/.hscc/agents.json`
(`health.py:551`). Idle requires **every** enabled agent's `status` to be
`idle` (i.e. no agent `working`/`failed`, `health.py:561-564`). This is the
already-maintained fleet status used by `check_heartbeat`.

### 1c. The measured inactivity window has elapsed
A **single monotonic "activity timestamp"** is maintained on disk at
`~/.hscc/autodown.json`, field `last_activity_iso`, updated by every activity
source in §1d/§1e. Idle-window-elapsed = `now - last_activity_iso >=
idle_minutes`. Re-reading the clock is not enough — the timestamp must be
*advanced by actual events*, so that a long stretch of silence (no events at
all) does NOT count as idle-only-after-a-silent-cluster. Wait: a cluster with
zero events is by definition idle for measurement purposes, but the precedent
here is important — see §1d on what "activity" means and the 1e "warm-up"
guard so autodown never fires on a freshly-booted, never-touched cluster.

### 1d. Activity sources that RESET the idle timer
Any of these advances `last_activity_iso` (and cancels any in-progress teardown
countdown):

1. **Inbound HSCC HTTP API request** — the daemon must be able to see it. Today
   the API server (`hscc-api/api_server.py`) is a separate process and the
   daemon does not watch it. The design adds a wake/activity probe (§4): an
   API request writes a "last seen" timestamp the daemon polls, OR the API
   server pokes the daemon. Recommend the file-based signal so no new RPC:
   `api_server` writes `last_activity` to `~/.hscc/activity.json` on every
   authenticated request (single write, `api_server._do_stamp_http_activity`),
   and the daemon's idle timer reads it. This is CPU-side-writable and needs no
   model. *The marker lives at `~/.hscc/activity.json` — OUTSIDE
   `~/.hscc/state/` — because activity is event-driven (updates only on a
   request) and carries no `ok` key, so it must not share the periodic-streams
   dir that `verify.py::check_daemon_streams` requires to be fresh ok streams.*
2. **Inbound Telegram message** — the fleet's Telegram is owned by a
   single-writer MCP daemon (`~/.hermes-tg/mcp_server.py`), not by the daemon.
   The daemon does NOT parse Telegram. Instead the **gateway supervisor or the
   Telegram MCP daemon stamps an "inbound activity" file** whenever it receives
   a message. Design: a tiny CPU-side watcher in the daemon polls a heartbeat
   file, e.g. the Telegram MCP daemon is extended to touch
   `~/.hscc/activity.json` (field `telegram_ts`) on an inbound update.
   This is a **model-free observable event** the CPU daemon can see.
   *(If that MCP integration proves infeasible in Phase 1, an acceptable
   Phase-1 fallback is: the daemon treats a Telegram-aware event via
   `api_server`'s `/mcp` route as an activity source — but the primary design
   is the MCP daemon stamping the file. Honest note: the exact wiring into the
   Telegram MCP daemon needs one interop card to confirm the hook point; the
   design keeps the contract abstract so the implementation can pick the hook.)
3. **New kanban card** — a card created/ready in the DB changes kanban state.
   The idle monitor reads the DB (§1a) each cycle; a transition from
   "no active work" to "new ready card" is activity. Additionally, the kanban
   DB mtime in `~/.hermes/kanban.db` serves as a coarse proxy: any write to the
   DB resets the timer. Both are model-free and CPU-side.
4. **Explicit CLI command** — `hscc autodown wake` (and any `enable`, or any
   `hscc` invocation that touches the serving layer) directly sets
   `last_activity_iso` and, if down, triggers autoup. Model-free: the CLI runs
   on the CPU gateway.

### 1e. Warm-up / first-boot guard
`last_activity_iso` starts as the moment autodown is enabled (or daemon start).
Autodown will therefore not fire for at least `idle_minutes` after enablement,
and never on a never-used cluster — because idle requires an elapsed window
measured from an actual activity timestamp, not from boot. This prevents a
just-enabled or just-booted, touched-once-then-silent cluster from tearing down
while an operator is mid-setup.

### 1f. Keepalive-nodes and the idle signal
Keepalive worker units are no longer exempt from teardown (C4 reversed), so
under autodown the whole serving layer either serves or is down. A keepalive
unit does not itself advance the idle timer, but the idle predicate in §1a–1d
applies to work/activity, not to whether keepalive servers exist.

---

## 2. Where the timer runs (+ evaluation of PERIODIC_STREAMS reuse)

**Decision: run the timer as a NEW thread inside `run_daemon_loop()`**
(`daemon_ops.py:167`), NOT as a launchd `PERIODIC_STREAMS` entry.

Rationale (grounded in the code):

- `PERIODIC_STREAMS` (`event_driven.py:47`) installs fixed-interval **launchd
  jobs** — each becomes a separate short-lived `hscc check <stream>` process
  (`event_driven.py:437-447` generates the plist with the interval baked in).
  These are stateless per-invocation checks. Autodown needs **stateful,
  in-process coordination**: it must correlate the watchdog block file, the
  activity timestamp, teardown/wake sequencing, and in-flight guard against the
  same daemon's watchdog thread. A stateless launchd job cannot hold that
  state across invocations without re-reading/rewriting the world each tick and
  cannot atomically coordinate with an in-process watchdog thread it doesn't
  share memory with.
- The daemon's own `run_daemon_loop()` already runs a watchdog thread at a
  30s cadence (`daemon_ops.py:217-223`) and a trigger thread at 15s
  (`daemon_ops.py:225-237`). A dedicated `run_autodown_loop()` thread at,
  say, 30s is the natural sibling — same daemon, same state dir, same
  `stop_event` shutdown discipline.
- The timer must run on the **always-on CPU daemon**. A launchd `PERIODIC_STREAMS`
  entry also runs on the CPU gateway, so it is *technically* viable — but it
  loses the in-process coordination with `pipeline_watchdog` that C2 demands
  (the teardown must set the block in the same state the watchdog reads).
  Reusing `PERIODIC_STREAMS` would force the timer logic to be a self-contained
  CLI-callable check that communicates only through files.

**Decision recorded:** a new `run_autodown_loop()` thread in
`daemon_ops.run_daemon_loop()`, cadence 30s (matching the watchdog), calling a
new `autodown.cycle()` function. It is OFF by default: the thread short-circuits
immediately when `autodown.json` has `enabled: false`.

Minimum cadence: since activity is written on events and the idle window is `N`
minutes (default 10), a 30s timer gives ample slack and never re-actively tears
down after a recent event.

---

## 3. Teardown sequence

Goal: free every GPU in the serving layer, in a defined, reversible order,
marking the teardown intentional so the watchdog doesn't fight it (C2). **All
serving units — orchestrator AND keepalive AND non-keepalive workers — are
torn down** (C4 reversed).

What is **preserved** (never torn down, never stopped):
- the daemon itself (CPU, on the Mac gateway)
- the Hermes gateway supervisor job `ai.hermes.gateway` (CPU, reads Telegram)
- the Telegram MCP daemon (CPU, `~/.hermes-tg/mcp_server.py`)
- the HSCC HTTP API server (CPU, `hscc-api`)
- NAS, state files, kanban DB, profile configs — all on disk

Only the remote GPU serving layer is stopped. On wake, `hscc cluster up` /
`autodown.autoup()` restores EVERY unit (orchestrator AND keepalive workers).

Order of operations (all inside one `autodown.teardown()` that runs in the
autodown thread; each step is logged via `daemon_ops.log`, `daemon_ops.py:146`):

1. **Re-verify idle** (C3 interlocks, §6). If any work appeared since the timer
   decided to teardown, abort. This is the last-line guard.
2. **Write the watchdog block BEFORE stopping anything** (C2). Set
   `blocked: true`, `reason: "autodown: intentional idle teardown"`,
   `blocked_at: now`, and a new field `intentional: "autodown"`. Persist via
   `save_watchdog_block()`. From this instant the watchdog backs off
   (`lifecycle.py:215-234`) and `check_workers` must be taught to skip
   keepalive-relaunch while this block is set (§5).
3. **Stop the ENTIRE fleet** — ONE `sparkrun stop --all` (built by the shared
   `serving.fleet_down_cmd()`, `serving.py` — the same source of truth the
   `hscc cluster down` CLI wrapper uses). This powers ALL serving units down:
   orchestrator AND keepalive AND non-keepalive workers. Autodown no longer
   issues per-unit `sparkrun stop --hosts <nodes>` (the form that failed with
   no TARGET). Cancel is still honored via `cancel_requested` before the stop.
4. **Verify down**: after the stop, confirm every unit's HEAD port no longer
   responds (reuse `health.http_check`, `autodown._probe_down`). TP peers
   serve through their head only, so only each unit's `nodes[0]` is probed.
   Record final serving state in `~/.hscc/autodown.json` (`state: "down"`).
5. **Write `~/.hscc/autodown.json` state** with `state: "down"`,
   `down_since`, `reason`. This file is the single source
   of truth for "we are intentionally down" that autoup, status, and the
   watchdog all read.
6. **Notify the operator** (desktop + Telegram ops topic):
   `send_macos_notification` (`desktop.py`) + `notify_operations`
   (`telegram.py:58`). Note the Telegram notify itself is outbound-only and
   does not require the serving layer.

**Failure mid-teardown:** if a stop fails (step 3), the design does NOT leave a
half-torn cluster with the block latched. §8 failure modes covers it: roll the
block back (clear intentional flag, leave `blocked` as the watchdog would) and
report, so the watchdog resumes and can heal the remaining units back up.

### C4 decision: keepalive units are NO LONGER exempt (operator decision)
Originally autodown exempted keepalive worker units (`serving.keepalive_units()`,
`serving.py:172`) — the keepalive flag was treated as a standing "this model
must stay up" commitment that beats an idle timer. **That is REVERSED (operator
decision).** Autodown now powers the WHOLE serving layer down, including
keepalive units — freeing that GPU memory and power is the entire point of the
feature. The design:

- issues a single fleet stop (`serving.fleet_down_cmd()`, `sparkrun stop
  --all`) that stops EVERY unit — orchestrator, keepalive, AND non-keepalive
  workers;
- on wake (`serving.fleet_up_plan()` / `hscc cluster up`) restores EVERY unit,
  orchestrator AND keepalive workers;
- keeps the `keepalive_ok` **interlock** (§6.4): if a keepalive unit is sick
  we abort teardown — a "don't shut down mid-problem" guard, NOT an
  exemption. Healthy or not, keeping it still goes down.

---

## 4. Wake sequence + first-message handling (C1)

Wake is triggered by an **observable event the CPU daemon sees without a
model** (see §1d). When `autodown.cycle()` observes an activity event while
`state == "down"`, it calls `autodown.autoup()`.

`autodown.autoup()` sequence:

1. **Load config + mark waking.** Set `autodown.json` state to `"waking"`
   (idempotent; if already waking, no-op).
2. **Record the event that woke us** in `autodown.json` (`wake_source`,
   `wake_at`) so the operator can see the trigger after the fact.
3. **Start the serving layer back up — EVERY unit** (orchestrator first, then
   workers) via the shared `serving.fleet_up_plan()` (the exact reverse of the
   fleet down; there is no in-flight request because we are waking from zero).
   Orchestrator built from `serving.VLLM_START_CMD` / `serving.orchestrator_recipe()`
   (`sparkrun run <recipe> --ensure`); workers from their own recipe. Keepalive
   AND non-keepalive workers all come back up.
4. **Wait for readiness** — poll `serving.VLLM_HEALTH_URL` (and each unit's
   port) with `health.http_check` until healthy or a timeout (reuse the load
   grace pattern, `lifecycle.py:125`, `VLLM_LOAD_GRACE_MINUTES` default 20).
   This is the model-load window.
5. **Clear the watchdog block** (C2). ONCE serving is confirmed up, set
   `blocked: false`, clear `intentional`, clear `failures`
   (`save_watchdog_block`), restoring the watchdog to normal supervision.
   **Order matters**: only clear the block after the units are demonstrably
   up — otherwise the very first watchdog tick after the block clears could
   see a not-yet-ready cluster and latch the breaker.
6. **Flush the held wake reason** and set `state: "up"`.
7. **Notify** desktop + ops Telegram "serving layer is back up".

### First-message handling — the critical C1 case
The wake event itself ("inbound Telegram message", "inbound HTTP request",
"new kanban card") arrives **before** the serving layer is up. The daemon
cannot itself answer the message (answering needs a model), so the design makes
the point of arrival the *signal*, and the handling depends on the source.

**Corrected behaviour (2026-08-25, design card t_a4e700ee — supersedes the
interim t_d6bdec0e "notify, do not replay" approach):** contrary to the
original claim below, the wake-triggering **Telegram message is NOT durably
queued by Telegram itself.** Telegram is not a queue — the Hermes gateway
*drains* each message on arrival: it consumes it, calls the not-yet-loaded
model, fails, and replies with an error. The original text is persisted only
for session context and is **never retried**. Reproduced live 3/3 times. If
autodown simply notifies, the user must re-send. The operator has since decided
the better fix: **queue the messages while the cluster is down/waking and
replay them automatically once it is up**, exactly as they would have arrived.
That is what this design now specifies and what `hscc_daemon/replay.py`
implements.

- **Inbound Telegram message.** The wake is triggered by the daemon's
  `probe_telegram_activity` (§1d.2) observing the gateway log line
  `inbound message: platform=telegram ...`. That probe does NOT just note the
  fact — while the cluster state is `down` or `waking` it captures each fresh
  inbound line's full routing metadata (text, platform, chat id, `reply_to_id`,
  user, arrival timestamp) into a **durable HSCC-side queue** at
  `~/.hscc/queued_messages.json` (`replay.enqueue_inbound`, `replay.py:280`),
  preserving **arrival order** via a monotonic `seq`. This is the real fix for
  the dropped-first-message bug: the message is not lost, it is queued and
  replayed once the wake completes. Details:
  1. **Once-per-wake waking notice.** On the FIRST message queued in a wake
     (`replay.enqueue_inbound`, when `notice_sent_this_wake` is still false)
     the daemon posts one "cluster is waking from idle autodown; this takes a
     few minutes; your message is queued and will be processed automatically —
     no need to re-send" notice via the best-effort notifier (desktop + ops
     Telegram, `telegram.notify_operations`, `telegram.py:58` — CPU-side,
     works with the fleet down). Fired **once per wake**, not per message and
     not per tick. The flag resets when the queue is emptied on a successful
     wake, readying the next wake.
  2. **Replay on readiness.** After `autoup()` confirms readiness (the point
     where the watchdog block is cleared — `_autoup_locked` step 6a,
     `autodown.py`), `replay.replay_queued` (`replay.py:360`) delivers each
     queued message **in arrival order** through a delivery seam, then clears
     the queue. Delivery is via the operator-configured path (see §4.4 below on
     the production default and its runtime requirement).
  3. **Wake-complete notice.** After replay, `_notify_wake_complete` still
     posts a "cluster is up" notice quoting the original triggering message for
     the operator's record.
- **Inbound HTTP API request.** The HSCC API server is CPU-side and always-on;
  it receives the request immediately. For a request that needs the serving
  layer (e.g. a generation request), the API handler synchronously triggers
  `autodown.autoup()`, then either:
  - **blocks** until readiness (bounded by the load-grace timeout) and then
    serves the request, or
  - returns `202 Accepted` + `{"status": "waking", "retry_after": 90}` and the
    client polls `/v1/status` until `"up"`, then retries.
  Recommend the **`202 + retry_after`** path: it never holds a connection open
  across a multi-minute model load, and the request is preserved client-side.
  A pure READ endpoint (`/v1/ping`, `/v1/status`) does not need the serving
  layer and is served immediately (it also resets the idle timer, which is
  desirable — the API is being used).
- **New kanban card.** New cards are work created on disk; the dispatcher
  (also CPU-side / always-on) is what actually picks them up. Autodown's
  discovery of the card only triggers autoup; the card itself is safe in the
  DB. The dispatcher will claim and dispatch it once profiles are serving
  again. `live_dispatch_hosts()` (`hscc.py:90`) continues to function.
- **CLI `hscc autodown wake`.** The CLI call is synchronous; it triggers
  autoup (in-process or via writing the wake marker), and returns a status
  line. There is no first-message problem because the CLI invocation prompted
  the wake itself.

**Rule for all sources:** the daemon's ONLY job on wake is to (a) persist the
event, (b) bring serving up, (c) signal "waking", and (d) — for Telegram —
capture the message into the durable replay queue (see §4.4 for bounds,
the once-per-wake notice, and the failure contract). It never fabricates an
answer, and it never silently drops a queued message: if a queued message
cannot be replayed it is retained and reported loudly, never discarded.

**Corrected guarantee (telegram):** while the cluster is down/waking, every
inbound Telegram message is durably queued (in ~/.hscc/queued_messages.json)
with full routing metadata and replayed automatically in arrival order once the
wake completes — it is NOT lost to a dropped first message. The contract is
now a real queue + replay, not a silent drop-and-process and not just a
notification asking the user to re-send. Bounds and failure modes are in §4.4.

### 4.4 Queue bounds, delivery, and the failure contract

Implementation: `hscc_daemon/replay.py` (queue file `~/.hscc/queued_messages.json`,
atomic writes via tmp + `os.replace`, exactly the `save_config` pattern). The
queue is capped and age-bounded so it can never grow unbounded or execute a
stale instruction against a changed cluster:

- **Capacity.** `MAX_QUEUED_MESSAGES = 100` (`replay.py:44`). When a new message
  arrives while the queue is already full, the **oldest** queued message is
  dropped with a clear `WARN` log. The newest messages win, so the most recent
  user intent is never evicted.
- **Age.** `MAX_MESSAGE_AGE_MINUTES = 180` (`replay.py:49`, i.e. 3 hours). A
  message that has been queued longer than this is **not** replayed; at replay
  time it is dropped with a `WARN` log ("not executed") rather than silently
  run against a cluster that has moved on hours later. Fresh messages under the
  bound are always replayed.
- **Ordering** is guaranteed by a monotonic `seq` stamped at enqueue time;
  replay iterates strictly in `seq` order.
- **Idempotency / no double-send.** A message is removed from the durable queue
  only *after* its delivery handoff succeeds. The queue file is persisted after
  each successful dequeue, so a crash between replaying one message and the
  next cannot re-send an already-handed-off message: the handoff is durable
  before the dequeue, and the dequeue is durable before the next handoff. The
  residual window — a crash precisely between an external handoff returning
  success and the local atomic write landing — yields at-least-once delivery,
  the industry-standard guarantee for a non-idempotent remote.
- **Failure-safe.** If a delivery fails (returns False or raises), the message
  **stays queued** and a loud `ERROR` is logged; replay stops, so a
  cluster-side problem cannot silently eat every message. `replay_queued`
  raising inside `autoup` is caught (`autodown.py`) so a delivery problem never
  crashes the wake itself.
- **Delivery seam.** `replay.replay_queued(deliver_message=...)` hands each
  queued message to an injectable callable. In tests this is a fake; in
  production the default (`replay.default_deliver_message`, `replay.py`) is a
  full orchestrator round-trip through components that exist on this host:
    1. **Map** the message's chat/topic to an orchestrator profile+session
       (the `<project>-orch` / session `<project>` convention from
       `hscc-roles/orchestrators.py`, catch-all `general-orch` / `general`),
       resolved via the flightdeck registry's per-project `topic` binding.
    2. **Invoke** that orchestrator exactly the way the HSCC API does
       (`hscc-api/routes_orchestrator.py`): `hermes -p <profile> chat -Q
       --continue <session> -q <text>` (argv as a LIST — never shell-
       interpolated), so the message is actually processed and the reply is
       the only thing on stdout.
    3. **Reply** — post the orchestrator's reply back to the message's
       ORIGINAL chat/topic via `telegram.send_message` (the same Bot API
       transport `telegram.notify_operations` uses for operator notices,
       generalised to an explicit `chat_id`/`thread_id`/`reply_to_id`).
  **Mapping policy (deliberate):** a `platform != "telegram"` message is
  UNMAPPABLE — the installed reply path is telegram-only — so it stays queued
  and is reported loudly, never guessed. A telegram message whose thread/topic
  matches a registry project's `topic` goes to that project's orchestrator; a
  telegram message with no matching topic (General topic, direct chat, or an
  unbound topic) goes to the `general` catch-all (which exists precisely for
  that). No runtime webhook configuration is required — the daemon reads the
  registry file (`~/.flightdeck/registry.yaml`, overridable via `HSCC_REGISTRY`)
  and shells `hermes` (`HSCC_HERMES_BIN`) directly.
  **Failure contract:** `default_deliver_message` returns True only on the FULL
  round trip (orchestrator produced a non-empty reply AND the reply was posted).
  If the orchestrator is unavailable, its session is not ready, it returns an
  empty reply, or the reply cannot be posted, delivery returns False and every
  message is retained + reported loudly — fail-closed, never silently dropped.
  A pathological orchestrator-ran-but-reply-failed case is logged as CRITICAL
  (re-running it would re-process the prompt — at-least-once), so the operator
  can clear the queue deliberately.

### 4.5 No-op on empty queue
A wake via CLI/HTTP/kanban with an empty (or absent) queue is a clean no-op —
`replay.replay_queued` returns `empty=True` and `autoup` proceeds normally. No
crash, no spurious notice.

---

## 5. Watchdog coordination (C2)

The single source of truth for "intentional down" is
`~/.hscc/watchdog-block.json` (read by `load_watchdog_block`,
`lifecycle.py:132`) with the existing `blocked` flag + a NEW field the design
adds: `intentional` (value `"autodown"` when autodown tore the layer down,
else absent).

### How a crash-while-intentionally-down is distinguished from normal intentional down
- **Normal intentional down:** autodown set `blocked: true` AND `intentional:
  "autodown"` AND `state == "down"` in `autodown.json`, with the teardown
  verified complete. The watchdog sees `blocked` and backs off
  (`lifecycle.py:215-234`) — it does NOT resurrect anything. This is exactly
  the desired state: the cluster can stay down indefinitely without the
  watchdog fighting it.
- **Crash-while-intentionally-down:** something the daemon does NOT control
  died after the (whole-fleet) teardown finished, or the NAS drops. How we tell
  a deliberate down from a crash: with the whole-fleet-down decision (C4
  reversed), the entire serving layer is down BY DESIGN while
  `intentional == "autodown"` — every unit (orchestrator, keepalive,
  non-keepalive) was deliberately stopped, so the watchdog must NOT resurrect
  any of them. `pipeline_watchdog` skips the orchestrator auto-restart
  (`lifecycle.py:319`) while the intentional block is latched; `check_workers`
  relaunches crashed keepalive units, so its relaunch must also respect the
  intentional block or it would fight the teardown (the block gates both).
  Distinguisher: a unit recorded `state == "down"` in autodown.json is NOT a
  crash — it's expected. A unit that is up/down contrary to autodown's recorded
  state IS a crash and must be healed.
  - A decision table lives in `autodown.py` (`classify(idle_state_block,
    autodown_state)`): given the watchdog block + autodown state, classify each
    unit as `expected_down`, `should_be_up` (heal), or `healthy`. Autodown
    writes this; the watchdog's intentional-aware fork consults it.

### State transitions that matter
- Idle teardown requested → write block (`blocked:true, intentional:"autodown"`)
- All stops done + verified → `autodown.json` `state:"down"`, `down_since`
- Wake event → `state:"waking"`
- Serving verified up → clear block (`blocked:false, intentional: removed`)
  → `state:"up"`
- Operator `hscc autodown disable` while down → **leave serving down** (don't
  auto-restart on disable) but clear the `intentional` block so the watchdog
  resumes supervision; the operator then decides when to bring it up. This is
  explicit in the CLI design §7.

---

## 6. Safety interlocks (C3)

Before EVERY teardown (both at the timer's trigger decision and again
immediately before the first stop), `autodown.cycle()` evaluates the full idle
predicate §1. It is a conjunction; any false ⇒ abort. Sources:

1. **Kanban work** — read `~/.hermes/kanban.db` (`_has_active_work`), zero
   running/ready/review/qa.
2. **Agent liveness** — read `~/.hscc/agents.json`, all enabled agents `idle`
   (no working/failed).
3. **Elapsed window** — `now - last_activity_iso >= idle_minutes`.
4. **Keepalive units ready** (`keepalive_ok` INTERLOCK) — keepalive units ARE
   teardown targets now (C4 reversed), but if a keepalive unit is itself
   unhealthy we abort, so we do not tear the whole fleet down mid-problem while
   a worker is mid-flight relying on it. Cautious and simple.

The double-evaluation (timer + pre-stop) closes the race where a card is
created between the cycle's idle decision and the first `sparkrun stop`. If the
pre-stop re-check fails, teardown is cancelled cleanly (block already written —
roll it back, §3 failure handling) and the operator is notified
"aborted: work arrived during teardown".

A **manual abort button** is provided: `hscc autodown cancel` sets a
`cancel_requested: true` flag in `autodown.json` that the teardown checks
between steps. Teardown is stepped and re-checks `cancel_requested` before each
stop, so an operator can interrupt a teardown mid-way. (§7)

---

## 7. Config file + CLI surface + defaults (C5)

### Config file: `~/.hscc/autodown.json`
A state + config JSON, read/written by a new `autodown.py` module (analogous
to how `lifecycle.py` owns `watchdog-block.json`). Schema:

```json
{
  "enabled": false,             // C5: OFF by default. New file starts disabled.
  "idle_minutes": 10,           // default 10; 0 = only via explicit wake/never auto
  "state": "up",                // one of: up | waking | down
  "last_activity_iso": null,    // advanced by every activity source (§1d)
  "down_since": null,
  "wake_source": null,
  "wake_at": null,
  "wake_trigger_text": null,      // first ~120 chars of the telegram message that woke us (§4)
  "cancel_requested": false,
  "reason": "",
  "force_armed": false,           // true only when armed with --force despite active cron jobs (§7)
  "force_armed_overrides": []     // names of the jobs overridden at arming time (§7)
}
```

Defaults: `enabled: false`, `idle_minutes: 10`, `state: "up"`. Absent file ⇒
treated as disabled (fail-closed, matching C5). The file is created when autodown
is first enabled.

### CLI surface (design only — following the `api` verb-group pattern)
Mirror exactly how `hscc api` is wired: `hscc.py:main()` dispatches the `api`
group (hscc.py:738-741) to `api_cli.cmd_api(argv[1:])`. Add `autodown` the same
way, dispatching to a new `hscc_daemon/autodown_cli.py:cmd_autodown(argv)`.

```
hscc autodown status                       # enabled? state? idle_minutes? last activity? down_since?
hscc autodown enable [--idle-minutes N]    # turn ON (default idle_minutes=10)
hscc autodown enable --force               # arm EVEN IF active Hermes cron jobs exist
hscc autodown disable                      # turn OFF; clears intentional block; does NOT restart serving
hscc autodown wake                         # force autoup now (also resets idle timer)
hscc autodown cancel                       # abort an in-progress teardown
hscc autodown --help                       # group help
```

Flag/group conventions kept consistent with existing HSCC style:
- verb groups dispatch a `cmd_<group>` function and exit non-zero on unknown
  subcommands (see `api_cli.cmd_api`, `api_cli.py:275-297`).
- `--json` flag (as `status`/`autoscale` do, `hscc.py:578`) for scripting.
- `--idle-minutes N` matches the `--port N` / `--bind` style of
  `api_cli._parse_start_flags` (`api_cli.py:70-96`).
- All mutating verbs are explicit; `status` is read-only.

`enable --idle-minutes N` persists to `autodown.json` and resets
`last_activity_iso = now` (start of the first window, §1e). `enable` when the
serving layer is currently down does NOT auto-restart it — it only arms the
automation; a separate `wake` (or an inbound event) brings it up. This keeps
`enable` non-acting.

**Cron-job guard (feat t_2b711a94 §7, refined t_c94f8b8c).** A scheduled
Hermes job and an idle-down cluster are in direct conflict only when the job
needs the GPU serving layer: autodown may power that layer down exactly when
the job is due. So before arming, `enable` enumerates ACTIVE Hermes jobs and
classifies each one by whether it needs the serving layer
(`autodown.list_active_cron_jobs` + `autodown.cron_job_is_cpu_only`,
`hscc_daemon/autodown.py`):

- **Model-requiring** active job (has a `model`, or `no_agent` is false/absent —
  i.e. it runs an agent) ⇒ `enable` **ABORTS** non-zero, printing the job's
  name, schedule, and next run and explaining that autodown may power the
  cluster down when it is due.
- **CPU-only** active job (`no_agent: true` AND `model: null` — a script
  watchdog that never touches the GPU) ⇒ does **NOT** abort. It succeeds
  identically whether or not the fleet is down, so it poses no conflict; it is
  mentioned as an informational note in the `enable` output and in `status` so
  the operator still knows it exists.
- A job whose nature **cannot be determined** (missing/ambiguous fields) is
  treated as model-requiring and aborts — never arm on an unverifiable signal.

It reads Hermes' OWN on-disk source of truth, `~/.hermes/cron/jobs.json`
(`autodown.list_active_cron_jobs`, `hscc_daemon/autodown.py`) — NOT the
`hermes cron list` CLI, which on this host resolves a profile that reports
"No scheduled jobs" even though jobs.json holds 2 active jobs (so the CLI is
not a reliable interface; jobs.json is what the scheduler actually fires
from). Only jobs with `enabled: true` count; a paused/disabled job cannot fire
and poses no conflict. **Fail-closed:** an unreadable OR absent jobs.json is
treated as "cannot determine" and `enable` also aborts with that reason —
autodown never arms on a signal it cannot verify.

`--force` overrides the guard (feat t_2b711a94 §7 / t_c94f8b8c): `enable
--force` arms anyway, prints each *model-requiring* job it overrode, and
records `force_armed: true` + `force_armed_overrides` (the overridden
model-requiring job names) in `autodown.json`. CPU-only jobs never appear in
the overrides record — they do not conflict, so there is nothing to override;
they are still noted informationally. `hscc autodown status` then surfaces
`force-armed: YES` and the overridden jobs, so the operator can always see WHY
autodown is armed despite scheduled model-requiring jobs. A later clean
`enable` (when no model-requiring jobs remain) clears the markers.

`disable` semantics (C2/C5): set `enabled: false`, set `state` to the current
reality, and **clear the `intentional` marker + `blocked` flag in the watchdog
block** so the watchdog resumes ordinary supervision. It does NOT run autoup —
if the layer is down and the operator wants it up, they run `hscc autodown wake`
(or the normal `hscc template apply` path). Document this in the CLI help.

### 7.1 Cron jobs and wake-on-cron — part 3 investigation (feat t_2b711a94)

The card's part 3 asked whether a scheduled job that fires while the fleet is
down should **wake the cluster** (and whether the job should be **queued and
executed once it is ready**). We investigated the observable signals on this
host on 2026-08-26 and reached the finding below; **no wake-on-cron path was
built**, and this subsection records why.

**Cron firings ARE observable by the CPU-side daemon.** The Hermes cron engine
writes one row per execution to `~/.hermes/cron/executions.db` (SQLite table
`executions`) with a `claimed_at` timestamp in local time. A daemon probe could
scan this exactly like `probe_telegram_activity` scans the gateway log
(§1d.2): remember the last-seen `claimed_at`/high-watermark, and treat any new
row for an ACTIVE job as a firing. This is additively written, so a byte-offset
style watermark works. So observability is NOT the obstacle — a firing is
readily detectable.

**The obstacle is that the jobs that would trigger it do not need the GPU
serving layer, and waking for them would be actively harmful.** The two ACTIVE
jobs on this host are both `no_agent: true` pure-script watchdogs with
`model: null`:

| job | schedule | kind | runs on |
|---|---|---|---|
| `hscc-dep-watcher` (bdf1af7e169e) | `0 8 * * *` (daily 08:00) | `no_agent`, `model:null` | CPU, ~1s, completed 10× |
| `hscc-escalate-watcher` (6407ea32e1dd) | `*/15 * * * *` (every 15 min) | `no_agent`, `model:null` | CPU, <1s, completed 990× |

Both are CPU-side Hermes cron script jobs that run **entirely independent of
the GPU serving layer** — they shell a script, post the result to Telegram, and
complete in well under a second. They do NOT "run against a dead cluster and
fail": they succeed identically whether or not autodown has powered the fleet
down. There is therefore **no lost job to queue** — the job already executed to
completion on the CPU side. "Queue the job so it executes once the fleet is
ready" would mean re-running an already-completed CPU-side job after a ~9-min
GPU model load, doubling it for zero benefit.

Worse, the wake itself would be counterproductive: waking the whole fleet (a
~9-minute model-load window) for a `no_agent` script job that needs no model is
pure GPU waste, and the every-15-min `hscc-escalate-watcher` would re-trigger
the wake faster than idle could ever count down — permanently defeating idle
teardown and running up a large power/GPU bill for nothing.

**Decision:** implement parts 1 and 2 (the abort guard + `--force` override)
and NOT part 3 (wake-on-cron). The conflict the feature set out to make
explicit is handled at the arming gate: the operator either disables/pauses the
offending scheduled jobs, or explicitly `--force`-arms knowing the cluster may
be down when they fire. Part 3's "wake + queue the job" was the riskiest, least
valuable piece — a reliable-but-harmful wake is covered by documenting the
limitation rather than half-building it (the card's own guidance: an unreliable
"we'll wake for crons" promise is worse than a documented limitation).

**The "future trigger keyed on model presence" is now implemented.** The earlier
TODO proposed revisiting wake-on-cron "keyed on `model` presence" if a
GPU-model job were ever added. That classification is exactly what this card
(t_c94f8b8c) implements — not for wake-on-cron, but for the abort guard:
`autodown.cron_job_is_cpu_only` classifies every active job as CPU-only
(`no_agent: true` AND `model: null`) or model-requiring (everything else), and
the guard aborts only for the model-requiring ones. So a GPU-model job is no
longer a hypothetical to key on later; it is detected at the arming gate today.

**What happens when a model-requiring cron fires while the fleet is down.**
In practice this **should not arise** from a cleanly-armed autodown: `enable`
aborts at arming time the moment a model-requiring job is active (unless the
operator explicitly `--force`-arms). The only path to a model-requiring job
running against a down fleet is an intentional `--force` arm — the operator has
explicitly accepted that the cluster may be down when it fires. Autodown does
not wake for it (there is no wake-on-cron path) and does not queue-and-replay
it (CPU-side jobs already completed; a model job lost to a down fleet is not
re-run — per the §1d.2 Telegram / §4 philosophy, re-executing an instruction
or job minutes later without confirmation is not safe). If such a job matters,
do not `--force`-arm over it.

**Ordering risk documented:** a GPU-model job that fires at 08:00 but only runs
at 08:09 after the model load — the ~9-min gap — would need the operator to
decide whether a late run is acceptable before relying on
wake-on-cron; that decision is deferred until such a job exists.

---

## 7.2 Dispatch guard — wait for cluster readiness before dispatching work
### (feat t_5d1118de — follow-up to the accepted finding)

**Finding (`t_ab177036`, do not re-investigate):** there is no supported seam
to HOLD a Hermes kanban dispatch. `pre_kanban_dispatch` was removed in 0.17
(`docs/hermes-017-new-features.md:20`); `kanban_task_claimed` fires AFTER
`claim_task` commits ready→running and is observer-only (return values
discarded, `kanban_db.py:164-186`); `pre_gateway_dispatch` gates inbound
MessageEvents only, not kanban dispatch; HSCC's fork patches do not touch the
dispatch gate. **HSCC does NOT patch Hermes.** So dispatch cannot be held.

**The consequence (live incident):** work dispatched while the fleet is
down/waking is destroyed. autodown tore down at 21:19, three cards were
created, the dispatcher spawned workers into a still-loading cluster, and 2 of
3 ended `blocked` with `worker exited cleanly (rc=0) without calling
kanban_complete` — a protocol violation caused purely by having no model to
talk to.

**The fix:** since dispatch cannot be held, prevent the window at the source
HSCC controls — its OWN dispatch entry point, `hscc project message dispatch`
(wired through `hscc_daemon/hscc.py:_handle_project:663` → `flightdeck.cli.main`
→ `flightdeck/commands/message.py:cmd_dispatch`). Before creating the card,
`cmd_dispatch` calls a cluster-readiness guard:

```
flightdeck message dispatch <project> <task> [--apply] [--no-wait]
```

Decision table (`flightdeck/commands/message.py:_ensure_cluster_ready`):

| autodown state | default (no flag) | `--no-wait` |
|---|---|---|
| disabled | proceed immediately (never interfere) | proceed |
| `up` | proceed immediately (cluster already serving) | proceed |
| `down` / `waking` / `error` | wake (`autodown.autoup()`) + **wait for readiness**, then create card | create immediately (operator accepts the risk) |
| unknown | refuse (exit non-zero; cluster unverifiable) | proceed |
| **wake fails** | **NO card created**, exit non-zero, clear error | n/a |

Mechanics:
- The guard reads `~/.hscc/autodown.json` via `autodown.load_config()` (the
  single source of truth), and triggers the wake via `autodown.autoup()` — the
  SAME function the daemon's wake path and `hscc autodown wake` use. Reusing
  the library (not re-implementing, not shelling out) matches the house rule
  that the wake logic lives in exactly one place.
- `autoup()` starts the whole fleet and polls each unit's port until healthy
  or the load-grace deadline (`VLLM_LOAD_GRACE_MINUTES`, default 20 min) — this
  IS the "wait for readiness" the card wants, bounded by the same window.
- Progress is printed to stdout so the operator sees WHY dispatch is pausing —
  a silent multi-minute pause is explicitly not acceptable. Lines: "waking the
  serving layer before dispatch (load-grace window Nm, please wait)..." then
  "serving layer is UP — proceeding with dispatch."
- Only the `--apply` path touches the cluster. A dry-run previews the plan and
  never wakes anything — dry-run mutates nothing.
- The wake limit: `autoup()` returns `up` on readiness; any other result
  (`start-failed` / `not-ready` / `no-units` / `busy` / `already-waking`) is a
  failure. The guard reads the failure reason autoup persisted to
  `autodown.json.reason` (`_record_wake_failure`), reports it, and exits
  non-zero **without creating the card** — so the work is not lost to a
  protocol violation (it was never created). The operator fixes and retries, or
  passes `--no-wait`.

**Limitation (documented honestly):** this guard covers ONLY cards created via
`hscc project message dispatch` — HSCC's own dispatch entry point. It does NOT
cover cards created by other paths that bypass it: Hermes-internal
decomposition (`delegate_task` → `kanban_create`), cron jobs, direct `kanban
create`, `kanban_submit_review`, or an orchestrator's own fan-out. Those paths
are not guarded here and can still create a card into a down/waking fleet.

**Recovery when it does happen (`hscc kanban blocked --recover`).** The
dispatcher's circuit-breaker auto-block sets `block_kind` NULL with no comment,
and Hermes' `reclaim_task` REFUSES blocked cards outright (early guard,
`kanban_db.py:4491-4493` returns False — the reason two cards had to be
recovered by direct DB write the night of the incident). `hscc kanban blocked`
(owned by `hscc_daemon/kanban_blocked.py`) SHOWS why a card is blocked and
`--recover` un-sticks it. So if a card is ever dispatched into a not-ready
fleet and ends up circuit-breaker-blocked with no model to talk to, the
recovery is: `hscc kanban blocked --recover <id>` (see `hscc_daemon/kanban_blocked.py`
and the `kanban blocked` design doc). Record this so the next operator is never
stuck re-writing the DB by hand.

---

## 8. Failure modes + the SAFE state for each

Principle: when in doubt, **favor UP and supervised over down and unsupervised**.
Autodown is a power optimization — it must never make the cluster less reliable
than leaving it on. Every recovery path ends in either (a) serving restored +
watchdog supervising, or (b) a clear, loudly-notified blocked state requiring a
human, never a silent half-state.

| Failure | Detection | SAFE state / recovery |
|---|---|---|
| **Wake fails** (autoup can't bring units up, or readiness timeout expires) | autoup watches readiness; timeout = `VLLM_LOAD_GRACE_MINUTES` | Leave `state:"up-or-error"`, log + notify critical. Do NOT re-latch the block (that would hide it). Keep retrying on each new wake event / each cycle, with backoff. SAFE = up-when-possible with loud alerts. If a unit genuinely cannot come up, the normal watchdog logic takes over once `intentional` is cleared — but autoup must NOT clear `intentional` until at least one serving unit is confirmed healthy (clearing it early would let the breaker latch while we're mid-load). So: clear the intentional block only after the FIRST unit reports healthy. |
| **Wake fails — empty plan** (autoup can determine NO units to start; serving.json missing/corrupt) | `_build_wake_plan` returns `[]` ⇒ result `no-units` | ZERO units started, so there is no "first healthy" to wait for and nothing is mid-load. Leave the block UNLATCHED (`state:"error"`, NOT `"up"` since nothing started, and NOT `"down"` which would re-latch via cycle()'s self-heal), clear `intentional` so the watchdog resumes and can heal whatever is actually there, log + notify critical. SAFE = supervised, not a silent half-state. Recovery: fix serving.json, then a fresh wake (event, `hscc autodown wake`, or a later cycle once units are determinable) brings it up. |
| **Teardown fails** (a `sparkrun stop` errors) | per-stop return code | Abort remaining stops. Clear `intentional` marker but preserve `blocked` state as watchdog would (leave the block as a normal failure so the watchdog resumes + can heal whatever partial state remains). Set `autodown.json.state = "up"` (reality: not fully down). Notify. SAFE = watchdog resumes supervision over whatever's left; nothing silently half-down. |
| **Daemon dies while down** (daemon process exits while `state:"down"`) | daemon restart on next boot/launchd | On daemon startup (`run_daemon_loop` / autodown.cycle first run), read `autodown.json`. If `state == "down"`, the cluster is intentionally down but NOTHING supervises it now. **SAFE = restore the block + resume autodown supervision.** The new daemon re-writes the watchdog block (`blocked:true, intentional:"autodown"`) so the watchdog (now running under the new daemon) doesn't fight it, and resumes the idle/wake monitoring. The serving layer stays down (it was the operator's intent), guarded. Autoup still works on the next event. |
| **Daemon dies while waking** | same as above | On startup, if `state == "waking"`: the autoup may or may not have completed. Resume: re-run autoup (idempotent — starting `--ensure` units that exist is a no-op). SAFE = finish the wake. |
| **Watchdog-block file corrupt/missing while down** | `load_watchdog_block` returns default (`blocked:false`) | autodown.cycle, when `state=="down"` and block lacks `intentional`, RE-asserts the block each cycle. SAFE = the intentional marker is self-healing (re-written on every cycle while down), so a lost/deleted block file cannot cause the watchdog to resurrect a deliberately-down layer. |
| **Power/thermal edge** (GPU thermal trip, node reboot, cluster loss while down) | daemon's DGX/gateway health checks | These are failures of machinery, not of autodown. The layer is down by intent; when a wake comes, autoup tries to bring it up; if the node is genuinely gone, autoup's readiness timeout fires → "wake fails" row. SAFE = same as wake-fails: loud, supervised, not silently half-up. |
| **Config corrupt** (`autodown.json` unparseable) | read returns parse error | Fail-closed to DISABLED + report (C5). Never act on corrupt config. SAFE = no automation; operator fixes file or re-enables. |
| **Double teardown** (two daemon instances, or operator + autodown both) | `state` guard + `intentional` check | Teardown is gated on `state != "down"` and a lockfile (atomic `O_EXCL` on `~/.hscc/autodown.lock`) so only one teardown runs. Second requester blocks/returns. SAFE = exactly one teardown at a time. |
| **Keepalive/teardown overlap** (C4 reversed) | ~~`_teardown_locked` step 1b abort guard~~ **REMOVED** | Fleet down (`serving.fleet_down_cmd()`, `sparkrun stop --all`) intentionally stops keepalive units too (operator decision, C4 reversed). The old guard aborted whenever a keepalive node was in the teardown set — with the whole-fleet `--all` stop that would abort EVERY teardown, so it is gone. The `keepalive_ok` INTERLOCK (sick keepalive unit ⇒ abort teardown) is kept as the "don't shut down mid-problem" signal. SAFE = nothing is exempt, so the layer cleanly and wholly goes down (and fully back up on wake). |

**Overall SAFE state for the system:** idle→down is only ever allowed while
the interlock holds AND sets an intentional block; any deviation routes to
supervised healthy or a loud human-visible block. The cluster self-describes its
state in `autodown.json` at all times, so `hscc autodown status` is always an
honest report of the current reality.

---

## 9. Phase breakdown into small, single-file implementation cards

Each phase is one file + its tests, implementable from this doc alone.

### Phase 1 — config + core state module
**File:** `hscc_daemon/autodown.py` (new)
- `load_config()`, `save_config()` for `~/.hscc/autodown.json`
  (fail-closed default disabled; schema §7).
- `record_activity(source)` — advance `last_activity_iso` (+ optional
  `source`); the single choke point every activity source (§1d) calls.
- `classify(...)` — unit classification table for watchdog coordination (§5).
- `_has_active_work(kanban_db)` — kanban idle predicate (§1a).
- Tests: `hscc_daemon/tests/test_autodown.py` — config read/write, fail-closed
  on absent/corrupt config, `record_activity` timestamp advance, kanban
  predicate for each status.

### Phase 2 — the idle timer thread in the daemon loop
**File:** `hscc_daemon/daemon_ops.py`
- Add `run_autodown_loop()` (30s cadence) to `run_daemon_loop()` (daemon_ops.py:167);
  short-circuits when `enabled: false`; calls `autodown.cycle()`.
- SDG integration via the same `stop_event`.
- Tests: loop starts/stops, off-by-default short-circuit, cycle called on
  cadence (monkeypatched cycle counter).

### Phase 3 — idle evaluation + safety interlocks
**File:** `hscc_daemon/autodown.py` (additions)
- `cycle()` — the decision function: read config → if disabled, return → if
  down/waking, check wake sources (§4) → if up, evaluate idle predicate §1 →
  if idle-and-elapsed, run teardown().
- Full interlock evaluation (§6): kanban DB query + agents.json + window.
- Tests: each interlock independently blocks teardown; conjunction semantics.

### Phase 4 — teardown sequence + watchdog coordination
**File:** `hscc_daemon/autodown.py` (additions)
- `teardown()` — interlock re-check, write watchdog block (`intentional:
  "autodown"`), stop non-keepalive units (workers then orchestrator via
  `sparkrun stop`), verify down, write `state:"down"`, notify, handle
  cancel/failure per §3/§8.
- Tests: ordering (workers before orchestrator), keepalive exemption,
  block written before any stop, cancel flag honored between steps, failed-stop
  rollback.

### Phase 5 — wake sequence + first-message handling
**File:** `hscc_daemon/autodown.py` (additions)
- `autoup()` — waking→start units (orchestrator via `VLLM_START_CMD`, workers) →
  readiness wait via `health.http_check` → clear watchdog block on first healthy
  → flush state:"up" → notify.
- `_handle_http_wake(`202`+`retry_after`)`, `_notify_waking()` for Telegram.
- Tests: idempotent autoup, block cleared only after healthy, waking→up.

### Phase 6 — activity-source probe for inbound signals
**File:** `hscc_daemon/autodown.py` or `hscc-api/api_server.py` (small touch)
- Wire the four activity sources (§1d): API request stamp, Telegram MCP stamp,
  kanban create/ready detection, CLI wake.
- HTTP: `api_server` writes `~/.hscc/activity.json` (event-driven, OUTSIDE
  `~/.hscc/state/`) on each authenticated request (one extra write per request,
  cheap). See the §1d.1 note in this doc.
- Tests: each source advances `last_activity_iso` / triggers autoup when down.

### Phase 7 — CLI verb group `hscc autodown`
**File:** `hscc_daemon/autodown_cli.py` (new) + `hscc_daemon/hscc.py` (wiring)
- `cmd_autodown(argv)` handling `status|enable|disable|wake|cancel` (+ `--json`,
  `--idle-minutes N`), per §7.
- `hscc.py:main()` adds an `autodown` branch dispatching to `cmd_autodown`
  (mirror the `api` block, hscc.py:738-741), plus help text entries.
- Tests: `hscc_daemon/tests/test_autodown_cli.py` — each subcommand's
  side-effects on `autodown.json` + watchdog block + serving state, no-subcommand
  help, unknown-subcommand exit non-zero, `disable` semantics (§7).

### Phase 8 — daemon-start recovery (daemon died while down)
**File:** `hscc_daemon/daemon_ops.py` (tiny) + `hscc_daemon/autodown.py`
- On `run_daemon_loop` startup: if `autodown.json.state == "down"`, re-assert
  the intentional block + resume monitoring (§8, "daemon dies while down").
- Tests: startup with `state:"down"` re-writes block; `state:"waking"` resumes
  autoup; `state:"up"` unchanged.

---

## Ordering / dependencies between phases
1 → 2 → 3 → 4 → 5 sequentially (each builds on the previous). Phase 6 is
independent-ish (can land after 5, or parallel). Phase 7 depends only on 1+4+5
(the CLI calls into config/teardown/autoup). Phase 8 depends on 2+4. Suggested
dispatch: 1,2,3,4,5 then 6 and 7 (parallelizable), then 8.

## Things intentionally NOT in scope
- ~~Auto-tearing down keepalive units~~ now IN scope (C4 reversed, operator
  decision) — autodown powers the whole serving layer down incl. keepalive.
- Tearing down the CPU-side daemon/gateway/API/Telegram processes (C1 — they
  must stay up to wake).
- Any model/agent-in-the-loop wake decision (C1 — daemon-only).
- Autoscale worker count changes (separate feature; `autoscale.py` exists but
  is engine-only today) — autodown is all-or-nothing for the serving layer.

---

## Verify behaviour across the intentional-autodown lifecycle (up / down / waking)

`hscc verify` (hscc_daemon/verify.py) must honestly report three distinguishable
readings — "off on purpose" (down), "coming back" (waking), and "actually
broken" (real fault). The SINGLE mechanism is `autodown.classify()` (autodown.py:643)
extended with a `waking` verdict, plus the breadth-of-window predicate
`autodown.intentional_window()` (autodown.py:704) which is True for both
`expected_down` and `waking`. The verify reader (`verify._intentional_window_verdict`)
and the stream-tagging writers (`health._intentional_window`) both consult the
SAME predicate on the SAME `classify()` table — no parallel representation.

A stream is excused by `check_daemon_streams` ONLY when (a) the window verdict is
`expected_down` or `waking` AND (b) the stream itself carries
`intentional == "autodown"`. Either missing ⇒ genuine failure. So a real fault
in an untagged stream still fails even during the window; and a wake that
fails leaves `waking` (→ `error`, `should_be_up`) so verify stops excusing it.

### Verify check matrix (run_all)
| check | up | down | waking |
|-------|----|------|--------|
| plugins | independent — pass | pass | pass |
| multiplex | independent — pass | pass | pass |
| config_wiring | independent — pass | pass | pass |
| daemon_streams | pass iff every stream ok/current | PASS (excuses tagged serving streams) | PASS (excuses tagged serving streams) |
| proxy | pass iff models present | PASS (excused, "intentionally down") | PASS (excused, "waking from autodown") |

### Stream writer matrix (`state/*.json`)
| stream | writer | up | down | waking |
|--------|--------|----|------|--------|
| watchdog | lifecycle.PipelineWatchdog (:249/:274/:302) | ok=True healthy | ok=False + `intentional:"autodown"` (block latched) | ok=False + `intentional:"autodown"` (block stays latched mid-wake) |
| dgx | health.check_dgx (:274) | ok from SSH+vLLM | ok=False + marker | ok=False + marker |
| gateway | health.check_gateway (:397) | ok from job+vLLM+mux | ok=False + marker | ok=False + marker |
| proxy | health.check_proxy (:483) | ok from port probe | ok=False + marker | ok=False + marker |
| workers | health.check_workers (:1210) | ok based on unit supervision | ok=True + marker (defer, no relaunch) | ok=True + marker (defer, no relaunch) |
| heartbeat | health.check_heartbeat (:654) | always ok=True | ok=True | ok=True |
| local | health.check_local (:573) | ok from infra, not serving | same (infra, not serving) | same |
| nas | health.check_nas (:751) | ok from mount, not serving | same | same |
| idle | health.check_idle (:919) | ok from idle counts, not serving | same | same |

Only the five serving streams (watchdog, dgx, gateway, proxy, workers) legitimately
go unhealthy during a transition and are excused in the window. The infra
streams (heartbeat/local/nas/idle) are independent of the serving layer and are
NOT excused — a genuine infra fault during a wake still fails, as intended
(negative control).
