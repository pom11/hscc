# Audit: FleetControlView (t_ecdfbfe5)

Full audit of `ios-app/Sources/HSCC/Views/FleetControlView.swift`.
Fleet up/down are DESTRUCTIVE — confirm-gating and honest feedback are the priority.

Target file (relative to this repo): `ios-app/Sources/HSCC/Views/FleetControlView.swift`

---

## 1. DATA IN

- **Endpoint(s):**
  - Applied-template status → `GET /v1/template/status` via `HSCCClient.templateStatus()`
    (`HSCCClient.swift:706-708`).
  - Cluster up → `POST /v1/cluster/up` via `HSCCClient.clusterUp()` (`HSCCClient.swift:890-892`).
  - Cluster down → `POST /v1/cluster/down` via `HSCCClient.clusterDown()` (`HSCCClient.swift:897-899`).

- **Live read (read-only GET), real values fetched:**
  ```
  GET /v1/template/status
  {
    "applied": {
      "template": "4node-dual-dsv4",
      "applied_at": "2026-08-30T04:01:00",
      "orchestrator_node": "10.0.0.x",   // LAN node — redacted in public docs
      "families": ["reasoning"],
      "units": 2                                // INT here
    },
    "note": "",                                  // empty string
    "speak": "Template {'template': '4node-dual-dsv4', ... 'units': 2} is applied."
  }
  ```
  (fetched via curl from the live `hscc api status` address; token from ~/.hscc/api-token)

- **Does every field the view needs arrive? YES.** View reads: `state.speak`,
  `applied.template`, `applied.applied_at`, `applied.orchestrator_node`,
  `applied.families`, `applied.units`, `state.note`. All present in the live body.
  - `units` arrives as an INT here. `TemplateApplied.units` is `JSONValue?`
    (Models.swift:886) because it can be an int OR a `{total, per_family}` dict —
    the view's `displayJSON` (FleetControlView.swift:157-166) renders int fine.

- **Route sweep** (`scripts/api_route_sweep.py`): `/v1/template/status` → 200 parseable JSON.
  `/v1/cluster/up` and `/v1/cluster/down` are POST-only; GET returns 405, which the
  sweep treats as "route exists; POST-only, not exercised" (verified manually: both
  return HTTP 405 on GET, i.e. registered POST routes). The client POSTs them with
  `confirm: true`. Mutating POSTs deliberately NOT fired during audit.

---

## 2. RENDER

With the live data above, the operator sees (appliedSection, lines 67-109):
- `state.speak` — line 77-80 (italic muted subheadline). NOTE: server `speak` is a raw
  Python repr string (`Template {'template': '4node-dual-dsv4' ...} is applied.`) —
  technical but informative. Not truncated.
- `Template` = "4node-dual-dsv4" — line 83
- `Applied at` = "2026-08-30T04:01:00" — line 86 (raw ISO timestamp, no timezone/localization — ISO has no TZ suffix, so it reads as a naive timestamp; minor)
- `Orchestrator` = "10.0.0.x" — line 89 (**real LAN IP shown to operator**; correct on-device, but an identifiability note for public screenshots)
- `Families` = "reasoning" — line 92
- `Units` = "2" — line 95 via `displayJSON(.int(2))`
- `note` = "" — empty so the info-circle Label at line 100-104 is skipped (good; non-empty notes do render).

**No dropped fields, no wrong units, no truncation hiding meaning.** Client renders the
server's own counts directly (units=2 from the server), no client-side counting that
could disagree. RENDER is faithful.

---

## 3. STATES  ← one real gap

How each state renders in `appliedSection` (lines 67-109):

- **Loading** — `.loading` → `ProgressView()` spinner (line 71-72).
- **Loaded (non-empty)** — detailed rows (line 75-96).
- **Empty** (`applied == nil`, e.g. no template applied yet) — `emptyLabel("No template applied.")`
  (line 97-99): muted `tray` icon + muted text.
- **Failed** — `errorLabel(message)` (line 73-74): red `exclamationmark.triangle.fill`
  + red message (HSErrorLabel, Theme.swift:354-362).
  → **Empty and Failed ARE visually distinct** (muted tray vs red warning). ✔
- **Idle** — NOT handled → `default: EmptyView()` (blank card). Transient only: `.task`
  fires immediately and sets `.loading`, so on-screen flicker is minimal. Minor.
- **Stale/offline — NOT HANDLED (BUG).** `status` never becomes `.stale` because
  `loadStatus()` (lines 54-59) uses a plain `.loading → .loaded/.failed` and does NOT
  use `Offline.load`. And even if it did, `appliedSection`'s switch has NO `.stale`
  case, so a stale state would fall to `default: EmptyView()` — a BLANK card.

  Compare the sibling `TemplatesView` (same data source, `GET /v1/template/status`):
  - `loadStatus` uses `await Offline.load(...)` (TemplatesView.swift:71-77).
  - `appliedCard` switch HAS a `.stale` case rendering `StaleBanner(age:reason:)`
    + last-known data (TemplatesView.swift:112-123).

  **Consequence:** when the cluster is unreachable but the app holds last-known
  (cached) template-status data, `TemplatesView` shows it under a "Can't reach the
  cluster... showing state from X ago" banner — `FleetControlView` shows a BLANK
  Applied Template card (and a hard red error at worst), even though it has the
  same data available. Inconsistent offline behavior across the two surfaces that
  render the exact same endpoint.

  This is the operator-facing "stale/offline" gap. FIXED in this audit (see §FIX).

- **`.stale` never rendered** — see above.

---

## 4. CONTROLS

- **Bring Fleet Up** (`MutationButton`, lines 123-132):
  - Tap → confirmationDialog (MutationSupport.swift:63). Prompt names consequence
    ("starts every serving unit").
  - Confirm → `client.clusterUp()` → POST /v1/cluster/up with `confirm:true`
    (HSCCClient.swift:890-892). Route exists (405 on GET = POST-only). ✔
  - On success: `await loadStatus()` then show success alert with `result.message`.
  - Feedback: MutationButton shows in-flight spinner + disabled (MutationSupport.swift:62),
    then success alert (line 76-78). ✔ visible feedback.
  - Non-2xx throws → failure alert (line 79-82). ✔ honest failure, never a false success.

- **Stop All Workloads** (`MutationButton`, lines 136-146):
  - `destructive: true` → confirm button rendered red with role `.destructive`
    (MutationSupport.swift:65).
  - Confirm → `client.clusterDown()` → POST /v1/cluster/down with `confirm:true`
    (HSCCClient.swift:897-899). Route exists (405 on GET = POST-only). ✔
  - Prompt NAMES the destructive consequence: "shuts down every serving unit across
    the entire cluster and interrupts any in-flight work" (line 140). ✔ strong.
  - Feedback mirrors up: spinner + alert. ✔

  **All controls are confirm-gated, route to live POST endpoints, and give visible
  feedback. No one-tap destruction. GOOD.**

  Every mutation goes through MutationButton. No raw single-tap fire confirmed.

---

## 5. OBSERVATION

- No `ObservableObject` is held in this view. `status` is `@State` over the value enum
  `LoadState<TemplateStatusResponse>` (line 21) — value type, re-renders on set. ✔
- No `@StateObject` keyed by a changing value (the classic "stale first instance"
  bug) — no ObservableObjects at all here.
- `client` is a plain `let` (line 19) — it's a struct value used for calls, not an
  ObservableObject, so no re-render issue. (The infamous `if client != nil {
  await load(client) }` does-not-unwrap trap is NOT present — line 37 uses
  `if client != nil { await loadStatus() }` and loadStatus does `guard let client`
  (line 55), line 39 same. Safe.)
- **OBSERVATION: PASS — no re-render bug.**

---

## 6. LAYOUT

- `ScrollView` → `VStack(alignment: .leading, spacing: 16)` with `.padding()` (lines 25-31).
- Long `speak`/families text wraps; `LabeledContent` rows stack label over value —
  fine on small screens.
- `displayJSON` for `.object`/`.array` returns `<complex>` (line 164) — if `units`
  arrives as a dict (`{total, per_family}`), the operator sees the literal string
  `<complex>` instead of the unit count. **This is a real render gap for the dict
  shape.** The view *handles* the dict type but does NOT render it — it shows
  "<complex>" which hides meaning. Worth fixing: render the dict's meaningful fields.
- Dynamic Type: uses semantic fonts (`.subheadline`, `.caption`, `.headline`) — scales.
  No fixed sizes that would clip. LAYOUT mostly OK.

---

## 7. ACCESSIBILITY

- `MutationButton` labels carry both `Text(title)` and `Image(systemName:)` — the
  text gives a spoken label. ✔
- `LabeledContent` rows have visible text labels (`Template`, `Applied at`, etc.) — ✔
- Color is NOT the only signal for empty vs failed (distinct icons + text). ✔
- The section headers are `Label(title, systemImage:)` — readable. ✔
- No icon-only unbordered controls without a label. ✔ A11Y mostly OK.

---

## FIX (made in this audit)

**Stale/offline handling** — `FleetControlView` now uses `Offline.load` and renders
the `.stale` case with a `StaleBanner` (matching `TemplatesView`), so a reachable-
cluster failure with last-known data shows that data under an explicit "Can't reach
the cluster... showing state from X ago" banner instead of a blank card.

File: `ios-app/Sources/HSCC/Views/FleetControlView.swift`

---

## RANKED RISKS (operator likelihood)

1. **HIGH — Stale/offline blank card.** Cluster unreachable + app has cached status →
   FleetControlView shows blank (or red error) while TemplatesView shows last-known.
   Operator on a flaky tailnet sees this often. FIXED.
2. **LOW — `units` as dict renders "<complex>".** If the applied apply recorded a
   `{total, per_family}` dict, the operator sees the literal "<complex>" instead of a
   count. Only when the applied template recorded per-family units — rare on this
   cluster (live value was int). NOT fixed (see deliberate decision).
3. **LOW — `Applied at` naive timestamp.** ISO string with no timezone/relative
   formatting; reads as naive local. Cosmetic.
4. **LOW — real LAN IP `orchestrator_node` shown.** Correct on-device; only a concern
   for public screenshots. Deliberately not masked in-app (operator needs it).

## DELIBERATE NON-FIXES (with reason)
- **units dict → "<complex>":** rendering a per-family breakdown is a feature decision;
  today's live shape is an int. Flagged for the operator rather than guessed. Would
  be a follow-up card if the operator wants the dict rendered.
- **Applied-at timestamp localization:** cosmetic; no functional bug.
- **Idle → blank flash:** transient; `.task` covers it immediately.

---

## PROOF
- Live GET output for /v1/template/status (see §1) — fetched read-only, real values.
- `scripts/api_route_sweep.py` → "All swept routes answered with parseable JSON"
  (/v1/template/status included).
- POST-only check: `curl -w %{http_code} .../v1/cluster/up` → 405,
  `.../v1/cluster/down` → 405 (route exists, not fired).
- Build check: `ios-app/scripts/build_check.sh` → see final status in kanban summary.
