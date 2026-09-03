# User-Facing Error & Empty-State Strings — Swift View Files

Extracted from the HSCC iOS-app view files. Machine-parseable table of every
user-facing error and empty-state string literal.

## Extracted Strings

| File:Line | String (verbatim) | Kind | Q4 (what went wrong) | Q5 (what to do next) | Flags |
|---|---|---|---|---|---|
| MemoryView.swift:69 | `Connect to your cluster` | empty-state | yes | no | — |
| MemoryView.swift:71 | `Set the host, port, and token in Settings to inspect memories.` | empty-state | yes | yes | — |
| MemoryView.swift:124 | `Something went wrong.` | error | no | no | vague (generic fallback) |
| MemoryView.swift:151 | `This profile holds no memories.` | empty-state | yes | no | — |
| OpsView.swift:136 | `No checks reported.` | empty-state | yes | no | — |
| ProjectsView.swift:81 | `No projects registered.` | empty-state | yes | no | — |
| ProjectsView.swift:109 | `No projects registered.` | empty-state | yes | no | — |
| ProjectsView.swift:311 | `Not a git repo.` | empty-state | yes | no | — |
| ProjectsView.swift:346 | `Needs eyes — tap the Board section to triage.` | empty-state | yes | yes | — |
| ProjectsView.swift:355 | `Nothing blocked` | empty-state | yes | no | — |
| ProjectsView.swift:443 | `Compaction at risk` | error | yes | no | — |
| ProjectsView.swift:701 | `No open cards on this board.` | empty-state | yes | no | — |
| QRScannerView.swift:95 | `Camera access is off` | error | yes | no | — |
| QRScannerView.swift:97 | `HSCC needs the camera only to scan the setup QR code from \`hscc api status\`. Turn on camera access in Settings to continue.` | error | yes | yes | — |
| QRScannerView.swift:120 | `Camera unavailable` | error | yes | no | — |
| SettingsView.swift:164 | `Scan rejected` | error | yes | no | — |
| SessionHistoryView.swift:138 | `Couldn't load older events` | error | yes | yes (Retry button) | — |
| SessionHistoryView.swift:215 | `Something went wrong.` | error | no | no | vague (generic fallback) |

## Dynamic / Non-literal error carriers (content is runtime HSCCError text)

| File:Line | Context | Kind | Notes |
|---|---|---|---|
| SettingsView.swift:170 | `Text(scanError ?? "")` under alert "Scan rejected" | error | dynamic |
| SessionHistoryView.swift:141 | `Text(message)` in "Couldn't load older events" banner | error | dynamic |
| AutodownView.swift:169 | `Text(message)` in wake banner | loading | dynamic |

These use `HSCCError.localizedDescription` values at runtime; Q4/Q5 cannot be
classified from source.

## Excluded (not error/empty-state)

- Loading indicators: `Waking the fleet…` (AutodownView:167), `Testing…` (SettingsView:105), `Older . . .` (SessionHistoryView:114), ProgressView instances.
- Confirmation prompt: `Connect to \(code.host):\(code.port) and set a new token (from the scanned code)?` (SettingsView:161).
- Healthy/informational status: `Compaction healthy` (ProjectsView:459), `Binding OK` etc.
- Static labels/section headers: `Memories`, `Profile`, `Host`, `Daemon`, `Git`, `Session health`, `Chat`, `Status`, etc.
