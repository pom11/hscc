#!/bin/bash
# generate.sh — regenerate HSCC.xcodeproj from project.yml with signing set up.
#
# Why this exists: the app AND both app-extension targets (HSCCWidgets,
# HSCCLiveActivity) each need a DEVELOPMENT_TEAM. Setting it on the app alone
# still fails the extensions with:
#     Signing for "HSCCWidgets" requires a development team.
# And a team picked in the Xcode UI is LOST on the next `xcodegen generate`,
# because the project is a generated artifact.
#
# project.yml reads ${HSCC_DEVELOPMENT_TEAM} at the project level, which all
# three targets inherit. If that variable is unset, XcodeGen writes the literal
# string "${HSCC_DEVELOPMENT_TEAM}" as the team — an invalid ID that produces a
# confusing error much later. This script refuses to generate in that state.
#
# Usage:
#   scripts/generate.sh                      # auto-detect the team, or explain
#   HSCC_DEVELOPMENT_TEAM=ABCDE12345 scripts/generate.sh

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v xcodegen >/dev/null 2>&1; then
  echo "error: xcodegen not found. Install it with:  brew install xcodegen" >&2
  exit 1
fi

team="${HSCC_DEVELOPMENT_TEAM:-}"

# Auto-detect: an Apple signing certificate's Organisational Unit IS the team id.
# Walk every valid codesigning identity rather than assuming the certificate is
# named "Apple Development" — a machine may carry differently-named certs (and a
# self-signed one carries no OU at all, which correctly yields nothing here).
if [ -z "$team" ]; then
  while IFS= read -r cn; do
    [ -n "$cn" ] || continue
    team=$(security find-certificate -c "$cn" -p 2>/dev/null \
      | openssl x509 -noout -subject 2>/dev/null \
      | tr ',/' '\n\n' | awk -F= '/^ *OU/{gsub(/ /,"",$2); print $2; exit}') || true
    [ -n "$team" ] && break
  done <<< "$(security find-identity -v -p codesigning 2>/dev/null \
      | sed -n 's/.*\"\(.*\)\"/\1/p')"
fi

if [ -z "$team" ]; then
  cat >&2 <<'MSG'
error: no development team.

All three targets (HSCC, HSCCWidgets, HSCCLiveActivity) need one. Find your team
id in Xcode > Settings > Accounts (or the OU field of your signing certificate),
then either export it for this shell:

    export HSCC_DEVELOPMENT_TEAM=YOURTEAMID
    scripts/generate.sh

or pass it inline:

    HSCC_DEVELOPMENT_TEAM=YOURTEAMID scripts/generate.sh

A free personal Apple ID team works for sideloading onto your own device.
MSG
  exit 1
fi

echo "development team: $team"
HSCC_DEVELOPMENT_TEAM="$team" xcodegen generate --spec project.yml

# Prove it actually landed on every target, rather than assuming inheritance.
missing=0
for t in HSCC HSCCWidgets HSCCLiveActivity; do
  got=$(DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}" \
        xcodebuild -project HSCC.xcodeproj -target "$t" -showBuildSettings 2>/dev/null \
        | awk '/^ *DEVELOPMENT_TEAM = /{print $3; exit}')
  if [ "$got" != "$team" ]; then
    echo "  $t -> ${got:-<EMPTY>}   MISMATCH" >&2
    missing=1
  else
    echo "  $t -> $got"
  fi
done
[ "$missing" = "0" ] || { echo "error: signing not applied to every target" >&2; exit 1; }
echo "HSCC.xcodeproj regenerated. Open it and Run on your device."
