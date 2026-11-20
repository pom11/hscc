kind: Fixed
task: t_facf327b

- **Widgets now look like the approved mock (iOS and macOS).** Every widget sits on a solid card (white in light mode, #1C1C1E in dark) instead of the translucent `.fill.tertiary` grey that read as washed out over the wallpaper. Cluster names nodes S1–S4 in route order (hostnames truncated to "gx10-…" in the medium column and did not fit a Lock Screen ring; the hostname stays in the accessibility label). An idle fleet — live backends, nothing running, no rate — reads **0 tok/s** instead of "— tok/s" and a bare dash as the large hero; no serving answer at all still reads unknown. The small Merge Queue PR number uses the mock's 22 pt size.
---
kind: Verified
task: t_facf327b

- `ios-app/scripts/cluster_widget_check.sh`: 141 executed checks, including the new idle-0 and no-serving-unknown cases and the S1–S4 / hostname split. Every offline app gate and both build checks run locally; renders of the real views compared against the mock.
