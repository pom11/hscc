#!/bin/bash
# notify_check.sh — PROVE the notify-operator decision engine (t_0454eb56).
#
# Card t_0454eb56: "Phase 1 decision engine + foreground local notifications,
# headlessly proven". Like chat_state_check.sh, this compiles the REAL engine
# sources — OperatorAlert.swift, ObservedState+LastSeenState.swift,
# NeedsOperatorNotifier.swift (never redeclared here) — plus a slice of the
# REAL `AppGroup` enum out of SharedModels.swift (so the App-Group persistence
# links against the genuine suite name), together with a pure-logic harness
# (scripts/notify_check/main.swift) into a plain macOS CLI, then asserts every
# differential rule in the plan.
#
# The engine is pure Foundation (decision logic + Codable state), so a macOS
# CLI is the faithful runner — there is no iOS runtime on this host and none is
# claimed. `NotificationCoordinator.swift` / `NotificationsAppDelegate.swift`
# are excluded because they import iOS-only frameworks (UserNotifications,
# UIKit); their seams are verified by the app build, not this CLI.
#
# Usage: scripts/notify_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Slice the REAL `AppGroup` enum out of SharedModels.swift so the lastSeen store
# links against the genuine suite name, not a redeclaration. The enum must exist
# and be self-contained (starts `enum AppGroup {`, ends at the first standalone
# `}`).
shared="Sources/Shared/SharedModels.swift"
if ! grep -q '^enum AppGroup {' "$shared"; then
  echo "error: could not locate the real AppGroup enum (marker moved?)" >&2
  exit 1
fi
awk '/^enum AppGroup \{/{f=1} f{print} f && /^}/{exit}' "$shared" > "$TMP/AppGroup.swift"
if ! grep -q 'suiteName' "$TMP/AppGroup.swift"; then
  echo "error: sliced AppGroup lacks suiteName (slice broken?)" >&2
  exit 1
fi

# The pure engine sources. ORDER MATTERS for top-level `enum NeedsOperatorNotifier`:
# files without a `main.swift`-style top-level expression compile fine in any
# order; the harness (main.swift) is the entry point.
real_sources=(
  Sources/HSCC/Notify/OperatorAlert.swift
  Sources/HSCC/Notify/ObservedState+LastSeenState.swift
  Sources/HSCC/Notify/NeedsOperatorNotifier.swift
  "$TMP/AppGroup.swift"
)
harness=(
  scripts/notify_check/main.swift
)

echo "compiling the REAL notify engine + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/notify_check" \
     "${real_sources[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the notify engine — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

"$TMP/notify_check"
exit $?
