# Main-only cluster surface: per-profile plugin/toolset resolution and the hooks that must survive

**Card:** t_1700dfc8 (clean re-cut of t_0f214ba0) — read-only investigation
**Measured:** analysis + probe sweep 2026-11-23 07:2x from worktree `wt/t_1700dfc8` at
`origin/main` = `e88c1bb7`. **Every `file:line` citation was then re-pointed to the repo
tree at `origin/main` = `3cb11669`** by `t_66c77e6c`: the first cut quoted the *deployed*
copies under `~/.hermes/plugins/`, whose line numbers differ (§1, §0.4). Fleet
denominators re-swept 2026-11-23 10:06 +02:00 and **corrected to 53 profile dirs by the
11:45 +02:00 re-measure** (`t_bf5b1d50`) — the sweep's glob also counted
`profiles/.deleted`, a Hermes tombstone dir and not a profile (§5.1). Unqualified citations are
**repo-tree**; deployed-tree readings are labelled *deployed*.
**Operator rule this doc implements (verbatim, 2026-11-23 07:24):** *"all this control and
monitor cluster plugins should be hooked only to general profile or whatever the users in
bootstrap call it"*.
**Posture:** read-only. No code change, no fleet call, no write to the live `~/.hermes`.
Every probe ran against a sandbox `HERMES_HOME` under the scratch dir; the live home was
only ever opened for reading. `~/fleet-check/opencode-runs/nolive-rule.txt` respected:
no `hscc-bootstrap/*.py` executed (not even `--help`), no `sparkrun`/`ssh` invoked, no
real host contacted.
**Sibling doc:** `docs/audits/2026-11-23-agent-surface.md` — its **LEFT-2** ("same matrix
for `hscc-cluster`'s *hooks* … they are a 6th surface and were not counted as one here")
is answered here in §3.
**Downstream:** this doc is the gate for `t_e98c6ad4` (bootstrap), `t_b52e70ba`
(install-skills), `t_c3a29c37` (SOUL templates), `t_caf98114` (doctor drift),
`t_035b1b0b` (QA sandbox). §9 maps each finding to its card.

---

## 0. Headline

**The brief's premise is false, and the truth is worse in one specific way.**

1. **Profiles do NOT inherit the root `plugins.enabled`.** Hermes has no root→profile
   config overlay: `load_config()` merges `DEFAULT_CONFIG` with **one** file —
   `get_hermes_home()/config.yaml` (`hermes_cli/config.py:2215-2262`, path from
   `config.py:433-435`) — and a kanban worker is *spawned with that home*: the dispatcher
   injects `HERMES_HOME=<profile dir>` into the child (`hermes_cli/kanban_db_dispatch.py:2800-2808`,
   "so the worker reads the profile-scoped config.yaml"). Measured: **0 of 51 profile
   configs has a `plugins` key** (§5.1), so for every profile `_get_enabled_plugins()`
   returns `None` → `gate_manifest` returns *"not enabled in config"* for all three
   cluster plugins and they never load. Proven by running the production gate against a
   copy of `profiles/backend-engineer/config.yaml`: **every `hscc-*` plugin → `placeholder
   / not enabled`**, while a copy of the root config gives **`load`** for exactly
   `hscc-cluster`, `hscc-commands`, `sparkrun-hermes` (§5.2). So the cluster tools are
   already absent from workers *and* project orchs — by accident, not by design.

2. **The surface is not even on the main orchestrator profile today.** It is on the
   **root home** (`~/.hermes`), which is the operator's interactive CLI, the gateway
   process and the kanban dispatcher. The main orchestrator is started as
   `hermes -p <main> chat` (`hscc_daemon/main_cli.py:8,61`) → its home is
   `~/.hermes/profiles/<main>` → **no `plugins.enabled` there → the main orchestrator
   loads none of it either.** The name is `general` right now
   (`~/.hscc/main-orchestrator.json` → `{"name": "general"}`, resolved by
   `hscc-roles/main_orch.py:1-30`, `DEFAULT_NAME = "general"`). So the operator's rule is
   an **ADD + REMOVE**, not a remove: someone must put the surface *on* the main profile.

3. **The 51 clean profile configs are one migration away from all having the surface.**
   `_migrate_to_21` writes `plugins.enabled` = **every directory under
   `$HERMES_HOME/plugins/` that has a manifest** (`hermes_cli/config_migrations.py:300-325`,
   `:328-347`) for any config lacking the key. Every profile home that has a `plugins/` dir — **all 53** profile
   dirs (the 10:06 sweep read "53 of the 54"; the 54th was `profiles/.deleted`, not a profile — §5.1) —
   *currently carries* `plugins/hscc-cluster`, `hscc-commands`, `sparkrun-hermes`,
   `hscc_daemon`, `hscc-api`, … (§5.1: 21 entries per profile `plugins/` dir). The next
   time `migrate_config()` reaches a profile config (`hermes_cli/profiles_config.py:26`,
   `post_update.py:97`, `doctor_config.py:391`, …) that profile silently gains the whole
   cluster surface — the exact state the operator just forbade. **This is the landmine
   the bootstrap card must defuse.**

4. **Hooks cannot be separated from tools by config, and 6 of 6 hooks are worker-lane.**
   Hermes gates at *plugin load*: `gate_manifest` → `_load_plugin` → `register(ctx)`, and
   `hscc-cluster`'s `register()` registers the 31 tools **and** all 6 hooks in the same
   call (`hscc-cluster/__init__.py:189-244`; declared at `hscc-cluster/plugin.yaml:13-64`).
   **That 31 is a repo-tree count (28 at `3cb11669`; with `sparkrun-hermes`'
   `sparkrun_exec` the agent catalog is 32), and it matters:** the *deployed* copy the
   worker and gateway lanes actually load (`~/.hermes/plugins/hscc-cluster/__init__.py`)
   is 169 lines and registers **26** tools — no `autodown_status`, no
   `merge_queue_status` — against 196 lines / 28 declared tools at `3cb11669`, and
   31 declared tools in the repo at 2.17.4. The deployed plugin trees lag
   main, so a line number read there need not resolve in the repo; this doc's citations
   are repo-tree unless labelled *deployed* (§1).
   There is no per-hook or per-tool enable anywhere in the loader. Meanwhile the hooks
   fire where the *worker* is: `post_api_request`/`transform_tool_result` are the
   `[budget]` note the worker SOUL promises and are explicitly worker-only
   (`hscc-cluster/budget_watch.py:8-16,126,292-301`), `pre_tool_call` stamps the
   per-profile tool ledger (`hscc-cluster/workflow.py:639-655`), and
   `kanban_task_completed`/`_blocked` fire inside `complete_task`/`block_task` — which the
   worker itself calls (`hermes_cli/kanban_db.py:2743,3211`). **Moving `plugins.enabled`
   wholesale to the main profile would kill the budget watch and the observability
   stamps.** §3 has the matrix.

5. **The rule is implementable inside hscc, with no hermes change.** `register()` can
   gate *tool* registration on "is this home the main orchestrator?" using the resolver
   that already exists (`hscc-roles/main_orch.py`, the same one `orch_message` and
   `hscc main status` read — the S1 pattern the sibling audit praises at
   `2026-11-23-agent-surface.md:268-275`), while hooks register unconditionally. Hermes'
   own knobs for hiding tools (`platform_toolsets` + `known_plugin_toolsets`,
   `hermes_cli/tools_config.py:520-527,558-565`; `agent.disabled_toolsets`,
   `tools_config.py:637-643` → `model_tools.py:300-328`) are platform-shaped,
   bookkeeping-dependent and **unproven for a plugin toolset name** — §6 recommends the
   code gate as primary and config as belt-and-braces, and §7 files the one real
   hermes-side ask.

---

## 1. Where each fact comes from

| what | path | why this path is authoritative |
|---|---|---|
| hermes source | `~/.hermes/hermes-agent` @ `v0.23.0-7-g045881137d` | It is the **editable** install the runtime uses: `venv/lib/python3.14/site-packages/__editable__.hermes_agent-0.0.0.pth` + `__editable___hermes_agent_0_0_0_finder.py` point at this checkout, so there is no separate site-packages copy to diverge from. Every hermes file:line below is from here. |
| hscc source — **repo tree**, the authority for every unqualified `file:line` below | this worktree at `origin/main` = `3cb11669` (the five plugin files cited are blob-identical at `15d3cad6`, the merge base the review measured) | plugin/SOUL/CLI text under audit, cited so a reader opening the repo finds that exact line |
| hscc source — **deployed copies**, labelled *deployed* wherever a figure comes from here | `~/.hermes/plugins/` (root) and `~/.hermes/profiles/*/plugins/` | discovery's *user* dir is `get_hermes_home()/plugins` (`hermes_cli/plugins_discovery.py:210-212`) — this is what actually loads for the lanes this doc is about, **but it lags main** (`hscc-cluster/__init__.py` 169 lines / 26 tools *deployed* vs 31 in the repo at 2.17.4 — 196 / 28 at `3cb11669`). The first cut of this doc read plugin line numbers from here and labelled them repo-tree; `t_66c77e6c` re-pointed them and verified each against `git show 3cb11669:<file>` |
| configs | `~/.hermes/config.yaml`, `~/.hermes/profiles/*/config.yaml` | read via `yaml.safe_load` in three probe scripts (below); no writes |
| main-orchestrator identity | `~/.hscc/main-orchestrator.json` via `hscc-roles/main_orch.py` | the single resolver; never a hard-coded name |

**Probes** (in `$SCRATCH`, all read-only, run with the hermes venv interpreter):
`probe_plugins_sweep.py` (per-profile `plugins.*`), `probe_toolsets_sweep.py` (per-profile
`toolsets`), `probe_profile_homes.py` (per-home `plugins/` + hscc skill dirs),
`probe_gate.py` + `probe_gate_child.py` (the **production** gate path
`collect_directory_manifests` + `gate_manifest` + `_get_enabled_plugins` run against a
sandboxed `HERMES_HOME` — no plugin code imported), `probe_surface_refs.py` (needle
sweep over SOUL/skill prose). Raw tables: `plugins-sweep.tsv`, `toolsets-sweep.tsv`,
`profile-homes.tsv`, `surface-refs.tsv` (4,290 hits / 260 files).

---

## 2. Q4 first — the actual resolution order (everything else depends on it)

Per-process, Hermes resolves config as **one file**, not a stack:

1. `get_hermes_home()` = context-local override → `HERMES_HOME` env → platform default
   (`hermes_constants.py:194-201`; `get_process_hermes_home()` at `:243-252`).
2. `get_config_path()` = `get_hermes_home()/"config.yaml"` (`hermes_cli/config.py:433-435`).
3. `load_config()` = `DEFAULT_CONFIG` deep-merged with *that file* (+ managed scope,
   env-expanded) — `config.py:1978-1983` → `_load_config_impl` `:2215-2262`. **There is no
   merge with a parent/root config.** A profile home with no `plugins:` key therefore has
   `plugins.enabled is None`, full stop.
4. Worker exception: a *managed* worker binds a frozen session config at exec and every
   config reader returns it — `agent/managed_worker.py:126-143` (`bind_worker_policy`),
   `agent/safe_worker_policy.py:45-49` (`_bind_worker_config`: "plugins, MCP, hooks and
   providers load as usual"), `:77-80`, short-circuited into the loader at
   `hermes_cli/config.py:1779-1789,1792-1795`. The frozen config is the *session policy's*
   snapshot with only `command_allowlist` refreshed from the live profile file
   (`gateway/session_managed_worker.py:79-93`) — so for the CLI-`-p` path below it is
   still the profile's own `config.yaml`.
5. Plugin discovery scans `get_bundled_plugins_dir()` (the *hermes* repo's `plugins/`),
   then `get_hermes_home()/plugins`, then optional project dir
   (`hermes_cli/plugins_discovery.py:190-219`). Later sources win on key collision
   (`:222-236`). **The cluster plugins are all `source=user`** (`kind: backend`,
   `hscc-cluster/plugin.yaml:5`), so they need `plugins.enabled`: the bundled auto-load
   branches (`gate_manifest` `:312-319`) do not apply, and `:320-324` skips them.
6. `multiplex_profiles: true` (`~/.hermes/config.yaml:554`; note the *nested*
   `gateway.multiplex_profiles: false` at `:487` is a different key) does **not** change
   any of this. One process serving several profiles keeps **one PluginManager per
   resolved home** (`hermes_cli/plugins.py:1517-1520,1536,1630-1632`), and each manager's
   discovery reads *that home's* config through the same `_get_enabled_plugins()`
   (`plugins_discovery.py:122-131`). Multiplex changes which manager is *active*, not
   what is enabled where.

**Consequence to state plainly to the reviewer:** `plugins.enabled` is a **per-home**
switch in Hermes. "Move it to the main profile" is not a one-line edit of the root file —
it is (a) add it to `<main>/config.yaml`, (b) keep it where the dispatcher and gateway
run, (c) make sure nothing else ever enables it per profile (see the §0.3 landmine).

---

## 3. Q1 — the hook matrix, and whether a disabled plugin keeps its hooks

### 3.1 The load path (why "hooks but not tools" is not a config decision)

| step | file:line |
|---|---|
| read `plugins.disabled` / `plugins.enabled` | `hermes_cli/plugins_discovery.py:112-131` |
| decide per manifest (disabled wins → kind → bundled auto-load → **opt-in list**) | `plugins_discovery.py:269-334`; the opt-in refusal is `:320-324` |
| load survivors in `requires_plugins` order | `hermes_cli/plugins.py:1337-1355` |
| `register(ctx)` runs → tools *and* hooks are registered together | `hscc-cluster/__init__.py:151-196` |
| hook callbacks stored on the manager (`self._hooks`) | `hermes_cli/plugins_dispatch.py:559-566`, `track_hook` `:567-580` |
| delivery resolves **the active home's manager** | `hermes_cli/plugins.py:1800-1812` (`invoke_hook` → `_delivery_manager()` `:1784-1798`) |

There is **no** loader notion of "load for hooks, skip tools". A plugin is either loaded
(`plugins.enabled` ∩ manifest gate → `register()` ran → tools registered **and** hooks
registered) or it is a `LoadedPlugin(enabled=False, error="not enabled in config")`
placeholder with neither. `has_hook()` (`plugins.py:1848`) and
`_kanban_observer_consumed()` (`hermes_cli/kanban_db.py:184-192`) both short-circuit on
exactly that, so a disabled plugin's hooks are silently dead, not degraded.

### 3.2 The six hooks

Registered at `hscc-cluster/__init__.py`: claimed `:162`, blocked `:169`, completed `:176`,
pre_tool_call `:183`, post_api_request `:193`, transform_tool_result `:194`. Declared in
`hscc-cluster/plugin.yaml:56-64`.

| hook | what hscc does with it | process that fires it (hermes file:line) | lane | must survive workers losing the **tools**? |
|---|---|---|---|---|
| `kanban_task_claimed` | WS4 idempotent-resume note so a re-dispatched worker continues instead of redoing work (`__init__.py:157-158`) | dispatcher, **inside the board's dispatch lock** (`hermes_cli/kanban_db.py:2202`; `kanban_db_dispatch.py:1360`) | dispatcher→worker | **YES.** Fires in the dispatcher process (root home) → survives a per-profile move *only if* the dispatcher keeps running with the home that has the plugin. |
| `kanban_task_blocked` | surface blocked tasks to ops (`:169`) | the process calling `block_task` — the worker's own tool call (`kanban_db.py:3211`), and the dispatcher on breaker/reclaim paths | worker + dispatcher | **YES** (worker-initiated blocks are the majority). |
| `kanban_task_completed` | record completions + auto-unblock (`:176`) | the worker's own `complete_task` call (`kanban_db.py:2743`) | worker | **YES.** |
| `pre_tool_call` | stamp `profile_name` + `tool_name` into the HSCC tool-event ledger (`workflow.py:639-655`) | every agent turn, in that agent's process | worker (and orch) | **YES** — without it the per-profile observability the fleet dashboards read goes blank exactly where it is most needed. |
| `post_api_request` | worker budget watch: per-session `prompt_tokens` + call count, **worker-only by construction** (`budget_watch.py:8-16,126,292-301`) | the agent process, per API call | worker | **YES** — this is the `[budget]` note the worker SOUL promises. Losing it is a documented-promise regression. |
| `transform_tool_result` | append the one `[budget]` line (`budget_watch.py:14-16`) | the agent process, per tool result | worker | **YES** — same promise. |

**Answer to Q1 in one sentence:** every one of the six hooks serves a worker or a
dispatcher-on-behalf-of-a-worker, none of them is main-orchestrator-only, and hermes
offers no way to keep a disabled plugin's hooks — so **`plugins.enabled` cannot move
wholesale to the main profile.** The removal has to happen one layer *below* the plugin:
which tools `register()` exposes (or which toolsets the profile selects), not whether the
plugin loads.

### 3.3 Two safety notes found while reading the delivery path

* `invoke_hook`/`has_hook` return `[]`/`False` wholesale when the process is a **safe-mode
  bypass worker** (`plugins_dispatch.py:282-284,559-566` → `agent/safe_worker_policy.py:81-83`).
  Ordinary managed workers (`safe_mode=False`) keep hooks. So a QA sandbox card that
  exercises the budget note must **not** run the worker in safe mode or it will measure a
  false negative.
* Hook callbacks are bounded by `plugins.hook_callback_timeout` and `pre_tool_call`
  **fails closed** with a block directive on timeout/exception
  (`plugins_dispatch.py:290-320`, `plugins.py:1800-1812`). A worker-lane hook that is
  *loaded but slow* can block tool calls fleet-wide; keep the new identity gate cheap and
  cached (the `main_orch` resolver is in-process-cached — `main_orch.py:2-3`).

---

## 4. Q2 — who else touches `orch_message` and the `hscc-commands` verbs today

| surface | today's reach | evidence |
|---|---|---|
| `orch_message` (tool) | Reachable by any home whose manager loads `hscc-cluster` (i.e. the root home today). Its **own** policy already refuses every worker and `default` at **both** ends: `ORCH_PROFILE_RE = ^…-orch$` (`hscc-cluster/orch_message.py:92`), "worker profiles and `default` are refused" (`:17-18,150,323-326,620`), plus a same-turn loop guard (`:33-39` prose, `:514` `_loop_guard_refusal`) and rate limit. Non-`*-orch` admission of the *main* name is resolved through `hscc-roles/main_orch.py`, never hard-coded. | §4 of `2026-11-23-agent-surface.md:243` |
| `orch_message` **callers** (text that tells agents to use it) | `hscc-roles/generator.py:709-713` — work rule **11** ("`orch_message` is a nudge, not a work order") is emitted into the generated SOUL of profiles. Measured: it lands in the 21 `*-orch` SOULs *and* the worker-role SOULs (525 SOUL rows across the 50 profile SOULs in `surface-refs.tsv`, the 07:2x sweep artifact); the rule text itself is not profile-conditional in `generator.py`. | `hscc-roles/generator.py:692-713` |
| `hscc-commands` slash verbs (12) | Registered whenever the plugin loads — `hscc-commands/__init__.py:965-1020`, twelve `ctx.register_command` calls, **no profile/identity gate anywhere in the module** (its only "orchestrator" references are about the *serving* unit, e.g. `:168,225-236`). A worker that can load the plugin can run `/cluster-down`, `/cluster-docker-prune`, `/heal`. | `hscc-commands/__init__.py:965-1020`; wedge-safety rationale at `:5` |
| `/v1/commands` | republishes the same catalog to clients (`2026-11-23-agent-surface.md:318-319`) — so a profile-scoped verb list would need this route to agree. | sibling doc §6 |

Because slash commands are gateway-side and *deterministic* (they exist precisely so the
operator has a path when the model is wedged, `hscc-commands/__init__.py:5`), the
recommended gate keeps them available **where the operator is** (root home + main profile)
and drops them for worker/project-orch homes. `orch_message` needs no new gate — it is
already correct; the SOUL rule text is what misleads (card `t_c3a29c37`).

---

## 5. Q3 — what breaks when workers lose the toolset (card `t_c3a29c37` + `t_b52e70ba` consume this)

### 5.1 Measured state of the fleet's config surface

| measurement | value | source |
|---|---|---|
| root `plugins.enabled` | `[hscc-cluster, hscc-commands, sparkrun-hermes]` | *deployed* `~/.hermes/config.yaml:529-535` (a root-config reading, not a repo line) |
| profile dirs | **53** | re-swept 2026-11-23 10:06 +02:00 as **54**; corrected by the **2026-11-23 11:45 +02:00 re-measure** (`t_bf5b1d50`) — the glob counted `profiles/.deleted`, not a profile (note below this table) |
| profile `config.yaml` files | **51** (of the 53 dirs) | same |
| profile configs with any `plugins` key | **0 / 51** configs | same |
| profile configs naming `hscc-cluster` + `sparkrun` in top-level `toolsets:` | **25 / 51** configs — incl. `general`, `general-orch`, `orchestrator`, every `*-orch`, **and the worker roles `backend-engineer`, `ecofire-flows-executor`** | re-swept 2026-11-23 10:06 +02:00; the 07:2x `toolsets-sweep.tsv` had **26 / 52** |
| root `toolsets:` | `[hermes-cli, hscc-cluster, kanban, sparkrun, delegation]` | same |
| profile homes carrying hscc plugin dirs | **53 / 53** profile homes (`plugins/` present in **all 53**, **21 entries in every one**, incl. `hscc-cluster`, `hscc-commands`, `sparkrun-hermes`, `hscc_daemon`, `hscc-api`, `hscc-roles`, `hscc-bootstrap`, `hscc-skills`) | re-swept 2026-11-23 10:06 +02:00 (read there as **53 / 54**); re-measured 2026-11-23 11:45 +02:00; `profile-homes.tsv` is the 07:2x sweep |
| profile `SOUL.md` files | **51** (of the 53 dirs); **0** carry the cluster-ops block of §5.3 | re-swept 2026-11-23 10:06 +02:00; denominator corrected 2026-11-23 11:45 +02:00 |
| hscc-owned skill dirs per profile | 0–20 (e.g. `backend-engineer` 20, `ios-engineer` 19, `devops-engineer` 11) — the `t_45fa38e2` fan-out | `profile-homes.tsv` (07:2x sweep) |

**Why `profiles/.deleted` is not a profile, so the next sweep does not re-count it.** The 10:06
denominator came from a `find ~/.hermes/profiles -maxdepth 1 -type d`-style glob, and such a glob
returns dot dirs too. Re-measured 2026-11-23 11:45 +02:00 (re-checked 2026-11-24 01:45 +02:00,
same answer): **53** non-dot profile dirs plus `.deleted`. `.deleted` is Hermes' tombstone
location for a renamed/deleted profile home — *deployed* `hermes_cli/profiles.py:2320` names it
(`profiles/.deleted/<old_name>`) — and Hermes' own listing would never return it:
`_iter_named_profile_dirs()` keeps only names matching `PROFILE_ID_RE`
(`^[a-z0-9][a-z0-9_-]{0,63}$`, *deployed* `hermes_constants.py:401`, used at
`hermes_cli/profiles.py:480-497`) that carry a profile identity marker. `.deleted` satisfies
neither and holds no `config.yaml`, no `SOUL.md` and no `plugins/` — only two `probe-20261026-*`
marker files, mtime Oct 26 22:02. Hermes skips it on the name alone wherever it lists profiles
(`PROFILE_ID_RE` applied at *deployed* `hermes_cli/web_server_profiles.py:179`,
`hermes_cli/backup.py:1780`), so nothing was deleted between the 10:06 sweep and the 11:45
re-measure: the count was simply wrong and the sweep itself is not retracted. hscc's own
per-home sweep does not carry the bug —
`skill_guard.discover_profiles()` skips dot dirs (`hscc-bootstrap/skill_guard.py:500-513`) and the
`doctor` row added by `t_caf98114` inherits that by delegating rather than globbing again, pinned
by `hscc-bootstrap/tests/test_doctor_main_only_surface.py::test_dot_dirs_are_not_profiles`. A
future re-sweep must filter `-name '.*'`, not just `-type d`.

### 5.2 The gate, run (not reasoned)

Same production code path, sandboxed homes (`probe_gate.py`), nothing imported/executed:

```
== worker-backend-engineer   plugins.enabled = None
   hscc-cluster    kind=backend src=user -> placeholder  "not enabled in config"
   hscc-commands   kind=backend src=user -> placeholder  "not enabled in config"
   sparkrun-hermes kind=backend src=user -> placeholder  "not enabled in config"
   hscc_daemon / hscc-api / hscc-roles / hscc-bootstrap / hscc-skills / memori_byodb
                  -> placeholder  "not enabled in config"
== root-home                  plugins.enabled = ['hscc-cluster','hscc-commands','sparkrun-hermes']
   hscc-cluster    -> load
   hscc-commands   -> load
   sparkrun-hermes -> load
   (all other user plugins in the same tree -> placeholder)
```

So **the tools are already gone for workers and project orchs**; what is *still* there is
everything that would bring them back: the plugin trees in every profile home, the
`toolsets:` entries naming them, and the `_migrate_to_21` grandfather.

### 5.3 Text that still promises the surface to non-main profiles

| what | file:line | verdict |
|---|---|---|
| "## Cluster ops = hscc-cluster toolset" … 7 named read tools, `provision_model`/`stop_model`/`restart_model`, `remount_nas`/`repair_nas_export`/`reap_orphans`, `confirm=true`, **and the claim "Only the orchestrator profile has this toolset; workers do not."** | template `hscc-bootstrap/install_soul.py:75-100` (claim at `:86`); deployed copy **`~/.hermes/SOUL.md:37-48`** | The claim is **true of the root home's own tools and false about its audience**: the block is installed only into the ROOT home SOUL (`install_soul.py:289-300` + `_root_home()` `:267-287` — "the ROOT install by default; `--home` to target one profile deliberately"). Measured: it appears in exactly **1** deployed SOUL — the **root** `~/.hermes/SOUL.md` — and in **0 of the 51** profile `SOUL.md` files (re-swept 2026-11-23 10:06 +02:00). So the file that asserts the rule is the one file whose reader is *not* the main orchestrator profile. |
| "`sparkrun_exec` — a raw `sparkrun ...` CLI passthrough (**orchestrator-only**)" | `install_soul.py:89-94`, deployed `~/.hermes/SOUL.md:41` | Same single-file problem. `sparkrun_exec` is a `sparkrun-hermes` tool, so its reach == that plugin's reach. |
| Operator slash commands `/cluster`, `/orch-restart`, `/cluster-restart` + "wedged engine … `/orch-restart` is the fix" | `install_soul.py:95-107`, deployed `~/.hermes/SOUL.md:44-48` | Fine for the main/operator reader; must not be presented as worker-reachable. |
| Work rule 11 "`orch_message` is a nudge, not a work order" | `hscc-roles/generator.py:709-713` | Emitted into generated role SOULs. Tool-level policy already refuses workers (`orch_message.py:17-18,92`), so for a worker this rule is a promise of a tool it cannot call. Either gate rule 11 to `*-orch`/main or add "(not available to you)". |
| "Rely on sparkrun and hermes — do not reimplement what they own" (≈10 lines, `cluster_status`/`sparkrun` wording) | 29 non-`-orch` profile SOULs × 10 lines (`surface-refs.tsv`, worker bucket: `sparkrun=264` hits) | This is the *only* cluster-adjacent prose workers actually carry, and it is a **constraint**, not a capability grant — it says fleet facts come from sparkrun via `hscc_daemon/health.py`. It should stay, with one added sentence: workers get fleet facts through the **routes/CLI read surface**, not the cluster toolset. |
| Bundled skills naming the tools: `install/hscc-skills/hscc-cluster/SKILL.md` (57 hits), `hscc/SKILL.md` (53), `hscc/hscc-default-tools/SKILL.md` (12), `hscc-model-onboard/SKILL.md` (11), `devops/hscc-architecture/SKILL.md` (11) | `install/hscc-skills/**` | These are installed into **every** profile skills tree by `t_45fa38e2`. Card `t_b52e70ba` must reconcile the operator's "every profile has all the cluster-control info" ruling (`2026-11-23-agent-surface.md:564-571`) with "only the main profile has the tools": documentation of a toolset a profile cannot call is a lie in the prompt. Recommended split in §8. |
| `doctor`'s plugin checks (`hscc-bootstrap/doctor.py`, 135 surface hits) and `verify.check_plugins` | `hscc-bootstrap/doctor.py`, `hscc_daemon/verify.py` | Both currently assert the plugins are *installed*; per-profile expectations ("must be loadable by `<main>` only") are card `t_caf98114`. |

**What breaks the moment the tools genuinely go away for a worker:** nothing at runtime —
they are already gone (§5.2). What breaks is **truthfulness**: the SOUL/skill text above
tells a worker it has capabilities it does not have, and one line (`install_soul.py:86`)
claims a rule that no config enforces. That is card 4's whole scope.

---

## 6. Q4 — the config shape

Design decisions (mine, as owner of this card — both subtree cards must inherit them):

* **D-A. Identity is resolved, never hard-coded.** Anything that needs "the main profile"
  calls `hscc-roles/main_orch.main_name()` (single resolver; `~/.hscc/main-orchestrator.json`;
  currently `general`). No config file or code path may contain the literal `general` as a
  gate.
* **D-B. The gate lives in `register()`, not only in config.** `hscc-cluster`,
  `hscc-commands` and `sparkrun-hermes` register their *tools/commands* only when
  `get_plugin_manager().profile_name` (`hermes_cli/plugins.py:401-407`) resolves to the
  main profile **or** to the home that hosts the operator/gateway; hooks always register.
  This is the only mechanism that is (a) per-profile, (b) independent of `hermes tools`
  bookkeeping, (c) impossible to undo with a config migration, and (d) testable without a
  fleet.
* **D-C. The plugin stays enabled everywhere it must fire hooks** (root + main + every
  worker/project-orch profile), so §3's matrix keeps firing.
* **D-D. Config is belt-and-braces only**, because the hermes tool knobs are
  platform-shaped and unproven for plugin toolset names (§7).

```yaml
# ── ~/.hermes/config.yaml ───────────────────────────────────────────────
# The ROOT home = the operator's interactive `hermes`, the gateway process and
# the kanban dispatcher. It keeps the plugins so the dispatcher-fired lane
# (kanban_task_claimed, kanban_db.py:2202) and the operator wedge-path slash
# verbs (hscc-commands/__init__.py:965-1020) stay alive.
plugins:
  enabled: [hscc-cluster, hscc-commands, sparkrun-hermes]   # unchanged
# The root home is NOT the main orchestrator and must not be described as one
# in SOUL text (install_soul.py:86 fix, card t_c3a29c37).

# ── ~/.hermes/profiles/<main>/config.yaml ───────────────────────────────
# <main> = hscc-roles/main_orch.main_name()  (today: "general").
# THE ONLY HOME THAT GETS THE TOOLS. Written by bootstrap (card t_e98c6ad4).
plugins:
  enabled: [hscc-cluster, hscc-commands, sparkrun-hermes]
platform_toolsets:            # optional hardening; see §7 G-2 before relying on it
  cli: [hermes-cli, kanban, web, browser, terminal, file, code_execution,
        vision, skills, todo, memory, session_search, clarify, delegation,
        cronjob, messaging, hscc-cluster, sparkrun]

# ── ~/.hermes/profiles/<worker-or-orch>/config.yaml ─────────────────────
# Loaded (hooks fire: pre_tool_call, post_api_request, transform_tool_result,
# kanban_task_completed/_blocked) but registers NO cluster tool, because of D-B.
plugins:
  enabled: [hscc-cluster]     # hooks only — NO hscc-commands, NO sparkrun-hermes
# Hardening (same caveat): an explicit saved platform list that omits the
# cluster toolsets, plus the bookkeeping key that makes omission authoritative
# (hermes_cli/tools_config.py:520-527, :558-565):
platform_toolsets:
  cli: [hermes-cli, kanban, web, browser, terminal, file, code_execution,
        vision, skills, todo, memory, session_search, clarify, delegation,
        cronjob, messaging]
known_plugin_toolsets:
  cli: [hscc-cluster, sparkrun]
```

Three rules the cards must hold so this stays true:

1. **Never write `plugins.enabled` into a profile home except `<main>` (D-C's
   exception is `hscc-cluster`, hooks only).** That makes the `_migrate_to_21`
   grandfather (`config_migrations.py:328-347`) *dangerous*: it fires only when the key is
   absent, so bootstrap writing an explicit key is also what inoculates a profile. A
   profile created by `hermes profile add` after this change must be seeded the same way
   (card `t_e98c6ad4`, and `hermes_cli/profiles_config.py:26` is the migration entry
   point).
2. **Removing `hscc-cluster`/`sparkrun` from those 25 configs' top-level `toolsets:` is
   cosmetic, not a control.** Only two readers exist for that key — the legacy kanban
   fallback (`hermes_cli/tools_config.py:633`) and `tools/kanban_tools.py:46` — and
   neither gates plugin tools. Cards that "fix" the surface by editing that list fix
   nothing; keep the entries honest, but do not count them as enforcement.
3. **Skills follow the same split as §8**, so a profile never reads docs for a toolset it
   cannot call.

---

## 7. Gaps that need a hermes-side change (upstream ask)

| id | gap | why hscc cannot close it alone | ask |
|---|---|---|---|
| **G-1** | **No hooks-vs-tools split.** `gate_manifest` (`plugins_discovery.py:269-334`) is load/no-load; `register(ctx)` is all-or-nothing. The operator rule needs "this home loads the plugin for its hooks, and gets no tools". | hscc can emulate it in `register()` (D-B) — a workaround that lives in the plugin and depends on `profile_name()` staying accurate for routed/managed homes (`plugins.py:401-407`). If hermes ever gains a first-class notion it should replace the workaround. | `plugins.enabled` entries that can carry `{hooks_only: true}` (or a `plugins.tools_disabled: [names]` per-home list) honoured by `PluginContext.register_tool()`. |
| **G-2** | **Plugin toolsets are default-ON unless `known_plugin_toolsets.<platform>` bookkeeping says otherwise** (`tools_config.py:520-527`), and `agent.disabled_toolsets` (`:637-643` → `model_tools.py:300-328`) has to pass `validate_toolset()` for a *registry-only* name (`toolsets.py:278,367-380`). Nothing I measured proves a worker profile can name `hscc-cluster` there and have its 31 tools (repo-tree count at 2.17.4; 28 at `3cb11669`; the deployed tree registers 26) pruned. | It is a hermes resolution question, answerable only by running hermes. **Must be measured, not assumed** — this is exactly the class of "assumption underneath correct code" that shipped the v1.15.0 grace bug. | One assertion in the QA sandbox (`t_035b1b0b`): with `plugins.enabled=[hscc-cluster]` + `agent.disabled_toolsets=[hscc-cluster]`, `hermes tools --summary` and a live session show **zero** `cluster_*` tools, and `has_hook("post_api_request")` is still `True`. If it passes, config becomes a usable second lock; if not, G-1 becomes mandatory. |
| **G-3** | **`_migrate_to_21` grandfathers every manifest-bearing dir under `$HERMES_HOME/plugins/`** (`config_migrations.py:300-347`) — so "install the plugin into all profiles" and "keep profiles opt-in clean" are mutually unsatisfiable until bootstrap writes an explicit `plugins.enabled` everywhere. | hscc's installer is the thing putting plugin trees into profile homes; but the *behaviour* (a migration silently enabling third-party code) is hermes'. | Either (a) grandfather only plugins that were enabled-or-installed **in that home before the plugin dir existed**, or (b) log a WARNING naming the plugins it just enabled. Cheap; hscc can also neutralise it (card `t_e98c6ad4` writes the key). |
| **G-4** | **No per-profile view of "what is loaded here".** `hermes plugins list` is home-scoped and requires running as that profile; the fleet's truth is spread across 53 profile homes plus the root home. | Would be a hermes read-surface. | `hermes plugins list --all-profiles` (or `--profile X`) reporting each home's `plugins.enabled` + the gate verdict, i.e. exactly what `probe_gate.py` had to reconstruct. Until then hscc should ship the same check in `doctor` (card `t_caf98114`). |

G-3/G-4 are worth filing upstream; **G-2 must be measured by the QA card before anyone
relies on the YAML in §6 as enforcement.**

---

## 8. Handoff — one concrete job per sibling card

| card | does | the fact from this doc it must use |
|---|---|---|
| `t_e98c6ad4` bootstrap | write the §6 shape: `plugins.enabled` into `<main>` (via `main_orch.main_name()`), `[hscc-cluster]` into every worker/project-orch home; seed the same for newly added profiles | §0.1-0.3, §2 (per-home), §6 rule 1, **G-3** |
| `t_b52e70ba` install-skills | stop installing cluster-*tool* docs into homes that cannot call them: keep the *concept* + read-path skills, drop/defer `hscc-cluster` tool catalogs to `<main>`; keep the operator's "fold curated knowledge back into bundled source" ruling | §5.3 last two rows, §6 rule 3 |
| `t_c3a29c37` SOUL templates | rewrite `install_soul.py:74-100` per-audience: the cluster-ops block goes to `<main>` **and the root home must stop claiming to be the orchestrator** (`:86`); gate `generator.py:709-713` rule 11 to `*-orch`/main; keep the sparkrun-reliance paragraph for workers, with the read-path sentence | §5.3 rows 1-3, §4 |
| `t_caf98114` doctor drift | assert per home: `<main>` loads all three; every other home loads `hscc-cluster` **and no `hscc-commands`/`sparkrun-hermes`**; flag any home whose `plugins.enabled` was auto-written by migration | §5.1-5.2, §6, **G-4** |
| `t_035b1b0b` QA sandbox | prove **G-2** (toolsets knob actually prunes a plugin toolset) and prove the hooks still fire for a worker (`has_hook("post_api_request")` True; a `[budget]` line appears) **in a non-safe-mode worker** (§3.3) | §3.2-3.3, §7 |

Nobody in that list should merge anything; each hands off to `@hscc-orch`.

---

## 9. LEFT (deliberately not done here)

* **LEFT-1** — G-2 measurement (the single most load-bearing unknown in §6). QA card.
* **LEFT-2** — the same audit for the **`sparkrun-hermes`** plugin: its tool names, its
  `sparkrun_exec` passthrough (`install_soul.py:89-94` claims orchestrator-only) and
  whether it registers hooks at all. Not measured here; only its enablement was.
* **LEFT-3** — `hscc_daemon`, `hscc-api`, `hscc-roles`, `hscc-bootstrap`, `hscc-skills`
  are `kind: backend` in every profile home and currently not enabled anywhere; if any of
  them is *supposed* to load per-profile (e.g. `hscc-roles` hooks/SOUL authoring), that is
  a separate decision — this card only covers the operator's control+monitor set.
* **LEFT-4** — no test pins "`hscc-cluster/plugin.yaml:56-64` hook list == the
  `register_hook` sites" *plus* "each hook's firing process" — the drift test
  (`hscc_daemon/tests/test_plugin_manifest_drift.py`, cited at `plugin.yaml:49-55`)
  covers the declaration half only.

---

*Prepared by @devops-engineer. Read-only: no config, plugin, skill, SOUL or fleet state was
modified by this card. Probes and raw TSVs live in the worker scratch dir; the commands
that produced each number are named in §1. Citation re-point to the repo tree at
`3cb11669` and the denominator refresh: `t_66c77e6c`, same read-only posture.*
