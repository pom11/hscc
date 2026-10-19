# hscc project docs — index

How to find the right page. This directory holds reference material
(what the tool does today) and design/history (why it is shaped this way and
where it came from). Start here, then jump to the file you need.

## Current reference — what the tool does today

These describe shipped behaviour. Read these first.

| file | what it is | who it is for |
|---|---|---|
| [COMMANDS.md](COMMANDS.md) | The full command reference: every subcommand, every flag. The CLI auto-discovers its own commands, so this is kept in sync with what `hscc project --help` actually exposes | anyone running the tool |
| [CONFIGURATION.md](CONFIGURATION.md) | Config + registry, field by field | anyone wiring a machine |
| [CONCEPTS.md](CONCEPTS.md) | The mental model: what a "card", "board", "project", "milestone" means | anyone learning the tool |
| [ROADMAP.md](ROADMAP.md) | Subprojects and milestones with stable ids; the plan of record for what is being built next | operator, anyone asking "what's next" |
| [INCIDENTS.md](INCIDENTS.md) | A log of real failures and the lessons they taught, newest first. Append with `hscc project incident` | operator, reviewers |
| [config.example.yaml](config.example.yaml) | Connection-level settings, e.g. the Hermes kanban DB path. Copy it into the hscc-project home as `config.yaml` | anyone wiring a machine |
| [registry.example.yaml](registry.example.yaml) | The registry shape: one entry per project binding repo ↔ board (↔ topic id). Copy it into the hscc-project home as `registry.yaml` | anyone adding or auditing a project |

The full command reference and the config reference are the two files above;
the example yamls are their starting points, not complete docs.

The "hscc-project home" is the directory these files are copied into — on disk
that is `~/.flightdeck/` (or `$HERMES_HOME/` when that is set). The directory
keeps its original name: renaming it would move real user state, so the
product name changed and the path did not. `hscc project init` creates and
seeds it for you.

## Design / history — why it is this way

For context, not for day-to-day use.

| file | what it is | who it is for |
|---|---|---|
| [DESIGN.md](DESIGN.md) | The approved architecture — scope, non-goals, principles, the registry, each command. Ground truth for the shape of the tool | architects, contributors |
| [APPROACH.md](APPROACH.md) | A recommendation for putting Hermes (not the human) back in the driving seat via topic sessions + the project MCP server | operator, anyone thinking about how the fleet learns |
| [FEATURES.md](FEATURES.md) | Historical brainstorm (2026-08-09) — superseded by shipped commands, kept for the reasoning | historians |
| [FEATURES-2.md](FEATURES-2.md) | Historical brainstorm round 2 — superseded by shipped commands, kept for the reasoning | historians |
| [BRAINSTORM-what-is-missing.md](BRAINSTORM-what-is-missing.md) | Design exploration (2026-08-17) on what the tool still lacked, incl. the self-update-daemon question — a brainstorm, not a spec; nothing here is a commitment | historians |

## `assets/`

One file: `assets/banner.png`, the banner image shown at the top of
[`../README.md`](../README.md). The README references it as
`docs/assets/banner.png`, so anything added here must keep that path stable —
replace the file in place rather than moving it.

## Pointers

- **What is this tool / how do I use it?** → [`../README.md`](../README.md).
- **The old-command ↔ `hscc project X` command mapping and naming notes**
  (kept so muscle memory and older transcripts still resolve) →
  [`../../docs/PROJECT-COMMANDS.md`](../../docs/PROJECT-COMMANDS.md).
- **How do I run tests / add a command / mutate safely?** → the pytest suite
  under `tests/` (each plugin runs its own isolated pytest process, see
  [`../../scripts/run_tests.sh`](../../scripts/run_tests.sh)).
