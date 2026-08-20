# Flightdeck docs — index

How to find the right page. This directory holds reference material
(what the tool does today) and design/history (why it is shaped this way and
where it came from). Start here, then jump to the file you need.

## Current reference — what the tool does today

These describe shipped behaviour. Read these first.

| file | what it is | who it is for |
|---|---|---|
| [ROADMAP.md](ROADMAP.md) | Subprojects and milestones with stable ids; the plan of record for what is being built next | operator, anyone asking "what's next" |
| [INCIDENTS.md](INCIDENTS.md) | A log of real failures and the lessons they taught, newest first. Append with `flightdeck incident` | operator, reviewers |
| [config.example.yaml](config.example.yaml) | Connection-level settings (Telegram group id, MCP daemon url). Copy to `~/.flightdeck/config.yaml`. Telegram config lives here — there is no separate TELEGRAM doc | anyone wiring a machine |
| [registry.example.yaml](registry.example.yaml) | The registry shape: one entry per project binding repo ↔ board ↔ topic. Copy to `~/.flightdeck/registry.yaml` | anyone adding or auditing a project |

There is no separate COMMANDS or CONFIGURATION doc beyond the example files
above. The full command reference (every subcommand, every flag) lives in the
project's `README.md` and in the CLI itself (`flightdeck --help`), which is
always the ground truth because the CLI auto-discovers its own commands.

## Design / history — why it is this way

For context, not for day-to-day use.

| file | what it is | who it is for |
|---|---|---|
| [DESIGN.md](DESIGN.md) | The approved architecture — scope, non-goals, principles, the registry, each command. Ground truth for the shape of the tool | architects, contributors |
| [APPROACH.md](APPROACH.md) | A recommendation for putting Hermes (not the human) back in the driving seat via topic sessions + the flightdeck MCP | operator, anyone thinking about how the fleet learns |
| [FEATURES.md](FEATURES.md) | Historical brainstorm (2026-08-09) — superseded by shipped commands, kept for the reasoning | historians |
| [FEATURES-2.md](FEATURES-2.md) | Historical brainstorm round 2 — superseded by shipped commands, kept for the reasoning | historians |
| [assets/README.md](assets/README.md) | What lives in `docs/assets/` (currently the README banner) | contributors touching assets |

## Pointers

- **What is this tool / how do I use it?** → `../README.md` at the repo root.
- **What's the mental model (project, card, milestone)?** → [CONCEPTS.md](CONCEPTS.md).
- **How do I run tests / add a command / mutate safely?** → `../CONTRIBUTING.md` at the repo root.
