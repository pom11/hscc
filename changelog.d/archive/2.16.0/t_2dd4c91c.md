kind: Docs
task: t_2dd4c91c

- **`ios-app/docs/WIDGET_COVERAGE.md` — a field × surface matrix for
  `hscc cluster monitor` and `hscc cluster status`, with the gaps named.** The
  page previously answered only "does this family exist and render at all"; it
  now answers "which CLI field does this surface draw, and which file decides
  it". One row per column the CLI itself adds — nine from `host_table()`, eleven
  from `serving_table()` (two of them wide-only), the `total (n)` row, the seven
  lines around the tables, and the seven labels `render_cluster_status` prints —
  against one column per iOS family, each Live Activity surface of both cluster
  activities, each desktop family and the menu bar title. A cell distinguishes
  the CLI's own cell string from a value the app formats itself from the same
  reading, which is the difference the parity work was for and the thing a
  headline/detail table could not express. Section 12 is the gap list: `Routes`,
  `Prompt t/s`, the per-row `Gen t/s`, `container`, the idle-host list, the
  proxy-port line and the `In/Out = tokens since each engine started` legend are
  printed by the CLI and drawn by no surface at all. Section 9a corrects a
  premise the family has been carrying: GPU power limit, VRAM, clock, fan, net,
  uptime and load are not `cluster monitor` columns — `cluster_live.py` prints
  none of them, they are route fields, and the power limit is never shown even
  though it is the denominator of the watt cell's colour. The duplicated
  per-family table in §5a and the compile counts in §4 are gone rather than
  corrected: a second copy of a matrix and a transcribed gate tally both rot,
  and the §4 one already had.
