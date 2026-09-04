# Chat: attachments and images in the transcript — WIRE GAP report (t_3fc4801c)

Date: 2026-09-03 (initial) / 2026-09-04 (re-audit, run 477)
Branch: audit/chat-attachments-t_3fc4801c
Assignee: ios-engineer

## RE-AUDIT (run 477, completed 2026-09-04) — findings still hold, this is the terminal deliverable

Re-claimed after the previous run blocked for review. Re-audited against the CURRENT
operator dev head (`/Users/desac/dev/hscc`, branch `dev` @ `b2668ba`) and the workspace
tree. The session-event contract is STILL attachment-less on every path:

- `ProjectsView.swift:217` — `case .chat:` still routes to `StreamingChatView` (the only
  reachable transcript); OrchestratorChatView is STILL un-instantiated (dead code).
- `session_event.py` (both workspace + ops `dev`) — still the same seven types
  (`hello/message/tool_call/card/agent/system/error`); `MessagePayload` is still
  `{role, delta, done}` with NO image/file field.
- `StreamingChatStore.swift:408` — the WS send frame is still `{kind:send, text}` only.
  The WS handler (`hscc-api/routes_ws.py:281`) accepts only text; it has no image path.
- No attachment type has been added to the wire in the 11h since the first audit.

The card's own rule is explicit and its condition is met: *"if the wire format has no
attachment type, record exactly what is missing and stop rather than inventing one."*
Inventing an attachment type or resurrecting the abandoned OrchestratorChatView to bolt
on a half-feature would violate the rule, so neither is done. THIS document is the
deliverable. The one REAL, committed image capability — `POST /v1/orchestrator/chat`
accepting `image_data`/`image_mime` (t_a779c06f) — remains unwired to any reachable iOS
surface, and DOES NOT help: its reply is the same attachment-less `message` contract, so
nothing can be rendered back. This is the terminal state; no further iOS work is possible
until the wire contract is extended by the API owner.

## Status: NO DELIVERABLE SHIPPED — the session-event contract has no attachment type

Per the card: *"Respect the session-event contract — if the wire format has no
attachment type, record exactly what is missing and stop rather than inventing
one."*

The session-event contract — the ONLY wire that renders a chat transcript in
this app — has **no attachment type and no image/file field on any path**
(send frame, message event, history). The chat cannot carry an image or a file
from the phone to the orchestrator, and cannot render an attachment back in the
transcript, without first extending the wire contract. This card explicitly
forbids inventing one, so the correct deliverable is this precise record of
what is missing.

This report records, precisely:
1. which chat surface is the "transcript" the card refers to,
2. the full session-event wire contract (what exists today),
3. exactly what is missing to attach + render, with file:line evidence,
4. the minimal wire additions that would unblock it (NOT built — for the API owner).

---

## 1. What "the transcript" is in the live app

The project chat surface is **StreamingChatView** — a live window onto the
whole project session (`ios-app/Sources/HSCC/Views/ProjectsView.swift:217`:

```swift
case .chat:     StreamingChatView(project: project.name)
```

`StreamingChatView`/`StreamingChatStore` render the typed session event stream
(`message` / `tool_call` / `card` / `agent` / `system` / `error`) in real time,
token by token (`StreamingChatStore.swift:1-36`). This IS the "transcript" the
card means. `SessionHistoryView` is the read-only pager over the same events.

The legacy **OrchestratorChatView** (job-based: POST a prompt, poll for one
reply) is **no longer instantiated anywhere** in the app — only comments still
name it (`ProjectsView.swift:179`, `StreamingChatStore.swift:7`,
`StreamingChatView.swift:315`, `ProjectIntents.swift:175`). It is dead code.
So "the transcript" unambiguously means the streaming session view.

---

## 2. The session-event wire contract (what exists TODAY)

Locked in `hscc-api/session_event.py` (committed on dev by t_47f51a71), mirrored
1:1 in `ios-app/Sources/HSCC/SessionEvent.swift`.

Every event is an envelope:

```json
{ "seq": 42, "type": "message", "ts": "2026-08-29T00:00:00Z", "payload": {...} }
```

The iOS `ParsedPayload` decodes exactly seven concrete `type`s
(`SessionEvent.swift:81-91`), plus a forward-compat `unknown` fallback:

| type | iOS payload struct | fields |
|---|---|---|
| `hello`    | `HelloPayload`     | `next_seq` |
| `message`  | `MessagePayload`   | `role`, `delta`, `done` |
| `tool_call`| `ToolCallPayload`  | `call_id`, `name`, `status`, `args?`, `result?`, `duration_s?` |
| `card`     | `CardPayload`      | `board`, `id`, `title`, `status` |
| `agent`    | `AgentPayload`     | `role`, `action`, `task?` |
| `system`   | `SystemPayload`    | `kind`, `details?` |
| `error`    | `ErrorPayload`     | `code`, `message` |

**There is no `attachment` type** in `ParsedPayload`, and the `message` payload
is exactly `{role, delta, done}` with **no image/no file/no attachment field**:

- `ios-app/Sources/HSCC/SessionEvent.swift:166-170` — `MessagePayload { role, delta, done }`
- `hscc-api/session_event.py` — the locked contract; `message` carries only
  `role`/`delta`/`done` and there is no `TYPE_ATTACHMENT`-style variant.

### Send path over the wire

The live chat sends a prompt over the session WebSocket as:

```swift
// StreamingChatStore.swift:408
let payload: [String: String] = ["kind": "send", "text": trimmed]
```

A `{"kind":"send"}` frame carries **only a text string** — no image, no file,
no MIME, no bytes. The orchestrator session receives plain text and streams
typed events back.

---

## 3. Exactly what is MISSING from the wire contract

To satisfy the card's deliverable (*attach an image or a file to a chat message,
and render attachments that come back in the transcript*), the session-event
contract needs, at minimum:

**A. Send side — an image/file field on the `send` frame.** The WS send frame
currently has only `text` (`StreamingChatStore.swift:408`). There is no way to
carry image bytes + a MIME type (or a file upload) from the phone to the
orchestrator over the live socket. The backend's socket send handler consumes
`{"kind":"send","text":...}` — it does not accept image data.

**B. Render side — an attachment type on the receive side.** The transcript can
only render the seven existing `ParsedPayload` cases (`SessionEvent.swift:81-91`).
There is **no `attachment` type** and `MessagePayload` has no `image`/`file`
field (`SessionEvent.swift:166-170`). An image the orchestrator (or the
operator) attaches to a message cannot be described, and therefore cannot be
rendered, by any client that respects the contract. The `unknown` fallback
would surface an invented type only as raw JSON text — not as an image.

**C. File (non-image) support is absent from the API entirely.** The only
place the API accepts an image at all is the legacy job-based POST (see §4),
and it VALIDATES `image/*` only (`routes_orchestrator.py:1334-1350`,
`_backing_invoke` `_validate_image`). Arbitrary files are rejected everywhere;
without an upload/sharing endpoint, "attach a file" has no wire home.

---

## 4. One partial capability exists — but does not satisfy the deliverable

**The legacy job-based orchestrator chat API takes an image; nothing in the
live chat does.**

`POST /v1/orchestrator/chat` (the OLD chat path) accepts optional base64
`image_data` + `image_mime` and forwards `--image <file>` to `hermes chat`.
Committed on dev by **t_a779c06f** (`git log` commits `de692e5`, `87f5d53`):
- request parsing + validation: `hscc-api/routes_orchestrator.py:1328-1350`
  (`image_data`+`image_mime` must be supplied together, `image/*` only, 20MiB
  decoded cap)
- threaded through the job: `routes_orchestrator.py:1151-1169`, `1240`,
  `1480`
- written to a temp file and passed as `--image`: `routes_orchestrator.py:266-274`

**Why this does NOT unblock the card:**

1. **It is not wired to any reachable iOS surface.** The client method
   `orchestratorChatStart(project:prompt:)` POSTs only `prompt`/`confirm`/`project`
   — no `image_data`/`image_mime` (`ios-app/Sources/HSCC/HSCCClient.swift:852-876`).
   And `OrchestratorChatView` — the only view that would call it with an image —
   is **dead code** (never instantiated; §1). So today the operator cannot attach
   an image from any screen, even via this path.

2. **It cannot render an attachment "that comes back".** The job-based path
   returns a single text reply and the session-event stream it emits is the same
   attachment-less `message {role, delta, done}` contract. There is nothing that
   comes back as an attachment to render.

3. **It is image-only.** "or a file" has no support in this path either.

Reviving dead code (`OrchestratorChatView`) to bolt on a half-feature whose
reply cannot carry the image back would not satisfy the deliverable, so I did
not do it — that would be inventing capability the transcript wire cannot
express.

> A `ChatImage` struct, a PhotosPicker/camera attach control, and a thumbnail
> render for a locally-persisted `ChatEntry.image` were fully designed in the
> earlier t_a779c06f session (session 20260828_212420), but they only make sense
> on the job-based chat — the API accepts an image there. Because that view is
> now dead, and because the live chat's wire has no attachment type, that design
> is not applicable to the current app and was not resurrected.

---

## 5. Minimal wire changes that WOULD unblock the deliverable (NOT built)

These are the smallest contract extensions, for the API owner to weigh. **I am
not implementing them** — the card forbids inventing a wire format.

### Send side — allow an image on the live socket

Extend the `{"kind":"send"}` frame:

```json
{
  "kind": "send",
  "text": "what broke here?",
  "image_data": "<base64>",
  "image_mime": "image/jpeg"
}
```

The backend socket send handler would decode/validate (reusing the
`_validate_image` logic from `routes_orchestrator.py`) and pass `--image` to the
session. This mirrors the already-committed job-based capability.

### Render side — a new `attachment` event (or an image field on `message`)

EITHER a new event type:

```json
{ "seq": 55, "type": "attachment", "ts": "...",
  "payload": { "mime": "image/png", "data": "<base64>", "caption": "..." } }
```

OR an optional `image`/`attachment` field on `MessagePayload`. Either way the
iOS `ParsedPayload` gains a case and `StreamingChatView` renders an
`Image(uiImage:)` (downsampled) thumbnail in the bubble — the OCR/photos path is
precedented (the QR setup already decodes images in-app).

---

## Evidence trail

- Streaming chat is the live surface: `ios-app/Sources/HSCC/Views/ProjectsView.swift:217`.
- OrchestratorChatView is dead code (never instantiated): grep `OrchestratorChatView(`
  across `Sources` returns no call sites (only comment references).
- ParsedPayload has NO attachment case: `ios-app/Sources/HSCC/SessionEvent.swift:81-91`.
- MessagePayload = `{role, delta, done}`, no image/file: `SessionEvent.swift:166-170`.
- WS send frame = `{"kind":"send","text":...}`, no image: `StreamingChatStore.swift:408`.
- Session contract is THE locked type set: `hscc-api/session_event.py`
  (message carries only role/delta/done; no attachment variant).
- Job-based API image support (t_a779c06f, committed):
  `hscc-api/routes_orchestrator.py:1328-1350` (validate), `266-274` (write tmp),
  `1151-1169`/`1240`/`1480` (thread through), `git log` `de692e5`/`87f5d53`.
- iOS client does NOT send image on the job POST:
  `ios-app/Sources/HSCC/HSCCClient.swift` `orchestratorChatStart` (payload has
  only prompt/confirm/project).
