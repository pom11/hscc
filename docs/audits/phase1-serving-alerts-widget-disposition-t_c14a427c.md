# Phase 1 Reconciliation — serving control / alerts / widget

Task t_c14a427c | Group: serving-control/alerts/widget (fleet-mutating surfaces)

## Disposition table

| Branch | Unique commit | Content | Disposition | Evidence |
|--------|--------------|---------|-------------|----------|
| audit/servingcontrol-t_e13c8c2b | ed24616 | per-unit ServingControlView (stop/restart), confirm-gated | **SUPERSEDED** | Files on main; main's ServingControlView carries MutationButton (confirm dialog) + `confirm: true` in both `/v1/cluster/stop` and `/v1/cluster/up`, and adds a `retry:` to the error label (strictly more complete). Full feature + docs + project.yml entry present. |
| audit/notify-operator-t_0454eb56 | a7a03ca | notify-operator foreground alerts (5 Notify files + wiring) | **SUPERSEDED** | All 5 Notify source files on main (4 byte-identical); main's NotificationCoordinator is strictly more advanced — adds `UNUserNotificationCenterDelegate` deep-link routing (t_136762f3), content strictly superior. project.yml + SettingsView + HSCCApp adaptor wiring all present. |
| audit/widget-t_344fbc58 | a75f35f | widget shows fleet board work (running/queue/blocked) | **SUPERSEDED** | All 5 files (2 widget, ExtensionClient, SharedModels, decode check) byte-IDENTICAL on main. |
| audit/show-worker-diffs-t_cb93feee | 10862d7 | DiffDetailView — card files + diff | **SUPERSEDED** | All 4 product files byte-IDENTICAL on main (HSCCClient, Models, CardsView, DiffDetailView), project.yml + diff_model_check.sh present. AUDIT_REPORT_t_cb93feee.md not preserved (script artifact) — the feature itself is landed. |
| audit/show-worker-diffs-t_178cb1a0 | 759eb54 | docs-only: show-worker-diffs gap (no endpoint) | **SUPERSEDED/STALE** | `ios-app/docs/show-worker-diffs-gap.md` byte-IDENTICAL on main. The gap it documents was closed: DiffDetailView lands against `GET /v1/review/{card_id}/diff` (cb93feee branch). No code to land. |
| audit/memoryview-t_1869d327 | 1176192 | report-only: MemoryView audit — iOS clean, backend memory profile-resolution bug | **STALE** | Report preserved as `ios-app/docs/memoryview-audit.md` on main; the backend bug it found is separately filed (`docs/audits/memory-path-resolution-t_7a16aed0.md`). No code to land. |

## Summary

All 6 branches are already reconciled on main — **no LAND required**. The operator
integrated every product change via re-commits (the established pattern for this
Phase 1 pass). Every landed fleet-mutating surface is confirm-gated (see below).
No branch deleted; each left intact since none required deletion.

## Confirm-gating on landed mutating surfaces

- **ServingControlView** (the only fleet-mutating UI): every mutation (Stop one
  unit, Restart) goes through `MutationButton`, which shows a confirm dialog
  naming the exact unit before acting, and sends `confirm: true` in both
  `POST /v1/cluster/stop` and `POST /v1/cluster/up`. The Restart confirm dialog
  honestly states the `up` re-asserts every serving.json unit (fleet-wide),
  because there is no per-unit start endpoint server-side. Non-2xx responses
  surface as a "Failed" alert, never a success. No `0.0.0.0` binding anywhere
  (iOS is not a network listener). Verified by grepping current main.
- **Notifications**: foreground-only (Phase 1+2); Phase 3 background refresh
  deliberately excluded → no silent autonomous mutation.
- **Widget / DiffDetailView**: read-only surfaces — no mutation path.

## Commands used (verification by execution)

- `git rev-list --left-right --count main...<branch>` → each 1 ahead (the listed commit)
- `git show <commit> --stat` / `--name-only` → captured each branch's file set
- `git show main:<path>` vs `git show <commit>:<path>` diffed per file → byte-identical or strictly-superior on main
- grep of main's ServingControlView for `MutationButton`/`confirm`/`cluster/stop`/`cluster/up` → confirm gate present

## Branch hygiene

- No branch deleted (task rule: never delete a branch not proven superseded/stale — all proven, so SAFE to delete, but kept for audit trail; the operator may clean up).
- No merge to main needed, so no `install_payload.py` re-run required for backend (branch content already on main).
