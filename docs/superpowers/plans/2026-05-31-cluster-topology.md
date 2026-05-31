# Cluster Serving-Topology Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `~/.hscc/serving.json` the single declarative source of truth for serving topology — daemon reconciles orchestrator units + exempts their nodes from reaping + auto-updates worker base_url; coordinator routes dispatched tasks to worker units by free capacity, cold-starting nodes on demand.

**Architecture:** New `serving.json` read by both the daemon (`hscc-daemon/hscc.py`) and the agent-coordinator (`hscc-agent-coordinator/hscc.py`). Both fall back to today's hardcoded behavior when the file is absent/invalid. Pure, injectable core functions (`parse_worker_units`, `pick_unit`, `orchestrator_nodes`) are unit-tested with stdlib `unittest`. Dual-layout: every active-plugin edit mirrored to `install/hscc-plugins/<p>/`.

**Tech Stack:** Python 3 stdlib (json, re, os, subprocess), sparkrun CLI, hscc-provision plugin, Hermes profiles, stdlib `unittest`.

**Hard constraints (in force):** no gateway restart (until user says "si"); no sparkrun recipe edits; no Hermes-core patches; no destructive git/FS; commit only the listed files.

---

## File Structure

- `~/.hscc/serving.json` — NEW. Declarative serving topology (the lock). Data only.
- `hscc-agent-coordinator/hscc.py` — MODIFY. Add serving-topology read + capacity-greedy routing; keep `pick_worker_agent` as fallback.
- `install/hscc-plugins/hscc-agent-coordinator/hscc.py` — MIRROR.
- `hscc-daemon/hscc.py` — MODIFY. Read serving.json for orchestrator-node set (reaper exempt) + base_url auto-update; fallback to `PRIMARY_NODE`.
- `install/hscc-plugins/hscc-daemon/hscc.py` — MIRROR.
- `hscc-agent-coordinator/tests/test_serving.py` — NEW. Coordinator pure-core tests.
- `hscc-daemon/tests/test_serving.py` — NEW. Daemon pure-core tests.

---

## Task 1: serving.json schema reader + pure core (coordinator)

**Files:**
- Modify: `hscc-agent-coordinator/hscc.py` (after `pick_worker_agent`, ~line 1714)
- Test: `hscc-agent-coordinator/tests/test_serving.py`

Pure, injectable functions (no file I/O in the core so tests need no fixtures on disk):

- `SERVING_JSON = os.path.join(HSCC_DIR, "serving.json")`
- `load_serving(path=SERVING_JSON) -> dict | None` — read+parse; return None on missing/malformed (caller falls back).
- `parse_worker_units(serving) -> list[dict]` — `[{id, recipe, head, max_workers, model}]` for `role==worker`; `head = nodes[0]`; skip units with no nodes; `max_workers` default 4.
- `pick_unit(worker_units, unit_load) -> unit_id | None` — most-free-capacity (`max_workers - load`), ties broken by lowest head octet; None when every unit `free<=0`.

- [ ] **Step 1: Write failing tests** (`test_serving.py`):

```python
import importlib.util, os, sys
from pathlib import Path

def _load():
    p = Path(__file__).resolve().parent.parent / "hscc.py"
    spec = importlib.util.spec_from_file_location("coord_hscc", p)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m
H = _load()

def test_parse_worker_units_basic():
    s = {"version":1,"port":8000,"units":[
        {"id":"orch-244","role":"orchestrator","nodes":["192.0.2.10"],"recipe":"r","model":"m"},
        {"id":"worker-246","role":"worker","nodes":["192.0.2.11"],"recipe":"r","model":"m","max_workers":3}]}
    wu = H.parse_worker_units(s)
    assert [u["id"] for u in wu] == ["worker-246"]
    assert wu[0]["head"] == "192.0.2.11" and wu[0]["max_workers"] == 3

def test_parse_worker_units_default_cap():
    s = {"units":[{"id":"w","role":"worker","nodes":["192.0.2.12"],"recipe":"r","model":"m"}]}
    assert H.parse_worker_units(s)[0]["max_workers"] == 4

def test_pick_unit_most_free():
    wu = [{"id":"worker-246","head":"192.0.2.11","max_workers":4},
          {"id":"worker-247","head":"192.0.2.12","max_workers":4}]
    assert H.pick_unit(wu, {"worker-246":3,"worker-247":1}) == "worker-247"

def test_pick_unit_tie_lowest_octet():
    wu = [{"id":"worker-248","head":"192.0.2.13","max_workers":4},
          {"id":"worker-246","head":"192.0.2.11","max_workers":4}]
    assert H.pick_unit(wu, {}) == "worker-246"

def test_pick_unit_all_full():
    wu = [{"id":"w","head":"192.0.2.11","max_workers":2}]
    assert H.pick_unit(wu, {"w":2}) is None

def test_load_serving_missing(tmp_path=None):
    assert H.load_serving(path="/nonexistent/serving.json") is None
```

- [ ] **Step 2: Run, verify fail** — `cd hscc-agent-coordinator && python3 -m unittest tests.test_serving -v` → AttributeError (functions absent).

- [ ] **Step 3: Implement** the four functions in `hscc.py`. `pick_unit` sorts candidates by `(-free, head_octet)`.

- [ ] **Step 4: Run, verify pass.**

- [ ] **Step 5: Commit** `feat(hscc-coordinator): serving.json schema reader + capacity-greedy pick`.

## Task 2: Wire routing + capacity into dispatch/release/cancel/green-check

**Files:** Modify `hscc-agent-coordinator/hscc.py` — `ensure_worker_vllm` (recipe arg), `cmd_dispatch_task`, `cmd_release_task`, `cmd_cancel_task`, `cmd_green_check`; mirror to template.

- `ensure_worker_vllm(host, recipe=WORKER_VLLM_RECIPE, wait=False, timeout=300)` — use `recipe` in the provision call (back-compat default).
- New `pick_worker_unit() -> (unit_id, head, recipe) | (None,None,None)`: `load_serving`; if None → return None triple (caller falls back to `pick_worker_agent`). Else `parse_worker_units`, read `_unit_load` from bridge, `pick_unit`; bump `_unit_load[unit_id]`, save bridge.
- New `release_unit_slot(unit_id)`: decrement `_unit_load` (floor 0), save bridge.
- New `reconcile_unit_load()`: recount live (status held/released) bridge entries per `unit_id`, overwrite `_unit_load`. Called at start of `pick_worker_unit`.
- `cmd_dispatch_task`: when serving.json present and no override/assignee, call `pick_worker_unit`; store `unit_id`, `worker_host=head`, `recipe` in bridge entry; profile = `worker-<head octet>`; pass recipe to `ensure_worker_vllm`. When serving.json absent → existing `pick_worker_agent` path unchanged.
- `cmd_release_task`: pass entry `recipe` to `ensure_worker_vllm`; on `not ok` → `release_unit_slot(entry.unit_id)`.
- `cmd_cancel_task` + `cmd_green_check`: if entry has `unit_id`, `release_unit_slot`.

Steps: write tests for `release_unit_slot`/`reconcile_unit_load` (bridge injected via temp file by monkeypatching `BRIDGE_FILE`), run-fail, implement, run-pass, commit `feat(hscc-coordinator): capacity-tracked worker-unit routing`.

## Task 3: Daemon — orchestrator-node set + reaper exempt + base_url auto-update

**Files:** Modify `hscc-daemon/hscc.py`; test `hscc-daemon/tests/test_serving.py`; mirror to template.

- `SERVING_JSON`, `load_serving` (same as coordinator), `orchestrator_nodes(serving) -> set[str]` (union of nodes from `role==orchestrator`), `orchestrator_head(serving) -> str|None` (`nodes[0]` of first orchestrator unit).
- `resolve_cluster_config`: after cluster.json, if serving.json valid set module `ORCH_NODES` set + `PRIMARY_NODE = orchestrator_head` (keeps existing var meaning). Absent → `ORCH_NODES = {PRIMARY_NODE}` + loud log.
- `check_idle_monitor`: replace `if host == PRIMARY_NODE` with `if host in ORCH_NODES`.
- `update_worker_base_urls(head, port)`: for each `~/.hermes/profiles/worker-*/config.yaml`, if `model.base_url != http://<head>:<port>/v1` and `curl -sf http://<head>:<port>/v1/models` 200 → rewrite that line; skip+log on validation fail. Pure helper `compute_base_url_change(current, head, port)` for tests. Call once per reconcile tick.

Tests: `orchestrator_nodes` union, `orchestrator_head`, `compute_base_url_change` (no-op when equal / change when differ). Run-fail, implement, run-pass, commit `feat(hscc-daemon): serving.json reconcile + worker base_url auto-update`.

## Task 4: Write the lock (serving.json) + verify fallback

- [ ] Write `~/.hscc/serving.json` (orch-244 + worker-246/247/248, max_workers 4, port 8000, recipe = canonical local-fixed).
- [ ] Verify: coordinator `load_serving` returns it; `parse_worker_units` → 3 units; `pick_worker_unit` rotates by capacity. Daemon `orchestrator_nodes` → {.244}.
- [ ] Verify fallback: temporarily point `load_serving` at a bad path in a test → None → existing behavior. (No deletion of the live file.)
- [ ] Commit `feat(hscc): lock current serving topology in serving.json`.

## Task 5: Code review + safe integration verify

- [ ] Dispatch `code-reviewer` + `silent-failure-hunter` subagents on the diff. Fix high-confidence findings. Re-run all unit tests.
- [ ] Safe (no-restart) checks: `python3 -m unittest` green both plugins; `serving.json` parses live; `pick_worker_unit` rotation across 3 calls; `ensure_worker_vllm` healthy-path no-op (does NOT launch when endpoint already healthy / does nothing destructive).
- [ ] Document deferred-for-user: gateway restart, daemon restart to load new code, live cold-spin E2E (spins a worker vLLM), full dispatch→release E2E.
- [ ] Final commit if any fixes.

---

## Self-Review

- **Spec coverage:** Section 1 schema → Task 1+4. Section 2 reconcile/exempt/base_url → Task 3. Section 3 routing/capacity → Task 1(pick)+2(wire). Section 4 lock/migration → Task 4 + fallbacks in 1–3. Section 5 errors/testing → tests in every task + Task 5. Covered.
- **Placeholder scan:** none — all functions have signatures + behavior; tests have real assertions.
- **Type consistency:** `pick_unit(worker_units, unit_load)` and `parse_worker_units→{id,recipe,head,max_workers,model}` used consistently; `_unit_load` keyed by `unit_id` everywhere; `ensure_worker_vllm(host, recipe=..., wait, timeout)` signature stable across dispatch/release.
