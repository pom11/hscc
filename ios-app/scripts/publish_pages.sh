#!/bin/bash
# publish_pages.sh — publish an Ad Hoc IPA and the OTA install page.
#
# Inputs: <version> <path-to-ipa>
# Env:    HSCC_BUNDLE_PREFIX (default com.hscc), HSCC_REPO (default pom11/hscc)
#
# What lands where:
#   * GitHub release `ios-v<version>` on pom11/hscc, asset HSCC-<version>.ipa.
#     It is explicitly NOT the repository's "latest" release, so it never
#     shadows the CLI releases.
#   * branch `gh-pages`, only under /ios/ (plus a root .nojekyll):
#       ios/index.html          install instructions + Install button
#       ios/manifest.plist      Apple OTA manifest for the CURRENT build
#       ios/manifests/<ver>.plist   versioned archive of every manifest
#       ios/icons/icon-{57,512,1024}.png
#     An existing gh-pages branch is preserved and only those files are
#     committed. A missing branch is created as an orphan with only the site.
#   * GitHub Pages enabled from gh-pages via `gh api`, idempotently.
#
# The IPA itself is never committed to the branch; the manifest points at the
# release asset over HTTPS, which is what iOS downloads.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IOS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$IOS_DIR/.." && pwd)"

VERSION="${1:-}"
IPA="${2:-}"
if [ -z "$VERSION" ] || [ -z "$IPA" ]; then
  echo "usage: $0 <version> <path-to-ipa>" >&2
  exit 2
fi
if ! printf '%s' "$VERSION" | grep -Eq '^[0-9]+(\.[0-9]+){1,2}$'; then
  echo "error: version must look like 1.2.3: $VERSION" >&2
  exit 2
fi
[ -f "$IPA" ] || { echo "error: IPA not found: $IPA" >&2; exit 1; }

REPO="${HSCC_REPO:-pom11/hscc}"
OWNER="${REPO%%/*}"
NAME="${REPO##*/}"
PAGES_BASE="https://${OWNER}.github.io/${NAME}"
PREFIX="${HSCC_BUNDLE_PREFIX:-com.hscc}"
BUNDLE_ID="${HSCC_BUNDLE_ID:-${PREFIX}.ios}"
TAG="ios-v${VERSION}"
ASSET="HSCC-${VERSION}.ipa"
IPA_URL="https://github.com/${REPO}/releases/download/${TAG}/${ASSET}"
ICON57_URL="${PAGES_BASE}/ios/icons/icon-57.png"
ICON512_URL="${PAGES_BASE}/ios/icons/icon-512.png"
MANIFEST_URL="${PAGES_BASE}/ios/manifest.plist"

for tool in gh git python3 sips unzip; do
  command -v "$tool" >/dev/null 2>&1 || { echo "error: $tool not found" >&2; exit 1; }
done
gh auth status >/dev/null 2>&1 || { echo "error: gh is not authenticated. Run: gh auth login" >&2; exit 1; }

step() { echo ""; echo "==> $*"; }

step "Upload the IPA to GitHub release $TAG (not latest)"
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  gh release upload "$TAG" "$IPA#$ASSET" --repo "$REPO" --clobber
  gh release edit "$TAG" --repo "$REPO" --latest=false
else
  gh release create "$TAG" "$IPA#$ASSET" \
    --repo "$REPO" \
    --title "HSCC iOS $VERSION" \
    --notes "Ad Hoc iOS build $VERSION. Install from ${PAGES_BASE}/ios/ on a registered iPhone (Safari)." \
    --latest=false
fi
echo "    asset: $IPA_URL"

step "Prepare the gh-pages worktree"
PAGES_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hscc-pages.XXXXXX")/gh-pages"
cleanup() {
  git -C "$REPO_ROOT" worktree remove --force "$PAGES_DIR" >/dev/null 2>&1 || rm -rf "$(dirname "$PAGES_DIR")"
}
trap cleanup EXIT

if git ls-remote --exit-code --heads origin gh-pages >/dev/null 2>&1; then
  git fetch --quiet origin gh-pages
  git -C "$REPO_ROOT" worktree add --quiet --detach "$PAGES_DIR" FETCH_HEAD
  git -C "$PAGES_DIR" checkout --quiet -B gh-pages
  echo "    reusing existing gh-pages (site files preserved)"
else
  git -C "$REPO_ROOT" worktree add --quiet --detach "$PAGES_DIR" HEAD
  git -C "$PAGES_DIR" checkout --quiet --orphan gh-pages
  git -C "$PAGES_DIR" rm -rf . >/dev/null
  echo "    creating orphan gh-pages (site files only)"
fi
# Pages ignores files/dirs starting with an underscore; this is the standard
# opt-out for a hand-built site.
mkdir -p "$PAGES_DIR/ios/icons" "$PAGES_DIR/ios/manifests"
: > "$PAGES_DIR/.nojekyll"

step "Generate the OTA manifest and icons"
SOURCE_ICON="$IOS_DIR/Assets.xcassets/AppIcon.appiconset/AppIcon.png"
[ -f "$SOURCE_ICON" ] || { echo "error: missing $SOURCE_ICON (run scripts/make_appicon.sh)" >&2; exit 1; }
cp "$SOURCE_ICON" "$PAGES_DIR/ios/icons/icon-1024.png"
sips -z 57 57 "$SOURCE_ICON" --out "$PAGES_DIR/ios/icons/icon-57.png" >/dev/null
sips -z 512 512 "$SOURCE_ICON" --out "$PAGES_DIR/ios/icons/icon-512.png" >/dev/null

python3 "$SCRIPT_DIR/make_manifest.py" \
  --bundle-id "$BUNDLE_ID" \
  --version "$VERSION" \
  --ipa-url "$IPA_URL" \
  --icon-57-url "$ICON57_URL" \
  --icon-512-url "$ICON512_URL" \
  --title "HSCC" \
  --out "$PAGES_DIR/ios/manifest.plist"
# Keep every published manifest so an older build can still be audited.
cp "$PAGES_DIR/ios/manifest.plist" "$PAGES_DIR/ios/manifests/${VERSION}.plist"

step "Render the install page"
python3 - "$IOS_DIR/site/index.html" "$PAGES_DIR/ios/index.html" "$VERSION" <<'PY'
import sys
from pathlib import Path

template, out, version = sys.argv[1], sys.argv[2], sys.argv[3]
text = Path(template).read_text(encoding="utf-8")
text = text.replace("__HSCC_VERSION__", version)
Path(out).write_text(text, encoding="utf-8")
PY

step "Commit and push gh-pages (site files only)"
git -C "$PAGES_DIR" add .nojekyll ios
if git -C "$PAGES_DIR" diff --cached --quiet; then
  echo "    no change to publish"
else
  git -C "$PAGES_DIR" -c user.name="HSCC release" \
    -c user.email="release@users.noreply.github.com" \
    commit --quiet -m "ios: publish HSCC ${VERSION}"
  git -C "$PAGES_DIR" push --quiet origin HEAD:gh-pages
  echo "    pushed $(git -C "$PAGES_DIR" rev-parse --short HEAD) to gh-pages"
fi

step "Enable GitHub Pages from gh-pages (idempotent)"
PAGES_API="repos/${REPO}/pages"
PAGES_BODY='{"build_type":"legacy","source":{"branch":"gh-pages","path":"/"}}'
if gh api "$PAGES_API" >/dev/null 2>&1; then
  printf '%s' "$PAGES_BODY" | gh api --method PUT "$PAGES_API" --input - >/dev/null
  echo "    Pages already enabled; source set to gh-pages"
else
  printf '%s' '{"source":{"branch":"gh-pages","path":"/"}}' \
    | gh api --method POST "$PAGES_API" --input - >/dev/null
  echo "    Pages enabled from gh-pages"
fi

step "Published"
echo "    install page: $PAGES_BASE/ios/"
echo "    manifest:     $MANIFEST_URL"
echo "    release:      https://github.com/${REPO}/releases/tag/${TAG}"
echo "    GitHub may take a minute to build the Pages site."
