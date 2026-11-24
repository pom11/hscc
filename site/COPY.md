# hscc — landing page copy

Source of truth for https://pom11.github.io/hscc/ . The frontend card renders the
seven sections below **in this order, verbatim**. Do not reword claims, add
numbers, or add adjectives in the page — if the copy is wrong, fix it here.

Every claim below was checked against the real thing on 2026-11-20: the
installed CLI (`hscc` 2.16.0, `hscc --help` and `hscc help <group>`), the real
template registry (`hscc template list`), the real widget sources
(`ios-app/Sources/HSCCWidgets/`, `ios-app/docs/WIDGET_COVERAGE.md`,
`macos-app/README.md`), and the two design contracts the apps ship against
(`docs/design/2026-11-20-widget-redesign.md` and
`docs/design/2026-11-19-main-orchestrator-design.md`). Nothing here is a
benchmark, a customer, or a forecast.

---

## 1. What it is (ELI5)

**hscc looks after a small cluster of AI computers for you.**

You have a few machines that run AI models, and a team of AI helpers that do
your work on them. hscc is the manager in between: it starts the models, keeps
them running, hands work to the right helper, and restarts anything that falls
over — then tells you only when it actually needs you.

You watch all of it from your phone or your Mac, without opening a terminal.

## 2. Why

**One tool instead of ssh + docker + a folder of your own scripts.**

- **One command brings the fleet up or down.** No logging into each machine, no
  remembering which container runs what.
- **The models stay up on their own.** A watchdog keeps checking the serving
  path and restarts a model that died; if it can't fix it, it stops retrying and
  tells you why.
- **Templates switch what the cluster serves.** One command turns the same
  hardware from "one big brain" into "one brain plus a team of coders".
- **Agents work on task boards you can actually read.** Orchestrator agents take
  a goal, split it into cards, and workers pick the cards up — with a review
  step before anything lands.
- **One agent runs the whole show.** Bootstrap creates one main orchestrator,
  named `general` unless you pick another name. It knows every project and the
  cluster, watches the boards, steers the other orchestrators, and does
  everything except a LIVE publish.
- **Green PRs merge in order, not all at once.** The local merge queue rebases
  each one onto the latest main, runs the project's gates, and merges it. It is
  on by default for GitHub projects that declare gates.
- **You see everything from your phone or Mac.** Five widgets, Lock Screen
  gauges, one Live Activity, and a menu-bar app — no laptop required.

## 3. What it does

- 🖥️ **Runs the fleet** — starts and stops the models on every machine, from one
  place.
- 🩺 **Watches it constantly** — health checks for the machines, the serving
  gateway, storage, and the AI agents themselves.
- 🔁 **Heals what it can** — restarts a dead model on its own, and speaks up when
  it can't.
- 💤 **Saves power when idle** — puts the GPU serving layer to sleep after a
  quiet period once you turn autodown on, and wakes it when work arrives.
- ⏸️ **Parks one machine when you need it** — `hscc node pause <host>` frees a
  machine for a training run without editing a template or the serving config:
  you confirm the plan it prints before anything stops, the pause is a durable
  record the heal, wake and fleet-up paths honour, and the widgets, the CLI and
  the API all show it as `paused (reason, since)`. `--until 2h` gives the
  machine back by itself.
- 📋 **Runs agents on boards** — cards for todo / ready / running / blocked /
  done, per project, with a standup digest.
- 🧭 **Keeps one agent in charge** — the main orchestrator, `general` by
  default, wakes on a schedule, checks every board and pane, and does everything
  except a LIVE publish, which asks you first.
- 🔀 **Merges green PRs in order** — one local queue rebases, gates and
  squash-merges one PR at a time; `hscc merge-queue status` shows the queue.
- 📱 **Shows it on your devices** — five widgets on iOS and macOS, Lock Screen
  gauges, one Live Activity, and a CarPlay screen. There are no push
  notifications to configure.

## 4. Templates

**A template is a picture of your cluster written down.** It says: this many
machines, this model here, these workers there. Applying a template rebuilds the
cluster to match the picture — so switching what your hardware does is one
command, not an afternoon.

`hscc template preview <name>` shows what would change before anything moves;
`hscc template apply <name> --confirm` makes it real.

The registry we actually ship — 23 templates, from 1 machine to 8
(from `hscc template list`; run it for the live list, this table drifts):

| Template | What you get |
|---|---|
| `1node-orchestrator-only` | 1 machine: just the orchestrator, no worker team |
| `2node-coding` | 2 machines: orchestrator + 1 coding worker |
| `2node-fast` | 2 machines: orchestrator + 1 fast coding worker — the smallest useful fleet |
| `3node-balanced` | 3 machines: orchestrator + 1 coding worker + 1 fast worker |
| `3node-coding` | 3 machines: orchestrator + 2 coding workers |
| `3node-flux2-media` | 3 machines: orchestrator + an image unit + a video unit — **draft, preview only, never applied** |
| `4node-balanced` | 4 machines: orchestrator + 2 coding + 1 fast |
| `4node-coding` | 4 machines: orchestrator + 3 coding workers |
| `4node-coding-plus-fast` | 4 machines: orchestrator + a coding family + a fast family |
| `4node-colocated-dual` | 4 machines: orchestrator + 3 workers, each running two models side by side |
| `4node-deepseek-v4-orchestrator` | 4 machines: a big orchestrator split over 2 machines + 2 coding workers |
| `4node-dsv4-plus-coding` | 4 machines: a big orchestrator over 2 machines + 2 coding workers |
| `4node-dual-dsv4` | 4 machines: two big models, each over 2 machines — one orchestrator, one reasoning pool |
| `4node-dual-orch` | 4 machines: two orchestrators (one per project board) + 2 coding workers |
| `4node-flash-next-all` | 4 machines: the same model everywhere — 1 orchestrator + a load-balanced pool of 3 |
| `4node-quality` | 4 machines: a quality-first orchestrator + 2 coding + 1 fast |
| `4node-review-heavy` | 4 machines: orchestrator + 1 coder + 2 reviewer workers, sized for heavy auto-review |
| `4node-throughput` | 4 machines: fast orchestrator + 3 fast coders — burst mode |
| `8node-coding` | 8 machines: orchestrator + 7 coding workers |
| `8node-coding-plus-fast` | 8 machines: orchestrator + 4 coding + 3 fast |
| `colocated-two-models` | One worker family, two models per machine on their own ports behind one proxy |
| `single-family` | Orchestrator + one coding family across all machines, load-balanced behind a proxy |
| `hscc-live` | Our own running layout: orchestrator + one coding family across all workers, behind the proxy |

Which model each template serves lives in the template itself — check
`hscc template preview <name>` rather than trusting a web page.

## 5. Commands

The whole tool is one command, `hscc`. This is every command group, in the order
the tool lists them.

<!-- COMMANDS:BEGIN — generated from `hscc --help` (repo VERSION) plus
     `hscc help <group>` for the groups that take subcommands. Format contract
     for the drift test: one bullet per top-level command, in `hscc --help`
     order, as `- `hscc <cmd>` — plain one-line explanation`. Keep the markers.
     The wording is the card's; the arguments are not — every option and
     placeholder inside `hscc ...` has to be one `hscc --help` publishes for
     that command, and scripts/gen_site_commands.py refuses to render a page
     that advertises one the CLI does not take. -->

### Daemon control

The background watcher that does the checking and the healing.

- `hscc start` — start the watcher in the background.
- `hscc stop` — stop it gracefully.
- `hscc status` — is everything healthy? One screen: the watcher plus the last
  result of every health check.
- `hscc install` — make the watcher start by itself when the machine boots.
- `hscc uninstall` — remove that auto-start and stop the watcher.
- `hscc plist` — print the auto-start config without installing it.
- `hscc log` — read what the watcher has been saying.
- `hscc start-daemon` — run the watcher loop in the foreground yourself
  (normally launchd runs it).

### Health & monitoring

- `hscc check` — run the health checks now instead of waiting, or just one
  stream (dgx, gateway, local, heartbeat, nas, watchdog, triggers).
- `hscc check --repo <path>` — is a given checkout still protected by the
  commit-time leak guard? Says so plainly when it isn't.
- `hscc watch` — follow the check results live.
- `hscc triggers` — the alert rules and what fired recently.
- `hscc verify` — one long smoke test of the whole cluster.
- `hscc stats` — what the fleet has been doing over the last N days.
- `hscc throughput` — how fast the models are answering and how deep the queue
  is, across the fleet.
- `hscc autoscale` — what the autoscaler would decide right now, without doing
  it.
- `hscc escalate` — which failures are waiting for a human, without touching
  them.
- `hscc heal-clear <node>` — clear one machine's heal-breaker strikes; this
  mutates the ledger, and `--help` clears nothing. Not the `/heal` chat command.
- `hscc heal-status` — list the heal-breaker strikes per machine, read-only.
  Not the `/heal` chat command, which restarts unhealthy workers instead.

### Cluster & templates

- `hscc cluster status` — what is running and which machines are idle.
- `hscc cluster hosts` — every machine and every saved cluster you know about.
- `hscc cluster monitor` — CPU, memory and GPU per machine; `--live` adds the
  load per model.
- `hscc cluster jobs` — every job running right now.
- `hscc cluster info` — the resolved cluster configuration in detail.
- `hscc cluster stop <id>` — stop one running workload.
- `hscc cluster down` — stop every workload on the fleet (`--dry-run` first).
- `hscc cluster up` — start every unit the serving config declares
  (`--dry-run` first).
- `hscc template list` — every template we ship.
- `hscc template status` — which template is applied right now.
- `hscc template preview <name>` — a dry run: what applying it would change.
- `hscc template validate <name>` — check a template offline and against the
  live cluster.
- `hscc template apply <name> --confirm` — rebuild the cluster to match the
  template.
- `hscc profiles` — how many cards each agent role is holding.
- `hscc project …` — mission control for several projects at once: standup,
  decompose, review, qa, verify, doctor, metrics and more.
- `hscc api start` — the read-only HTTP API your phone, Mac and CarPlay screen
  read from; prints the pairing QR code (`stop` / `status` too).
- `hscc autodown` — the idle sleep/wake for the GPU layer, off until you enable
  it: `status`, `enable`, `disable`, `wake`, `cancel`.
- `hscc kanban` — board hygiene: blocked cards (list and recover), stale cards
  (list and archive), and a live board monitor.
- `hscc merge-queue` — the local queue of green PRs, on by default for GitHub
  projects with gates: `status`, `list`, `add`, `remove`, `pause`, `resume`,
  `enable`, `disable`.
- `hscc cards monitor` — one live panel per running card, streaming what its
  worker is doing.
- `hscc main` — the main orchestrator (`general` by default): bring its pane up,
  see whether it is busy, and edit the wake-up schedule the watcher fires into
  it.
- `hscc orch inject` — type a message into a running agent's live pane. (Three
  other things share the "orch" name and are not this: the `orch_message`
  agent tool, the `/orch-restart` chat command, `/v1/orchestrator/chat`.)
- `hscc orch waker` — the built-in orchestrator waker: `status`, `enable`,
  `disable`.
- `hscc node pause <host>` — park one machine and only that machine. It prints
  the plan — the host, every model it would stop, the reason, the deadline —
  then asks `Pause <host> and stop these N model(s)? [y/N]`: only `y` proceeds,
  Enter or `n` records nothing, `--yes` answers it for a script (without it a
  run with no terminal refuses), and `--dry-run` previews without asking. It
  refuses, recording nothing, when a model on the machine also runs on other
  machines — naming that model and its peers — or when the machine runs no
  models at all. `--force` is only for the guard on the machine your
  orchestrator model runs on. `hscc node resume <host>` confirms the same way
  and starts the recorded models back up; `hscc node paused` lists what is
  parked, read-only.

### Utility

- `hscc notify "<title>" "<body>"` — post an alert to the feed (`--level`,
  `--key`); `hscc notify list` reads it newest first (`--since`, `--limit`,
  `--json`) and `hscc notify ack` marks one seen. The feed is what the Needs Me
  widget shows; hscc sends no push notifications.
- `hscc bootstrap` — the installer, runnable from a repo checkout, a plugin tree
  or a Homebrew install; it creates the main orchestrator and its board.
- `hscc update --check` — ask GitHub whether a newer release exists. Reads only;
  it never installs anything.
- `hscc clean` — archive then remove finished cards' workspaces, and prune
  rebuildable caches. Dry run until `--apply`; the safe set (package caches,
  stale pytest dirs) runs there, and the deep set — the whole uv cache, Xcode
  DerivedData, simulator caches, the Trash — only with `--apply --deep
  --confirm`, because deep never runs without an operator asking for it.

<!-- COMMANDS:END -->

## 6. How it looks

Five widgets — Cluster, Boards, Running, Needs Me and Merge Queue — the same
five on iPhone and on the Mac desktop, in a Native look: system colours, SF Pro
Rounded numbers, and colour used only for state.

- **Cluster** — is it serving, waking up, or down, with throughput and a row per
  machine showing load and temperature.
- **Boards** — open cards per board as stacked bars (running, todo, blocked),
  with the worker-slot cap and cards done today.
- **Running** — the longest-running card, its elapsed time and a heartbeat, then
  the other cards being worked right now.
- **Needs Me** — what is waiting for you from the alert feed, and a calm "All
  clear" when nothing is. It says "All clear" only when the last check actually
  succeeded; a failed check shows offline instead.
- **Merge Queue** — the PR at the front of the local merge queue, its stage
  (rebase, gates, push, merge), what is waiting behind it, and the last result.
- **Lock Screen** — all five also have gauge layouts for the Lock Screen, so a
  glance shows the hottest node, worker slots, the longest run, what needs you,
  or the merge queue.

**One Live Activity: Cluster watch.** It puts the cluster's serving state on the
Lock Screen and in the Dynamic Island: four node columns with temperature and
load, throughput, the daemon state and whether autodown is off. It starts from
the app's Cluster screen and ends after 8 hours or when you stop it.

**A CarPlay screen** shows cluster health, boards, running work, what needs you
and the merge queue on the car display. It runs in Apple's CarPlay simulator
today; shipping it on the road needs Apple's CarPlay entitlement, which hscc
does not have yet.

**Fresher widgets on the Mac.** iPhone widgets refresh about every 30 minutes,
the most often iOS allows, and whenever you open the app. On the Mac, the
menu-bar app can refresh the desktop widgets every 1, 2, 5 or 15 minutes while
it runs (Settings → Desktop Widgets). Every widget shows how old its data is.

**No push notifications.** hscc does not ask for notification permission and
sends nothing to your phone. The Needs Me widget is the at-a-glance view, and
`hscc notify list` is the full alert feed on the command line.

## 7. Get it

**CLI (Mac or Linux control host):**

```sh
brew tap pom11/hscc
brew install hscc
hscc bootstrap --yes
```

**iPhone app:** open <https://pom11.github.io/hscc/ios/> in **Safari** on the
iPhone and tap Install. It's an Ad Hoc build, so your device has to be
registered with the maintainer first — see
[ios-app/INSTALL.md](https://github.com/pom11/hscc/blob/main/ios-app/INSTALL.md).

**Mac menu-bar app + desktop widgets:**

```sh
brew install --cask pom11/hscc/hscc-widgets
```

**The code:** <https://github.com/pom11/hscc> (MIT).

You'll need a configured sparkrun DGX Spark cluster and Hermes on the control
host — hscc is the manager layer, not the hardware.

---

## REVIEW notes for the orchestrator (not page copy)

- REVIEW: the brief's "one command to bring the fleet up/down" is true
  (`hscc cluster up` / `hscc cluster down`, both with `--dry-run`), but it is
  not a complete first-time setup — the daemon must be running (`hscc start`)
  and `hscc bootstrap --yes` explicitly does **not** start models. Copy says
  "one command brings the fleet up or down" only, which is the claim the CLI
  supports.
- REVIEW: `README.md` says "declared fleet layouts for 1–8 nodes (22 today)";
  the real `hscc template list` reports **23 template(s)**. The registry is the
  source of truth — the README count is stale and should be fixed or dropped.
- REVIEW: the phone claim has a real gate — iOS is Ad Hoc and needs the device
  UDID registered. Section 7 says so; do not let the page design imply an
  open/self-serve install (no "download now" button without that line).
- REVIEW: template descriptions in the registry carry model speeds and benchmark
  figures (tok/s, SWE-bench Pro scores). They are the registry's own claims, not
  verified here, so none of them are repeated on the page.
- REVIEW: the "leak guard" line for `hscc check --repo` refers to the repo's
  commit-time address guard (`scripts/address_guard.py`). Kept to one line and
  jargon-free; drop it from the page if the frontend wants fewer cards.
