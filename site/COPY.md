# hscc — landing page copy

Source of truth for https://pom11.github.io/hscc/ . The frontend card renders the
seven sections below **in this order, verbatim**. Do not reword claims, add
numbers, or add adjectives in the page — if the copy is wrong, fix it here.

Every claim below was checked against the real thing on 2026-11-18: the
installed CLI (`hscc` 2.15.11, `hscc --help` and `hscc help <group>`), the real
template registry (`hscc template list`), and the real widget sources
(`ios-app/Sources/HSCCWidgets/`, `ios-app/docs/WIDGET_COVERAGE.md`,
`macos-app/README.md`). Nothing here is a benchmark, a customer, or a forecast.

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
- **You see everything from your phone or Mac.** Six widgets, Live Activities,
  and a menu-bar tile — no laptop required.

## 3. What it does

- 🖥️ **Runs the fleet** — starts and stops the models on every machine, from one
  place.
- 🩺 **Watches it constantly** — health checks for the machines, the serving
  gateway, storage, and the AI agents themselves.
- 🔁 **Heals what it can** — restarts a dead model on its own, and speaks up when
  it can't.
- 💤 **Saves power when idle** — puts the GPU serving layer to sleep after a
  quiet period and wakes it when work arrives.
- 📋 **Runs agents on boards** — cards for todo / ready / running / blocked /
  done, per project, with a standup digest.
- 📱 **Shows it on your devices** — the same state as widgets on iOS and macOS,
  plus live tiles on the Lock Screen and Dynamic Island.

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

<!-- COMMANDS:BEGIN — generated from `hscc --help` (hscc 2.15.11) plus
     `hscc help <group>` for the groups that take subcommands. Format contract
     for the drift test: one bullet per top-level command, in `hscc --help`
     order, as `- `hscc <cmd>` — plain one-line explanation`. Keep the markers. -->

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

### Health & monitoring

- `hscc check` — run the health checks now instead of waiting, or just one
  stream (machines, gateway, local, heartbeat, storage, watchdog, triggers).
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
- `hscc api start` — the read-only HTTP API your phone and Mac read from; prints
  the pairing QR code (`stop` / `status` too).
- `hscc autodown` — the idle sleep/wake for the GPU layer: `status`, `enable`,
  `disable`, `wake`, `cancel`.
- `hscc kanban` — board hygiene: blocked cards (list and recover), stale cards
  (list and archive), and a live board monitor.
- `hscc cards monitor` — one live panel per running card, streaming what its
  worker is doing.
- `hscc orch inject` — send a message into a running agent's session.
- `hscc orch waker` — the built-in orchestrator waker: `status`, `enable`,
  `disable`.

### Utility

- `hscc notify "message"` — send a desktop notification.
- `hscc bootstrap` — the installer, runnable from a repo checkout, a plugin tree
  or a Homebrew install.
- `hscc update --check` — ask GitHub whether a newer release exists. Reads only;
  it never installs anything.

<!-- COMMANDS:END -->

## 6. How it looks

Six widgets, the same six on iPhone and on the Mac desktop, so the cluster is a
glance instead of a login:

- **Cluster** — is it serving, waking up, or down, with a small map of the
  machines and how long before an idle cluster goes to sleep.
- **Monitor** — CPU, memory and GPU per machine.
- **Running cards** — the jobs being worked right now, who is doing them, and
  for how long.
- **Kanban monitor** — running, ready and blocked at a glance.
- **Board status** — todo / ready / running / blocked / done for one board you
  pick.
- **Projects** — your projects and the board each one owns.

**Live Activities** put the same information on the Lock Screen and the Dynamic
Island while something is happening: the cluster waking, models serving, cards
running, a board making progress, or the fleet's overall health.

**You can restyle it.** One global style for every widget and Live Activity, or
a different one per tile — colour (including a transparent background), font,
weight, text size, density, and whether the header and icons show. Presets get
you most of the way there, and everything has a live preview.

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
