#!/bin/bash
# logs_redactor_check.sh — verify LogRedactor masks secrets and keeps prose.
#
# Redaction is the SECURITY boundary of the logs view (t_2eda26a6): the
# backend redacts first, and this second line of defence must provably mask
# tailnet hosts, RFC1918 addresses, Bearer tokens, key=value secrets, session
# ids, and long opaque runs — while leaving legitimate prose intact.
#
# Compiles the REAL model sources (so LogEntry/LogSource are the real types)
# plus LogRedactor.swift and the harness into a macOS CLI, then runs a fixed
# redaction assertion set. Fails (exit 1) on any missed secret or mangled
# prose. Same pattern as model_decode_check.sh.
#
# Usage: scripts/logs_redactor_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

real_models=(
  Sources/HSCC/Models.swift
  Sources/Shared/SharedModels.swift
  Sources/HSCC/APIError.swift
  Sources/HSCC/SessionEvent.swift
)
harness=(
  scripts/model_decode_check/ThemeStub.swift
  scripts/logs_redactor_check/main.swift
  Sources/HSCC/Views/LogRedactor.swift
)

if ! xcrun --sdk "$SDK" swiftc -o "$TMP/run" "${real_models[@]}" "${harness[@]}" 2>"$TMP/build.err"; then
  echo "BUILD FAILED:"; cat "$TMP/build.err"; exit 1
fi

"$TMP/run"
rc=$?
exit $rc
