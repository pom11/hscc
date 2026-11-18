#!/bin/bash
# release_apps.sh — the ONE release command for the iOS + macOS HSCC apps.
#
#   scripts/release_apps.sh <version> [--ios-only|--mac-only] [--dry-run]
#
# What it does:
#   1. refuses a dirty tree and a version that is not greater than the last
#      released app version (read from apps-v* tags; the pre-migration
#      ios-v* / macos-widgets-v* tags are the 1.0.0 baseline);
#   2. writes CFBundleShortVersionString=<version> and a monotonic
#      CFBundleVersion (apps/BUILD_NUMBER + 1) into BOTH project.yml files;
#   3. commits "apps: release <version>" and tags apps-v<version>;
#   4. renders release notes from the changelog.d fragments that touched
#      ios-app/ or macos-app/ since the previous app tag;
#   5. runs the two platform scripts with that one tag and those notes:
#        ios-app/scripts/release_adhoc.sh   → HSCC-<version>.ipa
#        macos-app/scripts/release.sh       → HSCC-Widgets-<version>.zip
#      Both assets land on ONE GitHub release, apps-v<version>, NEVER "latest"
#      (CLI releases own latest). The old per-platform tags still work when a
#      platform script is run standalone with HSCC_RELEASE_TAG …
#   6. each platform publishes its Pages feed: /ios/version.json + notes, and
#      /macos/appcast.xml + notes; the cask is rendered from the local zip.
#
# --dry-run validates the version/build, renders the notes and stages BOTH
# Pages sites / feeds in a temp directory, then prints the commit/tag and the
# platform commands. It never builds, signs, notarizes, uploads or commits.
#
# Prerequisites: Xcode signed in (Settings → Accounts), notary profile
# hscc-notary, the Sparkle EdDSA key in the login keychain, and the target
# iPhones registered. See RELEASING.md for setup, adding a device and rollback.
#
# Exit codes: 0 success; 2 usage/preflight; anything else is the failed
# platform step (the tag exists; RELEASING.md documents rollback).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
IOS_DIR="$REPO_ROOT/ios-app"
MACOS_DIR="$REPO_ROOT/macos-app"
BUILD_FILE="$REPO_ROOT/apps/BUILD_NUMBER"
BUILD_NUMBER_SCRIPT="$SCRIPT_DIR/apps_release.py"

VERSION=""
IOS_ONLY=0
MAC_ONLY=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --ios-only) IOS_ONLY=1 ;;
    --mac-only) MAC_ONLY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '1,42p' "${BASH_SOURCE[0]}"; exit 0 ;;
    -*) echo "error: unknown option: $arg" >&2; exit 2 ;;
    *)
      if [ -n "$VERSION" ]; then
        echo "error: more than one version given: $VERSION and $arg" >&2
        exit 2
      fi
      VERSION="$arg" ;;
  esac
done

if [ -z "$VERSION" ]; then
  echo "usage: $0 <version> [--ios-only|--mac-only] [--dry-run]" >&2
  echo "       e.g. $0 1.1.0 --dry-run" >&2
  exit 2
fi
if [ "$IOS_ONLY" = "1" ] && [ "$MAC_ONLY" = "1" ]; then
  echo "error: --ios-only and --mac-only are mutually exclusive" >&2
  exit 2
fi
if ! printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "error: version must look like 1.1.0 (digits and dots, no leading 'v'): $VERSION" >&2
  exit 2
fi

step() { echo ""; echo "==> $*"; }
note() { echo "    $*"; }
fail() { echo "error: $*" >&2; exit 2; }

cd "$REPO_ROOT"

# ── preflight ────────────────────────────────────────────────────────────────

step "Preflight"
if [ -n "$(git status --porcelain)" ]; then
  git status --short >&2
  fail "the tree is dirty — commit or stash everything before releasing"
fi
[ -f "$BUILD_FILE" ] || fail "missing $BUILD_FILE (one integer build number)"
PREV_BUILD="$(cat "$BUILD_FILE")"
printf '%s' "$PREV_BUILD" | grep -Eq '^[0-9]+$' \
  || fail "$BUILD_FILE must contain one integer, got '$PREV_BUILD'"
BUILD=$((PREV_BUILD + 1))

PREV_TAG="$(git tag -l --sort=-v:refname 'apps-v*' 'ios-v*' 'macos-widgets-v*' | head -1)"
LAST_VERSION="$(python3 "$BUILD_NUMBER_SCRIPT" last-released)"
python3 - "$VERSION" "$LAST_VERSION" <<'PY'
import sys

def key(v):
    return tuple(int(p) for p in v.split("."))

current, previous = sys.argv[1], sys.argv[2]
if key(current) <= key(previous):
    raise SystemExit(
        f"error: {current} is not greater than the last released app version "
        f"{previous}")
PY

note "version:      $VERSION (build $BUILD, previous build $PREV_BUILD)"
note "last release: $LAST_VERSION${PREV_TAG:+ ($PREV_TAG)}"
note "tag:          apps-v$VERSION (never 'latest')"
note "platforms:    $([ "$MAC_ONLY" = 1 ] && echo macOS || { [ "$IOS_ONLY" = 1 ] && echo iOS || echo "iOS + macOS"; })"
note "iOS prefix:   ${HSCC_IOS_BUNDLE_PREFIX:-ro.parsec.hscc} (shipped identity)"
note "macOS prefix: ${HSCC_MACOS_BUNDLE_PREFIX:-com.hscc}"
[ "$DRY_RUN" = "1" ] && note "mode:         DRY RUN — nothing built, signed, uploaded or committed"

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hscc-apps-release.XXXXXX")"
cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT
NOTES_MD="$WORK_DIR/notes.md"

step "Render release notes from changelog.d/"
if [ -n "$PREV_TAG" ]; then
  python3 "$BUILD_NUMBER_SCRIPT" notes --version "$VERSION" \
    --since "$PREV_TAG" --out "$NOTES_MD"
else
  python3 "$BUILD_NUMBER_SCRIPT" notes --version "$VERSION" --out "$NOTES_MD"
fi
sed -n '1,12p' "$NOTES_MD" | sed 's/^/    /'

# ── dry run ──────────────────────────────────────────────────────────────────

if [ "$DRY_RUN" = "1" ]; then
  step "Stage both Pages sites and feeds (temp directory)"
  mkdir -p "$WORK_DIR/pages"
  IPA_URL="https://github.com/pom11/hscc/releases/download/apps-v${VERSION}/HSCC-${VERSION}.ipa"
  ZIP_URL="https://github.com/pom11/hscc/releases/download/apps-v${VERSION}/HSCC-Widgets-${VERSION}.zip"
  if [ "$MAC_ONLY" = "0" ]; then
    python3 "$BUILD_NUMBER_SCRIPT" publish-ios \
      --pages-dir "$WORK_DIR/pages" --ios-dir "$IOS_DIR" \
      --version "$VERSION" --build "$BUILD" --notes-md "$NOTES_MD" \
      --bundle-id "${HSCC_IOS_BUNDLE_PREFIX:-ro.parsec.hscc}.ios" \
      --ipa-url "$IPA_URL"
  fi
  if [ "$IOS_ONLY" = "0" ]; then
    python3 "$BUILD_NUMBER_SCRIPT" publish-macos \
      --pages-dir "$WORK_DIR/pages" --macos-dir "$MACOS_DIR" \
      --version "$VERSION" --build "$BUILD" --notes-md "$NOTES_MD" \
      --zip-url "$ZIP_URL" --length 123456 \
      --signature "DRY-RUN-SIGNATURE-NOT-A-REAL-RELEASE"
  fi
  note "validated XML/JSON under $WORK_DIR/pages (discarded on exit)"

  step "Planned version bump + commit + tag"
  note "ios-app/project.yml:   MARKETING_VERSION=\"$VERSION\" CURRENT_PROJECT_VERSION=\"$BUILD\""
  note "macos-app/project.yml: MARKETING_VERSION=\"$VERSION\" CURRENT_PROJECT_VERSION=\"$BUILD\""
  note "apps/BUILD_NUMBER:     $BUILD"
  note "git commit -m \"apps: release $VERSION\""
  note "git tag apps-v$VERSION"

  step "Planned platform commands"
  if [ "$MAC_ONLY" = "0" ]; then
    note "HSCC_RELEASE_TAG=apps-v$VERSION HSCC_BUILD_NUMBER=$BUILD \\"
    note "HSCC_RELEASE_NOTES_FILE=<notes> HSCC_BUNDLE_PREFIX=${HSCC_IOS_BUNDLE_PREFIX:-ro.parsec.hscc} \\"
    note "  ios-app/scripts/release_adhoc.sh $VERSION"
  fi
  if [ "$IOS_ONLY" = "0" ]; then
    note "HSCC_RELEASE_TAG=apps-v$VERSION HSCC_BUILD_NUMBER=$BUILD \\"
    note "HSCC_RELEASE_NOTES_FILE=<notes> HSCC_BUNDLE_PREFIX=${HSCC_MACOS_BUNDLE_PREFIX:-com.hscc} \\"
    note "  macos-app/scripts/release.sh --push-cask $VERSION"
  fi
  echo ""
  echo "DONE (dry run): $VERSION (build $BUILD) is release-ready; nothing was changed."
  exit 0
fi

# ── bump, commit, tag ────────────────────────────────────────────────────────

step "Bump both project.yml files and apps/BUILD_NUMBER"
python3 "$BUILD_NUMBER_SCRIPT" set-project-version \
  --project "$IOS_DIR/project.yml" --version "$VERSION" --build "$BUILD"
python3 "$BUILD_NUMBER_SCRIPT" set-project-version \
  --project "$MACOS_DIR/project.yml" --version "$VERSION" --build "$BUILD"
printf '%s\n' "$BUILD" > "$BUILD_FILE"
git add "$IOS_DIR/project.yml" "$MACOS_DIR/project.yml" "$BUILD_FILE"
git commit -m "apps: release $VERSION"
git tag "apps-v$VERSION"

step "Push the release commit and tag"
git push origin HEAD
git push origin "apps-v$VERSION"

export HSCC_RELEASE_TAG="apps-v$VERSION"
export HSCC_BUILD_NUMBER="$BUILD"
export HSCC_RELEASE_NOTES_FILE="$NOTES_MD"

# ── platform releases ────────────────────────────────────────────────────────

if [ "$MAC_ONLY" = "0" ]; then
  step "iOS — Ad Hoc IPA + install page + version.json"
  HSCC_BUNDLE_PREFIX="${HSCC_IOS_BUNDLE_PREFIX:-ro.parsec.hscc}" \
    "$IOS_DIR/scripts/release_adhoc.sh" "$VERSION"
fi

if [ "$IOS_ONLY" = "0" ]; then
  step "macOS — Developer ID + notarize + zip + appcast + cask"
  HSCC_BUNDLE_PREFIX="${HSCC_MACOS_BUNDLE_PREFIX:-com.hscc}" \
    "$MACOS_DIR/scripts/release.sh" --push-cask "$VERSION"
fi

echo ""
echo "DONE: HSCC apps $VERSION (build $BUILD)"
echo "Release: https://github.com/pom11/hscc/releases/tag/apps-v$VERSION"
echo "iOS:     https://pom11.github.io/hscc/ios/"
echo "macOS:   https://pom11.github.io/hscc/macos/"
