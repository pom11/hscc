# flightdeck CONFIGURATION.md — Configuration Reference

This reference covers every configurable surface of flightdeck: the
connection config file, the project registry, environment overrides, and
where state lives. Field lists and defaults below were read from the
authoritative source (`flightdeck/core/config.py` and
`flightdeck/core/registry.py`) at v0.2.1.

**Definitive source:** `flightdeck/core/registry.py` is authoritative for
the registry fields; `flightdeck/core/config.py` is authoritative for the
config file and env overrides. If this document disagrees with source, the
source wins and this is a bug — file it.

## Two design rules that matter everywhere

1. **Read-only by default.** Every mutating command is a dry-run unless
   its `--apply` flag is given. A bare invocation never writes state.
2. **Never report a state that was not verified.** flightdeck only reports
   something as "done", "verified", or "all good" after it has actually
   checked the underlying system.

---

## 1. Config file: `~/.flightdeck/config.yaml`

Connection-level settings live at `~/.flightdeck/config.yaml` (path
resolvable to `~/.flightdeck/config.yaml`; overridable in tests via the
`path` argument). It is deliberately separate from `registry.yaml` so
private values (a Telegram group id) never leak into the public repo as
source constants.

Precedence — **environment wins over the file**:

```
FLIGHTDECK_TELEGRAM_GROUP_ID  >  config.yaml telegram.group_id
FLIGHTDECK_MCP_URL            >  config.yaml telegram.mcp_url
                               >  default http://127.0.0.1:8787/mcp
```

### `telegram` section

| Field | Type | Default | What breaks without it |
|-------|------|---------|------------------------|
| `telegram.group_id` | int | **none** | There is deliberately NO default. When it is unset everywhere, resolving it raises `MissingGroupIdError` with an actionable message naming the config key, the env var, and how to find a group id. Commands that touch Telegram surface that error to the user; commands that never touch Telegram never need config at all. |
| `telegram.mcp_url` | str | `http://127.0.0.1:8787/mcp` | This HAS a sane, public-safe default (the shared MCP daemon's local binding). Set it only when your MCP daemon is not on localhost. |

A missing config file, an empty file, or a file with no `telegram` key are
all "not configured" — fine except for `group_id`. A present but malformed
file raises rather than silently guessing, because an unparseable config
is an unknown state.

### Environment overrides

| Env var | Overrides | Notes |
|---------|-----------|-------|
| `FLIGHTDECK_TELEGRAM_GROUP_ID` | `telegram.group_id` | Set this instead of the config file to avoid shipping a group id in source. |
| `FLIGHTDECK_MCP_URL` | `telegram.mcp_url` | Overrides the config file and the localhost default. |

### Example minimal config

```yaml
telegram:
  group_id: -1001234567890   # required — no default
  mcp_url: http://127.0.0.1:8787/mcp  # optional — this is the default
```

---

## 2. Project registry: `~/.flightdeck/registry.yaml`

The registry tracks the projects flightdeck operates on: one entry per
project, stored at `~/.flightdeck/registry.yaml` by default (overridable
globally with `--registry PATH`). Every command that names a project
looks its row up here.

### Fields — from `flightdeck/core/registry.py` (authoritative)

Only **`repo`** is required. `name` defaults to the repo basename when
absent. Every other field is optional; an absent field means "unknown"
and a project is NEVER dropped from output because a field is missing.

| Field | Required? | Type | Default | How it degrades when absent |
|-------|-----------|------|---------|-----------------------------|
| `name` | no* | str | repo basename | The project key; commands reference projects by this. Absent → derived from the repo path's basename. |
| `repo` | **yes** | str | — | Local git checkout path. A row without `repo` is malformed: `load_registry` raises `MissingRepoError` rather than silently dropping the row, and `add_project` requires it. |
| `board` | no | str | "unknown" | Hermes kanban board slug. Absent → board-scoped commands report "unknown". |
| `topic` | no | int | "unknown" | Telegram topic id. Absent → topic-scoped commands report "unknown". |
| `topic_name` | no | str | falls back to `name` | Expected Telegram topic NAME. The audit (`topics audit`) detects when a topic's live name was overwritten by comparing it against this. Absent → falls back to `name`. |
| `verify` | no | str | "unknown" | Shell command that proves the project works. Absent → `verify` reports "unknown" and cannot gate. |
| `roadmap` | no | str | "unknown" | ROADMAP.md path within the repo. Absent → roadmap commands report "unknown". |
| `install_cmd` | no | str | "unknown" | Opaque install command run by `release --apply` AFTER the release is cut, so the installed artifact actually carries the released version. Absent → the release can never be verified and is reported **UNVERIFIED**. |
| `installed_version_cmd` | no | str | "unknown" | Shell command printing the deployed version. Absent → release verification cannot confirm the deployed version. |
| `version_file` | no | str | `"VERSION"` | File within the repo holding the source version. Absent → defaults to `VERSION`. |
| `deployed_at_cmd` | no | str | "unknown" | Shell command printing the unix timestamp of the last deploy. Absent → deploy-time metrics report "unknown". |
| `depends_on` | no | list[str] | `[]` | Names of OTHER registered projects this project depends on (e.g. a client app that consumes another project's API). Validated on load: a name that does not resolve to a registered project is a load-time `RegistryError`, never silently tolerated. Absent/empty → self-contained, no dependents surfaced anywhere. Purely advisory, never blocking: a project WITH dependents (i.e. other projects declare it in their own `depends_on`) gets a one-line nudge — `N dependent project(s): a, b — consider verifying they still work` — in `standup`'s footer, `review <card>` (text and `--json`), and appended to a dispatched card's body via `message dispatch`. Nothing here gates a merge, a dispatch, or a release. |

\* `name` is optional in the sense that it defaults to the repo basename,
but every project effectively has one (derived or explicit). `repo` is the
only strictly-required field.

### Top-level key: `ignored_topics`

The registry may also carry a top-level `ignored_topics:` list of topic
ids that are known-permanent (e.g. Telegram's built-in `General` topic, id
1, which is always suppressed). These are excluded from commands that
would otherwise report them forever. A missing file or missing key is an
empty list — never an error; a malformed value raises. Set/maintained via
`project sync --ignore-topic ID --apply`.

### Semantics

- **Required (`repo`):** a row without it is malformed and raises, because
  silently dropping a project is the exact failure this tool exists to
  prevent. `add_project` requires both `name` and `repo`.
- **Optional (all others):** absent means "unknown", never an error. The
  two design rules still apply — nothing unverified is reported, and the
  registry file is only written by mutating commands run with `--apply`.

### Example registry

```yaml
projects:
  - name: myproj
    repo: ~/dev/myproj
    board: default
    topic: 12345
    topic_name: MyProj cluster
    verify: "pytest -q"
    roadmap: ROADMAP.md
    install_cmd: "pip install -e ."
    installed_version_cmd: "myproj --version"
    version_file: VERSION
    deployed_at_cmd: "stat -f %m ~/dev/myproj/VERSION"
  - name: myproj-client
    repo: ~/dev/myproj-client
    board: myproj-client
    depends_on:
      - myproj
ignored_topics:
  - 1
```

---

## 3. Where state lives: `~/.flightdeck/`

All persistent state, templates, and caches live under `~/.flightdeck/`:

| Path | What lives there | Written by |
|------|------------------|------------|
| `~/.flightdeck/config.yaml` | Connection settings (section 1). | `flightdeck init --apply`, or by hand |
| `~/.flightdeck/registry.yaml` | Project registry (section 2). | `project new/remove/repair/sync`, `topics bind/unbind`, etc., with `--apply` |
| `~/.flightdeck/templates/` | User-editable prompt templates. | `flightdeck init --apply` (only when absent), `ask template edit` |
| `~/.flightdeck/report-state.yaml` | Report bookkeeping — the last-reported timestamp per project, which becomes the default `--since` for `report`. | `flightdeck report --apply` |
| `~/.flightdeck/state.yaml` | Verify result + timestamp, so `standup` can show verify status. | `flightdeck verify` (recorded on every run) |
| `~/.flightdeck/test-baseline.yaml` | The `review` test baseline. | `flightdeck review` |
| `~/.flightdeck/qa-notified.yaml` | Cards already notified via `qa --notify`, so a card is announced once, on the transition into the needs-QA queue. | `flightdeck qa --notify` |
| `~/.flightdeck/ingest-context-<project>.md` | Per-project ingest context scratch (overwritten each run). | `flightdeck ingest` |

These files are written only as documented — a bare, non-`--apply`
invocation never touches this directory (except `verify`, which records
its own result by design).

---

## 4. Global CLI-level override

The registry path can also be pointed elsewhere per-invocation, so you can
run against a throwaway or different registry without touching
`~/.flightdeck/registry.yaml`:

```
flightdeck --registry /path/to/registry.yaml standup
```

The config path has no such CLI override (its override is the `--home
PATH` flag on `flightdeck init`, and the env vars for the two telegram
settings).
