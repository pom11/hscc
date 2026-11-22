# Agent-surface coverage matrix: CLI × agent tool × slash command × API route × skill

**Card:** t_4c12ff7f (2.17.4 workstream, card 1 — gates the tools/skills/profile/doctor/QA cards)
**Measured:** 2026-11-23 at `origin/main` = `cf6ef9c4` (worktree `wt/t_4c12ff7f`)
**Posture:** read-only. No fleet mutation, no daemon/API write call, no `sparkrun`
verb beyond what a read implies. Every number below is a grep/table count against
this sha, with the file:line that produces it.
**Verdict legend**

| mark | meaning |
|---|---|
| ✅ | exists on that surface and agrees with the CLI verb |
| ➖ | not applicable to that surface (it *is* that surface) |
| ✖ MISSING | no surface cell exists; nothing routes to it |
| ⚠ STALE | a cell exists but says something the code no longer does |
| 🔒 INTENTIONALLY CLI-ONLY | deliberate; the reason column carries the decision |

---

## 0. Headline

Five surfaces, one product, and they do not agree:

1. **The agent toolset is the best-covered surface and nobody documented it.**
   `hscc-cluster` registers **26 tools** (counted from `hscc-cluster/__init__.py:31-131`:
   6 read + 5 ops + 3 debug + 4 heal + 4 fleet + 3 template + 1 orch). The bundled
   `hscc-cluster` skill documents **none** of the four newest ones (`node_pause`,
   `node_resume`, `node_paused`, `orch_message`) — measured `grep -rl node_pause install/hscc-skills/hscc-cluster/`
   → 0 files.
2. **The bundled skills document almost none of the current CLI.** Of 49 verb
   strings tested against `install/hscc-skills/`, **7** hit anything (§7 table).
   `autodown`, `merge-queue`, `orch inject`, `node pause`, `main`, `cards monitor`,
   `heal-*`, `template *`, `cluster jobs|info|up|down|history` are **absent from every
   bundled skill**, including the ones bootstrap installs on a fresh host.
3. **The operator's "install skips existing skills" hypothesis is only partly right,
   and the bigger half is different.** `install-skills` *does* update in place
   (content-hash per file, `hscc-skills/hscc.py:120-151`), and the default-home copies
   of all 11 bundled skills are byte-identical to this sha (§8). The real defects are:
   (a) **the bundled source itself is stale** — the installed copy matches a stale
   template exactly; (b) **bootstrap only ever writes the *default* home**
   (`HERMES_HOME` default `~/.hermes`, `hscc-skills/hscc.py:31`), while every worker
   and orchestrator reads its skills from `~/.hermes/profiles/<profile>/skills`, which
   bootstrap never touches; and (c) `hscc-master-knowledge` (the Jul-1 dated dir the
   operator measured) is **not in `BUNDLED_SKILLS` at all** — it is an orphan no
   installer updates or removes.
4. **Two help surfaces, one stale.** `hscc kanban --help` lists all 7 dispatched
   subcommands; `hscc help kanban` lists 3 and omits `pause`, `resume`, `paused`,
   `enforce` (§9 F3).
5. **One second implementation of fleet-down.** `/cluster-down` (slash) builds its own
   parallel `sparkrun stop` per node (`hscc-commands/__init__.py:467-510`) while
   `hscc cluster down` and `POST /v1/cluster/down` both go through
   `hscc-cluster/hscc.py:621 cmd_cluster_down` → `serving.fleet_down_cmd()`. Two
   answers to one question (§9 F5).

**The card's own "candidates already named" list is measurably wrong about three
verbs** — they are *not* CLI-only today: `autodown status/enable/disable/wake/cancel`
have full route parity, and `verify` has both a route and a tool. §3 re-decides each
named verb on the code, not on the premise.

---

## 1. The five surfaces and where each list comes from

Re-measure any row with the command in its own line; nothing here is hand-counted
from memory.

| surface | count | authoritative list | how to re-count |
|---|---|---|---|
| `hscc` CLI top-level verbs | **33** | `hscc_daemon/hscc.py:698-705` `TOP_LEVEL_COMMANDS` | `python -c` over that tuple, or `hscc nosuchverb` |
| CLI group subcommands | 10 groups, 48 leaves | per-group tables: `_CLUSTER_SUBCOMMAND_HELP` (hscc.py:838) + `CLUSTER_SUBCOMMANDS` (hscc.py:669) = 9, `TEMPLATE_SUBCOMMANDS` (hscc.py:671) = 5, `autodown_cli.py:28` = 5, `kanban_cli.py:34` + `blocked` in `kanban_blocked.py` = 7, `api_cli.py:43` = 5, `main_cli.py:35` = 3 (+4 `wakeups` sub-subcommands), `merge_queue_cli.py:77` = 8, `_NODE_SUBCOMMAND_HELP` (hscc.py:1345) = 3, `cards_live.py` `monitor` = 1, `hscc-cluster/hscc.py:1174-1176` `inject`+`waker` = 2 | each module's `VALID_SUBCOMMANDS` / help table |
| agent tools (`hscc-cluster`) | **26** | `hscc-cluster/__init__.py:31-131` (7 lists) | `grep -c '(", S\.' …` or count the tuples |
| slash commands (`hscc-commands`) | **12** | `hscc-commands/__init__.py:852-913` `register_command` calls | `grep -c register_command` |
| API routes (`hscc-api`) | **89 HTTP + 1 WS** | `api_server.py:608 ROUTES` + `routes_cluster.load():1725-1736` (12 in a loop) + `routes_ws.py:813` | regex-sweep `(METHOD, re.compile("…"))` triples across `hscc-api/*.py` incl. the `load()` table, then dedupe on `(method, path)` — 89 unique pairs, 56 GET / 32 POST / 1 PATCH |
| bundled skills | **12 dirs / 13 names** | `hscc-skills/hscc.py:59-83` `BUNDLED_SKILLS` | `ls install/hscc-skills` |

`hscc project <verb>` is a 6th, *delegated* surface: it forwards to the flightdeck CLI
(`hscc-project/flightdeck/commands/`, ~25 commands, each with its own flags). It is
inventoried at family level only in this card — see §10 LEFT-1.

---

## 2. Coverage matrix

Row = one CLI verb (or group leaf). **Tool** = the `hscc-cluster` agent tool that
performs the same operation. **Slash** = `hscc-commands` verb. **Route** = `hscc-api`
endpoint. **Skill** = a *bundled* skill (`install/hscc-skills/`) that documents the
verb — this is the column that is almost always empty.

### 2.1 Daemon / service verbs

| CLI verb | agent tool | slash | API route | bundled skill | verdict |
|---|---|---|---|---|---|
| `hscc start` | ✖ | ✖ | ✖ | ✖ | 🔒 service-manager: the API cannot start the process that serves it |
| `hscc stop` | ✖ | ✖ | ✖ | ✖ | 🔒 same |
| `hscc status` | ✖ | ➖ (`/status` is the *fleet* dashboard, not the daemon's) | ✅ `GET /v1/daemon/status` | ✅ `hscc` | tool ✖ is the only real gap; covered by the doctor card, not a new tool |
| `hscc check [stream]` / `check --repo` | ✖ | ✖ | ✖ | ✖ | 🔒 reads one local checkout's hook posture — meaningless off the host |
| `hscc watch [stream]` | ✖ | ✖ | ✖ | ✖ | 🔒 live-tail TTY |
| `hscc triggers` | ✖ | ✖ | ✅ `GET /v1/triggers`, `POST /v1/triggers/run` | ✖ MISSING | route parity exists; skill MISSING |
| `hscc notify` / `list` / `ack` | ✖ | ✖ | ✅ `POST /v1/alerts`, `GET /v1/alerts`, `POST /v1/alerts/{id}/ack` | ✖ MISSING | full route parity, zero docs. Tool: **decision D7** |
| `hscc log` | ✖ | ✖ | ✅ `GET /v1/logs` (bounded, redacted) | ✖ MISSING | skill MISSING |
| `hscc plist` | ✖ | ✖ | ✖ | ✖ | 🔒 prints a unit file — a template, not a cluster fact |
| `hscc install` / `uninstall` | ✖ | ✖ | ✖ | ✖ | 🔒 writes launchd units — never agent-reachable |
| `hscc bootstrap [flags]` | ✖ | ✖ | ✖ (**name trap**: `/v1/projects/new*` is *project* bootstrap) | ✖ MISSING | 🔒 installer rewrites `~/.hermes` + state + daemon — never agent-reachable. Needs a **skill** sentence, not a tool |
| `hscc update --check` | ✖ | ✖ | ✖ | ✖ | 🔒 read-only GitHub release probe; `hscc status` already surfaces the cached verdict |
| `hscc verify [--json] [--chat]` | ✅ `cluster_verify` | ✖ | ✅ `GET /v1/verify` | ✅ `hscc` | **card premise falsified** — already 3-surface. Keep parity (D1) |
| `hscc stats [days]` | ✅ `fleet_stats` | ✖ | ✅ `GET /v1/fleet/stats` | ✖ MISSING | skill MISSING |
| `hscc throughput` | ✅ `cluster_throughput` | ✖ | ✅ `GET /v1/fleet/throughput` | ➖ (`devops` mentions the word) | skill MISSING as a verb |
| `hscc autoscale` | ✅ `autoscale_advice` | ✖ | ✅ `GET /v1/autoscale` | ✖ MISSING | skill MISSING |
| `hscc escalate` | ✖ | ✖ | ✅ `GET /v1/escalate` **and** `POST /v1/escalate` (mutating) | ✅ `hscc` | **finding F6**: a mutating route with no tool, no confirm gate named in the verb, and a read-only CLI twin |
| `hscc heal-status` | ✖ | ✖ (`/heal` is a *different* operation) | ✖ | ✖ | **F7 naming trap** + tool MISSING (D4) |
| `hscc heal-clear <node>` | ✖ | ✖ | ✖ | ✖ | same as above; the breaker's own alert text names this verb |
| `hscc start-daemon` | ➖ | ✖ | ✖ | ✖ | 🔒 launchd `ProgramArguments` target, documented in `hscc help advanced` |
| `hscc help [topic]`, `advanced` | ➖ | ➖ | ➖ | ➖ | help topic, not dispatchable (`advanced` exits 1 without a help token) |

### 2.2 `cluster` group

| CLI verb | agent tool | slash | API route | bundled skill | verdict |
|---|---|---|---|---|---|
| `cluster status` | ✅ `cluster_status` | ✅ `/cluster` | ✅ `GET /v1/cluster/status` (pause-overlay aware, `routes_cluster.py:428-435`) | ✅ `hscc`,`hscc-cluster`,`hscc-model-onboard`,`devops` | **the only fully-covered verb in the repo** |
| `cluster hosts` | ✅ `discovery_status` | ✅ inside `/cluster` | ✅ `GET /v1/cluster/hosts` | ✅ `hscc-cluster` | ok |
| `cluster monitor` | ✖ | ✅ inside `/cluster` (`sparkrun cluster monitor --simple --json`, `cmdlib.py:149`) | ✅ `GET /v1/cluster/monitor` | ✅ `hscc-cluster` | tool ✖ = decision D2 (the card named it a CLI-only candidate; the **route already exists**, so the only live question is the tool) |
| `cluster jobs` | ✖ | ✖ | ✅ `GET /v1/cluster/jobs` | ✖ MISSING | skill MISSING |
| `cluster info` | ✖ | ✖ | ✅ `GET /v1/cluster/info` | ✖ MISSING | skill MISSING |
| `cluster stop <cid>` | ✅ `stop_model` (per-model granularity, not per-container) | ✅ `/cluster-down` (coarser) | ✅ `POST /v1/cluster/stop` | ✖ MISSING | granularity mismatch is fine; skill MISSING |
| `cluster up [--dry-run]` | ✖ | ✅ `/workers-up` (keepalive-only subset) | ✅ `POST /v1/cluster/up` (confirm-gated) | ✖ MISSING | **D3**: no tool by design? decide |
| `cluster down [--dry-run]` | ✖ | ⚠ `/cluster-down` — **second implementation** (F5) | ✅ `POST /v1/cluster/down` (confirm-gated) | ✖ MISSING | F5 |
| `cluster history [--window\|--tier]` | ✖ | ✖ | ✅ `GET /v1/cluster/history` | ✖ MISSING | skill MISSING. The CLI is the *only* place the tiers are explained |

### 2.3 `template` group

| CLI verb | agent tool | slash | API route | bundled skill | verdict |
|---|---|---|---|---|---|
| `template list` | ✅ `list_templates` | ✅ `/template list` | ✅ `GET /v1/template/list` | ✖ MISSING | skill MISSING |
| `template status` | ✖ | ⚠ slash `args_hint` = `list\|preview <name>\|apply <name> [confirm]` — **no `status`, no `validate`** | ✅ `GET /v1/template/status` | ✖ MISSING | ⚠ STALE slash (F4) |
| `template preview <name>` | ✅ `preview_template` | ✅ | ✅ `GET /v1/template/preview/{name}` | ✖ MISSING | skill MISSING |
| `template validate <name>` | ✖ | ⚠ absent from `args_hint` (F4) | ✖ **MISSING — the only template verb with no route** | ✖ MISSING | ✖ on 3 of 4 surfaces. Not a decision, an omission: validate is the *safe* verb a dashboard would want (D5) |
| `template apply <name> --confirm` | ✅ `apply_template` (HIGH-RISK, confirm) | ✅ `/template apply <name> [confirm]` | ✅ `POST /v1/template/apply` | ✖ MISSING | 4-surface parity on the **highest-risk verb in the fleet** — flag as risk R1, not a gap |

### 2.4 `autodown` group (idle serving-layer down/up)

| CLI verb | agent tool | slash | API route | bundled skill | verdict |
|---|---|---|---|---|---|
| `autodown status` | ✖ | ✖ | ✅ `GET /v1/autodown/status` | ✖ MISSING | card named it CLI-only: **false today**. Skill MISSING is the gap |
| `autodown enable [--idle-minutes] [--force]` | ✖ | ✖ | ✅ `POST /v1/autodown/enable` | ✖ MISSING | 🔒 for the *tool* (fleet-power policy is daemon/operator scope, not a per-turn agent call); keep route |
| `autodown disable` | ✖ | ✖ | ✅ `POST /v1/autodown/disable` | ✖ MISSING | same |
| `autodown wake` / `cancel` | ✖ | ✖ | ✅ `POST /v1/autodown/wake`, `/cancel` | ✖ MISSING | same |

No `hscc-cluster` tool touches `autodown.json`; no slash command either. The daemon
(`autodown.py:34`) and the CLI are the only writers, and the routes are the only
other door. That is a coherent design — it just is not written down anywhere.

### 2.5 `kanban` / `cards` / `merge-queue` groups

| CLI verb | agent tool | slash | API route | bundled skill | verdict |
|---|---|---|---|---|---|
| `kanban blocked [--recover <id>]` | ✖ | ✖ | ✅ `GET /v1/kanban/blocked`, `POST /v1/kanban/blocked/{id}/recover` | ✖ MISSING | skill MISSING |
| `kanban stale [--archive <id>]` | ✖ | ✖ | ✅ `GET /v1/kanban/stale` | ✖ MISSING | skill MISSING |
| `kanban monitor [--live]` | ✖ | ✖ | ✅ `GET /v1/kanban/running` (same `kanban_live` backing) | ✖ MISSING | route parity exists under a **different name** — F8 |
| `kanban pause <board>…` | ✖ | ✖ | ✖ | ✖ | 🔒 board brake. Never agent-reachable: a worker that can pause its own board can pause the fleet's work (D6) |
| `kanban resume` / `paused` / `enforce` | ✖ | ✖ | ✖ | ✖ | 🔒 same; `enforce` is daemon-facing and is in **neither** help verb list except `kanban --help` (F3) |
| `kanban` help surface | — | — | — | — | ⚠ STALE: `hscc help kanban` lists 3 of 7 dispatched verbs (F3) |
| `cards monitor [--live] [--tools]` | ✖ | ✖ | ✖ (`GET /v1/activity/feed` is a different, non-transcript view) | ✖ MISSING | 🔒 interactive TTY panel (keys tab/enter/t/p/q) — no surface can carry it |
| `merge-queue status` | ✖ | ✖ | ✅ `GET /v1/merge-queue` | ✖ MISSING | card named it CLI-only: **false**, the route exists |
| `merge-queue list` | ✖ | ✖ | ✅ same route (rows) | ✖ MISSING | skill MISSING |
| `merge-queue add / remove / pause / resume / enable / disable` | ✖ | ✖ | ✖ **no mutation route** | ✖ MISSING | 🔒 correct to keep closed: the queue force-pushes and squash-merges. An agent-writable merge door is a self-merge path (D8) |

### 2.6 `orch` / `main` / `node` groups

| CLI verb | agent tool | slash | API route | bundled skill | verdict |
|---|---|---|---|---|---|
| `orch inject <profile> "<msg>" [--mode] [--pane] [--dry-run]` | ✅ adjacent-only: `orch_message` (queue-only, refuses non-`*-orch`) | ⚠ `/orch-restart` shares the `orch-` prefix but **restarts the orchestrator vLLM** | ⚠ `POST /v1/orchestrator/chat` is a *different* capability (a chat turn with id + stop) | ✖ MISSING | 🔒 CLI-only is right for inject (it steers a live tmux pane); the agent path is `orch_message`. **F7 naming trap**: four different things named "orch" |
| `orch waker status\|enable\|disable` | ✖ | ✖ | ✖ | ✖ MISSING (the *curated profile* skill has `references/orch-waker-wake-dedupe.md`; the bundled copy does not) | 🔒 for enable/disable (it changes when the operator wakes); `status` is a legit read candidate — D9 |
| `main start` | ✖ | ✖ | ✖ | ✖ MISSING | 🔒 spawns a tmux pane running the operator's orchestrator — an agent must not be able to spawn or re-attach it |
| `main status [--json]` | ✖ | ✖ | ✖ | ✖ MISSING | card named it CLI-only; **it is a pure read** — recommend route for the dashboard (D9), not a tool |
| `main wakeups list\|set\|enable\|disable` | ✖ | ✖ | ✖ | ✖ MISSING | 🔒 rewrites the daemon's wake schedule (`~/.hscc/main_wakeups.json`) |
| `node pause <host> [--force] [--yes] [--dry-run]` | ✅ `node_pause` (confirm-gated, returns plan) | ✖ | ✖ | ✖ MISSING | tool parity ✅; route MISSING — decide D10. Skill MISSING |
| `node resume <host> [--yes] [--dry-run]` | ✅ `node_resume` | ✖ | ✖ | ✖ MISSING | same |
| `node paused [--json]` | ✅ `node_paused` | ✖ | ⚠ only *indirectly*: `GET /v1/cluster/status` projects the overlay (`routes_cluster.py:428`), but no endpoint returns the pause records | ✖ MISSING | **F9**: the iOS widget / web dashboard can show "PAUSED" but cannot show *why, since when, until when* |

### 2.7 `api` / `profiles` / `project`

| CLI verb | agent tool | slash | API route | bundled skill | verdict |
|---|---|---|---|---|---|
| `api start\|stop\|status` | ✖ | ✖ | ➖ (it *is* the server) | ✖ MISSING | 🔒 self-hosting: cannot stop itself over the transport it serves |
| `api install-service\|uninstall-service` | ✖ | ✖ | ✖ | ✖ MISSING | 🔒 launchd unit writes (`api_launchd.py`) |
| `hscc profiles` (running card counts per profile) | ✖ | ✖ | ⚠ **two** routes answer it: `GET /v1/profiles` (`routes_ops`) and `GET /v1/profiles/list` (`routes_profiles`) | ✖ MISSING | **F2 second source of truth** — pick one canonical, alias the other |
| `hscc project <verb>` (~25 flightdeck commands) | ✖ (kanban tools are native Hermes) | ✖ | partial: 16 read + 8 mutating routes in `routes_project.py` / `routes_actions.py` | ✖ MISSING | family-level only. LEFT-1: per-verb map of flightdeck → route |

---

## 3. The card's named CLI-only candidates, re-decided per verb

The body named seven candidates. Measured against the code, three already have API
parity and two of those already have a tool. Deciding on the code:

| verb (as named in the card) | measured state | decision |
|---|---|---|
| `autodown status` | route ✅ | **not CLI-only.** Keep the read route; add skill docs |
| `autodown disable` | route ✅ | keep route; 🔒 **no tool** (fleet-power policy) |
| `kanban pause/resume/paused` | no route, no tool | 🔒 confirm CLI-only — a board brake an agent can pull is not a brake |
| `merge-queue status` | route ✅ | **not CLI-only** for the read; 🔒 mutations stay closed (D8) |
| `doctor` / `verify` | `verify` = route + tool ✅; `doctor` is `hscc project doctor` (flightdeck), no route | `verify` keep parity; `doctor` stays CLI (it reads *this host's* trees — see D11) |
| `cluster monitor` snapshot | route ✅, tool ✖ | **not CLI-only** as stated; decide only whether a tool is wanted (D2) |
| `orch inject` | tool `orch_message` is deliberately *adjacent*, not equal | 🔒 CLI-only — live pane steering |
| `main status` | no route, pure read | **not** a destructive/interactive case; recommend a read route (D9) rather than "CLI-only" |

---

## 4. Where each agent tool reads state

The card's core requirement: no tool may work from a stale copy or a second source of
truth. All 26 tools, with the `~/.hscc` file (if any) each one touches and who else
writes that file. "live" = resolved through sparkrun at call time; "none" = the tool
holds no local state, so it cannot be stale.

| tool | state source | other writers of that file | stale-copy risk |
|---|---|---|---|
| `cluster_status` | live sparkrun query; `cluster.json` for names/ids (`ops.py:103`) | bootstrap, `hscc-roles` | low — identity only |
| `list_recipes` | sparkrun recipes (live) | sparkrun (read-only) | none |
| `pick_node` | live status + pause overlay | `node_pause_exec` | low |
| `discovery_status` | precedence: **live `sparkrun cluster list --json` → `~/.hscc/cluster.json` cache → defaults** (`discovery.py:6,29-31`) | bootstrap, doctor | **medium**: the cache is a documented fallback, so a stale `cluster.json` answers when sparkrun is unreachable. The precedence line is the defence — keep it, do not re-order |
| `nas_status` | discovery + one mount probe | — | low |
| `node_paused` | `~/.hscc/node_pauses.json` via `node_pauses.py:88 OVERLAY_NAME` | `node_pause_exec`, CLI `node pause/resume` | low — one writer module, read-only tool |
| `provision_model` | serving topology from `serving.json` + applied template (`clusterlib.py`, `cluster_template.py`) | bootstrap `serving_gen.py`, `template apply` | medium — three writers of `serving.json` (bootstrap, template apply, onboard.py) |
| `stop_model` | live + `serving.json` | as above | medium |
| `model_health` | live endpoint probe | — | none |
| `node_pause` | **delegates to the CLI's own verb functions** (`node_pause_tools.py:31`) — no second implementation | — | **none by construction** (the intended pattern; see §5) |
| `node_resume` | same delegation | — | none |
| `vllm_logs` | live ssh/docker via sparkrun | — | none |
| `node_diagnostics` | live | — | none |
| `nas_diagnose` | live | — | none |
| `restart_model` | live + `lifecycle.py` pause guard (`lifecycle.py:242 node_pauses.covering()`) | — | none |
| `remount_nas` | live | — | none |
| `repair_nas_export` | live (QNAP exports), backs up first | — | none |
| `reap_orphans` | `serving.json` is the *definition* of "orphan" | as above | **high**: a stale `serving.json` makes a live workload look orphaned. This is the one tool where a stale copy deletes work — worth an explicit guard note |
| `cluster_verify` | `verify.py` reads plugins, streams, proxy, `applied_template.json` | `cluster_template.py`, `cluster-guard.py` hook | low |
| `cluster_throughput` | live vLLM metrics | — | none |
| `fleet_stats` | Hermes kanban DB (via Hermes) | Hermes | n/a — Hermes owns it; do not read `state.db` directly |
| `autoscale_advice` | live queue depth | — | none |
| `list_templates` | template dir (repo/bundled) | repo | low |
| `preview_template` | templates + live cluster diff | — | low |
| `apply_template` | writes `applied_template.json` | `cluster-guard.py`, `node_pause_exec`, `verify.py`, `declaration_agreement.py`, `hscc-commands/cmdlib.py` all **read** it | low for the writer, but see F1: five readers |
| `orch_message` | `~/.hscc/cluster.json` (rate-limit config) + `~/.hscc/state/orch_messages.jsonl` (audit) | env override `HSCC_ORCH_MSG_MAX_PER_HOUR` | none — audit log is append-only; config env-resolved at call time (`orch_message.py:149-153`) |

**Grouped state files** (who owns each — one owner per file is the invariant):

| file | owner | readers |
|---|---|---|
| `~/.hscc/serving.json` | `hscc_daemon/serving.py` + bootstrap `serving_gen.py` | discovery, clusterlib, cluster_template, doctor, enable_plugins, cluster-guard hook, onboard.py |
| `~/.hscc/applied_template.json` | `hscc-cluster/cluster_template.py` | cluster-guard hook, `node_pause_exec`, `verify`, `declaration_agreement`, `hscc-commands/cmdlib` |
| `~/.hscc/node_pauses.json` | `hscc_daemon/node_pauses.py` (via `node_pause_exec`) | cluster_render, autodown, lifecycle, hscc.py, hscc-cluster, doctor_node_pauses, routes_cluster |
| `~/.hscc/cluster.json` | bootstrap / operator-authored + discovery cache merge | discovery, ops, clusterlib, doctor_*, install_* |
| `~/.hscc/discovery_cache.json` | `hscc-cluster/discovery.py` | discovery only |
| `~/.hscc/autodown.json` (+`_interlock.json`) | `hscc_daemon/autodown.py` | routes_autodown, autodown_cli |
| `~/.hscc/merge-queue-state.json` | `hscc_daemon/merge_queue.py:148` | merge_queue_cli, routes_merge_queue |
| `~/.hscc/state/kanban_paused.json` | `hscc_daemon/kanban_pause.py:65` | orch_waker (`:1503`), kanban_cli |
| `~/.hscc/state/orch_waker.json(.jsonl)` | `hscc_daemon/orch_waker.py:143,1501` | routes/CLI status |
| `~/.hscc/main_wakeups.json` + `state/main_wakeups.json` | `hscc_daemon/main_wakeups.py:67-68` | main_cli |
| `~/.hscc/state/<stream>.json` (polling streams) | daemon `state.py` (`STATE_DIR`) | doctor rows, `hscc status`, `orch_waker.py:348` |
| `~/.hscc/history.db` | `hscc_daemon/metrics_history.py:94` | `cluster history` CLI + `GET /v1/cluster/history` (one store, two readers — correct) |
| `~/.hscc/alerts.json` | `hscc_daemon/alerts.py:103` | notify_cli, routes_alerts |
| `~/.hscc/update-check.json` | `hscc_daemon/update_check.py` | `hscc status`, `hscc update` |

---

## 5. Second-source-of-truth findings (state)

**S1 — the good pattern.** `node_pause` / `node_resume` / `node_paused` call the
unified CLI's own verb functions (`hscc-cluster/node_pause_tools.py:31`: "These call
the unified CLI's own verb functions — there is no second implementation to fall out
of step"). Same for `routes_ops._backing_cluster_up/down` → `eng.cmd_cluster_up/down`.
This is the pattern the rest of the surface should follow; say so in the tool docs.

**S2 — `/cluster-down` re-implements fleet down.** `hscc-commands/__init__.py:467-510`
runs its own parallel `sparkrun stop` per node instead of
`serving.fleet_down_cmd()` / `cmd_cluster_down()`. Two implementations, and only the
CLI/API one carries the post-boot marker clearing and the benign-"already down"
classification (`hscc-cluster/hscc.py:621-650`). Consequence: an operator who uses
`/cluster-down` gets different bookkeeping than one who types `hscc cluster down`.
→ F5, needs its own card.

**S3 — `hscc profiles` has two routes.** `GET /v1/profiles` (routes_ops) and
`GET /v1/profiles/list` (routes_profiles). Two answers to one question is how drift
becomes invisible. → F2.

**S4 — slash commands shell out to `sparkrun` directly.** `hscc-commands/cmdlib.py:19
SPARKRUN = "sparkrun"` and every handler builds its own argv. That is *deliberate* —
the file's docstring says the commands must work "when the orchestrator LLM is wedged"
— so it is not a defect, but it does mean the slash surface bypasses every guard the
cluster engine adds (pause overlay, dry-run contract, marker clearing). Any guard added
to `cmd_cluster_*` does **not** protect `/cluster-down`, `/cluster-reboot`,
`/cluster-restart`. That is the sentence to put in `hscc-commands/README.md`.

---

## 6. Slash-command inventory (12)

All from `hscc-commands/__init__.py:852-913`; backing = what the handler actually runs.

| slash | backs onto | CLI twin | route twin | notes |
|---|---|---|---|---|
| `/cluster` | cmdlib sparkrun reads | `hscc cluster status` | `/v1/cluster/status` | agree in content, differ in code path |
| `/status` | sparkrun + proxy + daemon probes | `hscc status` (daemon) — **different subject** | `/v1/mobile/summary` is closer | naming: two "status" surfaces about different things |
| `/orch-restart` | sparkrun restart of orchestrator vLLM | no CLI twin | no route | confirm-first ✅ |
| `/cluster-restart` | re-apply active template | `hscc template apply` | `POST /v1/template/apply` | third implementation of "re-apply" (with `/cluster-reboot`) |
| `/cluster-reboot` | reboot nodes | none | none | confirm-first ✅ |
| `/cluster-down` | **own** sparkrun stop loop | `hscc cluster down` | `POST /v1/cluster/down` | **F5** |
| `/cluster-docker-prune` | `docker system prune -af` per node | none | none | 🔒 operator-only, confirm-first ✅ |
| `/cluster-apt-upgrade` | apt update+upgrade | none | none | 🔒 same |
| `/cluster-prune` | macro of the three above | none | none | 🔒 same |
| `/heal` | heal unhealthy workers | `hscc heal-status`/`heal-clear` are a *different* subsystem (breaker strikes) | none | **F7** naming |
| `/template` | template list/preview/apply | `hscc template …` | `/v1/template/*` | ⚠ `args_hint` omits `status` + `validate` (F4) |
| `/workers-up` | bring up down keepalive workers | subset of `hscc cluster up` | `POST /v1/cluster/up` (superset) | non-destructive ✅, no confirm needed |

Also note `/v1/commands` (`routes_commands.py`) *serves* this catalog to clients, so a
stale `args_hint` (F4) is also a stale client render.

---

## 7. Skill coverage of the CLI (the empty column, quantified)

Method: for each verb string, `grep -rl -- "<verb>" install/hscc-skills/` and take the
top-level skill dirs. 49 strings tested; **7 produced any hit.**

| verb string | bundled skills that mention it |
|---|---|
| `hscc status` | `hscc`, `hscc-cluster` |
| `hscc cluster status` | `hscc`, `hscc-cluster`, `devops`, `hscc-model-onboard` |
| `hscc cluster hosts` | `hscc-cluster` |
| `hscc cluster monitor` | `hscc-cluster`, `devops` |
| `hscc verify` | `hscc` |
| `hscc escalate` | `hscc`, `devops` |
| `throughput` (word) | `devops` |
| **all 42 others** — `cluster jobs\|info\|up\|down\|history`, `template list\|status\|preview\|validate\|apply`, `autodown`, `merge-queue`, `kanban blocked\|stale\|monitor\|pause`, `cards monitor`, `orch inject\|waker`, `main start\|status\|wakeups`, `node pause\|resume\|paused`, `heal-clear\|heal-status`, `hscc stats`, `autoscale`, `notify`, `api`, `profiles`, `project`, `bootstrap`, `update`, `check`, `triggers`, `watch`, `log`, `plist`, `install` | **NONE** |

Contrast with the **curated** skill in a worker's profile tree
(`~/.hermes/profiles/backend-engineer/skills/hscc/`), which *does* carry
`references/node-pause-visibility-seams.md`, `references/orch-waker-wake-dedupe.md`,
`references/kanban-monitor-session-identity.md`, `references/cards-monitor-live-capture.md`
and names `hscc kanban monitor`. So the knowledge exists — it lives in one profile's
tree and in zero templates. A fresh `hscc bootstrap` therefore produces an agent that
cannot know 85% of the verbs exist. That is the finding, and it is a **bundling**
finding, not an installer finding.

---

## 8. What bootstrap installs, from where, and update-vs-skip semantics

**Chain:** `hscc bootstrap` → `hscc-bootstrap/bootstrap.sh:413-417` →
`hscc-skills/hscc.py install-skills` (`:320-368`), plus a second, separate stage
`install_review_skill.py --skill all` (`bootstrap.sh:498-504`).

**From where:** `SKILLS_SOURCE = $HSCC_SKILLS_SRC` else the first `install/hscc-skills`
found by walking up from the module (`hscc-skills/hscc.py:36-48`) else
`$HERMES_HOME/skills`. **To:** `$HERMES_HOME/skills`, `HERMES_HOME` defaulting to
`~/.hermes` (`:30-31`).

**What it installs — 13 names / 12 dirs** (`hscc-skills/hscc.py:59-83`):
`brainstorming, caveman, executing-plans, systematic-debugging, test-driven-development,
verification-before-completion, writing-plans, hscc, hscc-cluster, hscc-model-onboard,
sdlc-review, devops` (devops carries 7 sub-skills: `brand-migration,
hscc-architecture, hscc-plugins, kanban-orchestrator, kanban-worker, template-audit,
webhook-subscriptions`). `hscc-lessons` exists under `install/hscc-skills/` but is **not
in `BUNDLED_SKILLS`** → never installed by bootstrap.

**Update or skip — three distinct outcomes, and only one of them is a skip:**

| outcome | condition | code |
|---|---|---|
| **UPDATED** | file missing, or MD5 differs → copied | `copy_if_different` `:120-131`, `copy_skill_dir` `:132-151` |
| **UP-TO-DATE** ("skipped") | MD5 equal → not rewritten | same |
| **GUARDED** (a real skip) | the skill name already resolves from *another* path in this home's scan set → hscc refuses to write it | `_guard_skips` `:285-317` via `hscc-bootstrap/skill_guard.py` |

**Measured on this host** (default home): all 11 top-level bundled `SKILL.md` files are
**byte-identical** to this sha's templates (md5 pairs equal for `hscc`, `hscc-cluster`,
`hscc-model-onboard`, `sdlc-review`, `brainstorming`, `caveman`, `executing-plans`,
`systematic-debugging`, `test-driven-development`, `verification-before-completion`,
`writing-plans`). So on the default home: **no stale skip is happening.** The operator's
Jul-1 dates are directory mtimes on `hscc-cluster/` and `hscc-master-knowledge/`, and a
directory mtime does not move when a file inside it is rewritten in place.

**But** three real defects sit behind that:

1. **The template is the ceiling.** `install/hscc-skills/hscc-cluster/SKILL.md` (8.5 KB,
   identical to the installed copy) contains **zero** occurrences of `node_pause`,
   `node_resume`, `orch_message`, `merge-queue`, `autodown`, `hscc main`, `orch inject`.
   A perfect installer cannot install content that is not there.
2. **Only the default home is ever written.** Hermes sets `HERMES_HOME` to the profile
   dir for every agent, so worker/orchestrator skills live in
   `~/.hermes/profiles/<profile>/skills/` — which bootstrap does not write (except the
   guarded `install_review_skill.py` fan-out for the review skill). Verified: the
   `hscc` skill *I* am loaded from is
   `.../profiles/backend-engineer/skills/hscc/SKILL.md`, and `diff` against
   `install/hscc-skills/hscc/SKILL.md` reports **differ**. There is no mechanism that
   propagates a curated profile edit back into the template, and none that pushes a
   template update into the 30+ profile trees. **Drift is therefore guaranteed in both
   directions.**
3. **Orphans are never reaped.** `hscc-master-knowledge` (Jul 1, `mode 600`) is not in
   `BUNDLED_SKILLS`; nothing updates it and nothing removes it. `hscc-lessons` is the
   mirror case: present in the template tree, absent from the list, so it can never be
   installed. Both are silent.

**Verdict on the operator's hypothesis:** *partly false, and the true part is worse.*
The installer updates correctly where it writes; it simply writes to one place, from a
stale source, and leaves orphans behind.

---

## 9. Findings (each needs its own card; do not fix them here)

| id | finding | evidence | severity |
|---|---|---|---|
| **F1** | Bundled skill content is ~85% silent on the CLI surface (42/49 verbs undocumented), while the *same* knowledge exists only in one profile's curated skill tree | §7 table; `diff` of profile vs bundled `hscc/SKILL.md` | **High** — a fresh install yields an agent that cannot discover most of its own surface |
| **F2** | `hscc profiles` has two GET routes (`/v1/profiles` and `/v1/profiles/list`) — second source of truth for the same answer | `routes_ops.py` route table; `routes_profiles.py` route table | Medium |
| **F3** | `hscc help kanban` lists 3 of the 7 dispatched subcommands (`pause`, `resume`, `paused`, `enforce` missing); `hscc kanban --help` lists all 7 — two help sources for one group | `hscc_daemon/hscc.py:585-592` vs `hscc_daemon/kanban_blocked.py:42-72` | Medium (help-honesty; the F5 rule in this repo's own comments) |
| **F4** | `/template` slash `args_hint` advertises only `list\|preview\|apply` — `status` and `validate` unreachable through the slash surface, and `/v1/commands` republishes the same stale hint | `hscc-commands/__init__.py:902-905` | Medium |
| **F5** | `/cluster-down` is a second fleet-down implementation (own parallel `sparkrun stop`) that misses the post-boot marker clear + benign-stop classification the engine has | `hscc-commands/__init__.py:467-510` vs `hscc-cluster/hscc.py:621-650` | **High** — operator-visible divergence in a destructive path |
| **F6** | `POST /v1/escalate` mutates while the CLI twin is explicitly "dry-run, no mutations" — the verb's help and the route's semantics disagree | `hscc_daemon/hscc.py:649` vs `routes_ops.py` escalate pair | Medium |
| **F7** | Naming collision across surfaces: `hscc heal-status/clear` (breaker strikes) vs `/heal` (heal unhealthy workers) vs `heal` tools; and `orch inject` / `orch_message` / `/orch-restart` / `/v1/orchestrator/chat` are four different capabilities under one prefix | §2.1, §2.6, §6 | Medium |
| **F8** | `hscc kanban monitor` has route parity only under a different name (`/v1/kanban/running`) — no doc connects them | `routes_kanban.py:252` | Low |
| **F9** | Pause records (reason/since/until/span) are reachable by an agent tool but not by any API route; `/v1/cluster/status` projects a PAUSED *state* only, so mobile/web cannot show why a node is parked | `routes_cluster.py:428-435`; no `/v1/node-pauses` route | Medium (the iOS/FlightDeck lane cares) |
| **F10** | `template validate` is missing on 3 of 4 surfaces (route, tool, slash hint) while `apply` — the destructive half — has all four | §2.3 | Medium |
| **F11** | Bootstrap writes only `$HERMES_HOME/skills` (default `~/.hermes`); the 30+ profile trees it cannot reach are what agents actually load. No update path, no reverse-sync of curated edits | `hscc-skills/hscc.py:30-31`; §8.2 | **High** |
| **F12** | Orphan/mis-listed skills: `hscc-master-knowledge` installed but not bundled; `hscc-lessons` bundled but not listed → neither is ever updated *or* removed | `hscc-skills/hscc.py:59-83` vs `ls install/hscc-skills` | Low-Medium |
| **F13** | `reap_orphans` decides "orphan" from `serving.json`, which has ≥3 writers; a stale `serving.json` makes a live workload reapable | §4 table | Medium (guard note / confirmation text) |

## 10. Decisions this card asks the operator to make

The matrix cannot be finished without these, and each is a judgement, not a
measurement. Recommended answer in **bold**.

- **D1 `verify`** — keep the existing route+tool parity, or retire the route and
  declare the verb CLI-only? **Keep parity.**
- **D2 `cluster monitor`** — add a `cluster_monitor` tool, or keep the read to the CLI +
  route? **No tool**: `cluster_status` + `discovery_status(probe=true)` already answer a
  worker, and the route covers dashboards. Document that instead.
- **D3 `cluster up|down`** — should any agent tool bring the fleet up/down, given
  `provision_model`/`stop_model` exist per-unit? **No tool** — fleet-wide verbs stay
  operator + confirm-gated route.
- **D4 `heal-*`** — expose breaker-strike reads/clears to agents (the breaker's own
  alert text tells a worker to run `hscc heal-clear`)? **Yes, read-only first**
  (`heal_status` tool); keep `clear` CLI-only.
- **D5 `template validate`** — add `GET /v1/template/validate/{name}` + the slash hint?
  **Yes** (safe read, completes the family).
- **D6 `kanban pause/resume/paused/enforce`** — confirm CLI-only. **Yes.**
- **D7 `notify`** — an agent tool to post operator alerts, or is `orch_message` + the
  daemon's own alerting enough? **No tool** — an agent that can page the operator
  can train the operator to ignore pages.
- **D8 `merge-queue` mutations** — keep the door closed (no route/tool)? **Yes, closed.**
- **D9 `main status`, `orch waker status`** — add read routes for the dashboards?
  **Yes, reads only.**
- **D10 `node pause/resume/paused` routes** — the fleet-physical trio is agent-tool
  covered but has no HTTP surface; the mobile app shows the fleet. Add
  `GET /v1/node-pauses` (+ confirm-gated POSTs) or keep it agent+CLI? **Add the read
  (F9); keep the mutations agent-tool + CLI only.**
- **D11 `doctor`** — stays `hscc project doctor` (reads this host's trees) with no
  route? **Yes.**

## 11. LEFT (what the next worker does; this card deliberately stops here)

- **LEFT-1** — per-verb map for `hscc project <verb>` (≈25 flightdeck commands in
  `hscc-project/flightdeck/commands/`) against the 24 `routes_project.py` +
  `routes_actions.py` endpoints. Family-level only here (§2.7).
- **LEFT-2** — same matrix for `hscc-cluster`'s *hooks* (6 `register_hook` sites:
  `kanban_task_claimed`, `kanban_task_blocked`, `kanban_task_completed`,
  `pre_tool_call`, `post_api_request`, `transform_tool_result`) — they are a 6th
  surface and were not counted as one here.
- **LEFT-3** — verify F5 in the field before fixing: run `hscc cluster down --dry-run`
  and read `/cluster-down`'s plan side by side, and only then decide whether the slash
  handler delegates to `cmd_cluster_down(dry_run=…)` or keeps its loop deliberately (the
  "works when the LLM is wedged" docstring is a real constraint — it does not, however,
  justify skipping the marker clear).
- **LEFT-4** — no test currently pins "every dispatched CLI verb appears in
  `COMMAND_HELP`" or "every `BUNDLED_SKILLS` name exists on disk and every dir under
  `install/hscc-skills` is listed". Both are cheap gates that would have caught F3 and
  F12. This card documents the gap; it does not add the gates (one-doc-only scope).

---

*Prepared by @backend-engineer for operator sign-off. This document is the gate: the
tools/skills/profile/doctor/QA cards parented to t_4c12ff7f stay in todo until the
verdict is recorded on the board.*
