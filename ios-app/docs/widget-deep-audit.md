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

## FINDING 4 (AUDIT 5 — App Group + Keychain from extension) — NO BUG (verified, one limitation)
- Sharing seam is correctly configured: app and widget entitlements have IDENTICAL `application-groups` (`group.com.hscc.ios`) and `keychain-access-groups` (`$(AppIdentifierPrefix)group.com.hscc.ios`). Verified by diff of HSCC.entitlements vs HSCCWidgets.entitlements → IDENTICAL.
- Reader and writer cannot drift: the widget's `APIConfig.load()` / `KeychainShared.readToken()` / `SnapshotStore.load()` (SharedModels.swift) and the app's `SettingsStore.save/load` (SettingsStore.swift:42,224-225) + `KeychainStore` (KeychainStore.swift:36,70) all reference the SAME `AppGroup` constants declared once in SharedModels.swift and compiled into every target. One source of truth.
- Failure behaviour: if the extension can't read the shared suite or keychain token (missing entitlement, group unavailable, keychain item gone / -34018), `APIConfig.load()` returns nil → widget shows the setup invite (FINDING 1 ensures this is the honest invite, never a lie or blank). Widget's empty `keychainAccessGroup=''` makes iOS fall back to the first entitlement group → matches the app (per memory).
- Documented limitation (not fixable headless): the widget cannot distinguish "genuinely unconfigured" from "configured but can't read shared config" — both render the setup invite. This is honest (never claims live data it lacks) and is the best possible without an iOS runtime.

## FINDING 5 (AUDIT 2 — family fit + Dynamic Type) — NO PROVABLE BUG (audited; design is resilient)
- Architecture is deliberately Dynamic-Type-resilient: no fixed-size frames anywhere; only fixed-size SPACERS that absorb text growth.
- Small: topology glyph uses 7px circles with NO text → scales perfectly to any size; state label ("Serving/Waking/Down") is short, in an HStack with a Spacer, wraps into space if it grows. ✓
- Medium: topology uses fixed 10pt mono node labels (deliberately not Dynamic-Type-scaled) — pair width ≈ 88px each, two pairs + spacing ≈ 192px < 338pt medium width; fits. Header metrics use fixed 9pt labels, Spacer absorbs headline growth. ✓
- Reasoning-level caveat (not a bug): the 9pt metric labels ("models", "to autodown") and 10pt node labels do not scale with accessibility Dynamic Type — at very large AX sizes they stay small. Deliberate readability tradeoff for a dense one-glance widget; secondary content only.
- No iOS runtime on this host → fit proven by layout reasoning + clean build, NOT by pixel rendering. This is the one area that deserves an on-device visual pass.

## CONCLUSION
Widget audited across all 5 required areas with executed evidence (full build + 6 harnesses all pass). Two real correctness bugs found and fixed, each committed independently:
  1. t_5c554c5b audit-3: unconfigured widget no longer shows stale "can't reach" — always invites setup (0846deb).
  2. t_5c554c5b audit-4: medium widget dims its stale topology so not-live is unambiguous (200ce80).
Remaining areas (timeline/failure OK, App-Group+Keychain OK w/ documented limitation, family-fit resilient) audited and documented. New harness scripts/widget_view_dispatch_check.sh enforces the no-config invariant.

Status: DONE

