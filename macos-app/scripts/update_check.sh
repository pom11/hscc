#!/bin/bash
# update_check.sh — PROVE the macOS Sparkle 2 wiring (t_2a1e5913).
#
# Static contract (always):
#   * Sparkle is still pinned, linked to the APP only, and the appcast URL /
#     public-key / automatic-check keys are in the app Info.plist;
#   * SparkleUpdater.swift is compiled into HSCCMac, never into the widget;
#   * the updater is the standard controller, checks daily, exposes both
#     automatic-check choices, and reports a found update to the dropdown;
#   * the menu bar dropdown and Settings both offer the manual check.
#
# When a resolved/prebuilt Sparkle.framework is available (HSCC_SPARKLE_FRAMEWORK,
# or macos-app/build/DerivedData/.../Sparkle/Sparkle.framework), this also runs
# build_check.sh against it, so the REAL `import Sparkle` branch compiles rather
# than only the canImport fallback.
#
# Usage: scripts/update_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

python3 - <<'PY'
import os
import re
import sys

spec = open('project.yml').read()
targets = '\n' + spec.split('\ntargets:\n', 1)[1]

def block(name):
    text = targets.split(f'\n  {name}:', 1)[1]
    return re.split(r'\n  [A-Za-z]\w*:\n', text, maxsplit=1)[0]

app = block('HSCCMac')
widget = block('HSCCMacWidgets')
errors = []
ok = 0

def want(condition, message):
    global ok
    if condition:
        ok += 1
    else:
        errors.append(message)

want('exactVersion: 2.6.4' in spec, 'Sparkle is no longer pinned to 2.6.4')
want('- package: Sparkle' in app, 'HSCCMac no longer links the Sparkle package')
want('- package: Sparkle' not in widget, 'the widget extension must not link Sparkle')
want('- path: Sources/HSCCMac/SparkleUpdater.swift' in app,
     'SparkleUpdater.swift is not compiled into HSCCMac')
want('Sources/HSCCMac/SparkleUpdater.swift' not in widget,
     'SparkleUpdater.swift must not be compiled into the widget')

for key, value in (
    ('SUFeedURL', 'https://pom11.github.io/hscc/macos/appcast.xml'),
    ('SUPublicEDKey', '$(HSCC_SPARKLE_PUBLIC_KEY)'),
    ('SUEnableAutomaticChecks', 'true'),
):
    want(f'{key}: {value}' in app, f'HSCCMac Info.plist lost {key}: {value}')
    want(f'{key}:' not in widget, f'the widget extension must not carry {key}')

source = open('Sources/HSCCMac/SparkleUpdater.swift', encoding='utf-8').read()
for marker, message in (
    ('SPUStandardUpdaterController', 'the standard Sparkle updater controller is gone'),
    ('dailyCheckInterval: TimeInterval = 24 * 60 * 60', 'daily checks are not configured'),
    ('updateCheckInterval = Self.dailyCheckInterval', 'the daily interval is not applied'),
    ('automaticallyChecksForUpdates', 'automatic checks are not exposed'),
    ('automaticallyDownloadsUpdates', 'automatic downloads are not exposed'),
    ('didFindValidUpdate', 'a found update is no longer observed'),
    ('availableVersion', 'the update badge has no version to show'),
    ('checkForUpdates(nil)', 'the manual Sparkle check is gone'),
):
    want(marker in source, message)

dashboard = open('Sources/HSCCMac/DashboardView.swift', encoding='utf-8').read()
settings = open('Sources/HSCCMac/MacSettingsView.swift', encoding='utf-8').read()
want('updates.availableVersion' in dashboard,
     'the menu bar dropdown does not show the available update')
want('Check for Updates…' in dashboard,
     'the menu bar dropdown lost Check for Updates…')
want('Check for Updates…' in settings,
     'Settings lost Check for Updates…')
want('Check for updates automatically' in settings,
     'Settings lost the automatic-check toggle')
want('Download updates automatically' in settings,
     'Settings lost the automatic-download toggle')

for base, _, files in os.walk('Sources/HSCCMacWidgets'):
    for name in files:
        if not name.endswith('.swift'):
            continue
        path = os.path.join(base, name)
        text = open(path, encoding='utf-8').read()
        want('Sparkle' not in text, f'{path} references Sparkle; the widget never updates itself')

if errors:
    for error in errors:
        print(f'error: {error}')
    sys.exit(1)
print(f'PASS: {ok} Sparkle wiring assertions')
PY
rc=$?
[ "$rc" = "0" ] || exit $rc

sparkle_root="${HSCC_SPARKLE_FRAMEWORK:-}"
if [ -z "$sparkle_root" ] && [ -d "build/DerivedData/SourcePackages/artifacts/sparkle/Sparkle" ]; then
  sparkle_root="build/DerivedData/SourcePackages/artifacts/sparkle/Sparkle"
fi
if [ -n "$sparkle_root" ]; then
  echo "compiling the REAL import Sparkle branch against $sparkle_root..."
  HSCC_SPARKLE_FRAMEWORK="$sparkle_root" scripts/build_check.sh || exit $?
else
  echo "Sparkle framework not resolved locally — static wiring passed; build_check uses the canImport fallback"
fi

echo "macOS update-check gate green"
