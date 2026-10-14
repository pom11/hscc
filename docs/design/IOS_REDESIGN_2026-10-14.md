# HSCC iOS redesign — design spec (Phase 1, 2026-10-14)

Status: **awaiting operator approval.** No implementation code in this PR.
Branch: `feat/ios-redesign-2026-10-14` (from main `0b890d72`, app 2.8.0).
Companion: `ios_redesign_mockup.html` (open on phone or Mac; light/dark toggle;
icon candidates at home-screen size).

Non-negotiables carried through every section:

- Chat protocol code (`StreamingChatStore`, `StreamHello`, `SessionEvent`,
  `SessionStreamCursor`, `StreamConnectionError`, `StreamingTranscript` data
  path) stays **behaviourally unchanged**; `ws_decode_check.sh` and
  `reconnect_check.sh` stay green. This is UI over the same seams.
- Every screen reachable today stays reachable (map in §8 — nothing merged or
  moved without a row here).
- New Swift files get registered in `project.yml` `sources:` (an unlisted file
  silently does not build — the StreamHello incident, 2026-10-13).
- The operator's phone is never the test target: compile + simulator only.

---

## 1. Today's problems (grounded in the 2.8.0 code)

### 1.1 Navigation

- Three-tab `TabView` (Projects / Cluster / Settings) forces 2–3 levels of
  pushes before most content: Settings (connection) crowds a tab beside
  content it has nothing to do with; fleet surfaces are buried inside
  ClusterView's long scroll; Ops/Sessions/Cards/Approvals/Cron/Logs/Memory/
  Templates are reachable only from deep scroll positions or hidden menus.
- `ConnectionBanner` is a full-width strip rendered as the first element of
  every tab (ContentView.swift:237-309) — a permanent horizontal bar eating
  transcript height, with prose ("Connected to <host>.") where a glyph +
  one tap should do.
- Deep links (t_136762f3) assume `selectedTab = .projects` + a `NavigationPath`
  push — the new shell must preserve `hscc://project/<name>`,
  `hscc://session/<project>`, `hscc://card/<id>` routing exactly.

### 1.2 Chat (the screen the operator lives in)

- The transcript rows are **boxed cards inside a padded column**
  (StreamingChatView): user/assistant rows carry their own rounded
  `surfaceRaised` backgrounds **on top of** the list's card background —
  nested cards (requirement 1 says: no).
- The turn-state banner ("working…" / "waiting for approval", t_7cc2e7d5) and
  the `ConnectionBanner` both stack above the transcript: up to three chrome
  bands before the first message.
- Tool-call rows expand **in place into a dump** (`renderArgs(pretty:)`,
  2-line clamp then full pre-wrapped block) rather than a tidy collapsible
  detail; reasoning renders as a generic system chip (kind label +
  `details` dump), identical shape to every other system kind — the operator
  cannot scan "reasoning vs working vs compaction" at a glance.
- Composer sits inside the safe-area with its own bordered container; with
  the keyboard up, the transcript does not use the freed height cleanly.

### 1.3 Theme consistency

Theme.swift already defines the token set (Palette/Semantic/Spacing/Corner,
`HS*` components) — better than a from-scratch problem — but adoption is
incomplete: per-view copies of `sectionCard` linger, ~10 raw numeric
`.padding(_, 14)`-style values remain in ProjectsView alone, corner radii
(`cornerRadius: 10` literals) bypass `Theme.Corner`, and several views
hand-build banners (ContentView dropped-queue overlay) instead of `StaleBanner`
/ `HSErrorLabel`. Light mode exists via dynamic colors but was clearly
designed dark-first: light surfaces get white-on-white shadows nowhere and
`surfaceElevated` light is a flat grey with no elevation signal.

### 1.4 App icon

**There is no AppIcon and no asset catalog at all** (no `*.xcassets` anywhere;
project.yml wires no `ASSETCATALOG_COMPILER_APPICON_NAME`) — iOS renders the
placeholder "H" on the home screen. This is a real gap, not a style gap:
shipping a proper icon set is part of this redesign (§7).

<!-- AUDIT_APPEND: full per-screen audit from parallel review lands here as evidence -->
