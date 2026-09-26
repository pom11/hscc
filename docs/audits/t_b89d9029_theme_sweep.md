# t_b89d9029 — Phase 2 [project-sessions] Theme sweep audit

Surface group: ProjectsView / SessionsView / SessionHistoryView / ActivityFeedView /
CreateCardSheet. Job: make every view use Theme.* (not invent one).

## Outcome (reconciliation finding)

The Phase 2 theme work for these five surfaces is SUBSTANTIALLY already on main
(commits predating this card: theme guard, HSStatusDot/HSStatusRow into
ProjectsView rows, a11y-label fixes, StaleBanner retry, deep-link router, etc.).
Evidence:

- `scripts/check_theme.sh` → CLEAN on the whole app (0 raw named-colour/hex
  outside Theme.swift; the only allowlisted raw `.white` carries an inline
  `// theme-allow:` marker on the amber unread chip).
- `scripts/build_check.sh` (all 4 targets) → full compile clean, 0 errors,
  0 warnings, on the UNMODIFIED current tree.

So this card is a residual-cleanup pass, not a from-scratch theming. The
remaining genuine contract violations (below) were fixed in this card.

## Sweep table (violation -> fix -> commit sha)

Commit: 9a4ee436 (see git log)

| File | Violation fixed | Theme token |
|------|-----------------|-------------|
| SessionsView.swift | `.secondary` x4 (notConfig caption, profileField caption, speak-line) | `Theme.Semantic.onSurfaceMuted` |
| SessionsView.swift | duplicated hand-rolled notConfiguredView (.secondary + .system(size:44) + .padding(.top,60)) | `HSConnectGate(...)` |
| SessionsView.swift | `RoundedRectangle(cornerRadius: 12)` x2 (profileField, listSection cards) | `Theme.Corner.card` |
| SessionsView.swift | raw `spacing: 16/12/8` (stack gaps) | `Theme.Spacing.lg/.md/.sm` |
| SessionsView.swift | subsection `.loading` bare `ProgressView()` | `HSLoading("Loading…")` |
| ActivityFeedView.swift | `.secondary` x3 (notConfig caption, speak-line) | `Theme.Semantic.onSurfaceMuted` |
| ActivityFeedView.swift | duplicated hand-rolled notConfiguredView | `HSConnectGate(...)` |
| ActivityFeedView.swift | `RoundedRectangle(cornerRadius: 12)` x2 (listSection card, traceCard) | `Theme.Corner.card` |
| ActivityFeedView.swift | raw `spacing: 16/12/8` (stack gaps) | `Theme.Spacing.lg/.md/.sm` |
| ActivityFeedView.swift | subsection `.loading` bare `ProgressView()` | `HSLoading("Loading…")` |
| CreateCardSheet.swift | `foregroundColor(.primary)` on chosen assignee | `Theme.Semantic.onSurface` |

## What deliberately STAYS as fixed-size primitives (not spacing-scale gaps)

Consistent with the already-accepted ProjectsView (which this card does not
churn): fixed SF Symbol glyph sizes (`.font(.system(size: 12/14/44))`),
fixed-cap priming dots (`Circle().frame(width: 8/10, …)`), and tight interior
chip/badge paddings (`.padding(.horizontal, 6)`, `.padding(.vertical, 2)`) for
the unread/bloat/kind badges. These are indivisible visual primitives, not gaps
on the spacing scale. The Theme.Spacing scale governs stack/section gaps, which
is what this card routed. Converting genuine 3/4/6px icon-to-icon gaps to scale
tokens would change established layouts for no contract gain.

## Verify

- `bash scripts/check_theme.sh` → CLEAN.
- `bash scripts/build_check.sh` → full compile clean, 0 errors, 0 warnings.
- Addresses scrubbed to 100.64.0.1; no secrets; repo public.
