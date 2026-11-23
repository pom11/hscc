kind: Added
task: t_be4e1a48

- **The macOS menu bar dropdown grows a `GRAPHS` section: per-node
  temperature and utilisation history, drawn as charts.** One more read rides
  the dropdown's existing refresh loop — a single `GET /v1/cluster/history?window=15m`
  for all nodes and the four host metrics (CPU°C, GPU°C, GPU%, CPU%), the
  canonical history route per DECISION-t_447cc7fc; the retired
  `/v1/metrics/history` is not read here. `15m` is the operator's amendment:
  the menu bar refreshes faster than a home-screen widget, so it defaults to
  the recorder's 2-second tier — and the section prints the step the ROUTE
  reported (`15m · every 2 s · 4 nodes`), because the same rows mean different
  charts at 2 s, 1 m and 5 m and a sparse series must never read as live data.
  The charts are the iOS graph tile's (`t_f4551339`): `ClusterGraphMarks`, the
  shared Swift Charts view, compiled into the menu bar app BY PATH, so the
  dropdown embeds the marks rather than a macOS copy of them and the two
  surfaces cannot drift into two different charts. What the dropdown supplies is
  its own measured width: the panel is pinned at 360 pt minus the `lg` padding,
  the two unit groups split that row, and the resulting pixel integer is BOTH
  what the series are reduced to and what the axis labels are counted from — the
  rule the marks' own file states, so an axis in the dropdown cannot claim a
  density its buckets do not have. Under each mini, the numbers the chart cannot
  say out loud: each line's current and the window's true peak (the extreme over
  every point, before any thinning), formatted by the shared caption, whose
  decimal places are the CLI's own column precisions. A metric the node never
  reported renders `—` rather than 0, a node with no samples at all gets the dash
  in the chart's place rather than a flat line, and an empty answer keeps a
  reason — the payload's own note when it sent one, otherwise the shared layer's,
  which distinguishes "the 2 s ring lives in the daemon process" from "nothing is
  stored at this window". Nothing in the section re-derives a series, a min/max,
  a unit or a tier.
- **The desktop graph tile ships all four home sizes.** `systemExtraLarge` was
  missing from the macOS arm of `supportedFamilies` while the Cluster, Monitor,
  Cards Running and Kanban tiles all ship it; WidgetKit withholds only the
  accessory families on macOS. The extra-large arm already existed in the
  dispatch and passes its own family into the panel layout, and the chart width
  is measured per tile, so the wider desktop size gets a wider reduction and more
  axis labels rather than a stretched large. Platform-conditional branch: this is
  inside the existing `#else` (macOS) arm of `supportedFamilies`; the iOS arm
  still reads `WidgetFamily.clusterGraphFamilies`, and the dropdown's own layout
  has no platform branch at all — it is macOS-only code that consumes
  platform-common views.
- **`macos-app/scripts/widget_parity_check.sh` now covers the graph tile.** It
  pinned Cluster and Monitor only, and the graph widget's own gate iterates the
  iOS family list, so a macOS arm quietly offering three of the four sizes was
  invisible to both. It also refuses a `macos-app/project.yml` where the app
  target stops compiling the graph marks and their projection: those files live
  in the widget extension, so dropping them takes the charts out of the dropdown
  while both build gates stay green, because the widget target compiles the same
  paths either way.
