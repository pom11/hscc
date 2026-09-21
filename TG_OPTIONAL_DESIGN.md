# Make Telegram fully optional — design + implementation log

Task: t_1a0a47e8 — every hscc command works fully with Telegram disabled.
This file records the design decisions and the implementation state as I go.
Final deliverable is the committed code in this worktree (branch wt/tg-optional).

## The switch

Name: `telegram.enabled`
Where: `~/.flightdeck/config.yaml` under the existing `telegram:` mapping,
       alongside `group_id` and `mcp_url` — same file, same code path, one
       switch for the whole fleet (config.py:44 `DEFAULT_CONFIG`).

Why this name/location:
- It is a single, fleet-wide boolean with an obvious default. The other
  telegram connection knobs already live here (`telegram.group_id`,
  `telegram.mcp_url`, config.py:55-56), so an operator who knows where to
  configure Telegram finds the switch in the same place.
- Env override for parity with the other keys:
  `FLIGHTDECK_TELEGRAM_ENABLED` (1/true/yes/y/on → enabled;
  0/false/no/n/off → disabled; unrecognized → disabled/safe-down).
- Code default when ABSENT (key missing): ENABLED (True). NOT disabled.
  Why: an operator upgrading from a pre-switch build has a config with
  `telegram.group_id` but no `enabled` key, and their Telegram works. A code
  default of disabled would silently break every existing install on upgrade.
  "Default OFF for a fresh install" is achieved by the INSTALLER writing
  `enabled: false` into a brand-new config (bootstrap `_ensure_flightdeck_telegram`
  and `docs/config.example.yaml` for `flightdeck init`) — never by turning the
  absent-key default off. Precedence: env > config `telegram.enabled` > default True.

## How commands behave when disabled (off)

- `message dispatch`: creates the card, records it, does NOT touch a topic and
  does NOT claim it announced anything. Reports success with a neutral line.
  NEVER fails, NEVER warns repeatedly.
- `project new`: skips topic creation; records the project with no topic
  (registry already tolerates missing topic → "unknown").
- `message send` / `message read` / `message broadcast`: fail CLEANLY with ONE
  clear line "Telegram is disabled (telegram.enabled=false in <config>). Set
  it to true to enable." — not a traceback, not a silent no-op.
- `ask` / `decompose` / `ingest` / `sync` / mcp_server: any telegram path when
  off degrades to the clean "disabled" single line instead of failing.

## Where the switch is read

`core/telegram.py` gains a module-level `enabled()` helper (mirroring
`_resolve_group_id`) that the command layer consults. The command layer
(message.py, project_lifecycle.py, ask/decompose/ingest/sync, mcp_server.py)
checks the switch ONCE at dispatch time and either proceeds (on) or takes the
clean-disabled path (off).

Design choice: put the read in telegram.py, not inline in every command, so
there is ONE helper and commands call it. This keeps the "single switch" truly
single-sourced.

## Bootstrap wiring (fresh install defaults to OFF)

`enable_plugins.py` already wires `~/.hermes/config.yaml` (approvals
single_query_mode). The telegram switch lives in `~/.flightdeck/config.yaml`,
a DIFFERENT file that enable_plugins currently never touches. So:

- Add `_ensure_telegram(config_path)`-style logic to enable_plugins.py that
  reads `~/.flightdeck/config.yaml`, and when `telegram.enabled` is ABSENT
  writes `telegram.enabled: false` (fresh-install default OFF). Never
  overwrite an operator's explicit `true`/`false`. Mirrors the
  `approvals.single_query_mode` only-when-absent semantics.

Note: `bootstrap.sh` already asks the operator about Telegram via
`telegram_choice.py` (`--telegram=yes|no`, default no). The fresh-install
default OFF in enable_plugins aligns with that default-no. An operator who
answers `--telegram=yes` can still be enabled (explicit true re-enables).

## Single choke point

`_dispatch` in `core/telegram.py` is the ONE place every telegram core operation
(list/create/rename topics, send, read) flows through. It raises
`TelegramDisabledError` when the switch is off, BEFORE any client/daemon dispatch,
so a single guard covers the whole transport surface. Commands additionally
short-circuit via `telegram.enabled()` for the clearest message.

## Command-by-command behavior when disabled (final)

- `message send/read/broadcast` — clean error, exit 2, no daemon call.
- `message dispatch` — STILL CREATES THE CARD; skips the topic requirement and
  the announcement; reports "card <id> created on board <b> (telegram disabled —
  not announced)". Works even with no topic on the project.
- `project new` — topic step SKIPPED (recorded, never a failure); registry writes
  topic=None. `project repair` / `cmd_chat` need no change (repair's create_project
  skips topic too; chat uses hermes sessions, not telegram).
- `ask` / `decompose` / `topics rename|create` / `report._post` — clean error, exit 2.
- `topics list|audit` — print disabled message, return 0 (empty healthy state).
- `sync` — still syncs repos+boards; topics=[], no failure.
- `ingest` — telegram source returns "EMPTY (0 messages): telegram is disabled";
  board/card ingest still works.
- `qa._run_notify` — returns ([], []), records nothing as notified (re-enabling
  re-notifies).
- `doctor._topic_ok` — benign {"ok": True, "detail": "telegram disabled (topic
  not checked)"}; no false "transport broken" alarm.
- `init._probe_telegram_daemon` — short-circuits, reports disabled (not probed).
- mcp_server / registry / legacy / review / start / probe / templates — no
  telegram transport calls; unchanged.

## Tests

Standing rule: no Telegram in tests — inject a fake client / monkeypatch
`telegram.enabled`. Real command-output + bootstrap-wiring coverage:

- tests/test_flightdeck_config.py: telegram_enabled() precedence + absent-key
  default True + garbage safe-down (5 new).
- tests/test_message.py: send/read/broadcast fail cleanly; dispatch still
  creates the card with no announcement, topic optional (5 new).
- tests/test_telegram.py: every core op raises TelegramDisabledError when off (1 new).
- hscc-bootstrap/tests/test_enable_plugins.py: _ensure_flightdeck_telegram
  fresh-OFF / fresh-yes / env-unset / preserve-operator / idempotent / malformed (7 new).

Full hscc-project suite: 1218 passed; 6 pre-existing sandbox failures
(kanban delegate-child write guard + sqlite ops), confirmed identical on the
clean tree via git stash — NOT caused by this change.
