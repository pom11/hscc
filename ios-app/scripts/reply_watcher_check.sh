#!/bin/bash
# reply_watcher_check.sh — PROVE StreamReplyWatcher's watermark + unread-badge
# state machine (card t_c9cc4ef9 — "I have to switch tabs to see it").
#
# Compiles the REAL StreamReplyWatcher.swift, ProjectUnreadCenter.swift,
# SessionEvent.swift and their real decode-layer deps, together with a harness
# (scripts/reply_watcher_check/main.swift, which stubs only HSCCClient) into a
# macOS CLI and asserts:
#   * first observation baselines to history WITHOUT badging (no wall of badges
#     on a fresh install);
#   * a reply arriving while the operator is away is counted exactly once;
#   * user echoes and tool calls never badge;
#   * replies the operator already READ live (noteSeen) are never re-badged;
#   * a reply after the operator stopped reading IS badged;
#   * a reply landing while actively reading the chat is suppressed.
#
# Run whenever StreamReplyWatcher.swift / StreamingChatStore.swift /
# StreamingChatView.swift / ProjectUnreadCenter.swift changes.
# Usage: scripts/reply_watcher_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

real_sources=(
  Sources/HSCC/Models.swift
  Sources/Shared/SharedModels.swift
  Sources/HSCC/APIError.swift
  Sources/HSCC/SessionEvent.swift
  Sources/HSCC/SessionStreamCursor.swift
  Sources/HSCC/StreamingTranscript.swift
  Sources/HSCC/Views/ProjectUnreadCenter.swift
  Sources/HSCC/Views/StreamReplyWatcher.swift
)
harness=(
  scripts/model_decode_check/ThemeStub.swift   # SharedModels references Theme colors
  scripts/reply_watcher_check/Runner.swift
)

echo "compiling real sources + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/reply_watcher_check" \
     "${real_sources[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

"$TMP/reply_watcher_check"
exit $?
