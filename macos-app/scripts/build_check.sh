#!/bin/bash
# build_check.sh — full compile of every macOS target, with each target's real
# file set, against the macosx SDK.
#
# Same contract as ios-app/scripts/build_check.sh:
#   * full compile (-c), not -typecheck: definite-initialization and other
#     SIL-level diagnostics only appear in a real compile;
#   * each target compiles exactly the files project.yml lists (sources are
#     explicit, and Shared files are referenced by path from ios-app — no
#     copies to drift);
#   * warnings are FAILURES, not noise: 0 errors AND 0 warnings is the gate.
#
# This does NOT generate the Xcode project, sign, link an app bundle, launch or
# install anything. Compile only.
#
# Usage: macos-app/scripts/build_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macosx SDK (is Xcode installed?)" >&2; exit 1; }
TARGET_TRIPLE=arm64-apple-macos14.0
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Sparkle is a SwiftPM package, and plain swiftc does not resolve SwiftPM. The
# source guards its import with `#if canImport(Sparkle)`, so this gate still
# compiles the target when the framework is absent. When a resolved/prebuilt
# framework IS available, pass it through and compile the REAL updater branch:
#   HSCC_SPARKLE_FRAMEWORK=/path/containing/Sparkle.framework scripts/build_check.sh
sparkle_flags=()
sparkle_root="${HSCC_SPARKLE_FRAMEWORK:-}"
if [ -z "$sparkle_root" ] && [ -d "build/DerivedData/SourcePackages/artifacts/sparkle/Sparkle" ]; then
  sparkle_root="build/DerivedData/SourcePackages/artifacts/sparkle/Sparkle"
fi
if [ -n "$sparkle_root" ]; then
  if [ -d "$sparkle_root/Sparkle.framework" ]; then
    sparkle_flags=(-F "$sparkle_root")
    echo "Sparkle: compiling against $sparkle_root/Sparkle.framework"
  elif [ -d "$sparkle_root" ] && [ "$(basename "$sparkle_root")" = "Sparkle.framework" ]; then
    sparkle_flags=(-F "$(dirname "$sparkle_root")")
    echo "Sparkle: compiling against $sparkle_root"
  else
    echo "warning: HSCC_SPARKLE_FRAMEWORK has no Sparkle.framework; using the canImport fallback" >&2
  fi
fi

sources_for() {  # extract one target's Swift file list from project.yml
  python3 - "$1" <<'PY'
import re, sys
name = sys.argv[1]
spec = open('project.yml').read()
# Anchor on the targets: section first. Other top-level sections (schemes:)
# reuse target names as two-space keys.
spec = '\n' + spec.split('\ntargets:\n', 1)[1]
blk = spec.split(f'\n  {name}:', 1)[1]
blk = re.split(r'\n  [A-Za-z]\w*:\n', blk, maxsplit=1)[0]
# Paths may be local (Sources/...) or reused by path (../ios-app/Sources/...).
for m in re.findall(r'- path: ([^\s]+\.swift)', blk):
    print(m)
PY
}

rc=0
warnings=0
for t in HSCCMac HSCCMacWidgets; do
  files=()
  while IFS= read -r line; do
    [ -n "$line" ] && files+=("$line")
  done < <(sources_for "$t")
  if [ "${#files[@]}" -eq 0 ]; then
    echo "$t: no sources found in project.yml — check the target name" >&2
    rc=1; continue
  fi
  out=$(swiftc -sdk "$SDK" -target "$TARGET_TRIPLE" \
        -swift-version 5 \
        -module-name "$t" -emit-module -emit-module-path "$TMP/$t.swiftmodule" \
        -wmo -c -o "$TMP/$t.o" ${sparkle_flags[@]+"${sparkle_flags[@]}"} "${files[@]}" 2>&1)
  n=$(printf '%s\n' "$out" | grep -c "error:")
  w=$(printf '%s\n' "$out" | grep -c "warning:")
  echo "$t: ${#files[@]} files, $n error(s), $w warning(s)"
  if [ "$n" != "0" ]; then
    printf '%s\n' "$out" | grep "error:" | head -20
    rc=1
  fi
  if [ "$w" != "0" ]; then
    printf '%s\n' "$out" | grep "warning:" | head -30
    warnings=$((warnings + w))
  fi
done

if [ "$warnings" != "0" ]; then
  echo "FAIL: $warnings compiler warning(s) — fix them or the operator sees them in Xcode"
  rc=1
fi

[ "$rc" = "0" ] && echo "full compile clean, 0 warnings (compile only — never built or run)"
exit $rc
