#!/bin/bash
# offline_queue_check.sh — PROVE the offline send queue contract (t_42ba90d2).
#
# Card t_42ba90d2: "A message composed while the cluster is unreachable must
# not be lost, never silently dropped, never sent twice, and must be visibly
# distinct until it lands. When the connection returns, queued messages flush."
#
# Like chat_state_check.sh, this compiles the REAL source — OfflineSendQueue.swift,
# ConnectionMonitor.swift, APIError.swift, ChatStore.swift, plus the REAL ChatEntry
# enum sliced out of OrchestratorChatView.swift (never redeclared here) — together
# with a pure-logic harness (scripts/offline_queue_check/main.swift) into a plain
# macOS CLI, then asserts:
#   * enqueue persists the message (never lost);
#   * flush on `.reachable` delivers each message EXACTLY once and removes it;
#   * a transport failure keeps the message queued (never dropped);
#   * a permanent rejection removes it with a recorded reason;
#   * flush is a no-op before reachability / without a handler (stays queued);
#   * auto-flush fires on the ConnectionMonitor -> .reachable transition;
#   * ChatStore.reconcileQueued flips a delivered .queued to .prompt and a
#     non-delivered one to .failure — never silently faked as sent.
#
# The store/queue are pure Foundation + Combine and the harness is pure logic,
# so a macOS CLI is the faithful runner — there is no iOS platform runtime on
# this host, and a runtime claim is never made here. Same pattern as
# chat_state_check.
#
# Usage: scripts/offline_queue_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Slice the REAL ChatEntry enum out of OrchestratorChatView.swift so we test the
# exact Codable persisted by the app, not a redeclaration. Both markers must exist.
chat_entry="Sources/HSCC/Views/OrchestratorChatView.swift"
if ! grep -q '^enum ChatEntry: Codable, Equatable {' "$chat_entry" \
   || ! grep -q '^private struct ChatBubble' "$chat_entry"; then
  echo "error: could not locate the real ChatEntry enum (markers moved?)" >&2
  exit 1
fi
awk '/^enum ChatEntry: Codable, Equatable \{/{f=1} f{print} /^\/\/\/ Bubble rendering/{exit}' \
  "$chat_entry" > "$TMP/ChatEntry.swift"
# The slice is a standalone file (the app's import lives above the enum in the
# view file). Prepend the import the enum's Codable/UUID types need to compile.
{ printf 'import Foundation\n\n'; cat "$TMP/ChatEntry.swift"; } > "$TMP/ChatEntry.swift.tmp"
mv "$TMP/ChatEntry.swift.tmp" "$TMP/ChatEntry.swift"

if ! grep -q 'case queued(text' "$TMP/ChatEntry.swift"; then
  echo "error: sliced ChatEntry lacks the .queued case (t_42ba90d2 gone?)" >&2
  exit 1
fi

real_sources=(
  Sources/HSCC/OfflineSendQueue.swift
  Sources/HSCC/ConnectionMonitor.swift
  Sources/HSCC/APIError.swift
  Sources/HSCC/Views/ChatStore.swift
  Sources/HSCC/Views/ProjectUnreadCenter.swift
  "$TMP/ChatEntry.swift"
)
harness=(
  scripts/offline_queue_check/main.swift
)

echo "compiling the REAL OfflineSendQueue + ConnectionMonitor + ChatStore + ChatEntry + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/offline_queue_check" \
     "${real_sources[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the offline queue harness — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

"$TMP/offline_queue_check"
exit $?
