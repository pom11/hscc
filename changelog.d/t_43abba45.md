kind: Changed
task: t_43abba45

- **Widget and Live Activity views now render the operator's `WidgetStyle`.**
  Every surface resolves its own style once at the configuration root
  (`WidgetStyle.resolved(for:)`) and applies it through the environment:
  Projects, Running Cards, Kanban Monitor, Board Status, Cluster and Monitor
  widgets, plus the fleet-wake, monitor, cluster-health, running-cards and
  board-progress Live Activities, including their Lock Screen and Dynamic
  Island compact / expanded / minimal presentations. Backgrounds come from
  `containerBackground` / `activityBackgroundTint`, text/accent/status roles
  come from the style, and font design, weight and text scale apply to every
  label. Density scales the shared spacing and corner tokens. Header and icon
  switches are wired through the shared `widgetHeader` / `widgetIcon` /
  `WidgetLabelStyle` modifiers.
- **Lock Screen accessory families stay system-tinted.** Circular, rectangular
  and inline complications apply only the font design/weight/scale and the
  header/icon switches; their colours remain the system's. The Appearance
  editor footer now says so.
- **No hard-coded colours or fonts remain in widget / Live Activity views.**
  Across the eleven view files the literal `Color(...)`, `.font(.system...)`,
  `.hsccMono(...)` and `Theme.Semantic` / `Theme.Palette` / `Theme.cardStatusColor`
  call sites drop from 457 to 0 (comments excluded).
- **`ios-app/scripts/widget_style_check.sh` — new gate.** Fails if a widget or
  Live Activity view uses a literal colour or system font, reads a Theme colour,
  omits a registry surface's style resolution, applies style colours inside a
  system-tinted accessory, or loses the style environment / header / icon
  wiring. Widget backgrounds must come from `style.backgroundColor` and Live
  Activity roots must tint from it. Also updated the projects widget harness
  stub so it compiles the new style bridge.
