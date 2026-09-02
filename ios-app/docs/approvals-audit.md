# StreamingChatView Audit — t_d77e927f

Status: COMPLETE
Started: 2026-09-02   Completed: 2026-09-02
Auditor: ios-engineer
Branch: wt/t_d77e927f (from dev, b6b2420)
Live API: read-only, verified 2026-09-02. Address redacted to 100.64.0.1.

## Task
Full audit of `ios-app/Sources/HSCC/Views/StreamingChatView.swift` (the Chat tab).
The send path is being fixed on t_c1ab8a2c — everything else is in scope:
data-in flow, render mapping, loading/empty/error/stale states, controls,
observation, layout, accessibility. Fix clearly-broken items, build-check, commit.

## 1. Data-in flow (traced + verified live)

The Chat tab is a live window onto a project's Hermes session. Two HTTP routes
feed it, both backed by the same in-memory SessionEventStore per project:

1. **History seed** — `GET /v1/projects/{name}/session/events?limit=N`
   (`HSCCClient.sessionEvents`, StreamingChatStore.seedFromHistory). Replays
   retained events, append-only, shared seq space.
2. **Live stream** — `GET /v1/projects/{name}/session/ws?after=<lastSeq>`
   (RFC 6455 upgrade, StreamingChatStore.openSocket). Server sends a `hello`
   frame `{next_seq: N}` then replays `seq > after`, then streams live.
   Reconnect is gap-free/duplicate-free via SessionStreamCursor.

Live verification (read-only, 2026-09-02):
- `GET /v1/projects/hscc` → 200, parses (project detail route alive).
- `GET /v1/projects/hscc/session/events?limit=200` → 200, **0 events,
  next_seq=1** → the operator currently sees the EMPTY state; the event store
  is in-memory and reset by the last server restart.
- WS handshake (raw RFC 6455 client): `101 Switching Protocols` +
  `Sec-WebSocket-Accept` + first frame `{"seq":1,"type":"hello",
  "payload":{"next_seq":1}}` → the live-stream transport answers and the
  `hello` maps to `.connected` (proves reconnect/seed path end-to-end at the
  transport level; send relay is t_c1ab8a2c's scope).
- `scripts/api_route_sweep.py` (read-only GET sweep): all literal routes the
  app calls answered with parseable JSON; 0 failures.

## 2. Render mapping — every field the operator sees

All fields on the wire are rendered; only the timestamp is dropped (see below).

| Event type | Wire fields | What the operator sees |
|---|---|---|
| message | role, delta/text, done/streaming | MessageBubble: user → right, accent-tinted, " ▍" caret while streaming; assistant → left, surface, streams token-by-token |
| tool_call | call_id, name, status, args, result, duration_s | ToolChip: one collapsed line `name — 0.4s`, tap to expand args/result; duration shown as `%.1fs` (s → seconds, correct) |
| card | board, id, title, status | CardChip: tappable chip → CardDetailView; title + board + status; status color-coded |
| agent | role, action, task | AgentRow: role (semibold) + action; task one-liner |
| system | kind, details | SystemRow: uppercase kind + details (2-line) |
| error | code, message | ErrorRow: code (semibold, red) + message |
| notice | text (client-local) | NoticeRow: reconnect/gap framing |
| unknown | type + raw json | UnknownRow: surfaces raw so nothing is dropped |

**Dropped field (low):** every event carries a `ts` timestamp, and the view never
renders it. For a live stream that is defensible ("now"), but after a reconnect
or app relaunch the history seed can include events that are hours old, and the
operator gets no sense of when they happened. SessionHistoryView shows
timestamps; the live view does not. Not fixed — a deliberate live-stream style
choice (consistent with "primary mode is watching the tail"), and adding
timestamps to a streaming bubble is noisy. Flagged for the operator to decide.

## 3. States — loading / empty / error / stale MUST differ

Four phases → four clearly distinct banner treatments (StreamingChatView
statusBanner + bannerContent, lines 146-161):
- **loadingHistory** → neutral banner, `tray.and.arrow.down` icon.
- **connecting** → warn banner, antenna icon.
- **connected (empty)** → ok/green banner `Live — watching <project>'s session`
  + `emptyState` invite ("Watching <project>'s session … Send a prompt below.").
- **failed(reason)** → bad/red banner `exclamationmark.triangle` + reason.
- **reconnecting** → warn banner `arrow.clockwise`, "resuming from latest, no gap".

**Bug fixed here:** before my change, when the stream FAILED with zero rows, the
view showed the identical live-chat invite (emptyState) underneath the red banner.
0-results and failed-to-load differed only by the banner. Added a distinct
`failedState` (exclamation icon + reason + "check Settings → pull to reconnect")
that replaces the invite whenever `phase == .failed` and rows are empty
(StreamingChatView, lines 53-62, failedState at 205-229). Now the two states
cannot look the same.

Stale/offline: not a distinct phase — `reconnecting` (warn) and `failed` (red)
cover the drop case. There is no dedicated "offline/can't reach" state, but
failed(reason) is honest about it, so stale data is never shown as live.

## 4. Controls — every button/toggle has a route answer + visible feedback

- **Send (paperplane)** — disabled when draft empty; tap → confirmation dialog
  ("Send to <project>?"), Send runs store.send. Feedback: optimistic user row
  appears instantly (transcript.addLocalUserMessage + done echo adoption), draft
  clears, a red sendError line surfaces on failure. **Bug fixed:** sendError was
  never cleared after a success, so a one-off "Not connected yet" lingered
  forever above the composer (comment promised "shown, then cleared" — code
  didn't). Cleared at the start of send(). (Store send() line ~376.)
- **ToolChip tap** — toggles expandedToolIDs via store.toggleTool; chevron
  flips + panel animates + ProgressView "running…" while in flight. Visible.
- **CardChip tap** — NavigationLink → CardDetailView(cardID). Pushes.
- **Session History toolbar** — NavigationLink → SessionHistoryView. Proper
  `Label("Session History", systemImage:)` — also reachable/a11y-ok.
- **Reconnect** — all automatic (no manual refresh control); a gap forces
  reconnect and adds a NoticeRow "Some session events were skipped …". Honest.

## 5. Observation — @StateObject / @ObservedObject audit

`store` is `@StateObject` created in `init(project:)` (line 36-41). This is the
correct pattern — stable across body recomputes, created once per view identity.

**Checked the "stale first instance keyed by project" risk:** StreamingChatView
is instantiated only in `ProjectDetailView`'s switch, case .chat
(ProjectsView.swift:212), and ProjectDetailView is pushed per-project via
NavigationLink inside a ForEach over projects (ProjectsView.swift:86-87, 113-115).
Each project gets its own ProjectDetailView on the nav stack → its own
StreamingChatView identity → its own @StateObject store. **No cross-project
stale-instance bug.**

`.onAppear { startStream() }` / `.onDisappear { stop() }` bracket the store
lifetime. Within a single project, switching Chat→Other→Chat destroys and
recreates the store (it's conditionally present in the switch), so state is
fresh each time — expected. `@ObservedObject` in ToolChip correctly re-renders
when expandedToolIDs changes.

**Bug fixed here:** a quick navigate-away during `.loadingHistory` (stop() runs
while the history fetch is in flight) could later call openSocket() and open an
orphaned WebSocket that nothing ever cancels (stop() had already run).
Added `guard isActive` at the top of openSocket() (StreamingChatStore, ~196).

## 6. Layout — Dynamic Type + small-screen

- All prose uses scalable Dynamic Type fonts (.caption/.subheadline/.body) —
  no fixed-size text signatures shrink. Good.
- Composer TextField `axis: .vertical` + `lineLimit(1...4)` grows for long
  prompts on small screens.
- ToolChip expandPanel args/result use `.hsccMono(12)` with no lineLimit — wraps
  on narrow screens (asset: line 416-430). textSelection enabled for copy.
- MessageBubble `Spacer(minLength: 60)` (user) / 40 (assistant) keeps bubbles
  from touching screen edges.
- No fixed-width layouts or hardcoded pixel widths that break at SE size.

## 7. Accessibility

**Bug fixed here:** the paperplane send button was an icon-only control with no
label — VoiceOver would read it as a bare symbol/button. Added
`.accessibilityLabel("Send")` (line 260).

- No colour-only signals anywhere: every status/role signal is icon + text
  (status banner, statusColor-text pairs, ErrorRow red+code+message, ToolChip
  icon-shape change). Good.
- ToolChip: the whole chip is one Button; its combined a11y label is name +
  duration + collapsed summary + chevron name. Noisy but key info (name/status)
  is read. Low-priority polish, not broken.
- All tappable rows (ToolChip, CardChip) are actual buttons/links → accessible.

## What I fixed (3 real bugs + 1 hygiene)

1. **StreamingChatStore.send — sticky sendError** (StreamingChatStore.swift:
   sendError cleared at start of send). A transient failure never cleared; the
   comment promised "shown, then cleared". Correctness/feedback bug.
2. **StreamingChatView — failed-vs-empty state** (rows-empty conditional now
   distinguishes `.failed`). A failure with zero rows no longer shows the
   live-chat invite. 0-results and failed-to-load now clearly differ.
3. **StreamingChatView — send button a11y label** (.accessibilityLabel("Send")).
   Icon-only control was unlabelled for VoiceOver.
4. **StreamingChatStore.openSocket — isActive guard** (orphan WS on quick
   navigate-away during history load). Resource-leak fix.

Verification: build_check.sh clean (0 errors, 0 warnings, all 4 targets);
check_theme.sh CLEAN (no raw colour); check_sources.sh in sync (61 files);
streaming_check.sh ALL PASSED (transcript aggregation intact);
api_route_sweep.py all answered. Committed as `5163097`.

## Deliberately not fixed

- **Timestamp rendering** — every event's `ts` is dropped; a design choice for a
  live streaming view. Flagged, not changed.
- **Send relay path** — t_c1ab8a2c owns it (server `_default_relay`). Not touched.
- **ToolChip combined a11y label noise** — low-priority polish.
- **Live Activity (SessionActivityDriver)** — audited on t_10bc37be.
