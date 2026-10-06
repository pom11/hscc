# Phase 1 disposition — settings-profile group (card t_1bfe8908)

Status: **LANDED** (docs-only rescue commit by orchestrator, 2026-10-06).
Card disposition itself is **CHERRY-PICKED-TO-MAIN**: the operator integrated the
work by re-committing (not merging), which is why the 5 source `audit/*` branches
still report not-ancestor of main while their content is on main. This file closes
the trail gap: the card summary carried the evidence, but the group was the only
Phase 1 group without a disposition doc on main (GOAL_REPORT_2026-09-26.md claimed
all 7 docs were on main — it was 6/7 until this commit).

Card history: completed 2026-09-26 after 4 runs (runs #792 protocol-violation,
#793 pid-not-alive, #795 protocol-violation, #796 completed). Retry loop stopped
per operator; disposition recorded in the card summary and ledger tick 20:25.

## Branch dispositions

| Branch | Disposition | Evidence |
|---|---|---|
| `audit/settingsview-t_1223ea1a` | SUPERSEDED (re-committed) | background-thread `@State` data-race fix on main as `9a7da3b` (ancestor-verified 2026-10-06 `git merge-base --is-ancestor`) |
| `audit/a11y-t_4cedf275` | SUPERSEDED (re-committed) | light-mode contrast + VO labels + decorative-hiding on main as `a77d952` (ancestor-verified) |
| `audit/profileeditor-t_7e6dcf50` | SUPERSEDED (re-committed) | Save-after-any-edit fix + decode fixture on main as `dcaedd6` (ancestor-verified) |
| `audit/memorypicker-t_c5f20f12` | SUPERSEDED (re-committed) | `git cherry origin/main` → `-` (patch-id-equivalent commit already on main) |
| `audit/settings-multi-cluster-t_8f30cf67` | SUPERSEDED (re-committed, content-verified) | `git cherry` shows `+` only because the re-commit diverged; main's SettingsView carries the full feature: multi-cluster picker + `switchTo(_:)` + per-cluster Keychain token + "Switching clears all cached state" + ContentView re-key (`origin/main:ios-app/Sources/HSCC/Views/SettingsView.swift` lines 76, 275, 329; comment at 366 says "the multi-cluster model landed") |

All five branches LEFT INTACT on disk per goal-doc §6 ("when in doubt, leave it");
deletion is deferred to a proven-ancestor prune (t_ad5dd538 tooling only removes
strict ancestors of origin/main, so these will NOT be auto-removed — that is the
correct safety behavior, and their content is provably on main by the evidence above).
