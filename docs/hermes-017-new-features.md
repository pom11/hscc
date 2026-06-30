# hermes-agent 0.17.0 — New Features for HSCC

**Source:** diff between `298bb93d3` (prev runtime) → `885e80df7` (v0.17.0)
**Commits:** 2,503 (317 feat, 229 fix, 46 perf, 4 refactor, 41 docs, 41 test, 46 chore)

---

## 1. Kanban Lifecycle Hooks (BREAKING CHANGE + NEW)

### What changed

The kanban hooks were restructured from a single `pre_kanban_dispatch` hook to a **three-event lifecycle**:

| Hook | When it fires | Passed kwargs |
|------|---------------|---------------|
| `kanban_task_claimed` | **Every** task claim (first claim AND re-claims) | `task_id`, `profile_name` |
| `kanban_task_completed` | Worker calls `complete_task()` | `task_id`, `profile_name`, `summary` |
| `kanban_task_blocked` | Worker or dispatcher blocks a task | `task_id`, `profile_name`, `reason` |

The old `pre_kanban_dispatch` hook was removed entirely.

### How we use it

- **Resume notes on re-dispatch:** The old `on_pre_kanban_dispatch` handler posted a "resume, don't redo" comment from the task branch's committed state. We already ported this to `kanban_task_claimed` — but note: it now fires on **every** claim, not just re-claims. The handler must check branch state to decide if a note is needed (which the HSCC code already does).
- **Completion tracking:** New `kanban_task_completed` hook lets us log when workers finish tasks — useful for HSCC's own task metrics, auto-unblocking dependencies, or triggering downstream kanban rows.
- **Block reason visibility:** New `kanban_task_blocked` hook surfaces the block reason — useful for HSCC's dashboard, alerting on stuck tasks, or auto-retry policies.

---

## 2. Plugin Context & API

### `ctx.profile_name` — new plugin context property

```python
# Plugins can now read the active profile name without importing profiles
@ctx.register_hook("pre_tool_call")
def _hook(task_id, tool_name, **kw):
    profile = ctx.profile_name  # new!
    ...
```

### How we use it

- HSCC's hook handlers already work with board slugs, but `ctx.profile_name` lets us correlate which profile's gateway fired a hook — useful for multi-profile clusters or debugging.

### `ctx.profile_name` and session-agnostic access

Previously, some plugin context was session-scoped. The new `profile_name` property accesses the active profile via `hermes_cli.profiles.get_active_profile_name()` — session-agnostic, works in gateway background threads.

---

## 3. Gateway & Session

### AsyncSessionDB offload

Session database operations (reads/writes) are now offloaded to a background thread pool (`AsyncSessionDB` facade). This was a major refactor to prevent blocking the gateway's event loop.

### How we use it

- **No direct HSCC impact** — this is internal to the gateway. But it means fewer "session lock" errors under load, which was a known issue in HSCC's dispatcher.

### Gateway multiplexing (phases 0–4)

The gateway now supports **multiple profiles on a single gateway process**:

- **Phase 0:** config flag, profile enumeration, profile-stamped session keys
- **Phase 1:** HTTP-inbound `/p/<profile>/` routing
- **Phase 2:** fail-closed profile credential isolation
- **Phase 3:** secondary-profile adapter registry + conflict detection
- **Phase 4:** lifecycle guard + per-profile observability

### How we use it

- HSCC's orchestrator model runs on one profile. If we add worker models as secondary profiles, this infrastructure supports it.
- Per-profile session isolation means HSCC worker profiles can run independently without interfering with the orchestrator's session state.

### Scale-to-zero idle detection

The gateway can now detect idle periods and quiesce — effectively "scale to zero" for cost savings on hosted deployments.

### How we use it

- **Not relevant for HSCC.** HSCC's gateway runs 24/7 on dedicated hardware. But the "dormant-quiesce" config option means we can opt-in to idle detection if we wanted.

---

## 4. Kanban Core Improvements

### Handoff freshness stamp

Tasks now carry a "freshness" stamp so workers don't read stale state as current. This prevents race conditions where a worker acts on an outdated claim.

### Typed block reasons + unblock-loop breaker

Task blocks now have a typed `reason` field (was a plain string). An "unblock-loop breaker" prevents tasks from being unblocked by the same process that blocked them — useful for HSCC's crash-recovery.

### Linked project worktrees

Tasks can now be linked to project worktrees (not just repo worktrees). This is a new kanban data model field.

### HSCC impact

- **Handoff freshness** → fewer stale-task race conditions for HSCC workers
- **Typed block reasons** → HSCC can distinguish between "crash", "review needed", "blocked by dependency" etc.
- **Project worktrees** → HSCC's task branches live in repo-level worktrees, not project worktrees. No impact.

---

## 5. Dashboard & Profile Management

### Multi-profile dashboard

The dashboard now has a "unified multi-profile management" UI — one machine dashboard with a global profile switcher, profile-scoped skills & toolsets, and a full profile builder (model + skills + MCPs).

### HSCC impact

- The dashboard UI changes are cosmetic. HSCC's plugin system is CLI/daemon-based.
- However, the **per-profile toolsets** management means we can configure separate toolsets for orchestrator vs worker profiles in the future.

### Session switcher panel

The dashboard's Chat tab now has a session switcher panel (like a sidebar history).

### HSCC impact

- Not directly relevant. HSCC doesn't use the dashboard for task management.

---

## 6. Cron System Overhaul

### CronScheduler ABC + InProcessCronScheduler

The cron system was refactored from a simple scheduler into an **abstract provider model**:

```python
class CronScheduler(ABC):
    @abstractmethod
    async def on_jobs_changed(self): ...
    @abstractmethod
    async def fire_due(self, job_id): ...
    @abstractmethod
    async def reconcile(self): ...
```

### Chronos NAS-mediated managed-cron provider

A new `Chronos` cron provider uses NAS storage (like HSCC's existing NAS infrastructure) to coordinate cron execution across multiple machines.

### HSCC impact

- **Chronos is directly relevant** — HSCC already uses NAS for shared state. The Chronos provider could be a future HSCC integration for coordinated cron execution across multiple worker nodes.
- The ABC refactoring means plugins can write custom cron providers — useful if HSCC wants its own task scheduler.

### Cron Recipes & Suggested Cron Jobs

New CLI/Dashboard features:
- `/cron-recipe <name>` — seeds a conversational fill for cron jobs
- "Suggested Cron Jobs" — one surface for proposed automations

### HSCC impact

- HSCC uses cron for health checks and periodic tasks. The new CronScheduler ABC doesn't change HSCC's existing usage — the InProcessCronScheduler is still available.

---

## 7. MCP & ACP

### Late-connecting MCP tools

MCP tools are now discovered asynchronously. "Late-connecting" MCP servers (those that connect after the agent starts) now have their tools exposed to the agent without a restart.

### How we use it

- If HSCC workers use MCP servers that are slow to start, this means tools become available automatically.

### Thread-safe interactive approval

ACP (Agent Control Protocol) interactive approval now uses `contextvars` for thread safety — fixes a race condition where approval prompts could be sent to the wrong thread.

### How we use it

- HSCC workers using ACP (Copilot CLI) will have more reliable approval flows.

### Short-TTL HTTP sessions

MCP HTTP sessions now have configurable keepalive pings, preventing TTL-based connection drops.

---

## 8. Agent Tools & Coding Context

### Configurable coding_instructions

Agents can now receive custom `coding_instructions` that are injected into the system prompt. This replaces the old hardcoded "coding posture" behavior.

### Pre-verify hook + verify-on-stop

New agent-level hook: `pre_verify` — fires before coding verification starts. This is a **new plugin hook** for policy decisions about coding tasks.

### Coding-context posture

The agent now has a structured "coding-context posture" that works across CLI, TUI, desktop, and ACP. This exposes project facts (file structure, git state, etc.) as structured data.

### How we use it

- HSCC's worker tasks are coding-focused. The new `coding_instructions` field lets us customize worker behavior per task (e.g., "use pytest", "don't modify config.yaml").
- The `pre_verify` hook could be used by HSCC to inject coding guidelines before verification runs.

---

## 9. CLI & Desktop

### `/prompt` — compose next prompt in $EDITOR

New CLI command: `/prompt` opens the user's `$EDITOR` to compose the next prompt, then submits it.

### `/reasoning full` — show complete thinking

New CLI command that shows the agent's complete thinking (not the 10-line clamp).

### HSCC impact

- Cosmetic — not relevant for HSCC's automation.

---

## 10. What's NOT Changing for HSCC

| Area | Status | Notes |
|------|--------|-------|
| `kanban_db.connect()` | ✅ Same | Signature unchanged |
| `kanban_db.get_task()` | ✅ Same | Signature unchanged |
| `kanban_db.add_comment()` | ✅ Same | Signature unchanged |
| `PluginContext.register_hook()` | ✅ Same | Signature unchanged |
| `PluginContext.register_tool()` | ✅ Same | Signature unchanged |
| Plugin activation mechanism | ✅ Same | `__init__.py` still runs at load |
| HSCC's `workflow.py` hooks | ✅ Safe | Ported to `kanban_task_claimed` |
| Bootstrap script | ✅ Same | `bootstrap.sh` unchanged (except comment update) |

---

## 11. How HSCC Can Use These Features

### Immediate (already done)
- [x] Port `pre_kanban_dispatch` → `kanban_task_claimed` ✅
- [x] Update `__init__.py` registration ✅

### Medium-term (nice to have)
- [ ] Hook `kanban_task_completed` for HSCC task metrics
- [ ] Hook `kanban_task_blocked` for auto-retry policies
- [ ] Use `ctx.profile_name` in multi-profile scenarios
- [ ] Leverage Chronos NAS provider for cross-node cron coordination
- [ ] Use `pre_verify` hook for coding policy injection

### Long-term (if we expand)
- [ ] Multi-profile gateway (orchestrator + workers on same gateway)
- [ ] Custom CronScheduler provider for HSCC's dispatcher
- [ ] Project worktrees (if we shift from repo-level to project-level kanban)

---

## 12. Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| `pre_kanban_dispatch` removed | **HIGH** (blocking) | Ported in HSCC PR #7 ✅ |
| `kanban_task_claimed` fires on every claim | MEDIUM | HSCC handler checks branch state before posting note |
| AsyncSessionDB changes | LOW | Internal refactor; no API surface change |
| Gateway multiplexing complexity | LOW | HSCC still uses single-profile gateway |