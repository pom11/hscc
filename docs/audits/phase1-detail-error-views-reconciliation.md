# Phase 1 [detail-error-views]: branch reconciliation ledger

Task: t_3c5149fd
Scope: reconcile audit/* branches for detail screens + error/empty states
(errorcopy, empty-states, activityfeed, sessions, sessionhistory,
templatedetail, deeplinks, deeplink-harness, liveactivity-rehydration).
Decide LAND / SUPERSEDED / STALE for each with EVIDENCE. Never delete a branch
not proven superseded/stale. Merge LANDs to main, push, run install_payload.py.

## Disposition

| branch | disposition | evidence |
|---|---|---|
| audit/errorcopy-t_b89d0b9a | SUPERSEDED | Shared operatorErrorMessage() helper + stop-leaking-DecodingError fix both already on main via operator re-commit. git log -S "func operatorErrorMessage" on main → commit 0961fe5 ("replace 16 vague dead-end fallbacks with shared operatorErrorMessage() helper"); git log -S DecodingError → 06c0ad7 ("stop leaking raw DecodingError symbols"). APIError.swift main vs branch: **0-line diff** (byte-identical). Call sites: ContentView/ActivityFeed/Autodown/Cluster/Memory/Ops/OrchestratorChat/Projects/TemplateDetail/Templates all resolved through operatorErrorMessage on main (later "no user action may dead-end in a no-op handler" epic evolved error labels with retry). Harness scripts fleet_offline_check + qr_classify_check: **0-line diff** vs main. Report ios-app/docs/errorcopy-audit.md already on main. |
| audit/empty-states-t_f57347f7 | SUPERSEDED | Retry button on inline error state across 12 views already on main. HSErrorLabel now takes `retry: (() -> Void)?` on main (LoadState.swift:389-391, "callers that omit retry get exactly the old bare label"); Theme.ErrorState full-pane retry action on main (Theme.swift:173-194, Button "Try again"). 10 of 13 touched views byte-identical to main (ActivityFeed/Fleet/Sessions/Ops/Autodown/Logs/Memory/ServingControl/TemplateDetail/Theme); remaining 3 (Cluster/FleetControl/Templates) differ only at unrelated lines (cluster-switch .id rebuild, applied sections). Report already at ios-app/AUDIT_empty_states_t_f57347f7.md on main (stray path — see Relocations). |
| audit/activityfeed-t_03f8192d | SUPERSEDED | Backend fix "running rows always survive the limit cap" already on main. routes_activity.py main vs branch: **0-line diff** (byte-identical); docstring documents "rows are never truncated by limit". Tests test_routes_activity.py: **0-line diff** (byte-identical, 672 passed on branch). Report AUDIT_ACTIVITYFEED_t_03f8192d.md NOT on main → relocated to docs/audits/ (see Relocations). |
| audit/sessions-t_bf2394ab | SUPERSEDED | Offline/stale handling — "refresh failure keeps last-known list" already on main. StateCache is a standalone file on main (ios-app/Sources/HSCC/StateCache.swift, listed in project.yml); SessionsView on main routes through Offline.load(...) with .stale/StaleBanner (SessionsView.swift:105-131). sessions_row_check harness on main. Branch inlined StateCache into HSCCClient.swift (older main layout) — not a real delta. Report ios-app/AUDIT_SessionsView_t_bf2394ab.md NOT on main → relocated to docs/audits/ (see Relocations). |
| audit/sessionhistory-t_3359b983 | SUPERSEDED | Pure proof/report branch (no product code). Paging no-gap/no-dup + eviction proof scripts already on main (scripts/audits/sessionhistory_paging_proof.py + sessionhistory_eviction_proof.py, **0-line diff**). Report already on main at docs/audits/AUDIT_SessionHistoryView.md. Nothing to land. |
| audit/templatedetail-t_6060f92b | SUPERSEDED | 'Pull to retry' + HealthCheck.ok Tri-state (Bool?) fixes already on main. HealthCheck.ok is `Bool?` on main (Models.swift:99, "ok is the server's documented TRI-STATE (bool\|None) — must be Optional or a single unverified check fails the decode"). HealthCheckIndicator.swift main vs branch: **0-line diff** (byte-identical). OpsView/FleetView render nil-unverified via the indicator on main. Report ios-app/docs/templatedetailview-audit.md already on main. |
| audit/deeplinks-t_136762f3 | SUPERSEDED | hscc:// deep links (open project/card/session) + router cleanup already on main. DeepLink.swift introduced on main by commit 939804e ("Add hscc:// deep links"). DeepLink.swift / HSCCApp.swift / NotificationCoordinator.swift / ProjectsView.swift: **0-line diff** (byte-identical). ContentView on main wires DeepLinkRouter (onOpenURL, onContinueUserActivity com.hscc.ios.open, router.projectsPath, requestedTab, alert). Branch-only ContentView/project.yml diffs are because main evolved past the branch (cluster-switch .id rebuild, offline-queue banner, later features) — not missing deep-link code. |
| audit/deeplink-harness-t_5320945e | SUPERSEDED | Companion harness for hscc:// router already on main. ios-app/scripts/deeplink_check.sh + deeplink_check/Stubs.swift + deeplink_check/main.swift all on main, **0-line diff** (byte-identical). Lands with deeplinks (which is superseded) → both superseded together. |
| audit/liveactivity-rehydration-t_7a0e9c4e | SUPERSEDED | Re-hydration sweep ending process-killed wake/session orphans on launch already on main. ContentView.swift:114-115 on main calls LiveActivityManager.sweepLeftoverWakes() + SessionActivityDriver.sweepLeftoverSessions(). LiveActivityManager.swift + SessionActivityDriver.swift main vs branch: **0-line diff** (byte-identical). Check scripts + report on main (docs/reports/t_7a0e9c4e_live_activity_rehydration.md). |

== VERDICT ==
ALL NINE branches are SUPERSEDED — every product-code change each branch
introduced is already present in the current main tree, proven by byte-identical
(0-line) diff on the shared files and by content-grep / git-log-S on the
distinctive symbols (operatorErrorMessage, HealthCheck Bool?, DeepLinkRouter,
sweepLeftover*, running-rows-never-truncated). Remaining branch-unique content is
AUDIT REPORT DOCS + proof scripts — no product code to LAND to main.
install_payload.py NOT required (no payload change).

Per process rule "Relocate stray AUDIT_*.md at repo root to docs/audits/", the two
branch reports NOT on main were relocated into docs/audits/ (activityfeed,
sessions) — see Relocations. The pre-existing on-main stray
ios-app/AUDIT_empty_states_t_f57347f7.md is left in place (already on main, moved
in an earlier reconciliation; flagged here for the operator rather than churned).

## Relocations (this reconciliation)
- AUDIT_ACTIVITYFEED_t_03f8192d.md  (repo root, from activityfeed branch) -> docs/audits/AUDIT_ACTIVITYFEED_t_03f8192d.md
- ios-app/AUDIT_SessionsView_t_bf2394ab.md (from sessions branch) -> docs/audits/AUDIT_SessionsView_t_bf2394ab.md

## Commands / verification notes
- operatorErrorMessage helper + DecodingError fix: found on main via git log -S.
- APIError.swift / DeepLink.swift / HSCCApp.swift / NotificationCoordinator.swift /
  ProjectsView.swift / HealthCheckIndicator.swift / LiveActivityManager.swift /
  SessionActivityDriver.swift / routes_activity.py / test_routes_activity.py /
  deeplink_check/* / live_activity_rehydration_check/* / sessions proof scripts:
  all byte-identical (0-line diff) between main and their branch.
- HSErrorLabel retry param + Theme.ErrorState on main (LoadState.swift:389-391,
  Theme.swift:173-194).
- HealthCheck.ok Bool? tri-state on main (Models.swift:99).
- DeepLinkRouter wired in ContentView on main.
- LiveActivity sweeps on main (ContentView.swift:114-115).
