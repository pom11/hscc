# t_93f1ba4d — Live feed must capture CLI-driven session frames 1:1

## Task restated
The app's live stream for a project must reflect NEW frames written by ANY
process (primarily the operator's CLI REPL), appearing as they happen — not just
the serve-side pty activity that /api/events fans out.

## Root cause (confirmed in task body + hermes source)
- /api/events is a PURE FAN-OUT of the serve's /api/pub sidecar (chat_ws.py:622-654).
  It only carries events the serve's OWN pty session publishes INTO /api/pub.
- The operator drives the SAME named session in a SEPARATE CLI REPL process that
  writes frames directly to the shared persisted session store (state.db messages).
- Those writes NEVER reach the serve's /api/pub → never reach /api/events → never
  reach the bridge → the store freezes at mount-time backfill watermark (next_seq 247).

## Evidence (store frozen at backfill watermark)
- Store stuck at next_seq 247; does not advance on subsequent CLI activity.

## Fix direction
- Add a SECOND live source for each mounted GatewayDriver: poll/tail the
  session's on-disk frames (state.db messages) for NEW rows (id > high-water
  message id), and append frames with seq > store high-water, translating them
  exactly like FrameTranslator / the backfill does.
- Keep the serve-side pty for app→session outbound (send_user_message).
- The app must ALSO see CLI-driven new frames.

## Acceptance gate (verify by EXECUTION on live stack)
1. git log origin/main shows my merge.
2. Deploy via install_payload, restart API.
3. With operator typing in live CLI session for project hscc:
   GET /v1/projects/hscc/session/events → next_seq INCREASES between two probes
   ~10-30s apart. Last frame content matches CLI's most recent activity.
4. App shows the operator's newest CLI reply live.
5. Suite green under BOTH interpreters (hermes venv AND miniconda p313).

## Findings so far
- (to be filled)

## Findings (implementation)
- Fix implemented in hscc-api/gateway_driver.py (committed 8405d0d):
  * tail_named_session() — polls the session's on-disk messages table for NEW
    rows (id > shared watermark) and translates them exactly like the backfill.
  * Shared per-project store-tail watermark, seeded by backfill and updated by
    BOTH the disk poller and the native events loop, so serve-driven turns the
    native feed translates are never duplicated by the disk poller.
  * New gateway-store-tail thread per mounted GatewayDriver (2s poll).
- +8 new tests in hscc-api/tests/test_gateway_store_tail.py.
- Suite green under BOTH interpreters:
  * hermes venv: 839 passed, 1 skipped
  * miniconda p313: 824 passed, 16 skipped (store-tail tests skip — the
    documented hermes_state env gap, same as backfill tests)
- PRE-DEPLOY BASELINE (live API, old code): GET /v1/projects/hscc/session/events
  -> next_seq 247 (STUCK, matches operator's report). Confirmed under the
  OLD running API before my fix is deployed.

## Next steps
- Merge wt/t_93f1ba4d -> main, push.
- Deploy via install_payload, restart API.
- Verify next_seq increases with live CLI activity (acceptance gate 3/4).
