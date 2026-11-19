#!/bin/bash
# alert_feed_check.sh — the macOS alert path, EXECUTED and then checked for the
# two rules a compile cannot see.
#
# Card N4 chose polling over APNs: `GET /v1/alerts?since=<id>` every 5 s while
# the app runs, local `UNUserNotificationCenter` banners, and a badge/list in the
# menu bar dropdown. The properties that matter are the ones this gate owns:
#
#   1. EXECUTED — compile the SHIPPED `Sources/HSCCMac/AlertFeed.swift` together
#      with the real shared wire page (`WidgetDataClient.swift`) and run it over
#      committed JSON fixtures. A first launch must not replay history, and a
#      full page must advance by `next_since`, not `latest_id` (which would skip
#      alerts the page did not include).
#   2. STRUCTURAL — the app must still be polling, not registering for APNs; the
#      entitlements must carry no `aps-environment`; the 5 s cadence and the
#      dropdown wiring must be in the shipped files, and both files must be in
#      the target's explicit source list.
#
# Compile only, on the macOS SDK; nothing is signed, launched or installed.
#
# Usage: bash macos-app/scripts/alert_feed_check.sh   (from any cwd)
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SELF_DIR/.." || exit 1

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

real_sources=(
  ../ios-app/Sources/Shared/WidgetDataClient.swift
  ../ios-app/Sources/Shared/SharedModels.swift
  ../ios-app/Sources/Shared/SharedStore.swift
  Sources/HSCCMac/AlertFeed.swift
)
harness=(
  ../ios-app/scripts/model_decode_check/ThemeStub.swift
  scripts/alert_feed_check/main.swift
)

echo "compiling the shipped alert policy + the real wire page + harness..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/alert_feed_check" \
     "${real_sources[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the alert policy — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi
if [ -s "$TMP/compile.err" ]; then
  echo "error: the alert policy compiled with diagnostics:" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

"$TMP/alert_feed_check"
executed=$?

# ---------------------------------------------------------------------------
# Structural half. Every leg prints ok:/FAIL and bumps ONE counter, so the
# number a passing run prints is measured from the legs that ran.
# ---------------------------------------------------------------------------
grepfail=0
struct=0
ok()   { struct=$((struct + 1)); echo "ok   $1"; }
bad()  { struct=$((struct + 1)); grepfail=$((grepfail + 1)); echo "FAIL $1"; }

monitor=Sources/HSCCMac/AlertFeedMonitor.swift
policy=Sources/HSCCMac/AlertFeed.swift
view=Sources/HSCCMac/DashboardView.swift
label=Sources/HSCCMac/MenuBarLabel.swift
entitlements=Sources/HSCCMac.entitlements

# (1) The cadence the card requires, and the `since` paging read it names.
if grep -qn 'pollInterval: TimeInterval = 5' "$monitor"; then
  ok "$monitor polls every 5 s"
else
  bad "$monitor no longer names the 5 s poll cadence"
fi
if grep -qn 'alertFeed(since: known' "$monitor"; then
  ok "$monitor advances the feed by its since cursor"
else
  bad "$monitor does not pass the stored cursor to the feed read"
fi

# (2) Polling, not APNs. A remote-notification registration or an APNs
#     entitlement would mean the signing story changed without this gate.
if grep -qn 'registerForRemoteNotifications' "$monitor"; then
  bad "$monitor registers for remote notifications (N4 chose polling)"
else
  ok "$monitor does not register for APNs"
fi
if grep -qn 'aps-environment' "$entitlements"; then
  bad "$entitlements carries aps-environment (signing change not in this card)"
else
  ok "$entitlements still has no aps-environment"
fi
if grep -qn 'UNUserNotificationCenter' "$monitor"; then
  ok "$monitor delivers through UNUserNotificationCenter"
else
  bad "$monitor has no local notification delivery"
fi

# (3) The dropdown draws the badge + the rows, so a monitor nobody observes is
#     caught here rather than by looking at a menu bar and guessing.
if grep -qn 'alerts.unseenCount' "$view" && grep -qn 'ForEach(alerts.rows)' "$view"; then
  ok "$view draws the unseen badge and the last alerts"
else
  bad "$view is not wired to the alert rows/badge"
fi
if grep -qn 'alerts.markViewed' "$view"; then
  ok "$view clears the badge after the dropdown closes"
else
  bad "$view never marks alerts viewed"
fi
if grep -qn 'alerts.unseenCount' "$label"; then
  ok "$label surfaces unviewed alerts beside the H"
else
  bad "$label hides the alert indicator"
fi

# (4) The policy file stays Foundation-only — that is what makes the executed
#     half compile on its own. Comment lines are excluded: the header explains
#     the rule in prose that names the frameworks it forbids.
if grep -vE '^[[:space:]]*//' "$policy" | grep -qE '^import (SwiftUI|AppKit|UserNotifications)|Color'; then
  bad "$policy is no longer Foundation-only (the executed half cannot compile it)"
else
  ok "$policy is Foundation-only"
fi

# (5) Both new files are in the target's explicit list; build_check.sh reads
#     those same - path: lines, so an unlisted file is never compiled/shipped.
for leaf in AlertFeed.swift AlertFeedMonitor.swift; do
  if grep -qn "$leaf" project.yml; then
    ok "project.yml compiles $leaf"
  else
    bad "project.yml does not list $leaf — the target never compiles it"
  fi
done

echo ""
if [ "$grepfail" -gt 0 ] || [ "$executed" -ne 0 ]; then
  echo "ALERT FEED CHECK FAILED — executed half rc=$executed, $grepfail structural check(s) failed"
  exit 1
fi
echo "ALERT FEED CHECK PASSED — executed half green + $struct structural checks"
