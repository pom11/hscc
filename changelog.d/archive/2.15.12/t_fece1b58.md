kind: Added
task: t_fece1b58

- **`ios-app/Sources/HSCCWidgets/ProjectsWidget.swift` — the projects surface
  now works on the Lock Screen.** `supportedFamilies` gains
  `.accessoryInline`, `.accessoryCircular` and `.accessoryRectangular`, so the
  same one-read entry draws: an inline line that counts the unfiltered list
  ("3 projects", or "1 project") or names the scoped project; a circular count
  of the projects in the entry (a dash, never `0`, when the read returned
  nothing); and a rectangular pane with up to three project names and their
  board as the short status. The entry gains the `shortGateLabel` the prior doc
  comment anticipated, with the setup invite checked before the paused copy, so
  an unconfigured device reads "Set up in HSCC" and a scoped pause can say
  "radio · paused" inside the Lock Screen's ~25-character budget. Stale rows
  stay warn-coloured in the accessory families and paused entries still draw a
  label rather than a count, keeping the existing false-zero rule.
- **`ios-app/scripts/projects_widget_check.sh`,
  `ios-app/scripts/projects_widget_check/main.swift` and
  `ios-app/scripts/widget_view_dispatch/widgetProjects.sh` — the gates expect
  six families.** The dispatch surface call drops its `home-screen-only` mode
  and requires five gated bodies, matching small/medium/large/circular/
  rectangular (inline routes through the entry's `inlineLine`); the structural
  counts move from 3 to 5 for the gate step and 3 to 4 for the failure/empty
  branches, and the family assertion now pins the three accessory cases. The
  executed harness adds section 9 over the real entry: inline count/name/empty/
  failure/long-name fallback, invite-first `shortGateLabel`, nil circular
  numbers for empty and failed reads, and the rectangular three-row cap.
