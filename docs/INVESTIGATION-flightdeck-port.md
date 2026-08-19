# Design: port flightdeck's features into hscc — to make hscc the single tool

**Design exploration — task t_ab126417**
**Date:** 2026-08-19
**Assignee:** architect
**Status:** For operator review — NOT approved, NOT merged. Branch `design/flightdeck-port` off main.

> **Placement note (important):** this repo gitignores `docs/superpowers/` (it is
> internal, untracked working-doc space — see `docs/README.md` and `.gitignore`).
> The task asked for this file under that path, so it lives here for the working
> record; but because that path cannot be committed/pushed, the **identical**
> reviewable copy is committed on this branch at
> `docs/INVESTIGATION-flightdeck-port.md`. Read either; they are the same content.
> The committed copy is what the operator reviews on the pushed branch.

---

## TL;DR recommendation

**Do NOT physically merge flightdeck into hscc.** The two tools have genuinely
different domains (project/kanban/release orchestration across a portfolio of
unrelated app repos vs. GPU cluster/fleet operations), and the user's actual
goal — "one CLI to remember, one repo to check" — is better served by a
dependency/wrapper integration that makes every flightdeck feature reachable as
an `hscc` subcommand, than by literally folding flightdeck's source tree into
hscc.

| Question | Verdict |
|----------|---------|
| Full physical merge (move flightdeck source into `hscc-project/`)? | **No.** Different domains, huge blast radius, re-verify 1200+ tests, re-point every muscle-memory command — and it does not actually reduce "which CLI to remember" since `hscc` remains a distinct command anyway. |
| Dependency/wrapper (`hscc-cli` depends on the `flightdeck` package, exposes `hscc project … / hscc kanban …`)? | **Recommended.** One CLI entry point (`hscc`), flightdeck's 1200+ tests stay the source of truth, zero data migration (both already read the SAME Hermes kanban DB). No code moved. |
| Do the two tools conflict? | **No.** Confirmed: flightdeck's `core/kanban.py` imports `hermes_cli.kanban_db` and reads the same `~/.hermes/kanban.db` store the hscc/Hermes kanban tools use. Both integrate with Telegram's single-writer MCP daemon, but over disjoint state and config. The only shared mutable substrate is the Hermes kanban DB itself — which they are already safely co-reading today. |
| Is "just one tool" better served by a wrapper or a merge? | **A wrapper.** The user's reason is *command-surface reduction* and *repo-discovery reduction*, both fully achieved by a wrapper namespace. A physical merge buys none of that extra and costs far more. |

---

## 0. Why this card exists (the decision at stake)

The user wants to stop maintaining flightdeck as a standalone tool and use only
hscc going forward, with every flightdeck feature available from hscc. That is a
major architectural decision: flightdeck is an actively-developed (heavy work
this week), separately-tested (1200+) tool with its own CLI, docs, and registry.
Folding it into hscc — which is architecturally a *different kind of thing* (a
Hermes-agent plugin ecosystem, not a standalone CLI) — deserves a concrete,
reviewable plan before any porting work. This document is that plan. It moves
no code; it only surveys both codebases and recommends one integration path.

The two most important facts established by this investigation, up front:

1. **The two tools share the same kanban substrate.** flightdeck's
   `core/kanban.py` does not implement its own kanban — it imports Hermes'
   `hermes_cli.kanban_db` and reads/writes the same SQLite store
   (`~/.hermes/kanban.db` + per-board files) that hscc's agents/orchestrator and
   the Hermes `kanban_*` tools use. **Therefore a port needs ZERO data
   migration and ZERO dual-write shim** — there is already exactly one kanban DB.
2. **The two tools have non-overlapping domains.** flightdeck = project
   portfolio orchestration (ecofire-app, efsdriver, EcoFire_customizations_bc,
   sphoin, soconn, flosana, …). hscc = GPU cluster / fleet operations (DGX
   Spark, sparkrun, vLLM serving, role profiles, dep-bump automation). Almost no
   feature overlap; no state conflict.

---

## 1. Full feature inventory of flightdeck

Entry point: `flightdeck.cli:main` (console script `flightdeck`). Commands are
auto-discovered from `flightdeck/commands/*.py` — a module exposing
`build_subparser(sub)` + `run(args, registry_path)` registers itself. A module
may register several names (`legacy.py` → `legacy-cards` + `migrate-card`);
`sync.py`/`daemon_install.py` are helper modules wired into `project`/`topics`
and `daemon`, not standalone commands. An MCP server `flightdeck-mcp` wraps the
same `run()` entry points. Two global rules: read-only by default (`--apply` to
mutate); never report a state not verified.

State root: `~/.flightdeck/` (honours `HERMES_HOME`). Files: `config.yaml`,
`registry.yaml`, `templates/`, `state.yaml` (verify), `report-state.yaml`,
`test-baseline.yaml` (review), `qa-notified.yaml`, `manual-qa.yaml`,
`ingest-context-<project>.md`, `update-check.yaml`, `daemon/` (PID, log,
`state/<stream>.json`).

### 1.1 Project registry

| Command | What | State r/w | Integrations |
|---|---|---|---|
| `flightdeck project` (`new/list/remove/repair/sync/pull/push`) | Project lifecycle + registry CRUD; wires repo + Telegram topic + Hermes board + roadmap. `sync` adopts existing repos/topics/boards, `--create-boards`. `pull` ff-only, `push` gated, never force. | `registry.yaml` (rw) | Hermes kanban (boards), Telegram (topic discovery), git (`git_state`), `gh` CLI (`--github`) |

### 1.2 Kanban & dispatch

| Command | What | State r/w | Integrations |
|---|---|---|---|
| `standup` | Daily fleet digest — NEEDS YOU / FAILING / STALE / RUNNING / DRIFT + coverage footer. One-shot or `--watch`. | reads `state.yaml`; rw `update-check.yaml` | Hermes kanban DB, git, deployment probes. Read-only. |
| `monitor` | Live redraw of every RUNNING/CLAIMED card with elapsed time. | — | Hermes kanban DB. Read-only. |
| `qa` (`add/done`) | Manual-testing queue: pre-merge cards + `NEEDS MANUAL VERIFICATION` post-merge. `--watch`, `--notify`. | `qa-notified.yaml`, `manual-qa.yaml` | Hermes kanban DB, Telegram topic. |
| `why` | Trace one card's full story across kanban events + git. | — | Hermes kanban DB, git. Read-only. |
| `decompose` | Ask the cluster to break a milestone into quality-gated cards; `--apply` creates cards + stamps `MILESTONE:` tag. | `~/.flightdeck/templates/` | Telegram MCP daemon (orchestrator round-trip), Hermes kanban DB. |
| `start` | Release a milestone's cards out of block/ready into work, concurrency-aware vs fleet `max_in_progress`. | — | Hermes kanban DB, git. |
| `message` (`send/read/dispatch/broadcast`) | `send` posts to topic, `read` lists, `dispatch` creates card + announces (anchors worktree), `broadcast` to several topics. | — | Telegram MCP daemon (primary), Hermes kanban DB (dispatch). |
| `legacy-cards` | Surface cards on boards outside the registry mapping, attributed by repo path. | — | Hermes kanban DB. Read-only. |
| `migrate-card` | Move a card between projects/boards. | — | Hermes kanban DB. |
| `lint-cards` | Flag cards missing VERIFY/acceptance/concrete references; exit non-zero if any card would run in a MAIN TREE. | — | Hermes kanban DB, `core.lint`. Read-only. |

### 1.3 Review & QA

| Command | What | State r/w | Integrations |
|---|---|---|---|
| `review` (`--queue`) | Show a card's diff + merge verdict; `--apply` merges branch into base AND closes the card. Gate runs verify against a test baseline. | `test-baseline.yaml` | Hermes kanban DB, git. |
| `reconcile` | Close cards whose branch already merged; flag dead/stale. | — | Hermes kanban DB, git. |
| `hygiene` | Detect board decay — duplicate cards, triage traps, stale worktrees. | — | Hermes kanban DB, git. |
| `verify` (`--all`) | Run the registry `verify` shell command for a project, report PASS/FAIL/NO_VERIFY honestly. | `state.yaml` (rw every run) | `registry`, `core.verify`. |
| `doctor` | Self-check — config, registry, environment, kanban, Telegram daemon; reports what it can/can't trust. | — | everything. Read-only. |

### 1.4 Release

| Command | What | State r/w | Integrations |
|---|---|---|---|
| `release` | Validate preconditions + dry-run plan; `--apply` bumps version, runs install/verify, tags + GitHub release. | `VERSION`, pyproject | git, `gh` release, install shell cmd. NOT an MCP tool. |
| `update` | Detect install mechanism, compare installed vs upstream commit, `--apply` self-updates. | install metadata + `VERSION` | git, pip. NOT an MCP tool. |
| `incident` | Append dated incident entry (symptom/fix/cause/lesson) to project `docs/INCIDENTS.md`. | project file | `registry`. |

### 1.5 Reporting & metrics

| Command | What | State r/w | Integrations |
|---|---|---|---|
| `report` ($since; `--apply` posts to Telegram) | Summarise board activity; learnable summary to project topic. | `report-state.yaml` | Hermes kanban DB, Telegram. |
| `metrics` ($since) | Fleet/project operational metrics (throughput, cycle time, in-flight, verify state, deploy age). | — | Hermes kanban DB, git. Read-only. |
| `roadmap` (`show/progress/add/move/done/adopt`) | Show/progress a project's `ROADMAP.md`; `progress` ties live cards to milestones via `MILESTONE:` tags. | project `ROADMAP.md`/`.draft.md` | Hermes kanban DB. |
| `ingest` | Gather skills, repo docs, git log, Telegram topic; orchestrator synthesises `ROADMAP.draft.md`; `--apply` writes it (never overwrites). | `ingest-context-<project>.md` | Telegram, Hermes kanban DB, git. |
| `ask` | Fill a prompt template with project context + send via Telegram; or manage the template store. | `~/.flightdeck/templates/` | Telegram. |

### 1.6 Daemon

| Command | What | State r/w | Integrations |
|---|---|---|---|
| `daemon` (`start/stop/status/check/watch/log/notify/install/uninstall`) | Persistent background watcher (fleet in-flight counts, board freshness, orphan boards, version drift). **Hard read-only on projects.** | `~/.flightdeck/daemon/` | Hermes kanban DB (fleet/freshness/orphans streams), git (`ls-remote` version stream), macOS notifications, optional Telegram. `install/work` writes a launchd plist + `launchctl`. |

### 1.7 Cross-cutting facts

- **Project auto-detection from cwd** for `qa`, `review`, `report`, `metrics`, `verify`, `message send/read/dispatch`. Explicit project wins.
- **Card→project attribution** by repo path (card → branch `wt/<card>` → worktree → repo → registry), never by board slug.
- **Three external integrations:** Hermes kanban DB (SQLite, shared with hscc/Hermes native), Telegram single-writer MCP daemon (HTTP at `~/.hermes-tg/mcp_server.py`, default `http://127.0.0.1:8787/mcp`), git (+ `gh` for GitHub, launchd on macOS).

The full grouped inventory is preserved as an uncommitted artifact in this
worktree at `flightdeck-feature-inventory.md`.

---

## 2. Does flightdeck overlap or conflict with hscc? (the critical question)

### 2.1 The kanban relationship — RESOLVED: same store, no conflict

This was the flagged "critical to get right" question. Definitive answer with
code evidence:

- `flightdeck/core/kanban.py` does **not** hardcode a DB path and does **not**
  implement its own store. It imports Hermes' kanban library directly:
  `from hermes_cli import kanban_db` (`core/kanban.py:106`, inside
  `_load_kanban_db()`, lines 86–111). It puts the Hermes source tree on
  `sys.path` (`core/kanban.py:103–104`, `_HERMES_AGENT_PATH =
  "~/.hermes/hermes-agent"`, overridable via `HERMES_AGENT_PATH`) and does a real
  Python `import` — **not** a subprocess.
- Every read/write goes through Hermes' `kdb` API: `kdb.connect(board=slug)`
  (`kanban.py:309`), `list_tasks` (311), `list_events` (680), `list_boards`
  (218/230/305), `boards_root()` (500), `get_current_board()` (944),
  `create_board` (266).
- Hermes' `kanban_db_path()` resolves default board → `~/.hermes/kanban.db`
  and other boards → `~/.hermes/kanban/boards/<slug>/kanban.db`. Confirmed
  `~/.hermes/kanban.db` exists on disk.
- flightdeck's freshness-watermark SQL (`core/kanban.py:390–396`) queries
  Hermes' exact table/column names (`tasks.created_at` / `started_at` /
  `completed_at`).

**Conclusion: flightdeck's kanban IS Hermes' native kanban.** Both tools operate
on the same store. Merging them requires no data migration and no dual-write
shim, and there is no "separate implementation" conflict to resolve. This is the
single most important fact for the design: it makes integration dramatically
cheaper and lower-risk than a naive read of the task would suggest. (This also
explains why flightdeck is the natural read/reconcile/release shell over a board
the Hermes agents already write.)

### 2.2 Telegram — same daemon, disjoint state

Both talk to the same single-writer Telegram MCP daemon, but over disjoint
concerns: flightdeck owns topic/board mapping in `~/.flightdeck/registry.yaml`;
hscc uses Telegram only for escalation alerts (autonomy watcher). No state
collision. flightdeck's daemon-install helper `commands/daemon_install.py`
"mirrors `hscc_daemon/install.py`" — a small overlap worth noting (§5): the
launchd install/uninstall pattern already exists twice in near-parallel form.

### 2.3 Domain mismatch is real and should not be forced together

| | flightdeck | hscc |
|---|---|---|
| Kind | Standalone pip CLI | Hermes-agent plugin ecosystem (pure-stdlib) |
| Domain | Project/kanban/release orchestration across a *portfolio of unrelated app repos* (ecofire-app, efsdriver, EcoFire_customizations_bc, sphoin, soconn, flosana) | GPU cluster / fleet ops (DGX Spark, sparkrun, vLLM serving, role profiles, dep-bump) |
| Mutable state | `~/.flightdeck/*` | `~/.hscc/*` + `~/.hermes/plugins/*` |
| Shared substrate | **Hermes kanban DB (same store as hscc)** | Hermes kanban DB + git worktrees |

There is essentially **no feature overlap** — flightdeck has no cluster/model
tools; hscc has no project-portfolio/release tools. The shared substrate (Hermes
kanban + worktrees) is exactly what makes them *composable* rather than
*competing*.

---

## 3. Integration architectures (with tradeoffs)

### Option (a) — Dependency/wrapper: `hscc project …` / `hscc kanban …` over the `flightdeck` package — RECOMMENDED

`hscc-cli` (or a new `hscc-project` plugin dir) adds a `project`/`kanban`
subcommand group that depends on the installed `flightdeck` package as a
library and delegates to its command modules. No flightdeck code moves into the
hscc repo.

**Mechanics (concrete).** `flightdeck/commands/*.py` already have a uniform
`run(args, registry_path)` signature. The wrapper builds an argparse `hscc
project …` namespace whose sub-subcommands map 1:1 onto flightdeck's
`build_subparser`/`run`, loads the flightdeck package (pip dependency or
vendored onto `sys.path` exactly as flightdeck itself vendors Hermes),
constructs the args, and calls `module.run(args, registry_path)`.
`hscc project standup` ≈ `flightdeck standup`, etc. The `hscc-cli` `__init__.py`
already demonstrates this exact "find the package, insert on sys.path, import"
pattern (it does it for `hscc_daemon`).

**Pros.** Lowest risk. Keeps flightdeck's 1200+ test suite as the source of
truth. Zero data migration (same kanban DB). flightdeck keeps evolving
independently; hscc just exposes it. Natural namespace: `hscc project` reads as
"my projects" and never collides with `hscc cluster` (the cluster domain).

**Cons.** Keeps *two repos* (flightdeck + hscc) — the user's "one repo to check"
concern is only partially met. Still need to `pip install flightdeck` or vendor
it. Splits the command surface across two codebases; docs need a mapping table.

### Option (b) — Physical merge: move flightdeck source into `hscc-project/`, re-home tests, rewire entry points

Move flightdeck's actual source tree and tests into a new directory in the hscc
repo, re-point `hscc project …` onto it, and deprecate/archive the standalone
repo.

**Pros.** Literally "just one repo" — every flightdeck module, test, and doc
lives under hscc. One install, one version number (potentially), one PR flow.

**Cons.** Large diff and large blast radius. Re-verify 1200+ tests migrate
cleanly (they currently import `flightdeck.*`; every import path and the CLI
entry point must be re-homed). Re-point every script and muscle-memory command
(`flightdeck standup` → `hscc project standup`). **And it does not actually
reduce "which CLI to remember":** the command is still invoked as `hscc …`.
The one thing the user literally asked to reduce — the number of CLIs — is not
further reduced by moving source code between two private repos the user
already looks at. Meanwhile it destroys flightdeck's standalone identity and
folds a 1200-test actively-developed suite into a repo whose entire architecture
(plugin directories + pure stdlib) is different.

### Option (c) — Narrow: port only the kanban/dispatch primitives flightdeck shares with hscc's domain

Port only the parts of flightdeck that genuinely overlap hscc's existing
cluster-ops domain, and leave the project-portfolio commands as a separate
concern.

**Finding:** there is **no** meaningful kanban/dispatch primitive in flightdeck
to port — because flightdeck *has no kanban of its own*; it already delegates
to the same `hermes_cli.kanban_db` both systems use. There is nothing to
"sharpen" into an overlap. The remaining flightdeck-specific value (the project
registry, portfolio release/qa/report commands) is precisely the part with
NOTHING to do with GPU cluster ops. So option (c) collapses into either "do the
wrapper" or "do nothing," because there is no separate shared primitive layer to
extract. **A full merge is therefore the WRONG call** — the evidence does not
support it.

**Pros.** Smallest change. **Cons.** It is a non-answer: the project-portfolio
commands (the bulk of flightdeck's value) are exactly the ones it leaves out,
so it does not achieve "every flightdeck feature available from hscc."

---

## 4. Recommendation: Option (a) — dependency/wrapper

**Recommend a staged dependency/wrapper integration.** Grounded reasoning:

1. **The user's actual reason for "just one tool" is fully served by a wrapper.**
   The stated motivation is *reducing which CLI to remember, and reducing which
   repo to check for the command surface*. Option (a) delivers both at the
   command-line level: `hscc project standup`, `hscc project review`, `hscc
   kanban …` — one `hscc` binary, one `--help` tree. A physical merge (b)
   delivers *no additional command-surface reduction* beyond (a) — you still
   type `hscc …` either way — while adding a large, risky diff. The "one repo"
   benefit of (b) is marginal because both repos are private, adjacent
   (`~/dev/`), and the operator already checks both. The thing being reduced is
   the number of CLIs, and (a) already makes it one CLI.

2. **The shared kanban substrate makes (a) trivial, not aspirational.**
   Because flightdeck already reads the exact Hermes kanban DB hscc uses, the
   wrapper needs no data layer, no migration, no shim. It is purely a command
   surface: re-expose the same `run(args, registry_path)` entry points under an
   `hscc project …` namespace. This is the cheapest integration that satisfies
   the requirement, and it is cheap precisely because the underlying systems
   already share their store.

3. **blast radius and migration risk are dramatically lower with (a).**
   1200+ flightdeck tests stay where they are, still runnable with
   `cd flightdeck && pytest`. The hscc wrapper's own tests only assert that
   delegation works (subcommand exists, correct flightdeck module invoked), plus
   a smoke test that an existing flightdeck command runs. No re-homing of tests,
   no re-pointing of internal imports.

4. **The domains genuinely don't fit together, so don't force them.**
   flightdeck's portfolio commands (standup/review/qa/release across
   ecofire-app/efsdriver/etc) have nothing to do with GPU cluster ops. Merging
   them into hscc's plugin tree couples two unrelated bodies of work and two
   different test cultures in one repo — the opposite of the team's stated value
   of *many small parallel tasks over few large serial ones*. (c) correctly
   identifies they shouldn't be coupled; (a) is the mechanism that keeps them
   decoupled while still presenting one CLI.

**The one legitimate "merge-ish" item:** the launchd daemon-install pattern that
already exists in near-identical form in both `flightdeck/commands/daemon_install.py`
and `hscc_daemon/install.py`. Rather than a full merge, the wrapper should have
hscc's `project daemon install` delegate to flightdeck's command (as it does for
everything else), so the duplicated pattern is not itself duplicated via a
second wrapper. Not a merge — just not re-implementing it.

### What stays where (final boundary)

- **Stays in flightdeck repo (read-only from hscc's perspective):** all core
  logic (`core/*.py`), all commands, all 1200+ tests, all its docs, its
  `~/.flightdeck/` state, its package/versioning.
- **Added in hscc:** a thin `project`/`kanban` command group in `hscc-cli`
  (or a new `hscc-project` plugin dir) that imports the `flightdeck` package and
  delegates. The `hscc` CLI remains the single entry point the operator types.
- **Deprecation:** flightdeck stops being shipped/installed as a standalone
  `flightdeck` console script once the wrapper is proven; the repo is archived,
  not deleted. No flightdeck file is modified in this card.

---

## 5. Phased implementation plan (for the follow-on port card, if approved)

This card produces only this design + branch. If the operator approves option
(a), the follow-on implementation should proceed in phases. Rough scope estimates
assume a single engineer-agent on the existing fleet.

### Phase 1 — Dependency deployment (S / ~1–2 days)

- Add the `flightdeck` package as a dependency of `hscc-cli` (it is pip-install-
  able; will be installed into the runtime). If a plugin dir is preferred, add a
  `hscc-project/` plugin that vendors/imports flightdeck the way `hscc-cli`
  currently finds `hscc_daemon`.
- Add an `hscc project` group to the `hscc` argparse/click tree with all
  flightdeck subcommands mapped 1:1 (`standup`, `review`, `qa`, `release`,
  `verify`, `roadmap`, `ingest`, `decompose`, `start`, `message`,
  `report`, `metrics`, `monitor`, `why`, `doctor`, `hygiene`, `reconcile`,
  `lint-cards`, `legacy-cards`, `migrate-card`, `incident`, `project new/list/
  remove/repair/sync/pull/push`, `update`). Exact names in the mapping table
  below.
- **Deliverable:** `hscc project standup` and `hscc project review` work
  end-to-end and behave like their `flightdeck` counterparts (same output, same
  state files touched).

### Phase 2 — Prove parity + keep flightdeck's tests authoritative (S–M / ~2–3 days)

- Add an hscc-side test that each `hscc project <cmd>` delegates to the correct
  flightdeck module (assert the mapping), plus one smoke test running an existing
  flightdeck command through the wrapper.
- Do NOT move flightdeck's tests. They remain the source of truth in the
  flightdeck repo. hscc's own test suite stays small.
- Cross-check: run flightdeck's full 1200+ suite once against the same venv to
  confirm the wrapper didn't disturb anything.

### Phase 3 — Docs, aliases, muscle-memory (S / ~1 day)

- Ship a command-mapping table (`flightdeck X` ↔ `hscc project X`) in hscc docs
  and a shim in flightdeck's README pointing users to `hscc project …`.
- Optionally add `hscc flightdeck …` or `hscc kanban …` as documented aliases for
  the same group, so both mental names work.

### Phase 4 — Deprecate standalone, archive repo (S / ~½ day)

- Stop the `flightdeck` console-script install; keep the package importable.
- Archive the flightdeck repo (mark read-only / "archived" in its README), do NOT
  delete it. Update muscle-memory: anything that ran `flightdeck <cmd>` now runs
  `hscc project <cmd>`.
- Update `docs/superpowers/specs` (this doc's local working copy) to "Approved /
  implemented" per repo convention.

**Total rough scope: ~4–6 engineer-days** for the wrapper path, versus
~2–3× that plus a re-verified 1200-test migration for a physical merge.

### Command mapping table (illustrative — the exact surface is in the inventory)

| flightdeck | hscc |
|---|---|
| `flightdeck project new/list/remove/repair/sync/pull/push` | `hscc project new|list|…` |
| `flightdeck standup` | `hscc project standup` |
| `flightdeck review` | `hscc project review` |
| `flightdeck qa` | `hscc project qa` |
| `flightdeck verify` | `hscc project verify` |
| `flightdeck release` | `hscc project release` |
| `flightdeck roadmap <sub>` | `hscc project roadmap <sub>` |
| `flightdeck ingest` | `hscc project ingest` |
| `flightdeck decompose` | `hscc project decompose` |
| `flightdeck start` | `hscc project start` |
| `flightdeck message send/read/dispatch/broadcast` | `hscc project message …` |
| `flightdeck report` | `hscc project report` |
| `flightdeck metrics` | `hscc project metrics` |
| `flightdeck daemon …` | `hscc project daemon …` (note: distinct from `hscc daemon` cluster daemon — needs explicit naming to avoid collision) |
| `flightdeck doctor/why/monitor/hygiene/reconcile/lint-cards/legacy-cards/migrate-card/incident/ask/update/topics/init` | `hscc project <same>` |

> **Naming collision to resolve in Phase 1:** both tools already have a *daemon*
> concept — flightdeck's project-watcher daemon and hscc's cluster-self-heal
> daemon. Under a flat `hscc daemon` name they collide. The wrapper must put the
> flightdeck one under `hscc project daemon` (as shown) or another distinct
> namespace, and document the distinction. Same for `verify` (flightdeck's
> project verify shell-command vs hscc's cluster verify) and `doctor`. Namespace
> grouping (`hscc project …`) is precisely what keeps these from colliding.

---

## 6. What does NOT move (and why)

- **The project-portfolio commands appending `docs/INCIDENTS.md`, `ROADMAP.md`
  to project repos** — these operate on the *application repos'* own files, not
  on hscc. They stay in flightdeck, exposed through the wrapper.
- **flightdeck's `~/.flightdeck/` state files** — stay where they are; wrapped
  commands keep reading/writing them. No migration.
- **The Hermes kanban DB** — already shared; nothing to move.
- **Telegram MCP daemon** — already shared and already single-writer; no change.

## 7. Risks & mitigations

| Risk | Mitigation |
|---|---|
| `hscc project daemon` collides with hscc's cluster daemon | Distinct namespace + doc note (Phase 1/3). |
| Installing `flightdeck` adds a dependency to the pure-stdlib hscc runtime | flightdeck deps are `PyYAML` + `mcp` (lazy). Acceptable; or vendor flightdeck source with a shim. Explicitly acknowledged in the design — not a silent change. |
| Version skew between flightdeck and hscc release cadences | flightdeck keeps its own version; the wrapper just requires a minimum. |
| Operator muscle-memory: still types `flightdeck …` | Keep a console-script alias `flightdeck` that prints "use `hscc project …`" during Phase 3/4, then remove. |
| Two repos persist, splitting the effort | Accepted; the user's core "one CLI" goal is met. If full repo-consolidation ever becomes a hard requirement, Phase 2's proven wrapper makes a later physical merge (b) *safer* than doing it now, because semantics are already pinned by tests and parity checks. |

---

## 8. Honest non-goals / what this card deliberately does not decide

- Does NOT move, modify, deprecate, or delete anything in flightdeck's repo
  (read-only survey only).
- Does NOT modify hscc's `main` branch. This design lives on
  `design/flightdeck-port`, for operator review.
- Does NOT install/uninstall/move any plugin.
- Does NOT write code. This is a decision document + roadmap, nothing else.

## 9. The one-line answer to the operator

**Don't merge the source. Make `hscc project …` a thin wrapper over the
`flightdeck` package — both already read the same Hermes kanban DB, so the whole
port is just re-exposing flightdeck's existing `run()` entry points under the
`hscc` CLI. One CLI, zero data migration, flightdeck's 1200 tests stay the source
of truth; flightdeck's repo gets archived, not absorbed.**
