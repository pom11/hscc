kind: Added
task: t_61ed3f5e

- **`ios-app/Sources/Shared/ClusterMonitorViewModel.swift` — one shared
  cluster-monitor view-model for the apps.** Every string `hscc cluster monitor`
  puts in its host table and its per-engine serving table is now produced once,
  in Swift, from the same decoded payload: nine host cells (Jobs, CPU%, RAM%,
  Swap as used/total, GPU%, CPU Temp, GPU Temp, GPU Power), the serving rows with
  their per-model `total (n)` rows, and the CLI's width rule at
  `cluster_live.WIDE_MIN`. `MonitorSample` and `ServingRow` gained the fields the
  route was already sending, so no printed column is missing from the model. The
  widget cards draw these cells instead of re-deriving them.
  `sparkrun_job_names` stays undeclared on purpose (§7); the decode check names
  the omission by re-encoding a decoded sample rather than asserting it in prose.
  Threshold colours are the view-model's own — the CLI colours serving load,
  never a temperature or a wattage — and are documented where they are declared.
---
kind: Verified
task: t_61ed3f5e

- **`ios-app/scripts/cluster_monitor_cli_parity.py` — the monitor parity golden
  comes from the CLI, not from a reading of it.** The script runs the real
  `host_table()` / `serving_table()` / `render()` out of
  `hscc_daemon/cluster_live.py` over a fixture's bytes and records the cell
  strings the CLI produced plus its plain-text render; `model_decode_check` then
  compares `ClusterMonitorViewModel` against that at three levels (column labels,
  every cell in both widths, the cells against a line of the CLI's own text). A
  transcribed golden would only ever test the transcription. It refuses a fixture
  carrying a private address, so the rule cannot be laundered into the golden.
  Two fixtures ship with it: a sanitized capture of the live route and an edge
  fixture for the shapes a capture cannot hold (no sample, no `running`, null
  `kv`, `G`-band tokens, 30-character error truncation, the width boundary).
  `model_decode_check.sh` prints its own before/after counts; this card's run is
  in its PR.
