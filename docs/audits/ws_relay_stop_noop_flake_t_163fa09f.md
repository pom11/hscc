# t_163fa09f — p313 collection-order flake: `test_ws_relay_not_noop.py::test_stop_kind_with_nothing_in_flight_is_noop`

Status: IN PROGRESS (this file is updated as findings land).

## Task restatement

The stop-no-op test fails when the FULL hscc-api suite runs on the p313
interpreter, and passes in isolation. Prior partial fix (t_7cc2e7d5) made the
directory-wide autouse fixture `_isolate_chat_jobs` empty the module-global
`routes_orchestrator._jobs` before *and* after every test — the flake still
manifests on p313 full runs.

Deliverables: (1) deterministic reproduction of the polluting order,
(2) airtight fixture isolation of ALL process-global relay/job state,
(3) hermetic regression test that runs the pair in the polluting order and
proves isolation, (4) full suite green twice on BOTH interpreters
(hermes venv py3.11 + miniconda p313 py3.13).

## Code map (read, with line refs at HEAD 59788b5)

- `hscc-api/tests/test_ws_relay_not_noop.py::test_stop_kind_with_nothing_in_flight_is_noop`
  — asserts the `{kind:stop}` ack carries `stopped: False`.
- `hscc-api/routes_ws.py:457` `_handle_client_stop` → `stopped = bool(interrupt_user_turn(project))`.
- `hscc-api/routes_ws.py:366` `_interrupt_relay` → `_ro._in_flight_job(project)`.
- `hscc-api/routes_orchestrator.py:1861` `_in_flight_job` — scans the
  process-global `_jobs` (1288) for a job with this project and
  `finished_at is None`.
- `hscc-api/routes_ws.py:310` `_default_relay._work` — runs on a **daemon
  background thread** (`ws-relay-<project>`), and it is the thing that creates
  the job via `_ro._new_job` (routes_orchestrator.py:1402).

## Working hypothesis (to be proven by execution)

`_isolate_chat_jobs` clears `_jobs` at setup and at teardown, but the relay's
job is created on a **thread that outlives the test**. Sequence:

1. test T calls `_default_relay("hscc", ...)` → spawns thread `ws-relay-hscc`;
2. T's assertions finish (often via the polling `_wait_for`, sometimes with a
   fixed `time.sleep(0.05)`, e.g. test_project_permanent_session.py:247) and T
   ends while the thread is still before `_new_job` / inside
   `_ensure_session_exists`;
3. teardown clears `_jobs` — nothing there yet;
4. the next test's setup clears `_jobs` again;
5. the stray thread NOW calls `_new_job` → a live job for project `"hscc"`
   exists inside the stop-no-op test;
6. `_in_flight_job("hscc")` returns it → `stopped: True` → assertion fails.

Clearing a dict cannot fix a leak whose writer is an unjoined thread; the
isolation has to make the thread quiesce (signal + join) BEFORE the clear.

## Findings

_(append as proven)_

## Fix

_(append)_

## Verification

_(append: interpreter, command, pass counts, run #1/#2)_
