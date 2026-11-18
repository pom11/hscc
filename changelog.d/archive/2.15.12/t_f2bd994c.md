kind: Added
task: t_f2bd994c

- **The HSCC Monitor widget now reaches the Home Screen small size and all
  three Lock Screen families.** `MonitorViews.swift` offered only
  `systemMedium`/`systemLarge`; it now also registers `.systemSmall`,
  `.accessoryInline`, `.accessoryCircular` and `.accessoryRectangular`, and the
  dispatcher names every family. The compact layouts draw only numbers the
  medium view already has: the small tile is the state line plus the first
  node's CPU/RAM/GPU (or the daemon-host strip when the node section is off),
  inline is that node's CPU and GPU, circular is fleet CPU as a gauge, and
  rectangular is the node label plus CPU/MEM. No new route and no second fetch.
- **The accessory refresh spends nothing on the daemon-host half.** The
  provider's fetch is now family-aware (`fetchEntry(family:)`): the two entry
  points pass `context.family`, and `WidgetFamily.showsDaemonHost` refuses the
  `/v1/daemon/host` request before it is sent for the Lock Screen families,
  which draw no host block (the same positive-list decision `ClusterWidget`
  uses for its board row). A no-host refresh also preserves the host half
  already in `MonitorSnapshotStore` instead of overwriting it with nil.
- **`ios-app/scripts/widget_view_dispatch/widgetMonitor.sh` is the monitor's
  own surface check**, and `widget_view_dispatch_check.sh` now lists it in the
  manifest. It proves the six-family registration and dispatch, the one-step
  `!configured || paused` gate in every family body, the accessory
  `shortGateLabel` invite-first copy, the compact families' shared
  `headlineNode`/`averageCPU`/`inlineLine` rules, the exactly-two read budget,
  and the family-aware host refusal's position before the request.
