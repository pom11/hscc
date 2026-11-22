kind: Fixed
task: t_16eee2b2

- **The Lock Screen no longer calls a machine you parked a down backend.**
  Park a node with `hscc node pause`, its engine stops answering, and the queue
  sentence read `1 backend down` — an alarm about a choice the operator made on
  purpose, on the one surface that shows the sentence before the app is even
  opened. The cause was one layer down from where it shows: `CLIMonitorServingRow
  .isDown` is `row.error != nil`, and the pause overlay deliberately KEEPS a
  parked row's error (parking a machine is no reason to lose a real diagnostic),
  so any reader that asked `error` first faulted the operator twice. t_3c0b54b6
  closed this class for the node tally, the dropdown's host rows and Model cell,
  CarPlay's rows and every `N/M` fraction; the lock screen was the one surface
  left, and `isDown` stayed `error != nil` there on purpose — the CLI's
  `serving_table` consults the overlay only inside its `if r.get("error")`
  branch, and the committed parity golden pins its `down` byte. So the split
  happens in `CLILockScreenMetrics`, the one place the lock screen's sentence is
  decided: `backendsDown` now counts errored rows that are not paused, and the
  new `backendsPaused` states them separately — `1 backend paused`, `2 backends
  paused` — in every copy site (the rectangular queue line, the inline headline,
  the VoiceOver sentence). No second phrase exists for the slot: the word comes
  from `FleetFormat.pausedWord`, the same owner the node rows read, through one
  private phrase helper. The inline headline puts the clause AFTER the faulting
  one, because when a payload has both, a real fault is the sharper answer to "is
  it working" — and a parked engine is never the word `idle` either. Deliberately
  unchanged: `isDown`, the Model cell, the parity golden, and every byte an
  unpaused errored backend prints.

---
kind: Verified
order: 1
task: t_16eee2b2

- Compile-only per the operator's no-live rule (no install, no launch, no
  simulator, no device). `ios-app/scripts/build_check.sh`: HSCC 71 files,
  HSCCWidgets 33, HSCCLiveActivityMonitor 11 — **0 errors, 0 warnings**.
  `macos-app/scripts/build_check.sh`: HSCCMac 37 files, HSCCMacWidgets 33 —
  **0 errors, 0 warnings** (`HSCCMacWidgets` compiles this file by path).
  `cluster_widget_check.sh`: **207 executed checks** plus the structural half,
  PASS — 22 of them a new `lock-backend` group, whose parked payload is planted
  on 192.0.2.x hosts (the committed paused capture's three backends all ANSWER,
  and `serving_table` has no verdict to print for an engine that answers). They
  prove a parked machine's stopped engine leaves `backendsDown` and lands in
  `backendsPaused`, that the paused clause carries no `down` byte, that an empty
  queue on a parked engine headlines the pause rather than `idle`, that the fault
  outranks the choice, that the backend and node tallies agree, and — the
  CONTROL — that the same errored row on an untagged machine is byte-for-byte the
  committed sentence. Each expectation that names the verdict word composes
  THROUGH `FleetFormat.pausedWord`, so a second phrase fails by name.
  Negative-proven by mutation with the subject file restored byte-identical
  (sha256 before/after): reverting the parked subtraction reddens 13 checks,
  forcing `backendsPaused = 0` reddens 12, inventing a second word reddens 7.
  `model_decode_check.sh`: **359/359 over 74 fixtures**, PASS.
  `carplay_check.sh`: **92 checks**, PASS. `cli_dropdown_check.sh`:
  **417/417 checks over 7 fixtures** against the CLI goldens + 8 structural
  checks, PASS — the parity half is the proof this card did not move a `down`
  byte the CLI owns. `widget_parity_check.sh`: PASS. `check_sources.sh`: 92 Swift
  files in sync. `check_theme.sh` reports the
  same seven `NativeStyleKit.swift` violations as `origin/main` — byte-identical
  list, re-verified in a clean detached worktree of `origin/main` in this run —
  pre-existing, not introduced here.
