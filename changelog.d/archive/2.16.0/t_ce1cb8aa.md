kind: Added
task: t_ce1cb8aa

- **`macos-app/scripts/widget_parity_check.sh` — the desktop tile's density is
  now measured against the phone tile's, by path.** `HSCCMacWidgets` compiles the
  iOS widget sources by path rather than by copy, which means the macOS desktop
  widget is exactly as dense as the two `project.yml` file lists agree — and until
  now nothing checked that they did. The gate asserts the two targets' shared
  source sets are equal (a source only the phone compiles is a rule the desktop
  cannot reach, and a desktop-only path is a copy or a stale entry), that every
  listed path exists on disk so the equality is not a set comparison over
  filenames no compiler will open, and that all four desktop families — small,
  medium, large, extra-large — are declared in the `#else` (non-iOS)
  `supportedFamilies` arm, dispatched to an arm of their own, and draw a view
  that differs from the family above them. The last check is the one that matters
  for this card: an extra-large arm that reuses the large view compiles, ships,
  and silently drops the six fields the CLI host table has no column for. It also
  pins those six (VRAM, GPU clock, Net, Up, Fan, Load) to the shared layout model
  so a value cannot be re-derived per platform, and refuses a `system*` family
  hidden behind `#if os(iOS)` — only the three accessory families may be
  iOS-only.
---
kind: Changed
task: t_ce1cb8aa

- **`Sources/Shared/MetricsHistoryTrend.swift` is compiled by the desktop widget
  target too.** It was in `HSCCWidgets` and missing from `HSCCMacWidgets`, so the
  history-trend projection — including its rule that a metric the route did not
  report stays a gap and never becomes `0.0` — was reachable from the phone and
  unreachable from the desktop. The two lists are equal again, and the gate above
  keeps them equal.
