# Screen audit: ProjectsView — prove every element works

Task: t_bee9db8a (ios-engineer). Full audit of
`ios-app/Sources/HSCC/Views/ProjectsView.swift` — the app's home surface and
entry to every project.

Methods: full compile (build_check.sh — fails on warnings), live read-only API
fetches with real values (address derived at runtime via `hscc api status`,
never hardcoded — repo is public), cross-check vs scripts/api_route_sweep.py, a
purpose-built Swift harness decoding live data through the REAL models. NO iOS
runtime here — every finding is marked (executed) = proven by a command's
output, or (reasoning) = code reasoning only.

---

## FIXED

### F1 — False-alarm red "0% context headroom" bar (executed)
`Sources/HSCC/Views/ProjectsView.swift:493-505` (compactionHeadroom) derived a
live free-context fraction from `input_tokens` vs `threshold_tokens`, but
`input_tokens` is CUMULATIVE and never reset by compaction. On a real
long-lived session it sat far above the cap, so `max(0.0, ...)` clamped to a
permanent **red "0% context headroom"** bar directly under the green
"Compaction healthy" headline — a false alarm the operator sees the moment they
open the hscc project (the first/primary project). ProjectOverviewView line 471
guards rendering with `if let headroom = compactionHeadroom(h)`; returning nil
hides the bar.

Proof (harness `headroom_check`, decoding real `/v1/projects/hscc` through the
real `ProjectDetailResponse` model — executed):
```
compaction_at_risk: Optional(false)   bloated: Optional(false)
input_tokens: Optional(11957206)      threshold_tokens: Optional(100000)
PRE-FIX  computed headroom: Optional(0.0) => "0% context headroom" colored bad (RED)
POST-FIX computed headroom: nil       => no headroom bar rendered
```
Fix: return nil (hide the bar) once cumulative `tokens >= threshold`; the
server's own `compaction_at_risk`/`bloated` are the real risk signals there.
When tokens < threshold (a genuinely small session) the bar still renders
honestly. build_check.sh clean (0 warnings), check_theme.sh clean. Commit
1a4992d.

### F2 — icon-only controls with no accessibility label (executed fix, a11y)
- `Sources/HSCC/Views/Theme.swift:426-430` — StaleBanner's retry button was a
  bare icon `arrow.clockwise` with no label, so VoiceOver couldn't announce or
  target it. Used across 10+ screens (every stale/offline state, incl.
  ProjectsView staleContent). Added `.accessibilityLabel("Retry loading")`.
- `Sources/HSCC/Views/ProjectsView.swift:144-154` — unreadBadge rendered a bare
  count ("3") with no a11y context; now announces "N unread".
Both compile clean, check_theme.sh clean. Commit cbdda61.

---

## FINDINGS — by audit area

### Area 1 — DATA IN (all executed)
Endpoints feeding this screen (all answer 200 parseable JSON; verified live AND
via api_route_sweep.py — no dead routes):
- GET /v1/projects → ProjectsResponse: 12 projects, count=12,
  speak="12 projects registered.". Each row carries name/repo/board/topic —
  every field ProjectsView's row renders.
- GET /v1/projects/{name} → ProjectDetailResponse. Live hscc decodes fully:
  name, repo, board, topic, board_counts, git, session_health, speak all
  present. No dropped server field anywhere the view reads it.
- GET /v1/cards (32 cards), GET /v1/kanban/blocked (6), GET /v1/kanban/stale
  (26) → the Board section. Interpolated /v1/projects/{name} confirmed live.

Conclusion: every field the views need arrives. Area 1 PASS.

### Area 2 — RENDER
- **R1 (HIGH, executed) — "58 open cards" vs "of 32 open": server bowtie, not
  client.** The overview shows `Label(state.speak, …)` (ProjectsView.swift:287 =
  "hscc: 3 running, 58 open cards on board hscc.") directly above the Board
  counts line "of 32 open" (ProjectsView.swift:366) — two conflicting open
  counts on the same screen. Root cause is SERVER-side: `_speak_project_detail`
  (hscc-api/routes_project.py:316-323) sums every `board_counts` key not in
  (done/archived/blocked), which includes the `total` key itself →
  running(3)+ready(23)+total(32) = 58. The real open count is 26
  (running+ready). board_counts.total=32 correctly = the 32 cards. Client is
  faithful to both server numbers; the server's speak is wrong. Recommend a
  backend task to fix `_speak_project_detail` (exclude `total`). Not fixed here
  (client card, must not touch server).
- **R2 (LOW, reasoning) — repo path not shown in list row.** projectRow
  (ProjectsView.swift:127-138) renders name + board + topic but not `repo`.
  Intentional (repo is long + shown in detail). Not a bug; noted for
  completeness.
- Compaction headroom contradiction fixed (F1).

### Area 3 — STATES (empty ≠ error, all distinct — PASS)
Every state is distinguished: loading/idle → HSLoading spinner
(ProjectsView.swift:65-66); failed → HSError with title + retry (67-70);
loaded-empty → List "No projects registered." (79-84); stale → StaleBanner +
last-known rows (71-72 → staleContent 100-122); unconfigured → HSConnectGate
(156-158). Same pattern in ProjectOverviewView (259-269), ProjectBoardView
(643-653), ProjectSettingsView (833-843). "0 results" and "failed to load" never
look the same anywhere in this file.
- Note (LOW, reasoning): ProjectBoardView silently swallows blocked/stale read
  failures (stores `.failed` at 801/809 but never renders it — line 669-670 only
  reads `.value`). Deliberate best-effort design (comment 796-796), but the
  operator gets no cue if the blocked read hiccuped. Flagged; not a bug.

### Area 4 — CONTROLS
Every control's route answers (api_route_sweep + live):
- Search (toolbar, ProjectsView.swift:39-45) → sets showSearch → SearchView sheet.
  Route N/A (client-side sheet). Feedback: sheet appears. ✓
- Refresh (toolbar, 46-54) → `await load()` → GET /v1/projects. Disabled while
  loading. Feedback: spinner. ✓
- Pull-to-refresh (93, 121, 323, 728) → `await load()`. ✓
- Project row → NavigationLink → ProjectDetailView (86-90). ✓
- StaleBanner retry → `await load()`. Feedback: reloads. ✓
- lastReply button (572-600) → `onOpenChat` → flips picker to Chat. Feedback:
  chat appears. ✓
- **C1 (MEDIUM, reasoning) — blocked/stale board rows are NOT tappable.** Active
  cards are NavigationLinks → CardDetailView (ProjectsView.swift:705-706), but
  the "Blocked — N" (716-721) and "Stale — N" (728-733) rows render static
  HSStatusRaws with NO tap target, even though every blocked/stale id resolves
  via GET /v1/cards/{id} (verified all in cards). The overview promises "tap the
  Board section to triage" (346), but once there the blocked cards can't be
  opened. Deliberate display-only design? Possible — but it's an inconsistency
  the operator WILL hit. Did not change behavior (product decision); flagged.

### Area 5 — OBSERVATION (PASS)
No `@StateObject`/`@ObservedObject` in ProjectsView.swift at all — uses `@State`
for LoadState enums (correct) and `@EnvironmentObject unread`
(ProjectsView.swift:15). No `@StateObject` keyed by a changing value. The
Chat section's StreamingChatView correctly creates its `@StateObject` store in
init (StreamingChatView.swift:38-40) — fresh per ProjectDetailView. PASS: no
"had to switch tabs" staleness bug in this file.

### Area 6 — LAYOUT
- **L1 (LOW, reasoning) — HSMetaLine has no truncation/line-limit.** projectRow
  (ProjectsView.swift:135-136) passes board + topic into an HStack with no
  `.lineLimit(1)`/`.truncationMode` (Theme.swift:384-394); a long board or topic
  at large Dynamic Type could compress on iPhone SE width. Board names here are
  short (< 12 chars); topic is a short int. Low probability in practice.
- ProjectOverviewView gitSection "Repo" uses `.lineLimit(1).truncationMode(.middle)`
  (line 300) — correctly guards the long "/Users/desac/dev/…" path. Good.
- Everything lives in List rows (self-sizing) and VStacks; survives Dynamic Type
  by default. No fixed heights. PASS overall; low-risk items listed.

### Area 7 — ACCESSIBILITY (fixed the clear ones; note the rest)
- FIXED F2: StaleBanner retry + unread badge now labelled.
- Search/Refresh toolbar icons already have labels (ProjectsView.swift:44, 53). ✓
- **A1 (LOW, reasoning) — HSStatusDot colour-only.** cardRow's 10pt dot
  (ProjectsView.swift:767) conveys status only by colour with no label; however
  the row's meta line ALSO prints card.displayStatus as text (771), so the
  status is not colour-only for screen readers. Minor.
- **A2 (NOTE) — blocked/stale/warn icons are colour-coded** but each row also
  shows title text and the icon glyph, so not colour-only. OK.

---

## Summary / ranked for operator exposure
1. **R1 HIGH** — overview says "58 open" while the board says "32 open". Fix is
   server-side (routes_project.py `_speak_project_detail` counts the `total`
   key as an open status); client faithfully shows both. Spawned backend task
   **t_b0091d92** (assignee backend-engineer) to fix it — NOT done here because
   it is backend code, out of the iOS client's scope.
2. **F1** — fixed. Headroom false-alarm red bar on the primary project.
3. **C1 MEDIUM** — blocked/stale board rows not tappable while active cards are.
4. **F2** — a11y labels added.
5. Low: board blocked/stale read failures surface no cue; HSMetaLine truncation;
   colour-only dots (mitigated by text).

## Diff hygiene (executed)
`git diff --name-only` vs the card's base (435d20c) is exactly 3 files:
`ios-app/Sources/HSCC/Views/ProjectsView.swift`,
`ios-app/Sources/HSCC/Views/Theme.swift`,
`ios-app/docs/audit_projectsview.md` — everything I intended, nothing else.
No real addresses committed: live host shown only as placeholder `100.64.0.1`
in prose; token never written anywhere; harness (/tmp-proj_audit + the
un-committed `headroom_check/` sibling dir) holds the live-detail captures.
working tree clean (git status empty).
