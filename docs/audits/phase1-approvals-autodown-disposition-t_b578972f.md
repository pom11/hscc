# Phase 1 Reconciliation — approvals + autodown controls

Task t_b578972f | Group: approvals (VO labels) + autodown view (gate controls)

## Disposition table

| Branch | Unique commit | Content | Disposition | Evidence |
|--------|--------------|---------|-------------|----------|
| audit/approvals-t_48bbf99b | 5e68bea | per-row Allow button gets card-naming accessibility label for VoiceOver (`.accessibilityLabel("Allow \(card.displayTitle)")`) | **SUPERSEDED** | Already on main. `ApprovalsView.swift` is **byte-identical** between main and the branch (`git diff main audit/approvals-t_48bbf99b -- <file>` → 0-line diff). The a11y label sits at main `ApprovalsView.swift:184`. |
| audit/autodownview-t_9b678f46 | 667ca4b | gate the Controls section on having a status value (`if let value { … }`) so an already-armed cluster never flashes a misleading "Enable Autodown" set during load | **SUPERSEDED** | Already on main. Main `AutodownView.swift` carries the identical `if let value {` gate with the identical explanatory comment (`git show main:<file>` controlSection region matches the branch's fix verbatim; gate at main `AutodownView.swift:231`). The only diff remaining between main and branch in this file is in the unrelated error-handling region where main is AHEAD (main uses `(error as? HSCCError)?.localizedDescription` and `errorLabel(message)` single-arg; branch still has the older `operatorErrorMessage` / `errorLabel(message,retry:)` form). Branch is not ahead of main in any product code. |

## Summary

Both branches are already reconciled on main — **no LAND required**. The operator
integrated both iOS fixes via re-commits (the established pattern for this Phase 1
pass). Neither branch carries any backend product code, so no `install_payload.py`
re-run is required and no backend suite is affected (iOS = compile + link only).

Both branches are pure superseded (no STALE component) because each carried the
headline fix AND a full audit report; the reports carry independent value and
were preserved by relocating them to `docs/audits/` (see Relocations). The HEAD
commits of both branches were docs-only (report + disposition).

## Relocations (this reconciliation)

Per process rule "Relocate stray AUDIT_*.md at repo root to docs/audits/", the
approvals branch report NOT on main was relocated into `docs/audits/`:

- `ios-app/AUDIT_APPROVALS_t_48bbf99b.md` → `docs/audits/AUDIT_APPROVALS_t_48bbf99b.md`
  (full 7-question screen audit of ApprovalsView + the a11y fix)
- `REPORT_t_9b678f46.md` → `docs/audits/preserved_branches/REPORT_t_9b678f46.md`
  (full AutodownView screen audit + the gate-controls fix; `preserved_branches/`
  is where the repo keeps branch reports that are gitignored as bare
  `REPORT_*.md` under `docs/audits/` — `.gitignore:39`)

Both reports were scrubbed for real addresses (none present).

## Commands used (verification by execution)

- `git log --oneline main..audit/approvals-t_48bbf99b` → 3 (fix + report + scaffold)
- `git log --oneline main..audit/autodownview-t_9b678f46` → 3 (fix + report + skeleton)
- `git diff main audit/approvals-t_48bbf99b -- ios-app/Sources/HSCC/Views/ApprovalsView.swift` → **0-line diff** (byte-identical)
- `git show main:ios-app/Sources/HSCC/Views/AutodownView.swift` controlSection + grep `if let value` → gate (with explanatory comment) present on main at line 231
- `git log --oneline audit/autodownview-t_9b678f46..main` → main is strictly ahead (no branch-only product code absent from main)
- git-level scrub grep of relocated reports for real IP addresses → none

## Branch hygiene

- No branch deleted (task rule: never delete a branch not proven superseded/stale —
  both proven, so SAFE to delete, but kept for audit trail; the operator may clean up).
- No merge to main needed, so no `install_payload.py` re-run required (both
  branches' product content already on main; this reconciliation only adds docs).
