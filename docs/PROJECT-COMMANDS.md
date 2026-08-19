# hscc project — command mapping & naming notes

This page is the durable reference for the **`hscc project …`** verb group —
the project/kanban orchestration domain formerly served by the standalone
`flightdeck` tool. It holds two things:

1. The **`flightdeck X` ↔ `hscc project X`** command mapping (so anyone with
   muscle memory for the old tool, or reading flightdeck's docs, can translate).
2. The **naming-collision awareness** — why some commands live under
   `hscc project …` and not at the hscc top level (this is intentional, not a
   bug).

The full flightdeck command reference (every subcommand, every flag) is
[`hscc-project/docs/COMMANDS.md`](../hscc-project/docs/COMMANDS.md).

---

## Command mapping

Every flightdeck command is reachable unchanged under the `project` verb.
`hscc project` forwards the rest of the argv straight to flightdeck's own
argv-driven entry point, so the command surface is identical — only the
prefix changes.

| flightdeck (standalone) | hscc |
|---|---|
| `flightdeck project new/list/remove/repair/sync/pull/push` | `hscc project new\|list\|…` |
| `flightdeck standup` | `hscc project standup` |
| `flightdeck review` | `hscc project review` |
| `flightdeck qa` | `hscc project qa` |
| `flightdeck verify` | `hscc project verify` |
| `flightdeck release` | `hscc project release` |
| `flightdeck roadmap <sub>` | `hscc project roadmap <sub>` |
| `flightdeck ingest` | `hscc project ingest` |
| `flightdeck decompose` | `hscc project decompose` |
| `flightdeck start` | `hscc project start` |
| `flightdeck message send/read/dispatch/broadcast` | `hscc project message …` |
| `flightdeck report` | `hscc project report` |
| `flightdeck metrics` | `hscc project metrics` |
| `flightdeck daemon …` | `hscc project daemon …` |
| `flightdeck doctor/why/monitor/hygiene/reconcile/lint-cards/legacy-cards/migrate-card/incident/ask/update/topics/init` | `hscc project <same>` |

---

## Naming-collision awareness (intentional, not a bug)

hscc already has its own top-level verbs for the **cluster** domain, and a few
of their names overlap with flightdeck's project-domain commands. That overlap
is resolved by the `project` namespace — the two commands coexist because one
is nested under `hscc project …` and the other is not. Do not "fix" this: the
collision is deliberately avoided by namespacing, not left unresolved.

| Command | hscc top level (cluster domain) | hscc project (project domain, formerly flightdeck) |
|---|---|---|
| `verify` | `hscc verify` — cluster-stack smoke test | `hscc project verify` — run a project's registry `verify` shell command |
| daemon lifecycle | `hscc daemon …` (via the `hscc_daemon` cluster self-heal daemon) | `hscc project daemon …` — the project-watcher daemon (fleet in-flight counts, board freshness, orphan boards, version drift) |

Both tools independently had a `verify` and a *daemon* concept before the port.
Under a flat `hscc verify` / `hscc daemon` the two would collide. Putting the
project-domain ones under `hscc project verify` / `hscc project daemon …` keeps
them distinct and unambiguous. This was implemented correctly in Phase 2 of the
port; this note exists so a future reader doesn't mistake it for an oversight.

The project-doctor command (`hscc project doctor`) is likewise distinct from
any cluster-side health check — it self-checks the project registry, config,
kanban, and Telegram daemon.

---

The design rationale and full port plan live in
[`docs/INVESTIGATION-flightdeck-port.md`](INVESTIGATION-flightdeck-port.md).
