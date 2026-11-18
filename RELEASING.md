# Releasing the iOS and macOS apps

Both apps ship from **one command** and **one GitHub release**:

```sh
scripts/release_apps.sh <version>            # iOS + macOS
scripts/release_apps.sh <version> --ios-only
scripts/release_apps.sh <version> --mac-only
scripts/release_apps.sh <version> --dry-run  # rehearse; changes nothing
```

`<version>` is `MAJOR.MINOR.PATCH` (for example `1.1.0`). The entry point:

1. refuses a dirty tree and a version that is not greater than the last released
   app version (the highest `apps-v*` tag; the pre-migration `ios-v*` /
   `macos-widgets-v*` tags count as the 1.0.0 baseline);
2. writes `CFBundleShortVersionString=<version>` and a monotonic
   `CFBundleVersion` (`apps/BUILD_NUMBER` + 1) into **both**
   `ios-app/project.yml` and `macos-app/project.yml`, and stores the new build
   number in `apps/BUILD_NUMBER`;
3. commits `apps: release <version>` and tags `apps-v<version>`, then pushes the
   commit and tag;
4. renders release notes from the `changelog.d/` fragments that touched
   `ios-app/` or `macos-app/` since the previous app tag (grouped Added /
   Changed / Fixed, card ids removed) and uses them as the GitHub release body;
5. runs `ios-app/scripts/release_adhoc.sh` and
   `macos-app/scripts/release.sh` with `HSCC_RELEASE_TAG=apps-v<version>`, so
   the IPA and the notarized zip land on **one** release. That release is
   always created/edited with `--latest=false`; the CLI releases own “latest”.
   The old per-platform tag names still work when a platform script is run
   standalone with `HSCC_RELEASE_TAG=ios-v<version>` (or
   `macos-widgets-v<version>`).

`--dry-run` validates the version/build, renders the notes, writes both Pages
sites and both feeds into a temp directory (and discards them), then prints the
version-bump commit/tag and the exact platform commands. It never builds, signs,
notarizes, uploads or commits.

## Prerequisites (one-time, on the release Mac)

These values are never committed.

| What | How |
|---|---|
| Xcode signed in | Xcode → Settings → Accounts → **+**, sign in with the team's Apple ID. Automatic signing creates the distribution certificate, App IDs, App Group and Ad Hoc profiles. |
| Team id | `export HSCC_DEVELOPMENT_TEAM=YOURTEAMID` (shown in Xcode → Settings → Accounts). |
| Notary profile | `xcrun notarytool store-credentials hscc-notary --apple-id <id> --team-id <team>` |
| Sparkle EdDSA key | Resolve the pinned package, then run `generate_keys` once (below). The private key stays in the login keychain. |
| iPhones | Register each UDID at developer.apple.com → Devices (see “Add a device”). |
| GitHub CLI | `gh auth login` with write access to `pom11/hscc` and the Homebrew tap. |

### Sparkle key and tools

The macOS app pins Sparkle 2 in `macos-app/project.yml`
(`packages: Sparkle`, exact version). The package also supplies the release
tools; resolve it once on the release Mac:

```sh
xcodebuild -project macos-app/HSCCMac.xcodeproj -scheme HSCCMac \
  -resolvePackageDependencies -derivedDataPath macos-app/build/DerivedData
```

The tools then live at:

```text
macos-app/build/DerivedData/SourcePackages/artifacts/sparkle/Sparkle/bin/
  generate_keys    # one-time: creates the EdDSA key in the login keychain
  sign_update      # release.sh runs this on every notarized zip
```

Create the key once (if `generate_keys` says a key already exists, keep it):

```sh
macos-app/build/DerivedData/SourcePackages/artifacts/sparkle/Sparkle/bin/generate_keys
```

`generate_keys` prints the **public** key. Put it in the build environment for
releases and in `macos-app/project.yml` (`HSCC_SPARKLE_PUBLIC_KEY`), which is
what writes `SUPublicEDKey` into the app's Info.plist:

```sh
export HSCC_SPARKLE_PUBLIC_KEY="<public key printed by generate_keys>"
```

Never copy the private key out of the keychain and never commit it. If the key
is lost, generate a new pair; existing installs then require one manual update
because their old public key cannot verify the new feed.

### iOS identity

The shipped iOS app uses `ro.parsec.hscc` (`group.ro.parsec.hscc.ios`);
`group.com.hscc.ios` belongs to another team. `release_apps.sh` fixes that
prefix for the iOS leg, so a release cannot silently change the app identity.
macOS keeps `com.hscc` (`com.hscc.mac`), which is what 1.0.0 shipped.

Override only if you deliberately move the app: `HSCC_IOS_BUNDLE_PREFIX` /
`HSCC_MACOS_BUNDLE_PREFIX`. The App Group is always `group.<prefix>.ios`.

## What a release publishes

| Where | What |
|---|---|
| GitHub release `apps-v<version>` (never latest) | `HSCC-<version>.ipa` + `HSCC-Widgets-<version>.zip` |
| `gh-pages` `/ios/` | install page (version, What's new, previous versions), `manifest.plist`, `manifests/<v>.plist`, `version.json`, `releases.json`, `notes/<v>.html`, icons |
| `gh-pages` `/macos/` | brew command + What's new, `notes/<v>.html`, Sparkle `appcast.xml` |
| Homebrew tap `pom11/homebrew-hscc` | `Casks/hscc-widgets.rb` with the new version, sha256 and `auto_updates true` |

The iOS feed is `https://pom11.github.io/hscc/ios/version.json`:

```json
{"version": "1.1.0", "build": 2, "released_at": "…",
 "notes_url": "https://pom11.github.io/hscc/ios/notes/1.1.0.html",
 "install_url": "https://pom11.github.io/hscc/ios/", "min_os": "26.0"}
```

The macOS feed is a Sparkle 2 appcast at
`https://pom11.github.io/hscc/macos/appcast.xml`. Each item carries the version,
build (`sparkle:version`), release date, notes link, and an enclosure with the
notarized zip URL, its byte length and `sparkle:edSignature`. `sign_update` signs
the exact zip that was uploaded.

`version.json` and `appcast.xml` are what the in-app update checks read:

* **iOS** (`UpdateChecker`) fetches `version.json` on launch and at most every
  six hours in the foreground, over its own short-timeout `URLSession` with no
  token and no cluster client. A newer version/build shows a dismissible
  "Update &lt;version&gt; available" banner with **What's new** and **Install**;
  Settings → About shows the running version/build, the last result and a
  manual check.
* **macOS** uses Sparkle 2 (`macos-app/Sources/HSCCMac/SparkleUpdater.swift`)
  against `appcast.xml`. Automatic checks run daily by default; **Check for
  Updates…** is in the menu bar dropdown and Settings, which also carries the
  automatic-check and automatic-download toggles. Sparkle installs the signed
  app bundle (including the embedded widget extension) and relaunches the menu
  bar app.

## Add a device (iOS Ad Hoc)

1. Connect the iPhone to the Mac, open Finder, select it, and click the
   model/capacity line until the **UDID** appears (Apple Configurator shows it
   too).
2. Add the UDID at developer.apple.com → Devices.
3. Re-run `scripts/release_apps.sh <same-or-newer-version> --ios-only` so the
   Ad Hoc profile includes the new device. Existing installs keep working.

Nothing else changes: the App Group and bundle ids stay the same.

## Rollback

A release is a commit, a tag, a GitHub release and two Pages feeds. Decide which
layer is wrong:

* **Bad notes/feed only** — fix the generator or the fragment, then re-run the
  platform publish step for that platform (`publish_pages.sh` /
  `publish_macos.sh`) with the same version. The appcast upsert replaces the
  item for that build instead of duplicating it.
* **Bad binary, tag already pushed** — do **not** move the tag. Delete the
  GitHub release and its assets, then cut a new patch version with the fix:

  ```sh
  gh release delete apps-v<bad-version> --yes
  git push origin :apps-v<bad-version>          # delete the remote tag
  git tag -d apps-v<bad-version>                # and the local one
  scripts/release_apps.sh <next-version>
  ```

  Bump `apps/BUILD_NUMBER` only through a release (it is the monotonic source
  for `CFBundleVersion`); never hand-edit it downward.
* **Bad cask** — fix the tap file with
  `python3 scripts/bump_cask.py --version <good-version> --zip <good-zip> --push`.
  Homebrew users get the corrected bytes on the next `brew upgrade`.

The iOS install page always points at the current manifest; every old manifest
stays under `/ios/manifests/`, and old notes stay under `/ios/notes/` and
`/macos/notes/`.

## Gates

```sh
ios-app/scripts/build_check.sh        # 0 errors, 0 warnings
macos-app/scripts/build_check.sh      # 0 errors, 0 warnings (swiftc-only;
                                      #  Sparkle is resolved separately)
ios-app/scripts/update_check.sh       # feed rules + app-only target boundary
macos-app/scripts/update_check.sh     # Sparkle wiring + extension boundary
python3 -m pytest scripts/tests/test_apps_release.py scripts/tests/test_bump_cask.py
scripts/release_apps.sh 1.1.0 --dry-run
python3 scripts/address_guard.py --tracked
```

`macos-app/scripts/build_check.sh` compiles the exact per-target file sets with
`swiftc`, so it does not resolve SwiftPM packages. `SparkleUpdater.swift` guards
its `import Sparkle` with `#if canImport(Sparkle)`: an Xcode build links the
pinned package and compiles the real updater, while the package-less compile
gate still checks the rest of the target. To compile the REAL Sparkle branch in
the gate, point it at a resolved or prebuilt framework:

```sh
HSCC_SPARKLE_FRAMEWORK=/path/containing/Sparkle.framework \
  macos-app/scripts/build_check.sh
```
