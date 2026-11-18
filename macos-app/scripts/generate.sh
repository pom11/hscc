#!/bin/bash
# generate.sh — regenerate HSCCMac.xcodeproj from project.yml.
#
# project.yml reads ${HSCC_DEVELOPMENT_TEAM} for the app target. If unset,
# XcodeGen writes the literal placeholder into the project and signing fails
# much later with a confusing message, so refuse to generate in that state.
#
# Usage:
#   HSCC_DEVELOPMENT_TEAM=ABCDE12345 macos-app/scripts/generate.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v xcodegen >/dev/null 2>&1; then
  echo "error: xcodegen not found. Install it with:  brew install xcodegen" >&2
  exit 1
fi

team="${HSCC_DEVELOPMENT_TEAM:-}"
if [ -z "$team" ]; then
  cat >&2 <<'MSG'
error: no development team.

Export the same team the iOS app uses, then regenerate:

    export HSCC_DEVELOPMENT_TEAM=YOURTEAMID
    macos-app/scripts/generate.sh
MSG
  exit 1
fi

echo "development team: $team"
HSCC_DEVELOPMENT_TEAM="$team" xcodegen generate --spec project.yml

got=$(DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}" \
      xcodebuild -project HSCCMac.xcodeproj -target HSCCMac -showBuildSettings 2>/dev/null \
      | awk '/^ *DEVELOPMENT_TEAM = /{print $3; exit}')
[ "$got" = "$team" ] || { echo "error: signing team on HSCCMac is '${got:-<EMPTY>}', expected $team" >&2; exit 1; }
echo "HSCCMac.xcodeproj regenerated."
