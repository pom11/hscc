# HSCC macOS app

Menu-bar-only macOS client for the same HSCC API the iOS app reads. Part 1 is
the XcodeGen project, the menu bar app and Settings. Part 2 (t_1143c19b) adds
`HSCCMacWidgets`, a WidgetKit extension that puts the same six widgets on the
desktop and in Notification Center. Both targets join the iOS app's App Group.

## Build

```sh
brew install xcodegen
export HSCC_DEVELOPMENT_TEAM=YOURTEAMID   # once per shell
macos-app/scripts/generate.sh             # writes macos-app/HSCCMac.xcodeproj
open macos-app/HSCCMac.xcodeproj
```

The app is `LSUIElement` (no Dock icon). It lives in the menu bar as a
monochrome H, optionally followed by the short stat chosen in Settings:
running-card count or nodes-up/total.

## Desktop widgets

The `HSCCMacWidgets` extension compiles the iOS widget sources **by path** from
`ios-app/Sources/HSCCWidgets`: the same six widgets, providers, configuration
intents and views. There is no macOS copy to drift. All six offer
`systemSmall`/`systemMedium`/`systemLarge`; Cluster and Monitor also offer
`systemExtraLarge`. The accessory families are iOS-only and are guarded with
`#if os(iOS)` inside the shared files.

Widgets read through the same `WidgetDataClient` (GET-only, multi-address
failover) and the same App Group store as the menu bar app. They honour the
shared `WidgetStyle` registry, and `WidgetRendering.swift` swaps the operator's
palette for the system's primary/secondary palette plus a clear container
background when macOS renders a widget accented or vibrant on the wallpaper,
so it stays readable in either desktop mode.

## What it shows

All reads go through the iOS app's `Sources/Shared/WidgetDataClient` by path —
the same GET-only client, the same App Group connection settings, the same
Keychain token, and the same multi-address failover loop. No copied client and
no new backend routes.

- cluster health from `/v1/autodown/status` + `/v1/cluster/monitor`
  (state, nodes up/total, models served);
- running cards from `/v1/kanban/running` (title, worker, age);
- board totals from `/v1/mobile/summary` (todo/ready/running/blocked/done);
- projects from `/v1/projects`.

`/v1/cluster/monitor` exposes no tokens-per-second field in the shared model,
so the app does not invent one.

## Settings

The Settings window uses the existing `SettingsStore` / `KeychainStore`: saved
clusters, multiple addresses with automatic failover, and one Keychain token
per cluster. macOS-only preferences are the menu bar stat, refresh cadence, and
launch at login (`SMAppService`). The **Appearance** tab is the iOS editor
(`WidgetAppearanceView`) reused by path: one global style plus a per-widget
override, presets, colour/typography/density controls and the live preview.
Every write reloads the desktop timelines with `WidgetCenter.reloadAllTimelines`
and refreshes the open menu bar dropdown.

## Gates

```sh
macos-app/scripts/build_check.sh       # both targets, per-target swiftc, 0/0 required
macos-app/scripts/actool_icon_check.sh # asset catalog + AppIcon resolve
macos-app/scripts/make_appicon.sh      # regenerate PNGs from ios-app masters
```

The app icon is generated from `ios-app/assets-master/appicon-*.svg`; do not
hand-drop PNGs. The menu bar mark is `MenuBarIcon.imageset/menubar-h.svg`, a
template-rendered monochrome H.
