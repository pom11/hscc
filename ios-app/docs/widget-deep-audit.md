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
- Live API reachable (derived via `hscc api status`); both widget endpoints /v1/cluster/status and /v1/autodown/status answer with the exact decoded shape. (Address scrubbed from this public doc per repo rule — derive it, never hardcode it.)

## Findings will be appended below with file:line + evidence.

## FINDING 1 (AUDIT 3 — no-config invitation) — BUG, FIXED [commit 0846deb]
- Symptom: unconfigured widget could show "Can't reach the cluster" + stale topology instead of the "Set up the app" invite.
- Cause: fetchEntry returned `.unreachable` with `configured:false` when config is nil but a stale snapshot exists (ClusterWidget.swift:71-80 OLD). The views checked `state == .unreachable` BEFORE `!configured` (ClusterWidgetViews.swift:49,119 OLD), so the setup invite was shadowed.
- Trigger: operator once configured, then cleared/token or host/port in Settings (snapshot persists — SnapshotStore is only overwritten by a successful fetch, never removed on unconfigure).
- Fix: fetchEntry config==nil now always returns `.unconfigured` (ClusterWidget.swift:72); both views check `!entry.configured` first (ClusterWidgetViews.swift:49,119).
- Evidence (executed): scripts/widget_view_dispatch_check.sh → every (state, configured) pair: unconfigured → setup invite; the (unreachable, false) case now yields unconfiguredView. Widget target recompiles 0 errors / 0 warnings. build_check passes.

## FINDING 2 (AUDIT 4 — staleness signalling) — BUG, FIXED [commit 200ce80]
- Symptom: medium widget's unreachable view rendered the STALE last-known topology at full per-node color (green "up" dots possibly hours old) → looked live when it wasn't.
- Cause: MediumClusterWidget.unreachableStateView called `miniPair(pair)` (ClusterWidgetViews.swift:222 OLD) which fills dot color from `node.state.color` with no dimming, while SmallClusterWidget dims its stale topology (`dimmed: true`, line 98). Inconsistent staleness signal between families.
- Fix: added `dimmed: Bool = false` to medium `miniPair` and `dot`; unreachable view now passes `dimmed: true` (line 222) → mist, matches small widget. Live serving view (line 144) keeps `dimmed: false`.
- Evidence (executed): build_check → HSCCWidgets 0 errors / 0 warnings; grep shows miniPair callers 144 (live, not dimmed) + 222 (unreachable, dimmed:true).

## FINDING 3 (AUDIT 1 — timeline policy + failure) — NO BUG (audited, evidence)
- Policy: getTimeline (ClusterWidget.swift:52-60) fetches ONE entry then .after(now+5min) → 5-minute cadence, within WidgetKit's refresh budget. Correct for minutes-scale state changes.
- Failure rendering: fetchEntry NEVER returns nil — always a usable entry. Both GETs nil → `.unreachable` + last-known snapshot + age (ClusterWidget.swift:91-103), or `.unreachable` + sample pairs if no snapshot (104-110). Never a blank, never presents failure as liveness.
- Surface language: "Can't reach the cluster" (ClusterState.label, SharedModels.swift:247) is honest.
- Reasoning-level note (not a bug): when reachable fetch succeeds, no explicit age is shown — acceptable because the data IS from this refresh (≤5 min).


