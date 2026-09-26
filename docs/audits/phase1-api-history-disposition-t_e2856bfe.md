# Phase 1 [api-history] — branch reconciliation disposition — t_e2856bfe

Date: 2026-09-26
Assignee: backend-engineer
Group: API addition (daemon history) + report-only branches (4 branches)
Baseline main: dec011dc (== origin/main at reconciliation time)
VERIFIED BY EXECUTION: every evidence line below was produced by comparing main's
tree against each branch tip via `git rev-parse main:<path> <branch>:<path>` (blob
IDs), `git merge-base --is-ancestor`, `git show <commit> --stat`, and `git grep` of
main for the route and view.

## Disposition table

| Branch | Content | Decision | Evidence |
|---|---|---|---|
| audit/history-t_b5ce7935 | GET /v1/daemon/history + iOS SelfHealHistory view | **SUPERSEDED (already LANDED on main)** | `hscc-api/routes_history.py` blob `90cb78c9…` identical on main and branch; `ios-app/.../SelfHealHistoryView.swift` blob `5efbd3be…` identical. Route registered on main: `api_server.py:743` `import routes_history` + `README.md:27` lists `/v1/daemon/history`. Main's history route first landed @ aa543b7, iOS view @ 44a60e7 (per orchestrator ledger). The two branch commits `ff37d02`/`0d08af2` are NOT strict ancestors of main (re-landed via cherry-pick with different SHAs) but the merged file blobs are byte-identical — card intent fully satisfied. Branch LEFT INTACT (no explicit delete directive; content on main, no merge risk). |
| audit/health-fix-t_fd2be5be | docs-only HealthResponse decode fix report | **SUPERSEDED** — decode fix already shipped | `git merge-base --is-ancestor c0cecb1 main` → YES (c0cecb1 = HealthCheck.ok Bool? tri-state fix, verified 33/33 live per branch's own docs commit). Branch unique commit `f4e1b82` adds only `REPORT_t_fd2be5be.md` (61 lines docs, zero code). Card says "then delete branch"; report preserved to docs/audits/preserved_branches/REPORT_t_fd2be5be.md (byte-identical). Branch DELETED. |
| audit/boardhygiene-t_c13011d7 | audit report only — no bug | **SUPERSEDED/STALE (report-only)** | Branch unique commit `e501236` adds only `AUDIT_BoardHygiene_t_c13011d7.md` (301 lines docs, zero code diff); the screen it audits is healthy on main. Card says "keep notes in docs/audits/ then delete branch"; report preserved byte-identical. Branch DELETED. |
| audit/searchview-t_51bad645 | no-code-change audit — healthy screen | **STALE (report-only)** | Branch unique commits `efe94bc`, `fcf3277` add only `ios-app/docs/searchview_audit_t_51bad645.md` (docs, zero code). No bug found; screen healthy. Card says "STALE (report only)" with no delete directive; per "when in doubt LAND it or leave it", report preserved byte-identical to docs/audits/preserved_branches/ and branch LEFT INTACT. |

## Summary

All four branches in this group are SUPERSEDED or STALE:

- **history**: the route (GET /v1/daemon/history) and the iOS SelfHealHistory view are ALREADY on main with byte-identical file blobs and the route registered in api_server.py + README. Nothing to merge. Merging the branch would risk reverting main's expanded README (branch carries an older Phase-A1-only README version). SUPERSEDED.
- **health-fix**: the decode fix (c0cecb1) is already an ancestor of main; the branch's only unique commit is a report confirming this. SUPERSEDED.
- **boardhygiene / searchview**: report-only audit docs with zero code change; the screens they audit are healthy on main. STALE.

No LAND merge required. The reconciliation note plus all three preserved reports are committed to docs/audits/ so the findings persist after branch cleanup.

## Branch disposition actions (per card body)

- audit/health-fix-t_fd2be5be: report preserved → branch DELETED (card: "then delete branch").
- audit/boardhygiene-t_c13011d7: report preserved → branch DELETED (card: "keep notes in docs/audits/ then delete branch").
- audit/searchview-t_51bad645: report preserved → branch LEFT INTACT (no delete directive; conservative leave-it).
- audit/history-t_b5ce7935: content on main → branch LEFT INTACT (no delete directive; conservative leave-it).

Deleted: audit/health-fix-t_fd2be5be, audit/boardhygiene-t_c13011d7.
Left intact: audit/history-t_b5ce7935, audit/searchview-t_51bad645.

## Acknowledgements / scrub-check

- All preserved report files verified byte-identical to their branch blobs (`git hash-object` match confirmed before deletion).
- Scrub scan over the committed content: no real/tailnet IPs (only permitted placeholders), no tokens/secrets. `~/.hscc/api-token` appears only as a filesystem path mention in a preserved command log, not a value.
- no AI attribution in committed content.
