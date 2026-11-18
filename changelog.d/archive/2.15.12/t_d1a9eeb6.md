kind: Added
task: t_d1a9eeb6

- **iOS Ad Hoc releases install directly from GitHub Pages.** `ios-app/scripts/release_adhoc.sh <version>`
  archives the four targets with Xcode's automatic signing (the account signed
  in under Xcode → Settings → Accounts; an App Store Connect API key remains an
  optional `ASC_*` override), exports a `release-testing` IPA, and publishes it
  to a non-latest `ios-v<version>` release. `publish_pages.sh` generates Apple's
  OTA `manifest.plist` (a plist, not JSON), `index.html` and 57/512/1024 px
  icons into the `gh-pages` branch under `/ios/`, keeps a versioned manifest
  archive, and enables Pages through `gh api` idempotently. The install page
  carries the full step-by-step instructions (device registration → Safari →
  install → widgets/Live Activities → updates) and troubleshooting.
- **The OTA manifest generator is tested.** `ios-app/scripts/make_manifest.py`
  emits the Apple `items/assets/metadata` shape with the release asset URL and
  matching bundle id / `bundle-version`; `scripts/tests/test_ios_manifest.py`
  pins the plist shape, URLs and version fields. `ios-app/INSTALL.md` is the
  tester-facing copy; `ios-app/RELEASE.md` is the maintainer runbook.
---
kind: Changed
task: t_d1a9eeb6

- **One configurable bundle-id prefix for all iOS targets and their App Group.**
  `HSCC_BUNDLE_PREFIX` (default `com.hscc`) now derives the four product bundle
  ids and `HSCC_APP_GROUP: group.$(HSCC_BUNDLE_PREFIX).ios` in `project.yml`;
  every entitlement and Info.plist reads that derivation. `SharedStore.AppGroup`
  reads the group from the `HSCCAppGroup` Info.plist key instead of naming a
  literal, so a team can release under its own reverse-DNS prefix without
  editing Swift. The macOS target carries the same derived key because it reuses
  the shared store.
- **The App Group and release-config gates moved with the derivation.**
  `widget_settings_check.sh` reconstructs its bare-executable suite shim from
  the same `project.yml` prefix, and `notify_check.sh` imports Foundation for
  the sliced `AppGroup` enum. `generate.sh` now verifies all four targets and
  names the missing-team error for each of them.
