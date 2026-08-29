#!/bin/bash
# chat_state_check.sh — PROVE ChatStore's honest terminal-state machine.
#
# Card t_c0953d4c: "A message that fails to deliver stays in the composer marked
# UNSENT with a retry — never silently discarded. A reply that dies mid-stream
# shows what arrived plus a clear terminal state, never a spinner forever."
#
# Like reconnect_check.sh, this compiles the REAL source — ChatStore.swift
# (never redeclared here) plus the REAL ChatEntry enum sliced out of
# OrchestratorChatView.swift — together with a pure-logic harness
# (scripts/chat_state_check/main.swift) into a plain macOS CLI, then asserts:
#   * a DELIVERY failure (POST never created a job) yields an UNSENT entry that
#     keeps the exact prompt + reason (never discarded), and retry re-sends it
#     as a fresh turn keeping the historical UNSENT;
#   * an in-flight reply ALWAYS reaches a terminal state: reachabilityLost()
#     (sustained unreachable polls) keeps the job for later resume and is
#     idempotent, abandonWaiting() (operator Stop) clears the job and is
#     idempotent, and failSend() (job WAS created, terminal error) is unchanged;
#   * ChatEntry Codable round-trips and backward-decodes persisted transcripts
#     written before `unsent` carried a `reason`.
#
# The store is pure Foundation + Combine [the default runtime re-exports the
# Combine symbols via Foundation on this SDK], and the harness is pure logic, so
# a macOS CLI is the faithful runner — there is no iOS platform runtime on this
# host, and a runtime claim is never made here. Same pattern as reconnect_check.
#
# Usage: scripts/chat_state_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Slice the REAL ChatEntry enum out of OrchestratorChatView.swift so we test the
# exact Codable used by the app, not a redeclaration. Both markers must exist.
chat_entry="Sources/HSCC/Views/OrchestratorChatView.swift"
if ! grep -q '^enum ChatEntry: Codable, Equatable {' "$chat_entry" \
   || ! grep -q '^private struct ChatBubble' "$chat_entry"; then
  echo "error: could not locate the real ChatEntry enum (markers moved?)" >&2
  exit 1
fi
awk '/^enum ChatEntry: Codable, Equatable \{/{f=1} f{print} /^\/\/\/ Bubble rendering/{exit}' \
  "$chat_entry" > "$TMP/ChatEntry.swift"

if ! grep -q 'case unsent(prompt' "$TMP/ChatEntry.swift"; then
  echo "error: sliced ChatEntry lacks the .unsent case (t_c0953d4c gone?)" >&2
  exit 1
fi

real_sources=(
  Sources/HSCC/Views/ChatStore.swift
  Sources/HSCC/Views/ProjectUnreadCenter.swift
  "$TMP/ChatEntry.swift"
)
harness=(
  scripts/chat_state_check/main.swift
)

echo "compiling the REAL ChatStore + ChatEntry + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/chat_state_check" \
     "${real_sources[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the chat state machine — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

"$TMP/chat_state_check"
exit $?
