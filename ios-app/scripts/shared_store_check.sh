#!/bin/bash
# shared_store_check.sh — prove the App Group sharing failure is LOUD, not
# silent, after the .standard fallback was removed.
#
# Why this exists (t_d64ea494): SettingsStore's shared-suite accessor used to be
#
#     UserDefaults(suiteName: AppGroup.suiteName) ?? .standard
#
# On the operator's FREE personal Apple team the App Group cannot be provisioned,
# so the suite resolved to nil and EVERY write fell back to the app's OWN store:
# settings appeared to save in-app but the widget/Live Activity (which read the
# real group suite) saw nothing — the "save then vanish" symptom. The fix removed
# that fallback, split app-local vs shared storage, and added SharedStore as the
# single loud gate.
#
# This check has two independent halves:
#   1. GREP GUARD — the dangerous `.standard` fallback on the shared suite is
#      gone from the committed SettingsStore source (regression-proof). We also
#      assert the old forcing `suite` accessor no longer exists and that the
#      share writes are gated on suite availability.
#   2. LOGIC TEST — EXTRACTS the real `SharedStore` verbatim from
#      Sources/Shared/SharedModels.swift (so it can never drift), re-homes it,
#      and asserts isAvailable is false when the provider yields nil (free team)
#      and true when it yields a real store.
#
# It cannot run on a device (no iOS runtime on this host); it proves the code
# removed the silent path and that the availability decision logic is correct.
#
# Usage: scripts/shared_store_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
SS="Sources/HSCC/SettingsStore.swift"
SRC="Sources/Shared/SharedModels.swift"

# ---- 1. GREP GUARD: no silent fallback on the shared suite ---------------------
fail=0

if grep -nE 'UserDefaults\(suiteName:[^)]*\)[[:space:]]*\?\?' "$SS"; then
  echo "❌ SettingsStore still has a '?? .standard'-style fallback on the shared suite (silent failure returns)." >&2
  fail=1
fi

if grep -nE 'suite: UserDefaults' "$SS"; then
  echo "❌ The old forcing 'suite: UserDefaults' accessor is still present." >&2
  fail=1
fi

# The share write must be gated on availability, not unconditional.
if ! grep -qE 'guard let d = Self\.sharedSuite' "$SS"; then
  echo "❌ publishActiveCluster is not gated on shared suite availability." >&2
  fail=1
fi
if ! grep -qE 'sharedStoreUnavailable|SharedStore\.isAvailable' "$SS"; then
  echo "❌ SettingsStore does not surface the unavailable state." >&2
  fail=1
fi
if ! grep -qE 'isAvailable' "$SRC"; then
  echo "❌ SharedStore gate not present in SharedModels.swift." >&2
  fail=1
fi

if [ "$fail" != "0" ]; then
  echo "❌ GREP GUARD FAILED (silent fallback detected)." >&2
  exit 1
fi
echo "✅ GREP GUARD: no silent .standard fallback; share writes gated on availability."

# ---- 2. LOGIC TEST: SharedStore availability decision --------------------------
# Extract the real enum VERBATIM from committed source (no drift), then drop the
# comment lines so the harness type-checks standalone.
blk=$(awk '/^enum SharedStore \{/,/^\}/' "$SRC" | grep -v '^    ///' | grep -v '^///' | grep -v '^$')

cat > "$TMPDIR/shared_check_$$.swift" <<SWIFT
import Foundation
struct AppGroup { static let suiteName = "group.com.hscc.ios" }
$blk

var ok = true
func check(_ name: String, _ cond: Bool) {
    print("  \\(cond ? "PASS" : "FAIL") \\(name)")
    if !cond { ok = false }
}

// (a) FREE-personal-team state: the container resolves to nil → must be LOUD.
SharedStore.suiteProvider = { nil }
check("isAvailable == false when suite provider returns nil",
      SharedStore.isAvailable == false)

// (b) PAID/provisioned state: a real store resolves → sharing available.
let real = UserDefaults(suiteName: "group.com.hscc.ios")
SharedStore.suiteProvider = { real }
check("isAvailable == true when suite provider returns a store",
      SharedStore.isAvailable == true)

if ok { print("✅ REAL SharedStore availability logic OK") }
else { print("❌ SHARED STORE LOGIC FAILED"); exit(1) }
SWIFT

SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || { echo "no macOS SDK" >&2; exit 1; }
xcrun swiftc -sdk "$SDK" -o "$TMPDIR/shared_check_$$" "$TMPDIR/shared_check_$$.swift" 2>&1 || { echo "compile failed" >&2; exit 1; }
"$TMPDIR/shared_check_$$"
rc=$?
rm -f "$TMPDIR/shared_check_$$" "$TMPDIR/shared_check_$$.swift"
exit $rc
