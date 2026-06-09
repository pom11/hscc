# Phase 1 R2 Faulty-Business-Logic Review Results

## Finding 1: reap_orphans blast radius
**Risk**: If `serving.json` is empty/corrupt, `reap_orphans` would target ALL running containers, including the orchestrator on HEAD (.244).

**Ground truth**: `~/.hscc/serving.json` contains one unit: `orch-27b` on HEAD (.244). HEAD must NEVER be auto-killed.

**Resolution**: FIXED with defense-in-depth guard. Changed orphan filter from:
```python
orphans = [c for c in running if c["node"] not in serving]
```
to:
```python
orphans = [c for c in running if c["node"] not in serving and c["node"] != cl.HEAD]
```
This ensures HEAD is never targeted even if `serving.json` is missing/corrupt. Guard implemented at `heal.py:64`, test at `tests/test_heal.py:test_reap_never_touches_head`.

## Finding 2: pick_node fairness
**Observation**: `pick_node` returns `idle[0]` (first idle worker), which is acceptable for single-model provisioning scenarios.

**Potential enhancement**: Could skip degraded nodes (OOM history, ECC errors, NAS stale-mount). However, this adds complexity and the user's model strategy (1 orchestrator + 3 workers) does not require advanced load balancing.

**Resolution**: DOCUMENTED, no code change. Degraded-node avoidance is a FUTURE enhancement, not implemented in Phase 1.

## Finding 3: provision-then-idle-reaper race
**Observation**: `provision_model` does NOT verify the target node has assigned work before provisioning. If an agent provisions a model without assigning work, the idle-reaper could later kill it.

**Ground truth**: The user's HSCC daemon personality enforces "never provision without work" as a human/orchestrator responsibility. This is intentional design, not a bug.

**Resolution**: DOCUMENTED, no code change. Work assignment is an orchestrator responsibility enforced by personality rules, not plugin code.

## Finding 4: stop_model / restart_model self-decapitation
**Risk**: An agent could accidentally stop/restart the orchestrator on HEAD (.244), severing its own control plane.

**Resolution**: FIXED with HEAD-refuse-unless-force guards.
- `stop_model`: Guard at `ops.py:63-66`, tests at `tests/test_ops.py:test_stop_refuses_head_without_force` and `test_stop_head_with_force_executes`.
- `restart_model`: Guard at `heal.py:27-30`, test at `tests/test_heal.py:test_restart_refuses_head_without_force`.
- Schema updates: Added `"force": {"type": "boolean", "default": False}` to `STOP_MODEL_SCHEMA` and `RESTART_MODEL_SCHEMA` at `schemas.py:26,43`.

Both tools now refuse HEAD operations unless `force=true` is explicitly passed.

## Finding 5: confirm preview honesty (provision_model)
**Requirement**: When `node="auto"`, the preview `would_do` string must name the resolved node, not the placeholder "auto".

**Verification**: `ops.py:43-51` shows:
1. Lines 46-50: Resolve `auto` → actual node via `pick_node({})` BEFORE building action string.
2. Line 51: Build action string with resolved `node` value (not "auto").
3. Line 52-54: Pass action to `confirm_gate`, which returns preview if `confirm=False`.

**Result**: PASS. Preview is honest; it names the resolved node.

## Summary
- 3 guards implemented with TDD (reap/stop/restart HEAD protection).
- 2 findings documented as intentional design (pick_node fairness, provision-work-assignment).
- 1 finding verified PASS (provision preview honesty).
