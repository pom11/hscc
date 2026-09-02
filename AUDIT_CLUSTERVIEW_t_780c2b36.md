# ClusterView Screen Audit — t_780c2b36

Scope: `ios-app/Sources/HSCC/Views/ClusterView.swift` — the fleet hub / operator's main read on fleet health.

Live API (read-only, derived via `hscc api status`): tailnet host on port 8788 (**REDACTED** per address guard). Token from `~/.hscc/api-token`.

Build: `scripts/build_check.sh` → HSCC 57 files, 0 error, 0 warning (all 4 targets clean). Route sweep: every client literal route answers parseable JSON.

---
## 1. DATA IN — endpoints + live values (executed proof)

| View needs | Endpoint | Client fn | Live value (fetched read-only, redacted) |
|---|---|---|---|
| fleet up/down + workload count + node list | `GET /v1/cluster/status` | `clusterStatus()` HSCCClient.swift:371 | `total_hosts: 4`, `workloads: [2 entries {name,tp:"2",pp:"1",container_id}]`, `idle_hosts: [4 strings "node_N 192….244 Up 4h imag"]`, `speak:"4 hosts up. 2 workloads running, 4 idle."` |
| node IPs / roles / registered count | `GET /v1/cluster/hosts` | `clusterHosts()` HSCCClient.swift:381 | `hosts: [5 dicts {id,name,ip,role,ssh_user}]` — gateway(244), worker-1(246), worker-2(247), worker-3(248), nas(249); `speak:"5 hosts registered."` |

Route sweep (`python3 scripts/api_route_sweep.py`): `/v1/cluster/status` and `/v1/cluster/hosts` both → 200 + parseable JSON. `/v1/activity/feed` verified 200 + parses (dict of entries). Every hub destination route (verify, daemon, triggers, escalate, fleet/stats, throughput, streams, autoscale, template/list, template/status, autodown/status, kanban/blocked, kanban/stale, sessions, memory, profiles, health) answered 200 in the sweep.

**Decode coverage:** the model `ClusterStatusResponse` requires `workloads`, `idle_hosts`, `total_hosts`, `speak` (SharedModels.swift:33) — all present live → decodes, `.loaded`. `ClusterHostsResponse` (Models.swift:58) requires `hosts`, `speak` + optional dicts — all present → decodes.

**Every field the strip renders arrives.** The strip needs `total_hosts` and `speak`; both arrive.

---
## 2. RENDER — what the operator sees for the data (mix of executed proof + reasoning)

- **Healthy case (live):** 4 green dots (.244/.246/.247/.248 in two pairs) + caption "Two TP pairs — only each pair's head serves HTTP." + line `speak` = "4 hosts up. 2 workloads running, 4 idle." — accurate, from server text, no client-computed count to disagree. ✅
- **F1 (reasoning + code): node-level state is NOT derived per node.** `nodeState(ip:)` (ClusterView.swift:166-181) **never uses its `ip` argument** and reads only the global `total_hosts` — so all four dots ALWAYS share one colour. The design doc (lines 100-106) explicitly justifies this ("reports hosts as text blobs…we drive the strip's overall state from the two honest signals we DO have…without fabricating per-node telemetry"). So this is a **documented design decision, not a bug** — the strip intentionally cannot say WHICH node is down. Reported, deliberately NOT "fixed" (would contradict the documented design and can't be runtime-verified).
- **F2 (reasoning): `loaded clusterHosts()` is a no-op for rendering.** `loadHosts` (ClusterView.swift:83-87) fetches `/v1/cluster/hosts`, stores it in `@State hosts` (line 33), and **nothing ever reads `hosts`** in the body/render path (grep: only lines 33/71/72/84/85/86 reference `hosts`, all in the load path). Same class of "the function ran but nothing consumed it" that sunk the chat pipeline — but here it's harmless to the user: it's a wasted request + an unused cache write, no dead UI. The design doc (lines 100-106) explains hosts aren't parsed. Reported as dead work + a second independent failure path (hosts can go `.failed` with no UI consequence), NOT fixed (removing it changes cache behavior for nothing rendered; low value, some risk).
- **F3 (executed/observed): client topology hardcodes 4 serving nodes; server reports 5 hosts.** The strip's node set is hardcoded (ClusterView.swift:150,151,157,158). Live `/v1/cluster/hosts` lists 5 (incl. NAS .249). The four shown ARE the real serving TP nodes and match `idle_hosts`, so no live disagreement in the healthy case — but if the fleet grows, the strip can't reflect it. Defensible (it's deliberately the serving set), reported as latent.

---
## 3. STATES — loading / empty / error / stale (executed proof + reasoning)

`status` is a `LoadState` (LoadState.swift:23). `topologyStrip` switches on it (ClusterView.swift:110-131):
- **`total_hosts == 0` (empty success):** all dots `.down` (red) + `fleetStatusLine(state.speak)` ≈ "0 hosts up, 0 workloads running." Distinct from failure. ✅
- **`.failed` (never loaded):** all dots `.unknown` (grey) + red `Label(error)` (lines 123-127). Distinct from empty (grey vs red + error text). ✅ Requirement "'0 results' and 'failed to load' must NEVER look the same" — **satisfied**, with evidence.
- **`.stale` (offline, last-known held):** dots from the stale value + `StaleBanner(age, "Can't reach the cluster right now.")` + retry (lines 114-119). Clearly labelled offline. ✅
- **Not configured:** `client == nil` → `HSConnectGate` "Connect to your cluster" (line 62-64) — fully distinct. ✅
- **Initial/loading:** first frame `.idle` → `default` branch → strip with grey dots, no spinner (line 128-130). `.loading` with no value → `ProgressView` (line 111-113). NOTE: `loadStatus` (line 75-81) never sets `.loading` — it jumps `.idle→.loaded` via `Offline.load` — so the `ProgressView` branch is largely unreached; the initial render is the grey-dot strip, which is itself the "loading" cue. Cosmetic, low impact.

**State model is honest and consistent.** Empty ≠ failed ≠ stale ≠ not-configured all differ visually and textually. This is a strength, not a bug.

---
## 4. CONTROLS (executed proof + one reasoning feedback gap)

Affordances on ClusterView itself:
- **Hub rows → NavigationLinks** (`hubRow` ClusterView.swift:281-316; `approvalsRow` 227-257). Each pushes to a dedicated view. Destination data routes all answered 200 in the sweep (see §1). Feedback = the push navigation. ✅
- **Pull-to-refresh** (line 51): `.refreshable { await loadAll() }` — re-runs both loads with the OS pull spinner for feedback. ✅
- **StaleBanner retry** (line 116-118): `Task { await loadAll() }` — refetches both.
  - ✅ feedback present in the sense that the banner re-renders on result.
  - **F4 (reasoning): no in-flight feedback during retry.** `loadStatus` (75-81) sets no `.loading`, so tapping retry shows no spinner/progress — if the cluster is down and the request is slow (or hangs to the 60s timeout), the button appears to do nothing for that whole window. Lower severity because pull-to-refresh (the main path) DOES show the system spinner. Reported, NOT fixed (adding `.loading` would flash the healthy strip into a spinner on every refresh — worse).

---
## 5. OBSERVATION — @StateObject/@ObservedObject (verified)

ClusterView itself holds **no ObservableObject**. Its only mutable state:
- `@State status` (line 32), `@State hosts` (line 33) — both `LoadState` value-type enums. `@State` is correct: mutation re-renders, and value-type storage avoids the "plain `let` won't re-render" failure. ✅
- `let client: HSCCClient?` (line 26) — a `struct`, immutable, correct as a plain let.
- `var approvalCount: Int?` (line 30) — plain input, fed by ContentView's `@StateObject ApprovalPoller.pendingCount` (ContentView.swift:24,42). Correct.

No `@StateObject` is keyed by a changing value here. The "stale first instance after navigation" trap lives in the *destination* subviews, not ClusterView. **Item 5 = clean for ClusterView.**

(Tab re-entry freshness — reasoning, not runtime-proven: with iOS 18+ TabView, a re-selected tab re-runs `.task`, so ClusterView refreshes on return. If a deployment keeps tabs alive, it won't and stale data persists silently until pull-to-refresh. The global connection banner (ContentView.swift:105-118) mitigates by always showing live connexion state.)

---
## 6. LAYOUT — Dynamic Type + iPhone SE (reasoning)

- **SE width (320pt):** content width after `.padding(.horizontal)` ≈ 288pt. Topology `HStack(spacing:20)` holds two pairs; each node ≈ [8pt dot + ~40pt label], each pair ≈ 100pt → ~220pt < 288pt. Fits. Hub rows: HStack icon(28) + VStack(title+subtitle) + chevron; long subtitles wrap (no `.lineLimit(1)` truncation). `Text(state.speak)` wraps. ✅
- **Dynamic Type:** hub titles `.headline`, subtitles `.caption`, status `.caption` — system fonts, scale correctly. ✅
- **F5 (reasoning): node labels use `.hsccMono(12)` (NodeTopologyView.swift:70) = `Font.system(size:12)`, fixed size, does NOT scale with Dynamic Type.** So the strip's node labels stay tiny at large accessibility text. They're short machine values so low impact; but the *role labels* (`.caption2`) DO scale, so the strip is not uniformly non-scaling. Minor. Reported, NOT fixed (changing to a scalable metric is outside this audit's clear-bug bar and risks the strip's compact look).

---
## 7. ACCESSIBILITY (two fixed, one reasoned)

- **FIXED — F6:** topology strip's combined VoiceOver label omitted node state (NodeTopologyView.swift:83-87). State was **colour-only** — exactly the task's "colour as the ONLY signal" warning, and `.accessibilityElement(children:.combine)` + the explicit summary DROPPED the state that each nodeDot labels (`nodeDot` line 73 does carry ".244, up" but `.combine` collapses it and the explicit summary overrides it). Now the summary reads "orchestrator: .244 (up) paired with .246 (up), worker: …". Fixed — build clean.
- **FIXED — F7:** StaleBanner retry button was icon-only with no label (Theme.swift:426-428). Added `.accessibilityLabel("Retry")`. Fixed — build clean.
- Hub rows: icon + title + subtitle text; NavigationLink label is the full row. ✅ Icons are decorative (`Image` with no a11y label, but the row text labels it). No colour-only signals besides dot state (now fixed).

---
## What was fixed
1. `NodeTopologyView.accessibilitySummary` now includes each node's state → VoiceOver can tell up/down (item 7).
2. `StaleBanner` retry button labelled "Retry" → no more unlabeled icon button (item 7).

Both proven: `build_check.sh` full compile 0 err/0 warn, committed `b764783`.

## What was deliberately NOT fixed, with why
- **F1 all-dots-same-colour:** documented design decision (ClusterView.swift:100-106) — the API ships node state only as text blobs and the author chose global honest signals over fabricated per-node telemetry. Contradicting that risks regression on the operator's main screen without runtime verification.
- **F2 dead `hosts` fetch:** harmless wasted request; removing it touches cache behavior for an unused path. Low value.
- **F4 no in-flight retry feedback:** the primary refresh path (pull) has the OS spinner; adding `.loading` to `loadStatus` would flash the healthy strip away on every refresh.
- **F5 fixed-size node labels:** minor, cosmetic, risks the strip's compact design.

## Ranked by likelihood the operator hits it
1. **F1 — all 4 dots always same colour** / can't see WHICH node is down. Very likely hit (any node fault). But it's a documented design limitation, not a regression.
2. **F4 — StaleBanner retry shows no in-flight feedback** if a re-fetch is slow. Likely when offline/edge.
3. **F2 — dead hosts fetch** — invisible to the operator.
4. **F5 — node labels don't scale at large Dynamic Type** — only for accessibility users at large sizes.
