# Releasing HSCC for iPhone (Ad Hoc + GitHub Pages)

**The supported entry point is [`../scripts/release_apps.sh`](../RELEASING.md).**
It releases iOS and macOS together, sets both versions/builds, cuts one
`apps-v<version>` release and renders the notes/feeds. This file documents the
iOS leg and remains usable standalone.

The iOS app is distributed from this public repository. A release produces a
signed **Ad Hoc** IPA, attaches it to a GitHub release, and publishes an
`itms-services` install page on GitHub Pages:

- Install page: <https://pom11.github.io/hscc/ios/>
- Manifest: <https://pom11.github.io/hscc/ios/manifest.plist>
- Release asset: `https://github.com/pom11/hscc/releases/download/ios-v<version>/HSCC-<version>.ipa`

Only iPhones registered in the team's Ad Hoc provisioning profile can install.

## One-time setup

1. **Sign in to Xcode.** Open **Xcode → Settings → Accounts**, click **+**, and
   sign in with the Apple ID that belongs to the team. This is the default
   signing path; no API key is needed. Xcode's automatic signing creates the
   **Apple Distribution** certificate, registers the App IDs and the App Group,
   and makes the Ad Hoc profiles for all four targets.
2. **Register the iPhones.** Connect each iPhone to the Mac, open **Finder**,
   select the iPhone, and click the model/capacity line until the **UDID**
   appears (Apple Configurator shows it too). Add the UDID at
   [developer.apple.com → Devices](https://developer.apple.com/account/resources/devices/list).
   Re-run the release after adding a device so the profile includes it.
3. **Have the team id ready.** It is shown next to the team in Xcode → Settings
   → Accounts. Export it:
   ```sh
   export HSCC_DEVELOPMENT_TEAM=YOURTEAMID
   ```
4. **Optional: App Store Connect API key (headless CI).** Create a key with the
   **Developer** role in App Store Connect → Users and Access → Integrations,
   download the `.p8` once, and set all three variables:
   ```sh
   export ASC_KEY_ID=... ASC_ISSUER_ID=... ASC_KEY_PATH=/path/to/AuthKey_....p8
   ```
   When all three are set the archive is passed
   `-authenticationKeyPath/-authenticationKeyID/-authenticationKeyIssuerID`.
   When none are set, Xcode's signed-in account is used. A partial set is a
   hard error.
5. **Enable push notifications (one-time).** In
   [developer.apple.com → Identifiers](https://developer.apple.com/account/resources/identifiers/list),
   open the app id (`ro.parsec.hscc.ios` for the shipped identity, plus
   `com.hscc.ios` if you install Debug builds on a phone) and tick **Push
   Notifications**. Apple automatic signing with `-allowProvisioningUpdates`
   then adds `aps-environment` to the Ad Hoc profile; if the archive still
   fails to sign, open the profile and regenerate it, or let Xcode's
   **Certificates, Identifiers & Profiles** page recreate it. The Release
   build declares `aps-environment=production`; do not set it to development
   for an Ad Hoc IPA. The sender also needs the operator-side APNs key and
   `~/.hscc/apns.json` described by `hscc doctor` — nothing in the repo.
6. **GitHub Pages.** The first publish enables Pages from the `gh-pages` branch
   through `gh api`. The account running the release needs admin on
   `pom11/hscc`.

## Release

```sh
cd ios-app
HSCC_DEVELOPMENT_TEAM=YOURTEAMID scripts/release_adhoc.sh 1.2.3
```

`CFBundleShortVersionString` is the version; `CFBundleVersion` is the monotonic
build from `../apps/BUILD_NUMBER` (both are passed to the archive). The default
release tag is `apps-v1.2.3`; set `HSCC_RELEASE_TAG=ios-v1.2.3` when running the
platform script standalone to keep the old per-platform tag. Use digits and
dots only.

What the script does:

1. `xcodegen generate` for all four targets.
2. `xcodebuild archive` for `generic/platform=iOS`, Release, automatic signing,
   with `-allowProvisioningUpdates` (and device registration).
3. `xcodebuild -exportArchive` with `method=release-testing` (Ad Hoc),
   `signingStyle=automatic`, the team, `compileBitcode=NO`, `thinning=none`.
   The exported IPA is renamed `HSCC-1.2.3.ipa` and its bundle id/version are
   verified before publishing.
4. `publish_pages.sh` uploads the IPA to the `apps-v1.2.3` release with
   `--latest=false` (so it never shadows CLI releases), writes `manifest.plist`,
   the install page, `version.json`, release notes/history, icons and a versioned
   manifest archive to `gh-pages`, and enables Pages if needed.

### Preview without signing

```sh
scripts/release_adhoc.sh --dry-run 1.2.3
```

Every step is printed; nothing is generated, signed, uploaded or committed.

### Releasing under a different reverse-DNS prefix

`HSCC_BUNDLE_PREFIX` defaults to `ro.parsec.hscc` — the identity 1.0.0 actually
shipped (`group.com.hscc.ios` belongs to another team). Override it for the whole build;
the four bundle ids and the App Group (`group.<prefix>.ios`) move together
because project.yml derives them from that one setting:

```sh
HSCC_DEVELOPMENT_TEAM=YOURTEAMID HSCC_BUNDLE_PREFIX=dev.example \
  scripts/release_adhoc.sh 1.2.3
```

The App Group must already be registered to the team (automatic signing does
this on the first archive). Do not edit the generated entitlements by hand.

## After the release

- Open <https://pom11.github.io/hscc/ios/> in **Safari on the iPhone** and tap
  **Install HSCC**.
- Installation instructions for testers: [INSTALL.md](INSTALL.md).
- If installation fails with “Unable to install”, the device is not in the
  profile: add its UDID at developer.apple.com → Devices and re-run the release.
- To update, re-run the script with the new version; the install page always
  points at the newest manifest, and every old manifest stays under
  `ios/manifests/` on `gh-pages`.

## Troubleshooting

- **“requires a development team”** — `HSCC_DEVELOPMENT_TEAM` is unset or does
  not match the account signed in to Xcode.
- **Xcode cannot create a profile** — sign in under Settings → Accounts, or set
  the three `ASC_*` variables.
- **“Unable to install”** — the iPhone's UDID is not in the Ad Hoc profile, or
  the iPhone is not on the team. Add it and re-run.
- **Button does nothing** — the page is not open in Safari, or the manifest URL
  is not HTTPS. Use the GitHub Pages URL above.
