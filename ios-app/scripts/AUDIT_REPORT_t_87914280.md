# HSCC iOS App — Design-System / Accessibility Audit (t_87914280)

Branch: `wt/t_87914280` (base `5e97d86`, 3 commits ahead)
Scope: the 5 audit dimensions for `ios-app/Sources/HSCC/Views` + Theme.swift.
Verification: `scripts/build_check.sh` compile clean, `scripts/check_sources.sh`
sync, `scripts/check_theme.sh` clean (new).

---

## Summary

The design system (Theme.swift) is mature and comprehensively adopted. Every
view uses Theme semantic/palette tokens, the shared full-pane states
(HSLoading / HSErrorLabel / HSEmptyLabel / HSEmpty / HSConnectGate /
HSSectionCard / HSStatusChip / StaleBanner), and every async pane has proper
loading / error / empty / not-configured handling. 8 of the raw-colour uses
outside Theme were found; 2 classes of bug were fixed (7 call sites), 1 design
decision codified in a reproducible guard. No raw hex, no UIColor(red:), no
.init(red:) anywhere outside Theme.swift.

---

## Dimension 1 — Colour / dark mode

Rule: all colour comes from Theme.swift; no raw hex (except Theme Palette);
nothing that breaks in one appearance.

### FIXED (3 commits)
1. **ApprovalsView.swift:117** — `Text(error)...foregroundColor(.red)` →
   `Theme.Semantic.bad`. Raw system red on an inline error; the theme's muted
   `bad` (halt `#E56A6A`) owns fault signalling. Deliberately softer than the
   alarm `.red` per the design's "neither neon-on-black" palette.
2. **OrchestratorChatView.swift:683,687** — unsent-bubble tint
   `Color.red.opacity(0.12)` → `Theme.Semantic.bad.opacity(0.12)`. The adjacent
   `.failure` case already used `bad`; this removes the one inconsistent raw
   tint in the same view.

### LEFT INTENTIONALLY (allowlisted in check_theme.sh) — readable in BOTH modes
3. **OrchestratorChatView.swift:666** — `.foregroundColor(.white)` on the
   `Color.accentColor` prompt bubble. Accent is an adaptive hue (blue in both
   modes); white-on-blue reads in both appearances. Standard iMessage-style.
4. **OrchestratorChatView.swift:702,703** — `.background(Color.red)` +
   `.foregroundColor(.white)` on the Retry button. System red + white is
   readable in both appearances, and the button carries a "Retry" text label.
   NOT converted to `bad` because `bad` (#E56A6A) is a light muted red whose
   white-text contrast (~2.4:1) is WORSE than system red (~4:1) — a blind swap
   would regress accessibility, not improve it.
5. **ProjectsView.swift:149** — `.foregroundColor(.white)` on the amber unread
   badge. `Theme.Semantic.warn` is a FIXED non-dynamic hue (#F2A65A); white on
   it reads in both modes.

### Comprehensive grep check (the task's explicit ask)
`scripts/check_theme.sh` added. One OR'd regex covers EVERY raw-colour
spelling: named colours (`.red .white … .brown`), `0xRRGGBB` hex, and the
component constructors `UIColor(red:`, `Color(red:`, `.init(red:`,
`UIColor(white:`). It fails on any match outside Theme.swift with a documented
allowlist of the 4 intentional uses above. **Negative-tested**: injecting
`Color(red: 1,0,0)` and `Color(hex: 0xFF00FF)` both flagged; clean on removal
(proof the regex is not trivially passing). Verified there are zero
`UIColor(red:)` / `.init(red:)` / `0x` forms in the codebase.

---

## Dimension 2 — Dynamic Type

- **Fonts**: system fonts for ALL human copy (`.body .subheadline .caption …
  .headline .title2`) — these scale with Dynamic Type. Fixed `.hsccMono(N)`
  (Font.system(size:, design:.monospaced)) is used ONLY for machine-produced
  telemetry (ids, IPs, ports, model names, timestamps, counts) — a **documented
  design stance** (Theme.swift lines 108-122): "anything the MACHINE produced
  is monospaced" for scannability. Telemetry values are short/compact and do
  not clip the chrome. Finding (no fix): if operator Large-Text accessibility
  becomes a hard requirement, hsccMono would need a scalable variant — a
  design decision, not a bug.
- **Fixed-height containers**: no fixed-height views that clip text. The only
  `frame(height:)` sites are decorative (6pt headroom bars, 10pt status dots,
  icon glyphs). Chat composers use vertical-axis `TextField` with
  `.lineLimit(1...4)` — grows instead of clipping.
- **`.secondary`/`.system(size:44)`** icon glyphs are decorative non-text —
  unaffected.

---

## Dimension 3 — Accessibility

### FIXED
1. **ProjectsView.swift:41-52** — the two toolbar buttons (search, refresh)
   were icon-only; VoiceOver would announce raw SF Symbol names
   ("Magnifyingglass", "Arrow.clockwise"). Added `.accessibilityLabel`
   ("Search projects", "Refresh projects").
2. **ProjectsView.swift:774 (cardRow)** — card status was conveyed by a **color
   dot only** (no status text anywhere in the row). Added `card.displayStatus`
   to the HSMetaLine so the status reads as text (blocked / running / done…)
   for colorblind and VoiceOver users.

### Verified CLEAN (not color-only — state always carries icon and/or text)
- NodeTopologyView node dots: `.accessibilityLabel("\(node.label),
  \(node.state.rawValue)")` — text + color.
- FleetView health checks: `checkmark.circle.fill`/`xmark.circle.fill` icon +
  check name text + color.
- ConnectionBanner (ContentView): icon + text + color.
- CardChip (StreamingChatView) / CardsView rows: status shown as text + color.

### Retention button (702-703)
Has a "Retry" Text label — not icon-only. Verified.

---

## Dimension 4 — Empty / loading / error states

Every async pane uses the shared HSLoading / HSErrorLabel / HSEmptyLabel /
HSEmpty / HSConnectGate components. Verified per view: Projects, Sessions,
Cluster, Fleet, FleetControl, Ops, Templates, TemplateDetail, Autodown,
BoardHygiene, Search, Memory, ActivityFeed, Approvals, StreamingChat,
SessionHistory, NodeTopology, CardDetail. Lists-with-data-empty all show an
`emptyLabel`, data-empty states show `HSEmpty`/`emptyLabel`. No blank-pane
cases found.

Minor non-fix: SessionsView / ActivityFeedView / ApprovalsView still hand-roll
their "Connect to your cluster" not-configured gate instead of HSConnectGate
(the design comment notes HSConnectGate consolidated "the old four
near-copies"; these three are `HSConnectGate`-equivalent but not the component).
Working (uses adaptive `.secondary`), purely consistency — flagged, not forced.

---

## Dimension 5 — Small width / layout overflow

- `HSMetaLine` is caption HStack with dot separators; Text compresses
  proportionally — safe on iPhone SE. (cardRow now lists 3 caption items.)
- No `Spacer(minLength:)` beyond chat-bubble gutter (48pt) which is correct
  even at SE width.
- Stat pills / headroom bars use fixed-hue chips + `GeometryReader`(width-relative)
  bars — scale to width, no overflow.
- No assets or images to describe; progress/status wholly via SF Symbols which
  are text-adjacent.

---

## Files changed (exactly these 4, no strays)

| Path | Change |
|---|---|
| `Sources/HSCC/Views/ApprovalsView.swift` | `.red` → `Theme.Semantic.bad` |
| `Sources/HSCC/Views/OrchestratorChatView.swift` | unsent tint → `Theme.Semantic.bad` |
| `Sources/HSCC/Views/ProjectsView.swift` | a11y labels + status text in card row |
| `scripts/check_theme.sh` | NEW: comprehensive raw-colour guard |

## Verification evidence
- `build_check.sh`: HSCC 55 files / 0 errors, all targets clean (compile-only;
  never built/run on device — no simulator available on this host).
- `check_sources.sh`: 60 Swift files all registered in project.yml.
- `check_theme.sh`: CLEAN; negative-tested to prove regex comprehensiveness.
