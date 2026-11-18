#!/bin/bash
# release.sh — build, sign, notarize and publish the HSCC Widgets macOS app.
#
# Operator flow (one real command, after the one-time notary setup below):
#
#   export HSCC_DEVELOPMENT_TEAM=YOURTEAMID
#   macos-app/scripts/release.sh 0.1.0
#
# What it does, in order:
#   1. xcodegen generate (the archive's project is always generated, never a
#      stale checked-in .xcodeproj)
#   2. xcodebuild archive (Release, Any Mac) with hardened runtime enabled
#   3. xcodebuild -exportArchive with method=developer-id, so the app AND the
#      embedded HSCCMacWidgets.appex are signed by the keychain's
#      "Developer ID Application" identity, carry the App Group entitlement and
#      are re-signed for distribution
#   4. stage the app as "HSCC Widgets.app" (the name the cask installs), verify
#      the signature, the hardened-runtime flag, the embedded extension and the
#      App Group entitlement
#   5. notarize with `xcrun notarytool submit --keychain-profile
#      "${HSCC_NOTARY_PROFILE:-hscc-notary}" --wait`, then staple
#   6. zip with `ditto -c -k --keepParent` as HSCC-Widgets-<version>.zip,
#      print its sha256 and sign it with Sparkle's sign_update (EdDSA key in
#      the login keychain)
#   7. upload the zip to the GitHub release apps-v<version> on pom11/hscc
#      (created when absent, ALWAYS --latest=false so it never shadows the
#      CLI's releases; the iOS IPA lands on the same release)
#   8. publish /macos/ on gh-pages: the install page and a Sparkle 2 appcast
#      item with the notarized zip URL, length and EdDSA signature
#   9. render the Homebrew cask; with --push-cask commit it to the tap via
#      `gh api` (scripts/bump_cask.py)
#
# --dry-run runs steps 1-4 for real (generate, archive, export, verify) and then
# PRINTS steps 5-9 without notarizing, stapling, zipping or publishing. Use it
# to prove the signing setup before a real release. A missing Developer ID
# identity or team stops the dry run with the exact setup step that is missing;
# a missing notary profile only stops a REAL run (the dry run never reaches
# notarization, and prints the one-time setup command instead).
#
# SPARKLE (one-time, on the release Mac):
#   The Sparkle package is pinned in project.yml. Resolve it once, then create
#   the EdDSA key; the private key stays in the login keychain and only the
#   PUBLIC key is put in Info.plist:
#     xcodebuild -project macos-app/HSCCMac.xcodeproj -scheme HSCCMac \
#       -resolvePackageDependencies -derivedDataPath macos-app/build/DerivedData
#     macos-app/build/DerivedData/SourcePackages/artifacts/sparkle/Sparkle/bin/generate_keys
#   release.sh finds sign_update in that directory automatically.
#
# ONE-TIME NOTARY SETUP (values are never stored in this repo):
#   xcrun notarytool store-credentials hscc-notary --apple-id <id> --team-id <team>
#
# Env overrides:
#   HSCC_DEVELOPMENT_TEAM   Apple team id (required; same one the iOS app uses)
#   HSCC_CODESIGN_IDENTITY  exact "Developer ID Application: …" name; only needed
#                           when the keychain holds more than one
#   HSCC_NOTARY_PROFILE     notarytool keychain profile (default: hscc-notary)
#   HSCC_BUNDLE_PREFIX      reverse-DNS prefix (default: com.hscc); moves the
#                           bundle ids and the App Group together
#   HSCC_REPO               release repo (default: pom11/hscc)
#   HSCC_RELEASE_TAG        exact release tag (default: apps-v<version>)
#   HSCC_RELEASE_TAG_PREFIX tag prefix when HSCC_RELEASE_TAG is unset
#   HSCC_BUILD_NUMBER       CFBundleVersion (default: apps/BUILD_NUMBER)
#   HSCC_RELEASE_NOTES_FILE rendered notes (default: generic release text)
#   HSCC_SPARKLE_PUBLIC_KEY public EdDSA key; passed through to the build
#   HSCC_SPARKLE_SIGN_UPDATE override the sign_update path
#
# Exit codes: 0 success; 2 usage; any other non-zero is the failed
# build/sign/notarize/upload step (xcodebuild and notarytool keep their own
# codes, so a CI log says which tool rejected it).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MACOS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$MACOS_DIR/.." && pwd)"

DRY_RUN=0
UPLOAD=1
PUSH_CASK=0
VERSION=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-upload) UPLOAD=0 ;;
    --push-cask) PUSH_CASK=1 ;;
    -h|--help) sed -n '1,60p' "${BASH_SOURCE[0]}"; exit 0 ;;
    -*) echo "error: unknown option: $arg" >&2; exit 2 ;;
    *)
      if [ -n "$VERSION" ]; then
        echo "error: more than one version given: $VERSION and $arg" >&2; exit 2
      fi
      VERSION="$arg" ;;
  esac
done

if [ -z "$VERSION" ]; then
  echo "usage: $0 [--dry-run] [--no-upload] [--push-cask] <version>" >&2
  echo "       e.g. $0 0.1.0" >&2
  exit 2
fi
if ! printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "error: version must look like 0.1.0 (digits and dots, no leading 'v'): $VERSION" >&2
  exit 2
fi
if [ "$UPLOAD" = "0" ] && [ "$PUSH_CASK" = "1" ]; then
  echo "error: --push-cask needs the release asset to exist; drop --no-upload." >&2
  exit 2
fi

REPO="${HSCC_REPO:-pom11/hscc}"
TAG_PREFIX="${HSCC_RELEASE_TAG_PREFIX:-apps-v}"
TAG="${HSCC_RELEASE_TAG:-${TAG_PREFIX}${VERSION}}"
ASSET="HSCC-Widgets-${VERSION}.zip"
APP_NAME="HSCC Widgets"
PREFIX="${HSCC_BUNDLE_PREFIX:-com.hscc}"
APP_GROUP="group.${PREFIX}.ios"
TEAM="${HSCC_DEVELOPMENT_TEAM:-}"
PROFILE="${HSCC_NOTARY_PROFILE:-hscc-notary}"
BUILD="${HSCC_BUILD_NUMBER:-$(cat "$REPO_ROOT/apps/BUILD_NUMBER" 2>/dev/null || echo 1)}"
NOTES_FILE="${HSCC_RELEASE_NOTES_FILE:-}"
SPARKLE_KEY="${HSCC_SPARKLE_PUBLIC_KEY:-}"
if ! printf '%s' "$BUILD" | grep -Eq '^[0-9]+$'; then
  echo "error: build number must be one integer (apps/BUILD_NUMBER): $BUILD" >&2
  exit 2
fi

BUILD_DIR="$MACOS_DIR/build/release/$VERSION"
ARCHIVE="$BUILD_DIR/HSCCMac.xcarchive"
EXPORT_DIR="$BUILD_DIR/export"
EXPORT_OPTIONS="$BUILD_DIR/ExportOptions.plist"
STAGE_DIR="$BUILD_DIR/stage"
APP="$STAGE_DIR/$APP_NAME.app"
APPEX="$APP/Contents/PlugIns/HSCCMacWidgets.appex"
NOTARY_ZIP="$BUILD_DIR/notarize-$VERSION.zip"
ZIP="$BUILD_DIR/$ASSET"

step() { echo ""; echo "==> $*"; }
note() { echo "    $*"; }
fail() { echo "error: $*" >&2; exit 1; }

# Runs a command in every mode (the steps a dry run proves for real).
run() {
  printf '    $'
  printf ' %q' "$@"
  printf '\n'
  "$@"
}

# Prints a command without running it (the steps after notarization in dry-run).
plan() {
  printf '    [dry-run] $'
  printf ' %q' "$@"
  printf '\n'
}

need() {
  command -v "$1" >/dev/null 2>&1 || fail "$1 not found. $2"
}

# Sparkle's release tools come from the pinned SwiftPM package, after a resolve.
# The private EdDSA key is in the login keychain; only sign_update is needed
# here (generate_keys is documented in the header and RELEASING.md).
sparkle_tool() {
  local tool="$1" base candidate
  if [ "$tool" = "sign_update" ] && [ -n "${HSCC_SPARKLE_SIGN_UPDATE:-}" ]; then
    printf '%s\n' "$HSCC_SPARKLE_SIGN_UPDATE"
    return 0
  fi
  base="$MACOS_DIR/build/DerivedData/SourcePackages/artifacts/sparkle/Sparkle/bin"
  if [ -x "$base/$tool" ]; then
    printf '%s\n' "$base/$tool"
    return 0
  fi
  candidate="$(find "$MACOS_DIR/build" -type f -path "*/Sparkle/bin/$tool" -perm -111 2>/dev/null | head -1)"
  [ -n "$candidate" ] && printf '%s\n' "$candidate"
}

notary_setup_hint() {
  cat >&2 <<MSG

Create the notary credentials ONCE on this Mac (values are never committed):

    xcrun notarytool store-credentials $PROFILE --apple-id <id> --team-id <team>

Use the Apple ID and team that own the Developer ID certificate, then re-run:

    macos-app/scripts/release.sh $VERSION

MSG
}

# ── preflight ────────────────────────────────────────────────────────────────

step "Preflight"
note "version:       $VERSION (build $BUILD)"
note "release:       $REPO  tag $TAG  asset $ASSET"
note "bundle prefix: $PREFIX   app group: $APP_GROUP"
note "notary:        profile ${PROFILE}"
[ "$DRY_RUN" = "1" ] && note "mode:          DRY RUN — steps 1-4 run for real, 5-9 are printed only"
[ "$UPLOAD" = "0" ] && note "upload:        disabled (--no-upload)"

need xcodegen "Install it with: brew install xcodegen"
need xcodebuild "Install Xcode from the App Store, then: sudo xcode-select -s /Applications/Xcode.app"
need xcrun "Install Xcode from the App Store."
need security "macOS ships it; this does not look like a macOS host."
need ditto "macOS ships it; this does not look like a macOS host."
need shasum "macOS ships it; this does not look like a macOS host."
need python3 "Install the Xcode command line tools: xcode-select --install"
if [ "$UPLOAD" = "1" ] && [ "$DRY_RUN" = "0" ]; then
  need gh "Install the GitHub CLI: brew install gh"
  gh auth status >/dev/null 2>&1 || fail "gh is not authenticated. Run: gh auth login"
fi

if [ -z "$TEAM" ]; then
  cat >&2 <<'MSG'
error: HSCC_DEVELOPMENT_TEAM is not set.

The generated project signs with this team; export cannot find the App Group
profiles without it. Use the same team the iOS app uses:

    export HSCC_DEVELOPMENT_TEAM=YOURTEAMID
    macos-app/scripts/release.sh <version>
MSG
  exit 1
fi
note "team:          $TEAM"

IDENTITY="${HSCC_CODESIGN_IDENTITY:-}"
if [ -z "$IDENTITY" ]; then
  identities=()
  while IFS= read -r line; do
    [ -n "$line" ] && identities+=("$line")
  done < <(security find-identity -v -p codesigning 2>/dev/null \
           | grep -v CSSMERR \
           | sed -n 's/^ *[0-9]*) [0-9A-F]* "\(Developer ID Application: .*\)"$/\1/p')
  if [ "${#identities[@]}" -eq 0 ]; then
    cat >&2 <<'MSG'
error: no valid "Developer ID Application" identity in the keychain.

This is the one step a dry run cannot fake: distribution signing needs a
Developer ID certificate. Create/download it in Xcode → Settings → Accounts →
Manage Certificates (or developer.apple.com → Certificates), then check:

    security find-identity -v -p codesigning
MSG
    exit 1
  fi
  if [ "${#identities[@]}" -gt 1 ]; then
    echo "error: more than one Developer ID Application identity; pick one with" >&2
    echo "       HSCC_CODESIGN_IDENTITY=\"Developer ID Application: …\":" >&2
    printf '         %s\n' "${identities[@]}" >&2
    exit 1
  fi
  IDENTITY="${identities[0]}"
fi
note "identity:      $IDENTITY"

if [ "$DRY_RUN" = "0" ]; then
  if ! notary_history="$(xcrun notarytool history --keychain-profile "$PROFILE" 2>&1)"; then
    echo "error: the notarization keychain profile \"$PROFILE\" is missing or unusable." >&2
    echo "       xcrun said: $(printf '%s' "$notary_history" | head -3)" >&2
    notary_setup_hint
    exit 1
  fi
  note "notary profile: usable"
else
  note "notary profile: not checked in a dry run (never reaches notarization)"
  plan "xcrun notarytool store-credentials $PROFILE --apple-id <id> --team-id <team>   # once, if missing"
fi

mkdir -p "$BUILD_DIR"

# ── 1. generate ──────────────────────────────────────────────────────────────

step "Generate the Xcode project (xcodegen)"
run env HSCC_DEVELOPMENT_TEAM="$TEAM" HSCC_BUNDLE_PREFIX="$PREFIX" \
  HSCC_SPARKLE_PUBLIC_KEY="$SPARKLE_KEY" \
  xcodegen generate --spec "$MACOS_DIR/project.yml" --project "$MACOS_DIR"

# ── 1b. Sparkle tools + the PUBLIC EdDSA key ─────────────────────────────────

step "Resolve Sparkle and read the public EdDSA key"
if [ -z "$(sparkle_tool generate_keys || true)" ]; then
  resolve_cmd=(xcodebuild -project "$MACOS_DIR/HSCCMac.xcodeproj" \
    -scheme HSCCMac -resolvePackageDependencies \
    -derivedDataPath "$MACOS_DIR/build/DerivedData")
  if [ "$DRY_RUN" = "1" ]; then
    plan "${resolve_cmd[@]}"
  else
    run "${resolve_cmd[@]}"
  fi
fi
generate_keys_bin="$(sparkle_tool generate_keys || true)"
if [ -z "$SPARKLE_KEY" ] && [ -n "$generate_keys_bin" ] && [ "$DRY_RUN" = "0" ]; then
  SPARKLE_KEY="$("$generate_keys_bin" -p 2>/dev/null || true)"
fi
if [ -z "$SPARKLE_KEY" ]; then
  if [ "$DRY_RUN" = "1" ]; then
    note "[dry-run] no public key resolved yet (a real run requires one)"
    plan "${generate_keys_bin:-<Sparkle>/bin/generate_keys}   # once, if the keychain has no key"
  else
    fail "no Sparkle public EdDSA key. Run this once and re-run:
    ${generate_keys_bin:-<Sparkle>/bin/generate_keys}
then either export HSCC_SPARKLE_PUBLIC_KEY or let this script read it with -p."
  fi
else
  note "sparkle public key: ${SPARKLE_KEY:0:12}… (private key stays in the login keychain)"
fi

# ── 2. archive ───────────────────────────────────────────────────────────────

step "Archive (Release, Any Mac, hardened runtime)"
archive_cmd=(
  xcodebuild
  -project "$MACOS_DIR/HSCCMac.xcodeproj"
  -scheme HSCCMac
  -configuration Release
  -destination "generic/platform=macOS"
  -archivePath "$ARCHIVE"
  -allowProvisioningUpdates
  DEVELOPMENT_TEAM="$TEAM"
  HSCC_BUNDLE_PREFIX="$PREFIX"
  HSCC_SPARKLE_PUBLIC_KEY="$SPARKLE_KEY"
  CODE_SIGN_STYLE=Automatic
  ENABLE_HARDENED_RUNTIME=YES
  MARKETING_VERSION="$VERSION"
  CURRENT_PROJECT_VERSION="$BUILD"
  archive
)
run "${archive_cmd[@]}"

# ── 3. export with Developer ID ──────────────────────────────────────────────

step "Export with Developer ID signing"
cat > "$EXPORT_OPTIONS" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>method</key>
	<string>developer-id</string>
	<key>signingStyle</key>
	<string>automatic</string>
	<key>teamID</key>
	<string>$TEAM</string>
	<key>destination</key>
	<string>export</string>
</dict>
</plist>
PLIST
note "export options: method=developer-id signingStyle=automatic teamID=$TEAM"
rm -rf "$EXPORT_DIR"
run xcodebuild -exportArchive \
  -archivePath "$ARCHIVE" \
  -exportOptionsPlist "$EXPORT_OPTIONS" \
  -exportPath "$EXPORT_DIR" \
  -allowProvisioningUpdates

# ── 4. stage + verify the bundle ─────────────────────────────────────────────

step "Stage and verify $APP_NAME.app"
exported_app="$(find "$EXPORT_DIR" -maxdepth 1 -type d -name '*.app' -print -quit)"
[ -n "$exported_app" ] || fail "export produced no .app under $EXPORT_DIR"
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR"
ditto "$exported_app" "$APP"
[ -d "$APPEX" ] || fail "the widget extension is missing from the exported app: $APPEX"

verify_authority() {
  local target="$1" label="$2" info
  codesign --verify --deep --strict "$target" \
    || fail "$label does not pass codesign --verify --deep --strict"
  info="$(codesign -dv --verbose=4 "$target" 2>&1 || true)"
  printf '%s\n' "$info" | grep -q "^Authority=Developer ID Application" \
    || fail "$label is not signed by Developer ID Application (got: $(printf '%s\n' "$info" | sed -n 's/^Authority=//p' | paste -sd ', ' -))"
  printf '%s\n' "$info" | grep -q 'flags=.*runtime' \
    || fail "$label is not built with the hardened runtime"
  printf '%s\n' "$info" | grep -q "TeamIdentifier=$TEAM" \
    || fail "$label has the wrong TeamIdentifier (expected $TEAM)"
}

verify_entitlement() {
  local target="$1" label="$2" ent
  ent="$(codesign -d --entitlements :- "$target" 2>/dev/null || true)"
  printf '%s' "$ent" | grep -q "$APP_GROUP" \
    || fail "$label is missing the App Group entitlement $APP_GROUP"
}

verify_authority "$APP" "the app"
verify_entitlement "$APP" "the app"
verify_authority "$APPEX" "the embedded widget extension"
verify_entitlement "$APPEX" "the embedded widget extension"

built_version="$(plutil -extract CFBundleShortVersionString raw "$APP/Contents/Info.plist" 2>/dev/null || true)"
[ "$built_version" = "$VERSION" ] \
  || fail "the exported app reports version '$built_version', expected $VERSION"
built_build="$(plutil -extract CFBundleVersion raw "$APP/Contents/Info.plist" 2>/dev/null || true)"
[ "$built_build" = "$BUILD" ] \
  || fail "the exported app reports build '$built_build', expected $BUILD"
built_id="$(plutil -extract CFBundleIdentifier raw "$APP/Contents/Info.plist" 2>/dev/null || true)"
[ "$built_id" = "${PREFIX}.mac" ] \
  || fail "the exported app has bundle id '$built_id', expected ${PREFIX}.mac"
note "signed:  Developer ID Application + hardened runtime (app and extension)"
note "group:   $APP_GROUP"
note "version: $built_version ($built_build) [$built_id]"

# ── 5. notarize + staple ─────────────────────────────────────────────────────

step "Notarize with notarytool (profile $PROFILE)"
if [ "$DRY_RUN" = "1" ]; then
  note "dry run stops HERE — notarization is the step that needs the real notary profile."
  notary_setup_hint >&2
  plan ditto -c -k --keepParent "$APP" "$NOTARY_ZIP"
  plan xcrun notarytool submit "$NOTARY_ZIP" --keychain-profile "$PROFILE" --wait
  plan xcrun stapler staple "$APP"
  plan xcrun stapler validate "$APP"
else
  run ditto -c -k --keepParent "$APP" "$NOTARY_ZIP"
  notary_log="$BUILD_DIR/notary-$VERSION.log"
  if ! xcrun notarytool submit "$NOTARY_ZIP" \
        --keychain-profile "$PROFILE" --wait 2>&1 | tee "$notary_log"; then
    fail "notarytool submit failed. Inspect $notary_log, then re-run."
  fi
  if ! grep -q "status: Accepted" "$notary_log"; then
    submission="$(sed -n 's/^ *id: //p' "$notary_log" | head -1)"
    echo "error: notarization was not accepted (status in $notary_log)." >&2
    [ -n "$submission" ] && echo "       Details: xcrun notarytool log $submission --keychain-profile $PROFILE" >&2
    exit 1
  fi
  note "notarization accepted"
  run xcrun stapler staple "$APP"
  run xcrun stapler validate "$APP"
  run spctl --assess --type execute --verbose=2 "$APP"
fi

# ── 6. zip + digest ──────────────────────────────────────────────────────────

step "Package $ASSET and sign it for Sparkle"
if [ "$DRY_RUN" = "1" ]; then
  plan ditto -c -k --keepParent "$APP" "$ZIP"
  plan shasum -a 256 "$ZIP"
  plan "$(sparkle_tool sign_update 2>/dev/null || echo '<Sparkle>/bin/sign_update')" "$ZIP"
  note "a real run also verifies the zip's top-level $APP_NAME.app, prints the digest"
  note "and parses sign_update's sparkle:edSignature for the appcast."
else
  rm -f "$ZIP" "$ZIP.sha256"
  run ditto -c -k --keepParent "$APP" "$ZIP"
  unzip -l "$ZIP" | grep "^.*$APP_NAME.app/Contents/MacOS/" >/dev/null \
    || fail "$ZIP does not carry $APP_NAME.app at its root"
  digest="$(shasum -a 256 "$ZIP" | awk '{print $1}')"
  printf '%s  %s\n' "$digest" "$ASSET" > "$ZIP.sha256"
  note "sha256: $digest"
  note "zip:    $ZIP"

  sign_update="$(sparkle_tool sign_update || true)"
  [ -n "$sign_update" ] || fail "Sparkle's sign_update was not found. Resolve the pinned package:
    xcodebuild -project \"$MACOS_DIR/HSCCMac.xcodeproj\" -scheme HSCCMac \\
      -resolvePackageDependencies -derivedDataPath \"$MACOS_DIR/build/DerivedData\"
then re-run, or set HSCC_SPARKLE_SIGN_UPDATE=/path/to/sign_update."
  sig_output="$("$sign_update" "$ZIP")"
  signature="$(printf '%s\n' "$sig_output" | sed -n 's/.*sparkle:edSignature="\([^"]*\)".*/\1/p' | head -1)"
  zip_length="$(wc -c < "$ZIP" | tr -d ' ')"
  [ -n "$signature" ] || fail "sign_update produced no sparkle:edSignature (output: $sig_output).
If the key is missing, create it once with:
    $(dirname "$sign_update")/generate_keys
the private key stays in your login keychain; put the printed PUBLIC key in
HSCC_SPARKLE_PUBLIC_KEY so it lands in Info.plist."
  note "sparkle: signed with EdDSA (signature ${signature:0:16}…)"
fi

# ── 7. upload ────────────────────────────────────────────────────────────────

step "Publish GitHub release $TAG (never latest)"
if [ "$DRY_RUN" = "1" ] || [ "$UPLOAD" = "0" ]; then
  if [ "$UPLOAD" = "0" ]; then
    note "upload skipped (--no-upload); the cask would point at:"
  else
    note "dry run: a real run uploads the zip and keeps the release out of 'latest':"
  fi
  note "  https://github.com/$REPO/releases/download/$TAG/$ASSET"
  plan gh release view "$TAG" --repo "$REPO"
  plan gh release upload "$TAG" "$ZIP" --repo "$REPO" --clobber
  plan gh release edit "$TAG" --repo "$REPO" --latest=false
  note "  (when the release does not exist: gh release create $TAG <zip> --latest=false)"
else
  notes_args=(--notes "Menu bar app and desktop widgets for HSCC. The asset is a Developer ID-signed, notarized and stapled app bundle.")
  if [ -n "$NOTES_FILE" ] && [ -f "$NOTES_FILE" ]; then
    notes_args=(--notes-file "$NOTES_FILE")
  fi
  if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
    run gh release upload "$TAG" "$ZIP" --repo "$REPO" --clobber
    run gh release edit "$TAG" --repo "$REPO" --latest=false "${notes_args[@]}"
  else
    run gh release create "$TAG" "$ZIP" \
      --repo "$REPO" \
      --title "HSCC apps $VERSION" \
      "${notes_args[@]}" \
      --latest=false
  fi
  run gh release view "$TAG" --repo "$REPO" --json assets \
    --jq '.assets[].name' | grep -x "$ASSET" >/dev/null \
    || fail "release $TAG does not list $ASSET after upload"
  note "asset: https://github.com/$REPO/releases/download/$TAG/$ASSET"
fi

# ── 8. macos page + Sparkle appcast on gh-pages ─────────────────────────────

step "Publish the /macos page and appcast"
zip_url="https://github.com/$REPO/releases/download/$TAG/$ASSET"
if [ "$DRY_RUN" = "1" ] || [ "$UPLOAD" = "0" ]; then
  plan python3 "$REPO_ROOT/scripts/apps_release.py" publish-macos \
    --pages-dir "<gh-pages>" --macos-dir "$MACOS_DIR" \
    --version "$VERSION" --build "$BUILD" \
    --zip-url "$zip_url" --length "<zip length>" --signature "<EdDSA signature>"
else
  run "$SCRIPT_DIR/publish_macos.sh" "$VERSION" "$BUILD" "$ZIP" "$signature" "$NOTES_FILE"
fi

# ── 9. cask ──────────────────────────────────────────────────────────────────

step "Homebrew cask (pom11/homebrew-hscc)"
if [ "$DRY_RUN" = "1" ] || [ "$UPLOAD" = "0" ]; then
  plan env HSCC_RELEASE_TAG="$TAG" python3 "$REPO_ROOT/scripts/bump_cask.py" --version "$VERSION" --zip "$ZIP"
  plan env HSCC_RELEASE_TAG="$TAG" python3 "$REPO_ROOT/scripts/bump_cask.py" --version "$VERSION" --zip "$ZIP" --push
else
  run env HSCC_RELEASE_TAG="$TAG" python3 "$REPO_ROOT/scripts/bump_cask.py" --version "$VERSION" --zip "$ZIP"
  if [ "$PUSH_CASK" = "1" ]; then
    run env HSCC_RELEASE_TAG="$TAG" python3 "$REPO_ROOT/scripts/bump_cask.py" --version "$VERSION" --zip "$ZIP" --push
  else
    note "tap not changed. Publish the cask with:"
    note "  HSCC_RELEASE_TAG=$TAG python3 scripts/bump_cask.py --version $VERSION --zip $ZIP --push"
    note "  (or re-run this script with --push-cask to do it here)"
  fi
fi

echo ""
if [ "$DRY_RUN" = "1" ]; then
  echo "DONE (dry run): generated, archived, exported and verified $APP_NAME $VERSION;"
  echo "nothing was notarized, stapled, zipped or published."
else
  echo "DONE: HSCC Widgets $VERSION"
  echo "Install: brew install --cask pom11/hscc/hscc-widgets"
fi
