# E2E Chat Pipeline Proof — t_f909de4e

## What this is
A single script that proves a message posted by the app reaches the model and
returns — the way the operator experiences it, not just that a route answers.

## Deliverables
- `scripts/verify_chat_roundtrip.py` — the standalone proof (primary).
- `hscc_daemon/verify.py::run_chat_roundtrip()` — OPT-IN check wired into
  `hscc verify --chat`.
- `hscc_daemon/hscc.py` — `--chat` flag on the verify command + help text.

## Executed proof (live fleet, 2026-09-02)
The script was run against the LIVE API — real POSTs, real model traffic:

1. **Derive API base** from `hscc api status` → `http://100.64.0.1:8788`
   (never hardcoded; derived at runtime in `api_base()`).
2. **POST** `/v1/orchestrator/chat` with `confirm=true` → `202` + `job_id`.
   - `job chat-3`: status done, elapsed 6.0s, reply `'pong'`, orch
     `generation_tokens_total 84183 → 84299` (+116), exit 0.
   - `job chat-4` via `hscc verify --chat`: replied `'pong'`,
     `generation_tokens_total 105591 → 105686` (+95), exit 0; all 7 checks pass.
3. **Poll** `GET /v1/orchestrator/chat/{id}` to a terminal state → `done`.
4. **Assert a real reply** → `'pong'`, non-empty, not an error.

The critical assertion — **the model actually saw the message**: the
orchestrator unit's `vllm:generation_tokens_total` moved during every run
(+116, +95 tokens). This is what distinguishes "the API accepted it" from
"the model actually answered". This is the exact gap that hid the earlier
outage where every profile pointed at a dead host.

### How the metric is read (read-only)
The orchestrator unit (`role=="orch"`, serving.json nodes[0]=10.0.0.244,
port 8000) exposes vLLM Prometheus metrics at `http://<node>:<port>/metrics`.
We parse the `vllm:generation_tokens_total` counter (the standard vLLM counter
surface the codebase already uses in `throughput.py`). Note: the orch unit is
NOT a keepalive unit, so `/v1/fleet/throughput` does not cover it — the script
derives the orch endpoint directly from serving.json instead, exactly like
`serving.orchestrator_endpoint()` but pointing at `/metrics` (no `/v1`).

### WS relay equivalence
The iOS Chat tab's true path is the WebSocket
(`/v1/projects/{name}/session/ws`, `relay_user_message` in routes_ws.py). The
relay was the historical no-op bug (c07d702 fixed it to fall back to the same
`_backing_invoke` that `/v1/orchestrator/chat` uses). So the REST job path this
script exercises lands on the SAME underlying `hermes chat` invoke as the WS
relay — proving the model round-trip via REST proves the model reachable across
both. The REST path is scripted because it exposes a `job_id` + "poll to a
terminal state" contract (WS has no terminal-poll; it streams).

## Failure diagnosis (exit non-zero + named cause)
The script exits 1 and names the likely cause per failing step:
- **POST failed** → "profile endpoint unreachable (hermes chat hangs in
  SYN_SENT) → 400/5xx, or hermes not spawning (502/503)".
- **Poll timed out** → "hermes chat wedged (stuck spawning / endpoint
  unreachable) or model idle".
- **Reply empty/error** → "orchestrator did not answer with real text; model
  idle or hermes returned an error".
- **Metric delta == 0** → "the API answered but the MODEL never saw it — the
  profile endpoint is unreachable or the model is idle. This is the dead-host
  signature."
Exit 2 = preconditions missing (no API host/token, no orch endpoint, no
/metrics readable).

## Why wired as OPT-IN (`hscc verify --chat`), not a default check
`hscc verify` is a fast smoke test the operator runs routinely. A default check
that fires a REAL prompt at the live orchestrator and can block up to ~600s on
every verify would (a) make the smoke test slow and (b) dispatch real work on
every run — the exact invasive behaviour the task's own rules warn against for
routine operations. The `--chat` flag gives the operator the deep process-proof
on demand without polluting the default surface.

## Tests
- `hscc_daemon/test_verify.py` + `test_unified_cli.py`: 143 passed (verify
  command + --chat wiring, no regression).
- `hscc-api/tests`: full suite run (WS relay no-op guard etc.) — see run result.

## What I deliberately did not do
- Did NOT exercise the raw WebSocket path in the script (implementing an RFC
  6455 client and streaming to a terminal state): the REST job path lands on
  the identical `hermes chat` invoke and gives a clean terminal-poll contract,
  and the WS relay's no-op regression is already guarded by
  `hscc-api/tests/test_ws_relay_not_noop.py`. Proved equivalently via the job
  path.
- Did NOT add the chat round trip to the DEFAULT `hscc verify` run — too slow
  and too invasive (see above).
