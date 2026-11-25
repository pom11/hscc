#!/usr/bin/env bash
# Decide whether app-build-checks must compile the Apple clients (t_31679ad1).
#
# Runs in the workflow's cheap `changes` job and writes `apps=true|false` plus a
# `reason=` line to $GITHUB_OUTPUT.
#
# CONTRACT:
#   push                            -> true (the backstop: main is always compiled)
#   pull_request, Apple input hit   -> true
#   pull_request, Python-only diff  -> false
#   no readable diff base / failure -> true (fail closed: compile rather than skip
#                                      a build that might have been required)
#
# The touched-path list is deliberately wider than "Swift sources": the compile
# job also runs project.yml, asset catalogs and every app script, and one of those
# scripts greps a file outside the app trees (scripts/release_apps.sh, read by
# ios-app/scripts/carplay_check.sh). Widen when in doubt — a skipped compile on a
# touched input is a silent red, a needless compile is only runner minutes.
set -uo pipefail

REPO="${DECIDE_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
OUT="${GITHUB_OUTPUT:-}"
EVENT="${EVENT:-}"
BASE_SHA="${BASE_SHA:-}"
PR_HEAD_SHA="${PR_HEAD_SHA:-}"

if [ -z "$OUT" ]; then
  echo "decide_app_build.sh: GITHUB_OUTPUT is not set (this runs as a workflow step)" >&2
  exit 2
fi

emit() {
  printf 'apps=%s\nreason=%s\n' "$1" "$2" >>"$OUT"
  echo "apps=$1 — $2"
}

APPLE_PATHS=(
  "ios-app/"
  "macos-app/"
  ".github/workflows/app-build-checks.yml"
  ".github/scripts/decide_app_build.sh"
  "scripts/release_apps.sh"
  # ios-app/scripts/widget_registry.py --check reads this doc from outside
  # the app trees; an edit to it must still run the registry check.
  "docs/WIDGET_COVERAGE.md"
)

is_apple() {
  local path="$1" p
  for p in "${APPLE_PATHS[@]}"; do
    case "$path" in "$p"*) return 0 ;; esac
  done
  return 1
}

if [ "$EVENT" = "pull_request" ]; then
  base="${BASE_SHA:-}"
  head="${PR_HEAD_SHA:-}"
  zero="0000000000000000000000000000000000000000"
  if [ -z "$base" ] || [ -z "$head" ] || [ "$base" = "$zero" ] || [ "$head" = "$zero" ] \
     || ! git -C "$REPO" cat-file -e "${base}^{commit}" 2>/dev/null \
     || ! git -C "$REPO" cat-file -e "${head}^{commit}" 2>/dev/null; then
    emit true "no-diffable-base"
    exit 0
  fi

  changed="$(mktemp "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/changed.XXXXXX")" || exit 2
  trap 'rm -f "$changed"' EXIT
  # THREE dots: diff from the merge base, so main's newer commits are not
  # reversed into the PR's changed-file list.
  if ! git -C "$REPO" diff --name-only "${base}...${head}" >"$changed"; then
    emit true "diff-failed"
    exit 0
  fi

  echo "changed files:"
  sed 's/^/  /' "$changed"
  while IFS= read -r file; do
    [ -n "$file" ] || continue
    if is_apple "$file"; then
      emit true "touches:${file}"
      exit 0
    fi
  done <"$changed"
  emit false "no Apple build input in the diff"
  exit 0
fi

emit true "full-build-event:${EVENT:-unknown}"
