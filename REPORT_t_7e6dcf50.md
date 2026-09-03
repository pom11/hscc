# Screen audit: ProfileEditorView — t_7e6dcf50

**Assignee:** ios-engineer
**File audited:** `ios-app/Sources/HSCC/Views/ProfileEditorView.swift`
**Branch:** audit/profileeditor-t_7e6dcf50 (worktree, from dev)
**Commit:** 2b3d2ad

## Summary

One CRITICAL bug found and fixed (Save button permanently disabled — the `edited`
flag was never set true). One decode-coverage gap closed (ProfileEditorResponse
had no decode fixture). Everything else audited clean with live-API evidence.

---

## 1. DATA IN — every field the view needs arrives. PROVEN live.

The view is fed by `GET /v1/profile/editor/{profile}` via
`client.profileEditor(profile:)` (HSCCClient.swift:530-533). It decodes into
`ProfileEditorResponse` (Models.swift:771-783).

Live read (read-only curl of the real endpoint, derived address — never
hardcoded); redacting the host as `100.64.0.1`):

    GET /v1/profile/editor/grid-orch  →  200

    profile:        "grid-orch"
    model:          "orchestrator-model"
    provider:       "custom"
    toolsets:       [hermes-cli, kanban, web, browser, terminal, file,
                     code_execution, vision, skills, todo, memory,
                     session_search, clarify, delegation, cronjob, messaging]  (16)
    preload_skills: [brainstorming, writing-plans]                             (2)
    description:    "Claim tasks involving decomposing and dispatching work for
                     the **grid** project onto the `grid` board, ..."
    compression:    { threshold: 0.8, threshold_tokens: 100000 }
    toolsets_all:   32-entry catalog
    skills_all:     ~115 installed skills
    speak:          "grid-orch — model orchestrator-model, 16 toolsets, 2 preloaded skills."

Every field `ProfileEditorResponse` declares maps 1:1 onto a JSON key the
endpoint returns. **Executed proof:** added the live capture as a committed
fixture (`scripts/model_decode_check/fixtures/v1_profile_editor.json`) and a
check line (main.swift:79); `model_decode_check.sh` decodes it against the real
model — **49/49 pass** (was 48).

   NOTE — the response carries `speak` too; the view ignores it (fine — speak is
   for the voice/Siri path, not the form).

## 2. RENDER — what the operator SEES

- **model** → TextField (line 79). `orchestrator-model` renders as-is. ✓
- **provider** → TextField, **only shown when non-empty** (line 83 `if !provider.isEmpty`). Live value `custom` → shown. If a profile had a provider, it shows; if empty, the field hides (reasonable — can't clear it, but provider is set for these orch profiles). ✓
- **description** → vertical TextField, lineLimit 2–4 (line 92). Live description is ~200 chars with markdown `**`/backticks — shows as raw editable text (correct: it IS the raw YAML value; not a bug). ✓
- **toolsets count** → `Text("\(selectedToolsets.count)")` (line 108). Live 16 → shows **16**; matches server's own count (marker on the row = the app's client-side count, which equals server `toolsets` length). NO disagreement. ✓
- **skills count** → `Text("\(selectedSkills.count)")` (line 127). Live 2 → shows 2; matches speak. ✓
- **compression threshold** → Stepper + LabeledContent (line 138). Live `threshold_tokens: 100000` → shows **100000 tokens**. Note: the server ALSO has `threshold: 0.8` (a ratio) which the view does **not** surface — see "deliberately did not fix" below.

No dropped fields, no wrong units, no truncation hiding meaning among the
fields the view was designed to show.

## 3. STATES — "0 results" ≠ "failed to load". Clean.

`Offline.load` (LoadState.swift:120-145) gives the full four-state surface,
and the view switches on it (lines 33-46):

- **loading** (idle/loading) → `HSLoading("Loading profile…")` (line 36).
- **failed** (nothing cached, nothing held) → `HSError("Couldn't load profile",
  message)` with a Retry button (lines 37-40). Distinct from every other state.
- **stale** (live fetch failed but last-known cached) → the editor renders WITH
  a `StaleBanner(age:, reason: "Can't reach the cluster right now.")` +
  retry (lines 41-42, 66-69). Clearly marked, not pretending freshness.
- **loaded** (live success) → editor, no banner (line 44).

This view is a form, so there's no meaningful "zero rows" empty state — the
fields always render once a profile loads. The "0 results vs failed" trap does
not apply: zero toolsets would render the form with counts of 0, never an error
look. The three distinct non-loading states are visually different (spinner /
error+retry / form+banner).

**Offline cache integrity:** `profileEditor` uses the single-arg `get(_:as:)`
(HSCCClient.swift:199-232) which ALWAYS persists to StateCache on success
(line 223) — unlike the query-item GET variant which only caches empty-query
reads. `cacheKey` in the view is `"/v1/profile/editor/\(profile)"` (line 30,
raw name) and `get` stores under the same path (encoded name). For profile
names matching `[A-Za-z0-9._-]+` (enforced backend, routes_profile_editor.py:237),
`.urlPathAllowed` percent-encoding is a no-op (`-`, `.`, `_` are path-allowed),
so the keys match and offline last-known works. No stale/offline blank-screen
bug here (this screen is not in the query-item-unsafe class my other audit
found). ✓

## 4. CONTROLS — every route answers, feedback is present.

| Control | Call | Route answers? | Feedback |
|---|---|---|---|
| model TextField | local @State | n/a | — |
| provider TextField | local @State | n/a | — |
| description TextField | local @State | n/a | — |
| Toolsets button | sets `materials` → `MultiSelectSheet` | n/a | opens sheet |
| Skills button | sets `materials` → sheet | n/a | opens sheet |
| Stepper | local @State (0–200000, step 4000) | n/a | count updates live |
| Save (`MutationButton`) | `client.updateProfile` → POST | **YES** | confirm dialog + spinner + success/failure alert |

**Write route proven live (non-mutating check):** sent `POST
/v1/profile/editor/grid-orch` with `{"model":"<garbage>"}` and NO confirm →
**409 `confirm_required`**, nothing written (re-fetched: model still
`orchestrator-model`). This proves the POST route is registered and the
confirm-gate works — a bare tap can never mutate; only the confirm-gated
`updateProfile` (which always sends `confirm: true`, HSCCClient.swift:551)
writes.

**Save feedback** comes from `MutationButton` (MutationSupport.swift:30-103):
tap arms a `.confirmationDialog` naming the exact change; in-flight shows a
spinner + disables (double-tap guard); success shows "Done"+server `speak`;
failure shows "Failed"+real message (a non-2xx throws → lands in `.failure`,
never rendered as success). No feedback-less control.

## 5. OBSERVATION — CORRECT. No @StateObject-keys-by-project trap.

ProfileEditorView holds NO ObservableObject. All fields are `@State`
(lines 20-28) — value types, re-render correctly. There is no @StateObject to
key by a changing value, so the "stale first instance after navigation" bug
does not apply. `client` and `profile` are plain `let` value inputs (lines
14-16) passed by the parent (ProjectsView.swift:900 uses
`ProfileEditorView(client:profile:"\(name)-orch")`), freshly instantiated each
push — no stale-instance risk.

The `MultiSelectSheet` (lines 207-254) takes `@Binding selected` — the 
toolsets/skills bindings are forwarded through `ProfileEditorMaterials`
(line 201 `let selected: Binding<Set<String>>`) and presented via
`.sheet(item: $materials)`. Edits propagate back to `$selectedToolsets` /
`$selectedSkills`. Correct.

## 6. LAYOUT — survives Dynamic Type & iPhone SE width. Reasoning, not executed.

A `Form` with standard sections. Nothing hardcodes a width or frame; all rows
use flexible HStacks with `Spacer()`. The description TextField uses
`axis: .vertical` + `lineLimit(2...4)` so long text wraps instead of forcing a
horizontal scroll. The Stepper's `LabeledContent` puts the label and the value
inline; at SE width with large Dynamic Type the leading label
("Compaction threshold") may wrap awkwardly but won't break or clip — the value
(`Text` with mono font) shrinks naturally. The count rows use `.caption`
chevrons. No fixed frames, no ScrollView-with-a-fixed-width. This is reasoning
(no iOS runtime here) but the structural signals are all healthy.

## 7. ACCESSIBILITY — no icon-only controls, colour is never the only signal.

- Toolsets / Skills buttons use `Label("Toolsets"/"Preloaded skills",
  systemImage:)` — text label present (lines 106, 125), not icon-only.
- The `chevron.right` and checkmark images are decorative affordances inside
  labelled buttons — VoiceOver reads the button title, not a bare icon.
- Stepper has `LabeledContent("Compaction threshold")` — labelled value.
- Save uses `MutationButton(title: "Save profile", ...)` — labelled (line 151).
- Colour/checkmark in the sheet is accompanied by the option text (lines
  232-240): the checkmark is on the trailing edge beside the name, not the only
  signal of selection.
- No colour-only status anywhere on this screen.

Clean — no a11y violations found.

---

## What I FIXED

1. **CRITICAL — Save permanently disabled.** `@State edited` (line 27) was read
   by `.disabled(!edited)` (line 159) but never set to `true` anywhere. `apply()`
   (lines 170-177) populated every field yet never flipped `edited`. Result: the
   operator can type, toggle, and drag, and Save stays grey forever — the whole
   screen is a read-only dead-end. No compile/harness could catch it (it's a
   runtime logic gap; build_check.dart passed). **Fix:** six `.onChange` modifiers
   (lines 82, 87, 94, 112, 132, 144) — one per editable field — flip `edited =
   true` on the first edit, enabling Save. Two-parameter closure form (not the
   iOS-17-deprecated single-arg), matching the codebase convention
   (OrchestratorChatView.swift:126).

2. **Decode-coverage gap closed.** `ProfileEditorResponse` had no fixture in
   model_decode_check. Added a real live capture + a check line (main.swift:79)
   so the DATA IN shape is proven against the actual model. 48→49/49.

## What I deliberately did NOT fix (and why)

- **Compression `threshold` (ratio 0.8) not surfaced.** The `/v1/profile/editor`
  response carries BOTH `compression.threshold` (0.8, a float ratio) and
  `compression.threshold_tokens` (100000, absolute tokens). The view only edits
  `threshold_tokens` (via the Stepper) and `save()` POSTs only `threshold_tokens`
  (ProfileEditorView.swift:189). The ratio `threshold` is left untouched on save
  (safe merge preserves it), and is not displayed. This is arguably intentional —
  the UI exposes the absolute-token form the operator understands; the ratio is a
  Hermes-internal knob. Not broken, not a data risk (it's preserved). LEFT AS IS,
  but noted for the operator: editing the threshold on this screen does NOT change
  the 0.8 ratio. Medium-low priority.
- **`hermes-cli` / `messaging` toolsets invisible in the picker.** Every live
  orch profile's `toolsets` includes `hermes-cli` and `messaging`, but neither is
  in the backend `_TOOLSET_CATALOG` (routes_profile_editor.py:60-67), so they
  never render as options in the MultiSelectSheet. Because `apply()` seeds
  `selectedToolsets` from the actual `toolsets` (including these two), they ARE
  preserved on save — no data loss. The only cost: the operator can't see or
  toggle them directly. Low likelihood of being hit as a bug (preserved on save).
- **Live `GET /v1/profile/editor/` shows 0 profiles "running".** The API's
  `/v1/profiles` reports `running: 0` because no profiles are currently active —
  a live-state fact, not a bug in the editor itself. The editor reads on-disk
  config regardless of running state.

---

## Evidence trail

- **Live GET** (read-only curl, derived host) → returned full editable profile
  for grid-orch/pom-orch/efsdriver-orch; every field decoded.
- **Live POST confirm-gate** → 409 `confirm_required`, model unchanged after.
- **build_check.sh** → `full compile clean, 0 warnings` (58 files HSCC target).
- **model_decode_check.sh** → `ALL DECODE CHECKS PASSED — 49/49`.
- **Address/token guard** → no IPs, no secrets in fixture or view.

NO iOS runtime on this host — the Save-fix is proved at the compile + source
level (the onChange modifiers are standard, device-independent SwiftUI). The
live data-in and route-answering are executed proofs. Everything else is
reasoning clearly labelled as such.
