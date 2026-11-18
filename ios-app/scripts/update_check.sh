#!/bin/bash
# update_check.sh — PROVE the iOS in-app update contract (t_2a1e5913).
#
# Two independent halves:
#   1. the pure feed rules, compiled from the REAL Sources/HSCC/UpdateCheck.swift
#      with scripts/update_check/main.swift (version ordering, decode failures,
#      dismissal and the six-hour throttle);
#   2. the target boundary: UpdateCheck.swift / UpdateChecker.swift / the banner
#      are APP-ONLY. None of the three extensions may list them or reference
#      the checker/feed, so a widget or Live Activity can never start a check.
#
# The network is never touched: UpdateChecker's feed parsing is the pure file
# above, and this gate has no socket path at all.
#
# Usage: scripts/update_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

real_source="Sources/HSCC/UpdateCheck.swift"
checker="Sources/HSCC/UpdateChecker.swift"
banner="Sources/HSCC/Views/UpdateBanner.swift"
harness="scripts/update_check/main.swift"

for f in "$real_source" "$checker" "$banner" "$harness"; do
  if [ ! -f "$f" ]; then
    echo "error: $f missing — did it get moved?" >&2
    exit 1
  fi
done

# The checker must keep its public, unauthenticated, short-timeout transport.
if ! grep -q 'https://pom11.github.io/hscc/ios/version.json' "$checker"; then
  echo "error: UpdateChecker no longer points at the published iOS feed" >&2
  exit 1
fi
if grep -Eq 'HSCCClient\(|Authorization:|Bearer ' "$checker"; then
  echo "error: the update check must not use the cluster client or a token" >&2
  exit 1
fi
if ! grep -q 'requestTimeout' "$checker"; then
  echo "error: the update feed request lost its short timeout" >&2
  exit 1
fi

echo "compiling the REAL UpdateCheck + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/update_check" \
     "$real_source" "$harness" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the update-feed contract — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi
if [ -s "$TMP/compile.err" ]; then
  echo "warning: compiler output while building the harness:"; cat "$TMP/compile.err"
fi

"$TMP/update_check" || exit $?

python3 - <<'PY'
import os
import re
import sys

spec = open('project.yml').read()
spec = '\n' + spec.split('\ntargets:\n', 1)[1]

def block(name):
    text = spec.split(f'\n  {name}:', 1)[1]
    return re.split(r'\n  [A-Za-z]\w*:\n', text, maxsplit=1)[0]

app_files = (
    'Sources/HSCC/UpdateCheck.swift',
    'Sources/HSCC/UpdateChecker.swift',
    'Sources/HSCC/Views/UpdateBanner.swift',
)
extensions = ('HSCCWidgets', 'HSCCLiveActivity', 'HSCCLiveActivityMonitor')
problems = []
ok = 0

app = block('HSCC')
for path in app_files:
    if f'- path: {path}' not in app:
        problems.append(f'the app target does not compile {path}')
    else:
        ok += 1

for name in extensions:
    text = block(name)
    for path in app_files:
        if path in text:
            problems.append(f'{name} must not compile {path}')
    ok += 1

for root in ('Sources/HSCCWidgets', 'Sources/HSCCLiveActivity',
             'Sources/HSCCLiveActivityMonitor'):
    for base, _, files in os.walk(root):
        for name in files:
            if not name.endswith('.swift'):
                continue
            path = os.path.join(base, name)
            text = open(path, encoding='utf-8').read()
            for marker in ('UpdateChecker', 'UpdateBanner', 'version.json'):
                if marker in text:
                    problems.append(f'{path} references {marker}')
    ok += 1

if problems:
    for problem in problems:
        print(f'error: {problem}')
    sys.exit(1)
print(f'PASS: {ok} target-boundary assertions — update code is app-only')
PY

rc=$?
if [ "$rc" = "0" ]; then
  echo "update-check gate green (pure feed rules + app-only target boundary; no network)"
fi
exit $rc
