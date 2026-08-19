# Telegram MCP daemon

Flightdeck's Telegram features do **not** open their own Telegram session.
Every Telegram command talks to a single-writer **Telegram MCP daemon** — a
separate process that owns the one Telethon session for the machine. Flightdeck
is a thin MCP-over-HTTP client to that daemon; it never touches the Telethon
session file directly (a second client on the same session file is exactly
what raises `database is locked`).

This document describes the **contract** the daemon must satisfy — what it
must expose and where flightdeck looks for it. It deliberately does **not**
describe any particular implementation: the daemon is your own, and any MCP
server meeting this contract works.

## Where flightdeck looks for it

Flightdeck expects the daemon's MCP endpoint at:

```
http://127.0.0.1:8787/mcp
```

That is the default. You can override it per machine via the `telegram.mcp_url`
config key in `~/.flightdeck/config.yaml`, or the `FLIGHTDECK_MCP_URL`
environment variable (which takes precedence):

```sh
export FLIGHTDECK_MCP_URL=http://127.0.0.1:8787/mcp
```

See `docs/config.example.yaml` for the config-file shape. When a Telegram
command runs and the daemon is unreachable, the error names the daemon, the
exact configured URL, and this document — a bare connection failure is never
assumed to be self-explanatory.

## The contract: what the daemon must provide

The daemon is an MCP server exposing the following tools. Flightdeck calls each
by name and parses its text output.

| Tool | Purpose | Input | Output |
|------|---------|-------|--------|
| `telegram_topic_status` | List every topic with its current name (live forum-topic titles) | `group` | one `topic_id=<id>  title=<name>` line per topic |
| `telegram_topic_create` | Create a forum topic | `group`, `name` | `topic_id=<id>  title=<name>` |
| `telegram_topic_rename` | Rename a topic | `group`, `topic_id`, `name` | `topic_id=<id>  title=<name>` |
| `telegram_send` | Send a message into a topic | `group`, `message`, `topic_id` | free text reply |
| `telegram_read` | Read recent messages in a topic | `group`, `limit`, `topic_id` | one `[timestamp] sender: text` line per message |

The `group` argument is the Telegram group id, resolved by flightdeck from
`telegram.group_id` / `FLIGHTDECK_TELEGRAM_GROUP_ID` — never baked into source.

Because this is a **contract**, the list above is the single source of truth:
tests assert that `docs/TELEGRAM.md` names exactly the tools
`flightdeck/core/telegram.py` actually calls, so the doc cannot drift from the
code.

## What degrades without it

Only the **Telegram-facing** commands need the daemon:

- `topics` (list / audit / create / rename / bind / unbind)
- `message`
- `ask`
- `ingest`
- `report`
- `sync`
- `decompose`

Everything else works with no Telegram at all:

- `standup`, `qa`, `verify`, `roadmap`, `lint-cards`, `hygiene`, `reconcile`,
  `metrics`, `why`, `incident`, `doctor`

Those commands operate on the local board, git, and registry. They never reach
the Telegram transport, so an absent daemon does not affect them — and they
still work even when nothing is configured (they simply have no Telegram
integration to use). Note `doctor` reads Telegram to verify the board↔topic
binding, but it is deliberately non-fatal there: an unreachable daemon is
reported as an **UNVERIFIED** dimension, never as a silent all-clear and never
as a crash.

## Adding your own daemon

Any process that speaks the MCP protocol and exposes the tools above will do.
The operator's private implementation lives outside this repo and is not
shipped with flightdeck — the daemon is a documented prerequisite, not vendored
code. If you run the Hermes fleet's shared Telegram daemon on the same host,
pointing `telegram.mcp_url` at it is all that is needed.
