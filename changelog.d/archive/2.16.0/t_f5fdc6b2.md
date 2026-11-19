kind: Added
task: t_f5fdc6b2

- **The macOS menu bar dropdown now draws `hscc cluster monitor` and
  `hscc cluster status`, and the menu bar title can carry the fleet's thermal
  load.** Two new sections sit under the existing Cluster one: **GPU**, one row
  per host across four of `host_table`'s own columns (GPU%, GPU Temp, CPU Temp,
  GPU Power) with the CLI's column names as the header, then the serving table
  at the CLI's narrow width (Run, Wait, KV%, In, Out) with its per-model
  `total (n)` rows and the CLI's own `no backends behind the proxy` / `In/Out =
  tokens since each engine started` lines; backend rows are capped at what the
  pane fits and the count of hidden rows is stated rather than the rows being
  silently dropped, while the `total` rows are never capped away behind a long
  backend list; and **Cluster status**, the
  `cluster status` workload rows (name + tp/pp) with `total hosts` / `idle
  hosts` and each node's `used`/`free` slots. Every cell is a string out of
  `ClusterMonitorViewModel` — the view-model `cluster_monitor_cli_parity.py`
  pins against the CLI's own `host_table`/`serving_table` — read at the index
  its CLI column NAME resolves to, so the dropdown cannot re-round, re-unit or
  re-abbreviate anything, and a renamed CLI column degrades to `-` instead of
  mislabelling a value. Host and node identity is the `.NNN` display label,
  never the address (§7), and `idle_hosts` is drawn as a COUNT only: those
  strings are raw `sparkrun status` lines containing node addresses.
- `MenuBarStat` gained two dense title stats — **GPU load + watts** (the mean
  GPU% over the hosts that reported one, `48% · 195 W`) and **Hottest temp +
  watts** (`49 C · 195 W`, the CLI's own cell for the winning host and sensor).
  Both are the icon-alone shape: a metric no host reported is absent, never a
  `0` or a `-` that reads as a measurement, and the host and sensor the title
  dropped survive in its VoiceOver sentence and in the dropdown row it came
  from.
---
kind: Verified
task: t_f5fdc6b2

- **A new gate executes the dropdown's CLI parity, and the macOS targets still
  compile clean.** `macos-app/scripts/cli_dropdown_check.sh` compiles the
  shipped reading layer together with the real models and the widget's own CLI
  column reading, runs it over the committed fixtures against the goldens
  `cluster_monitor_cli_parity.py` wrote, prints its own check counts, and is
  proven by mutation: five edits to the reading layer and one tamper of a
  committed golden each turn it red by name, every one restored
  byte-identically and re-run green. `macos-app/scripts/build_check.sh` prints
  its own per-target file counts (the proof the new dropdown files are wired
  into `HSCCMac`),
  `ios-app/scripts/build_check.sh` and `ios-app/scripts/model_decode_check.sh`
  were re-run to show the shared view-model and both iOS widget targets are
  untouched, and `python3 scripts/address_guard.py --tracked` is clean. No
  shared `Sources/Shared` file and no iOS source changed: the dropdown consumes
  the view-model and the widget grid's own Foundation-only column reading
  (`ClusterSmallGrid.Column`, `ClusterSmallGrid.Row`) by path rather than
  growing a second table implementation in `macos-app/`.
