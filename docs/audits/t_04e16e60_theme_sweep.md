# t_04e16e60 — Phase 2 [detail-template] Theme sweep audit

Surface group: DiffDetailView / TemplateDetailView / TemplateTopologyView /
TemplatesView / NodeTopologyView / BoardHygieneView. Job: make every view use
Theme.* (not invent one).

## Outcome

All six surfaces were already colour-conformant at baseline
(`scripts/check_theme.sh` CLEAN on the whole app — 0 raw named-colour/hex
outside Theme.swift, before any edit). The residual work was routing raw numeric
`.padding(Int)` / stack `spacing:` to `Theme.Spacing.*`, section `.loading`
`ProgressView()`s to `HSLoading`, and `cornerRadius: 12` to
`Theme.Corner.card` — then re-verifying by full compile.

Baseline verified by execution BEFORE edits:
- `scripts/check_theme.sh` → CLEAN.
- (build_check run post-edit on the committed tree — see Verify.)

## Sweep table (violation -> fix -> theme token)

Commit: 0867034 (see git log)

### DiffDetailView.swift
| Violation (file:line → old) | Theme token |
|------------------------------|-------------|
| `.padding(.vertical, 12)` (pager "Loading more" footer, 53) | `Theme.Spacing.md` |
| `VStack(spacing: 4)` (headerBlock caption gap, 74) | `Theme.Spacing.xs` |
| `.padding(.horizontal, 12)` / `.padding(.vertical, 8)` (header block, 87-88) | `Theme.Spacing.md` / `.sm` |
| `HStack(spacing: 8)` (file header, 106) | `Theme.Spacing.sm` |
| `.padding(.horizontal, 12)` / `.padding(.vertical, 8)` (file header, 120-121) | `Theme.Spacing.md` / `.sm` |
| `.padding(.vertical, 2)` (between file cards, 129) | `Theme.Spacing.xxs` |
| `.padding(.vertical, 2)` (hunk header, 157) | `Theme.Spacing.xxs` |
| `.padding(.horizontal, 8)` / `.padding(.vertical, 2)` (hunk body, 165-166) | `Theme.Spacing.sm` / `.xxs` |
| `HStack(spacing: 4)` (footer, 209) | `Theme.Spacing.xs` |
| `.padding(12)` (footer, 218) | `Theme.Spacing.md` |

Also routed (in-scale stack `spacing:`): `LazyVStack(spacing: 0)` kept zero-gap
(45, see keeps).

### TemplateDetailView.swift
| Violation (file:line → old) | Theme token |
|------------------------------|-------------|
| `ProgressView()` previewSection `.loading` (207) | `HSLoading("Loading…")` |
| `Divider().padding(.vertical, 4)` x3 (config/routing section gaps, 239/247/273) | `Theme.Spacing.xs` |
| `.padding(.vertical, 2)` x4 (row tight gaps, 351/378/429/453) | `Theme.Spacing.xxs` |
| `.padding(.horizontal, 8)` (node chip, 393) | `Theme.Spacing.sm` |
| `.padding(.top, 4)` (nodeChips, 401) | `Theme.Spacing.xs` |
| `.padding(.leading, 8)` (change details indent, 424) | `Theme.Spacing.sm` |
| `VStack(spacing: 2)` x5 (row inners + ApplyConfirmSheet force-recreate, 338/357/405/434/588) | `Theme.Spacing.xxs` |
| `VStack(alignment:.leading, spacing: 16)` (detail body, 63) | `Theme.Spacing.lg` |
| `VStack(spacing: 16)` (reloadingSection, 500) | `Theme.Spacing.lg` |
| `VStack(spacing: 8)` (preview `.failed`, ApplyConfirmSheet inner, 209/577) | `Theme.Spacing.sm` |
| `VStack(spacing: 4)` (nodeChips, 384) | `Theme.Spacing.xs` |
| `cornerRadius: 12` x3 (preview/apply/reload cards, 221/491/553) | `Theme.Corner.card` |

### TemplateTopologyView.swift
| Violation (file:line → old) | Theme token |
|------------------------------|-------------|
| `.padding(.vertical, 12)` (31) | `Theme.Spacing.md` |
| `RoundedRectangle(cornerRadius: 12)` (32) | `Theme.Corner.card` |

### TemplatesView.swift
| Violation (file:line → old) | Theme token |
|------------------------------|-------------|
| `.padding(.top, 8)` (scroll content, 34) | `Theme.Spacing.sm` |
| `ProgressView()` appliedCard `.loading` (109) | `HSLoading("Loading…")` |
| `ProgressView()` librarySection `.loading` (204) | `HSLoading("Loading…")` |
| `.padding(.top, 4)` (chevron offset, 301) | `Theme.Spacing.xs` |
| `RoundedRectangle(cornerRadius: 12)` x2 (appliedCard 128, template row 306) | `Theme.Corner.card` |
| `.padding(.vertical, 2)` (AppliedBadge, 359) | `Theme.Spacing.xxs` |
| `VStack(spacing: 16)` (scroll content, 29) | `Theme.Spacing.lg` |
| `VStack(spacing: 12/8)`, `HStack(spacing: 12/8)` (library/section/row in-scale gaps, 199/210/206/222/256/264/274/276) | `Theme.Spacing.md`/`.sm` |

### NodeTopologyView.swift
| Violation (file:line → old) | Theme token |
|------------------------------|-------------|
| `.padding(.vertical, 12)` (39) | `Theme.Spacing.md` |
| `RoundedRectangle(cornerRadius: 12)` (42) | `Theme.Corner.card` |
| `HStack(spacing: 4)` (nodeDot, 65) | `Theme.Spacing.xs` |

### BoardHygieneView.swift
| Violation (file:line → old) | Theme token |
|------------------------------|-------------|
| `.padding(.vertical, 8)` (segmented picker, 43) | `Theme.Spacing.sm` |
| `VStack(spacing: 4)` (blockedRow, 121) | `Theme.Spacing.xs` |

## What deliberately STAYS as primitives (not contract violations)

Consistent with the established baseline (sibling cards t_b89d9029, t_dacdbc4a):

- `spacing: 0` container stacks where each child owns its padding
  (DiffDetailView 45) — a deliberate zero-gap layout control, not a scale step.
- Inline status / incremental `ProgressView`:
  * DiffDetailView 51 `ProgressView("Loading more…")` — an inline pager footer
    appended to a growing list, NOT a full-pane loading state, so `HSLoading`
    (centred, maxHeight:.infinity) is the wrong component (same ruling the chat
    sweep applied to its in-panel "running…" row and send-button spinner).
  * TemplateDetailView 501 big `ProgressView().controlSize(.large)` in
    `reloadingSection` — the full-pane post-apply transit screen; it carries its
    own custom label/status stack, not a `LoadState` branch. Kept as the panel's
    deliberate large mono spinner.
- Tight interior chip/badge paddings (indivisible visual primitives per
  t_b89d9029): statusBadge `.horizontal, 6` (DiffDetailView 144);
  nodeChips `.vertical, 3` (TemplateDetailView 394); AppliedBadge
  `.horizontal, 6` (TemplatesView 358). The `6`/`3` values are not on the
  scale so they can't map to a token without changing the chip layout; each
  chip's symmetric `2`-vertical counterpart was routed to `Theme.Spacing.xxs`.
- Sub-step diff-line density: `.padding(.vertical, 0.5)` (DiffDetailView 182) —
  below the scale's floor (xxs=2); converting would double row height of a
  2000-line diff, so it stays a sub-step line primitive.
- Deliberate offsets / not-on-scale values in the full-pane transit state:
  `.padding(.top, 40)` and `.padding(.top, 20)` (TemplateDetailView 503/525),
  `.padding(.vertical, 6)` (Apply button 483).
- Out-of-scale stack `spacing:` (10/6/20/5/3) in section-card inner gaps and
  small row primitives (TemplateDetailView 185/202/334/388/406/435/470/532,
  NodeTopologyView 25/27/51/52, TemplateTopologyView 17/18, TemplatesView
  104/113/137/242/275, DiffDetailView 45 `spacing: 0`, BoardHygieneView 194
  `spacing: 3`) — tight row geometry / zero-gap controls, not spacing-scale
  gaps. In-scale stack `spacing:` (2/8/12/16) was routed to the matching
  `Theme.Spacing` token (see sweep table rows marked `spacing:`).

## Verify (all against the committed tree)

- `bash scripts/check_theme.sh` → CLEAN.
- `bash scripts/build_check.sh` → full compile clean, 0 errors, 0 warnings
  (all 4 targets).
- Addresses scrubbed to 100.64.0.1; no secrets; no AI attribution; repo public.
