#!/bin/bash
# plist_drift_check.sh — the TRACKED ios-app Info.plists must be byte-identical
# to what project.yml generates.
#
# Why this exists. xcodegen does not merely read project.yml: every target's
# 'info:' block names a tracked file and rewrites it wholesale —
# project.yml:355 writes Sources/Info.plist, :527 writes
# Sources/HSCCWidgets/Info.plist, :605 writes
# Sources/HSCCLiveActivityMonitor/Info.plist. release_adhoc.sh:217 runs that
# generate as release step 1 and the release commit never carries the result,
# so the tracked copies drift from the source of truth in silence. Three hunks
# accumulated across four releases before anyone looked: BGTaskScheduler
# PermittedIdentifiers still listed three retired ids, Sources/Info.plist was
# missing the whole CarPlay UIApplicationSceneManifest that every shipped
# archive carried, and the Live Activity display name disagreed with project.yml.
# project.yml:394-396 already documents the hazard in prose — a key hand-added
# to Sources/Info.plist "survives only until the next generate.sh run, then
# vanishes silently". Prose is not a gate. This is.
#
# Four things the shape of this gate buys, each of which is a bug it prevents:
#
#   1. The plist set is DERIVED from git, never transcribed, so a fourth target's
#      plist is compared the day it lands instead of quietly joining the set of
#      files nobody checks.
#   2. Nothing is ever generated into the real tree. ios-app/ is copied into a
#      mktemp -d directory (minus the generated HSCC.xcodeproj) and xcodegen runs
#      there. An in-place generate that is interrupted leaves tracked plists
#      dirty for the next build or the next worker to trip over.
#   3. The copied plists are DELETED before the generate and must come back.
#      Without that step the comparison could pass by comparing a file against a
#      copy of itself: a plist project.yml stopped writing would read as
#      perfectly in sync, which is the one false green this gate exists to make
#      impossible.
#   4. The generate runs with HSCC_DEVELOPMENT_TEAM and HSCC_BUNDLE_PREFIX unset
#      (measured on a2088f2d: the plists are byte-identical either way), so the
#      gate needs no signing identity, no bundle prefix, and a laptop and a CI
#      runner agree on the answer.
#
# Missing tooling FAILS this gate. The workflow's "::notice:: + exit 0" skip
# idiom is deliberately not copied here: an announced skip is exactly how this
# drift survived four releases. app-build-checks.yml therefore installs xcodegen
# in its own step before the glob leg that runs this file.
#
# Usage:  bash ios-app/scripts/plist_drift_check.sh    # from the repo root
#         scripts/plist_drift_check.sh                 # or from ios-app/
# Exit:   0 — every tracked plist matches project.yml
#         1 — any drift, or the comparison could not be made (fail closed)

set -uo pipefail

# Resolved BEFORE anything else so the gate runs from any cwd: ${BASH_SOURCE[0]}
# is the path the caller typed, not where this file lives.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SELF_DIR/../.." && pwd)"
IOS_DIR="$REPO_ROOT/ios-app"

# --- fail closed on missing tooling ---------------------------------------
# No skip path, no notice, no exit 0: a machine that cannot run the generator
# cannot certify that the tracked plists match it.
if ! command -v xcodegen >/dev/null 2>&1; then
  echo "error: xcodegen not found. Install it with: brew install xcodegen" >&2
  echo "PLIST DRIFT CHECK FAILED — the gate cannot run without the generator whose output it audits" >&2
  exit 1
fi
if [ ! -f "$IOS_DIR/project.yml" ]; then
  echo "error: $IOS_DIR/project.yml is missing — run this gate from a repo checkout, do not copy it out" >&2
  echo "PLIST DRIFT CHECK FAILED" >&2
  exit 1
fi

WORK=$(mktemp -d) || {
  echo "error: mktemp -d failed, so there is nowhere to generate a scratch copy" >&2
  echo "PLIST DRIFT CHECK FAILED" >&2
  exit 1
}
cleanup() {
  # The scratch dir is the only thing this gate ever deletes, and only when it
  # is a non-empty absolute path this run created and can still see.
  if [ -n "$WORK" ] && [ "${WORK#/}" != "$WORK" ] && [ "$WORK" != "/" ] && [ -d "$WORK" ]; then
    rm -rf -- "$WORK"
  fi
  return 0
}
trap cleanup EXIT INT TERM

# --- the audited set: tracked plists, derived from git ---------------------
# --full-name so one variable holds a repo-root-relative path that addresses the
# tracked tree as "$REPO_ROOT/$rel" and the scratch copy as "$WORK/$rel".
TRACKED=$( (cd "$REPO_ROOT" && git ls-files --full-name -- 'ios-app' 2>/dev/null) | grep '\.plist$' )
if [ -z "$TRACKED" ]; then
  echo "error: git reports no tracked .plist under ios-app/." >&2
  echo "       Either this is not a git checkout — the gate measures tracked state against" >&2
  echo "       generated state, so it needs one — or the plists xcodegen writes have been" >&2
  echo "       untracked, which is the same drift one step worse and needs fixing by hand." >&2
  echo "PLIST DRIFT CHECK FAILED" >&2
  exit 1
fi

# Printed on every run because a generator version is the one input this gate
# cannot pin: if a brew bump changes how xcodegen formats a plist, the log has
# to say which generator was asked.
echo "generator: $(xcodegen --version 2>/dev/null || echo unknown)"
echo "auditing:  $(printf '%s\n' "$TRACKED" | grep -c .) tracked plist(s) under ios-app/"

# --- copy ios-app out; the tracked tree is never written -------------------
mkdir -p "$WORK/ios-app" || { echo "error: could not create the scratch copy" >&2; exit 1; }
# tar rather than cp so the generated project can be left behind: it is an
# artifact (.gitignore:69), and feeding a stale one back to the generator would
# make the gate measure a project file instead of project.yml.
(cd "$IOS_DIR" && tar -cf - --exclude 'HSCC.xcodeproj' .) \
  | (cd "$WORK/ios-app" && tar -xf -) \
  || {
    echo "error: could not copy ios-app/ into $WORK" >&2
    echo "PLIST DRIFT CHECK FAILED" >&2
    exit 1
  }

# Delete each copied plist so the generate has to put it back (see header 3).
while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  if [ -f "$WORK/$rel" ]; then
    rm -f -- "$WORK/$rel" || {
      echo "error: could not remove the scratch copy of $rel" >&2
      echo "PLIST DRIFT CHECK FAILED" >&2
      exit 1
    }
  fi
done <<< "$TRACKED"

# --- generate, off in the copy, with the two irrelevant variables unset ----
echo "generating from project.yml in the scratch copy (tracked tree untouched)"
GENLOG="$WORK/generate.log"
(cd "$WORK/ios-app" \
  && env -u HSCC_DEVELOPMENT_TEAM -u HSCC_BUNDLE_PREFIX xcodegen generate --spec project.yml) \
  > "$GENLOG" 2>&1
gen_rc=$?
if [ "$gen_rc" -ne 0 ]; then
  echo "error: xcodegen generate exited $gen_rc in the scratch copy:" >&2
  sed -e 's/^/    /' "$GENLOG" >&2
  echo "PLIST DRIFT CHECK FAILED — the generator did not run, so nothing was compared" >&2
  exit 1
fi

# --- compare --------------------------------------------------------------
drift=0
not_generated=0
untracked_generated=0

while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  if [ ! -f "$WORK/$rel" ]; then
    not_generated=$((not_generated + 1))
    echo "DRIFT: $rel is tracked but project.yml no longer generates it."
    echo "       Nothing writes that file any more, so it is hand-maintained — and"
    echo "       project.yml:394-396 is the record of where hand-maintained plists end up."
    continue
  fi
  if [ ! -f "$REPO_ROOT/$rel" ]; then
    drift=$((drift + 1))
    echo "DRIFT: $rel is tracked but absent from the working tree; project.yml generates it."
    continue
  fi
  if ! cmp -s "$REPO_ROOT/$rel" "$WORK/$rel"; then
    drift=$((drift + 1))
    echo "DRIFT: $rel is not what project.yml generates"
    diff -u --label "tracked: $rel" \
            --label "generated from project.yml: $rel" \
            "$REPO_ROOT/$rel" "$WORK/$rel"
  fi
done <<< "$TRACKED"

# A plist project.yml writes that git does not track has no tracked copy to
# drift FROM, which is not the same as being in sync: nothing in a diff of the
# repo ever shows its contents, so it is reported rather than silently skipped.
# The scan is every .plist, not every file named Info.plist: an info: block can
# be retargeted at a different leaf name, and a scan keyed on today's names
# would watch the old name and miss the new one (proved by leg E of the negative
# run, which is exactly how this line got widened).
while IFS= read -r gen; do
  [ -n "$gen" ] || continue
  rel="ios-app/${gen#./}"
  if ! grep -Fxq -- "$rel" <<< "$TRACKED"; then
    untracked_generated=$((untracked_generated + 1))
    echo "DRIFT: $rel is generated by project.yml but is not tracked by git — track it, or stop generating it."
  fi
done <<< "$( (cd "$WORK/ios-app" && find . -type f -name '*.plist' ! -path './HSCC.xcodeproj/*') | LC_ALL=C sort )"

echo ""
if [ "$drift" -eq 0 ] && [ "$not_generated" -eq 0 ] && [ "$untracked_generated" -eq 0 ]; then
  names=""
  while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    names="${names:+$names, }$rel"
  done <<< "$TRACKED"
  echo "ok: every tracked Info.plist is byte-identical to what project.yml generates ($names)"
  echo "PLIST DRIFT CHECK PASSED"
  exit 0
fi

echo "FIX: bash ios-app/scripts/generate.sh   — then commit the regenerated Info.plists with the change that caused it."
echo "PLIST DRIFT CHECK FAILED — $drift drifted, $not_generated no longer generated, $untracked_generated generated but untracked"
exit 1
