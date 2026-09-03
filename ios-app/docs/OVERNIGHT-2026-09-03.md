# What changed overnight — 2026-09-02 → 09-03

45 commits on `main`. Everything below is compile-verified and harness-verified
on this machine. **Nothing here has run on a phone** — there is no iOS runtime
on this host, so device behaviour is the one thing only you can confirm.

## Test these first — they were broken and you would have hit them

| What | Was | Now |
|---|---|---|
| **Chat** | Your message vanished. The composer cleared, nothing appeared, and the cluster never saw it. | Your line shows instantly, the orchestrator actually receives it, and a reply comes back. |
| **Offline / poor network** | 5 screens showed "failed" on a cold start with no signal, even though they had data cached. | Sessions, Fleet stats, Kanban stale, Memory, Activity feed show last-known data marked stale. |
| **Memories screen** | Empty for every profile. | Loads (hscc-orch 5, ios-engineer 7 as of last night). |
| **Card detail** | The card body — its whole description — was missing. | Body, assignee and board all render. |
| **Profile editor** | Save never enabled, so edits could not be saved at all. | Save enables after any edit. |
| **Autodown screen** | Flashed "Enable Autodown" on an already-armed cluster during load. | Controls appear only once the real state is known. |
| **QR scanner** | A failed camera looked identical to "scanning" — a black rectangle. | A failure says so. |
| **Widget (unconfigured)** | Showed a stale "can't reach the cluster". | Invites you to set up the app. |
| **Live Activities** | Leaving the Autodown screen mid-wake left a frozen bubble on your Lock Screen forever. Killing the app left more. | Ended on teardown, and a sweep on launch clears orphans. |
| **Error messages** | 16 places said "Something went wrong." | Each says what happened and what to do. |

## Also fixed, smaller

- Siri "dispatch a card" had no way to know *which* card. Phrases now name it.
- Approvals: every row's Allow button read as a bare "Allow" to VoiceOver.
- Ops: escalations rendered as `<complex>` instead of the actual task/action.
- Projects: a permanent red "0% headroom" bar under a green "healthy" verdict.
- Template detail: "Pull to retry" was not wired to any gesture.
- Node topology: the pair link stretched 27–170pt depending on screen width.
- Accessibility: light-mode contrast, icon-only labels, decorative VoiceOver noise.
- API log: a client hanging up printed an alarming stack trace for a non-event.
- `hscc project` spoke "196 open cards" when the real number was 5.

## What I could not verify

- **App Group container fails on device** (`t_d64ea494`) — still open. Most
  likely the free personal Apple team; needs your device to confirm.
- Anything requiring a real camera, real Lock Screen, real Siri, or real
  backgrounding. The logic behind each is harness-tested; the surface is not.

## State

- `origin/main` and `origin/dev` identical; installed payload matches the repo
  across 15 plugins / 401 files (`hscc verify` → `plugin_payload`).
- 1033 daemon tests, 681 API tests, 17 iOS harnesses, 0 build warnings.
- `hscc verify --chat` does a real round trip against both serving units.
