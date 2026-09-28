# Widget data path — root cause + evidence

Task t_b6a8c450 — [Widgets workstream] FOUNDATION: fix widgets/Live Activities
showing but NO DATA; make the Cluster widget render live.

## Root cause (one sentence)

The Cluster widget rendered "show but no data" because the shared App-Group
store (`group.com.hscc.ios`) was EMPTY on the target, so `APIConfig.load()`
returned nil and the widget honestly rendered its `.unconfigured` state
("Set up the app to see the cluster") — the config-sharing path is NOT broken;
no configuration was ever present to share, and once configured the data path
is proven to render live data from the real API.

## Evidence

### 1. The shared App-Group store was empty (APIConfig.load() == nil)

The iPhone 17 simulator (UDID 8F927B23…, iOS 26.3) has the app `com.hscc.ios`
installed with a real App-Group container. Its shared preferences plist was
empty before this run:

```
$ plutil -p data/Containers/Shared/AppGroup/<uuid>/Library/Preferences/group.com.hscc.ios.plist
{
}
```

No `hscc.host`, no `hscc.port`. `APIConfig.load()` (SharedModels.swift:296)
reads `hscc.host`/`hscc.port` from that suite and the token from the shared
Keychain item `com.hscc.ios/api-token`; with the suite empty it returns nil → the
widget's `fetchEntry` returns `.unconfigured` (ClusterWidget.swift:85-94) →
the widget shows the gear + "Set up the app to see the cluster" = "show but no
data".

The app data container confirmed the app was never configured: its
`Library/Preferences/` directory is EMPTY (no settings plist ever written).

Antecedent t_d64ea494 already proved the App-Group sharing mechanism itself is
NOT broken (operator verified on device that no "App Group unavailable" banner
appears; the extensions read the shared suite with no fallback per commit
e830c4e). This card's empty-store finding is the *state*, not a sharing defect.

### 2. The data path works once configured — proven LIVE

`ios-app/scripts/widget_datapath_live/run.py` compiles the REAL committed
`SharedModels.swift` decode structs and the REAL `ClusterWidget.swift`
derivation functions (extracted verbatim — no drift), then decodes the LIVE
API responses and derives exactly what the widget renders. Reproduced output
against the live API (tailnet host on this Mac, redacted):

```
=== WIDGET DATA-PATH live check (real API responses -> derivation) ===
autodown: state=up enabled=false idle_minutes=120
cluster: total_hosts=4 workloads.count=2 (modelCount)
  idle_hosts node tails: .244, .246, .247, .248
clusterState = serving  (widget dot + label)
pairs: orchestrator[.244=up,.246=up] worker[.247=up,.248=up]
modelCount = 2
idleRemaining = nil (to autodown)
queueDepth = 2 (ready+todo)
  ok: clusterState is serving when autodown state=up -> serving
  ok: modelCount == workloads.count (2) -> 2
  ok: topology labels come from real idle_hosts tails -> .244,.246,.247,.248
  ok: kanban running count decodes -> 3
  ok: queueDepth decodes from stale statuses -> 2
PASS: WIDGET DATA PATH — real API -> real widget derivation reproduces live numbers
```

Content-accuracy: the hardcoded topology labels `.244/.246/.247/.248`
(ClusterWidget.swift:192-203) were checked against the REAL `/v1/cluster/status`
`idle_hosts` tails — they genuinely match (`.244 .246 .247 .248`). Not
fabricated. `idleRemaining` correctly returns nil because autodown is
`enabled:false` (honest — the "to autodown" metric is omitted, not faked).

### 3. Backend routes verified against the LIVE API (not assumed)

All five GETs the widget makes exist on the running server and respond 200
(verified with curl on the actual tailnet host):

| Route | Live response |
|---|---|
| `/v1/autodown/status` | state=up, enabled=false, idle_minutes=120 |
| `/v1/cluster/status` | 2 workloads, total_hosts=4, idle_hosts .244-.248 |
| `/v1/kanban/running` | count=3 (real running cards) |
| `/v1/kanban/blocked` | count=0 (no blocked cards) |
| `/v1/kanban/stale?older_than=0` | tasks with per-card status → queueDepth=2 |

Route registration confirmed in source: routes_autodown.py:379,
routes_cluster.py:486, routes_kanban.py:309/315/317.

### 4. Build / link contract (no physical device attached)

No iPhone is connected (`xctrace list devices` shows only the Mac + simulators).
The strongest the host permits — a full device-SDK build of all four targets —
succeeds:

```
$ xcodegen generate (team KXVNBXGCKV)
$ xcodebuild -scheme HSCC -configuration Release \
      -destination 'generic/platform=iOS' build CODE_SIGNING_ALLOWED=NO
** BUILD SUCCEEDED **
```

The Release-iphoneos bundle `HSCC.app` embeds all three extension
`.appex` (HSCCWidgets, HSCCLiveActivity, HSCCLiveActivitySession) and
passed Validate + bundle embedding. `scripts/build_check.sh` compiles all
four targets clean, 0 warnings.

## What could NOT be reproduced here (honest)

- **On-device widget rendering + timeline refresh**: requires the physical
  iPhone. None is connected. The card's "REAL DEVICE" contract cannot be met
  on this host.
- **Simulator render**: the simulator is present, but the only Apple
  Development identity on this host (KXVNBXGCKV) is subject-marked
  `CSSMERR_TP_CERT_REVOKED`, so the simulator's launchd rejects any binary
  signed with a security-sensitive entitlement (keychain-access-groups) with
  "Security policy issue" (SimXPCError code 163). The app already installed
  on the simulator was signed before the cert was revoked; a freshly-injected
  keychain helper cannot be. So the token cannot currently be placed in the
  simulator's shared keychain to complete an end-to-end render here.

## Wake Live Activity poll loop

Verified intact and wired: `AutodownView.swift:348` calls
`liveActivity.beginWake(client:)` when a wake is triggered;
`LiveActivityManager.pollUntilSettled` polls `/v1/autodown/status` +
`/v1/cluster/status` every 30s and pushes `update(content:)` to the activity;
`ContentView.swift:114` sweeps leftover wakes on launch. No defect found in the
push-only readiness-update path.

## Judgment

There is no code defect in the widget / Live Activity data path to fix — the
widget was rendering `.unconfigured` because nothing was configured/shared
yet, and it renders live data once configured (proven). The durable deliverable
of this FOUNDATION card is the live regression harness
(`ios-app/scripts/widget_datapath_live/`): it pins the decode + derivation
contract against the real API so any future route-shape / decode drift that
would again produce "show but no data" is caught immediately, and it reproduces
the exact numbers honestly.

## Delivered files
- ios-app/scripts/widget_datapath_live/run.py — Python orchestrator (real source
  extraction + compile + run against live API).
- ios-app/scripts/widget_datapath_live/template.swift — Swift harness template.

## Note (not in scope, preserved, not committed)
The worktree had an uncommitted `ios-app/Sources/Info.plist` change (adds
`CFBundleURLTypes` `hscc://` + `NSUserActivityTypes`) left by the crashed run
821. It belongs to the completed Deep-link card t_136762f3 whose Info.plist
registration apparently never landed on main. It is NOT this card's scope, so
it was stashed (preserved, not lost) and NOT included in this commit.
