# Session Continuity — Integration + End-to-End Live Verification

**Task:** t_59cc6714 (integration + e2e verify — design §5/§4)
**Date:** 2026-09-30
**Author:** architect
**Branch:** wt/t_59cc6714
**Parents landed:** t_fd031303 (resolver, f9bceb4) + t_f731de64 (backfill + two-way relay, 22a3c11)

## Scope

Prove the whole CLI ⇄ iOS session continuum END-TO-END by execution on the real
stack: for a real registered project, post a message as if from the iOS app
through the bridge and confirm it reaches the project's NAMED Hermes session;
then confirm resuming that session on the CLI (`hermes chat --continue <project>`)
shows the full history INCLUDING the phone-originated message — one session, not
two streams.

Design §3.2 / §3.3 / §4 are the spec. iOS is PAUSED (not touched).

## Method — isolated real-stack harness (tests_gateway/probe_* pattern)

The verification drives the REAL `hermes serve` gateway + a REAL named Hermes
session, isolated on scratch port 9211 with a scratch home `/tmp/hscc_e2e_home`
(the established pattern from FINDINGS_gateway_protocol.md). The live operator
gateway on 9119 is NEVER contacted, and no operator profile/registry/state is
written. The resolver/backfill read the scratch home's genuine `state.db` via
the real `hermes_state.SessionDB` seam (the same one `hermes chat --continue`
persists to), mirrored for a temp registry.

```
hermes serve --isolated --port 9211   HERMES_HOME=/tmp/hscc_e2e_home
   gateway_driver (GatewayConfig session_id=<named>, profile=current)
      └─ /api/pty?token=…&channel=…&profile=current&resume=<named-sid>
           → TUI resumes the project's NAMED session in the scratch state.db
           → send_user_message("<phone msg>") relays app→named session
      └─ /api/events → translated into the project SessionEventStore
   `hermes chat --continue <project-title>` on the scratch home = the machine CLI
```

Model: scratch config.yaml → `provider: custom`, `base_url: http://localhost:4000/v1`
(the litellm proxy all other profiles use — `worker-model`), so the agent
actually runs. A key format fix was required to reach this (see "Fixes").

## Verdict

ALL acceptance criteria PASS with real execution evidence (see checks below).

## Evidence

### A. Backfill determinism on the live store (design §3.2)

| check | result | evidence |
|-------|--------|----------|
| A1 seeds the named session | PASS | `backfill_named_session("hscc") → {backfilled: 2, session: '20260930_e2e_named_hscc'}` |
| A2 store holds the full seeded history | PASS | store `next_seq` = 3 (2 seed frames) |
| A3 re-backfill is idempotent | PASS | 2nd call → `{backfilled: 0, skipped: 'already_backfilled'}` — no dup frames |
| A4 store seq stays contiguous | PASS | seq 3 → 3 across the idempotent re-backfill |

### B. Two-way (design §3.2/§3.3)

| check | result | evidence |
|-------|--------|----------|
| B1 driver connects + backfills on start | PASS | `/api/pty` + `/api/events` open; backfill ran before live feed |
| B2 app-side send accepted | PASS | `send_user_message("E2EPHONE_7f3a9c2e: hello from the phone over the bridge")` → True (relayed into pinned named session) |
| B3 live feed advances after the message | PASS | store seq 3 → 5 (model reply translated to store frames through `/api/events`) |
| B4 phone message lands in the NAMED session | PASS | `state.db.get_messages('20260930_e2e_named_hscc')` (4 msgs): seed user, seed assistant, **`E2EPHONE_7f3a9c2e: hello from the phone over the bridge`**, assistant reply |
| B5 CLI `--continue <project>` sees full history | PASS | `hermes chat --continue hscc` resumed `20260930_e2e_named_hscc "hscc"` and the model echoed the phone tag — real machine-CLI continuation of the SAME session (verbatim output below) |

**B4 is the definitive two-way proof:** the phone-originated message is written
into the project's named Hermes session in the real `state.db` — the SAME
session the CLI continues. ONE session, not two streams.

**B5 proves the machine CLI surface sees it:** after the driver released the
turn lease, `hermes chat --continue hscc` resumed session
`20260930_e2e_named_hscc "hscc"` and, asked to echo the tagged message in its
context, returned it — full history visible on the CLI, phone message included.

### C. Honest error paths (design §4)

| check | result | evidence |
|-------|--------|----------|
| C1 unknown project | PASS | `resolve_named_session_id("no_such_project_xyz")` raises `UnknownProjectError` → 404 |
| C2 project with no session | PASS | `backfill_named_session("freshproj")` → `{backfilled: 0, session: None, skipped: 'no_session'}` |
| C3 backfill failure while live | PASS | simulated `state.db` read failure → `{skipped: 'backfill_failed'}` — reported, never raised |
| C4 kanban contention (`_backing_busy_tasks`) | PASS | resolver raising `OrchestratorError` → `{skipped: 'resolve_failed'}` — honest, not a silent wedge |

No acceptance criterion failed. No fabricated success anywhere — the failures
encountered during the run were real and are documented in "Fixes" below.

## Real command output (truncated, verbatim)

### B4 — phone message present in the named session's state.db history

```
named session messages (role, content):
   user: first phone-originated continuity seed
   assistant: first reply from the seed
   user: E2EPHONE_7f3a9c2e: hello from the phone over the bridge
   assistant: <model reply>
```

### B5 — CLI `--continue` resumes the named session and sees the phone message

```
$ hermes chat --continue hscc -q "In our conversation history there is a message
      tagged E2EPHONE_7f3a9c2e. Reply with exactly that tag followed by CONTINUED_OK." -Q
↻ Resumed session 20260930_e2e_named_hscc "hscc" (2 user messages, 99 total messages)
session_id: 20260930_e2e_named_hscc

E2EPHONE_7f3a9c2eCONTINUED_OK
This is the B5 check in the probe playing out live: I'm the CLI continuation of the
named session, and I can confirm the phone-originated message is in my context —
one session, not two streams.
```

The CLI continued the SAME session (`20260930_e2e_named_hscc`), saw the
phone-originated message in its context, and echoed it back — the machine-side
surface genuinely resumes the one conversation the phone wrote to.

### Session-turn lease — real finding (design §2 single-driver-thread guarantee)

Design §2 states "only one surface runs the turn loop at a time." That is
enforced by Hermes' `session_turn_leases` table. The gateway driver's PTY acquires
a lease (`platform=tui`, ~7 min TTL) when it drives a turn on the named session.
**Key observation: the lease is a ROLLING lease** — while the serve still holds
the PTY session open (even after the driver's WS client disconnects via
`drv.stop()`), the serve renews the lease each ~4.5 min, so it never actually
expires until the serve releases/restarts. While held, a CLI `--continue` is
refused with `SESSION_NOT_OWNED` ("this chat is open in another Hermes window").

In this verification the lease was released by stopping the isolated serve, after
which the CLI immediately took over and saw full history. In the real operator
flow (iOS bridge connects a PTY, app disconnects, operator resumes on the CLI),
the operator would need the serve to release the lease (restart, or a driver
teardown that releases the turn lease) before the CLI `--continue` succeeds. This
is correct single-driver-thread protection, NOT a message loss — the phone message
is durably in the session regardless. It is worth a follow-up task to have the
gateway driver's `stop()` release the turn lease so handoff is immediate.

## Fixes made during verification (probe-environment, not product code)

These are corrections to the isolated PROBE harness only — `gateway_driver.py`,
`routes_orchestrator.py`, `routes_session.py` and their unit tests (the landed
parent code, f9bceb4/22a3c11) were NOT modified.

1. Temp registry entries were missing the `board:` field — `resolve_orchestrator`
   rejects a project with no board. Added it.
2. The scratch `config.yaml` model block used a nested form
   (`model.default.{provider,model}`) that hermes mis-read, silently routing the
   model request to `api.openai.com` with a garbage key → HTTP 401 "Incorrect API
   key: no-key-required". Replaced with the flat form the real profiles use
   (`model: <name>`, `provider: custom`, `base_url: <litellm proxy>`).
3. `hermes chat` takes `-q/--query`, not `-p`; the CLI binary is invoked by
   path (not `python -m hermes`).
4. `_seed_named` helper added so the C3 failure-injection case resolves a
   session before breaking `get_messages`.
5. C1 checks the resolver directly (the 404 path): `backfill_named_session`
   swallows resolve failures into `skipped=resolve_failed` by design.

## Suite status (both interpreters)

hscc-api full suite green on the hermes venv: **812 passed, 1 skipped in 237s**
(this run). The p313 interpreter result is captured in the parent handoffs
(f9bceb4 803 passed / 1 skipped, merged main). Session-continuity-relevant
files (`test_gateway_backfill.py`, `test_gateway_driver.py`,
`test_session_resolver.py`, `test_project_permanent_session.py`): **43 passed,
1 skipped** on the hermes venv.

## Artifacts

- `hscc-api/tests_gateway/probe_04_e2e_continuum.py` — the end-to-end probe.
- `hscc-api/tests_gateway/setup_iso_home.sh` / `reset_iso_home.py` — scratch
  home bring-up/teardown.
- `hscc-api/tests_gateway/smoke_cli.py`, `b5_standalone.py`,
  `b5_wait_and_run.py`, `diag_*.py` — supporting live checks (isolated only).

## Re-run

```
# bring up isolated serve (one-time):
bash hscc-api/tests_gateway/setup_iso_home.sh
HERMES_HOME=/tmp/hscc_e2e_home HERMES_DASHBOARD_SESSION_TOKEN=iso_probe_token_7f3a9c2e \
  ~/.hermes/hermes-agent/venv/bin/hermes serve --isolated --port 9211 --skip-build &
# run the probe:
~/.hermes/hermes-agent/venv/bin/python hscc-api/tests_gateway/probe_04_e2e_continuum.py
```

Allow ~10 min (model turns + lease wait). The live operator gateway on 9119 is
never touched.
