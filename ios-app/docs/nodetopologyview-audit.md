# NodeTopologyView Screen Audit — t_cbb6c6cc

Scope: `ios-app/Sources/HSCC/Views/NodeTopologyView.swift` — the node topology strip (the cluster's signature element).
Parent: `ios-app/Sources/HSCC/Views/ClusterView.swift` (Cluster tab). NodeTopologyView is a PURE leaf — it renders whatever `[TopologyPair]` the parent injects; it fetches nothing and owns no state. So every one of the audit's 7 questions resolves through the parent for data/state, and at the leaf for render/layout/a11y.

Companion: `ios-app/docs/clusterview-audit.md` (t_780c2b36) already audited ClusterView including this strip. This report is the dedicated pass; findings are re-derived independently. The two a11y fixes from that audit are confirmed present in the current tree.

Live API (read-only, derived via `hscc api status`): tailnet host port 8788 (**REDACTED** per address guard). Token from `~/.hscc/api-token`.

Build baseline: `bash ios-app/scripts/build_check.sh` → 58 files, 0 err, 0 warn (all 4 targets). Compile-clean is the floor, not the proof.

---

## 1. DATA IN — endpoints + live values (executed proof)

NodeTopologyView itself does NOT fetch. Its input `pairs: [TopologyPair]` is built by `ClusterView.topologyPairs()` (ClusterView.swift:145-163). That build reads ONLY `@State status` (`/v1/cluster/status`). The `/v1/cluster/hosts` read (`@State hosts`) is fetched but never consumed in the render path.

Live responses (fetched read-only, redacted):

| Endpoint | Client fn | Live value |
|---|---|---|
| `GET /v1/cluster/status` | `clusterStatus()` (HSCCClient.swift:372) | `total_hosts: 4`, `workloads: [2 {name,tp:"2",pp:"1",container_id}]`, `idle_hosts: [4], speak:"4 hosts up. 2 workloads running, 4 idle."` |
| `GET /v1/cluster/hosts` | `clusterHosts()` (HSCCClient.swift:382) | `hosts: [5 {id,name,ip,role,ssh_user}]` — gateway .244, workers .246/.247/.248, nas .249; `speak:"5 hosts registered."` |

Both endpoints answered 200 with parseable JSON (the live fetch above is the proof; `scripts/api_route_sweep.py` is not present on this branch — past audits referenced it from the main repo — so the live calls here are the route check).

**Every field NodeTopologyView renders arrives.** The strip needs `label` + `state.rawValue` per node; both are supplied by the hardcoded `topologyPairs()` and `nodeState()`, not fetched directly. `ClusterStatusResponse` (SharedModels.swift:33) requires `workloads`, `idle_hosts`, `total_hosts`, `speak` — all present live → decodes `.loaded`. No field the leaf renders is missing from the wire data.

**Dropped-data note (not a leaf bug, carried over from clusterview-audit):** `/v1/cluster/hosts` (real IPs + roles, 5 hosts incl. NAS) is loaded but never read — the strip's labels/roles are hardcoded (ClusterView.swift:150,151,157,158). Documented design decision (ClusterView.swift:100-106): the API ships per-node state only as text blobs, so the author drives an honest strip from the global signal rather than fabricate per-node telemetry.

---

## 2. RENDER — what the operator sees (executed proof where possible)

Healthy (live `total_hosts:4`): two pairs of dots + the strip. Per node: an 8pt coloured circle + the short ip tail label (`.244`, `.246`, `.247`, `.248`), a connector between the pair, a role caption (`orchestrator` / `worker`), and the static bottom caption. `speak` line under it: "4 hosts up. 2 workloads running, 4 idle." — server-authored text, no client-computed count to disagree.

**FIXED — F1 (executed proof): the pair connector stretched with the container.**
`pairLink` (NodeTopologyView.swift:77-81) was `Rectangle().fill(...).frame(height: 2)` — **no width**. A widthless `Rectangle` is flexible in an `HStack`, so it absorbs leftover horizontal space. Measured (macOS render harness on the verbatim layout):

| Container width | pairLink width, before | after fix |
|---|---|---|
| 600 | 170pt | 12pt |
| 320 (SE) | 44pt | 12pt |
| 288 (SE content) | 36pt | 12pt |
| 240 | 27pt | 12pt |

The design diagram (NodeTopologyView.swift:12) shows a short `●──●` connector, and the widget's equivalent `miniPair` uses a fixed `.frame(width: 12, height: 2)` (ClusterWidgetViews.swift:158). The unstretched leaf broke the signature element — visually pulling each pair's dots far apart (170pt gap at wide widths) instead of showing a compact bonded pair. **Fixed**: `pairLink` now `.frame(width: 12, height: 2)`. Deterministic 12pt at every width; build clean; matches the widget grammar.

**F2 (reasoning): all four dots always share one colour.** `nodeState(ip:)` (ClusterView.swift:166-181) never uses its `ip` argument — every dot is derived from the single global `total_hosts > 0`. Documented design decision (ClusterView.swift:100-106), deliberately NOT fixed (would contradict the design and can't be runtime-verified). Note the strip can therefore never say WHICH node is down.

**F3 (reasoning/latent): labels + roles are hardcoded, not data-derived.** If the operator's cluster layout ever differs from the fixed `.244/.246/.247/.248` assumption, the strip would draw the wrong node set. The live hosts confirm the four shown ARE the serving TP set (and match `idle_hosts`), so no current disagreement. The 5th host (NAS .249) is deliberately excluded (it is not a serving node). Latent, not hit today.

**Client/server count disagreement?** None. The strip shows exactly the 4 serving nodes; `/v1/cluster/status` reports `total_hosts: 4` — consistent. The `speak` line is server text, so no client-computed count can drift.

---

## 3. STATES — loading / empty / error / stale (executed proof + reasoning)

State lives in the parent `topologyStrip` (ClusterView.swift:107-132) switching on `@State status: LoadState<ClusterStatusResponse>` (LoadState.swift:23).

- **Loading (first):** `.loading where status.value == nil` → `ProgressView()` spinner, no strip (lines 111-113). BUT `loadStatus` (ClusterView.swift:75-81) never sets `.loading` — it goes `.idle → .loaded` via `Offline.load`. So the spinner branch is effectively unreached; the initial frame is the `default` branch → strip with grey dots, no spinner (line 128-130). The grey dots are themselves the loading cue. Cosmetic, matches clusterview-audit.
- **Empty (success, 0 rows):** `.loaded(state)` with `total_hosts == 0` → `nodeState` returns `.down` → **four RED dots** + `fleetStatusLine(state.speak)` ≈ "0 hosts up, 0 workloads running." (lines 120-122, 170-175). Distinct. ✅
- **Failed (never loaded):** `.failed` → dots `.unknown` (**GREY**) + red `Label(message, exclamationmark.triangle.fill)` (lines 123-127). Distinct. ✅
- **Stale (offline, last-known held):** `.stale` → strip (dots from the stale value, e.g. green if it was healthy) + `StaleBanner(age, "Can't reach the cluster right now.")` + retry button + the stale `speak` line (lines 114-119). Clearly labelled "Offline — …". ✅
- **Not configured** (`client == nil`): `HSConnectGate` "Connect to your cluster" (lines 62-64). Fully distinct. ✅

**Requirement "'0 results' and 'failed to load' must never look the same":** SATISFIED with evidence. Empty = red dots + "0 hosts up…" text; failed = grey dots + red "Something went wrong" error text. Different colours (red vs grey) AND different text. Neither state is blank. This is a strength of the screen.

Offline nuance: `Offline.load` (LoadState.swift:120-145) falls back to `StateCache` under `EndpointPath.clusterStatus`. `get(path:queryItems:)` caches only when `queryItems` is empty — `/v1/cluster/status` is called with no query items (HSCCClient.swift:372-374) → cached → stale works. Consistent with memory note on caching.

---

## 4. CONTROLS — buttons / toggles / swipe (no controls in this view)

NodeTopologyView contains **zero interactive controls** — no button, no toggle, no swipe action, no NavigationLink. It is purely presentational. So item 4 is trivially satisfied for the leaf.

The only interactive affordances in the surrounding screen:
- **StaleBanner retry** (ClusterView.swift:116-118 → Theme.swift:426-432): `Task { await loadAll() }` refetches both endpoints. Both routes answer (live 200 proof above). Retry has a labelled icon button. Feedback: the banner re-renders on result. (Known minor gap: no in-flight spinner during retry — prior audit F4, deliberately not fixed.)
- **Pull-to-refresh** (ClusterView.swift:51): `.refreshable { await loadAll() }` — OS spinner is the feedback.

There is no control without feedback in this view.

---

## 5. OBSERVATION — @StateObject/@ObservedObject (verified clean)

- **NodeTopologyView** (NodeTopologyView.swift:20-22): holds `let pairs: [TopologyPair]` — a plain value injected by the parent. No ObservableObject, no `@State`, no `@StateObject`. It re-renders whenever the parent passes a new `pairs` value. Correct; cannot hit the "plain `let` won't re-render" bug because it holds no observable state of its own — it is a pure render of input. ✅
- **ClusterView** (ClusterView.swift:26,32,33): `client` is an immutable struct (`let`), `status`/`hosts` are `@State` value-type `LoadState` enums → mutation re-renders. No `@StateObject` keyed by a changing value here (that trap lives in destination subviews, not this one). ✅

Item 5 clean for both the leaf and its immediate parent. Matches clusterview-audit.

---

## 6. LAYOUT — Dynamic Type + iPhone SE (executed measurement + reasoning)

**SE width (320pt; ~288pt content after horizontal padding):** measured with the fix — each pair block ≈ 106pt wide (dot .244 ≈ 40pt + 5 + link 12 + 5 + dot .246 ≈ 40pt + role caption width), two pairs + 20pt spacing = 232pt < 256pt available. **Fits with room to spare; nothing truncates.** The fixed 12pt link no longer starves or bloats at narrow widths. ✅

**Dynamic Type (reasoned):** node labels use `.hsccMono(12)` (NodeTopologyView.swift:70) = fixed 12pt — does NOT scale with Dynamic Type. Role labels (`.caption2`) and the bottom caption (`.caption2`) DO scale. So at large accessibility text the machine labels stay small while human copy grows — the strip is not uniformly non-scaling. Low impact (labels are short machine values) and deliberate to keep the strip compact; NOT fixed (matches clusterview-audit F5).

**F4 (latent, reasoned):** hardcoded caption "Two TP pairs — only each pair's head serves HTTP." (NodeTopologyView.swift:34) will be wrong if the caller ever passes != 2 pairs. Today the parent always passes exactly 2; worth documenting, not changing now.

---

## 7. ACCESSIBILITY (confirmed present; one remaining reasoned gap)

- **Colour is NOT the only signal — confirmed.** `nodeDot` labels each node with its state: `.accessibilityLabel("\(node.label), \(node.state.rawValue)")` (NodeTopologyView.swift:73). The strip is a single combined element via `.accessibilityElement(children: .combine)` + `.accessibilityLabel(accessibilitySummary)` (lines 45-46, 83-89) whose summary includes every node's state → "orchestrator: .244 (up) paired with .246 (up), worker: …". This is the clusterview-audit fix (t_780c2b36), present and correct. ✅
- `pairLink` is an 8→12pt decorative connector with no accessibility label — fine, the summary conveys the pairing.
- **F5 (reasoned, minor):** node dot labels are `.hsccMono(12)` fixed size — at large Dynamic Type, VoiceOver still reads them correctly (a11y is not size-dependent), but the on-screen glyphs don't grow. Not a functional a11y failure (VO labels are dynamic), only visual scaling. NOT fixed.

---

## What was fixed
1. **F1 — `pairLink` fixed 12pt width (NodeTopologyView.swift:77-81)**. Executed proof: without a width the connector stretched 27-170pt with the container (36pt at SE), pulling each pair's dots apart and defeating the short `●──●` connector the design shows. Now `.frame(width: 12, height: 2)`, deterministic at every width, matching the widget's `miniPair` (ClusterWidgetViews.swift:158). Full build: 58 files, 0 err, 0 warn. Committed `5731ce7`.

## What was deliberately NOT fixed, with why
- **F2 — all dots same colour (can't see which node is down):** documented design decision (ClusterView.swift:100-106); the API ships per-node state as text blobs and the author chose honest global signals over fabricated per-node telemetry. Fixing contradicts the design and can't be runtime-verified here.
- **F3 — hardcoded labels/roles:** currently accurate against live hosts (the four shown are the serving set); making data-driven is a larger change with no current wrongness. Latent only.
- **F4 — hardcoded "Two TP pairs" caption:** parent always passes exactly 2 today.
- **F5 — fixed-size node labels at large Dynamic Type:** short machine values, low impact, a11y (VO) not affected.

## Ranked by how likely the operator hits it
1. **F1 — (FIXED) connector stretched** — every single view of the strip on a normal-width iPhone had a too-long gap (44pt on SE, up to 170pt on wider/rotated). Highest likelihood; now fixed.
2. **F2 — all dots same colour** — hit on any node fault; cannot tell which node is down. But documented design, not a regression.
3. **F5 — node labels don't scale at large Dynamic Type** — only accessibility users at large sizes.
4. **F3/F4 — hardcoded topology/caption** — latent; not hit with the current cluster.

## Harness/evidence ledger
- `scripts/build_check.sh` → 0 err / 0 warn, all 4 targets (before and after the fix).
- macOS render harness (§2) — real layout, measured pairLink widths 27-170pt → 12pt fixed. Harness in /tmp/nodetopo_harness/ (not committed; uses shimmed Theme, faithful layout).
