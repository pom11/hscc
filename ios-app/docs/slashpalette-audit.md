# Slash-Command Palette — Audit / Build Report

Card: t_6f91edd3 — Chat: slash-command palette with autocomplete (part 1 of 2: palette only).

## Server source of truth

The palette command list comes from `GET /v1/commands` (added in `cdf4e26`),
which is sourced from the authoritative `hscc-commands` plugin `register()`.
So the palette list is NOT a hardcoded Swift array — it reflects whatever the
server registers, and cannot rot.

Response shape (confirmed against `hscc-api/routes_commands.py` +
`hscc-api/tests/test_routes_commands.py`):

    {
      "commands": [ { "name": "cluster", "description": "Show HSCC cluster status.", "takes_args": false }, ... ],
      "speak": "N slash command(s) available."
    }

Auth: `Authorization: Bearer <token>` (reads are authenticated).

## Implemented

- HSCCClient.commands() -> GET /v1/commands (typed CommandsResponse).
- Models: SlashCommand + CommandsResponse.
- SlashCommandPalette — reusable composer-attached palette.
- Wired into BOTH chat composers (OrchestratorChatView + StreamingChatView).

## Build check

`ios-app/scripts/build_check.sh` — full compile of every target from project.yml's
real file sets (not just -typecheck):

    HSCC: 59 files, 0 error(s), 0 warning(s)
    HSCCWidgets: 6 files, 0 error(s), 0 warning(s)
    HSCCLiveActivity: 4 files, 0 error(s), 0 warning(s)
    HSCCLiveActivitySession: 4 files, 0 error(s), 0 warning(s)
    full compile clean, 0 warnings (compile only — never built or run on a device)

**0 errors, 0 warnings.**

## Behaviour

- Typing "/" (at a command position — start of draft or after whitespace)
  shows the palette above the composer.
- The list is fetched once from `GET /v1/commands` (lazy, on first appear) —
  server-driven, not hardcoded.
- Filtering: name `hasPrefix` first, then `contains`, ties alphabetical; empty
  query (just "/") lists all.
- Selecting a command substitutes `/name ` into the draft (replacing the
  in-flight `/query` word, preserving any earlier leading text) and dismisses.
- If the catalog can't be fetched (unconfigured / offline / server degraded to
  `{speak: "Slash-command list unavailable."}`), the palette stays hidden — it
  never shows a fabricated or stale list.
- Pure logic (`SlashPaletteLogic`) is isolated from SwiftUI so it is testable
  from a headless CLI (repo convention); `SlashCommand` `takes_args` maps to
  `takesArgs` per the server contract.

## Files

- `Sources/HSCC/Views/SlashCommandPalette.swift` (new) — palette + pure logic
- `Sources/HSCC/Models.swift` — `SlashCommand`, `CommandsResponse`
- `Sources/HSCC/HSCCClient.swift` — `commands()`
- `Sources/HSCC/Views/OrchestratorChatView.swift` — wired into composer
- `Sources/HSCC/Views/StreamingChatView.swift` — wired into composer
- `project.yml` — registered the new source file

