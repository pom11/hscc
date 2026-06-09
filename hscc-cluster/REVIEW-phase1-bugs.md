# Phase 1 R1 Bug-Check Review Results

## 1. provision_model / restart_model timeout=900
**PASS** — `ops.py:55` provision_model uses `timeout=900`; `heal.py:33` restart_model uses `timeout=900` for sparkrun run.

## 2. run_cmd catches TimeoutExpired and FileNotFoundError
**PASS** — `clusterlib.py:19-22` catches `subprocess.TimeoutExpired` (→code 124) and `FileNotFoundError` (→code 127).

## 3. read_serving_units never raises on missing/corrupt/wrong-shape
**PASS** — `clusterlib.py:30-37` catches `FileNotFoundError` and `JSONDecodeError`, guards non-dict with `isinstance(data, dict)` check, returns `[]` on all failure modes.

## 4. model_health JSON parse wrapped in try/except
**PASS** — `ops.py:80-83` wraps `json.loads()` in try/except block; non-JSON body returns `served=None`, `reachable=False`, no traceback.

## 5. _running_by_node substring match safety
**PASS** — `ops.py:8-19` searches for IPs .244/.246/.247/.248; none is a prefix of another, so substring match is safe. **Note**: would break if a `.24` style prefix node were ever added (e.g., `192.0.2.24`).

## 6. All ssh_cmd paths use clusterlib.ssh_cmd with BatchMode/ConnectTimeout
**PASS** — `clusterlib.py:25-27` defines `ssh_cmd` with `-o BatchMode=yes -o ConnectTimeout=8`; all remote calls in `heal.py` and `debug.py` go through `cl.ssh_cmd`, ensuring fast-fail for key-rejecting QNAP.

## 7. stop_model/restart_model always pass explicit recipe AND --hosts
**PASS** — `ops.py:68` stop_model: `sparkrun stop {recipe} --hosts {node}`; `heal.py:32-33` restart_model: `sparkrun stop {recipe} --hosts {node}` and `sparkrun run {recipe} --hosts {node}`.

## Summary
All 7 R1 bug-check items PASS. No fixes required.

---

# Gate 1 Live Findings (caught what R1/R2 unit tests missed)

R1/R2 ran handlers with a single positional arg, so they never exercised the
real `registry.dispatch` call path. Live Gate 1 surfaced 3 defects:

## G1. CRITICAL — handler signature contract
`registry.dispatch(name, args, task_id=..., user_task=...)` forwards kwargs to
`handler(args, **kwargs)` (model_tools.py:986). All 13 handlers were
`def fn(args)` → `TypeError: unexpected keyword argument 'task_id'` at runtime.
**Fix:** added `**kwargs` to all 13 registered handlers (ops/debug/heal).
**Regression tests:** `test_handlers_accept_dispatch_kwargs` (live kwargs call)
and `test_all_registered_handlers_have_var_keyword` (introspects all 13).

## G2. faulty business logic — `_running_by_node` reported idle hosts as running
`sparkrun status` has an `Idle hosts (...)` section; old parser matched the IP
on those lines and recorded them as running with model=IP, so `idle_nodes` was
always `[]`. **Fix:** ops.py parser now tracks the `Idle hosts`/`Job:` sections
and records the recipe per running host. **Test:** `test_running_by_node_excludes_idle_section`,
`test_cluster_status_idle_and_units`.

## G3. faulty business logic — serving_units showed `[null]`
serving.json units key is `id` (`orch-27b`), code read `u.get("name")`.
**Fix:** `u.get("id") or u.get("name")`. Covered by `test_cluster_status_idle_and_units`.

## Live verification (read-only, no mutation)
`cluster_status` → head .244, running {.244: recipe}, idle [.246,.247,.248],
serving_units ['orch-27b']. `model_health` → reachable, served_model
`Qwen/Qwen3.6-27B-FP8`. Suite: 26 passed.
