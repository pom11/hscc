# Widget Deep Audit — t_5c554c5b

Status: IN PROGRESS
Branch: wt/t_5c554c5b (from dev a9b8974)
Owner: ios-engineer

## Scope
Deep audit of the Home Screen cluster widget (widget has NEVER been run).
Five areas: timeline policy + failure, family fit at all Dynamic Type sizes,
no-config invitation, staleness indication, App Group + Keychain from extension.

## Baseline (executed proof)
- build_check.sh: all 4 targets 0 errors, 0 warnings (widget compiles clean).
- check_sources.sh: 61 Swift files all listed in project.yml.
- snapshot_store_check.sh: REAL encode/decode round-trip OK (encode `.244:up;.246:busy;…`, decode recovers 4 nodes).
- model_decode_check.sh: 48/48 fixtures decode, incl. ClusterStatusResponse + AutodownStatusResponse (the widget's two endpoints).
- Live API reachable at http://100.64.0.1:8788; both widget endpoints /v1/cluster/status and /v1/autodown/status answer with the exact decoded shape.

## Findings will be appended below with file:line + evidence.

## FINDING 1 (AUDIT 3 — no-config invitation) — BUG, FIXED [commit 0846deb]
- Symptom: unconfigured widget could show "Can't reach the cluster" + stale topology instead of the "Set up the app" invite.
- Cause: fetchEntry returned `.unreachable` with `configured:false` when config is nil but a stale snapshot exists (ClusterWidget.swift:71-80 OLD). The views checked `state == .unreachable` BEFORE `!configured` (ClusterWidgetViews.swift:49,119 OLD), so the setup invite was shadowed.
- Trigger: operator once configured, then cleared/token or host/port in Settings (snapshot persists — SnapshotStore is only overwritten by a successful fetch, never removed on unconfigure).
- Fix: fetchEntry config==nil now always returns `.unconfigured` (ClusterWidget.swift:72); both views check `!entry.configured` first (ClusterWidgetViews.swift:49,119).
- Evidence (executed): scripts/widget_view_dispatch_check.sh → every (state, configured) pair: unconfigured → setup invite; the (unreachable, false) case now yields unconfiguredView. Widget target recompiles 0 errors / 0 warnings. build_check passes.

