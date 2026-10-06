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

### F1 — ROOT CAUSE, PROVEN BY EXECUTION (probe plugin, p313, full hscc-api dir)

Instrumented `_new_job` / `_in_flight_job` / `_run_job` / `_jobs.clear()` with the
thread name + the test that was executing at that moment (`-p jobprobe`, full
directory run: 856 passed, 16 skipped, log 3766 lines). The log shows the leak
directly. `test_client_send_busy_echoes_user_and_emits_handoff_pending`
(test_ws_handoff_path.py) leaves a relay thread alive that registers a job
belonging to NO live test, and that thread is still running three tests later:

```
32281.980 | ws-relay-hscc | NEW_JOB      | test_client_send_busy_echoes_user_and_... | project=hscc id=chat-31
32281.980 | MainThread    | JOBS.CLEAR   | test_client_send_busy_echoes_user_and_... | was=[('chat-31','hscc','queued')]
32281.980 | ws-relay-hscc | RUN_JOB-ENTER| ...                                            | id=chat-31          <- blocks ~240 ms
   ...  (test_relay_actually_reaches_the_orchestrator, test_relay_failure_is_surfaced_not_swallowed,
        test_client_send_path_produces_a_reply_end_to_end all run in between) ...
32282.219 | ws-relay-hscc | RUN_JOB-EXIT | test_stop_kind_while_turn_in_flight_cancels_and_acks | id=chat-31 status=error
```

`NEW_JOB` for `chat-31` lands **after** the clear that the boundary fixture just
performed, and the job sits in the store as `('hscc','running',True)` while
later tests run. `_in_flight_job("hscc")` (routes_orchestrator.py:1861) returns
any job with `project == "hscc"` and `finished_at is None` — so whenever that
phantom is still live when `test_stop_kind_with_nothing_in_flight_is_noop` runs,
`_handle_client_stop` (routes_ws.py:457) acks `stopped: True` and the test fails;
when the straggler has already landed a terminal state, the same test passes.
That is the whole flake: the outcome depends on where the straggler's
`_new_job` lands relative to the two `_clear()` calls, i.e. on scheduling.

Mechanism, precisely:

1. `routes_ws._handle_busy_send` spawns `ws-handoff-<project>` (routes_ws.py:259)
   whose `_work()` calls the **module-global** `relay_user_message`
   (routes_ws.py:238).
2. That thread typically runs *after* the test's `monkeypatch` teardown has
   restored `relay_user_message = _default_relay` — so a test that only meant to
   exercise handoff *wiring* ends up driving the REAL relay.
3. `_default_relay` spawns a second thread `ws-relay-<project>` (routes_ws.py:373)
   which reaches `_ro._new_job` only after `_backing_resolve` +
   `ensure_session` + `_ensure_session_exists` — so the registration can land
   arbitrarily late, after the fixture's clear.
4. The real `_backing_invoke` then runs a real `hermes -p <profile> chat -Q ...`
   (the fake invoke is gone with the reverted monkeypatch), which on this host
   takes ~240 ms and ends in `error` — a long-lived live job for project "hscc".

Therefore `_clear()` cannot fix it: **clearing a dict cannot fix a leak whose
writer is an unjoined thread.** t_7cc2e7d5's partial fix (clear before *and*
after) reduced the window and, correctly, did not close it.

### F2 — why "p313 only" and "full suite only"

Nothing about the race is 3.13-specific: it needs (a) a preceding test that
spawns a relay/handoff thread and (b) a straggler whose `_new_job` lands after
the next setup clear. (a) only exists in a full-directory run — in isolation the
file's own tests all join/wait their relays, hence "passes 6/6 alone". (b) is
GIL/scheduler timing, so an interpreter switch changes the *odds*, not the
possibility. Runs on p313 are slower per test (239 s for the dir) and the log
confirms the ~240 ms straggler window, which is why it surfaced there.

### F3 — the unguarded seam list (all process-global relay/job state)

- `routes_orchestrator._jobs` + `_jobs_lock` — scanned by `_in_flight_job`.
  (Guarded since t_7cc2e7d5, but only by clearing.)
- `routes_ws.relay_user_message` / `interrupt_user_turn` — module globals that
  tests swap; revert order vs. running threads is what turns a wiring test into
  a real-relay test.
- `routes_ws.coordinator_factory` — has its own autouse restore
  (test_ws_handoff_path.py:37) and is fine.
- `session_event._STORES` — reset by the per-file `clean_stores` fixtures.
- **Transient worker threads** `ws-relay-*` / `ws-handoff-*` — NOT part of any
  fixture's teardown. This is the actual missing piece.

### F4 — known-red baseline, excluded from this card (orchestrator note)

`hscc-bootstrap/tests/test_doctor.py::TestDoctorCLI::test_main_json_output` and
`::test_main_text_output` fail whenever `HERMES_HOME` points at a worker profile
(doctor then reports `<HERMES_HOME>/hermes-agent missing`, fatal). Not
order-dependent, not interpreter-specific, no file overlap with this card;
carded separately as t_267f9d88. Not touched here.

## Fix

Design decision (mine, recorded because two workers could have decided it
differently): **the store owns its own guards.** The worker registry, the
generation counter and the drain all live in `routes_orchestrator` — next to
`_jobs`, `_new_job` and `_in_flight_job` — not in `routes_ws`. Reason: the REST
job worker (spawned by `handle_orchestrator_chat`) writes the same store and is
the same class of leak; putting the registry in `routes_ws` would leave it
unguarded and split one invariant across two modules. `routes_ws` keeps two
thin pass-throughs (`_spawn_worker`, `drain_workers`) purely to avoid an import
cycle, since `routes_orchestrator` defers into `routes_ws` lazily.

Three cooperating pieces, in the only order that works (quiesce BEFORE clear):

1. **`spawn_tracked_worker` (routes_orchestrator).** Every turn worker — the
   REST `_run_job` worker, `ws-relay-<project>`, `ws-handoff-<project>` — is
   registered in `_workers` until it exits, carries its own retire event, and
   **inherits the job generation of its spawner** (HTTP handler threads and test
   bodies are untracked and always write into the live generation).
2. **`drain_workers` (routes_orchestrator).** Signal then wait: set each live
   worker's retire event (one that has not begun its work returns without
   touching the backing, the profile `state.db`, the store or the transcript),
   cancel every live job through the *production* stop path (`cancel_job` →
   `cancel_evt` + terminate the retained Popen) so a worker already blocked in
   `_backing_invoke` unwinds promptly, then join with a bounded, multi-pass loop
   (a handoff worker spawns a relay worker, so one pass is not enough).
3. **`advance_job_generation` + `_StaleJobGeneration` (routes_orchestrator).**
   The backstop for a worker whose join timed out: `_new_job` refuses a write
   from a thread born in a retired epoch. `_default_relay` catches that
   exception and returns **silently** — appending `relay_failed` to the store
   would have traded one cross-test leak for another (a store the *next* test
   owns).

`tests/conftest.py::_isolate_chat_jobs` now performs, per boundary:
`drain → clear → reset_stores → advance generation (stamping the test body)`.
The clear is explicitly demoted in its own docstring to "a mop, not the
defence": **a setup-side clear cannot be the primary defence**, because the
writer can post after any clear.

Inert in production, by design and by pin: the server never advances the
generation (stays 0, every worker inherits 0, every write accepted) and no
request path calls the drain (joining a turn worker from a request path would
block the socket loop on a live turn).
`test_server_path_never_advances_the_generation` scans the shipped modules and
fails if either mechanism ever gets wired into server code.

### Why not the alternatives

- **Clear harder / clear later** (t_7cc2e7d5's direction): cannot work — F1
  shows the write landing after every clear. Kept only as the mop.
- **`join` inside `_default_relay`**: would make the WS send path synchronous
  and destroy the "socket keeps serving" property the docstrings protect.
- **Epoch only, no drain**: a straggler blocked in a real `hermes chat` for
  minutes keeps mutating the transcript and the profile DB; the epoch only stops
  the *job store* write. The drain is what actually stops the work.
- **`pytest-xdist` / `-p no:randomized` ordering pin**: treats the symptom, and
  ordering alone demonstrably does not reproduce or prevent a few-ms race.

### Mutation-tested (each layer proven load-bearing)

Same 5-test file, external plugins disabling one layer at a time:

| mutation | result |
|---|---|
| `_calling_job_generation → None` (guard disabled) | `test_retired_worker_cannot_register_a_job` **FAILS** |
| `drain_workers → no-op` (join disabled) | `test_boundary_drain_joins_a_slow_relay_worker` **FAILS** |
| both disabled (pre-fix emulation) | pair test **FAILS** (straggler's `late reply` lands in the next store) |
| neither (committed state) | 11 passed (incl. the 6 original relay tests) |

## Verification

_(counts filled from the run logs below)_

