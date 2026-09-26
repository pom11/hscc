# t_dacdbc4a — Phase 2 [chat] Theme sweep audit

Surface group: StreamingChatView / OrchestratorChatView / SlashCommandPalette /
SlashCommandPreview / ComposerText. Job: make every view use Theme.* (not
invent one).

## Outcome

Three of the five surfaces (SlashCommandPalette, SlashCommandPreview,
ComposerText) were ALREADY fully theme-conformant at baseline — every padding
is `Theme.Spacing.*`, every colour `Theme.Semantic.*`, corners `Theme.Corner.*`.
ComposerText is pure text-shaping logic with no UI at all. No changes needed.

The two big hot files (StreamingChatView, OrchestratorChatView) carried the
raw-numeric-padding and a couple of system-colour bypasses that `check_theme.sh`
does not catch (`Color(.secondarySystemBackground)`, `.foregroundColor(.primary)`
— the regex only flags named `.red/.white/…`, hex, and component constructors).

Baseline verified by execution BEFORE edits: `check_theme.sh` CLEAN,
`build_check.sh` full compile clean 0/0 (all 4 targets).

## Sweep table (violation -> fix -> commit sha)

Commit: f104a49 (see git log)

### StreamingChatView.swift
| Violation (file:line → old) | Theme token |
|-----------------------------|-------------|
| `.padding(.top, 64)` x2 (failedState, emptyState centered invite offset) | `Theme.Spacing.page.rawValue` (60) |
| `.padding(.horizontal, 24)` x3 (empty/failed body text) | `Theme.Spacing.xl.rawValue` (24) |

### OrchestratorChatView.swift
| Violation (file:line → old) | Theme token |
|-----------------------------|-------------|
| `LazyVStack(spacing: 12)` (transcript) | `Theme.Spacing.md.rawValue` (12) |
| `.padding(.vertical, 8)` x3 (scroll content, composer, fleet banner) | `Theme.Spacing.sm.rawValue` (8) |
| `.padding(.bottom, 4)` (in-flight footer) | `Theme.Spacing.xs.rawValue` (4) |
| `VStack(spacing: 8)` x2 (emptyState, composer) | `Theme.Spacing.sm.rawValue` |
| `.padding(.top, 48)` (emptyState invite offset) | `Theme.Spacing.page.rawValue` (60) |
| `.padding(.horizontal, 24)` (emptyState) | `Theme.Spacing.xl.rawValue` (24) |
| `HStack(spacing: 8)` x2 (fleet banner, in-flight footer) | `Theme.Spacing.sm.rawValue` |
| `HStack(alignment: .bottom, spacing: 8)` (composer buttons) | `Theme.Spacing.sm.rawValue` |
| `.padding(.horizontal, 4)` (offline queue chip) | `Theme.Spacing.xs.rawValue` |
| `.background(Color(.secondarySystemBackground))` (reply bubble) | `Theme.Semantic.surfaceRaised` |
| `.foregroundColor(.primary)` (queued bubble text) | `Theme.Semantic.onSurface` |
| `.padding(.leading, 2)` ("will send when connected") | `Theme.Spacing.xxs.rawValue` (2) |
| `.padding(.horizontal, 8)` / `.padding(.vertical, 4)` (Retry button) | `Theme.Spacing.sm` / `.xs` |
| `VStack(spacing: 4)` (bubble inner) | `Theme.Spacing.xs.rawValue` |
| `.padding(10)` (bubble inset) | `Theme.Spacing.md.rawValue` (12) |
| `cornerRadius: 12` (bubble clip) | `Theme.Corner.card.rawValue` |

### SlashCommandPalette.swift / SlashCommandPreview.swift / ComposerText.swift
No violations — already fully conformant. No changes.

## What deliberately STAYS as primitives (not contract violations)

Consistent with the established baseline (sibling card t_b89d9029 and the
already-accepted files):

- `spacing: 0` in container stacks (StreamingChatView 52/376/467,
  OrchestratorChatView 107) — a deliberate zero-gap layout control where each
  child carries its own padding; not an arbitrary step on the spacing scale.
- Inline `ProgressView()` x2 (StreamingChatView 560 the tool "running…" row
  inside an expanded panel; OrchestratorChatView 339 the send-button spinner
  while sending). These are inline status indicators, NOT full-pane loading
  states, so `HSLoading` (a centred full-pane component) is the wrong
  component — the sibling sweep converted full-pane/section loading states to
  HSLoading, and these do not qualify.
- `.white` role tags and `.red` Retry with `// theme-allow:` markers
  (OrchestratorChatView 784/836/837, StreamingChatView 431/435) — the fixed
  accent/hue uses the comment-anchored allowlist, so check_theme stays green.
- `Spacer(minLength: 48/56/40)` turn spacing and `chatMeasure` 560 cap —
  fixed-width layout constraints, not spacing-scale gaps.

## Verify (all against the committed tree)

- `bash scripts/check_theme.sh` → CLEAN.
- `bash scripts/build_check.sh` → full compile clean, 0 errors, 0 warnings
  (all 4 targets: HSCC 76 + 3 extensions).
- `bash scripts/chat_state_check.sh` → ALL CHAT STATE MACHINE TESTS PASS
  (ChatEntry slice markers intact).
- Addresses scrubbed (none in this diff); no secrets; repo public.
