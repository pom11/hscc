#!/bin/bash
# publish_macos.sh — publish /macos/ and the Sparkle 2 appcast on gh-pages.
#
# Inputs: <version> <build> <zip> <edSignature> [<release-notes.md>]
# Env:    HSCC_REPO (default pom11/hscc), HSCC_RELEASE_TAG (default apps-v<v>)
#         HSCC_PAGES_REMOTE (default origin — where gh-pages is pushed; a path
#                 works, the same knob scripts/publish_site.sh already honours)
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
# Where gh-pages lives — the same knob scripts/publish_site.sh reads, so one
# variable redirects the root page and both app subtrees. A path is legal, which
# is how a CI run or a mirror release publishes without rewriting the remote of
# the checkout. Default keeps today's behaviour: push to origin.
REMOTE="${HSCC_PAGES_REMOTE:-origin}"
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
TMP_BRANCH=""
cleanup() {
  git -C "$REPO_ROOT" worktree remove --force "$PAGES_DIR" >/dev/null 2>&1 \
    || rm -rf "$(dirname "${PAGES_DIR:-}")"
  # The orphan path below needs a branch to be unborn on; it is throwaway, so
  # it is deleted here rather than left behind in the shared repository.
  if [ -n "$TMP_BRANCH" ]; then
    git -C "$REPO_ROOT" branch -D "$TMP_BRANCH" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# Both git calls name $REMOTE and are pinned to $REPO_ROOT with -C, and they
# must stay that way. A bare `git ls-remote origin` / `git fetch origin` resolves
# the remote and writes FETCH_HEAD in whichever repo owns the CURRENT WORKING
# DIRECTORY, which is this repo only when the release happened to be run from
# inside it. From anywhere else the branch check answers about someone else's
# remote and the worktree below either cannot resolve FETCH_HEAD at all or,
# worse, resolves an unrelated FETCH_HEAD and pushes the wrong commit onto the
# branch that serves the install page. Naming $REMOTE rather than `origin` is the
# other half: which remote gets read is then the operator's choice, not a fact
# about the checkout.
if git -C "$REPO_ROOT" ls-remote --exit-code --heads "$REMOTE" gh-pages >/dev/null 2>&1; then
  git -C "$REPO_ROOT" fetch --quiet "$REMOTE" gh-pages
  # Detached on purpose: the push below is HEAD:gh-pages, so no local branch
  # named gh-pages is needed — and a `checkout -B gh-pages` would reset, or
  # fail on, a gh-pages branch that another worktree already has checked out.
  git -C "$REPO_ROOT" worktree add --quiet --detach "$PAGES_DIR" FETCH_HEAD
  echo "    reusing existing gh-pages (site files preserved)"
else
  git -C "$REPO_ROOT" worktree add --quiet --detach "$PAGES_DIR" HEAD
  # A unique name, because the repository can already hold a local gh-pages
  # branch that has nothing to do with this remote branch; `--orphan gh-pages`
  # would then die with "a branch named 'gh-pages' already exists".
  TMP_BRANCH="hscc-macos-publish-$$-${RANDOM}"
  git -C "$PAGES_DIR" checkout --quiet --orphan "$TMP_BRANCH"
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
  git -C "$PAGES_DIR" push --quiet "$REMOTE" HEAD:gh-pages
  echo "    pushed $(git -C "$PAGES_DIR" rev-parse --short HEAD) to gh-pages"
fi

step "Published"
echo "    install page: ${HSCC_PAGES_BASE:-https://pom11.github.io/hscc}/macos/"
echo "    appcast:      ${HSCC_PAGES_BASE:-https://pom11.github.io/hscc}/macos/appcast.xml"
echo "    notes:        ${HSCC_PAGES_BASE:-https://pom11.github.io/hscc}/macos/notes/${VERSION}.html"
echo "    GitHub may take a minute to build the Pages site."
