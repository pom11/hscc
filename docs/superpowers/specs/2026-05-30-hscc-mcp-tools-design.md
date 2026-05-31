# HSCC MCP Toolset + Native Approval — Design

**Date:** 2026-05-30
**Repo:** `pom11/hscc` (`~/.hermes/plugins`)
**Status:** Approved (design), pending implementation plan

## Problem

The custom `~/.hermes/hooks/route-guard.py` `pre_tool_call` hook was added to force the
35B orchestrator to route project work through HSCC instead of running it inline on the
gateway. In practice it breaks Hermes three ways (observed in session
`20260530_203425_dbc25440`):

1. **False positives** — strict block-by-default allowlist blocks benign read-only work,
   e.g. `gh repo list --type private | grep X || echo "not found"` was BLOCKED.
2. **Trivially bypassable** — the hook matches only the `terminal` tool. When a blocked
   `rm -rf ~/.hscc/...` was rejected on `terminal`, the model re-ran the *same* command
   via `computer_use` and `execute_code` (inline python), which the guard never sees.
3. **Thrash loop** — wrong-CLI attempts (`hscc kanban show`, `hscc projects status` →
   `No such command`) plus the block lecture drove a `same_tool_failure` loop, flailing
   across `terminal` → `computer_use` → `execute_code`.

Net: the guard is simultaneously too strict (blocks reads) and too loose (one of three
exec tools guarded), and does not feel like default Hermes.

## Goals (from brainstorming)

- **Primary:** keep real work off the orchestrator GPU node (.244) — work should run on
  the fleet (worker nodes .246/.247/.248), not inline in the orchestrator.
- **Block heavy/destructive ops and ask the user how to handle that specific task** —
  human-in-the-loop, not a silent deterministic block.
- **Restore default-Hermes feel** — remove the unnatural custom guard machinery.

## Key discovery: default Hermes already does "block heavy + ask me"

`tools/approval.py` is a full dangerous-command approval system, enabled by default on
messaging platforms (`HERMES_EXEC_ASK=1`, `gateway/run.py:1001`):

- `DANGEROUS_PATTERNS` / `detect_dangerous_command` — covers `rm -rf`, `git reset --hard`,
  force-push, `git clean -f`, SQL DROP/DELETE-without-WHERE/TRUNCATE, `sudo` privilege
  flags, `mkfs`/`dd`, gateway kill/restart, fork bombs, curl|sh, etc.
- Prompts the **user** (async) on Telegram/Slack/Matrix before running a flagged command.
- Operates at the **exec boundary shared by all tools**, not a per-tool string matcher →
  `terminal`, `execute_code`, and `computer_use` are all covered (no bypass).
- `approvals.mode: smart` uses an auxiliary LLM to auto-approve low-risk commands and only
  ping the user on genuine risk.
- Persistent `command_allowlist` in `config.yaml` so the user teaches it once.

The route-guard is a worse reimplementation of this. **Risk** (destructive) and
**placement** (should-run-on-fleet) are different axes: native approval handles risk;
placement needs a different lever — ergonomics, not enforcement.

## Approach (selected: Approach 2 — native approval + HSCC as proper tools)

Make dispatching to the fleet the path of least resistance by exposing HSCC operations as
first-class **typed MCP tools**, and delete the guard in favor of native approval. A weak
35B fumbles raw CLI (`hscc kanban` does not exist; the real form is
`python3 ~/.hermes/plugins/hscc-projects/hscc.py ...`); a typed tool removes that failure
mode and makes "do work" naturally mean "dispatch to the fleet."

### Component: `hscc-mcp` stdio MCP server

- **Location:** `~/.hermes/plugins/hscc-mcp/server.py` (active) +
  `~/.hermes/plugins/install/hscc-plugins/hscc-mcp/server.py` (template) — dual-layout rule.
  Both inside the `pom11/hscc` git repo.
- **Runtime:** the hermes venv python (`~/.hermes/hermes-agent/venv/bin/python`), which
  already ships the MCP SDK (`mcp` 1.26.0, includes `mcp.server.fastmcp.FastMCP`). No new
  install.
- **Implementation style:** each tool is a thin wrapper that shells out to the existing
  `python3 ~/.hermes/plugins/hscc-*/hscc.py <subcommand> [...]` and returns the parsed
  JSON. This reuses 100% of current plugin logic — **a typed facade, not a rewrite**. The
  working CLI plugins are untouched; the CLI remains usable directly (MCP is additive).
- **Why shell-out over importing internals:** trades small per-call overhead for total
  reuse and zero blast radius on the working plugins.

### Tools exposed

Read-only (run freely):
- `hscc_cluster_status` → `hscc-cluster cluster-status`
- `hscc_fleet_activity` → `hscc-agent-coordinator fleet-activity --json`
- `hscc_projects_show` → `hscc-projects show`
- `hscc_task_status` → `hscc-agent-coordinator task-status <id>`

Write, low-risk (run freely — additive, nothing executes on the fleet yet):
- `hscc_project_create` → `hscc-projects create <name> <desc>`
- `hscc_task_add` → `hscc-projects add-task ...`
- `hscc_dispatch_task` → `hscc-agent-coordinator dispatch-task <id>` (pre-creates the git
  worktree + a BLOCKED kanban card; no worker runs)

Risky, gated (see below):
- `hscc_release_task` → `release-task <id>` (unblocks → gateway spawns a live worker)
- `hscc_cancel_task` → `cancel-task <id>`
- `hscc_green_check` → `green-check <id>`
- `hscc_merge_worktree` → `merge-worktree <id>`
- `hscc_remove_worktree` → `remove-worktree <id>`

### Gating the risky tools

Native `DANGEROUS_PATTERNS` only inspects shell strings, so it will not see MCP tool calls.
Human-in-the-loop for `release` / `cancel` / `merge` is enforced in the tool signature:

- Each risky tool declares a **required `confirm: bool` parameter**; the tool returns an
  error refusing to act unless `confirm=true`.
- The tool **description** instructs the model to confirm with the user before passing
  `confirm=true`.
- This layers with the existing SOUL.md rule ("confirm before release/merge/cancel").

Read/dispatch tools have no `confirm` gate — dispatch is intentionally frictionless to make
fleet routing the easy path.

### Removals / reverts (Phase 1)

- Delete the `hooks.pre_tool_call` route-guard entry from `~/.hermes/config.yaml` (kills
  bypass, false-positives, loop). The hook file itself is moved to a backup, not deleted.
- Revert SOUL.md's strict allowlist prose to a light nudge:
  *"To run project work, prefer the `hscc_*` tools — they dispatch it to the fleet."*
- Set `approvals.mode: smart` so any remaining inline shell still gets native
  block-heavy-and-ask, auto-approving trivia.

## How this satisfies the goals

- **Keep work off .244:** a typed `hscc_dispatch_task` tool beats a fumbled CLI, so the
  model's natural route for "do work" is the fleet. Ergonomics, not a brittle block.
- **Block heavy + ask me:** native approval (smart mode) for inline shell; `confirm`
  params for risky fleet ops.
- **Default feel:** the custom guard is gone; the rest is stock Hermes plus an additive
  toolset.

## Rollout (phased)

- **Phase 1 (instant relief, config-only):** remove guard hook, revert SOUL to a nudge,
  set `approvals.mode: smart`, one gateway restart. Default feel + native approval
  immediately. Reversible.
- **Phase 2 (the durable fix):** build `hscc-mcp/server.py` (+ template), add
  `mcp_servers.hscc` to config, verify tools, restart, then rely on the tools.

## Verification

1. `hermes mcp` lists the `hscc` server; its tools appear in the agent tool list.
2. `hscc_fleet_activity` returns the same JSON as the CLI command.
3. `hscc_release_task` without `confirm:true` returns a refusal; with it, unblocks.
4. Guard removal: the old `gh repo list ... | grep ... || echo` case now runs (no block).
5. End-to-end: `hscc_dispatch_task` → (confirm) `hscc_release_task` lands a worker whose
   `HERMES_HOME` is the correct `worker-<n>` profile and whose inference hits the worker
   node, not .244.
6. Orchestrator vLLM on .244 untouched; gateway restarted exactly once per phase.

## Constraints honored

- No Hermes-core patches (`~/.hermes/hermes-agent/` untouched) — MCP server is an additive
  external plugin; config edits only.
- No sparkrun recipe edits.
- Dual-layout: active plugin + install template kept in sync.
- No destructive FS ops: guard hook is backed up via `mv`, not `rm`.
- No git commits unless the user explicitly asks.

## Open questions / follow-ups

- Tool argument surface for `hscc_task_add` (roadmap/subproject params) — mirror the
  `hscc-projects add-task` CLI signature exactly during implementation.
- Whether to also expose `hscc_provision` controls as tools, or leave provisioning to the
  existing `ensure_worker_vllm` path triggered by dispatch/release. Default: leave it;
  provisioning stays inside dispatch/release.
