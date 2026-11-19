kind: Added
task: t_be4e1a48

- **The macOS menu bar dropdown grows a `GRAPHS` section: per-node
  temperature and utilisation history.** One more read rides the dropdown's
  existing refresh loop — a single `GET /v1/cluster/history?window=15m` for
  all nodes and the four host metrics (CPU°C, GPU°C, GPU%, CPU%), the
  canonical history route per DECISION-t_447cc7fc; the retired
  `/v1/metrics/history` is not read here. `15m` is the operator's amendment:
  the menu bar refreshes faster than a home-screen widget, so it defaults to
  the recorder's 2-second tier — and the section prints the resolution the
  ROUTE reported (`15m · resolution 2s`), because the same rows mean different
  charts at 2 s, 1 m and 5 m and a sparse series must never read as live
  data. Every number comes through the shared chart layer
  (`ClusterHistorySeries.swift`, t_b6512c2e): each node row shows the current
  value and the window's true peak per metric (the extremes computed over
  every point, so no thinning can move them), a metric the node never
  reported renders `—` rather than 0, and an empty answer keeps the payload's
  own reason — a `15m` whose `store.tier0_resident` is false says the 2 s ring
  lives in the daemon process, which is a gap with a reason, not a quiet
  fleet. The Spark-style line marks themselves arrive with the shared Swift
  Charts views of the iOS graph card (t_f4551339) and will be embedded here
  by path at the dropdown's 360 pt content width — this section deliberately
  contains no copied chart code in the meantime, and no layout branch is
  platform-conditional yet.
