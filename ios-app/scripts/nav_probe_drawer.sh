#!/usr/bin/env bash
# DEV PROBE (t_810ab152) — NOT a gate. Launches the installed HSCC.app on the
# iPhone-16-class sim with the drawer open and waits, so an operator can tap
# the drawer rows by hand and see whether the lower rows (logs, settings,
# search) are reachable. The XCUITest walk reported `drawer row "logs" not on
# screen`; this distinguishes "the drawer is unscrollable / a real trap" from
# "my test never scrolled".
#
#   scripts/nav_probe_drawer.sh            # boot + launch, leave it running
set -uo pipefail
DEVICE="${1:-1D04754E-C19B-46DE-BA6D-4472F69D7438}"
BUNDLE="com.hscc.ios"
xcrun simctl bootstatus "$DEVICE" -b >/dev/null 2>&1 || true
xcrun simctl terminate "$DEVICE" "$BUNDLE" >/dev/null 2>&1 || true
SIMCTL_CHILD_HSCC_DEBUG_FIXTURES=1 \
SIMCTL_CHILD_HSCC_DEBUG_OPEN_DRAWER=1 \
SIMCTL_CHILD_HSCC_DEBUG_QUIET_POLLERS=1 \
  xcrun simctl launch "$DEVICE" "$BUNDLE"
echo "launched $BUNDLE on $DEVICE with the drawer open — scroll it by hand"
echo "screenshot: xcrun simctl io $DEVICE screenshot /tmp/drawer.png"
