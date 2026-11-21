# Node pause — parking one node without touching the cluster

`hscc node pause` takes **one** worker node out of the fleet for a while: it stops
the sparkrun units on that node through sparkrun and records a durable pause in
`~/.hscc/node_pauses.json`. The repair paths then read that record and leave the
node alone. Nothing the cluster is built from is edited — no template, no
`serving.json`, no kanban cap.

First real use: giving a whole box to a training run, then putting it back.

The overlay is a **separate record the repair paths consult**, not a config
change. That is deliberate: every other knob (templates, `serving.json`, the
applied-template record, kanban caps, autodown, proxy config) is cluster source
of truth with an apply or heal path that would fight the edit — or restore the
node from it. The overlay module writes exactly one file, its own, and nothing
else.

## The verbs

```
hscc node pause  <host> [--reason <text>] [--until <duration>] [--span] [--force] [--apply]
hscc node resume <host> [--apply]
hscc node paused [--json]
```

Host names are what sparkrun calls them (`192.0.2.13`, or its alias). There is no
address or alias normalisation: a pause is keyed by the exact string you typed,
because guessing that `node-3` and `192.0.2.13` are the same box would invent a
pause nobody made.

**Dry-run is the default and `--apply` is opt-IN** (rule #478). `pause` and
`resume` plan, print and change nothing unless you pass `--apply`; `paused` is
read-only always. Note the polarity: this group is *opt-in to mutate*, which is
the opposite of `hscc cluster up|down [--dry-run]` — those are opt-out of a
mutation. When you script against either, read the flags again.

### `hscc node pause <host>`

Enumerates the node's units through the executor (sparkrun's structured status
query — the only sanctioned reader of fleet facts), prints every unit **and every
node a stop would touch**, then — with `--apply` — stops them and records the
pause.

| Flag | Meaning |
|---|---|
| `--reason <text>` | Free text recorded on the pause and shown by `hscc node paused`. Nothing reads it. |
| `--until <duration>` | A deadline recorded on the pause. Open-ended without it. See “`--until` records a deadline”. |
| `--span` | Accept that stopping a multi-node unit also stops it on every node it spans. Required **with `--apply`**. |
| `--force` | Pause a host the serving config says serves the orchestrator model, which the verb otherwise refuses. |
| `--apply` | Do it. Without it nothing stops and nothing is written. |

**Order is the contract:** with `--apply` the stop happens **first**, then the
overlay is written. So the record's `stopped` flag means “the units are confirmed
down”, not “we intended to”. A stop that failed still records the pause with
`stopped=false` and exits non-zero — the node *is* parked, and the heal paths must
still skip it, even though a unit is up. Conversely, if the units stopped but the
overlay write failed, that is an error and the command says so: a pause that never
reached disk did not happen.

### `hscc node resume <host>`

Restarts the units the pause recorded — through sparkrun's `--ensure` path, so the
same recipe and flags re-issue the declared workload instead of deriving a
different intent id and launching a parallel duplicate — then, and only then,
clears the pause and that node's heal strikes.

The overlay stores unit **ids**, not commands, so resume rebuilds each launch argv
from `serving.json`. Two refusals keep the pause in force on purpose:

- no launch command can be rebuilt for a recorded unit (nothing is known about how
  it was started) → `REFUSED`, **the pause stays recorded**; start the unit by hand
  (`hscc cluster up` / `hscc template apply`) and then clear the pause. A guessed
  `sparkrun run` would derive a different intent id and run a duplicate next to a
  live unit.
- a restart fails → the pause stays recorded, because the heal paths must not walk
  units onto a node that is half up.

On success it prints `pause for <host> cleared.` and clears the node's heal
strikes (the same `heal-clear` pairing), so a later breaker trip can alert again.

A host that is only *inside* another pause's span is not resumable on its own — the
span stops and starts as one thing, so the command names the owner and tells you to
resume that instead.

### `hscc node paused`

Reads the overlay file and **nothing else** — no executor, no `serving.json` read,
no sparkrun. That is the point: the list you need is exactly the list that must
survive a fleet that is down. Empty output is `No paused nodes — every node is
healable.`

Per host it prints the units, whether the stop was confirmed, the reason, `since`,
`until`, and the span. `--json` prints `{"count": N, "pauses": [ … ]}` with one
object per host: `host` plus the overlay's entry keys (`units`, `reason`, `since`,
`until`, `span_nodes`, `stopped`).

### Exit codes

`0` — the dry run completed, or the mutation succeeded, or the host was simply not
paused. `1` — a refusal, a bad flag, an unreadable plan, a failed stop, a failed
restart. A refusal always prints `Nothing stopped and nothing recorded.` (or names
what stayed recorded) and exits `1`; a dry run of the same host exits `0`.

### Agent tools

The same three verbs exist as declared agent tools — `node_pause` and
`node_resume` (execution requires confirmation) and `node_paused` (read). Each
handler calls the unified CLI's own verb function in-process rather than
re-implementing the guards, so an agent has no path around the dry-run default
that the operator does not have, and there is exactly one safety policy.

## What a pause never touches

A pause is byte-identical to no pause for everything the cluster is built from.
While a node is parked, these are unchanged on disk:

- the **templates** the fleet is declared from;
- **`serving.json`** — the unit still appears exactly as declared, so a resume can
  rebuild its launch command;
- the **applied-template record** — nothing is "un-applied" for the parked node;
- **kanban caps** and the dispatch posture;
- **autodown** config and state — the parked unit is not marked idle-down, and a
  deliberate-down record is not created;
- the **proxy config** and the router's view of the fleet.

Read that list as the contract: any behaviour a paused node needs must come from
the overlay, because none of these files will say "paused".

## Which loops skip a parked node

The overlay is read by the repair paths, each through its own explicit-root seam.
Two rules hold across all of them:

- **Ask `covering()` / `paused_nodes()`, never `paused()`.** A pause record is
  keyed by the host it was issued against and lists the rest of the unit's span in
  `span_nodes`. A seam that matched only the key would see a tensor-parallel unit's
  peer node as free and heal it behind your back. `paused_nodes()` is keyed hosts ∪
  their `span_nodes`.
- **A missing or malformed overlay fails open** — it reads as "no pauses", loudly
  WARNed, and never raises into a repair decision. A corrupt file must not freeze
  the fleet's ability to repair itself. So a pause you cannot read is not a pause:
  if `hscc node paused` cannot answer, fix `~/.hscc/node_pauses.json` before
  trusting the node to stay down.

| Loop | File | What a pause does |
|---|---|---|
| Watchdog auto-heal | `hscc_daemon/lifecycle.py` (`pipeline_watchdog`, `restart_vllm` rule 0) | The parked node is skipped: no restart attempt, no failure entry in the watchdog block file, no `PIPELINE BLOCKED` latch, no desktop notification. The stream still reports the truth (`ok: false`) and carries `paused` / `paused_node` / `paused_reason` / `paused_since`. The guard sits ahead of the heal lease — a skipped repair must not advertise itself as "a repair is in flight". |
| Heal breaker | `hscc_daemon/heal_breaker.py` (`note_heal`) | A parked node accrues nothing: no strike, no ledger write, `tripped` false — so your own `hscc node pause` cannot trip the breaker or page you. Only *accrual* is suppressed: `suspended()` still reports an existing suspension, strikes earned before the pause still count after the resume, and a pause on an unrelated node leaves the breaker armed. |
| Autoscale | `hscc_daemon/autoscale.py` (`decide_scale`) | A parked node is neither capacity nor a candidate: the paused hosts a snapshot measured drop out of capacity and their queue counters leave the fleet totals, and the ceiling becomes `max_workers` minus every node the overlay holds. When no unpaused slot is left the answer is `none`, naming the pause as the reason. The decision carries `capacity` and `paused_excluded` so you can see a pause shaped the answer. |
| Autodown wake | `hscc_daemon/autodown.py` (`autoup` → `_parked_nodes` / `_partition_wake_plan`) | A wake never `--ensure`s a parked node. The wake plan is split before anything starts and every record that could relaunch the unit later — readiness wait, post-boot footprint, reported wake set — runs on the startable half. Skipped unit ids are reported under `paused`, so "not started, parked" cannot read as "failed to start". If the whole plan is parked the wake reports `all-paused` and changes nothing. |

Two consequences worth internalising:

**Only starts skip.** Teardown still stops parked units — refusing a stop would
leave a unit *running* on a node you parked, which is the mirror image of the bug.

**The skip covers exactly the parked node.** A pause does not disarm supervision for
the rest of the fleet: a serving or gateway failure the pause did not cause still
counts, still latches, still pages.

## What does NOT skip a parked node yet

Named here because a docs page that promised otherwise would be worse than no page.
Two surfaces the overlay's own contract lists as skip seams have no overlay read
today:

- **The keepalive supervision loop** — `hscc_daemon/health.py` (`check_workers`).
  It probes the keepalive units and relaunches a down one through
  `serving._unit_run_cmd`. Its gates are the autodown window, the deliberate-down
  record, the heal lease and the heal breaker — and the breaker does *not* stop it:
  on a paused node `note_heal` returns `tripped` false, so the relaunch proceeds. A
  keepalive unit on a parked node can therefore be brought back by the daemon.
- **`hscc cluster up`** — `hscc-cluster/hscc.py` (`cmd_cluster_up`), and with it the
  post-boot relaunch (`hscc_daemon/post_boot.py`) and the recovery path
  (`hscc_daemon/recover.py`), which both call it. It builds the full
  `serving.json` plan and has no overlay read, so a fleet-up starts a parked node's
  units.

Practical rule until those land: a pause keeps a node down against auto-heal, the
breaker, autoscale and the autodown wake. Do not rely on it across a `hscc cluster
up`, a reboot of a node holding keepalive units, or a keepalive tick on a unit that
autodown would not have restored — if you need the node *stayed* down across those,
bring the fleet down deliberately or tear the unit's declaration down instead. Both
seams are tracked as follow-up work (`t_0f0a433f` for the keepalive loop,
`t_f17b1ae3` for `hscc cluster up` and the paths that call it); when one lands, its
bullet moves to the table above.

## Span rules

A unit can span several nodes (tensor-parallel across hosts). Stopping it stops it
everywhere, so:

- `pause` prints `every node this affects:` — the union of the observed placement
  and the intent-recorded hosts, so a span whose sibling node did not answer the
  sweep is still reported as a span and the unverified sibling is named.
- With `--apply`, a multi-node unit needs `--span`. The refusal names the siblings
  it would also have taken down:

  ```
  REFUSED: 192.0.2.13 hosts multi-node unit(s): tp-pair-b.
    A span unit stops as a whole, so stopping it here also stops it on: 192.0.2.14
    Pass --span to accept that, together with --apply.
  Nothing stopped and nothing recorded.
  ```

- **The opt-in guards the stop, not the reading of the plan.** A plain dry run of a
  span host previews the blast radius and exits `0`; `--span` without `--apply`
  prints `note: --span without --apply still stops nothing (dry-run is the
  default).` and changes nothing. Refusing the dry run would make the verb unusable
  for the one thing `--span` exists to let you accept.
- `resume` follows the same shape: the span starts as one thing, so a host named
  only in another pause's `span_nodes` is pointed at the owner.

## Orchestrator-model refusal

A host that serves the orchestrator model — the model every agent in the fleet
talks to — is refused unless you pass `--force`:

```
REFUSED: 192.0.2.11 serves the orchestrator model.
  why: serving config: orchestrator unit(s) ['orch-1'] declare 192.0.2.11
  Pausing it parks the model every agent in this fleet talks to.
  Pass --force to pause it anyway.
Nothing stopped and nothing recorded.
```

The `why:` line is the evidence the plan actually read — the serving config's
orchestrator unit ids, or the live workload that advertises the orchestrator alias —
never a name the CLI made up. The flag **fails closed**: when the status read or the
serving config is unavailable the evidence line reads `serving config unavailable —
cannot rule out this host serving the orchestrator model`, and the host is treated as
serving the orchestrator model, because refusing is the cheap direction to err in.
That asymmetry is deliberate: unlike the `--span` guard, this one refuses a dry run
too, because "I cannot prove this node is safe to park" is exactly what a dry run
should surface. `--force` on a host that is *not* flagged prints a note and
proceeds.

## `--until` records a deadline; nothing auto-resumes it yet

`--until` accepts the repo's single duration grammar — `90m`, `2h`, `1d3h` (parts
are added), `2w`, a bare number as **seconds**, or an ISO-8601 timestamp. Suffixes:
`s m h d w`. A leading `+` is **not** part of the grammar: `--until +2h` is refused.

The CLI refuses a value it cannot parse, and says why:

```
error: --until '2days' is not a duration (90m, 2h, 1d3h, 45 = seconds) or an ISO timestamp.
Refusing to record a pause whose deadline cannot be read.
```

That refusal lives in the CLI on purpose. The overlay's own `parse_until()` never
raises: an unparseable value degrades to **open-ended**, never to "already
expired" — the safe direction for the overlay (a malformed deadline must not
auto-resume a node behind your back) and the wrong one for a typo on a command
line, where `2days` would silently park a node forever.

**A deadline is not a resume.** `paused()` ignores the clock: a record whose
`until` has passed is still a pause, still skips every heal path, and keeps skipping
until something resumes it. `hscc node paused` flags it for you:

```
  until:   2026-11-22T18:00:00+00:00 — PAST DUE; resume it (hscc node resume 192.0.2.13 --apply)
```

The overlay does carry the primitives a daemon-driven auto-resume needs
(`pending_resume()` / `expiry_pass()`) and nothing calls them yet, so
`hscc node resume <host> --apply` is the only thing that ends a pause today. Treat
`--until` as a note to your future self, not a timer.

## Where else the pause shows up

The paused set is also projected onto the fleet read routes; `docs/API.md` is the
authority for that payload and this doc deliberately does not restate it. Whatever
those routes show, they only ever *read* the overlay — `hscc node pause` and
`hscc node resume` remain the only ways a pause appears or goes away. `hscc node
paused` is the same answer without the HTTP layer, and without needing the fleet to
be reachable.
