# Screen audit: ApprovalsView (t_48bbf99b)

Full audit of `ios-app/Sources/HSCC/Views/ApprovalsView.swift` — answers all 7
questions with file:line evidence. Live API read-only testable; address derived
at runtime and REDACTED below (repo is public — real IP never committed).

## Verdict up front
The ApprovalsView core is sound. The end-to-end Allow path is real and
feedback-complete (route exists + confirm-gated + reload-to-remove + success/fail
alerts). Live data decodes populated. One a11y defect fixed. All other findings
are minor / low-likelihood and ranked below.

---
## Q1 DATA IN — verified live, decodes populated ✓
- Endpoint: GET `/v1/kanban/blocked` (HSCCClient.swift:648-650).
  Model: `KanbanBlockedResponse` / `BlockedCard` (Models.swift:779-815).
- Route registered: backend hscc-api/routes_kanban.py:309-310.
  Sweep: `scripts/api_route_sweep.py` → `ok 200 /v1/kanban/blocked` (executed).
- Executed decode proof: fresh `scripts/capture_live.sh` (20260902_220420) →
  `scripts/live_decode_check.sh` → `DECODE+ v1_kanban_blocked.json → KanbanBlockedResponse [POPULATED]`.
  The real model decodes the real wire; nothing is silently-all-nil.
- Live values (read-only): count=7, boards=15, errors=[], speak="7 cards blocked across 15 boards."
  Every field the view reads decodes: board, id, status, assignee, age_days,
  block_kind, why, title, comments. No field the view needs is missing.

## Q2 RENDER — no dropped fields; count agrees (current data) ✓
- Header `Label(response.speak, …)` (ApprovalsView.swift:102) → "7 cards blocked across 15 boards."
- Server `count`=7 == client-side `pending`=7 (line 92 filters `.isPendingApproval`);
  NO client/server count disagreement in current live data.
- kind labels: 6/7 block_kind=null → "Unclassified" (line 189); 1/7 needs_input →
  "Needs a decision" (line 187).
- `why` (lines 148-150): guard suppresses only the exact `"kind=<kind>"` echo.
  Live: 5 cards have `why="(no block reason recorded)"` → that noise IS shown as a
  caption; 2 cards (t_b91cd750, t_2472675d) have real protocol-violation text →
  shown (good); t_b0091d92's `why="kind=needs_input"` → correctly suppressed.
- comments: all empty live → no bubbles. `comments.prefix(2)` (line 153) hides
  comments beyond the first two — deliberate, low.
- `displayTitle = title ?? id` (Models.swift:790); titles are multi-line (no
  lineLimit) so long titles wrap, not truncate. ✓

## Q3 STATES — 0-results and failed NEVER look the same ✓
- `.idle` → `ProgressView("Loading…")` with `.task { loadApprovals }` (lines 68-69).
- `.loading` → `ProgressView("Loading…")` (lines 70-71).
- `.loaded(empty)` → List shows header + green "No pending approvals." (lines 105-109).
- `.failed(msg)` → `ContentUnavailableView` "Couldn't load approvals" + real message +
  "Try again" button (lines 72-79).  ← distinct from empty.
- `.stale(v, ageMsg)` → data + `StaleBanner` (lines 80-81, 94-100) — clearly
  marked "Offline — showing state from N ago" (Theme.swift:416), distinct from
  a live empty screen.
- Not configured → `notConfiguredView` "Connect to your cluster" (lines 48-63),
  distinct from all the above.

## Q4 CONTROLS — Allow is real, confirm-gated, and feedback-complete ✓
- Allow (line 168): `MutationButton` → tap only arms `.confirmationDialog` naming
  the card (MutationSupport.swift:49-63); Confirm → `recoverBlockedCard(card.id)`
  → POST `/v1/kanban/blocked/{id}/recover` body `{confirm:true}` (HSCCClient.swift:878-885).
- Backend route registered routes_kanban.py:312-313; `_require_confirm`
  (line 169) matches the confirm body; success → 200 `{id, board, reason, message,
  speak}` (line 185-192) which maps to `RecoverCardResponse` (Models.swift:818-824).
- Feedback: after success the row re-fetches (`loadApprovals`, line 174) and the
  card disappears; `MutationButton` alerts "Done: <server message>" on success and
  "Failed: <error>" on failure (MutationSupport.swift:73-84). A failure can never
  render as success (client throws on non-2xx). ✓
- NOTE (reasoning, cannot mutate live): recover is the ONE backend mutation for a
  blocked card; "Don't allow" is deliberately a no-op (documented lines 19-26).
- StaleBanner retry (lines 96-97 → Theme.swift:426) and `.refreshable` (line 122)
  both re-run `loadApprovals`. Feedback present.

## Q5 OBSERVATION — clean ✓
- ApprovalsView holds NO ObservableObject; `@State private var approvals =
  LoadState<KanbanBlockedResponse>.idle` (line 33) on a value type is correct.
  No plain-`let` ObservableObject, no @StateObject keyed by a changing value.
- `ApprovalPoller` (badge) is `@StateObject` at ContentView.swift:24, keyed to the
  app-lifetime ContentView (NOT to a changing value); `setClient` re-arms on
  launch + connection identity change (ContentView.swift:56,61). ✓ Same
  .isPendingApproval classification as the inbox (ApprovalBadge.swift:45).
- Minor: after an Allow the inbox empties immediately but the tab badge reflects
  it only on the next 60s poll (ApprovalBadge.swift:22,35). Documented design
  tradeoff, low.

## Q6 LAYOUT — Dynamic Type / SE width: survives, one soft spot (reasoning)
- Long titles wrap (no lineLimit) — safe. Comments/why use .caption with
  maxWidth .infinity — safe.
- Soft spot: row HStack (lines 131-145) packs assignee + board + age + Spacer +
  trailing kind label ("Unclassified" / "Needs a decision" / "Blocked on access").
  On ~320pt width a long assignee (e.g. "backend-engineer") plus a long kind label
  could truncate the trailing label with "…". Low likelihood the operator hits it
  (labels are short in practice). Not executed (no iOS runtime here) — reasoning.

## Q7 ACCESSIBILITY — one defect found & FIXED, rest clean
- FIXED: per-row Allow buttons all had the identical label "Allow" —
  indistinguishable to VoiceOver. Now `.accessibilityLabel("Allow " + displayTitle)`
  (ApprovalsView.swift:181-184). compile-clean.
- Not color-only anywhere: kind label has hand.raised icon + text (line 142);
  "No pending approvals." has checkmark.seal + text (line 107); Allow has
  checkmark.seal icon + "Allow" text (line 170). ✓
- StaleBanner retry has `.accessibilityLabel("Retry loading")` (Theme.swift:428,432). ✓
- notConfiguredView / header / empty all carry real text. ✓

---
## Ranked findings (by likelihood the operator hits it)
1. (LOW) Header `speak` counts ALL blocked cards, inbox lists only pending. If a
   dependency/transient block ever coexists with pending ones, the header reads
   "N cards blocked" but the list shows fewer. Currently coincident (all 7 pending).
   No visible count label bugs because the badge + row count both use pending.
2. (LOW) "(no block reason recorded)" displayed as a caption on 5 live cards —
   honest but noisy; the one real recorded reason (t_b0091d92) is filtered as a
   kind-echo. Not clearly broken.
3. (LOW) Tab badge lags up to 60s after an Allow (poll-based). Documented.
4. (LOW) comments.prefix(2) hides 3rd+ comments. Deliberate.
5. (VERY LOW) Trailing kind label in row HStack may truncate on SE width with a
   long assignee. Reasoning only.

## What I fixed
- ApprovalsView.swift:181-184 — per-row Allow button gets a card-naming
  accessibility label. (5e68bea). build_check clean (0 warnings), sources in sync.

## What I deliberately did not fix and why
- Header speak vs pending mismatch: only material if dependency/transient coexist
  with pending; not currently reachable; the view semantics are defensible (the
  header is about blocked cards, the list about approvals). Would need design
  input to change copy — out of scope for a clear-bug fix.
- "(no block reason recorded)" caption: honest fallback text, not a bug.
- Recover-mutation live test: deliberately NOT run (mutates live state).
- Tab badge 60s lag / comments.prefix(2): documented design tradeoffs.

---
## Evidence inventory (all executed unless marked "reasoning")
- api_route_sweep.py: `ok 200 /v1/kanban/blocked`
- capture_live.sh + live_decode_check.sh: `DECODE+ v1_kanban_blocked.json → KanbanBlockedResponse [POPULATED]`
- build_check.sh: `HSCC 57 files 0 warnings` (after fix)
- check_sources.sh: `62 Swift files, all listed in project.yml`
- Live field dump (read-only) of /v1/kanban/blocked — values in Q1/Q2.
- Backend route audit (routes_kanban.py) for recover — code inspection.
