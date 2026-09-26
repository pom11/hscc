# Phase 2 theme sweep — cluster + control surfaces (t_320a8332)

Scope: ClusterView, FleetView, FleetControlView, OpsView, LogsView, MemoryView,
ApprovalsView, AutodownView, ServingControlView, SearchView.

Objective: route every remaining Theme-contract bypass through Theme tokens
(Theme.swift is the single source of truth). Keep the card atomic — theme
routing + token substitution only; NO change to confirm-gating or control
behaviour.

Contract (Theme.swift): colors ONLY Theme.Semantic.*; spacing ONLY
Theme.Spacing.* (xxs=2,xs=4,sm=8,md=12,lg=16,xl=24,page=60); corner ONLY
Theme.Corner.card(12)/badge(10); hsccMono for machine values; shared components
(HSLoading/HSError/HSEmpty/HSConnectGate/HSSectionCard/HSStatus/Chip) instead of
inlined copies.

## Baseline
- check_theme.sh: CLEAN (exit 0) — no raw named/hex/component colours outside
  Theme.swift. The `(raw colour: ApprovalsView/MemoryView/SessionsView)` note
  was STALE; only `.secondary/.primary` (invisible to check_theme) remained.
- build_check.sh: HSCC 77 files, 0 errors, 0 warnings (before edits).

## Sweep table (file -> violations fixed -> remaining primitives)

ClusterView        — ProgressView(116)->HSLoading; cornerRadius 12(270,329)->card;
                     spacing 16->lg,10->md,8->sm,2->xxs; padding .top 8->sm. CLEAN.
FleetView          — ProgressView(105,157,210,299,359)->HSLoading; cornerRadius
                     10->badge; spacing 16->lg,10->md,8->sm,4->xs,2->xxs;
                     padding .vertical 8->sm. Remaining: cornerRadius 4 (by-day
                     bar, half-height pill on 8pt bar), spacing 6 (autoscaleBody).
FleetControlView   — ProgressView(73)->HSLoading; spacing 16->lg,8->sm,12->md.
                     CLEAN.
OpsView            — ProgressView(110,165,211,283,329)->HSLoading; spacing
                     16->lg,10->md,8->sm,2->xxs; padding .vertical 2->xxs.
                     Remaining: spacing 6 x2 (rule-condition HStack, escalation body).
LogsView           — ProgressView(55,68)->HSLoading (removed dead logPlaceholder);
                     cornerRadius 12->card; spacing 16->lg,8->sm,2->xxs;
                     padding .horizontal 10->md, .vertical 2->xxs. Remaining:
                     LazyVStack spacing 0, severity-badge .horizontal 6,
                     row .vertical 6 (non-scale).
MemoryView         — hand-rolled notConfiguredView->HSConnectGate; all
                     .secondary/.primary->onSurfaceMuted/onSurface;
                     ProgressView(203,354)->HSLoading; cornerRadius 12->card,
                     10->badge; padding 10->md, .vertical 2->xxs, 8->sm;
                     spacing 16->lg,10->md,8->sm,2->xxs. Remaining: spacing 6,
                     .horizontal 6 x2 (badges), .system(size:36) glyph.
ApprovalsView      — hand-rolled notConfiguredView->HSConnectGate; raw
                     ContentUnavailableView(.failed)->HSError; ProgressView
                     (69,71)->HSLoading; all .secondary->onSurfaceMuted;
                     padding 8->sm; cornerRadius 8->badge. Remaining:
                     spacing 6 x2 (approvalRow).
AutodownView       — ProgressView(102)->HSLoading; cornerRadius 10->badge;
                     padding 10->md; spacing 16->lg,12->md,8->sm,2->xxs.
                     Remaining: inline ProgressView(167) waking-banner spinner
                     (status indicator beside text, not a loading pane),
                     spacing 14 (controls gap).
ServingControlView — ProgressView(106)->HSLoading; cornerRadius 12->card;
                     padding 12->md; spacing 16->lg,12->md,10->md,8->sm,4->xs.
                     CLEAN.
SearchView         — spacing 10->md, 8->sm. Remaining: inline ProgressView(104)
                     "Searching…" row spinner; spacing 6 x2 and 3 x2 (compact
                     sub-gaps).

## Accepted primitives (unchanged, consistent with merged Phase 2 baseline)
- Inline spinners: Autodown waking-banner ProgressView(167), Search
  "Searching…" ProgressView(104) — status indicators paired with text, not
  full-pane loading states (HSLoading is for loading panes).
- Non-scale compact gaps: 3, 6, 14 (not in Theme.Spacing scale).
- Decorative pill: FleetView by-day bar cornerRadius 4 (half of the 8pt bar
  height; badge(10) would distort).
- LazyVStack spacing 0 (rows separated by Dividers).
- Large glyph sizes: .system(size:36/44) alert/gate icons (matches HSConnectGate).
- Color.accentColor (app global tint, adaptive) — accepted across merged views.

## Verify
- check_theme.sh: CLEAN (exit 0).
- build_check.sh: HSCC 77 files, 0 errors, 0 warnings (all 4 targets).
- No .secondary/.primary remain in any in-scope file.
- a11y/Dynamic Type reviewed per file; no new regressions (padding/corner tokens
  are scale-equivalent; HSLoading replaces bare spinners with the shared pane).

## PITFALL recorded
patch tool: when a new_string/old_string contains a backslash, TYPE the backslash
as a SINGLE `\` in the raw tool arg. Typing `\\` (a JSON-style double) makes the
tool write TWO backslash bytes into the file, corrupting Swift `\(interp)` and
`\.keypath` (compile errors / "never used" warnings). Verified via `od -c` and
bit confirmed by build_check. This contradicts an earlier note that attributed
double-escaping only to write_file.
