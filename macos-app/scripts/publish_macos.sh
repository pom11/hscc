#!/bin/bash
# publish_macos.sh — publish /macos/ and the Sparkle 2 appcast on gh-pages.
#
# Inputs: <version> <build> <zip> <edSignature> [<release-notes.md>]
# Env:    HSCC_REPO (default pom11/hscc), HSCC_RELEASE_TAG (default apps-v<v>)
#
# What lands where (branch gh-pages, only under /macos/):
#   macos/index.html        brew command + What's new
#   macos/notes/<v>.html    permanent release notes
#   macos/appcast.xml       Sparkle 2 appcast, one item per release, newest first
#
# The zip itself is never committed: the appcast enclosure points at the
# GitHub release asset, with its length and EdDSA signature.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MACOS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$MACOS_DIR/.." && pwd)"

VERSION="${1:-}"
BUILD="${2:-}"
ZIP="${3:-}"
SIGNATURE="${4:-}"
NOTES_FILE="${5:-}"
if [ -z "$VERSION" ] || [ -z "$BUILD" ] || [ -z "$ZIP" ] || [ -z "$SIGNATURE" ]; then
  echo "usage: $0 <version> <build> <zip> <edSignature> [<release-notes.md>]" >&2
  exit 2
fi
[ -f "$ZIP" ] || { echo "error: zip not found: $ZIP" >&2; exit 1; }

REPO="${HSCC_REPO:-pom11/hscc}"
TAG="${HSCC_RELEASE_TAG:-apps-v${VERSION}}"
ASSET="HSCC-Widgets-${VERSION}.zip"
ZIP_URL="https://github.com/${REPO}/releases/download/${TAG}/${ASSET}"
ZIP_LENGTH="$(wc -c < "$ZIP" | tr -d ' ')"

for tool in gh git python3; do
  command -v "$tool" >/dev/null 2>&1 || { echo "error: $tool not found" >&2; exit 1; }
done
gh auth status >/dev/null 2>&1 || { echo "error: gh is not authenticated. Run: gh auth login" >&2; exit 1; }

step() { echo ""; echo "==> $*"; }

step "Prepare the gh-pages worktree"
PAGES_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hscc-pages.XXXXXX")/gh-pages"
cleanup() {
  git -C "$REPO_ROOT" worktree remove --force "$PAGES_DIR" >/dev/null 2>&1 \
    || rm -rf "$(dirname "$PAGES_DIR")"
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
mkdir -p "$PAGES_DIR/macos/notes"
: > "$PAGES_DIR/.nojekyll"

step "Render /macos/ and update the Sparkle appcast"
if [ -n "$NOTES_FILE" ] && [ -f "$NOTES_FILE" ]; then
  python3 "$REPO_ROOT/scripts/apps_release.py" publish-macos \
    --pages-dir "$PAGES_DIR" --macos-dir "$MACOS_DIR" \
    --version "$VERSION" --build "$BUILD" \
    --zip-url "$ZIP_URL" --length "$ZIP_LENGTH" --signature "$SIGNATURE" \
    --notes-md "$NOTES_FILE"
else
  python3 "$REPO_ROOT/scripts/apps_release.py" publish-macos \
    --pages-dir "$PAGES_DIR" --macos-dir "$MACOS_DIR" \
    --version "$VERSION" --build "$BUILD" \
    --zip-url "$ZIP_URL" --length "$ZIP_LENGTH" --signature "$SIGNATURE"
fi

step "Commit and push gh-pages (site files only)"
git -C "$PAGES_DIR" add .nojekyll macos
if git -C "$PAGES_DIR" diff --cached --quiet; then
  echo "    no change to publish"
else
  git -C "$PAGES_DIR" -c user.name="HSCC release" \
    -c user.email="release@users.noreply.github.com" \
    commit --quiet -m "macos: publish HSCC ${VERSION}"
  git -C "$PAGES_DIR" push --quiet origin HEAD:gh-pages
  echo "    pushed $(git -C "$PAGES_DIR" rev-parse --short HEAD) to gh-pages"
fi

step "Published"
echo "    install page: ${HSCC_PAGES_BASE:-https://pom11.github.io/hscc}/macos/"
echo "    appcast:      ${HSCC_PAGES_BASE:-https://pom11.github.io/hscc}/macos/appcast.xml"
echo "    notes:        ${HSCC_PAGES_BASE:-https://pom11.github.io/hscc}/macos/notes/${VERSION}.html"
echo "    GitHub may take a minute to build the Pages site."
