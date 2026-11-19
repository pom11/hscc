kind: Fixed
task: t_b5d0ec18

- **The large and extra-large monitor tiles no longer read a missing serving
  section as a present-and-empty one.** `ClusterMonitorTables.hasServingSection`
  answered `servingError == nil`, so a clean poll of a response that carried no
  `serving` key at all — no error, and nothing said about engines — reported
  that a serving section existed, and the two biggest sizes drew it as a table.
  Presence is now captured at projection time from the wire key
  (`response.serving != nil`) into `toldServingSection`, the same field and the
  same reasoning `ClusterMonitorViewModel` has carried since the small tile was
  fixed: `response.serving ?? []` has already flattened "absent" into "empty" by
  the time anyone asks, so an absent section is silence and never a zero
  (§6 rule 1). `model_decode_check/monitor_tables.swift` builds the payload a
  fixture cannot express and pins the case, plus a twins-agree assertion that
  the layout copy and the cell copy answer one way for the same bytes — the
  reason the two files exist is layout versus cells, not two answers.

---
kind: Fixed
task: t_b5d0ec18

- **The phone now says what the desktop says when the proxy route map is
  unreachable.** `ClusterMonitorTables.proxyError` was projected and then drawn
  by no iOS view, so a large or extra-large tile met the same failure the menu
  bar names out loud and reported nothing. The sentence, its precedence (proxy
  failure, then poll failure, then the empty table), the CLI's
  `(showing last known routes)` suffix rule and which line `render()` styles
  `warn` rather than `dim` now have one owner, `CLIMonitorNote`, in the
  Foundation-only shared view-model file that both apps already compile;
  `MonitorServingColumn` draws it and `DashboardFleetReading` prints the same
  value, so the desktop keeps no wording of its own and a per-platform
  re-wording has nowhere to live. `cluster_widget_check.sh` bans a string
  literal of those words anywhere in `ios-app/Sources` outside the owner. A
  serving section that was reported and came back empty now gets the CLI's own
  `no backends behind the proxy` instead of a header row with nothing under it;
  a section nobody sent still gets silence. `Copy.tokensNote` remains a
  desktop-only caption.

---
kind: Changed
task: t_b5d0ec18

- **Two comments described behaviour the code does not have; no code moved.**
  `CLIMonitorServingRow` claimed the `down` backend's reason "lands in `Routes`
  when wide and in `Run` when narrow" — `serving_table`'s own
  `row(node, port, model, routes, *rest)` puts the reason at `rest[0]`, the Run
  column, in both shapes, which is why the code below the comment has always
  written `run = reason` unconditionally. `FleetProjection.jobs` claimed to be
  `hscc cluster status`'s workload count while computing it from
  `/v1/cluster/monitor`; the status figure is `ClusterStatusSummary.jobs`, a
  different number, on a different route, drawn in one fallback line, and the
  two can disagree for the same refresh. Both wordings are now what the code
  does, with the file and line of the number they name — the kind of comment a
  future card can retarget a column or a route from without being misled.

---
kind: Verified
task: t_b5d0ec18

- **The new assertions were proven by mutation, on both sides.** Restoring
  `hasServingSection` to its old `servingError == nil` reddened exactly two
  checks — the absent-key case read as a section, and the twins disagreed — with
  the file restored byte-identically afterwards. Planting the CLI's
  `no backends behind the proxy` in a widget file outside the owner reddened the
  single-owner ban and nothing else. The note words are asserted against the
  terminal's literal sentences in both apps' harnesses (not against a shared
  constant, which would survive a re-wording of the owner), and the header
  caption is pinned inside the CLI's own rendered golden text. Both
  `build_check.sh` runs, `model_decode_check.sh`, `cluster_widget_check.sh` and
  `macos-app/scripts/cli_dropdown_check.sh` are green with no check dropped; the
  banner lines are in the pull request body rather than transcribed here.
