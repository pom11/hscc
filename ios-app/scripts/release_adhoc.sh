#!/bin/bash
# release_adhoc.sh — build, sign and publish an Ad Hoc iOS build from GitHub.
#
# DEFAULT SIGNING PATH (operator decision 2026-11-18): the Apple account already
# signed in to Xcode. Open Xcode → Settings → Accounts and sign in with the
# team's Apple ID, then run this script. Xcode's automatic signing creates the
# Apple Distribution certificate, registers the App IDs and App Group, and makes
# the Ad Hoc profiles for all four targets. No API key is required.
#
# OPTIONAL headless override: an App Store Connect API key. Set ALL THREE of
#   ASC_KEY_ID, ASC_ISSUER_ID, ASC_KEY_PATH
# and the archive step is given -authenticationKeyPath/-authenticationKeyID/
# -authenticationKeyIssuerID instead of relying on Xcode's account UI.
#
# What it does:
#   1. xcodegen generate (Release project; prefix + App Group from env)
#   2. xcodebuild archive for a generic iOS device, automatic signing
#   3. xcodebuild -exportArchive with method=release-testing (Ad Hoc)
#   4. scripts/publish_pages.sh: IPA → GitHub release apps-v<version> (not
#      "latest"), manifest.plist + index.html + icons → gh-pages branch
#
# Usage:
#   HSCC_DEVELOPMENT_TEAM=ABCDE12345 ios-app/scripts/release_adhoc.sh 1.2.3
#   HSCC_DEVELOPMENT_TEAM=ABCDE12345 HSCC_BUNDLE_PREFIX=dev.example \
#       ios-app/scripts/release_adhoc.sh 1.2.3
#   ios-app/scripts/release_adhoc.sh --dry-run 1.2.3
#
# HSCC_BUNDLE_PREFIX defaults to ro.parsec.hscc — the identity the shipped 1.0.0
# actually uses (group.com.hscc.ios belongs to another team). The App Group is
# always group.<prefix>.ios, derived once in project.yml. A release must never
# silently change the app identity, so the entry point
# scripts/release_apps.sh fixes this prefix too.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IOS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$IOS_DIR/.." && pwd)"

DRY_RUN=0
VERSION=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '1,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
    -*) echo "error: unknown option: $arg" >&2; exit 2 ;;
    *) VERSION="$arg" ;;
  esac
done

if [ -z "$VERSION" ]; then
  echo "usage: $0 [--dry-run] <version>   (for example: 1.2.3)" >&2
  exit 2
fi
if ! printf '%s' "$VERSION" | grep -Eq '^[0-9]+(\.[0-9]+){1,2}$'; then
  echo "error: version must look like 1.2.3 (digits and dots, no leading 'v'): $VERSION" >&2
  exit 2
fi

PREFIX="${HSCC_BUNDLE_PREFIX:-ro.parsec.hscc}"
BUNDLE_ID="${HSCC_BUNDLE_ID:-${PREFIX}.ios}"
APP_GROUP="group.${PREFIX}.ios"
TEAM="${HSCC_DEVELOPMENT_TEAM:-}"
BUILD="${HSCC_BUILD_NUMBER:-$(cat "$REPO_ROOT/apps/BUILD_NUMBER" 2>/dev/null || echo 1)}"
if ! printf '%s' "$BUILD" | grep -Eq '^[0-9]+$'; then
  echo "error: build number must be one integer (apps/BUILD_NUMBER): $BUILD" >&2
  exit 2
fi
TAG="${HSCC_RELEASE_TAG:-apps-v${VERSION}}"

KEY_ID="${ASC_KEY_ID:-}"
ISSUER_ID="${ASC_ISSUER_ID:-}"
KEY_PATH="${ASC_KEY_PATH:-}"
TEAM_FOR_PRINT="${TEAM:-<TEAM_ID>}"
KEY_COUNT=0
[ -n "$KEY_ID" ] && KEY_COUNT=$((KEY_COUNT + 1))
[ -n "$ISSUER_ID" ] && KEY_COUNT=$((KEY_COUNT + 1))
[ -n "$KEY_PATH" ] && KEY_COUNT=$((KEY_COUNT + 1))

BUILD_DIR="$IOS_DIR/build/adhoc/$VERSION"
ARCHIVE="$BUILD_DIR/HSCC.xcarchive"
EXPORT_DIR="$BUILD_DIR/export"
EXPORT_OPTIONS="$BUILD_DIR/ExportOptions.plist"
IPA="$BUILD_DIR/HSCC-$VERSION.ipa"

step() { echo ""; echo "==> $*"; }
note() { echo "    $*"; }
execute() {
  printf '    $'
  printf ' %q' "$@"
  printf '\n'
  [ "$DRY_RUN" = "1" ] && return 0
  "$@"
}

cleanup_hint() {
  local rc=$?
  cat >&2 <<MSG

release_adhoc.sh stopped (exit $rc).

If Xcode could not sign or export, check these in order:
  * Xcode → Settings → Accounts: the team's Apple ID is signed in.
  * developer.apple.com → Devices: this iPhone's UDID is registered. Connect
    the iPhone to this Mac with a cable and trust it, or add the UDID in the
    portal, then re-run.
  * The team id is right: HSCC_DEVELOPMENT_TEAM=$TEAM (settings in Xcode show it).
  * For a headless run, ASC_KEY_ID + ASC_ISSUER_ID + ASC_KEY_PATH are ALL set.

Nothing was published unless the "publish" step below ran.
MSG
}
trap cleanup_hint ERR

need() {
  command -v "$1" >/dev/null 2>&1 && return 0
  if [ "$DRY_RUN" = "1" ]; then
    note "missing on this host: $1 ($2)"
    return 0
  fi
  echo "error: $1 not found. $2" >&2
  exit 1
}

step "Preflight"
note "version:  $VERSION (build $BUILD)"
note "bundle:   $BUNDLE_ID"
note "app group: $APP_GROUP"
note "release:  $TAG"
need xcodegen "Install it with: brew install xcodegen"
need xcodebuild "Install Xcode from the App Store, then: sudo xcode-select -s /Applications/Xcode.app"
need xcrun "Install Xcode from the App Store."
need python3 "Install the Xcode command line tools: xcode-select --install"
need sips "sips ships with macOS."
need gh "Install the GitHub CLI: brew install gh"

if [ "$KEY_COUNT" -ne 0 ] && [ "$KEY_COUNT" -ne 3 ]; then
  echo "error: App Store Connect API key is an all-or-nothing override." >&2
  echo "       Set ALL of ASC_KEY_ID, ASC_ISSUER_ID and ASC_KEY_PATH, or none." >&2
  exit 1
fi
if [ "$KEY_COUNT" -eq 3 ] && [ ! -f "$KEY_PATH" ]; then
  echo "error: ASC_KEY_PATH does not exist: $KEY_PATH" >&2
  exit 1
fi
if [ "$KEY_COUNT" -eq 3 ]; then
  note "signing: Xcode + App Store Connect API key $KEY_ID"
else
  note "signing: Xcode account (Settings → Accounts); no API key"
fi

if [ -z "$TEAM" ]; then
  cat >&2 <<'MSG'
error: HSCC_DEVELOPMENT_TEAM is not set.

All three targets (HSCC, HSCCWidgets, HSCCLiveActivityMonitor) need the same
team. Find it in Xcode → Settings → Accounts (the "Team ID" next
to the team), then run:

    export HSCC_DEVELOPMENT_TEAM=YOURTEAMID
    ios-app/scripts/release_adhoc.sh <version>

MSG
  if [ "$DRY_RUN" = "0" ]; then exit 1; else note "[dry-run] continuing without a team so the steps can be inspected"; fi
fi

if [ "$DRY_RUN" = "0" ] && [ "$KEY_COUNT" -ne 3 ]; then
  teams="$(defaults read com.apple.dt.Xcode IDEProvisioningTeamByIdentifier 2>/dev/null || true)"
  if [ -n "$TEAM" ] && ! printf '%s' "$teams" | grep -q "$TEAM"; then
    if security find-identity -v -p codesigning 2>/dev/null | grep -q "($TEAM)"; then
      note "note: $TEAM has a local signing identity but is not listed in Xcode's Accounts; Xcode may still ask you to sign in."
    else
      cat >&2 <<MSG
error: team $TEAM is not available in Xcode.

Open Xcode → Settings → Accounts, click "+", and sign in with the Apple ID that
belongs to this team. Then run this script again. (A free personal team works
for sideloading; App Groups and Ad Hoc distribution need the paid team.)

If you meant to run headless instead, set ALL of ASC_KEY_ID, ASC_ISSUER_ID and
ASC_KEY_PATH.
MSG
      exit 1
    fi
  fi
fi

if [ "$DRY_RUN" = "0" ] && command -v xcrun >/dev/null 2>&1; then
  connected="$(xcrun xcdevice list --timeout 5 2>/dev/null | python3 -c '
import json, sys
try:
    devices = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for d in devices:
    if not d.get("simulator") and d.get("available") and not d.get("ignored"):
        print(d.get("name", "iPhone"))
' || true)"
  if [ -n "$connected" ]; then
    note "connected device(s): $(printf '%s' "$connected" | tr '\n' ',' | sed 's/,$//')"
  else
    cat >&2 <<'MSG'
note: no iPhone is connected over USB. That is fine only if this team already
      has a REGISTERED device in its Ad Hoc profile. If this is the first
      release, connect the iPhone, trust it on the Mac, then find its UDID in
      Finder (select the iPhone, click the model/capacity line) and add it at
      developer.apple.com → Devices before re-running.
MSG
  fi
fi

step "Generate Xcode project (xcodegen)"
note "HSCC_DEVELOPMENT_TEAM=$TEAM_FOR_PRINT xcodegen generate --spec project.yml"
if [ "$DRY_RUN" = "1" ]; then
  note "[dry-run] would run scripts/check_sources.sh, then xcodegen generate"
else
  ( cd "$IOS_DIR" && ./scripts/check_sources.sh )
  ( cd "$IOS_DIR" && HSCC_DEVELOPMENT_TEAM="$TEAM" xcodegen generate --spec project.yml )
fi

step "Archive for a generic iOS device (Release)"
mkdir -p "$BUILD_DIR"
archive_cmd=(
  xcodebuild
  -project "$IOS_DIR/HSCC.xcodeproj"
  -scheme HSCC
  -configuration Release
  -destination "generic/platform=iOS"
  -archivePath "$ARCHIVE"
  -allowProvisioningUpdates
  -allowProvisioningDeviceRegistration
  DEVELOPMENT_TEAM="$TEAM_FOR_PRINT"
  HSCC_BUNDLE_PREFIX="$PREFIX"
  CODE_SIGN_STYLE=Automatic
  MARKETING_VERSION="$VERSION"
  CURRENT_PROJECT_VERSION="$BUILD"
)
if [ "$KEY_COUNT" -eq 3 ]; then
  archive_cmd+=(
    -authenticationKeyPath "$KEY_PATH"
    -authenticationKeyID "$KEY_ID"
    -authenticationKeyIssuerID "$ISSUER_ID"
  )
fi
archive_cmd+=( archive )
execute "${archive_cmd[@]}"

step "Export the Ad Hoc IPA"
if [ "$DRY_RUN" = "1" ]; then
  note "[dry-run] would write $EXPORT_OPTIONS:"
  note "  method=release-testing signingStyle=automatic teamID=$TEAM_FOR_PRINT compileBitcode=NO thinning=<none>"
else
  cat > "$EXPORT_OPTIONS" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>method</key>
	<string>release-testing</string>
	<key>signingStyle</key>
	<string>automatic</string>
	<key>teamID</key>
	<string>$TEAM</string>
	<key>compileBitcode</key>
	<false/>
	<key>thinning</key>
	<string>&lt;none&gt;</string>
</dict>
</plist>
PLIST
fi
export_cmd=(
  xcodebuild
  -exportArchive
  -archivePath "$ARCHIVE"
  -exportOptionsPlist "$EXPORT_OPTIONS"
  -exportPath "$EXPORT_DIR"
  -allowProvisioningUpdates
)
if [ "$KEY_COUNT" -eq 3 ]; then
  export_cmd+=(
    -authenticationKeyPath "$KEY_PATH"
    -authenticationKeyID "$KEY_ID"
    -authenticationKeyIssuerID "$ISSUER_ID"
  )
fi
execute "${export_cmd[@]}"

step "Name the release asset"
note "HSCC-$VERSION.ipa"
if [ "$DRY_RUN" = "0" ]; then
  built_ipa="$(find "$EXPORT_DIR" -maxdepth 1 -type f -name '*.ipa' | head -1)"
  [ -n "$built_ipa" ] || { echo "error: export produced no .ipa under $EXPORT_DIR" >&2; exit 1; }
  cp "$built_ipa" "$IPA"

  # The manifest's bundle-version must equal the IPA's CFBundleVersion, and the
  # bundle id must match what the page says. Verify the artifact, not the intent.
  got_marketing="$(unzip -p "$IPA" 'Payload/*.app/Info.plist' | plutil -extract CFBundleShortVersionString raw -o - - 2>/dev/null || true)"
  got_version="$(unzip -p "$IPA" 'Payload/*.app/Info.plist' | plutil -extract CFBundleVersion raw -o - - 2>/dev/null || true)"
  got_bundle="$(unzip -p "$IPA" 'Payload/*.app/Info.plist' | plutil -extract CFBundleIdentifier raw -o - - 2>/dev/null || true)"
  [ "$got_marketing" = "$VERSION" ] || { echo "error: IPA CFBundleShortVersionString is '$got_marketing', expected $VERSION" >&2; exit 1; }
  [ "$got_version" = "$BUILD" ] || { echo "error: IPA CFBundleVersion is '$got_version', expected $BUILD" >&2; exit 1; }
  [ "$got_bundle" = "$BUNDLE_ID" ] || { echo "error: IPA bundle id is '$got_bundle', expected $BUNDLE_ID" >&2; exit 1; }
  note "verified IPA: $got_bundle $got_marketing ($got_version)"
else
  note "[dry-run] would copy the exported .ipa to $IPA"
fi

step "Publish the IPA and the GitHub Pages install site"
if [ "$DRY_RUN" = "1" ]; then
  note "[dry-run] would run:"
  note "  HSCC_BUNDLE_PREFIX=$PREFIX HSCC_BUILD_NUMBER=$BUILD \\"
  note "    $SCRIPT_DIR/publish_pages.sh $VERSION $IPA"
  note "  release tag: $TAG (never marked latest)"
  note "  install page: https://pom11.github.io/hscc/ios/"
  note "  manifest:     https://pom11.github.io/hscc/ios/manifest.plist"
else
  HSCC_BUNDLE_PREFIX="$PREFIX" HSCC_BUILD_NUMBER="$BUILD" \
    "$SCRIPT_DIR/publish_pages.sh" "$VERSION" "$IPA"
fi

echo ""
echo "DONE: HSCC $VERSION ($BUNDLE_ID)"
echo "Install page: https://pom11.github.io/hscc/ios/"
