# Apps fleet — iOS and macOS (shipped state, 2026-11-18)

Read this before touching `ios-app/` or `macos-app/`, or before cutting any app
release. Both apps shipped outside the kanban board (OpenCode cards, operator
merged), so older notes describing a full SwiftUI "operator console" app are
obsolete: `ios-app/` is now a slim widgets + Live Activities app and the console
screens are deleted.

Authoritative in-repo references:
`ios-app/docs/WIDGET_COVERAGE.md` (coverage matrix), `RELEASING.md` (release
procedure), `ios-app/README.md`, `macos-app/README.md`.

## What ios-app/ is

- Widgets + Live Activities only. No console screens, no chat/stream layer.
- **6 widgets**, each covering **all 6 families** (`systemSmall`, `systemMedium`,
  `systemLarge`, `accessoryInline`, `accessoryCircular`, `accessoryRectangular`),
  every cell `declared-and-renders` (no placeholder views): Projects,
  CardsRunning, KanbanMonitor, BoardStatus, Cluster, Monitor. Bundle:
  `ios-app/Sources/HSCCWidgets/HSCCWidgetsBundle.swift`.
  `.systemExtraLarge` has renderers but is not declared on iOS — absent by
  configuration, not a gap.
- **5 Live Activities**: `MonitorActivityAttributes`,
  `CardsRunningActivityAttributes`, `ClusterHealthActivityAttributes`,
  `BoardProgressActivityAttributes` (hosted by `HSCCLiveActivityMonitor`) and
  `WakingActivityAttributes` (separate `HSCCLiveActivity` extension). Each covers
  Lock Screen + Dynamic Island compact/expanded/minimal. The extension never
  refreshes them; app-side drivers poll and push.
- Appearance / `WidgetStyle` customisation, global and per widget
  (`WidgetSettingsStore`, `WidgetAppearanceView`).
- Theme: black + `#FF5722` accent, dark-only, orange "H" mark (`Theme.swift`,
  `HSCCMark.swift`, `assets-master/appicon-*.svg`).
- Multi-address clusters with automatic failover, and "Test connection" probing
  every address (`ConnectionAddressProbe`, `SharedStore`, `SettingsStore`).
- In-app update check reads `version.json`; a non-HTTPS `install_url` is
  rejected, so no plaintext update path exists.

## What macos-app/ is

New top-level area: a menu bar app (`LSUIElement`, no Dock icon) plus desktop
widgets. `HSCCMacWidgets` compiles the **iOS widget source files by path** — the
widget views are shared, not copied, so a widget change lands on both platforms.
macOS has no accessory families; `systemExtraLarge` is declared only by Cluster
and Monitor. Auto-update via Sparkle 2 (pinned package, `SUPublicEDKey` from
`HSCC_SPARKLE_PUBLIC_KEY`).

## Release lanes — two, deliberately disjoint

| | CLI (you cut this) | Apps (operator cuts) |
|---|---|---|
| version source | `VERSION` | `apps/BUILD_NUMBER` + both `project.yml` |
| entry point | `hscc-release-cut` flow | `scripts/release_apps.sh <version>` |
| tag | `v<MAJOR.MINOR.PATCH>` | `apps-v<version>` |
| tap file | `Formula/hscc.rb` | `Casks/hscc-widgets.rb` |
| tap repo | `pom11/homebrew-hscc` | same repo, different directory |

Never assume the two lanes share a tag namespace: `git tag -l 'apps-v*'` is not
`v2.15.x`. App releases are created with `--latest=false` — **the CLI releases
own GitHub "latest"**, so never mark an `apps-v*` release as latest and never
pick "latest" as the newest app.

App release publishes: GitHub release `apps-v<version>` with the `.ipa` and
notarized `.zip`; `gh-pages` `/ios/` (OTA install page + manifests +
`version.json`); `gh-pages` `/macos/` (Sparkle `appcast.xml` + notes); tap cask.
iOS is Ad Hoc OTA from `https://pom11.github.io/hscc/ios/` with bundle prefix
`ro.parsec.hscc` — `group.com.hscc.ios` belongs to **another team**, so never
"fix" the prefix. Registered devices only; adding a device means re-running the
iOS leg with `--ios-only`. macOS is Developer ID + notarized via keychain profile
`hscc-notary`, cask `pom11/hscc/hscc-widgets`.

`gh-pages` `/ios/` and `/macos/` are written by the release scripts — nothing
outside them may delete or rewrite them.

## Gates (compile only, never a device)

```sh
ios-app/scripts/build_check.sh       # 0 errors, 0 warnings
macos-app/scripts/build_check.sh     # 0 errors, 0 warnings (swiftc-only)
ios-app/scripts/model_decode_check.sh
ios-app/scripts/widget_view_dispatch_check.sh
ios-app/scripts/widget_settings_check.sh
ios-app/scripts/probes_check.sh      # executes every *_probe.sh
ios-app/scripts/update_check.sh ; macos-app/scripts/update_check.sh
python3 scripts/address_guard.py --tracked
```

`build_check.sh` uses `swiftc` against the real per-target file sets; `-typecheck`
alone is **not** the gate (a definite-init error once reached Xcode under it).
Never build or run on a device — the compile gate is the proof. Public repo: no
LAN IPs, hostnames, tenant ids or secrets.
