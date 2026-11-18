#!/usr/bin/env bash
# publish_site.sh — publish the landing page at the gh-pages ROOT.
#
# gh-pages serves three things and this script owns exactly one of them:
#
#   /            index.html + site.css + assets/   (this script)
#   /ios/        the Ad Hoc OTA install page       (ios-app/scripts/publish_pages.sh)
#   /macos/      the Sparkle appcast + install page(macos-app/scripts/publish_macos.sh)
#
# The two app subtrees are published by the release lane and are the only way
# anyone installs the apps. A root publish that wiped them would take the
# install pages down without touching a line of app code, so this script is
# written so that it CANNOT:
#
#   1. it stages only its own root paths (`git add -- index.html site.css
#      assets .nojekyll`), never `-A`, never `git rm`, never `git clean`;
#   2. before it writes anything it snapshots every path and content hash under
#      the protected subtrees, and it re-checks that snapshot immediately
#      before committing — any difference aborts the publish with no commit;
#   3. it reuses an existing gh-pages branch as-is. The orphan-branch path
#      (which starts from an empty tree) runs only when the branch does not
#      exist at all, i.e. when there is nothing under /ios/ or /macos/ to lose.
#
# That guarantee is proven by scripts/tests/test_site_landing.py: it plants a
# marker under /ios/ and /macos/ in a throwaway remote, runs this script, and
# fails unless both markers survive and the root page exists.
#
# Env:  HSCC_REPO          (default pom11/hscc) — only used for the echo line
#       HSCC_PAGES_REMOTE  (default origin)      — where to push; a path works,
#                                                  which is what the test uses
#       HSCC_PAGES_BRANCH  (default gh-pages)
#       HSCC_SITE_DIR      (default <repo>/site) — what to publish
#       HSCC_DRY_RUN=1     prepare + guard + report, do not commit or push
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

REMOTE="${HSCC_PAGES_REMOTE:-origin}"
BRANCH="${HSCC_PAGES_BRANCH:-gh-pages}"
SITE_DIR="${HSCC_SITE_DIR:-$REPO_ROOT/site}"
REPO="${HSCC_REPO:-pom11/hscc}"
DRY_RUN="${HSCC_DRY_RUN:-0}"

# The subtrees this script must leave exactly as it found them.
PROTECTED=(ios macos)

# The only paths this script is allowed to write at the root.
ROOT_FILES=(index.html site.css .nojekyll)
ROOT_DIRS=(assets)

step() { echo ""; echo "==> $*"; }

for tool in git shasum; do
  command -v "$tool" >/dev/null 2>&1 || { echo "error: $tool not found" >&2; exit 1; }
done
[ -f "$SITE_DIR/index.html" ] || { echo "error: $SITE_DIR/index.html not found" >&2; exit 1; }
[ -f "$SITE_DIR/site.css" ] || { echo "error: $SITE_DIR/site.css not found" >&2; exit 1; }

# A root page that loads the app subtrees as *assets* would break the moment
# the release lane republishes them, so refuse that. A plain link to /ios/ or
# /macos/ is different: pointing visitors at the install pages is the point.
for d in "${PROTECTED[@]}"; do
  if grep -Eq "(src|srcset)=\"[^\"]*/?$d/" "$SITE_DIR/index.html"; then
    echo "error: $SITE_DIR/index.html loads an asset out of /$d/, a subtree this" >&2
    echo "       script does not own. The page must be self-contained." >&2
    exit 1
  fi
done

step "Prepare the gh-pages worktree from $REMOTE/$BRANCH"
PAGES_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hscc-site.XXXXXX")/gh-pages"
TMP_BRANCH=""
cleanup() {
  git -C "$REPO_ROOT" worktree remove --force "$PAGES_DIR" >/dev/null 2>&1 \
    || rm -rf "$(dirname "${PAGES_DIR:-}")"
  # The orphan path needs a branch to be unborn on; it is throwaway, so it is
  # deleted here rather than left behind in the shared repository.
  if [ -n "$TMP_BRANCH" ]; then
    git -C "$REPO_ROOT" branch -D "$TMP_BRANCH" >/dev/null 2>&1 || true
  fi
  rm -f "${SNAP:-}" "${SNAP_NOW:-}" || true
}
trap cleanup EXIT

if git -C "$REPO_ROOT" ls-remote --exit-code --heads "$REMOTE" "$BRANCH" >/dev/null 2>&1; then
  git -C "$REPO_ROOT" fetch --quiet "$REMOTE" "$BRANCH"
  # Detached on purpose: publishing pushes HEAD:$BRANCH, so nothing needs a
  # local branch named $BRANCH, and a local branch that another worktree has
  # checked out cannot make this run fail.
  git -C "$REPO_ROOT" worktree add --quiet --detach "$PAGES_DIR" FETCH_HEAD
  echo "    reusing existing $BRANCH (every path you did not publish is intact)"
else
  echo "    $REMOTE has no $BRANCH branch — creating it with the site files only"
  git -C "$REPO_ROOT" worktree add --quiet --detach "$PAGES_DIR" HEAD
  # A unique name, because a repository can already hold a local gh-pages
  # branch that has nothing to do with this remote branch.
  TMP_BRANCH="hscc-site-publish-$$-${RANDOM}"
  git -C "$PAGES_DIR" checkout --quiet --orphan "$TMP_BRANCH"
  git -C "$PAGES_DIR" rm -rf . >/dev/null
fi

# ── the guard, part 1: remember exactly what the app subtrees hold ──────────
SNAP="$(mktemp "${TMPDIR:-/tmp}/hscc-protected-snapshot.XXXXXX")"
snapshot() {
  : > "$SNAP"
  local d
  for d in "${PROTECTED[@]}"; do
    [ -d "$PAGES_DIR/$d" ] || continue
    # Paths first, then the content hash of every file: a deleted directory
    # with no files left is still a change to what the app lane published.
    ( cd "$PAGES_DIR" && find "$d" | LC_ALL=C sort ) >> "$SNAP"
    ( cd "$PAGES_DIR" && find "$d" -type f -print0 | LC_ALL=C sort -z \
        | xargs -0 -r shasum -a 256 ) >> "$SNAP"
  done
}
restore_guard() {
  echo "error: the root publish changed a protected subtree — nothing was" >&2
  echo "       committed. The change is below (left = before, right = now):" >&2
  diff "$SNAP" "$SNAP.now" >&2 || true
  exit 1
}
snapshot

step "Write the root page"
mkdir -p "$PAGES_DIR/assets"
: > "$PAGES_DIR/.nojekyll"          # Pages must not run Jekyll over the page
install -m 644 "$SITE_DIR/index.html" "$PAGES_DIR/index.html"
install -m 644 "$SITE_DIR/site.css"   "$PAGES_DIR/site.css"
if [ -d "$SITE_DIR/assets" ]; then
  # The icon is copied from site/, which keeps a byte-for-byte copy of the app
  # icon — the page never redraws the mark.
  find "$SITE_DIR/assets" -maxdepth 1 -type f -exec install -m 644 {} "$PAGES_DIR/assets/" \;
fi

# ── the guard, part 2: prove the app subtrees are untouched ─────────────────
snapshot_now() {
  local snap_now="$1" d
  : > "$snap_now"
  for d in "${PROTECTED[@]}"; do
    [ -d "$PAGES_DIR/$d" ] || continue
    ( cd "$PAGES_DIR" && find "$d" | LC_ALL=C sort ) >> "$snap_now"
    ( cd "$PAGES_DIR" && find "$d" -type f -print0 | LC_ALL=C sort -z \
        | xargs -0 -r shasum -a 256 ) >> "$snap_now"
  done
}
SNAP_NOW="$(mktemp "${TMPDIR:-/tmp}/hscc-protected-now.XXXXXX")"
snapshot_now "$SNAP_NOW"
if ! cmp -s "$SNAP" "$SNAP_NOW"; then
  restore_guard
fi
echo "    protected subtrees unchanged: ${PROTECTED[*]/%/\/}"

step "Commit and push $BRANCH (root paths only)"
git -C "$PAGES_DIR" add -- "${ROOT_FILES[@]}" "${ROOT_DIRS[@]}"
if git -C "$PAGES_DIR" diff --cached --quiet; then
  echo "    no change to publish"
elif [ "$DRY_RUN" = "1" ]; then
  echo "    HSCC_DRY_RUN=1: staged but not committed"
else
  git -C "$PAGES_DIR" -c user.name="HSCC site" \
    -c user.email="site@users.noreply.github.com" \
    commit --quiet -m "site: publish the landing page at the gh-pages root"
  git -C "$PAGES_DIR" push --quiet "$REMOTE" "HEAD:$BRANCH"
  echo "    pushed $(git -C "$PAGES_DIR" rev-parse --short HEAD) to $BRANCH"
fi

step "Published"
echo "    https://${REPO%%/*}.github.io/${REPO##*/}/"
echo "    untouched by this run: /ios/ and /macos/ (guarded above)"
