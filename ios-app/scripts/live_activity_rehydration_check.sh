#!/bin/bash
# live_activity_rehydration_check.sh — prove the re-hydration STALENESS DECIDER.
#
# The launch sweep reads `Activity.activities`, which only exists on iOS at
# runtime — no ActivityKit here. So this compiles the DECIDER logic (transcribed
# verbatim from LiveActivityManager.sweepLeftoverWakes) into a macOS CLI and
# asserts the full decision table. See main.swift for the transcript + table.
#
# Usage: scripts/live_activity_rehydration_check.sh
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

swiftc -o /tmp/live_activity_rehydration_check \
  scripts/live_activity_rehydration_check/main.swift 2>&1 || {
  echo "compile failed" >&2; exit 1; }

/tmp/live_activity_rehydration_check
