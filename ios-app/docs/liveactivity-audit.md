# t_10bc37be — Live Activity deep audit (both targets, full lifecycle)

Branch: wt/t_10bc37be (from dev @ 64aef5f)
Date: 2026-09-02

Scope: HSCCLiveActivity (fleet wake) + HSCCLiveActivitySession.
Never run on device; NSSupportsLiveActivities was missing until this week.

## Baseline
- xcodegen generate: ok
- build_check.sh: PENDING (running in background)
- session_activity_check.sh: all passed (0 failures) — the pure SessionActivitySummary
  derivation is exercised headlessly.
- live_decode / others: to run.

## Findings (file:line + evidence)

### Q1 — Full lifecycle: request -> update -> end. Can an activity be orphaned?

### Q2 — Lock Screen AND Dynamic Island: content fitting / truncation.

### Q3 — Stale-state handling when updates stop arriving.

### Q4 — ActivityAttributes ContentState Codable-stability across app update.

### Q5 — Second activity for a different project.

## What I fixed

## What I deliberately did NOT fix (and why)
