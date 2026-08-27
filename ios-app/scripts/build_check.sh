#!/bin/bash
# build_check.sh — FULL compile of every target, with each target's real file set.
#
# Why -typecheck is not enough: definite-initialization errors ("'self' used in
# property access 'x' before all stored properties are initialized") are
# diagnosed in SIL, AFTER type checking. `swiftc -typecheck` cannot see them.
# One shipped to the operator's Xcode this way. Only a full compile catches it.
#
# Why per-target file sets matter: project.yml lists sources explicitly, and each
# target compiles a different subset. Compiling $(find Sources -name '*.swift')
# checks files the project never builds and misses per-target breakage.
#
# This does NOT replace building in Xcode: it compiles, it does not link app
# bundles, run on a device, or exercise Siri/widget/Live Activity at runtime.
#
# Usage: scripts/build_check.sh
set -uo pipefail
# NOTE: no `set -e` — we want every target compiled, then a single exit code.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk iphonesimulator --show-sdk-path 2>/dev/null) || {
  echo "error: no iphonesimulator SDK (is Xcode installed?)" >&2; exit 1; }
TARGET_TRIPLE=arm64-apple-ios26.0-simulator
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

sources_for() {  # extract one target's Swift file list from project.yml
  python3 - "$1" <<'PY'
import re, sys
name = sys.argv[1]
spec = open('project.yml').read()
blk = spec.split(f'\n  {name}:', 1)[1]
blk = re.split(r'\n  [A-Za-z]\w*:\n', blk, maxsplit=1)[0]
for m in re.findall(r'- path: (Sources/[^\s]+\.swift)', blk):
    print(m)
PY
}

rc=0
for t in HSCC HSCCWidgets HSCCLiveActivity; do
  # macOS ships bash 3.2, which has no `mapfile`.
  files=()
  while IFS= read -r line; do
    [ -n "$line" ] && files+=("$line")
  done < <(sources_for "$t")
  if [ "${#files[@]}" -eq 0 ]; then
    echo "$t: no sources found in project.yml — check the target name" >&2
    rc=1; continue
  fi
  out=$(swiftc -sdk "$SDK" -target "$TARGET_TRIPLE" \
        -module-name "$t" -emit-module -emit-module-path "$TMP/$t.swiftmodule" \
        -wmo -c -o "$TMP/$t.o" "${files[@]}" 2>&1)
  n=$(printf '%s\n' "$out" | grep -c "error:")
  echo "$t: ${#files[@]} files, $n error(s)"
  if [ "$n" != "0" ]; then
    printf '%s\n' "$out" | grep "error:" | head -12
    rc=1
  fi
done

[ "$rc" = "0" ] && echo "full compile clean (compile only — never built or run on a device)"
exit $rc
