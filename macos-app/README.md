# HSCC macOS app

Menu-bar-only macOS client for the same HSCC API the iOS app reads. Part 1 is
the XcodeGen project, the menu bar app and Settings. Part 2 (t_1143c19b) adds
`HSCCMacWidgets`, a WidgetKit extension that puts the same six widgets on the
desktop and in Notification Center. Both targets join the iOS app's App Group.
Part 3 installs the app with Homebrew (cask) and publishes signed, notarized
releases.

## Install

```sh
brew install --cask pom11/hscc/hscc-widgets
```

The cask installs `HSCC Widgets.app` into `/Applications`, requires macOS
Sonoma (14) or newer, and upgrades with `brew upgrade --cask hscc-widgets`.

**Add the desktop widgets:** right-click an empty part of the desktop →
**Edit Widgets** (or click the widget gallery at the bottom of Notification
Center), search for **HSCC**, then drag any of the six widgets onto the desktop.
The first launch may ask for permission to open the app; after that the tiles
read the clusters saved in the app.

**Menu bar icon:** after the first launch a monochrome **H** appears in the menu
bar. The app is menu-bar-only — there is no Dock icon and no main window. Click
the H for the dashboard, or open **Settings…** from that menu.

**Launch at login:** Settings → **Menu Bar** → **Launch at login**. This uses
`SMAppService`; macOS may ask for approval in System Settings → General →
Login Items, and the toggle mirrors the real state rather than only storing a
preference.

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
- projects from `/v1/projects`;
- operator alerts from `/v1/alerts` (newest rows + unseen badge in the
  dropdown, one local notification per new alert).

## Alerts

The main orchestrator reaches the operator through the cluster's alert feed.
The phone receives those alerts through APNs; the Mac polls the same feed every
5 seconds while it runs and posts a local macOS notification for each new alert
— no `aps-environment`, no device token, and no change to the Developer ID +
notarized signing. The first launch (and a feed that was cleared) establishes a
baseline and does not replay history as notifications. A burst is coalesced to
five banners plus one "earlier alerts" line, and the dropdown keeps the newest
six rows with a count of the ones not yet opened. Notifications use the local
`UNUserNotificationCenter` path; if macOS permission is denied, the dropdown
says so and the badge/list keep working.

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

## Updates

The app uses Sparkle 2 against
<https://pom11.github.io/hscc/macos/appcast.xml>. Automatic checks run daily by
default. **Check for Updates…** is in the menu bar dropdown and in Settings →
**Menu Bar** → **Updates**, which also has the automatic-check and
automatic-download toggles. When Sparkle finds a release, the dropdown shows an
**Update &lt;version&gt; available** item. Installing replaces the whole signed
app bundle — including the embedded `HSCCMacWidgets.appex` — and relaunches the
menu bar app.

## Release

**The supported entry point is [`../scripts/release_apps.sh`](../RELEASING.md).**
It fills both apps' versions/builds, cuts one `apps-v<version>` release for the
IPA and the zip, renders notes and publishes the Sparkle appcast. The commands
below are the macOS leg, usable standalone.

Releases are Developer ID-signed, notarized, stapled and published as a GitHub
release asset on `pom11/hscc`; the Homebrew cask points at that asset. One-time
setup on the release Mac (the values are never committed):

```sh
export HSCC_DEVELOPMENT_TEAM=YOURTEAMID   # same team as the iOS app
xcrun notarytool store-credentials hscc-notary --apple-id <id> --team-id <team>
```

Also resolve the pinned Sparkle package once and create the EdDSA key (the
private key stays in the login keychain; the public key is what release.sh puts
in `SUPublicEDKey`). The exact commands and tool paths are in
[`../RELEASING.md`](../RELEASING.md).

Rehearse first — `--dry-run` really runs the project generation, archive,
Developer ID export and signature/extension/App Group checks, then only prints
the notarize/staple/zip/upload/cask steps:

```sh
macos-app/scripts/release.sh --dry-run 0.1.0
```

Real release (builds, notarizes, staples, zips, uploads `apps-v<version>` with
`--latest=false`, updates the `/macos/` page and Sparkle appcast, and prints the
cask):

```sh
macos-app/scripts/release.sh 0.1.0
```

Publish the cask to the tap with `--push-cask`, or run the generator directly
(it renders from the local zip, commits through `gh api`, and needs no tap
clone):

```sh
python3 scripts/bump_cask.py --version 0.1.0 --zip macos-app/build/release/0.1.0/HSCC-Widgets-0.1.0.zip --push
```

The release tag is deliberately `apps-v<version>` (a standalone platform run can
use the legacy `macos-widgets-v<version>` with `HSCC_RELEASE_TAG`), not
`v<version>`: the app must never shadow the CLI's "latest" release. Re-running
the script for a version already released replaces the asset (`--clobber`) and
keeps the release out of "latest"; the cask push is idempotent. The appcast is
signed with Sparkle's `sign_update`; the EdDSA private key stays in the login
keychain and only the public key is in Info.plist (`SUPublicEDKey`).

## Gates

```sh
macos-app/scripts/build_check.sh         # both targets, per-target swiftc, 0/0 required
macos-app/scripts/widget_parity_check.sh # HSCCMacWidgets' shared sources and family list
                                         # equal the iOS widget target's, by path
macos-app/scripts/actool_icon_check.sh   # asset catalog + AppIcon resolve
macos-app/scripts/make_appicon.sh        # regenerate PNGs from ios-app masters
```

`HSCCMacWidgets` compiles the widget sources out of `../ios-app/Sources/…` by
path — there is no second copy to keep in sync, and equally no compiler complaint
when one project lists a file the other forgot. `widget_parity_check.sh` is what
makes that agreement measured instead of assumed: the two shared-source lists
must be equal, every listed path must exist, and all four desktop families
(small, medium, large, extra-large) must be declared, dispatched, and drawing a
view of their own — an extra-large arm that reuses the large view is the failure
that silently drops the six fields only the CLI's widest table prints. It runs in
CI as the "macOS widget parity gate" step of `app-build-checks.yml`.

The app icon is generated from `ios-app/assets-master/appicon-*.svg`; do not
hand-drop PNGs. The menu bar mark is `MenuBarIcon.imageset/menubar-h.svg`, a
template-rendered monochrome H.
