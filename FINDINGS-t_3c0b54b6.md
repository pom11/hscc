# t_3c0b54b6 — macOS widgets + menu bar + CarPlay render parked nodes `paused (reason, since)`

Card: https://github.com/pom11/hscc (kanban t_3c0b54b6) — apps 1.4.4 render seam, base `main`.
PR body draft lives at the bottom of this file; findings are appended as they are proven.

## Verified code paths (read, not assumed)

| Surface | Owns its own copy? | Shared seam it reads |
|---|---|---|
| macOS desktop widgets (`HSCCMacWidgets`) | no — `macos-app/project.yml` lists `../ios-app/Sources/HSCCWidgets` + `ClusterNodeTally.swift` by PATH | `FleetProjection.text(for:)` → `FleetFormat.status` (#573) |
| Menu-bar app (`HSCCMac`) | partly — `DashboardFleetReading` re-exposes `ClusterMonitorViewModel` CLI cells | `ClusterMonitorViewModel` (CLI parity), `ClusterNodeTally.project` |
| CarPlay rows (`Sources/HSCC/CarPlay/CarPlayReading.swift`) | **yes — forks the verdict** | builds `FleetNodeLine` then THROWS AWAY the paused fields |

## Findings

1. **CarPlay node rows say `down` and `idle` for a parked machine.**
   `CarPlayReading.swift:71-80` (`CarPlayNodeReading.init(_ node: FleetNodeLine)`) copies
   `label/model/isUp/…` and drops `isPaused`, `pausedReason`, `pausedSince`. Consequences:
   `:109` `if !isUp { parts.insert("down", at: 0) }` → a parked host prints **down**;
   `:86` `tpsText … ?? "idle"` → a parked host with no serving row prints **idle**.
   Both are the exact verdicts this card forbids.

2. **CarPlay's fleet tally alarms on a parked fleet.**
   `CarPlayReading.swift:143-147` `headline` = `if total > 0 && up == 0 { return "Down" }`,
   fed by `tally.nodesUp`/`nodesTotal`, which count a parked node as not-up AND in the
   denominator. An all-parked frame therefore says **Down**. `ClusterC1` (iOS widget) already
   excludes parked nodes from BOTH sides and says `Paused` (`ClusterWidgetMetrics.swift:178-192`);
   CarPlay does not ride that rule — and cannot today, because it lives in
   `ClusterWidgetMetrics.swift`, which neither `carplay_check.sh` nor either macOS target compiles.

3. **Menu-bar dropdown draws `down` in the serving Model cell for a parked backend.**
   `ClusterMonitorViewModel.swift:413` (`CLIMonitorServingRow.init(row:)`) sets `model = "down"`
   for any row with `error != nil`. The CLI's own `serving_table` has a paused arm
   (`hscc_daemon/cluster_live.py:560-575` → `cluster_render.pause_for_node`), and the parity
   golden never exercises it because the generator passes no overlay (`pauses=None`,
   documented at `cluster_live.py:553-558`). So the dropdown is behind the CLI it claims parity with.

4. **Menu-bar host row calls a parked host `isDown`.**
   `DashboardFleetReading.swift:64` `isDown = row.error != nil` — the overlay keeps the row's
   `error`, so a parked machine is flagged down (same fault the skill names: ask `isPaused` first).

5. **Menu-bar title tally counts parked nodes against the fleet.**
   `DashboardMonitor.swift:295-297/343-345` feed `snapshot.nodesUp/nodesTotal` from
   `ClusterNodeTally.project`; `menuBarText(.clusterNodes)` prints `"\(nodesUp)/\(nodesTotal)"`.
   Parked machines land in the denominator and out of the numerator.

6. **macOS desktop widgets DO inherit** the #573 seam: both macOS targets compile
   `../ios-app/Sources/Shared/ClusterNodeTally.swift` by path and `HSCCMacWidgets` compiles the
   whole `../ios-app/Sources/HSCCWidgets` directory. No macOS re-implementation exists, so no
   per-target fix is needed there — only proof (gate legs + compile).

## Fix plan (shared source of truth first)

- **A. `Sources/Shared/ClusterNodeTally.swift`**: the fleet-level judged pair
  (`nodesAnswered`, `nodesJudged`) + the one fleet-level word `FleetFormat.fleetPausedHeadline`,
  computed inside `project` so every target inherits the C1 rule.
- **B. `ClusterC1`** reads them from the tally (one owner; `pausedHeadline` stays a name that
  resolves to the shared constant, so #573's gate assertions still bind).
- **C. CarPlay** carries the paused fields and renders via `FleetFormat.status`; headline rides A.
- **D. Menu bar**: `CLIMonitorServingRow` gains a paused arm fed by the shared paused set
  (default nil → the CLI parity golden stays byte-identical); `DashboardHostRow.isDown` excludes
  parked; the tile gains the CLI's `node pauses` lines from `FleetProjection.paused`;
  the menu-bar title tally rides A.
- **E. Fixture prose**: `v1_cluster_monitor_paused.json` carries a pause reason naming the
  DROPPED span rule → reworded, with the one gate assertion that quoted it updated.

## Gate runs (quoted in the PR, counts not vibes)

(to be filled by the runs below — never transcribed from a handoff)
