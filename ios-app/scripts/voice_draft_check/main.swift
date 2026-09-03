import Foundation

// ===========================================================================
// voice_draft_check — prove the REAL composer draft-shaping rules headlessly.
//
// This compiles the ACTUAL ComposerText.swift (the pure text rules behind the
// chat composers' Voice/Dictate button) into a macOS CLI and asserts the draft
// merging. A failing assertion means the draft shaping no longer matches the
// pinned contract (no double spaces, clean trim, whitespace-only guard).
//
// The microphone capture itself is the SYSTEM keyboard dictation affordance
// and is device-only — it cannot run on this host. But the text rules the
// recognized speech passes through are pure Foundation, so THIS is the faithful
// thing a macOS CLI can prove.
//
// Run via scripts/voice_draft_check.sh. macOS only.
// ===========================================================================

var failures = 0
func check(_ name: String, _ cond: @autoclosure () -> Bool, _ detail: String = "") {
    if cond() {
        print("  ok: \(name)")
    } else {
        failures += 1
        print("FAIL: \(name) \(detail)")
    }
}

// ---- sendable / empty guard ----

let trimmed = ComposerText.sendable
let isEmpty = ComposerText.isEmpty

print("sendable(_:) — trim for send")
check("trims surrounding whitespace/newlines",
      trimmed("  status please \n") == "status please")
check("keeps interior whitespace",
      trimmed("run  deploy\n  and tail logs") == "run  deploy\n  and tail logs")
check("blank is empty after trim", isEmpty("   \n  "))
check("real text is not empty", !isEmpty("deploy"))
check("empty string is empty", isEmpty(""))
check("dictated result with stray trailing space still trims",
      trimmed("deploy  ") == "deploy")

// ---- inserting(_:into:) — merge dictated/pasted text into the draft ----

print("inserting(_:into:) — merge dictated text into a draft")
check("into an empty draft -> the fragment alone",
      ComposerText.inserting("  stop the job  ", into: "") == "stop the job")
check("flattens incoming leading/trailing whitespace",
      ComposerText.inserting("  tail  ", into: "run") == "run tail")
check("single space between words (no double space)",
      ComposerText.inserting("rollback", into: "deploy ") == "deploy rollback")
check("multi-word dictation keeps interior spacing",
      ComposerText.inserting("scale  to 4", into: "run") == "run scale  to 4")
check("draft trailing whitespace does not double-space",
      ComposerText.inserting("now", into: "status  ") == "status now")
check("empty incoming leaves draft untouched",
      ComposerText.inserting("   ", into: "keep me") == "keep me")
check("fragment-only preserves capitalisation",
      ComposerText.inserting("  Deploy Now  ", into: "") == "Deploy Now")
check("dictate into a mid-typing draft reads naturally",
      ComposerText.inserting("the log", into: "tail") == "tail the log")

print("")
if failures == 0 {
    print("voice_draft_check: ALL PASSED")
} else {
    print("voice_draft_check: \(failures) FAILURE(S)")
    exit(1)
}
