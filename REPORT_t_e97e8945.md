# UX: drop the "Send anyway" confirmation on every chat message

Task: t_e97e8945 (ios-engineer)

## What changed

Two chat views confirmed every send with a modal confirmation dialog. Sending a
chat message is neither destructive nor expensive, so the gate is gone — pressing
send sends.

### OrchestratorChatView.swift
- Send button now calls `submitSend()` directly (was: arm a `.confirmationDialog`).
- Removed the `showConfirm` + `retryCandidate` `@State`, and the
  `.confirmationDialog` / `confirmTitle` / `confirmMessage`.
- The UNSENT **Retry** path now also sends immediately (`retrySend(text)`
  directly) — re-sending a chat message is not destructive either. The
  historical UNSENT entry is still kept + a fresh `.prompt` appended
  (`store.retry`).
- Added a quiet caption under the composer naming which orchestrator the
  message lands in (replaces the info that used to live in the dialog).
- Updated the top-of-file doc comment that described the confirm gate.

### StreamingChatView.swift
- Send button now calls `store.send(store.draft)` directly (was: arm a
  `.confirmationDialog`). Removed the `showConfirm` `@State` and the
  `.confirmationDialog` / `confirmTitle`.
- Added a quiet caption under the composer naming which session the message
  lands in.
- `store.send` is unchanged: it appends the optimistic user row via
  `transcript.addLocalUserMessage` then clears the composer (`draft = ""`).

## Composer still clears; optimistic row still appears

Unchanged, same as before the edit:

- **Orchestrator**: `submitSend()` calls `store.beginSend(prompt:)` (appends the
  optimistic `.prompt` row + starts in-flight), then `store.draft = ""` (clears
  the composer). On failure the message is kept as UNSENT, never lost.
- **Streaming**: `store.send` calls `transcript.addLocalUserMessage(trimmed)`
  (optimistic row) then `draft = ""` (clears the composer).

## Verification

### build_check.sh (full compile, all 4 targets)
```
HSCC: 58 files, 0 error(s), 0 warning(s)
HSCCWidgets: 6 files, 0 error(s), 0 warning(s)
HSCCLiveActivity: 4 files, 0 error(s), 0 warning(s)
HSCCLiveActivitySession: 4 files, 0 error(s), 0 warning(s)
full compile clean, 0 warnings (compile only — never built or run on a device)
```

### streaming_check.sh — transcript half (ALL PASSED)
Includes:
- `ok: optimistic row appears before any server event`
- `ok: optimistic row is the operator's own text`
- `ok: echo does NOT duplicate the optimistic row`
- `ok: two identical sends show two rows`
- (and the tool/card/agent/error fold assertions)

### chat_state_check.sh — ChatStore state machine (ALL PASSED)
Includes:
- `PASS: 1a send begins in-flight (isSending)`
- `PASS: 2b transcript grew (historical UNSENT kept + new prompt)`
- `PASS: 2c retry appended a fresh .prompt`
- (and the reachabilityLost / abandonWaiting / failSend / unread-badge set)

## Files changed
- `ios-app/Sources/HSCC/Views/OrchestratorChatView.swift`
- `ios-app/Sources/HSCC/Views/StreamingChatView.swift`
