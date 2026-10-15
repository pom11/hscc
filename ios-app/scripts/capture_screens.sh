#!/usr/bin/env bash
# Capture the Phase 2 screen set WITHOUT taps (t_0d5632bc, operator brief
# 2026-10-15: "Don't wait on a human. Drive navigation without taps").
#
# How it avoids the blocker: the app never raises the first-run notification
# alert when launched with HSCC_DEBUG_FIXTURES=1 (DebugHarness gate), so there
# is no modal to answer; navigation happens through HSCC_DEBUG_SCREEN (AppShell
# seeds its drawer selection from it) — no tap, no keyboard, no XCUITest
# target. All of it is #if DEBUG.
#
# Data honesty: screens render from the repo's committed model_decode_check
# fixtures (captured live, kept decode-current by the gate) pushed into the
# app container's Documents/HSCCFixtures. They are FIXTURE-FED renders — the
# list recipe with real shapes, NOT a live fleet. Unmapped endpoints answer
# 501 and the screen shows its honest failed row.
#
# Usage: scripts/capture_screens.sh [device-udid] [out-dir]
# Requires: the app already BUILT (Debug) — see the build line in the header
# comment of the CI step, or:
#   xcodegen generate && xcodebuild -project HSCC.xcodeproj -scheme HSCC \
#     -configuration Debug -destination 'generic/platform=iOS Simulator' \
#     CODE_SIGNING_ALLOWED=NO build
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$(dirname "$HERE")"
DEVICE="${1:-booted}"
OUT="${2:-$APP_ROOT/docs/design/screens}"
BUNDLE="com.hscc.ios"
FIXTURES_SRC="$APP_ROOT/scripts/model_decode_check/fixtures"

APP=$(xcrun simctl get_app_container "$DEVICE" "$BUNDLE" app 2>/dev/null) \
  || { echo "install first: xcrun simctl install $DEVICE <path>/HSCC.app"; exit 1; }
CONTAINER=$(xcrun simctl get_app_container "$DEVICE" "$BUNDLE" data)
DOC="$CONTAINER/Documents/HSCCFixtures"

echo "==> seeding fixture folder ($DOC)"
mkdir -p "$DOC"
# Only files the harness maps; copy, not link (container is outside the repo).
for f in v1_ping v1_health v1_verify v1_cluster_status cluster_hosts \
         v1_cluster_monitor fleet_stats fleet_throughput fleet_streams \
         v1_autoscale cards card_detail_t_049d6986 kanban_blocked kanban_stale \
         v1_activity_feed autodown_status cron_list daemon_status \
         v1_daemon_history v1_standup v1_review_queue v1_qa_queue v1_projects \
         project_hscc commands v1_profiles profiles_list v1_sessions \
         v1_triggers v1_escalate logs_daemon memory_list template_list \
         template_status review_detail; do
  [ -f "$FIXTURES_SRC/$f.json" ] && cp "$FIXTURES_SRC/$f.json" "$DOC/" || true
done

mkdir -p "$OUT"
mkdir -p "$OUT/manifests"

# The device may report "booted" before SpringBoard actually accepts launches
# (FBSOpenApplicationServiceErrorDomain code=1, "denied by service delegate")
# — seen right after `simctl boot`. Wait for one real launch before looping.
wait_launchable() {
  for i in $(seq 1 24); do
    if xcrun simctl launch "$DEVICE" "$BUNDLE" >/dev/null 2>&1; then
      xcrun simctl terminate "$DEVICE" "$BUNDLE" >/dev/null 2>&1
      echo "  device accepts launches (attempt $i)"
      return 0
    fi
    sleep 5
  done
  echo "FATAL: device never accepted a launch after 120s"; return 1
}
wait_launchable || exit 1

# The recipe screens (spec §5/§8) + the shell's own surfaces. Fleet here also
# serves as the t_2a29a7ab gate shot once the Models section lands.
SCREENS=(home fleet nodesServing opsHealth autodown approvals memory \
         templates sessions activity logs cron)
APPEARANCES=(light dark)

for screen in "${SCREENS[@]}"; do
  for mode in "${APPEARANCES[@]}"; do
    xcrun simctl ui "$DEVICE" appearance "$mode"
    # terminate-then-launch guarantees a fresh render of the requested screen
    # (no leftover stack from the previous launch).
    SIMCTL_CHILD_HSCC_DEBUG_FIXTURES=1 \
    SIMCTL_CHILD_HSCC_DEBUG_SCREEN="$screen" \
      xcrun simctl launch --terminate-running-process "$DEVICE" "$BUNDLE" >/dev/null
    sleep 4   # fixture fetches + SwiftUI first render (local canned bodies)
    png="$OUT/phase2-${screen}-${mode}.png"
    xcrun simctl io "$DEVICE" screenshot "$png"
    # proof, not vibes: pull the per-launch manifest (fixture paths served vs
    # missed, written by DebugHarness.writeCaptureManifest) so a screen that
    # silently fell back to its connect gate FAILS the run instead of shipping
    # an empty-state screenshot labelled as the redesigned screen.
    if [ -f "$DOC/manifest-$screen.json" ]; then
      cp "$DOC/manifest-$screen.json" "$OUT/manifests/manifest-${screen}-${mode}.json"
    fi
    echo "  captured $png"
  done
done

echo "==> verifying manifests (every screen must have served >= 1 fixture)"
FAIL=0
python3 - "$OUT/manifests" <<'PY' || FAIL=1
import json, sys, pathlib
screens = {}
for p in sorted(pathlib.Path(sys.argv[1]).glob("manifest-*.json")):
    d = json.loads(p.read_text())
    name = p.stem.replace("manifest-", "").rsplit("-", 1)[0]
    screens.setdefault(name, d)
missing = [s for s, d in screens.items() if not d.get("served")]
print(f"  screens with manifests: {len(screens)}")
for s in sorted(screens):
    d = screens[s]
    print(f"  {s:14s} served={len(d.get('served', {}))} missed={len(d.get('missed', {}))} "
          f"isConfigured={d.get('isConfigured')}")
if missing:
    print(f"  FAIL — no fixture served (screen showed a gate/empty state): {missing}")
    sys.exit(1)
PY
[ "$FAIL" = "0" ] || { echo "MANIFEST CHECK FAILED"; exit 1; }
echo "DONE: ${#SCREENS[@]} screens x 2 appearances -> $OUT"
