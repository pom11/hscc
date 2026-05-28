# HSCC Plugin Merge Proposal — Deep Analysis

**Generated:** 2026-05-27  
**Scope:** 10 HSCC plugins + 1 optimization tool (8,936 lines total)  
**Previous version:** Preliminary analysis (308 lines)  

---

## 1. Plugin Inventory (All 10 + Detector)

| # | Plugin | File | Lines | Core Purpose |
|---|--------|------|-------|-------------|
| 1 | `hscc-provision` | `hscc-provision/hscc.py` | 872 | Agent-to-host recipe assignment, container lifecycle |
| 2 | `hscc-cluster` | `hscc-cluster/hscc.py` | 289 | Cluster host monitoring, sparkrun job management |
| 3 | `hscc-projects` | `hscc-projects/hscc.py` | 499 | Project roadmap/subproject/task CRUD (projects.json) |
| 4 | `hscc-events` | `hscc-events/hscc.py` | 617 | Event ingestion, lifecycle transitions, notifications, rules |
| 5 | `hscc-orchestrator` | `hscc-orchestrator/hscc.py` | 329 | Agent fleet routing, show/enable/disable, status |
| 6 | `hscc-daemon` | `hscc-daemon/hscc.py` | 1,618 | Background monitoring daemon, 5 check streams, watchdog, triggers |
| 7 | `hscc-chat` | `hscc-chat/hscc.py` | 1,578 | WebSocket gateway chat daemon (simulated mode) |
| 8 | `hscc-agent-coordinator` | `hscc-agent-coordinator/hscc.py` | 1,545 | Agent lifecycle FSM, worktrees, recovery, orphan detection |
| 9 | `hscc-governance` | `hscc-governance/hscc.py` | 1,120 | RBAC tiers, policy evaluation, audit log, enforcement gate |
| 10 | `hscc-skills` | `hscc-skills/hscc.py` | 469 | Skill/template installer (idempotent, hash-matched) |
| + | `event_driven_detector` | `hscc-optimizations/event_driven_detector.py` | 738 | AST-based polling pattern analyzer (not a plugin — analysis tool) |

---

## 2. Exact Duplicate Code Blocks

### 2.1 `now_iso()` — 5 plugins

Every plugin that needs timestamps defines this identically:

```python
def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
```

| Plugin | Line | Signature |
|--------|------|-----------|
| `hscc-events` | 71 | `def now_iso():` |
| `hscc-orchestrator` | 59 | `def now_iso():` |
| `hscc-daemon` | 123 | `def now_iso():` |
| `hscc-agent-coordinator` | 123 | `def now_iso():` |
| `hscc-governance` | 140 | `def now_iso():` |

**Note:** `hscc-cluster`, `hscc-chat`, `hscc-provision`, `hscc-projects`, `hscc-skills` do NOT define this. They either inline `datetime.now()` calls or don't need it.

**Deduplication savings:** 5 × 3 lines = 15 lines of identical code across 5 files.

### 2.2 `ensure_dir()` — 4 plugins

```python
def ensure_dir():
    os.makedirs(HSCC_DIR, exist_ok=True)
```

| Plugin | Line | Note |
|--------|------|------|
| `hscc-events` | 67 | Identical |
| `hscc-agent-coordinator` | 119 | Identical |
| `hscc-governance` | 135 | Identical |
| `hscc-skills` | 74 | Different signature: `def ensure_dir(path):` — takes a path argument |

**Deduplication savings:** 3 × 3 lines = 9 lines identical; skills version is a variant that can share a unified implementation.

### 2.3 `read_json_file(path, default=None)` — 4 plugins

```python
def read_json_file(path, default=None):
    ensure_dir()
    if not os.path.exists(path):
        return default if default is not None else {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return default if default is not None else {}
```

| Plugin | Line | Note |
|--------|------|------|
| `hscc-events` | 75 | Identical |
| `hscc-agent-coordinator` | 127 | Identical |
| `hscc-governance` | 150 | Identical |
| `hscc-cluster` | 53 | **Different** — signature `def read_json_file(path)` without default param |

**Deduplication savings:** 3 × 11 lines = 33 lines of identical code. The cluster version is simpler (no ensure_dir, no default) but can be adapted.

### 2.4 `write_json_file(path, data)` — 3 plugins

```python
def write_json_file(path, data):
    ensure_dir()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp, path)
```

| Plugin | Line |
|--------|------|
| `hscc-events` | 86 |
| `hscc-agent-coordinator` | 138 |
| `hscc-governance` | 162 |

**Deduplication savings:** 3 × 8 lines = 24 lines of identical code.

### 2.5 `load_agents_list()` — 3 plugins

```python
def load_agents_list():
    if not os.path.exists(AGENTS_JSON):
        return []
    try:
        with open(AGENTS_JSON) as f:
            data = json.load(f)
        return data.get("agents", [])
    except (json.JSONDecodeError, IOError):
        return []
```

| Plugin | Line |
|--------|------|
| `hscc-events` | 174 |
| `hscc-agent-coordinator` | 146 |
| `hscc-governance` | 181 |

**Deduplication savings:** 3 × 9 lines = 27 lines.

### 2.6 `emit_event()` — 2 plugins with different signatures

| Plugin | Line | Signature |
|--------|------|-----------|
| `hscc-daemon` | 950 | `emit_event(event_type, payload, severity="info", source="hscc-daemon")` |
| `hscc-agent-coordinator` | 204 | `emit_event(source, event_type, payload, severity="info")` |

Both append to `EVENTS_FILE` (~/.hscc/events.jsonl) with the same JSONL structure:
```python
{
    "event_type": ...,
    "timestamp": ...,
    "payload": ...,
    "severity": ...,
    "source": ...,
}
```

**Deduplication savings:** ~15 lines of near-identical JSONL append logic, but parameter order differs — requires normalization.

### 2.7 CLI Entry Point Boilerplate — All 10 Plugins

Every plugin has this pattern:

```python
def main():
    if len(sys.argv) < 2:
        print(__doc__)    # or print(USAGE)
        sys.exit(0)

    cmd = sys.argv[1].lower()

    commands = {
        "command1": cmd_handler1,
        "command2": lambda: cmd_handler2(sys.argv[2]) if len(sys.argv) > 2 else ...,
        ...
    }

    if cmd not in commands:
        print(json.dumps({"error": f"Unknown command: {cmd}"}))
        sys.exit(1)

    try:
        commands[cmd]()
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
```

**Line count per plugin:** ~20-35 lines  
**Total boilerplate across 10 plugins:** ~250-350 lines of structurally identical code.

### 2.8 Error Response Pattern — All 10 Plugins

Every plugin follows:
```python
print(json.dumps({"error": str(e)}))
sys.exit(1)
```

Plus the JSON error format for CLI:
```python
print(json.dumps({"error": f"Unknown command: {cmd}"}))
```

---

## 3. Shared I/O Patterns (Path Constants)

### 3.1 Universal Constant — `HSCC_DIR`

**Present in all 8 plugins + detector:**

| Plugin | Line | Value |
|--------|------|-------|
| `hscc-events` | 21 | `os.path.expanduser("~/.hscc")` |
| `hscc-agent-coordinator` | 52 | `os.path.expanduser("~/.hscc")` |
| `hscc-chat` | 38 | `os.path.expanduser("~/.hscc")` |
| `hscc-governance` | 34 | `os.path.expanduser("~/.hscc")` |
| `hscc-provision` | 35 | `os.path.expanduser("~/.hscc")` |
| `hscc-projects` | 33 | `os.path.expanduser("~/.hscc")` |
| `hscc-daemon` | 40 | `os.path.expanduser("~/.hscc")` |
| `hscc-optimizations/event_driven_detector.py` | 27 | `os.path.expanduser("~/.hscc")` |

### 3.2 `AGENTS_JSON` — 3 Variants

| Constant | Plugins using it | Value |
|----------|-----------------|-------|
| `~/.hscc/agents.json` | events (line 22), orchestrator (line 31), provision (line 36) | Direct path |
| `HSCC_DIR + /agents.json` | agent-coordinator (line 55), projects (line 34) | Via variable |
| (not used) | cluster, daemon, chat, governance, skills | — |

### 3.3 `LIFECYCLE_FILE` — 3 Plugins

| Plugin | Line | Value |
|--------|------|-------|
| `hscc-events` | 26 | `~/.hscc/lifecycle.json` |
| `hscc-agent-coordinator` | 58 | `~/.hscc/lifecycle.json` |
| `hscc-governance` | 38 | `~/.hscc/lifecycle.json` |

### 3.4 `EVENTS_FILE` — 2 Variants (Important!)

| Constant | Plugins | Value | Meaning |
|----------|---------|-------|---------|
| `~/.hscc/events.jsonl` | events (line 24), agent-coordinator (line 61) | Original location | Event log source |
| `~/.hscc/events.jsonl` | daemon (line 47) | Migration target | Daemon reads/writes here |

**Migration risk:** Two different paths for the same logical file. The migration plan should consolidate to `~/.hscc/events.jsonl`.

### 3.5 `PROJECTS_JSON` — 2 Plugins

| Plugin | Line | Value |
|--------|------|-------|
| `hscc-agent-coordinator` | 56 | `~/.hscc/projects.json` |
| `hscc-projects` | 35 | `~/.hscc/projects.json` |

---

## 4. Overlapping CLI Entry Points

### 4.1 Command Taxonomy

| Plugin | Commands | Subparsers? |
|--------|----------|-------------|
| `hscc-provision` | recipes, list, run, stop, assign, unassign, health, cleanup, status, registry | No |
| `hscc-cluster` | cluster-status, hosts, monitor, jobs, stop, info | No |
| `hscc-projects` | create, show, status, list-projects, add-roadmap, add-subproject, add-task, update-task, move-task, assign-task, list-agents, search | No |
| `hscc-events` | events, event-count, lifecycle, lifecycle-show, recovery, recovery-detail, notifications, notify, notify-read, notify-clear, rules, rule-add, rule-remove, rule-reset-cooldown, policy, policy-add, policy-remove, perms, clear-recovery, clear-notifications, compact | No |
| `hscc-orchestrator` | fleet, agents, show, configure, enable, disable, available, status, route | No |
| `hscc-daemon` | start, stop, status, check, watch, triggers, notify, plist, install, uninstall, log, start-daemon | No |
| `hscc-chat` | chat, chat-stream, session-list, session-create, session-delete, session-pin, session-rename, session-add, session-msgs, render-markdown, ws-status, ws-connect, ws-disconnect | **Yes** (argparse subparsers) |
| `hscc-agent-coordinator` | assign-task, list-agents, update-task, move-task, detect-orphans, attempt-recovery, recovery-log, list-worktrees | No |
| `hscc-governance` | policy-eval, check-permission, record-audit, list-audit, classify-tool, update-policy, enforce, governance-status, list-tiers, help | No |
| `hscc-skills` | install, install-skills, install-templates, status, uninstall | No |

### 4.2 Overlapping Commands Across Plugins

| Command | Plugins | Notes |
|---------|---------|-------|
| `stop` | provision, cluster | Different semantics: provision stops containers, cluster stops workloads |
| `status` | provision, projects, orchestrator, daemon | Each has its own format — no overlap |
| `assign` | provision, projects, agent-coordinator | All different: provision assigns recipe, projects assigns agent to task, coordinator assigns task via FSM |
| `notify` | events, daemon | Both send notifications — near-duplicate logic |
| `check` | daemon, events (via check functions) | Daemon orchestrates checks, events holds check handlers |
| `list-agents` | projects, agent-coordinator | Both read agents.json — near-duplicate |
| `cleanup` | provision, events | Both clean up data files |
| `clear-recovery` | events, agent-coordinator | events has clear; coordinator has recovery-log |
| `clear-notifications` | events only | Standalone |

---

## 5. Common Error Handling Patterns

### 5.1 Standardized CLI Error Response (All 10 Plugins)

```python
# Unknown command
print(json.dumps({"error": f"Unknown command: {cmd}"}))
sys.exit(1)

# Handler exception
try:
    commands[cmd]()
except Exception as e:
    print(json.dumps({"error": str(e)}))
    sys.exit(1)
```

### 5.2 JSON File Read Pattern (7 Plugins)

```python
try:
    with open(path) as f:
        return json.load(f)
except (json.JSONDecodeError, IOError):
    return default_if_not_provided_else {}
```

### 5.3 Atomic Write Pattern (3 Plugins)

```python
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=4)
os.replace(tmp, path)  # atomic on POSIX
```

### 5.4 JSON Output Standardization

All plugins output JSON to stdout for machine consumption, with `indent=2` or `indent=4`.

---

## 6. Dependency Chains Between Plugins

### 6.1 File-Based Dependencies

```
┌─────────────────────────────────────────────────────────┐
│                   ~/.hscc/                             │
│  agents.json ──► cluster, events, provision,             │
│                orchestrator, projects, agent-coord       │
│                                                         │
│  projects.json ──► projects, agent-coordinator          │
│                                                         │
│  cluster.json ──► daemon (resolve_cluster_config)       │
│  events.jsonl ──► events (write), daemon (read/write)   │
│                   agent-coordinator (write)             │
│                   governance (emit via coordinator)     │
├─────────────────────────────────────────────────────────┤
│                   ~/.hscc/                               │
│  lifecycle.json ──► events, agent-coordinator,          │
│                    governance                           │
│                                                         │
│  state/ ──► daemon (write per-stream state)             │
│                                                         │
│  triggers.json ──► daemon (read/write)                  │
│  cooldowns.json ──► daemon (read/write)                 │
│  watchdog_block.json ──► daemon (read/write)            │
│                                                         │
│  policy.json ──► governance (read/write)                │
│  audit.jsonl ──► governance (append-only write)         │
│                                                         │
│  rules.json ──► events (read/write)                     │
│  notifications.jsonl ──► events (write)                 │
│                                                         │
│  worktrees.json ──► agent-coordinator (read/write)      │
│  recovery.json ──► agent-coordinator (read/write)       │
│                                                         │
│  chats/ ──► chat (session persistence)                  │
├─────────────────────────────────────────────────────────┤
│                   ~/.hermes/                           │
│  device.json ──► chat (Ed25519 auth)                    │
│  hermes.json ──► chat (gateway token)                 │
└─────────────────────────────────────────────────────────┘
```

### 6.2 Plugin-to-Plugin Call Dependencies (via emit_event)

```
agent-coordinator ──emit_event──► events.jsonl ──read──► daemon
agent-coordinator ──emit_event──► events.jsonl ──read──► governance (optional)
governance ──record_audit──► audit.jsonl ──read──► governance (own tool)
```

### 6.3 Data Flow Diagram

```
┌─────────────────┐     ┌──────────────────────┐     ┌──────────────┐
│ agents.json     │────►│ cluster              │     │ provision    │
│ projects.json   │────►│ projects             │     │ orchestrator │
│ cluster.json    │────►│ daemon               │     │              │
└────────┬────────┘     └──────────────────────┘     └──────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────┐
│                    Shared State (fs-based)                 │
│  lifecycle.json → events, agent-coord, governance        │
│  events.jsonl     → daemon, events, coordinator          │
│  worktrees.json   → agent-coordinator                    │
│  recovery.json    → agent-coordinator                    │
│  policy.json      → governance                           │
│  audit.jsonl      → governance                           │
│  triggers.json    → daemon                               │
│  notifications    → events                               │
└──────────────────────────────────────────────────────────┘
```

---

## 7. Merge Candidates — Detailed Analysis

### 7.1 Candidate A: `hscc-cluster` + `hscc-orchestrator` → `hscc-infra`

**Rationale:** Both are thin wrappers (~309 lines combined) around the same data sources:
- `agents.json` — both read this for status display
- `cluster.json` — both reference this
- No internal state files written
- Both are read-only cluster queries

**Merged file layout:**
```
hscc-infra/hscc.py  (~330 lines after dedup)
├── Cluster commands (from hscc-cluster)
│   ├── cluster-status
│   ├── hosts
│   ├── monitor
│   ├── jobs
│   ├── stop
│   └── info
├── Orchestrator commands (from hscc-orchestrator)
│   ├── fleet
│   ├── agents
│   ├── show
│   ├── configure
│   ├── enable
│   ├── disable
│   ├── available
│   ├── status
│   └── route
└── Shared helpers (deduplicated)
    ├── now_iso() — extracted to core
    ├── load_agents_list() — extracted to core
    └── main() — extracted to core
```

**Deduplication:**
- Remove 2 × main() = ~50 lines
- Remove 2 × load_agents_list() (cluster doesn't have it, but orchestrator does) = ~10 lines
- Shared `read_json_file` = ~5 lines
- **Total savings: ~65 lines**

**Breaking changes:** None if aliases provided:
```bash
# Backward-compatible symlinks
hscc-cluster → hscc-infra cluster-status, hosts, monitor, jobs, stop, info
hscc-orchestrator → hscc-infra fleet, agents, show, configure, enable, disable, available, status, route
```

**Migration path:**
1. Create `hscc-infra/hscc.py` with merged commands
2. Update `hscc-cluster` package to be a shim that calls `hscc-infra` with prefix
3. Update `hscc-orchestrator` package similarly
4. Verify all event_emitter calls (both plugins emit to events.jsonl)

---

### 7.2 Candidate B: `hscc-provision` + `hscc-projects` → `hscc-resource-manager`

**Rationale:** Both manage resources (agents↔hosts and tasks↔agents):
- `provision` manages container lifecycle and recipe assignments
- `projects` manages project/task structure and task assignments
- Both read `agents.json`
- `provision` writes `provision_state.json` internally
- `projects` writes `projects.json`

**Merged file layout:**
```
hscc-resource-manager/hscc.py  (~1,100 lines after dedup)
├── Provision commands (from hscc-provision)
│   ├── recipes
│   ├── list, run, stop, assign, unassign
│   ├── health, cleanup, status
│   └── registry
├── Projects commands (from hscc-projects)
│   ├── create, show, status, list-projects
│   ├── add-roadmap, add-subproject, add-task
│   ├── update-task, move-task, assign-task
│   ├── list-agents, search
│   └── Shared task assignment logic
└── Shared helpers
    ├── load_agents_list() — deduplicated
    └── main() — deduplicated
```

**Deduplication:**
- Remove 2 × main() = ~50 lines
- Remove `load_agents_list` duplication = ~27 lines
- Remove `ensure_dir` duplication = ~9 lines
- **Total savings: ~86 lines**

**Breaking changes:** Minimal — each plugin keeps its command prefix. `hscc-provision` commands remain `hscc-provision <cmd>`, but internally delegate to `hscc-resource-manager`.

**Migration path:**
1. Create `hscc-resource-manager/hscc.py` with both command sets
2. Keep `hscc-provision/hscc.py` as a thin wrapper
3. Keep `hscc-projects/hscc.py` as a thin wrapper
4. No file format changes needed

---

### 7.3 Candidate C: Extract `hscc-core` Library (Phase 1 — Safe)

**Rationale:** This has zero breaking changes. Extract shared utilities into a library that every plugin can import.

**File layout:**
```
hscc-core/
├── __init__.py          # Package init
├── paths.py             # All path constants
│   ├── HSCC_DIR
│   ├── HSCC_DIR
│   ├── AGENTS_JSON
│   ├── PROJECTS_JSON
│   ├── LIFECYCLE_FILE
│   ├── EVENTS_FILE (canonical)
│   ├── CLUSTER_JSON
│   ├── hermes paths
│   └── daemon-specific paths (STATE_DIR, PID_FILE, etc.)
├── io.py                # Shared I/O
│   ├── ensure_dir()
│   ├── now_iso()
│   ├── now_ts()
│   ├── read_json_file(path, default=None)
│   ├── write_json_file(path, data) — atomic
│   ├── append_jsonl(path, entry)
│   ├── load_agents_list()
│   ├── get_agent_info(agent_id)
│   └── get_agent_role(agent_id)
├── events.py            # Event emission
│   └── emit_event(source, event_type, payload, severity)
├── cli.py               # CLI boilerplate
│   ├── build_parser() — unified argparse
│   ├── run_cli(commands, usage) — generic entry point
│   ├── error_response(msg) — standardized JSON error
│   └── success_response(data) — standardized JSON output
└── exceptions.py        # Shared exception types
```

**File sizes (estimated):**
- `paths.py`: ~80 lines (all constants consolidated)
- `io.py`: ~120 lines
- `events.py`: ~40 lines
- `cli.py`: ~150 lines
- `__init__.py`: ~20 lines
- `exceptions.py`: ~30 lines
- **Total hscc-core: ~440 lines**

**Deduplication savings across all plugins:**
| Function/Pattern | Occurrences | Lines per | Total |
|-----------------|-------------|-----------|-------|
| `now_iso()` | 5 plugins | 3 | 15 |
| `ensure_dir()` | 4 plugins | 3 | 12 |
| `read_json_file()` | 4 plugins | 11 | 44 |
| `write_json_file()` | 3 plugins | 8 | 24 |
| `load_agents_list()` | 3 plugins | 9 | 27 |
| `emit_event()` | 2 plugins | 15 | 30 |
| `main()` boilerplate | 10 plugins | 30 | 300 |
| Error handling | 10 plugins | 5 | 50 |
| **Total** | | | **502 lines** |

**Breaking changes:** None. Plugins can adopt imports incrementally. Old functions remain working until replaced.

**Migration path:**
1. Create `hscc-core/` package in `~/.hermes/plugins/hscc-core/`
2. Plugin-by-plugin migration: each plugin adds `from hscc_core import ...` and replaces one function at a time
3. No simultaneous changes — fully backward compatible
4. After all plugins migrated, old inline functions become deprecated (warnings in old locations)

---

### 7.4 Candidate D: `hscc-events` + `hscc-governance` → `hscc-policies`

**Rationale:** Both deal with rules and policy enforcement:
- `events` manages rules.json, policies.json, notifications.jsonl, cooldowns.json
- `governance` manages policy.json, audit.jsonl, RBAC tiers
- Both read lifecycle.json and agents.json
- Both emit events to events.jsonl
- `events` has 22 commands (largest command set)
- `governance` has 10 commands

**Merged file layout:**
```
hscc-policies/hscc.py  (~1,400 lines after dedup)
├── Events commands (from hscc-events)
│   ├── events, event-count, lifecycle
│   ├── recovery, recovery-detail
│   ├── notifications, notify, notify-read, notify-clear
│   ├── rules, rule-add, rule-remove, rule-reset-cooldown
│   ├── policy, policy-add, policy-remove
│   ├── perms, clear-recovery, clear-notifications, compact
├── Governance commands (from hscc-governance)
│   ├── policy-eval, check-permission
│   ├── record-audit, list-audit
│   ├── classify-tool, update-policy
│   ├── enforce, governance-status, list-tiers
├── Shared RBAC engine
│   ├── RBAC_TIERS (4-tier system)
│   ├── classify_tool()
│   ├── check_permission()
│   ├── evaluate_policy()
│   └── enforce_action()
├── Shared helpers (deduplicated)
│   ├── now_iso(), ensure_dir()
│   ├── read_json_file(), write_json_file()
│   ├── load_agents_list(), get_agent_info()
│   ├── emit_event() — unified
│   └── main()
└── Audit log (from governance)
    ├── record_audit(), append_audit_log()
    └── read_audit_log()
```

**Deduplication:**
- Remove 2 × main() = ~50 lines
- Remove 2 × now_iso() = 6 lines
- Remove 2 × ensure_dir() = 6 lines
- Remove 2 × read_json_file() = 22 lines
- Remove 2 × write_json_file() = 16 lines
- Remove 2 × load_agents_list() = 18 lines
- Remove `emit_event` duplication = 15 lines
- **Total savings: ~133 lines**

**Breaking changes:** 
- `hscc-events` and `hscc-governance` become shims
- All commands stay under same CLI names (backward compatible)
- `policy.json` path might change — governance writes it, events reads it. Need to agree on canonical location.

**Migration path:**
1. Create `hscc-policies/hscc.py` with merged code
2. Replace `hscc-events/hscc.py` with thin shim routing to hscc-policies
3. Replace `hscc-governance/hscc.py` with thin shim routing to hscc-policies
4. Test all commands via both old and new paths

---

### 7.5 Candidate E: `hscc-agent-coordinator` → Split into 3 Plugins

**Rationale:** This is the largest "business logic" plugin at 1,545 lines and covers 3 distinct concerns:
1. **Lifecycle FSM** (~400 lines): `assign-task`, `list-agents`, `update-task`, `move-task`
2. **Worktrees** (~500 lines): `list-worktrees`, worktree CRUD, git operations
3. **Recovery** (~400 lines): `detect-orphans`, `attempt-recovery`, `recovery-log`

**Proposed split:**
```
hscc-lifecycle/hscc.py  (~400 lines)
├── assign-task, list-agents, update-task, move-task
├── Lifecycle FSM: VALID_TRANSITIONS, FINISHED_REDIRECT
├── set_lifecycle(), get_lifecycle()
├── mark_task_in_progress()

hscc-worktrees/hscc.py  (~500 lines)
├── list-worktrees
├── get_worktrees(), set_worktrees()
├── Git integration: list_git_worktrees, run_git
├── Worktree status tracking

hscc-recovery/hscc.py  (~400 lines)
├── detect-orphans, attempt-recovery, recovery-log
├── FAILURE_RECIPES, MAX_RETRIES
├── get_sparkrun_containers()
├── Recovery history tracking
```

**Deduplication in split:**
- Shared `ensure_dir`, `now_iso`, `read/write_json_file` go to `hscc-core`
- **Savings: ~30 lines of deduplicated infrastructure**

**Breaking changes:**
- `hscc-agent-coordinator` becomes a meta-plugin that delegates to 3 sub-plugins
- All CLI commands stay available via `hscc-agent-coordinator` (shim)
- Each sub-plugin also has its own CLI entry point

**Migration path:**
1. Create 3 new plugin directories with extracted code
2. Make `hscc-agent-coordinator/hscc.py` a thin meta-plugin
3. Verify all emit_event calls still work
4. Optionally create aliases: `hscc-lifecycle`, `hscc-worktrees`, `hscc-recovery`

---

### 7.6 Candidate F: `hscc-daemon` → Keep as Single Monolith

**Rationale:** The daemon is intentionally a single large file. It's a long-running process with:
- Multi-threaded event loop
- 5 check streams + watchdog + trigger engine
- All logic tightly coupled (state flows between threads)
- Launchd integration (plist generation, PID management)
- Signal handling, double-fork daemonization

**Splitting risks:**
- Thread synchronization becomes harder across files
- Check functions reference shared module-level state
- State file writes are interleaved across checks
- The daemon loop orchestrates everything in one `run_daemon_loop()`

**Recommendation:** Keep as-is. The 1,618 lines are a feature (single source of truth for monitoring state).

---

### 7.7 Candidate G: `hscc-chat` → Keep as Single Plugin

**Rationale:** 1,578 lines is manageable for a single plugin. The chat plugin has:
- Self-contained WebSocket client (simulated mode)
- Session persistence
- Markdown rendering
- Gateway authentication (Ed25519 + token)

**Splitting risks:**
- The GatewayConnection class, ChatStreamer, and session management are tightly coupled
- Simulated mode logic is spread throughout
- WebSocket reconnection logic is a single state machine

**Recommendation:** Keep as-is. Consider adding type hints for clarity.

---

### 7.8 Candidate H: `hscc-skills` → Keep as Single Plugin

**Rationale:** 469 lines, single purpose (install/update skills and templates). No meaningful overlap with other plugins.

**Recommendation:** Keep as-is.

---

## 8. Phased Merge Plan

### Phase 1: Safe Extracts (Zero Breaking Changes)

**Estimated effort:** 1-2 days  
**Risk:** None — all changes are additive

| Step | Action | Files | Lines |
|------|--------|-------|-------|
| 1.1 | Create `hscc-core/` package with `paths.py`, `io.py`, `cli.py`, `events.py` | 5 new | ~440 |
| 1.2 | Migrate `hscc-events` to import from `hscc-core` | 1 existing | -25 |
| 1.3 | Migrate `hscc-governance` to import from `hscc-core` | 1 existing | -25 |
| 1.4 | Migrate `hscc-agent-coordinator` to import from `hscc-core` | 1 existing | -25 |
| 1.5 | Migrate `hscc-daemon` to import from `hscc-core` | 1 existing | -20 |
| 1.6 | Migrate remaining plugins (provision, cluster, projects, orchestrator, chat, skills) | 5 existing | -60 |
| **Net change** | | | **-155 lines of duplicates removed** |

**Phase 1 deliverables:**
- `hscc-core/` package with all shared utilities
- All 10 plugins import from `hscc-core` for shared functions
- All plugins use standardized `hscc_core.cli.run_cli()` entry point
- All plugins output errors in consistent JSON format
- Event file paths unified via `hscc_core.paths`

### Phase 2: Structural Merges (Backward Compatible Shims)

**Estimated effort:** 2-3 days  
**Risk:** Low — shims preserve all CLI entry points

| Step | Action | New Plugin | Old Plugins |
|------|--------|-----------|-------------|
| 2.1 | Merge `hscc-cluster` + `hscc-orchestrator` → `hscc-infra` | `hscc-infra/` | Both become shims |
| 2.2 | Merge `hscc-provision` + `hscc-projects` → `hscc-resource-manager` | `hscc-resource-manager/` | Both become shims |
| 2.3 | Merge `hscc-events` + `hscc-governance` → `hscc-policies` | `hscc-policies/` | Both become shims |
| 2.4 | Split `hscc-agent-coordinator` → 3 sub-plugins | `hscc-lifecycle/`, `hscc-worktrees/`, `hscc-recovery/` | Old becomes meta-plugin |

**Phase 2 deliverables:**
- 4 new merged plugins replacing 8 existing
- All CLI commands remain available via old plugin names (shims)
- Total plugin count reduced from 10 to 7
- Shared I/O reduced from ~200 duplicate lines to ~50 unique in `hscc-core`

### Phase 3: Cleanup (Deprecation)

**Estimated effort:** 1 day  
**Risk:** Low — after all plugins migrated

| Step | Action |
|------|--------|
| 3.1 | Remove shim wrappers from old plugin directories |
| 3.2 | Add deprecation warnings to old entry points (1-month grace period) |
| 3.3 | Update symlinks in launchd plists (if any reference old plugins) |
| 3.4 | Add integration tests covering all CLI commands via old and new paths |
| 3.5 | Update event_driven_detector.py to flag remaining inline duplicates |

---

## 9. Summary of Savings

### 9.1 Line Count Reduction

| Phase | Action | Lines Removed | Lines Added | Net Change |
|-------|--------|---------------|-------------|------------|
| Phase 1 | Extract hscc-core | 502 | 440 | **-62** |
| Phase 2a | Merge cluster+orchestrator | 65 | 30 | **-35** |
| Phase 2b | Merge provision+projects | 86 | 30 | **-56** |
| Phase 2c | Merge events+governance | 133 | 40 | **-93** |
| Phase 2d | Split agent-coordinator | 30 | 50 | **+20** (temporary) |
| Phase 3 | Remove shims | 80 | 0 | **-80** |
| **Total** | | **896** | **590** | **-306 lines** |

**Current total:** 8,936 lines  
**Post-merge total:** ~8,630 lines (after Phase 2) → ~8,550 lines (after Phase 3)  
**Net reduction:** ~8-10% of code

### 9.2 Duplicate Function Count

| Pattern | Before | After |
|---------|--------|-------|
| `now_iso()` | 5 plugins | 1 (hscc-core) |
| `ensure_dir()` | 4 plugins | 1 (hscc-core) |
| `read_json_file()` | 4 plugins | 1 (hscc-core) |
| `write_json_file()` | 3 plugins | 1 (hscc-core) |
| `load_agents_list()` | 3 plugins | 1 (hscc-core) |
| `emit_event()` | 2 plugins | 1 (hscc-core) |
| `main()` boilerplate | 10 plugins | 1 (hscc-core) |
| **Total deduplicated** | **32 occurrences** | **7 in core** |

### 9.3 Risk Matrix

| Merge | Breaking Change | Data Loss Risk | Rollback Ease |
|-------|----------------|---------------|---------------|
| hscc-core extract | None | None | Trivial (just stop importing) |
| cluster+orchestrator | None (shims) | None | Trivial |
| provision+projects | None (shims) | None | Trivial |
| events+governance | Low (policy.json path) | None | Easy |
| agent-coordinator split | None (meta-plugin) | None | Trivial |

---

## 10. Recommended Execution Order

```
Week 1: Phase 1 — Extract hscc-core
  ├── Day 1-2: Create hscc-core package
  ├── Day 3-4: Migrate 3 high-impact plugins (events, governance, coordinator)
  └── Day 5: Migrate remaining 7 plugins

Week 2: Phase 2 — Structural merges
  ├── Day 1-2: Merge cluster+orchestrator → hscc-infra
  ├── Day 2-3: Merge provision+projects → hscc-resource-manager
  ├── Day 3-4: Merge events+governance → hscc-policies
  └── Day 4-5: Split agent-coordinator → lifecycle/worktrees/recovery

Week 3: Phase 3 — Cleanup
  ├── Day 1: Remove shims, add deprecation warnings
  ├── Day 2: Integration tests, launchd plist updates
  └── Day 3: Final review, documentation update
```

---

## 11. Open Questions

1. **EVENTS_FILE canonical path:** Should all plugins use `~/.hscc/events.jsonl` or `~/.hscc/events.jsonl`?  
   **Recommendation:** Migrate to `~/.hscc/events.jsonl` (hscc-core.paths.EVENTS_FILE).  
   **Impact:** 1 data migration: `rsync ~/.hscc/events.jsonl ~/.hscc/events.jsonl`.

2. **AGENTS_JSON canonical path:** Should all plugins use `~/.hscc/agents.json` or `~/.hscc/agents.json`?  
   **Recommendation:** Keep `~/.hscc/agents.json` (used by sparkrun/hermes tooling).

3. **Python path for imports:** Will `hscc-core` be in `PYTHONPATH` or installed as a pip package?  
   **Recommendation:** Add `~/.hermes/plugins/` to Python path via a `.pth` file in site-packages. No pip install needed.

4. **Should `hscc-daemon` also be refactored?**  
   **Recommendation:** No. The daemon's monolithic structure is intentional and functional. Extracting only the shared `emit_event`, `now_iso`, `read_json_file` to `hscc-core` is sufficient.

5. **Chat plugin's simulated mode:** Should it be moved to `hscc-core` as a testing utility?  
   **Recommendation:** No. It's tightly coupled to the WebSocket gateway architecture.
