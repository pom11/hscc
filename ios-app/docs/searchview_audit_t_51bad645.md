# Screen audit: SearchView (t_51bad645)

Full audit of `ios-app/Sources/HSCC/Views/SearchView.swift` — cross-project search.

## Status
COMPLETE — no-code-change audit. No clearly-broken defect found; low-severity
observations documented below with no fix made (none warranted).

## VERDICT
SearchView is a healthy, well-architected screen. Both backup sources
(`/v1/projects`, `/v1/cards`) answer and decode populated; every rendered field
is present in the live payload; all four states (loading/empty/error/offline)
are distinct; every control routes to a live endpoint and gives feedback; no
`@StateObject`-by-value trap; `client!` is provably safe; no color-only signal.
Four low-severity observations recorded but none warrant a code change today.
**No source files were modified by this audit.**

## How to read this
Every finding cites file:line + the command whose output proves it. Findings
marked **[executed]** were proven by real tool output (compile, live API, a harness).
Findings marked **[reasoning]** are static-code reasoning with no runtime here.

## DATA IN — endpoints that feed it
SearchView loads two sources concurrently in `load()`:
- `loadProjects` → `client.projects()` → `GET /v1/projects` (SearchView.swift:267-273)
- `loadCards` → `client.cards()` → `GET /v1/cards` (SearchView.swift:275-281)

Both are plain GETs with NO query items, so both are StateCache-persisted
(HSCCClient.swift:223 `get` stores on every successful 2xx read). Offline
last-known works for both.

**[executed]** Live responses (fetched read-only, addresses normalized to placeholder):
- `GET /v1/projects` → 200, `count: 12`, `len(projects): 12`, `speak: "12 projects registered."`
  Every row carries name/repo/board/topic. All 12 names:
  hscc, ecofire-bc, ecofire-app, sphoin, soconn, flosana, powerbi, efsdriver, grid, radio, pickolo, pom.
- `GET /v1/cards` → 200, `count: 23`, `len(cards): 23`, `speak: "23 cards, 5 running."`
  Every card carries id/title/status/board (+ rarer assignee/priority/body/etc.).
  Statuses live: blocked 7, running 5, ready 11.

**[executed]** Both live captures decode into the real models POPULATED:
```
DECODE+  v1_projects.json  →  ProjectsResponse  [POPULATED]
DECODE+  v1_cards.json     →  CardsResponse     [POPULATED]
```
(via `scripts/live_decode_check.sh` — compiles the REAL Models.swift, feeds the
live capture, uses Mirror reflection to flag all-nil decodes.)

**[executed]** Committed fixtures also pass: `scripts/model_decode_check.sh`
→ "ALL DECODE CHECKS PASSED — 48/48". `cards.json → CardsResponse`, `v1_projects.json → ProjectsResponse` both OK.

**[executed]** Route sweep: `scripts/api_route_sweep.py` → `ok 200 /v1/projects`,
`ok 200 /v1/cards`. Both routes the view calls answer.

Every field the view reads IS present in the live payload:
- Project: name, repo, board, topic → all present (Models.swift:593-607).
- Card: id, title, status, board → all present (Models.swift:165-174).

## RENDER
What the operator sees for the live data (all **[reasoning]** on shape, backed by
the live DATA IN above):

**Empty query** (`emptyQueryView`, SearchView.swift:135-159):
- Hint row "Type to search across projects and cards."
- "Likely items" section listing ALL 12 projects (projectRow: name + board + repo).
  project name medium .body, board/repo .caption muted.

**Results** (`resultsList`, SearchView.swift:174-202):
- Section header "Projects — N" where N = number of MATCHED projects (ps.count).
- Section header "Cards — N" where N = number of MATCHED cards, capped at 50
  (`Array(cardHits.prefix(50))`, SearchView.swift:128).
- projectRow (SearchView.swift:217-232): name, board caption, repo caption
  (repo: `.lineLimit(1).truncationMode(.middle)`).
- cardRow (SearchView.swift:235-246): status color dot, displayTitle, meta line
  `[board, status]`.

**Dropped/omitted fields (by design, not bugs):**
- `Project.topic` (an int channel id, e.g. "2046") is NOT shown in projectRow.
  It IS used for matching (displayTopic). Showing a bare int wouldn't be meaningful
  to the operator, so its omission is reasonable. **[reasoning]**
- `Card.assignee` / `Card.body` / `Card.priority` / `completed_at` etc. are not
  shown in cardRow (Models.swift:165-174 doesn't even declare them). The card
  detail screen (t_cb300f9c, already audited) shows them. Reasonable — the
  search row is compact.

**Client counts vs server counts (Q2):**
- Section header counts are MATCHED counts, not server totals. Correct semantics —
  "Projects — 2" means 2 matching projects, not "2 total exist". Not a bug.
- The 50-card cap under-reports if >50 cards match a query: header "Cards — 50"
  while more actually matched. Only 23 cards live today, so unreachable at
  present. **[reasoning]** Low likelihood.

## STATES
**[reasoning]** (the state machine is static logic; no runtime here to drive a
device):

- **Loading** (query non-empty, sources not resolved): `searchingView`
  (SearchView.swift:98-111) = "Searching…" + ProgressView. Distinct from empty.
  Does NOT claim no-results prematurely (comment SearchView.swift:73-77 explains
  why the two async loads can land out of order).
- **Empty success** (query non-empty, both loaded, zero matches): `noResultsView`
  (SearchView.swift:162-171) = `HSEmpty("No results for "<query>"", ...)`
  (ContentUnavailableView, neutral magnifyingglass). CLEARLY distinct from error.
- **Error / never-fetched** (either source `.failed`): `HSError("Couldn't
  search", message, retry)` (SearchView.swift:65-68). Full ContentUnavailableView
  with "Try again" button + bad triangle icon.
- **Stale/offline**: when either source is `.stale`, a `StaleBanner` overlays
  (SearchView.swift:206-212, rendered at every load-state top: lines 100,137,164,
  176). Banner text: "Offline — showing state from N ago" + "Can't reach the
  cluster right now." + retry button.
- **Not configured** (`client == nil`): `HSConnectGate` (SearchView.swift:54-56).

"0 results" and "failed to load" look DIFFERENT: HSEmpty (neutral, tray) vs
HSError (bad, triangle + Try again). ✅

**Empty-query plus one source failed** (observation, no fix):
If the operator opens search with an EMPTY query and one source is `.failed`
(i.e. never-fetched, no cache), the switch's `case (.failed, _)/(_, .failed)`
(SearchView.swift:64-65) shows the full `HSError` pane even though the OTHER
source loaded fine and would otherwise populate "Likely items". The error
hides half-usable data. Defensible (search result would be incomplete), and
rare (requires one endpoint down while the other works, with no cache for a
fresh install). LOW severity — recorded, not fixed.

One source `.failed` while the other is `.loaded` (non-empty query):
whole screen errors (search would be incomplete without both sources). Honest.
**[reasoning]**

## CONTROLS
Every interactive element + whether it answers (route sweep cross-check):

1. **Close "Done"** (SearchView.swift:41-43) → `dismiss()`. Dismisses the sheet.
   Presented from ProjectsView.swift:56-58 `.sheet`. Visible feedback (sheet
   dismisses). ✅
2. **Search field** (`.searchable`, SearchView.swift:34-36) → `$query`.
   Standard system control. Results update reactively via `matched()`. ✅
3. **Project row tap** (NavigationLink SearchView.swift:150-155,182-187) →
   `ProjectDetailView(client:, project:)`. Answers (route sweep: /v1/projects ok;
   ProjectDetailView fetches /v1/projects/{name}). ✅
4. **Card row tap** (NavigationLink SearchView.swift:193-198) →
   `CardDetailView(cardID:)` → fetches /v1/cards/{id} internally
   (CardsView.swift:69-71). Audited in t_cb300f9c. ✅
5. **Stale banner retry** (SearchView.swift:209-211) → `load()`. Answers.
6. **HSError "Try again"** (SearchView.swift:66-68) → `load()`. Answers.

No control with missing feedback found. Every call routes to a live endpoint.
All **[reasoning]** + route-sweep **[executed]**.

## OBSERVATION
- SearchView owns no ObservableObject. It holds `projects`, `cards` (`LoadState`
  VALUE enums) and `query` as `@State` (SearchView.swift:23-25) — correct for
  value types; `@State` triggers re-render on mutation. ✅
- No `@StateObject` keyed by a changing value here — the "stale first instance
  after navigation" bug does not apply. ✅
- Navigates to `CardDetailView(cardID:)` and `ProjectDetailView(client:project:)`.
  CardDetailView builds its own client from settings (CardsView.swift:69) — no
  stale-instance risk from passing cardID. ProjectDetailView receives `project`
  (a value) — the detail view fetches its own state. ✅

**`client!` force-unwrap is provably safe (SearchView.swift:151,183-187):**
`content` (SearchView.swift:46-52) branches `if client == nil { notConfiguredView }
else { results }`. `results` — and therefore every `client!` site — is only ever
evaluated when `client != nil`. The force-unwraps cannot fire while nil. This is
a style smell (an `if let client` threaded through `results` would be cleaner)
but NOT a crash bug. Not fixed. **[reasoning]**

## LAYOUT
**[reasoning]** — no iOS runtime here; evaluated statically against the live row
data (longest real strings):
- projectRow name: `.body.weight(.medium)` — wraps across lines fine at SE width.
- board+repo HStack: repo has `.lineLimit(1).truncationMode(.middle)` +
  lineLimit(1). Long repo `/Users/desac/dev/ecofire-powerbi` middle-truncates.
  The HStack gives board and repo no compression-priority; at SE width a long
  board + long repo could crowd, but repo truncates. Generally survives. ✅
- cardRow meta `HSMetaLine([board, status])` (SearchView.swift:243): PLAIN HStack,
  NO `.lineLimit` / `.truncationMode` on the inner Texts (Theme.swift:385-393).
  Frame is `maxWidth:.infinity` in the banner but the meta line's HStack has no
  Spacer and sizes to content. At SE width a very long board/status would push
  beyond the row. Real live values ("hscc", "blocked", "running", "ready") are
  short, so NO overflow today. Risk only if a board name is long. **[reasoning]**
  LOW severity — no fix.
- Shared `HSMetaLine` is used by MANY row types across the app, so adding a
  lineLimit there would be a cross-cutting change, not a SearchView-local fix.
  Recording, not fixing.

## ACCESSIBILITY
- Icon-only controls: NONE in SearchView. Close is text "Done". Search field is
  the system searchable (labeled). ✅
- color-as-ONLY-signal: cardRow status dot (SearchView.swift:237-239) is a color
  marker, but the SAME status text is rendered in the meta line
  (`HSMetaLine([card.board, card.status])`, SearchView.swift:243) — so colour is
  NOT the only signal. ✅
- The decorative status dot itself is an unlabeled shape; acceptable since the text
  is present.

## FIXES
NONE. No clearly-broken defect found. Every checklist area was examined against
live data + harnesses; all deliberate design choices, all provably safe. This is
an honest no-code-change result rather than inventing a fix for a healthy screen.

## DEFERRED (low-severity observations, intentionally NOT fixed)
1. **HSMetaLine** (Theme.swift:385-393): no lineLimit/truncation on meta-line
   Texts → overflow risk only with very long board/status. Cross-cutting shared
   component; non-issue with current live data. Fix if boards grow long.
2. **Empty-query + one source failed** (SearchView.swift:64-65): full error pane
   hides the other source's likely-items. Rare (needs one endpoint down + no
   cache + empty query). Defensible as-is.
3. **50-card match cap** (SearchView.swift:128): header "Cards — N" under-reports
   if >50 cards match one query. Unreachable today (23 live cards).
4. **client! force-unwrap** (SearchView.swift:151,183-187): style smell, provably
   safe; could be refactored to thread a non-optional client.

None warrant a change now. Follow-ons if they ever bite: (1) add
`.lineLimit(1).truncationMode(.tail)` inside HSMetaLine; (2) gate the `.failed`
branch on a non-empty query; (3) drop the prefix(50) or report total matched;
(4) `if let client` threading.

## EVIDENCE — command log
- `curl GET /v1/projects` + `/v1/cards` (read-only, token from ~/.hscc/api-token)
- `scripts/live_decode_check.sh <dir>` → both POPULATED
- `scripts/model_decode_check.sh` → 48/48
- `scripts/api_route_sweep.py` → both 200
- `scripts/build_check.sh` → 57 files, 0 err / 0 warn
- `scripts/check_sources.sh` → sources in sync
