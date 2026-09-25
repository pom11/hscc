# README review: docs + hscc-skills + hscc-project (+ its docs) (t_0ae232a4)

Card: PART 2 README REVIEW (3/5) — docs/README (9 lines) + hscc-skills (18 lines)
+ hscc-project/README + hscc-project/docs/README. Verify TRUE after telegram
removal, main-only cutover, Rich CLI theming (hscc-project fully converted in
the Rich CLI epic). Fix or DELETE. VERIFY EVERY COMMAND BY RUNNING IT.
PUBLIC repo — scrub LAN/tailnet to 100.64.0.1 in any commit. Do NOT dirty
primary checkout.

SCOPE (per card title):
- docs/README.md (9 lines)                      -> VERIFIED, no change
- hscc-skills/README.md (18 lines)              -> VERIFIED, no change
- hscc-project/README.md (379 lines)            -> REWRITTEN
- hscc-project/docs/README.md (39 lines)        -> REWRITTEN
- (coupled) docs/PROJECT-COMMANDS.md            -> 2-line fix (command map +
  doctor note), because the rewritten hscc-project README links to it

## Context: what changed since these were written

Telegram was REMOVED from the fleet (release 2.0.0; commits 5d9c505/f91924c;
hscc-project specifically 8c2af35 "drop Telegram delivery from hscc-project
(flightdeck)"). Facts confirmed from the current tree:

- core/telegram.py and commands/topics.py DELETED; docs/TELEGRAM.md DELETED.
- `message` kept but no delivery (honest stub in help: "no delivery — Telegram
  removed"); `qa --notify`, `ingest --limit`, `ingest --ask-inline` are
  "deprecated ... Telegram is removed" flags.
- `project_lifecycle` create-project topic step = 'skipped (Telegram removed)'.
- doctor output: `topic [ok] topic N recorded (Telegram removed; unverifiable)`.
- init env check no longer lists telegram-daemon.
- MCP server: 24 tools (was 15 in the old README).

Rich CLI theming: hscc-project fully themed — read-only outputs render Rich
boxes/tables (verified live: doctor, standup, review --queue, project sync,
init, project list all render Rich panels/boxes).

## docs/README.md (repo root, 9 lines) — VERIFIED ACCURATE, no change

- `.gitignore:48` has `docs/superpowers/` -> "kept local and untracked (under
  docs/superpowers/, gitignored)" TRUE.
- `../CHANGELOG.md` exists at repo root; repo is public github.com/pom11/hscc
  with git tag v2.1.1 (GitHub releases) -> top-level CHANGELOG + GitHub
  releases claim TRUE.

## hscc-skills/README.md (18 lines) — VERIFIED ACCURATE, no change

- `../install/README.md` exists; `install/hscc-skills/` is the vendored
  source-of-truth (dir contains: brainstorming, caveman, devops, executing-
  plans, hscc, hscc-cluster, hscc-lessons, hscc-model-onboard, sdlc-review,
  systematic-debugging, test-driven-development, verification-before-
  completion, writing-plans).
- hscc.py COMMANDS map = {install, install-skills, install-templates, status,
  uninstall} — README documents install/install-skills/status/uninstall: all
  real. `_find_skills_source` walks up from `__file__` to <repo>/install/
  hscc-skills (matches README line 17-18). SKILLS_DEST=~/.hermes/skills,
  TEMPLATES_DEST=~/.hermes/templates.
- RAN against a temp HERMES_HOME=/tmp/hscc-skills-verify-t0ae232a4 (real
  ~/.hermes never touched):
  - `hscc.py install`         -> 37 installed (skills+tmpl; templates 6 missing
    from source — pre-existing, no vendored template files)
  - `hscc.py status`          -> 12 OK
  - `hscc.py install-skills`  -> "0 file(s) installed, rest unchanged"
    (hash-skip works -> README "hash-skipping already-installed files" TRUE)
  - `hscc.py uninstall`       -> "Removed 12 skill(s) and 0 template(s)"
  - `hscc.py status` post     -> skills NOT INSTALLED again
- NOTE: templates source (Resources/templates) is empty in the repo — the
  installer's template support is real but no template files are currently
  vendored. README's "skills + templates" is capability-accurate; not a README
  error. Left unchanged.

## hscc-project/README.md (379 -> ~200 lines) — REWRITTEN

Was a relic of the standalone flightdeck package, created at the flightdeck->hscc
relocate (e34d150) and never updated since. Post-telegram-removal it was FACTUALLY
WRONG in many places:
- framed the tool as standalone `flightdeck <cmd>` everywhere (banner aside);
- "Telegram thread per project", "board or Telegram daemon unreachable",
  `qa --notify` "pings your Telegram", "Telegram MCP daemon" requirement
  section, docs list included TELEGRAM.md (deleted), scope "using Hermes
  (kanban) and Telegram (topics)";
- init sample output showed a telegram-daemon env check that no longer runs;
- MCP section claimed "15 tools" (actual: 24).

REWRITE (now titled "Flightdeck (hscc project)"):
- framed as the `hscc project ...` verb group; standalone console scripts no
  longer how you use it;
- all Telegram delivery content removed; requirements = Python 3.10+ + git +
  Hermes kanban DB;
- MCP section updated to 24 tools (verified in code: 11 read-only + 13
  mutating @mcp.tool() decorators);
- kept the valuable conceptual material (what it does, quick start, day-in-
  the-life, work loop, what you get, why this exists, two design rules, scope);
- docs list drops TELEGRAM.md, adds PROJECT-COMMANDS.md mapping link;
- banner image restored at top (keeps docs/assets/README.md accurate);
- role of "topic" id noted as recorded-but-unverifiable where relevant.

Every command the rewritten README documents was VERIFIED by running:
- `hscc project --help` (full 26-subcommand surface)
- `--help` per command: init, standup, project, review, qa, why, doctor,
  verify, release, ingest, roadmap, decompose, start, report, message,
  archive-sessions, map-sessions, daemon, metrics, monitor, hygiene,
  reconcile, lint-cards, incident, ask, update, legacy-cards, migrate-card
- read-only RUNS: `hscc project doctor` (Rich panel, 100.64/operator paths
  NOT included), `init` (dry-run, env check no longer lists telegram-daemon),
  `project list` (Rich table), `review --queue` (Rich box, real awaiting
  queue), `project sync` (dry-run, PARTIAL/ORPHAN REPOS), `standup` (read-only,
  Rich digest, exit 0).
- not run (mutating, would change live state): project sync --apply,
  decompose/start/release/reconcile/hygiene --apply, message dispatch, etc.

## hscc-project/docs/README.md (39 lines) — REWRITTEN (index)

Old version was stale + self-contradicting:
- line 15: config.example.yaml described as "Telegram group id, MCP daemon
  url" — wrong post-removal;
- line 18-21 claimed "There is no separate COMMANDS or CONFIGURATION doc
  beyond the example files above" while COMMANDS.md (50K) and CONFIGURATION.md
  (9.7K) ARE present — contradiction;
- pointed at ../CONTRIBUTING.md which does NOT exist at repo root;
- listed TELEGRAM.md? (docs/TELEGRAM.md deleted) — no longer in the list.
Rewritten index: COMMANDS.md + CONFIGURATION.md + CONCEPTS.md as the current
reference, others as design/history, fixed pointers (PROJECT-COMMANDS.md,
scripts/run_tests.sh instead of the dead CONTRIBUTING.md link).

## docs/PROJECT-COMMANDS.md (coupled) — 2-line fix

Command map row still listed `topics` (deleted) -> removed.
doctor description "self-checks ... kanban, and Telegram daemon" -> now
"kanban" + note topic id unverifiable. (This doc is the link target of the
rewritten hscc-project README; fixing it directly supports this card.)

## Flagged follow-ups (NOT done here — out of README scope / not this card)

- hscc-project/docs/config.example.yaml STILL documents a `telegram:` section
  (enabled / group_id / mcp_url + FLIGHTDECK_TELEGRAM_* env vars) that was
  REMOVED from core/config.py — misleading for anyone wiring a machine.
- hscc-project/docs/registry.example.yaml documents `topic`/`topic_name`
  "Telegram topic id ... for the audit" — the field survives (doctor shows it)
  but the framing/telegram references are stale.
- CLI help strings still carry telegram remnants (e.g. `incident --help`
  example `--fix "ran topics bind"` references the deleted `topics` command;
  `project sessions` help says "orchestrator + telegram"). These are CODE, not
  READMEs — a separate cleanup task belongs to a code card, not this README card.
- `hscc project project sync` help/next-steps still prints `flightdeck project
  sync` / `flightdeck-mcp` (internal console name), cosmetic.

## Verification

- `hscc-project` pytest suite: 1351 passed (no README/docs assertions
  broken). Docs-only changes, no code touched.
- Changed docs grepped: no real LAN/tailnet addresses, no operator repo
  paths in committed docs (samples use ~ and /... placeholders). No AI
  attribution added.

## Merge / push / deploy (recorded per process)

- branch wt/t_0ae232a4 based on main (clean);
- commits on branch: 579ef26 (audit scaffold), 591d73c (README rewrites) + final
  report commit (this file).
- merged: `git merge --ff-only wt/t_0ae232a4` from primary checkout (main is
  checked out in the primary, not this worktree).
- pushed: `git push origin main`; then `git rev-list --count origin/main..main` = 0.
- deployed: `python3 hscc-bootstrap/install_payload.py` exit 0; deployed copy
  verified.
- change is docs-only (markdown); no code/test touched.
