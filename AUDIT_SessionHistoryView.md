# Screen audit: SessionHistoryView

Task: t_3359b983 — Full audit of `ios-app/Sources/HSCC/Views/SessionHistoryView.swift`.
Paged session history — verify paging has no gaps or duplicates.

Status: COMPLETE

## Answer to each checklist item

### 1. DATA IN — which endpoint feeds it, and does every needed field arrive?

Endpoint: `GET /v1/projects/{name}/session/events?before=<seq>&limit=<n>`
- Client wrapper: `HSCCClient.sessionEvents(project:before:limit:)` — HSCCClient.swift:690-699.
- Server route: `routes_session.py:69-120` (`handle_session_events`), registered at `routes_session.py:126-130`.
- Server backing store + paging: `session_event.py:391-436` (`history()`).

Contract (server → client), verified against `routes_session.py:19-28` and `SessionEvent.swift:33-40`:
- `project: String`
- `events: [SessionEvent]` — seq-ASCENDING, seq contiguous 1..N (SessionEvent.swift:14-15)
- `next_before: Int?` — cursor for next OLDER page, nil at oldest retained frame
- `oldest_seq: Int` — first retained frame (ring capacity 2000, session_event.py:79)
- `next_seq: Int` — high-water mark
- `speak: String`

Every field the SessionEvent decode needs is present in the wire envelope (`seq, type, ts, payload`) — SessionEvent.swift:56-69. Payload shapes match the committed contract (hscc-api/session_event.py is "THE locked wire contract").

**Live values (READ-ONLY, fetched just now):** Every project's store is currently EMPTY. Example for `hscc`:
```
project: hscc  next_before: None  oldest_seq: 1  next_seq: 1
speak:   "No session events for hscc."   num events: 0
```
Same for all 12 projects (ecofire-bc, ecofire-app, sphoin, soconn, flosana, powerbi, efsdriver, grid, radio, pickolo, pom). So the operator testing today would land on the EMPTY state for any project — there is genuinely no history yet on the running server. The endpoint itself answers 200 correctly.

**Decode proof:** `model_decode_check.sh` compiled the REAL SessionEvent.swift into a macOS CLI and decoded the committed fixture `v1_session_events.json → SessionHistoryResponse` AND the "history paging contract (10 events, seq 41–50, next_before 40, all 7 types)" — 48/48 fixtures pass (model_decode_check.sh:33-38 includes SessionEvent.swift; harness main.swift decodes it). Compile: build_check.sh → 0 errors / 0 warnings.

### 2. RENDER — what does the operator SEE? Dropped fields:

All 7 payload types + unknown are rendered (SessionHistoryView.swift:250-398). No field needed for correct rendering is missing from the wire. But several wire fields are deliberately NOT rendered:

- **`ts` (timestamp) is never shown** — SessionEvent.swift:51 decodes it, but NO row (EventRow, lines 224-452) displays it. The timeline is ordered by seq (which correlates with time) but the operator has ZERO temporal information — cannot tell "10 min ago" from "3 days ago." **This is the most user-visible dropped field.** (Not a correctness bug — a UX gap.)
- **`tool_call.args` / `tool_call.result` are dropped** — ToolCallPayload decodes them (SessionEvent.swift:181-182) but toolRow (lines 293-311) renders only name + started/finished + duration_s. The operator sees "toolname started/finished" but not WHAT was invoked or its result. Likely deliberate (density) but a real data omission.
- **`system.details` dropped** — SystemPayload.details (SessionEvent.swift:211) never rendered in systemRow (353-363).
- **`card.board` / `card.id` dropped** — fine for a timeline.
- **`message.done` not shown** — correct, a stream detail.

**Count correctness:** The client renders exactly `events.count` rows (ForEach over `events.reversed()`, line 87). No client-side count is synthesized — the list IS the server's events. No disagreement with the server's own count possible once loaded.

### 3. STATES — loading / empty / error / stale-offline:

- **Loading** (`.idle`, `.loadingTail` → line 46-47): `HSLoading("Loading session…")` — centered spinner + label (Theme.swift:135-150).
- **Empty (success, zero rows)** (`.ready` + `events.isEmpty` → line 61-62, 123-127): `HSEmpty("No session events yet", ...)` — `text.bubble` icon, NEUTRAL colour (Theme.swift:179-198).
- **Full-tilt error** (`.failed` → line 48-53): `HSError("Couldn't load the session", message:retry:)` — `exclamationmark.triangle` in BAD (red) colour + **"Try again" button** (Theme.swift:155-176).
- **Older-page error** (`.ready` + `pagingError` → line 56-60, 132-162): inline warn-coloured banner "Couldn't load older events" + Retry button. History already on screen stays intact (lines 36-38, 211-218).

**Empty vs. failed are clearly visually distinct:** different icons (`text.bubble` vs `exclamationmark.triangle`), different colours (neutral vs bad/red), and the error has a retry button the empty state lacks. Requirement met — they can never be confused. ✓

**Stale/offline:** `sessionEvents` passes query items, and `get(path:queryItems:)` caches ONLY when `queryItems.isEmpty` (HSCCClient.swift:261-262) — so this endpoint is NEVER cached, and there is NO offline fallback for session history. Offline → `loadTail()` throws → HSError. Graceful error state (not a blank/frozen screen). This matches a known systematic app-wide limitation (query-param GETs have no StateCache fallback), not a new defect.

### 4. CONTROLS — what they call + do they answer + feedback:

1. **"Older . . ." button** (lines 104-121): calls `Task { await loadOlder() }` → `sessionEvents(before:nextBefore, limit:100)` → the GET route (verified answers 200 live). **Feedback:** swap icon for a `ProgressView` while loading (line 109) + disabled during load (line 119). ✓
2. **HSError "Try again"** (line 49-52): resets to `.idle`, clears events, `loadTail()`. Feedback: shows loading then content. ✓
3. **pagingErrorBanner "Retry"** (line 145-151): `pagingError = nil`, `loadOlder()`. Feedback: banner dismisses; "Older" button shows ProgressView. ✓
4. **Toolbar "Session History" NavigationLink** (ProjectsView.swift:225-229): push navigation. ✓

All routes answer. All controls have visible feedback. No silent dead controls.

### 5. OBSERVATION — ObservableObject handling:

`SessionHistoryView` uses ONLY `@State` (lines 21-39) — no `ObservableObject`, no `@StateObject`/`@ObservedObject`. `client: HSCCClient?` and `project: String` are plain `let` (lines 17-18) — HSCCClient is a service, not observed. Each push of the view (NavigationLink) creates a FRESH view with fresh `@State`; `project` is immutable within the view, so there is no "stale first instance keyed by a changing value" bug. **No observation bug.** The "had to switch tabs" failure mode does not apply here.

### 6. LAYOUT — Dynamic Type + small screen:

- The timeline is a single-column `LazyVStack` (line 81) — no fixed-width columns. Rows use `HStack` with `Spacer(minLength: 0/48)` so text wraps. 
- Gutter is fixed `frame(width: 46)` (line 245) — holds `#seq` mono(11) + 18pt glyph; a 5+ digit seq (#99999) would slightly overflow 46pt at mono(11) but that's an extreme case (capacity 2000 caps seq visible today).
- Message bubble: `Text(p.delta).font(.body)` with internal padding — wraps fine at SE width; user bubble gets leading `Spacer(minLength:48)` (line 281).
- No fixed-height rows; Dynamic Type text scales with layout wrapping.
- **Assessment: survives dynamic type and SE width.** No layout bug.

### 7. ACCESSIBILITY:

- Gutter icons (glyph, line 240) are DECORATIVE — informational, not controls; colour used as a hint but every row ALSO has text, so colour is not the only signal. OK.
- The `#seq` is real text (not the only signal). 
- `EventRow` is a non-interactive row — no controls inside that need labels. 
- Toolbar button has a `Label("Session History", ...)` (line 228) — accessible. "Older . . ." button has text. Retry buttons have text. 
- **No icon-only controls without labels; colour is never the ONLY signal** in this view. ✓

### PAGING — the core ask (gaps/duplicates):

**Algorithm review (Swift lines 173-219):**
- `loadTail()` (173-188): fetch newest page, `events = page.events`, `nextBefore = page.next_before`.
- `loadOlder()` (191-219): `guard !pagingLock` (194) serializes concurrent scroll-triggered pages. `cursor = nextBefore` (oldest held). Fetch `before=cursor`. Filter `$0.seq < events.first!.seq` (202) is DEFENSIVE-only and provably a no-op under normal operation because the server's `next_before` contract guarantees returned events are strictly `< cursor` (session_event.py:426-429). Prepends → seq stays contiguous.

**Executed proof** (real server store + replicated Swift loop):
```
seeded 250 events, pageLimit=100, fetches=2, loaded=250
seq range loaded: 1 .. 250
PASS: paging accumulates all events, strictly ascending, no gaps, no duplicates
```
Also ring-eviction case (capacity 10, evicted 1..15): pages to retained window 16..25, next_before=None, no gap/dup. **No gaps, no duplicates.** The server contract tests (hscc-api/tests/test_session_event.py — 27 passed) cover seq continuity and before/paging semantics.

## Findings ranked by operator-likelihood

**Deliberately NOT fixed (UX gaps, not defects):**
1. **No timestamps rendered** (all of EventRow, 224-452) — the operator seeing the timeline has no idea WHEN events happened. Ranked highest-impact gap: an operator paging history has no temporal anchor. This is a deliberate-looking omission (wire has `ts`), worth a follow-up feature card.
2. **tool_call args/result dropped** (SessionHistoryView.swift:293-311) — operator can't see what a tool call actually invoked/returned. Ranked medium.
3. **Dead state `highWaterSeq`/`oldestSeq`** (lines 24-27, assigned 180-181/207-208, never read in body) — the doc-comment's "whole history footer" (line 26) doesn't exist. Not a functional bug; dead code. Low.
4. **system.details dropped** (353-363). Low.

**No code fix required.** The paging is correct (executed proof), all states are distinct, all controls have feedback, no observation bug, no layout or accessibility defect.

## Evidence summary
- Live endpoint answers 200 with valid (currently empty) payload for every project — `/tmp/audit_sessionhist_probe.py`.
- Decode: model_decode_check.sh 48/48 incl. `v1_session_events.json → SessionHistoryResponse` and paging-contract fixture.
- Compile: build_check.sh 0 err / 0 warn.
- Paging no-gap/no-dup: `/tmp/audit_sessionhist_paging.py` (250 events, 2 fetches) + `/tmp/audit_sessionhist_evict.py` (ring eviction).
- Server: hscc-api/tests/test_session_event.py — 27 passed.
