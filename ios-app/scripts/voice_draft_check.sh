#!/bin/bash
# voice_draft_check.sh — prove the REAL composer draft-shaping rules headlessly.
#
# Compiles the ACTUAL ComposerText.swift into a plain macOS CLI and asserts the
# draft-merge rules behind the chat composers' Voice/Dictate button (no double
# spaces, clean trim for send, whitespace-only guard).
#
# The microphone capture itself is the SYSTEM keyboard dictation affordance and
# is device-only — it cannot run on this host. But the text rules the recognized
# speech passes through are pure Foundation, so a macOS CLI is the faithful
# logic runner (the same "no iOS runtime on this host" reasoning as the other
# *_check harnesses).
#
# Run this whenever ComposerText.swift changes.
# Usage: scripts/voice_draft_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

real_sources=(
  Sources/HSCC/Views/ComposerText.swift
)
harness=(
  scripts/voice_draft_check/main.swift
)

echo "compiling real ComposerText.swift + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/voice_draft_check" \
     "${real_sources[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

"$TMP/voice_draft_check"
exit $?
