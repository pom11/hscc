#!/bin/bash
# model_decode_check.sh — decode every committed fixture against the REAL models.
#
# Unlike the old mirrored validator (which re-declared every struct and could
# drift into a false green), this compiles the ACTUAL model sources into a
# plain macOS CLI and decodes every committed fixture with them. A decode
# mismatch means the real Models.swift / SharedModels.swift no longer match the
# live JSON — the exact false-green this tool exists to prevent.
#
# What it compiles (all real, none redeclared here):
#   Sources/HSCC/Models.swift
#   Sources/Shared/SharedModels.swift
#   Sources/HSCC/APIError.swift
# The only non-model shim (ThemeStub.swift — a UI design token, not a model)
# exists because SharedModels.swift references Theme.Semantic.* colors, which on
# macOS render nothing but satisfy the needed types.
#
# Runs on macOS only — there is no iOS platform runtime on this host, and the
# decode logic is pure Foundation (the model files are iOS-Codable but decode
# anywhere), so a macOS CLI is the faithful fixture runner.
#
# Usage: scripts/model_decode_check.sh
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
)
harness=(
  scripts/model_decode_check/ThemeStub.swift
  scripts/model_decode_check/main.swift
)

echo "compiling real models + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/decode_check" \
     "${real_models[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the real model sources — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

FIXDIR=$(cd scripts/model_decode_check/fixtures && pwd)
"$TMP/decode_check" "$FIXDIR"
exit $?
