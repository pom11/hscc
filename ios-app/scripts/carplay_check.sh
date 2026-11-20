#!/bin/bash
# carplay_check.sh — the CarPlay scene's offline gate (card W5g).
#
# THREE LEGS, because the risk here is not only the Swift code:
#
#   1. READ-ONLY. The car screen may read the cluster and nothing else. The
#      scene has no ack, no write and no route of its own: this gate pins that
#      (no httpMethod/POST, no App Group or keychain write, and the only client
#      entry point is WidgetDataClient.makeIfConfigured()).
#
#   2. ENTITLEMENTS. Apple has NOT granted HSCC a CarPlay entitlement. The
#      driving-task key lives ONLY in Sources/HSCC.simulator.entitlements,
#      selected by CODE_SIGN_ENTITLEMENTS[sdk=iphonesimulator*]. Device and
#      release builds keep Sources/HSCC.entitlements byte for byte; the three
#      release entitlement files are pinned by SHA-256 below, so any edit to
#      one is a deliberate act reviewed in this file too.
#
#   3. EXECUTED RULES. carplay_check/main.swift runs the REAL CarPlayReading.swift
#      plus the REAL shared readers on the macOS SDK against fake 192.0.2.x
#      hosts: temperature bands, the four row sentences, the queue stages, the
#      30 s stale rule and "never All clear after a failed fetch". The iOS leg
#      then compiles the CarPlay source files against the iOS SDK (warnings are
#      findings). The scene is never built, signed or launched.
#
# Usage: ios-app/scripts/carplay_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

rc=0
ok()   { echo "  ok: $1"; }
fail() { echo "FAIL: $1"; rc=1; }
report() {  # report <label> <finding>
  if [ -n "${2:-}" ]; then fail "$1"; printf '%s\n' "$2" | sed 's/^/    /'; else ok "$1"; fi
}

APP=Sources/HSCC
SCENE="$APP/CarPlay/CarPlaySceneDelegate.swift"
READING="$APP/CarPlay/CarPlayReading.swift"
IMAGES="$APP/CarPlay/CarPlayImages.swift"
DEVICE_ENT=Sources/HSCC.entitlements
SIM_ENT=Sources/HSCC.simulator.entitlements
WIDGET_ENT=Sources/HSCCWidgets.entitlements
LIVE_ENT=Sources/HSCCLiveActivityMonitor.entitlements

for f in "$SCENE" "$READING" "$IMAGES" "$DEVICE_ENT" "$SIM_ENT" \
         project.yml scripts/carplay_check/main.swift; do
  [ -f "$f" ] || { echo "error: $f is missing" >&2; exit 1; }
done

echo "scene wiring"
report "Info.plist scene role names the CarPlay delegate" \
  "$(grep -q 'CPTemplateApplicationSceneSessionRoleApplication' project.yml \
     && grep -q 'CarPlaySceneDelegate' project.yml \
     || echo 'project.yml does not register CPTemplateApplicationSceneSessionRoleApplication with CarPlaySceneDelegate')"
report "scene manifest keeps multiple scenes (phone + car)" \
  "$(grep -q 'UIApplicationSupportsMultipleScenes: true' project.yml \
     || echo 'UIApplicationSupportsMultipleScenes is not true')"
report "CarPlay sources are listed in the app target" \
  "$(grep -q 'Sources/HSCC/CarPlay/CarPlaySceneDelegate.swift' project.yml \
     && grep -q 'Sources/HSCC/CarPlay/CarPlayReading.swift' project.yml \
     && grep -q 'Sources/HSCC/CarPlay/CarPlayImages.swift' project.yml \
     || echo 'a CarPlay source file is not in project.yml')"
report "scene uses the car window's own scale" \
  "$(grep -q 'carWindow.screen.scale' "$SCENE" \
     || echo 'CarPlaySceneDelegate does not read carWindow.screen.scale')"
report "refresh timer is 30 s and stops on disconnect" \
  "$(grep -q 'CarPlayRefresh.seconds' "$SCENE" \
     && grep -q 'refreshTimer?.invalidate()' "$SCENE" \
     && grep -q 'didDisconnectInterfaceController' "$SCENE" \
     || echo 'the 30 s timer / disconnect teardown is missing')"
report "images use UIGraphicsImageRenderer" \
  "$(grep -q 'UIGraphicsImageRenderer' "$IMAGES" \
     || echo 'CarPlayImages.swift does not use UIGraphicsImageRenderer')"
report "stale/offline content is dimmed to 50%" \
  "$(grep -q 'setAlpha(0.5)' "$IMAGES" \
     && grep -q 'CarPlayFreshness.label' "$READING" \
     || echo 'the Native kit dim rule is missing')"
report "CarPlay day/night style change re-renders" \
  "$(grep -q 'contentStyleDidChange' "$SCENE" \
     || echo 'the scene does not handle contentStyleDidChange')"
report "the Needs image receives the latest failure flag" \
  "$(grep -q 'failed: state.needsFailed' "$SCENE" \
     || echo 'a cached All clear could survive a failed fetch')"

echo ""
echo "read-only proof"
CARPLAY_SOURCES=("$SCENE" "$READING" "$IMAGES")
report "no HTTP verb but the client's GET" \
  "$(grep -nE 'httpMethod|\b(POST|PUT|PATCH|DELETE)\b' "${CARPLAY_SOURCES[@]}")"
report "no acks or notification writes" \
  "$(grep -nE '\.acked|SecItem|notify\(|ack\(' "${CARPLAY_SOURCES[@]}")"
report "no App Group or keychain writes" \
  "$(grep -nE 'UserDefaults|suiteName|SharedStore\.suite|SecItem' "${CARPLAY_SOURCES[@]}")"
report "the only client entry point is the read-only factory" \
  "$(grep -q 'WidgetDataClient.makeIfConfigured()' "$SCENE" \
     || echo 'the scene does not build its client with makeIfConfigured()')"

echo ""
echo "entitlements (device/release must stay byte-identical)"
EXPECTED_SHA=6eb3d3fa89c520853732cc99ae538ccadfad2ebac8741dc2c2bceda016253255
for ent in "$DEVICE_ENT" "$WIDGET_ENT" "$LIVE_ENT"; do
  got=$(shasum -a 256 "$ent" | awk '{print $1}')
  report "$ent is unchanged (sha256 ${EXPECTED_SHA:0:12}…)" \
    "$([ "$got" = "$EXPECTED_SHA" ] || echo "sha256 $got != $EXPECTED_SHA")"
done
report "device app does NOT carry a CarPlay entitlement" \
  "$(grep -in 'carplay' "$DEVICE_ENT" || true)"
report "simulator-only file carries com.apple.developer.carplay-driving-task" \
  "$(grep -q 'com.apple.developer.carplay-driving-task' "$SIM_ENT" \
     || echo 'the simulator entitlement file is missing the CarPlay key')"
report "simulator-only file keeps the app group and keychain groups" \
  "$(grep -q 'com.apple.security.application-groups' "$SIM_ENT" \
     && grep -q 'keychain-access-groups' "$SIM_ENT" \
     || echo 'the simulator entitlement file dropped a shared capability')"
report "device CODE_SIGN_ENTITLEMENTS still names Sources/HSCC.entitlements" \
  "$(grep -qE '^ *CODE_SIGN_ENTITLEMENTS: Sources/HSCC\.entitlements$' project.yml \
     || echo 'the unconditional (device) CODE_SIGN_ENTITLEMENTS changed')"
report "simulator SDK condition selects the simulator-only file" \
  "$(grep -qF '"CODE_SIGN_ENTITLEMENTS[sdk=iphonesimulator*]": Sources/HSCC.simulator.entitlements' project.yml \
     || echo 'the sdk=iphonesimulator* entitlement override is missing or changed')"
report "no release path names the simulator entitlements" \
  "$(grep -n 'HSCC.simulator.entitlements' scripts/release_adhoc.sh ../scripts/release_apps.sh 2>/dev/null || true)"
report "exactly four CODE_SIGN_ENTITLEMENTS settings (app x2, two extensions)" \
  "$(n=$(grep -c 'CODE_SIGN_ENTITLEMENTS' project.yml); \
     [ "$n" = "4" ] || echo "found $n CODE_SIGN_ENTITLEMENTS lines")"

echo ""
echo "compile + execute the reading rules on macOS"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

real_sources=(
  "$READING"
  Sources/Shared/SharedModels.swift
  Sources/Shared/SharedStore.swift
  Sources/Shared/WidgetSurfaceKind.generated.swift
  Sources/Shared/WidgetDataClient.swift
  Sources/Shared/NeedsMeReading.swift
  Sources/Shared/MergeQueueReading.swift
  Sources/Shared/ClusterNodeTally.swift
  Sources/Shared/CardsRunningProjection.swift
  scripts/model_decode_check/ThemeStub.swift
)
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/carplay_check" \
     "${real_sources[@]}" scripts/carplay_check/main.swift \
     2>"$TMP/compile.err"; then
  echo "error: failed to compile the CarPlay reading harness" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi
if [ -s "$TMP/compile.err" ]; then
  echo "compiler diagnostics (treated as findings):" >&2
  cat "$TMP/compile.err" >&2
  rc=1
fi
"$TMP/carplay_check" || rc=1

echo ""
echo "compile the CarPlay source files against the iOS SDK"
ios_ver=$(xcrun --sdk iphonesimulator --show-sdk-version 2>/dev/null || true)
ios_major="${ios_ver%%.*}"
if [ -z "$ios_major" ] || [ "$ios_major" -lt 26 ]; then
  echo "::notice::no iOS 26 SDK on this host; the CarPlay compile leg is skipped"
else
  IOS_SDK=$(xcrun --sdk iphonesimulator --show-sdk-path)
  if ! xcrun --sdk iphonesimulator swiftc -sdk "$IOS_SDK" \
       -target arm64-apple-ios26.0-simulator -module-name HSCC \
       -emit-module -emit-module-path "$TMP/HSCC.swiftmodule" \
       -wmo -c -o "$TMP/carplay.o" \
       "$READING" "$IMAGES" "$SCENE" \
       Sources/Shared/SharedModels.swift \
       Sources/Shared/SharedStore.swift \
       Sources/Shared/WidgetSurfaceKind.generated.swift \
       Sources/Shared/WidgetDataClient.swift \
       Sources/Shared/NeedsMeReading.swift \
       Sources/Shared/MergeQueueReading.swift \
       Sources/Shared/ClusterNodeTally.swift \
       Sources/Shared/CardsRunningProjection.swift \
       "$APP/Views/Theme.swift" 2>"$TMP/ios.err"; then
    echo "error: the CarPlay sources do not compile against the iOS SDK" >&2
    cat "$TMP/ios.err" >&2
    rc=1
  elif [ -s "$TMP/ios.err" ]; then
    echo "compiler diagnostics (treated as findings):" >&2
    cat "$TMP/ios.err" >&2
    rc=1
  else
    ok "CarPlay sources compile clean (no warnings)"
  fi
fi

echo ""
if [ "$rc" -eq 0 ]; then
  echo "CARPLAY CHECK PASSED — one read-only scene, simulator-only entitlement, executed rules"
else
  echo "CARPLAY CHECK FAILED — see above"
fi
exit "$rc"
