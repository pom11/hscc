# Accessibility + Dynamic Type Audit — t_4cedf275

Source-level audit of the HSCC iOS app (SwiftUI). No iOS runtime on this host —
all findings are from source inspection + WCAG relative-luminance contrast
computation. Nothing was runtime-tested on device.

## Scope
Every screen + the shared design system in `ios-app/Sources/HSCC/Views/`.
Prior specialist audits had already hardened several screens (NodeTopologyView,
ProfileEditorView, TemplateDetailView, SessionHistoryView, SearchView,
AutodownView, OpsView, FleetView, MemoryView, TemplateTopologyView,
ActivityFeedView). This pass focused on the SHARED components (Theme.swift)
and the screens with zero accessibility tooling, plus a second opinion on the
rest.

## Verification (all gates pass in this worktree)
- `scripts/build_check.sh` — full swiftc compile of HSCC / HSCCWidgets /
  HSCCLiveActivity / HSCCLiveActivitySession: **0 errors, 0 warnings**.
- `scripts/check_theme.sh` — **CLEAN** (no raw colour outside Theme.swift).
- `scripts/check_sources.sh` — 63 Swift files all in sync with project.yml.
- Note: there is NO run_tests.sh/pytest in this repo (that was a generic
  instrument suggestion). These three scripts are the project's gates.

---

## CRITICAL — Light-mode contrast was broken (Theme.swift) ✅ FIXED

The app supports BOTH light and dark appearance (no `.preferredColorScheme`
forcing anywhere). The design's light-mode variants were never actually defined:
the semantic `surfaceRaised`/`surfaceElevated`/`ok`/`warn`/`bad`/`neutral`
resolved to the SAME dark-tuned colors in light mode, while `onSurface` (`.label`)
flips to black. Result in light mode (measured WCAG):

| surface | current ratio | verdict |
|---|---|---|
| ok `#7AE2B0` on white | 1.58:1 | FAIL (need 4.5:1) |
| warn `#F2A65A` on white | 2.02:1 | FAIL |
| bad `#E56A6A` on white | 3.18:1 | FAIL (only passes large-text 3:1) |
| neutral `#A7B0C0` on white | 2.18:1 | FAIL |
| onSurface (black) on surfaceRaised slate `#232730` | **1.40:1** | **FAIL — cards unreadable** |

The last row is the worst: `HSSectionCard` and every `.fill(surfaceRaised)`
panel drew BLACK text on DARK SLATE in light mode — effectively invisible.

### Fix
Added light-appearance slots in `Theme.Palette` and resolved every semantic
role through `dynamic { ... }` so light mode remaps:

| role | dark (unchanged) | light (new) | on white |
|---|---|---|---|
| surfaceRaised | slate #232730 | `lightRaised` #F1F2F6 | black text 18.8:1 |
| surfaceElevated | slate@60% | `lightElevated` #E6E8EE | black text 17.1:1 |
| ok | #7AE2B0 | `lightSignal` #1B7E55 | 5.05:1 |
| warn | #F2A65A | `lightThermal` #B45309 | 5.02:1 |
| bad | #E56A6A | `lightHalt` #B3261E | 6.54:1 |
| neutral | #A7B0C0 | `lightMist` #55606E | 6.39:1 |

All light values pass WCAG AA 4.5:1 (dark values already did). Dark mode is
byte-for-byte unchanged. Verified: the three `.white`-text-on-fixed-hue spots
(badges/banners) sit on fixed colors, not on remapped semantics.

## FIXED — Item #1: icon-only buttons missing a11y labels
- `OrchestratorChatView.swift` composer Send button (`paperplane.fill`,
  confirm-gated) had NO label. Added `.accessibilityLabel("Send message")` +
  hint. (The StreamingChatView twin already had one.)
- `SettingsView.swift` token eye toggle (`eye`/`eye.slash`) had NO label. Added
  `.accessibilityLabel(Hide|Show token)` + hint.

## FIXED — Item #2/#4: colour-only / decorative VoiceOver noise
- `Theme.swift` `HSStatusDot` — decorative 10pt colour dot; state is always
  spelled out as text in the same row (meta line). Hidden from VoiceOver
  (`.accessibilityHidden(true)`) so it stops surfacing as an unlabeled element.
- `SearchView.swift` inline `Circle()` status dot — same; hidden.
- `QRScannerView.swift` decorative `camera.fill` in permission-denied view —
  hidden.
- `Theme.swift` `StaleBanner` retry button had a REDUNDANT double
  `.accessibilityLabel` ("Retry" on the image + "Retry loading" on the button);
  collapsed to the single button-level label.

NOT colour-alone (verified — no action needed): FleetView health uses distinct
icons + colour; `HealthCheckIndicator` always pairs a distinct SF Symbol
(checkmark/xmark/questionmark) with colour; every stat badge carries a text
label; approval/status chips carry text.

## Documented observations (not fixed — deliberate / out of scope)
- `.lineLimit(1)` on `.headline` titles in SessionsView:177 and
  ActivityFeedView:129 truncate at AX sizes. These are short identifier-style
  titles; tail-truncation is acceptable for rows, but a future pass could widen
  to `.lineLimit(2)`. Left as-is to avoid churn on healthy, previously-audited
  screens.
- Fixed heights in the app are decorative only (2–12pt dots, 24×24 icon
  frames); no text-bearing container has a fixed height, so Dynamic Type
  doesn't clip. Chat composers use `.lineLimit(1...4)` vertical grow — good.
- `ProjectsView` unreadBadge: white caption2 on amber `warn` chip =
  ~2.0:1 in dark mode. Deliberate notification-dot design, sits on the
  `// theme-allow` list, not changed here.
- `ClusterView`/`FleetView`/`OpsView`/`AutodownView`/`BoardHygieneView`/
  `ApprovalsView`/`CardsView`/`TemplateDetailView`/`TemplatesView`/
  `ProfileEditorView`/`NodeTopologyView` — audited clean in all 4 categories
  (all interactive elements carry visible text; state conveyed by icon/shape
  or text + colour; no text-clipping fixed heights; no orphaned unlabeled
  elements).

## Changed files
- `Views/Theme.swift` — light-appearance palette slots + dynamic resolution;
  HSStatusDot hidden from VO; StaleBanner label collapse.
- `Views/OrchestratorChatView.swift` — Send button a11y label.
- `Views/SettingsView.swift` — token eye toggle a11y label.
- `Views/SearchView.swift` — decorative status dot hidden.
- `Views/QRScannerView.swift` — decorative camera icon hidden.

## Note on source-level nature
Per the task: nothing here was runtime-verified (no iOS runtime on host).
Contrast numbers are from correct WCAG math (see /tmp/contrast_final.py).
The fix list is deliberately minimal and KISS — it only touches the shared
Theme and the few screens with genuine, provable gaps; it does not churn
screens that already satisfy the criteria.
