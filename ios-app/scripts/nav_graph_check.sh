#!/usr/bin/env bash
# nav_graph_check.sh — the navigation-graph GATE (t_810ab152, operator brief
# v2 2026-10-15: "the gate is XCUITest — real taps, no human").
#
# What it runs, in order (the step numbers below are the ones `fail` prints):
#   1  the code-derived screen table is FRESH (nav_graph_screens.py check) —
#      a drawer row added without a walk plan fails HERE, before the walker
#   2  seed the fixture sets into the app container (main = the committed
#      model_decode_check fixtures, same files capture_screens.sh seeds;
#      empty = the same file names with every top-level array zeroed, which
#      is what the EMPTY-state pass launches read via HSCC_DEBUG_FIXTURE_DIR)
#   3  build-for-testing + EXPLICIT install of HSCC.app and the UI-test
#      runner (test-without-building does not refresh installed bundles)
#   4  the WALK: every test in NavGraphUITests on the iPhone-16-class compact
#      simulator (enter -> marker -> back -> same-row re-tap -> PTR served-
#      count receipt; honest empty + error states; disconnected pass)
#   5  the receipts: per-launch fixture manifests under the app container
#      (pull-to-refresh increments proved by the harness's own served counts)
#   6  the NEGATIVE self-check: the walk re-run once with
#      TEST_RUNNER_HSCC_NEGATE_BACK=1 (xcodebuild passthrough into the test
#      process; the walk forwards it to the app, whose DEBUG seam then omits
#      drawerOpen/drawerClose). The suite MUST go red and name the screen —
#      testNegativeSeamEngages guards this wiring in the same run. A walker
#      that cannot fail is not a gate.
#
# The gate FAILS naming the screen + missing edge: every assertion message in
# NavGraphUITests is prefixed "DEAD END [<screen>]: <missing edge>".
#
# Usage: scripts/nav_graph_check.sh [device-udid]
#   default device: $HSCC_UI_DEVICE_UDID, else booted
# Requires: booted simulator with HSCC.app (Debug) installed — see
#   capture_screens.sh's header for the build line.
#
# Simulator-local only: fixtures never hit the network (fixtures.invalid is a
# reserved-TLD host, RFC 6761); nothing here touches fleet/daemon/proxy.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$(dirname "$HERE")"
DEVICE="${1:-${HSCC_UI_DEVICE_UDID:-booted}}"
BUNDLE="com.hscc.ios"
FIXTURES_SRC="$APP_ROOT/scripts/model_decode_check/fixtures"
# Derived dir per-CHECKOUT (path hash), not a shared global: any two
# workers building different branches (this card vs main vs a sibling
# fix) into one shared dir clobber each other's runner bundle, and a
# test-without-building run then executes someone else's binary.
# HSCC_UI_DERIVED overrides.
DERIVED="${HSCC_UI_DERIVED:-$HOME/.cache/hscc-uitest-dd/$(printf '%s' "$APP_ROOT" | shasum -a 1 | cut -c1-12)}"
RESULTS="$HOME/.cache/hscc-uitest-results/$(date +%s)-navgraph"
mkdir -p "$RESULTS"

step=0
fail() { echo "FATAL (step $step): $*" >&2; exit 1; }
say() { echo; echo "==> step $step: $*"; }

# Resolve the simulator's data dir (for container file operations) and the
# app container's Documents folder.
APP=$(xcrun simctl get_app_container "$DEVICE" "$BUNDLE" app 2>/dev/null) \
  || fail "install first: xcrun simctl install $DEVICE <path>/HSCC.app"
CONTAINER=$(xcrun simctl get_app_container "$DEVICE" "$BUNDLE" data) || fail "no data container"
DOC="$CONTAINER/Documents"
# iOS recreates the data container when the bundle is reinstalled (t_810ab152
# gate run: seed -> container A, step 3 install -> container B inherited the
# seeded Documents, the walk wrote receipts into B — any single resolved path
# is stale by receipt time). The receipt scan therefore covers EVERY
# container under the device, with an mtime window as the freshness proof.
DEVICE_UDID="$DEVICE"
if [ "$DEVICE" = "booted" ]; then
  DEVICE_UDID=$(xcrun simctl list devices booted | sed -n 's/.*(\([0-9A-F\-]\{36\}\)) (Booted).*/\1/p' | head -1)
fi
DATA_ROOT="$HOME/Library/Developer/CoreSimulator/Devices/$DEVICE_UDID/data/Containers/Data/Application"

# ---------------------------------------------------------------------------
step=1
say "screen table freshness (code-derived enumeration)"
python3 "$HERE/nav_graph_screens.py" check || fail 1 "NavGraphScreenTable.swift is drifted from AppShell/Sidebar — regenerate (nav_graph_screens.py emit) and commit, or add the missing walk expectations"

# ---------------------------------------------------------------------------
step=2
say "seed fixture sets into $DOC (main + empty)"
# The full list capture_screens.sh seeds (main set) — copy, not link.
MAIN_LIST=(v1_ping v1_health v1_verify v1_cluster_status cluster_hosts \
           v1_cluster_monitor fleet_stats fleet_throughput fleet_streams \
           v1_autoscale cards card_detail_t_049d6986 kanban_blocked kanban_stale \
           v1_activity_feed autodown_status cron_list daemon_status \
           v1_daemon_history v1_standup v1_review_queue v1_qa_queue v1_projects \
           project_hscc commands v1_profiles profiles_list v1_sessions \
           v1_triggers v1_escalate logs_daemon memory_list template_list \
           template_status review_detail)
mkdir -p "$DOC/HSCCFixtures" "$DOC/HSCCFixturesEmpty"
for f in "${MAIN_LIST[@]}"; do
  [ -f "$FIXTURES_SRC/$f.json" ] && cp "$FIXTURES_SRC/$f.json" "$DOC/HSCCFixtures/" || true
done
# The EMPTY set: same file names (the harness maps paths -> names identically),
# every top-level ARRAY zeroed (and matching *count fields). Decoding stays
# legal (keys/types preserved); rows vanish, so each screen must show its
# honest empty row. Shared with the dev loop via scripts/make_empty_fixtures.py.
python3 "$HERE/make_empty_fixtures.py" "$FIXTURES_SRC" "$DOC/HSCCFixturesEmpty" "${MAIN_LIST[@]}" \
  || fail 2 "empty-fixture generation failed"

# ---------------------------------------------------------------------------
step=3
say "build-for-testing (UITests target)"
cd "$APP_ROOT"
xcodegen generate >/dev/null || fail 3 "xcodegen generate failed"
xcodebuild build-for-testing -project HSCC.xcodeproj -scheme HSCCUITests \
  -destination "platform=iOS Simulator,id=$DEVICE" \
  CODE_SIGNING_ALLOWED=NO -derivedDataPath "$DERIVED" \
  > "$RESULTS/build.log" 2>&1 \
  || { tail -40 "$RESULTS/build.log"; fail 3 "build-for-testing failed (see $RESULTS/build.log)"; }
grep -q "TEST BUILD SUCCEEDED" "$RESULTS/build.log" || fail 3 "build did not report success"
echo "  build ok"
# test-without-building does NOT re-install changed bundles (proved
# t_810ab152: a stale runner on the sim kept executing an old test binary
# — confusing dead-end "findings" that were really stale code). Install
# both products explicitly every run.
xcrun simctl install "$DEVICE" "$DERIVED/Build/Products/Debug-iphonesimulator/HSCC.app" \
  || fail 3 "install HSCC.app failed"
xcrun simctl install "$DEVICE" "$DERIVED/Build/Products/Debug-iphonesimulator/HSCCUITests-Runner.app" \
  || fail 3 "install HSCCUITests-Runner.app failed"
echo "  app + runner installed (explicit — test-without-building does not refresh installs)"

# ---------------------------------------------------------------------------
step=4
say "the WALK — NavGraphUITests on $DEVICE (fixtures; never the live API)"
# Fresh receipts only: drop last run's manifests from EVERY container under
# this device so step 5 cannot pass on stale files (the walker names
# manifests per launch, deterministic; UUID churn makes single-path cleanup
# incomplete — see the DATA_ROOT note above).
find "$DATA_ROOT" -path "*/HSCCFixtures*/manifest-*.json" -delete 2>/dev/null
WALK_START_TS=$(date +%s)
xcodebuild test-without-building -project HSCC.xcodeproj -scheme HSCCUITests \
  -only-testing:HSCCUITests/NavGraphUITests \
  -destination "platform=iOS Simulator,id=$DEVICE" \
  CODE_SIGNING_ALLOWED=NO -derivedDataPath "$DERIVED" \
  -resultBundlePath "$RESULTS/walk.xcresult" \
  > "$RESULTS/walk.log" 2>&1
WALK_RC=$?
echo "  walk exit=$WALK_RC (log: $RESULTS/walk.log)"
if [ "$WALK_RC" != "0" ]; then
  # Surface exactly what the contract promises: screen + missing edge.
  grep -E "DEAD END|Test Case .* failed" "$RESULTS/walk.log" | sed 's/^/    /' | head -40
  fail 4 "nav-graph walk went RED (see above; full log $RESULTS/walk.log)"
fi
grep -E "Test Case .* passed" "$RESULTS/walk.log" | sed 's/^/    /'

# ---------------------------------------------------------------------------
step=5
say "manifest receipts (served fixtures per launch)"
python3 - "$DATA_ROOT" "$WALK_START_TS" <<'PY' || fail 5 "receipt check failed"
import json, sys, pathlib
root = pathlib.Path(sys.argv[1])
min_ts = int(sys.argv[2]) - 60   # one minute of slack for clock skew
fresh = lambda p: p.stat().st_mtime >= min_ts
# Scan EVERY app container under the device: iOS rotates the data-container
# UUID when the bundle is reinstalled mid-gate, so receipts may land in a
# container other than the one resolved at script start (t_810ab152).
def glob_pat(pat):
    return [p for p in root.glob(f"*/Documents/{pat}") if fresh(p)]
walk = sorted(glob_pat("HSCCFixtures/manifest-walk-*.json"))
# home + settings are static (no endpoint load until interaction), so a zero
# served-count there is honest, not a fixture miss. Every OTHER drawer screen
# must have served at least one fixture — otherwise its rows could be cache
# renderings or empty views, and the walk would prove nothing.
STATIC_SCREENS = {"manifest-walk-home.json", "manifest-walk-settings.json"}
missing = [p.name for p in walk
           if p.name not in STATIC_SCREENS
           and not (json.loads(p.read_text()).get("served"))]
print(f"  walk manifests: {len(walk)}")
if not walk:
    sys.exit("no walk-* manifests — the walker never ran in fixture mode")
if missing:
    sys.exit(f"manifests with zero served fixtures: {missing}")
err = sorted(glob_pat("HSCCFixtures/manifest-err-*.json"))
bad = [p.name for p in err
       if not json.loads(p.read_text()).get("failMode")]
print(f"  error-pass manifests: {len(err)} (all flagged failMode)")
if bad: sys.exit(f"error manifests without failMode: {bad}")
empty = sorted(glob_pat("HSCCFixturesEmpty/manifest-empty-*.json"))
print(f"  empty-pass manifests: {len(empty)}")
PY

# ---------------------------------------------------------------------------
step=6
say "NEGATIVE self-check — HSCC_NEGATE_BACK=1 must turn the walk RED"
# All manifests were already cleared by step 4's find-delete (the negative
# pass rewrites manifest-walk-*, which step 5 has already consumed).
# The flag must reach the TEST-RUNNER PROCESS environment. A plain
# `HSCC_NEGATE_BACK=1 xcodebuild ...` only sets it in xcodebuild's own
# process: XCUITests run in a separately-launched runner with a clean env,
# so the walk NEVER saw the flag, drawerClose existed in all 17 launches
# and the negative run was falsely GREEN (proven 2026-10-16 — the gate
# would have reported "the walker cannot detect a dead end" for what was
# a propagation hole). TEST_RUNNER_<NAME> is xcodebuild's documented
# passthrough: it strips the prefix and exports <NAME> into the test
# process, where NavGraphUITests picks it up and forwards it to the app.
# testNegativeSeamEngages is the alarm-first guard for this wiring.
TEST_RUNNER_HSCC_NEGATE_BACK=1 xcodebuild test-without-building -project HSCC.xcodeproj \
  -scheme HSCCUITests \
  -only-testing:HSCCUITests/NavGraphUITests/testNegativeSeamEngages \
  -only-testing:HSCCUITests/NavGraphUITests/testWalkEveryDrawerRow \
  -destination "platform=iOS Simulator,id=$DEVICE" \
  CODE_SIGNING_ALLOWED=NO -derivedDataPath "$DERIVED" \
  -resultBundlePath "$RESULTS/negative.xcresult" \
  > "$RESULTS/negative.log" 2>&1
NEG_RC=$?
if [ "$NEG_RC" = "0" ]; then
  fail 6 "NEGATIVE TEST PASSED — the walk stayed GREEN while drawerOpen/drawerClose were removed. The walker cannot detect a dead end; it is not a gate."
fi
# A red run whose only failure is SEAM INERT is a BROKEN HARNESS (the flag
# never reached the app), not a walker that misses dead ends — say which.
if grep -q "SEAM INERT" "$RESULTS/negative.log"; then
  grep "SEAM INERT" "$RESULTS/negative.log" | head -2 | sed 's/^/    /'
  fail 6 "negative run went red on the GUARD, not the walk: the negation flag never reached the app (TEST_RUNNER_ passthrough broken). Fix the gate wiring before trusting any green/negative result."
fi
if ! grep -q "DEAD END" "$RESULTS/negative.log"; then
  fail 6 "negative run failed for an unexpected reason (no DEAD END message): see $RESULTS/negative.log"
fi
grep -E "DEAD END" "$RESULTS/negative.log" | head -3 | sed 's/^/    /'
echo "  negative test correctly RED"

echo
echo "DONE — nav graph green: table fresh, walk green, receipts recorded, negative test red."
echo "logs: $RESULTS"
