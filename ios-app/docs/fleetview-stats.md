# Task t_b07ec05f — FleetView Stats render by_day bar list + activity

Status: IN PROGRESS
Assignee: ios-engineer
Branch: feat/fleetstats-t_b07ec05f
Repo: ios-app @ /Users/desac/.hermes/kanban/boards/hscc/workspaces/t_b07ec05f/hscc

## Goal (decision option-a from t_5e1c74b0)
Render two previously-dropped fields in `ios-app/Sources/HSCC/Views/FleetView.swift`
`statsBody`: `completions.by_day` (proportional per-day bar list) and
`activity` (top_tools + tool_calls_by_profile breakdown). Render-only; no model change.

## Evidence
See sections below as they are filled in.

## Live /v1/fleet/stats payload (EXECUTED, redacted host)
- completions.total = 39
- completions.by_profile = {} (EMPTY in live)
- completions.by_day = {2026-08-27:7, 2026-08-28:8, 2026-08-29:15, 2026-08-30:1,
  2026-09-01:2, 2026-09-02:1, 2026-09-03:5}  (full ISO dates, need shortening for display)
- activity.tool_calls_by_profile = {test:134, unknown:4}
- activity.top_tools = [[test_tool,134],[terminal,4]]
- speak = "About 39 work items across the last 7 days."

## What to render (plan)
1. By day (in completions branch): caption "By day" + per-date row: short date label +
   horizontal bar (Capsule/RoundedRectangle, width ∝ value/maxValue) + count. Sort
   chronologically. No-op when nil/empty.
2. Activity (after completions branch): caption "Activity" + top_tools rows (name+count)
   + tool_calls_by_profile rows (profile→count). No-op when nil/empty.

## Acceptance criteria checklist
- [x] by_day proportional bar list, absent when nil/empty
- [x] activity renders compactly, absent when nil/empty
- [x] existing speak + work-items badge + by-profile unchanged
- [x] build clean for iOS target; live/offline no regression
- [ ] diff/report + changed_files/tests_run in a comment for review

## Changed files
- ios-app/Sources/HSCC/Views/FleetView.swift  (render addition only)
- ios-app/scripts/fleet_stats_render_check/main.swift (NEW proof harness)
- ios-app/scripts/fleet_stats_render_check.sh (NEW proof harness)

## Tests run (evidence)
- build_check.sh: HSCC 58 files, 0 err/0 warn; HSCCWidgets 6, HSCCLiveActivity 4,
  HSCCLiveActivitySession 4 → "full compile clean, 0 warnings (all 4 targets)".
- check_sources.sh: "sources in sync: 63 Swift files, all listed in project.yml".
- capture_live.sh: 33 routes all 200 (incl. /v1/fleet/stats?days=7).
- live_decode_check.sh (real capture 20260903_064708): 33/33 decoded, 33/33
  populated; FleetStatsResponse [POPULATED].
- fleet_stats_render_check.sh (NEW) against real capture — ALL RENDER CHECKS PASS:
  by_day chronological, shortDay "2026-08-27"->"08-27", max day (08-29/15) -> full
  140 width, top_tools + tool_calls_by_profile parse/map correctly.
- empty/no-data path (mock): empty by_day + activity -> guards render nothing. PASS.

## Render proof (real 20260903 capture, total=40)
BY DAY (max=15, full width 140):
  08-27 7  barWidth=65
  08-28 8  barWidth=74
  08-29 15 barWidth=140   <- spike
  08-30 1  barWidth=9
  09-01 2  barWidth=18
  09-02 1  barWidth=9
  09-03 6  barWidth=56
ACTIVITY:
  top tools: test_tool 135, terminal 4
  by profile: test 135, unknown 4

## What the operator now sees that they didn't before
The "40 work items" count + speak line previously stood alone. Now it's followed
by a daily trend (clear spike on 08-29 then dropoff to 1-2, modest 09-03 uptick)
and a compact tool-usage breakdown. by_profile remained EMPTY in live data, so
that sub-list still won't show — unchanged.

