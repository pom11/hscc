# Offline queue — outbound chat messages (t_42ba90d2)

## Goal
A message composed while the cluster is unreachable (dead spot, Tailscale down)
must not be lost, must not be silently dropped, must not send twice, and must
be visibly distinct from a delivered message so the operator knows it hasn't
landed yet. When the connection returns, queued messages flush.

## The connection truth
`ConnectionMonitor.shared` (added in 53f71c9) is the single source of truth for
reachability, updated on EVERY completed real API request:
- `.reachable`  — a request reached the API (any HTTP response).
- `.unreachable` — a TRANSPORT failure (no HTTP response: refused/DNS/timeout).
- `.unknown`    — nothing completed yet this session.

`HSCCError.transport` is exactly the client-side signal that drives
`ConnectionMonitor.requestFailed()`. So "is this message un-deliverable because
the cluster is genuinely unreachable" == `HSCCError.transport` (or monitor .unreachable).

## Scope
Primary operator chat surface = OrchestratorChatView (job-based, persistent
per-project ChatStore). That is where the offline queue lives, flushed when the
shared ConnectionMonitor reports reachable again.

Streaming chat (live WS window): its send-when-not-connected path already keeps
the draft and shows an honest error (t_d58f7ec6 §7) — not a silent drop. Its
WS-frame delivery is a separate concern, out of scope here (documented, not
changed).

## Design

### New component: OfflineSendQueue
App-scoped, @MainActor, ObservableObject singleton, persisted in UserDefaults
(`hscc.offline.queue.pending`). The single source of truth for outbound messages
that haven't reached the cluster yet. Survives app relaunch.

Model `QueuedMessage` (Codable, Identifiable, Equatable):
- `id: UUID`             — identity; the dedupe key ("never send twice").
- `project: String`      — target orchestrator project.
- `text: String`         — the message body.
- `kind: Kind`           — `.orchestratorChat` (POST + poll).
- `createdAt: Date`

API:
- `enqueue(project:text:kind:) -> UUID` — append + persist + publish (returns id).
- `pending` / `pendingCount` / `queuedCount(for:)` / `isPending(_:)`.
- `remove(_ id:)`  — only after confirmed delivery or permanent rejection.
- `flushIfReachable()` — iterate a snapshot; guard against double-send with an
  `inFlight: Set<UUID>`; call `sendHandler`; `.delivered`/`.rejected` → remove,
  `.unreachable` → keep.
- `sendHandler: ((QueuedMessage) async -> SendOutcome)?` — the app seeds this
  once with the real delivery (POST + persist job_id for later resume).
- Observation: subscribes to `ConnectionMonitor.shared.$status`; on `.reachable`
  with pending items, calls `flushIfReachable()`.

### How a queued orchestrator message is actually delivered
The queue's `sendHandler` does exactly what the fresh-delivery path does:
1. `try await client.orchestratorChatStart(project:msg.project, prompt: msg.text)`.
2. On success: persist the returned job_id to the SAME key ChatStore reads
   (`hscc.chat.<project>.in-flight-job`) so when the operator opens that chat,
   `resumeInFlightJob()` polls and collects the reply. Return `.delivered`.
3. On `HSCCError.transport` → `.unreachable` (keep queued).
4. On api/decoding (server reached, rejected/failed) → `.rejected(msg)` (remove
   — the queue can't fix a 400/502; a re-queued loop would hammer the server).

This reuses the existing proven resume machinery — no new reply-collection
path, and a message that lands in the queue and flushes is fully collected.

### ChatStore / OrchestratorChatView integration
- New `ChatEntry.queued(text: String, messageID: UUID)` case — renders distinctly
  (muted "QUEUED — will send when connected", clock icon), reuses `.text`.
- `ChatStore.markQueued(messageID:text:)` — the optimistic `.prompt` from
  `beginSend` becomes `.queued`, inFlight cleared, job cleared. Persisted.
- `OrchestratorChatView.deliver` catch path:
  - `HSCCError.transport` (unreachable) → `OfflineSendQueue.shared.enqueue(...)`
    then `store.markQueued(messageID:)`. The ChatStore observes the queue; when
    `OfflineSendQueue` reports the message handled (delivered/rejected), the
    store updates its `.queued` entry (→ a fresh `.prompt` on delivered so the
    reply poll can resume; appends the rejected reason on rejected).
- The view's composer shows a small queue chip ("N queued") when the queue has
  pending items.

### Cluster switch drains, never silently
If the operator changes clusters while messages are queued, `reset()`-ing the
queue would silently discard them. Instead `drainDueToClusterSwitch()` clears
the queue and publishes the dropped set on `drainedDueToClusterSwitch`;
ContentView shows a bottom banner (count + "re-send by hand") so nothing is
silently lost. A per-cluster queue instead of a drain was considered and
rejected: messages destined for the old cluster must never flush into the new
one, and the safer default is to surface-and-drop than to risk cross-cluster
mis-delivery.

## "Never send twice"
- Identity is `UUID`, held in a persisted set; flush skips in-flight ids.
- A queued item is removed ONLY after `.delivered` (job created) or `.rejected`
  (server refused). A lost POST (transport) keeps it queued; the server's own
  session dedup + the idempotent prompt echo mean a re-post is the intended
  single send, and the ChatStore transcript's `.queued`→`.prompt` transition is
  idempotent by the messageID (only the matching entry flips).

## Files
- NEW `Sources/HSCC/OfflineSendQueue.swift`
- `Sources/HSCC/Views/ChatStore.swift` — markQueued + observe queue
- `Sources/HSCC/Views/OrchestratorChatView.swift` — .queued case + render +
  enqueue in deliver + wire flush
- `project.yml` — add OfflineSendQueue.swift
- NEW `scripts/offline_queue_check.sh` + `scripts/offline_queue_check/main.swift`
- NEW `docs/offline-queue-t_42ba90d2.md` — this report

## Definition of done
- `build_check.sh` clean (0 errors, 0 warnings).
- NEW harness `offline_queue_check.sh` PASS.
- ChatStore/ChatEntry `chat_state_check.sh` still PASS (backward compat).
- Committed on branch `audit/offline-queue-t_42ba90d2`; SHA in completion comment.
