<p align="center">
  <img src="assets/hscc.png" alt="HSCC — Hermes Spark Cluster Control" width="640">
</p>

# HSCC — Hermes Spark Cluster Control

**Turn a DGX Spark GPU cluster into a self-running team of specialized AI agents.**

Say *"build X"* in chat and a fleet of role-specialized agents brainstorms, decomposes, codes, reviews, and lands it — across multiple GPU nodes, hands-off.

[![v2.17.8](https://img.shields.io/badge/version-2.17.8-blue.svg)](CHANGELOG.md)
[![MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![pinned deps](https://img.shields.io/badge/python-pinned%20deps-orange.svg)](requirements.txt)
[![5,000+ tests](https://img.shields.io/badge/tests-5%2C000%2B-brightgreen.svg)](scripts/README.md)

HSCC runs on a cluster of DGX Spark (GB10 / Grace-Blackwell) nodes serving LLMs via vLLM, orchestrated by [Hermes](https://github.com/NousResearch/hermes-agent) agents. It is a pure-Python plugin set with pinned dependencies (see `requirements.txt`). Install it with Homebrew (or clone the repo, if you are editing it); `hscc bootstrap` then copies the plugins into the Hermes runtime (`~/.hermes/plugins/`), with runtime state in `~/.hscc/`.

The design is **native-Hermes-first**: agent work runs on Hermes' built-in kanban dispatcher and git worktrees. HSCC does not build a dispatcher — it *hooks into* the one Hermes already ships, adding the thin physical layer (cluster control, monitoring, model lifecycle), a role framework for specialized workers, and a review gate on the dispatch path. Why this shape (what stock Hermes lacks and HSCC adds): [docs/WHY.md](docs/WHY.md).

## In plain words (ELI5)

You have a few powerful computers and a team of AI helpers. **HSCC is the manager that runs them for you.** You say what you want built; it splits the work across the computers, hands each piece to the right specialist — one plans, one writes the code, one checks it — and keeps the whole thing running: it restarts anything that crashes, and only pings you when it's genuinely stuck. One command gives you a whole working team, instead of babysitting a single chatbot.

## The stack at a glance

```
DGX Spark nodes (GB10) ── vLLM units ── sparkrun ── worker proxy :4000
     ▲  self-heal (lease+breaker+guard), monitor --live      │
     │                                                       ▼
hscc daemon (launchd) ── Hermes kanban dispatcher ── role workers (worktrees)
     │                                                       ▲
hscc api :8787/8788 (bearer token, tailnet) ── iOS app (QR pair)
```

## Requirements

- A configured [sparkrun](https://sparkrun.dev/) DGX Spark cluster (one or more GB10 nodes)
- [Hermes](https://github.com/NousResearch/hermes-agent) installed (`~/.hermes/hermes-agent`)
- A control host for the daemon — macOS (launchd) or Linux (systemd --user); Python 3
- [Homebrew](https://brew.sh/) on that host, for the install path above (a source checkout needs none of it)
- Python deps: the packages pinned in `requirements.txt` — the brew formula builds them into the keg's own venv; from source, bootstrap installs them
- tmux — REQUIRED for the iOS chat path: the app delivers chat by typing into the project's REPL pane; a REPL outside tmux is invisible to it. Delivery matrix: [hscc-api/README.md](hscc-api/README.md)

## Quick start

Two brew commands to install, one `hscc` command to configure:

```
brew tap pom11/hscc
brew install hscc
hscc bootstrap --yes
```

`brew install` keeps everything inside its own keg — the release tree plus a brew-managed Python venv — and deliberately writes nothing into `~/.hermes` or `~/.hscc`. `hscc bootstrap --yes` is the step that does: it detects your cluster from `sparkrun cluster list`, then installs skills, role profiles, `~/.hscc` state, and the monitoring daemon. It does **not** start vLLM models. Drop `--yes` for the short interview (orchestrator node when needed, **main orchestrator name**, recipe), and `--dry-run` prints every planned write while performing none. `hscc --version` and `hscc --help` already work before bootstrap.

If your keg predates the `bootstrap` verb (v2.12.0 and earlier), run the installer from the installed tree instead — `brew install` prints this as its next step and `brew info hscc` reprints it:

```
$(brew --prefix hscc)/libexec/hscc/hscc-bootstrap/bootstrap.sh --yes
```

Then, in order:

```
hscc status                                 # daemon health + all streams at a glance
hscc template list                          # declared fleet layouts for 1–8 nodes (22 today)
hscc template preview 4node-coding          # dry-run against the live cluster
hscc template apply 4node-coding --confirm  # bring up the serving layer
hscc project standup                        # fleet digest: NEEDS YOU / FAILING / STALE / RUNNING
hscc api start --tailscale                  # HTTP API for external apps (prints pairing QR)
```

Then open the iOS app and scan the QR (`hscc api status` reprints it) — [ios-app/README.md](ios-app/README.md).

## The main orchestrator

Bootstrap creates **one main orchestrator** — the agent that knows every project
and the whole cluster. It replaces the two old cluster-wide identities
(`general-orch` and the `orchestrator` role) with one profile that has both the
board duties and the cluster authority. Design:
[docs/design/2026-11-19-main-orchestrator-design.md](docs/design/2026-11-19-main-orchestrator-design.md).

**Name.** The interview asks once:

```
Main orchestrator name [general]:
```

`--yes` takes `general`; an interactive install accepts any lowercase slug that
does not collide with a role or project profile. The answer is stored in
`~/.hscc/main-orchestrator.json` and every component reads it from there —
re-running bootstrap never changes it. Its profile, its session and its kanban
board all use the same name.

**Its board is the default.** Bootstrap creates the board and makes it the
machine-wide kanban **current** board, so a card created without an explicit
`board=` lands there instead of on whichever project board was switched to
last. The old `default` board keeps its history and cards; it simply stops
being the default. The bootstrap doctor warns (never fatally) if the current
pointer drifts, with the one-line `hermes kanban boards switch <name>` fix.

**It runs in its own pane.** `hscc main start` ensures one tmux pane runs the
main orchestrator's REPL; `hscc main status` reports it and `hscc orch inject`
resolves that pane like any other orchestrator. (`hscc orch inject` types into
that live pane; the `orch_message` agent tool, the `/orch-restart` slash
command and `POST /v1/orchestrator/chat` are separate capabilities that
merely share the "orch" name.) With no tmux server running, bootstrap prints
the command rather than starting a server behind your back.

**It wakes itself.** The hscc daemon owns four editable wake-ups, delivered into
the same running session you talk to (queued while it is busy, never
interrupting a turn):

| Wake-up | Default | What it does |
|---|---|---|
| `health` | every 2 h | nodes, serving, proxy errors, daemon, autodown, kanban caps |
| `sweep` | every 3 h | every orchestrator's board and pane, standing watch cards |
| `memory` | daily 07:00 | refresh its per-project state note from live boards/PRs |
| `settings` | weekly Monday 07:30 | review its own skills, config and schedules |

```
hscc main wakeups list
hscc main wakeups set sweep "every 4h"
hscc main wakeups disable health
```

**Authority.** Everything except a LIVE publish: start/stop models, steer any
orchestrator, card any board, review and merge PRs, change settings and ship
releases. A production/LIVE deploy asks you first.

**How it reaches you.** When something needs you, the main orchestrator records
it with `hscc notify <title> <body>`, which writes the durable alert feed. You
see it with `hscc notify list` (newest first; `--since <id>`, `--json`) or by
asking it in the pane. `hscc notify ack <id>` marks one seen. When you are in
the conversation it tells you directly. Alerts are deduplicated and
rate-limited, so the same condition does not re-alert until it changes.

The apps do **not** show or poll notifications: operator decision 2026-11-19
removed push and local notifications from both clients, and the server has no
APNs sender or device registration. Nothing to configure in the Apple developer
portal. The feed and `hscc notify` remain the record.

Existing installs upgrade in place: bootstrap creates the main profile, copies
(never moves) the legacy catch-all's memories and sessions where the main has
none, and marks both old profiles retired in their SOUL. Nothing is deleted.

## Install from source (development)

Editing HSCC itself? Clone instead of tapping: the checkout **is** the install tree, and the installer runs straight out of it.

```
git clone https://github.com/pom11/hscc ~/dev/hscc
~/dev/hscc/hscc-bootstrap/bootstrap.sh --yes
```

Same installer, same flags — from a checkout `hscc bootstrap [flags]` finds that script as a sibling of the repo, and `HSCC_BOOTSTRAP_SH` overrides the lookup outright. Bootstrap backs up the previous runtime copy on each run (`--no-backup` skips). Component READMEs and the [docs/](docs/README.md) index assume this tree.

## Upgrade

| Installed via | Upgrade with |
|---|---|
| Homebrew | `brew upgrade hscc`, then `hscc bootstrap --yes` |
| source checkout | `git pull`, then `hscc bootstrap --yes` (or `bootstrap.sh --yes` on a build that predates the verb) |

Re-running bootstrap is not optional bookkeeping: `~/.hermes/plugins` is the copy Hermes actually loads, so a new release is inert until it is refreshed there. The tap serves the published release only and its formula is re-pinned by the release process — you never touch the tap yourself. From a brew keg the CLI-shim stage skips itself with the reason printed: brew owns `hscc` on PATH, and a shim into the Hermes venv would shadow it with a second, brew-invisible entry point.

## Check whether you are behind

```
hscc update --check
```

Read-only: it asks GitHub's releases API (5 s timeout; authenticated `gh` when available, plain anonymous HTTPS otherwise) what the latest release is, compares it with the `VERSION` this build reports, and prints the upgrade command for YOUR install mode — `brew upgrade hscc` on a keg, `git -C <repo> pull` plus the bootstrap reminder in a checkout, both when the layout is unrecognisable. It never downloads, installs, or upgrades anything; the one file it writes is the 24 h cache at `~/.hscc/update-check.json`.

Up to date is one quiet line, exit 0:

```
up to date — v2.12.0 is the latest release.
```

A release out is the comparison plus the command, still exit 0 (a newer release is not an error):

```
installed: v2.12.0
latest:    v2.13.0
upgrade:   brew upgrade hscc
HSCC does not upgrade itself — run the command above.
```

Offline, quota-limited, or served malformed JSON you get exactly one short line on stderr and exit 1 — `update check failed:` followed by the reason (a 403 or 429 names the quota and points at `gh auth login`) — plus the last good check if one was cached. The failure never lands on stdout, so `hscc update --check 2>/dev/null` and `... || echo current` behave. `--json` prints the machine-readable shape instead, same exit codes.

You rarely need to ask: the cached answer is what `hscc status` reads. Status never touches the network — it appends one dim line, and only when the cache says a newer release exists and is under 24 h old:

```
update: HSCC v2.13.0 available (installed v2.12.0); upgrade: brew upgrade hscc
```

Anything else — no cache, a cache older than 24 h, a cached "up to date" — prints nothing, and the line can never make `hscc status` fail. The daemon makes the same call at most once a day: it logs a WARN with that sentence when a release is out (INFO when you are current), never ERROR, and a failed check logs nothing and does not poison the cache — the last good answer survives on disk and the next run retries.

## Common tasks

| I want to … | Run |
|---|---|
| see the daily fleet digest | `hscc project standup` |
| check health at a glance | `hscc status` · full smoke test: `hscc verify` |
| watch per-node / per-model live load | `hscc cluster monitor --live` |
| look back at the stored series, not just the present | `hscc cluster history --window 6h` |
| see throughput + queue depth, get scaling advice | `hscc throughput` · `hscc autoscale` |
| take one node out of the fleet for a while | `hscc node pause <host>` — prints the plan and asks; `hscc node paused` to see what is parked |
| review + merge a finished card | `hscc project review <card>` |
| see why a card is stuck / un-stick it | `hscc kanban blocked [--recover <id>]` |
| preview what would be escalated | `hscc escalate` (dry-run) |
| shut idle GPUs down | `hscc autodown enable` — OFF by default, see below |
| see if a newer release is out | `hscc update --check` — read-only, prints the upgrade command |
| run / stop the external API | `hscc api start [--tailscale]` · `hscc api stop` |
| the full command surface | `hscc --help` · `hscc help advanced` · `hscc project --help` |

## Cluster templates

Instead of provisioning by hand, apply a template that declares intent (which recipes, how many workers) and resolves against the live cluster at apply — auto-assigning nodes + ports and refusing layouts that don't fit (checked against `sparkrun show` VRAM). The library ships for 1–8 nodes — 22 templates as of 2026-10-19 (`hscc template list`) — and is topology-free by default (no IPs, no ports); a template may state placement/routing explicitly, but **omission means do-not-touch**: apply never silently re-routes something tuned by hand. `hscc template validate <name> --structural-only` runs offline (CI-friendly).

Deep reference: [hscc-cluster/templates/README.md](hscc-cluster/templates/README.md) · explicit-placement schema: [docs/DESIGN-template-explicit-placement.md](docs/DESIGN-template-explicit-placement.md)

## Idle autodown

After the cluster has been quiet for X minutes the daemon can tear the whole **GPU serving layer** down to save power and bring it back up automatically on the next API/kanban activity or `hscc autodown wake`:

```
hscc autodown status [--json]              # read-only: armed? state? idle window?
hscc autodown enable [--idle-minutes N]    # arm it — OFF by default until you do
hscc autodown wake · cancel · disable      # wake now / abort teardown / disarm
```

It never tears down while kanban work is running/ready/in-review, while an agent is busy, or if any signal can't be verified. Cron-conflict classification, messages-while-down behaviour, and the full never-do list: [docs/design/idle-autodown.md](docs/design/idle-autodown.md).

## Pausing a node

`hscc node pause` takes **one** node out of the fleet for a while — to hand a whole box to a training run, then put it back — without touching any cluster setting. The heal, keepalive, autoscale and fleet-up paths read the pause record and leave that node alone until it ends.

```
hscc node pause <host> [--reason <text>] [--until 2h]   # prints the plan, then asks you to confirm
hscc node pause <host> --dry-run                        # the plan alone — no question, nothing stops
hscc node pause <host> --yes                            # no question — for scripts and CI
hscc node resume <host>                                 # prints what restarts, then asks the same way
hscc node paused [--json]                               # what is parked — reads the record, so it works with the fleet unreachable
```

Nothing mutates until you answer. `pause` prints its plan first — the host, every model it will stop, the reason, and the `--until` deadline if you gave one — then asks `Pause <host> and stop these N model(s)? [y/N]`. Only `y` or `Y` proceeds; `n` and a bare Enter both abort, and an aborted pause records nothing at all, so there is no half-state to clean up. `--yes` skips the question for scripts and CI, and since a prompt nobody can answer is worse than a refusal, a run with no TTY refuses unless you pass it. `--dry-run` is the prompt-free preview: the same plan, printed and dropped. `resume` confirms the same way, listing the models it will restart and clearing the pause plus that node's heal strikes once you say yes.

Eligibility is about blast radius, not count. A node is pausable when **every** model on it runs only on that node, so several single-node models sharing a box pause together in one call and all come back together. It refuses — nothing stopped, nothing recorded — when any model also runs on other nodes (the refusal names the model and the other nodes it spans), or when the node runs no models at all. Those refusals have no override, because the blast radius is someone else's workload. `--until <duration>` (`90m`, `2h`, `1d3h`, or an ISO timestamp) records a deadline the daemon's expiry tick honours, so the node comes back even if you do not; the schedule lives in the overlay rather than in memory, because an in-memory timer dies in exactly the crash that also restarts the watchdog.

The orchestrator-node guard is the one refusal `--force` unlocks, and it refuses on two grounds only: the host is the applied template's declared `orchestrator_node`, or the pause would leave zero healthy units serving the orchestrator alias. A uniform pool is otherwise pausable — every replica advertises that alias on purpose — and says so with a `stays served by N other node(s)` note.

And the promise: the templates, `serving.json`, the applied-template record, kanban caps, autodown and proxy config stay byte-identical while a node is parked. A pause is a separate durable overlay (`~/.hscc/node_pauses.json`) the repair paths consult, never an edit to what the cluster is built from — which is why resume can rebuild the exact launch command. Full reference: [docs/ops/node-pause.md](docs/ops/node-pause.md).

## The fleet

Work flows through native Hermes kanban: the main orchestrator (or a project's own orchestrator) decomposes an idea into a dependency-ordered task graph; role-specialized workers run in their own git worktrees (27 role specs today — `ls hscc-roles/roles/`); a reviewer agent gates code onto an integration branch (main stays human-gated); an escalation watcher reassigns repeatedly-failing tasks and only then pings you. The autonomy flag (`~/.hscc/autonomy`) governs hands-off runs. Roles, SOUL layering, review loop, escalation detail: [docs/FLEET.md](docs/FLEET.md).

## Components

| Component | What it is |
|---|---|
| [hscc-cluster](hscc-cluster/README.md) | cluster-control toolset (main-orchestrator-only) + template engine ([templates/](hscc-cluster/templates/README.md)) |
| [hscc-roles](hscc-roles/README.md) | role framework: author + generate profiles; autonomy flag |
| [hscc_daemon](hscc_daemon/README.md) | monitoring + self-heal daemon (launchd / systemd --user) |
| [hscc-commands](hscc-commands/README.md) | operator slash commands (`/cluster`, `/status`, `/heal`, `/template`, …) |
| [hscc-cli](hscc-cli/README.md) | the `hscc` console-script entry point |
| [hscc-project](hscc-project/README.md) | `hscc project` — project/kanban portfolio control ([commands](hscc-project/docs/COMMANDS.md)) |
| [hscc-api](hscc-api/README.md) | bearer-token HTTP API backing the iOS client ([docs/API.md](docs/API.md)) |
| [ios-app](ios-app/README.md) | iOS companion app (QR pairing, gates, sideload) |
| [sparkrun-hermes](sparkrun-hermes/README.md) | guarded sparkrun passthrough Hermes plugin |
| [hscc-bootstrap](hscc-bootstrap/README.md) | preflight-gated, topology-detecting installer |
| [hscc-skills](hscc-skills/README.md) | bundled skill installer (sources in [install/](install/README.md)) |
| [memori_cloud / memori_byodb](memori_byodb/README.md) | memory-provider plugins (HSCC runs the BYODB variant) |

Also: [changelog.d/](changelog.d/README.md) — per-card changelog fragments (never edit `CHANGELOG.md` directly); [patches/](patches/MANIFEST.md) — curated upstream patches; [docs/](docs/README.md) — full index of the docs tree; [scripts/](scripts/README.md) — operator watchdogs + repo tooling (address guard, changelog fragments).

## Layout

```
~/dev/hscc/                 # the work repo — what you clone + edit
├── hscc-cluster/ hscc-roles/ hscc_daemon/ hscc-commands/   # plugins → README each
├── hscc-cli/ hscc-project/ hscc-api/ hscc-bootstrap/ hscc-skills/
├── sparkrun-hermes/ memori_cloud/ memori_byodb/            # plugins → README each
├── ios-app/                # iOS companion app → README
├── changelog.d/            # per-card changelog fragments → README
├── install/  patches/  docs/  scripts/                     # → README / MANIFEST
└── _archive/               # superseded code (moved, never deleted)

# Hermes runtime (bootstrap targets — not edited directly):
~/.hermes/plugins/ · profiles/ · skills/ · scripts/ · ~/.hscc/ · ~/.sparkrun-local/recipes/
```

## Conventions

- Pure-Python with the pinned deps above; cluster control belongs to the main orchestrator only.
- State is read from live sources (`sparkrun status`, `serving.json`), never stale caches.
- "Strip/archive" means move to `_archive/<date>/`, never delete.
- Restart the gateway after prompt/plugin/config edits — command in [docs/FLEET.md](docs/FLEET.md).

## License

MIT — see [LICENSE](LICENSE).
