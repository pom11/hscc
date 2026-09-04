#!/bin/bash
# diff_model_check.sh — decode the committed diff fixture against the REAL
# models, and prove DiffDetailResponse + its view helpers behave.
#
# Same faithful pattern as model_decode_check.sh: this compiles the ACTUAL
# model sources into a plain macOS CLI and decodes with them, so a decode
# mismatch means the real Sources/HSCC/Models.swift no longer matches the live
# GET /v1/review/{card_id}/diff JSON — the false-green this tool exists to
# prevent. Scoped to the diff contract so it reads fast and fails precisely.
#
# What it compiles (all real, none redeclared here):
#   Sources/HSCC/Models.swift
#   Sources/Shared/SharedModels.swift
#   Sources/HSCC/APIError.swift
#   Sources/HSCC/SessionEvent.swift
# plus the same Theme shim model_decode_check uses (a UI design token, not a
# model — needed only because SharedModels.swift references Theme.Semantic.*).
#
# Runs on macOS only — there is no iOS platform runtime on this host, and the
# decode logic is pure Foundation (the model files are iOS-Codable but decode
# anywhere), so a macOS CLI is the faithful fixture runner.
#
# Usage: scripts/diff_model_check.sh
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
  scripts/diff_model_check/main.swift
)

echo "compiling real models + diff harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/diff_check" \
     "${real_models[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the real model sources — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

FIXDIR=$(cd scripts/diff_model_check/fixtures && pwd)
"$TMP/diff_check" "$FIXDIR"
exit $?
