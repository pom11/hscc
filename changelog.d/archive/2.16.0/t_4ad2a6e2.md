kind: Docs
task: t_4ad2a6e2

- **`ios-app/docs/WIDGET_COVERAGE.md` — the Cluster Graph matrix.** The graph
  widget is added to both family tables (iOS section 1, macOS section 3) and a
  new section 5c records, for each family — small, medium, large, extraLarge and
  the macOS menu bar's GRAPHS section — which metrics and which windows it
  renders, in which panel, by which file, with the tier every window lands on
  (`15m` → 2 s, `1h`/`6h`/`24h` → 60 s, `7d` → 300 s) and the default window each
  surface uses, so a seven-day chart and a two-second chart are never read as the
  same claim. Accessory families are recorded as absent because the surface is
  gated home-screen-only. Metrics are named by their wire key so a row can be
  checked against a capture.

---

kind: Verified
task: t_4ad2a6e2

- **`GET /v1/cluster/history` — seven stored columns are drawn by nothing.** The
  coverage pass found seven `hosts[]` columns the route serves at every tier that
  no surface renders and no configuration can even request, because none of them
  is in the client's metric enum: `swap_used_mb`, `swap_total_mb`,
  `swap_used_pct`, `gpu_clock_mhz`, `net_recv_bytes_s`, `net_send_bytes_s`,
  `sparkrun_jobs`. They are GAP-1 to GAP-7 in the gaps section, each with the
  axis it would need. Recorded alongside them: no peak column is reachable at any
  surface's default window (`15m` refuses them), the serving metrics and VRAM
  exist only behind an explicit metric-set pick, and the menu bar's graphs section
  is fixed to four metrics and capped at four nodes per refresh.
