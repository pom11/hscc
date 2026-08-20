# Changelog

## [Unreleased] — flightdeck daemon (monitoring/logging only, never mutates)

### Added
- `flightdeck daemon` — a persistent background process that WATCHES, LOGS,
  and optionally NOTIFIES, keeping `standup`'s signal (fleet health, board
  freshness, orphaned boards, version drift) current without running a command
  by hand. Hard scope: read-only. It never merges, applies, archives, closes,
  or otherwise mutates any project's state — it mirrors HSCC's `escalate`
  (detect + report, human decides) pattern, not its apply-side commands.
  Subcommands: `start`/`stop`/`status`/`check [stream]`/`watch [stream]`/
  `log`/`notify`/`install`/`uninstall`. Four independently-scheduled check
  streams (`fleet` 60s, `freshness` 300s, `orphans` 300s, `version` 3600s),
  persisted per-stream state under `~/.flightdeck/daemon/` that survives
  restarts, and a launchd auto-start installer gated behind an explicit
  `--apply`.

## [0.6.0] — review actually closes cards, cwd auto-detection, a real bug sweep

### Fixed
- `review --apply` claimed "card closed" without ever archiving it —
  `args.close_card` was never wired to a real implementation in production
  (only tests injected one), so the print fired unconditionally while the
  kanban row stayed open. Now a real default archives via `kanban_db.
  archive_task` and checks its return value, wired through the shared
  `run()` entry point so both the CLI and the MCP `flightdeck_review` tool
  are covered. A duplicate unconditional "card closed" print is removed.
- `migrate-card` had the identical bug shape: `archive_task`'s bool return
  on the original card was discarded, so an already-archived or vanished
  source card still printed "archived with a pointer" and exited 0. Now
  raises and reaches the existing PARTIAL-migration path instead of
  claiming false success.
- `start --apply` printed "applied: released 0 card(s)" and exited 0 even
  when every assigned card failed to release — a total failure indistinguishable
  from "nothing was due." Now exits 3 (matching `decompose`'s
  `--apply`-created-nothing gate) and prints the failure to stderr; a
  partial success still exits 0.
- `review` attributed a card to a project by board slug, while every other
  command (`reconcile`, `qa`, `standup`, `hygiene`, ...) used the card's
  `workspace_path`. The two could disagree for a card whose workspace
  pointed at a different project than its board. Unified on
  `workspace_path` everywhere; the divergent `_project_for_board` helper
  is removed.
- `release`'s version-ordering check parsed only digit runs, so
  `1.8.1-rc1` sorted *above* its own `1.8.1` GA — a prerelease could be
  released after its GA had already shipped. Version parsing is now
  semver-aware: a prerelease sorts strictly below its same-core GA.
- Two `qa` tests read the real `~/.flightdeck/manual-qa.yaml` instead of an
  isolated tmp path, so they failed on any machine with real manual-QA
  entries. Isolated, matching the pattern already used elsewhere in the
  suite.

### Added
- Commands that take a `project` argument (`qa`, `metrics`, `verify`,
  `report`, `message send/read/dispatch`, `review`'s verify+gate form)
  now auto-detect the project from the current working directory when the
  argument is omitted, matching cwd against each registered project's repo
  (closest match wins). An explicit argument always overrides detection; a
  one-line note prints when auto-detection kicks in
  (`using project 'flightdeck' (detected from cwd)`). Purely additive — no
  change to any command's existing no-argument (fleet-wide) behavior.
- Every command's `--help` now carries a real, working usage example
  (`epilog`), including nested subcommands (`roadmap add/move/done/adopt`,
  `topics rename/create/bind/unbind`, `message send/read/dispatch/
  broadcast`, `project sync`). The top-level `flightdeck --help` gets a
  short "most people start with" pointer.

### Changed
- README rewritten for a first-time reader: leads with a plain-language
  problem statement, adds a "day in the life" walkthrough with real
  example output, an "if you get stuck" section near the top, and defines
  internal terms (board, card, project, milestone) on first use.

## [0.5.0] — post-merge manual-QA tracking

### Added
- `flightdeck qa add <project> "description" [--card CARD_ID]` and
  `flightdeck qa done <id>` — track post-merge items that still need a human to
  physically verify them (e.g. printer byte output on real hardware, real
  production data in an external system), which previously vanished from every
  flightdeck view once the card was merged and archived. Entries live in
  `~/.flightdeck/manual-qa.yaml`, are validated against the registry, and are
  never deleted — checked ones (via `qa done`) stay for history but drop out of
  the default view. `flightdeck qa` now prints a `NEEDS MANUAL VERIFICATION`
  section beneath the pre-merge queue (shown even when the queue is empty), and
  `--json` emits `{"queue": [...], "manual_qa": [...]}`.

## [0.4.1] — dispatch anchors cards in the project's repo

### Fixed
- `message dispatch` created every card as an unanchored `scratch` workspace
  (`workspace_kind='scratch'`, `workspace_path=NULL`), regardless of the
  target project's registered repo — a worker claiming it got a directory
  with no access to the project's actual git history/files. Now anchors the
  card as a `worktree` in the project's repo, matching `migrate-card`'s
  existing pattern. A project with no `repo` configured is refused up front
  (`flightdeck project repair <name>`) rather than silently degrading to
  scratch again.

## [0.4.0] — MCP parity, message dispatch body + apply gate

### Added
- MCP tools for the remaining report/inspection commands, closing the gap where
  only 17 of flightdeck's ~24 CLI commands were agent-callable. New read-only
  tools: `flightdeck_why(card_id)`, `flightdeck_metrics(project, since)`,
  `flightdeck_topics_list()`, `flightdeck_topics_audit()`. New apply-gated
  tools (the `apply` param maps 1:1 onto each command's own CLI `--apply`
  gate, defaulting to `False`): `flightdeck_report(project, since, all,
  backfill, apply)`, `flightdeck_incident(symptom, project, fix, cause,
  lesson, apply)`, `flightdeck_hygiene(apply, similarity)`. Each is a thin
  adapter over the same `module.run(args, registry_path)` the CLI dispatches
  to — no command logic is reimplemented in the MCP layer.
- `message dispatch` now accepts `--body-file PATH` so a dispatched card can
  carry a real spec (VERIFY line, acceptance criteria, concrete file/line
  references) instead of only the announcement text. `--body-file -` reads the
  full body from stdin. When `--body-file` is given, `--message` is used ONLY
  as the short Telegram announcement and may differ from the (longer) card
  body. Omitting `--body-file` keeps today's behavior: the body defaults to the
  announcement text.
- `flightdeck_message_dispatch` MCP tool registered, mirroring
  `flightdeck_migrate_card`: `(project, task, body, assignee, message, apply)`.

### Changed (breaking for `dispatch`)
- **`message dispatch` now defaults to a dry-run plan and requires `--apply` to
  actually create the card and send the announcement.** Previously it mutated
  immediately. This aligns it with every other mutating flightdeck command
  (`migrate-card`, `roadmap adopt`, `release`, ...) — `dispatch` also creates a
  durable card and anchors a worktree, a stronger action than `message send`
  (which remains immediate-by-design). Existing scripts or muscle-memory that
  called `dispatch` expecting immediate action must add `--apply`. The dry-run
  prints the resolved board, card title, full body, and announcement text so it
  is never a black box.

## [0.3.0] — live cluster monitor, migrate-card reaches archived boards

### Added
- `monitor [--time N]` — a long-running terminal loop (default refresh 5s) that
  redraws every RUNNING/CLAIMED card across every registered board, grouped by
  project, with id/title/status/assignee and elapsed time since claimed.
  Prints a single "cluster idle" line when nothing is active. Read-only, never
  writes to a board; a single bad board is reported inline
  (`(unreadable: <board>)`) without killing the loop. Not an MCP tool — it's
  an interactive loop that deliberately never returns.

### Fixed
- `migrate-card` couldn't act on the exact cards `legacy-cards
  --include-archived-boards` surfaces: `find_card` only scanned live boards,
  so an archived-board card 404'd with "not found on any board" the moment
  you tried to actually migrate it. `find_card` now falls back to the
  archived-board scan when the live lookup misses; live lookups pay no extra
  cost. Verified end to end by migrating the four remaining `ecofire` legacy
  board cards (Referate T2/T3 work) to `ecofire-app`.

## [0.2.3] — card migration, MCP registration, honest topics

### Added
- `legacy-cards` — read-only: surfaces every card on an unmapped or archived
  board. Suggests a target project only when `workspace_path` mechanically
  resolves to one; never guesses from title text. `--include-archived-boards`
  scans Hermes' `_archived/` board directories.
- `migrate-card <id> --to <project> [--apply]` — safely re-homes one card to
  the right project's board: creates a `[migrated]` copy with a provenance
  line, archives the original (never deletes it) with a pointer to the new
  card, refuses an active (running/claimed) source card and unknown target
  projects. Both commands are registered as MCP tools.
- `flightdeck-mcp` registered as an MCP server for both Hermes
  (`~/.hermes/config.yaml` `mcp_servers`) and Claude Code, so the orchestrator
  can drive flightdeck directly through a Telegram topic session.
- `topics audit` now honours the `ignored_topics` list `project sync
  --ignore-topic` already persists — a topic marked known-permanent stopped
  being suppressed by one of the two commands that report it.

### Fixed
- **Release commit step only staged the `VERSION` file.** `bump` correctly
  wrote both `VERSION` and `pyproject.toml`'s `[project] version` (and
  reported both in `files_written`), but `commit` hardcoded `git add
  <version_file>` — so v0.2.2 shipped with the two version sources
  disagreeing in git history, caught only after the fact. `commit` now stages
  every file `files_written` actually contains.
- A test (`test_run_attaches_seams`) reached the real, unmocked
  `kanban.list_archived_board_cards`, which imports Hermes' `hermes_cli` and
  fails on any machine without a local Hermes checkout — including every CI
  runner. `list_cards` was already mocked; this call wasn't.


### Added
- `docs/TELEGRAM.md` — documents the Telegram MCP daemon contract: the tools
  it must expose, the `http://127.0.0.1:8787/mcp` URL flightdeck expects, and
  that it is configured via `telegram.mcp_url` / `FLIGHTDECK_MCP_URL`.
- `docs/COMMANDS.md` and `docs/CONFIGURATION.md` — every command and every
  config/registry field, derived from real `--help` output and
  `core/registry.py` rather than written from memory.
- `docs/CONCEPTS.md`, `docs/README.md` (an index), and `CONTRIBUTING.md`.
- README `Requirements` section: Python 3.10+, git, and the two *optional*
  dependencies (a Hermes kanban DB and the Telegram MCP daemon), with what
  degrades when each is absent.
- README rewritten for a public reader: ~265 lines (from 739), quick start
  with real command output, the full work loop in one place, `pip install`
  replaced everywhere by the correct `pip install git+...` (the name
  `flightdeck` is taken on PyPI; this project is git-install only).

### Fixed
- A Telegram command with an unreachable daemon now errors with a message
  naming the daemon, the exact configured URL, and pointing at
  `docs/TELEGRAM.md` — never a bare traceback or generic connection error.
  Detected via the shared probe helper, so the failure is self-diagnosing.
- Docs: config example's Telegram-command list corrected (`standup` was
  wrongly named as a Telegram command).
- CI: the workflow installed the package but never declared `pytest` as a
  dependency, so every run failed with `No module named pytest`.
- CI: the `_open_hermes_db` probe verified a kanban DB with `SELECT 1`, a
  constant expression that does not force SQLite to validate the file's
  schema page on every libsqlite build — a corrupted `kanban.db` could read
  as reachable on Linux while correctly failing on macOS. Now queries
  `sqlite_master`, which forces a real page read.
- CI: `tomllib` (stdlib only from Python 3.11) and the `ExceptionGroup`
  builtin (only from 3.11, PEP 654) were used unconditionally in three test
  files despite `requires-python = ">=3.10"` including the 3.10 matrix
  entry. Backported via `tomli` / `exceptiongroup` for `python_version<'3.11'`.
- Five tests were coupled to the operator's real `$HOME` (assumed a Hermes
  kanban DB did or didn't exist, or that `~/dev/flightdeck` resolved) and
  failed on any other machine, including CI. Made hermetic.


## [0.2.1] — honest checks

### Added
- `init` — one-command bootstrap: seeds config/registry/templates (never
  overwriting), checks the environment, prints the MCP registration snippet.
- `metrics` — first-time-pass, stall rate, review latency, throughput, rework.
  Reads archived history and states the sample size on every figure.
- `why <card>` — one card's story across kanban and git, with a verdict
  distinguishing starved / working / awaiting-review / landed.
- `incident` — appends a dated entry to `docs/INCIDENTS.md`, seeded with the
  five incidents that produced these rules.
- `doctor` learning-pipeline checks: is the memory store writable, has it gone
  stale, and is the augmentation model actually served.
- GitHub Actions: pytest on 3.10-3.13 plus a packaging build, failing if the
  suite exceeds 30s.

### Fixed
- `release --apply` now bumps EVERY version source. Cutting v0.2.0 failed its
  own post-install verification because `VERSION` and `pyproject.toml` are
  separate, and the installed package still reported the previous version.
- Endpoint probes use a method the endpoint accepts. A bare GET against a
  POST-only endpoint reported healthy services as down -- three times, in two
  commands -- so both now share one probe, and any HTTP response proves
  reachability.
- `doctor` watches the memory files that actually move (per-profile and global
  `MEMORY.md`), not the inert provider DB.
- `metrics` reads archived cards; completed work is archived, so it previously
  reported n=0 for every figure.
- `mcp` 2.0 renamed `FastMCP`, and setuptools 70 rejects PEP 639 string
  licenses -- both broke `main` while branch tests passed.


## [0.2.0] — the fleet learns

### Added
- **MCP server** (`flightdeck-mcp`) — 15 tools so Claude or any MCP client can
  drive flightdeck directly. The 6 mutating tools default to `apply=False`.
- `ingest` — drafts a project's ROADMAP from Hermes skill references, the repo
  (README, docs, git subjects) and its Telegram topic. Synthesis runs as a
  kanban card, so bulk context never goes through chat.
- `roadmap adopt` — promote a reviewed draft with a diff and a backup.
- `roadmap progress` — counts roadmap items AND linked cards; a milestone with
  every item ticked reads complete, not "not started".
- `decompose --milestone`, `start` (concurrency-aware), `qa --watch/--notify`.
- `report` / `report --all --backfill` — post a learnable summary to a
  project's topic, so board work reaches the orchestrator.
- `project sync --create-boards` — a Hermes board per project, with
  `default_workdir` bound to the repo.
- Public-release readiness: MIT LICENSE, packaging metadata, and the Telegram
  group id and MCP url moved out of source into `~/.flightdeck/config.yaml`.

### Changed
- `release --apply` now executes the full sequence: bump, commit, tag, push,
  `gh release`, install, and post-install verification. It stops at the first
  failure, and a release it cannot verify reports UNVERIFIED rather than OK.

### Fixed
- Cards are attributed to projects by repo path, not board slug — the shared
  `default` board holds work from every project.
- `reconcile` no longer treats "ancestor of main" as proof work landed; an
  unstarted branch looks identical. It proposed closing three running cards.
- The ask seam correlates the reply to the prompt (watermark + poll) and waits
  for the answer rather than the acknowledgement.
- Telegram: both `mcp` SDK client names and stream arities; `group` sent as a
  string; a rejected call raises instead of rendering as a clean result.
- `mcp` 2.0 renamed `FastMCP`; setuptools 70 rejects PEP 639 string licenses.
- CLI dispatch uses the subcommand name argparse registered, so `lint-cards`
  (in `lint.py`) is reachable.


## [0.1.0] — trustworthy digest

### Added
- `standup` — daily digest: NEEDS YOU / FAILING / STALE / RUNNING / DRIFT, with
  `--watch`, a shared renderer, and starved-vs-working detection.
- `reconcile` — closes cards whose branch landed; the missing transition behind
  the phantom backlog. Dry-run by default.
- `doctor` — self-check across repo, board, topic and Telegram transport;
  exits non-zero when anything is unverifiable.
- `review --queue`, `qa`, `verify`, `hygiene`, `lint-cards` — the review loop.
- `ask` — prompt templates with project context auto-filled from the registry.
- `decompose` — cluster-backed card proposals, gated on card quality.
- `project new/sync/repair`, `topics` CRUD + overwrite audit, `message`.
- `release` — preconditions and dry-run plan (execution lands next).
- `roadmap show/add/move/done` over a plain versioned `ROADMAP.md`.

- Milestone tracking: `ROADMAP.md` subprojects + milestones with stable ids,
  `decompose --milestone` stamping `MILESTONE:` into every card it creates,
  `roadmap progress` linking cards to milestones, and `start` for
  concurrency-aware dispatch.
- `reconcile`, `hygiene` and `qa` attribute cards by repo path like `standup`,
  so a shared board no longer misattributes work.

### Fixed
- Telegram: accept both `mcp` SDK client names and both stream arities (2.0
  renamed and changed arity); send `group` as a string.
- Telegram: a rejected tool call raises instead of parsing into an empty list —
  `topics audit` had reported "audit clean" while every call was failing.
- `reconcile` no longer treats "ancestor of main" as proof the work landed. An
  unstarted worktree branch is an ancestor too, and reconcile proposed closing
  three cards that were running at the time. A branch must appear as the second
  parent of a merge on main, and active cards are never closed.
- CLI: dispatch on the subcommand name argparse registered, not the module
  name, so `lint-cards` (in `lint.py`) is reachable; a command module that
  fails to import now says so instead of reporting "not implemented yet".

### Known issues
- `release` performs preconditions and a dry-run plan only; execution (bump,
  tag, push, gh release, install verification) is not implemented yet.
- `qa --watch` / `--notify` are in progress.
