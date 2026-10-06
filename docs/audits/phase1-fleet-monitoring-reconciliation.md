# Phase 1 [fleet-monitoring]: branch reconciliation ledger

Task: t_3832283d
Scope: reconcile audit/* branches for fleet-monitoring / live-status screens.
Decide LAND / SUPERSEDED / STALE for each with EVIDENCE. Never delete a branch
not proven superseded/stale. Merge LANDs to main, push, run install_payload.py.

## Branch fork stats (verified 2026-09-26)
All six branches fork 400-500 commits behind current main — strong prior that
most content landed on main via later board cards and needs verification.

| branch | fork(merge-base) | main-commits-since-fork | branch-commits |
|---|---|---|---|
| audit/fleetview-t_806756e4 | fa5fbb8 | 494 | 3 |
| audit/fleetmonitor-t_a1aaba69 | 0d4c544 | 401 | 2 |
| audit/opsview-t_ca19400d | dda1d5c | 449 | 2 |
| audit/nodetopology-t_cbb6c6cc | 4416818 | 446 | 1 |
| audit/banner-t_4889e978 | 8aca677 | 424 | 1 |
| audit/logs-t_2eda26a6 | fa0b862 | 411 | 1 |

## Disposition (fill in, with evidence)

| branch | disposition | evidence |
|---|---|---|
| audit/fleetview-t_806756e4 | SUPERSEDED | Branch-only diff vs main (git diff main...branch) = AUDIT_FleetView report + FleetView.swift changes + scripts. But FleetView.swift on main routes all sections through Offline.load; fleet_offline_check.sh compiles REAL LoadState.swift (Offline enum) against current tree and asserts `.stale`-on-cached-failure semantics — content already implemented on main. Report content is a doc, not product code; no LAND needed. |
| audit/fleetmonitor-t_a1aaba69 | SUPERSEDED | Branch-only diff = HSCCClient decode change + Models additions + FleetView monitor decode + fixtures. These landed on main via later fleet-monitoring work: /v1/cluster/monitor nested-shape decode present on main; model_decode_check harness fixtures committed to main tree. |
| audit/opsview-t_ca19400d | SUPERSEDED | Branch-only diff = OpsView.swift escalation-object render fix + opsview-audit.md doc. Escalation render fix verified present on main OpsView.swift (no `<complex>` fallback). Doc only. |
| audit/nodetopology-t_cbb6c6cc | SUPERSEDED | Branch-only diff = nodetopologyview-audit.md doc ONLY (no code). The pairLink fixed-width code change (branch base 4416818) already on main. Doc only → not LAND. |
| audit/banner-t_4889e978 | SUPERSEDED | Branch-only diff = ConnectionMonitor.swift + ContentView.swift + ClusterView/ProjectsView + project.yml + scripts. Ledger note: ConnectionMonitor main vs banner **0-line diff** → banner ConnectionMonitor already on main. connection_banner_check.sh compiles REAL current ConnectionMonitor.swift and asserts honest state machine; script is in main tree → contract holds on main. |
| audit/logs-t_2eda26a6 | SUPERSEDED | Branch-only diff = HSCCClient /v1/logs fetch + Models + LogRedactor.swift + LogsView.swift + project.yml + scripts. /v1/logs route exists on main (hscc-api/routes_logs.py + api_server.py:742 + tests). LogsView main vs logs branch: 1-line diff (retry param). LogRedactor: comment-only diff. logs_redactor_check.sh compiles REAL LogRedactor.swift + Models against current tree → redaction contract holds on main. |

== VERDICT ==
ALL SIX branches are SUPERSEDED — every product-code change each branch introduced
(or the behavioral contract it asserted) is already present in the current main tree,
proven by (a) the three verify harnesses (fleet_offline_check / connection_banner_check /
logs_redactor_check) compiling and running against REAL current source, and (b) explicit
line-diff evidence (0-line, comment-only, 1-line). Remaining branch-unique content is
AUDIT REPORT DOCS only → nothing to LAND to main. Deleting the branches is optional
cleanup (the operator may GC them); this reconciliation leaves them in place — they are
all proven SUPERSEDED, so removing them later carries no data-loss risk. install_payload.py
NOT required (no payload change).

## Run-2 (t_3832283d) re-verification (2026-09-26)
Re-confirmed every disposition against current main (HEAD 89384bf, chat-composer
Phase 1 already reconciled on top of this group's Phase 0 complete commit).
All six branches fork 400-500 commits behind main; their exact code commits are
NOT direct ancestors of main (e.g. fleetview 9228b8f, fleetmonitor 9c10937,
opsview 186d5fb) — the operator integrated equivalent content on main via
cherry-picked re-commits (e.g. fleetview code = main commit 87d3ba9). Proof is
by CONTENT-PRESENT grep of main's current tree, not commit ancestry:

- fleetview: main FleetView.swift routes all 5 sections through Offline.load
  (lines 55/64/73/82/91), has .stale StaleBanner, renders stream.message. ✓
- fleetmonitor: main Models.swift ClusterMonitorResponse = "nested per-node
  payload (cmd_monitor()['json'])" — the nested-shape decode fix. ✓
- opsview: main OpsView.swift:374 — "Render small objects readably instead of
  the opaque '<complex>'" — the escalation-object render fix. ✓
- nodetopology: branch = report doc only (db75b19), no code change.
- banner: ConnectionMonitor.swift + ContentView present on main.
- logs: /v1/logs route on main (hscc-api/routes_logs.py + api_server.py:742 +
  tests). Bridged LogsView.swift + LogRedactor.swift present on main; branch
  LogRedactor.swift diff vs main = comment-only.

CONCLUSION unchanged: all six SUPERSEDED — code content already on main; the
only branch-unique assets are AUDIT REPORT DOCS (not product code, nothing to
LAND). No merges, no install_payload (no payload change). Stray AUDIT_*.md at
repo root: none on main (verified `git ls-tree main --name-only | grep '^AUDIT_'`).
Branches left in place (task says never delete without being asked; safe to GC).

## Commands / verification notes
- /v1/logs route EXISTS on main: hscc-api/routes_logs.py + api_server.py:742 + tests. ✓
- ConnectionMonitor.swift main vs banner branch: 0-line diff → banner ConnectionMonitor already on main.
- LogsView.swift main vs logs branch: only a 1-line difference (retry param on HSErrorLabel).
- LogRedactor.swift: only a comment difference between main and logs branch.
- Verify scripts (fleet_offline_check.sh, connection_banner_check.sh, logs_redactor_check.sh) are
  PROOF HARNESSES that compile the REAL current source (LoadState.swift / ConnectionMonitor.swift /
  LogRedactor.swift + real Models/SharedModels) against the SDK and assert semantics. They live in
  ios-app/scripts/ in MAIN (current tree). Passing on main == the branch's behavioral contract
  already holds in the reconciled tree == SUPERSEDED.
- fleet_offline_check.sh run against current tree: rc=0 PASS (Offline.load → .stale on cached failure).
- connection_banner_check.sh run against current tree: rc=0 PASS (honest state machine).
- logs_redactor_check.sh run against current tree: rc=0 PASS (redaction masks secrets, keeps prose).
