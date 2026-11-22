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
hscc node pause  <host> [--reason <text>] [--until <duration>] [--force] [--yes] [--dry-run]
hscc node resume <host> [--yes] [--dry-run]
hscc node paused [--json]
```

Host names are what sparkrun calls them (`192.0.2.13`, or its alias). There is no
address or alias normalisation: a pause is keyed by the exact string you typed,
because guessing that `node-3` and `192.0.2.14` are the same box would invent a
pause nobody made.

**The question is the mutation safety** (operator rule 11-22, which retired
`--apply`). `pause` and `resume` print the whole plan, then ask; only a literal
`y` (or `Y`) at that prompt changes anything. `paused` is read-only always. The
polarity to remember is not opt-in/opt-out at all — it is *nothing happens until
you answer*:

| What you type | `pause` | `resume` |
|---|---|---|
| `y` / `Y` | stops the units, records the pause | restarts the recorded units, clears the pause |
| bare Enter, `n`, `yes`, anything else | `Aborted:` — exit `1`, nothing stopped, nothing recorded | `Aborted:` — exit `1`, nothing restarted, the pause stays |
| nothing (no terminal) | `REFUSED: no terminal to ask` | `REFUSED: no terminal to ask` |
| `--yes` | acts, and prints `note: --yes given — proceeding without asking.` | acts |
| `--dry-run` | prints the plan, never asks, records nothing | prints the commands it would issue, never asks, keeps the pause |

`--yes` and `--dry-run` together are refused as contradicting: one says "do it",
the other says "only look", and a CLI that quietly picked one would be guessing
about a mutation.

A non-terminal stdin — a pipe, cron, launchd, `ssh -T`, the agent tool — is a
**refusal**, not a no-answer. `hscc node pause 192.0.2.13 > /tmp/out.txt` stops
nothing; a script that means it passes `--yes`. That is the half that keeps a
cron entry from parking a node nobody meant to park.

### `hscc node pause <host>`

Enumerates the node's units through the executor (sparkrun's structured status
query — the only sanctioned reader of fleet facts), prints every unit and every
node the stop would take down, runs the two guards below, and then asks:

```
Pause 192.0.2.13 and stop these 2 model(s)? [y/N]
```

The question is printed after the plan and lists the same count the plan listed,
so what you are confirming is what you just read. `hscc node resume` asks the same
question in its own shape: `Resume 192.0.2.13 and restart these 2 model(s)? [y/N]`.

| Flag | Meaning |
|---|---|
| `--reason <text>` | Free text recorded on the pause and shown by `hscc node paused`. Nothing reads it. |
| `--until <duration>` | A deadline recorded on the pause. Open-ended without it. See “`--until` records a deadline”. |
| `--force` | Override the **orchestrator guard** only — a host the applied template declares as the orchestrator node, or the last healthy server of the orchestrator model. It unlocks nothing else, and above all not a shared model: see “Which node is pausable”. |
| `--yes` | Answer the question for it, the way a script must. Prints a note saying it did. |
| `--dry-run` | Print the plan and both guards' verdicts, then stop. Never asks, never writes. |

**Order is the contract:** after a `y` the stop happens **first**, then the
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

It lists first and asks second, exactly like `pause`:

```
192.0.2.13: pause recorded 2 unit(s)
  worker-8b
  worker-tp
  every node this affects: 192.0.2.13, 192.0.2.14
Resume 192.0.2.13 and restart these 2 model(s)? [y/N]
```

Answering `n` (or nothing, or no terminal) costs the fleet **nothing at all** —
not even the status sweep the restart command has to be rebuilt from, because the
gate sits ahead of that rebuild. The daemon's own expiry tick calls the same
function without a gate: a pause whose `--until` has passed ends on its own
schedule, and nobody is standing at a terminal to ask.

The overlay stores unit **ids**, not commands, so resume rebuilds each launch argv
from `serving.json`. Two refusals keep the pause in force on purpose:

- no launch command can be rebuilt for a recorded unit (nothing is known about how
  it was started) → `REFUSED`, **the pause stays recorded**; put the unit back
  through an apply (`hscc template apply`) or start it by hand, then clear the pause.
  `hscc cluster up` is not that workaround any more — it skips a unit a live record
  still holds (see the skip table below). A guessed `sparkrun run` would derive a
  different intent id and run a duplicate next to a live unit.
- a restart fails → the pause stays recorded, because the heal paths must not walk
  units onto a node that is half up.

On success it prints `pause for <host> cleared.` and clears the node's heal
strikes (the same `heal-clear` pairing), so a later breaker trip can alert again.

A host that is only *inside* another pause's `span_nodes` is not resumable on its
own — that record stops and starts as one thing, so the command names the owner and
tells you to resume that instead. (A record with a span is a legacy or hand-written
one: a pause created under today's rule can only ever name the one node it parks.)

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

`0` — a `--dry-run` completed, or a confirmed mutation succeeded, or the host was
simply not paused. `1` — a refusal (eligibility, orchestrator guard, unreadable
plan), an answer that was not `y`, a prompt with no terminal to ask it on, a bad
flag, a failed stop, a failed restart. A refusal always prints
`Nothing stopped and nothing recorded.` (or names what stayed recorded) and exits
`1`; `--dry-run` of the same host exits `0`. Script against the exit code, not the
prose: an aborted pause and a refused one both exit `1` and both changed nothing.

### Agent tools

The same three verbs exist as declared agent tools — `node_pause` and
`node_resume` (both require `confirm: true` to execute) and `node_paused` (read).
Each handler calls the unified CLI's own verb function in-process rather than
re-implementing the guards, so an agent has no path around the safety policy that
the operator does not have, and there is exactly one policy in the tree.

`confirm` is the agent's spelling of the terminal question: `confirm: true` maps to
`--yes`, and its absence maps to `--dry-run` — never to an unguarded mutation. The
tool surface has no terminal, so without `confirm` there is nothing to answer and
the CLI's refusal is what the agent gets back, in `report`. A confirmed `node_pause`
result carries `stopped: [ … ]` — the unit ids the record says came down, read back
through the CLI's own `node paused --json`, because “something happened” is not an
answer an agent should act on. There is no `span` argument any more, on either
surface, and no tool argument that unlocks a multi-node model.

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
| Keepalive supervision and proxy relaunch | `hscc_daemon/health.py` (`check_workers`' auto-heal and `worker-relaunch`, `check_proxy`'s `proxy-relaunch`, each through `_pause_covering_repair`) | A parked node takes no heal lease, counts no strike and gets no force-recreate, no stop-and-re-issue and no `proxy start`. Each site asks `covering()` at the mutating surface — ahead of `heal_lease.acquire`, ahead of the strike — so a skip never advertises "a repair is in flight" and a tick that did nothing never counts as something. The unit stays counted `down`, because it is down; the workers stream gains `paused` with those labels and the proxy stream gains `paused` with the reason, recorded as neither a failed relaunch nor a fresh cooldown stamp. Each site asks only about the node its own action is keyed on, so a pause on one node leaves repair everywhere else armed. |
| Fleet up | `hscc-cluster/hscc.py` (`cmd_cluster_up` → `_parked_nodes` / `_partition_up_plan`) | The widest way to undo a park: every entry it issues is `sparkrun run` with `--ensure`, whose whole point is to make the declared workload exist again. The plan is partitioned against `paused_nodes()` (resolved ONCE per pass) before anything launches, and a unit with even ONE parked node in its span is parked WHOLE rather than half-started around its own missing member. A parked unit is never launched and never enters `issued`; `units` and `plan` carry the startable half, and the skipped ids ride out under one additive `paused` key (`[]` when nothing was skipped, so an unparked fleet returns the identical dict). `success` stays `True` — post-boot and recovery read exactly that boolean, and a `False` there pages you about your own pause. The `expected_up` marker is written from the STARTABLE plan, so a parked unit stops being a relaunch candidate at its source instead of only at the launch seam. When every unit of the pass is parked the result carries `result: "all-paused"` at exit `0`. |

Three consequences worth internalising:

**Only starts skip.** Teardown still stops parked units — refusing a stop would
leave a unit *running* on a node you parked, which is the mirror image of the bug.

**The skip covers exactly the parked node.** A pause does not disarm supervision for
the rest of the fleet: a serving or gateway failure the pause did not cause still
counts, still latches, still pages.

**One verb, four entry points.** `hscc cluster up` is the start path for the CLI, for
the post-boot relaunch (`hscc_daemon/post_boot.py`), for the recovery path
(`hscc_daemon/recover.py`) and for `POST /v1/cluster/up` (`hscc-api/routes_ops.py`).
The skip lives inside the verb, so all four inherit it and none carries its own
overlay read that could drift from the others. `nodes=` scoping is **not** an
opt-out: narrowing the launch to a parked host still skips it, and that narrowing is
exactly what a stale post-boot marker hands this verb. `--dry-run` previews the skips
— a parked unit is already out of its `dry-run: N unit(s) would start` count, and
nothing is launched and no `expected_up` marker written — and it keeps its own
polarity: `success` is `False` on every dry run, pause or no pause, while the CLI
still exits `0`. Ending a pause is `hscc node resume <host> --yes`, never a fleet
up.

## What a pause still does not do

Named here because a docs page that promised otherwise would be worse than no page.
Every seam the overlay's own contract lists as a skip seam now reads the record:
watchdog auto-heal, the heal breaker, autoscale, the autodown wake, the keepalive and
proxy repair paths in `hscc_daemon/health.py`, and the fleet up with the post-boot,
recovery and API paths that call it. What a pause still cannot do:

- **It does not survive being unreadable.** Every one of those seams inherits
  `node_pauses.load()`'s fail-open direction: a missing or malformed
  `~/.hscc/node_pauses.json` reads as "no pauses" — quietly when missing, loudly
  WARNed when corrupt, and on the fleet-up path as an `ERROR` naming the risk and the
  file to check — and the repair or the launch then proceeds UNFILTERED. A pause
  you cannot read is not a pause.
- **It can defer a fleet-wide repair that is keyed on the parked host.** `check_proxy`
  keys its strike on one host, so a proxy that is genuinely down while that host is
  parked waits until the record ends — `hscc node resume <host> --yes`, or the
  `--until` tick. The skip lands in the stream with its reason, so the deferral is
  visible rather than silent, and a pause on any other host leaves that path armed.

This section used to name two seams with no overlay read: the keepalive supervision
loop (`t_8e5ab6cb`) and `hscc cluster up` (`t_3b909c61`, with `t_f17b1ae3` for the
post-boot and recovery halves). All three landed and are in the table above. A loop
that learns to start or heal units is a new skip seam, and belongs here until it
reads the overlay.

## There is no `--include-paused`

`hscc cluster up` has no flag that starts a unit under a live pause record, and that
is a decision rather than an omission. A start that ignored the record would make the
overlay false for every other consumer at once: the health API would project
`paused(reason, since)` on a node that is serving traffic, the watchdog would keep its
stream at `ok: false` and suppress its own heal, the heal breaker would refuse to
accrue strikes for a machine that is live, and autoscale would keep deducting its
capacity. A flag that outranks the record is not an escape hatch, it is a second
source of truth. `hscc-cluster/hscc.py` points at this page for exactly that answer,
and a signature test pins the flag's absence so it cannot arrive by accident.

The way back in is to end the record: `hscc node resume <host> --yes` restarts the
units the pause recorded, through sparkrun's `--ensure` path, then clears the pause
and that node's heal strikes. If what you want gone is the workload rather than the
node, take the unit's declaration down deliberately — a pause and a `serving.json`
edit are different decisions with different undo paths, and only one of them is
supposed to survive a fleet up.

## Which node is pausable

One function decides, and only one: `node_pause_exec.pause_eligibility(host, plan)`.
`hscc node pause` prints its verdict verbatim and so does the `node_pause` agent
tool, which calls the verb — neither surface carries a rule of its own, and a
source-grep test fails if either ever grows one.

**The rule (operator 11-22, which retired `--span` and the #561/#567 opt-in):**
a node may be paused when **every model on it runs only on that node**.

- **Allowed — one model on the node, alone.** `tp=1`, placed on this host and no
  other. The whole node goes down in one call.
- **Allowed — several models side by side, each alone.** Colocated single-node
  models are not a problem: a node running three `tp=1` models is pausable and all
  three are stopped together, listed in the plan and named in the report. The rule
  is about *span*, never about count — a fleet that packs several small models on
  one box is exactly what the pause is for.
- **Refused — any model that spans other nodes.** If even one unit is tensor-parallel
  across hosts (`tp > 1`, or placed on more than one node), the whole call refuses.
  Nothing on this node is stopped, nothing is recorded, and the refusal names the
  model and the other nodes it lives on:

  ```
  REFUSED: 192.0.2.13 runs a multi-node unit — a node pause stops only units that run on this node alone.
    tp-pair-b: spans 192.0.2.13, 192.0.2.14 (other node(s): 192.0.2.14)
    Stopping it here also stops it on: 192.0.2.14
    No flag overrides this: park the node when its models are all single-node, or stop the shared model on every node it lives on.
  Nothing stopped and nothing recorded.
  ```

  When the sweep shows the unit on one host but the declaration says `tp=2`, the
  declaration wins and the refusal says so — the safer reading of a disagreement is
  "this model is split across processes":

  ```
    tp-pair-b: declares tp=2 on 192.0.2.13 — a model split across processes is not a single-node model
  ```

- **Refused — nothing running.** A node with no units has nothing to park, and
  recording a pause there would create a shield over a machine nothing stops:

  ```
  REFUSED: 192.0.2.13 runs nothing — nothing to pause.
  Nothing stopped and nothing recorded.
  ```

- **Refused — the plan could not be read.** A pause that cannot enumerate what it is
  stopping is not a pause, it is a guess: `REFUSED: <host>: its units could not be
  enumerated — a pause must know what it is stopping.`, with the underlying status
  error on the next line.

**There is no override.** Not `--span` (the flag is gone from the parser, the help,
the agent-tool schema and this page — typing it is an `unknown flag` refusal like
any other), not `--force`, not `confirm: true`, not a second call. `--force` belongs
to the orchestrator guard below and to nothing else. The way to take a box that
hosts a shared model is to stop that model deliberately on every node it lives on
(`hscc cluster stop <id>`), or to move it off first.

A refusal costs the fleet nothing at all: the plan is printed above the verdict, so
you always see what *would* have gone down, and the refusal is the last line the
verb writes — the overlay is byte-identical afterwards, including for every other
host in it. The refusal is also not a question: an ineligible node never reaches the
confirmation prompt, so there is no answer that unlocks it.

**The two guards compose in this order:** eligibility first, then the orchestrator
guard. Either refusal stops everything, and an ineligible node never reaches the
orchestrator question. `--force` is an answer to the second guard alone.

The blast radius is still printed for a node that passes: `every node this affects:`
is the union of the observed placement and the intent-recorded hosts, so a sibling
that did not answer the sweep is named rather than assumed away. `resume` keeps the
matching rule for records that predate this one: a host named only in another
pause's `span_nodes` is pointed at the owner, because that record stops and starts
as one thing.

## Orchestrator-model guard

One function answers "may this node be parked without taking the orchestrator
model down?" — `node_pause_exec.orchestrator_pause_guard`. `hscc node pause`
prints its verdict and so does the `node_pause` agent tool, which calls the verb;
neither carries a decision of its own. It refuses on exactly two grounds.

**The host is the applied template's declared orchestrator node.**

```
REFUSED: 192.0.2.11 is the applied template's declared orchestrator node.
  why: applied template '4node-flash-next-all' declares orchestrator_node=192.0.2.11; this stop takes down 1 unit(s) on it (span: 192.0.2.11)
  Pausing it parks the model every agent in this fleet talks to.
  Pass --force to pause it anyway.
Nothing stopped and nothing recorded.
```

The node comes from the applied-template record (`~/.hscc/applied_template.json`,
written by `hscc template apply`) read through
`declaration_agreement.load_applied_state` — the one reader that knows that
record's path and shape. A fleet with no applied record (a hand-written
`serving.json`, as `hscc-bootstrap` leaves it) simply has no declaration to
consult, and the guard says so in a note rather than treating every node as the
orchestrator node.

**The pause would leave no healthy unit serving the alias.** Healthy means the
sweep saw containers running and the pause overlay is not holding the node — a
replica you have deliberately parked is not a server, and neither is a unit the
sweep never observed. A multi-node unit is one server: it leaves with every node
it spans, so its sibling node is not counted as a survivor.

```
REFUSED: 192.0.2.12 serves the orchestrator model.
  why: orchestrator-model served by 1 healthy unit(s) — pool-1 — and all of them are on node(s) this stop takes down. Pausing 192.0.2.12 would leave no healthy unit serving orchestrator-model; paused and therefore not counted as a healthy server: 192.0.2.13, 192.0.2.14
  Pausing it parks the model every agent in this fleet talks to.
  Pass --force to pause it anyway.
Nothing stopped and nothing recorded.
```

Anything else is allowed, and says why it is safe:

```
note: orchestrator-model stays served by 3 other node(s): 192.0.2.11, 192.0.2.13, 192.0.2.14
```

**Why not "refuse any node that serves the orchestrator model"?** Because under a
`uniform_pool` template every replica advertises `orchestrator-model` on purpose
— `serving._served_model_name` stamps both role aliases so each alias
load-balances across the whole pool. A per-unit flag is then true on every node in
the fleet, which is what `#567` shipped: `hscc node pause <any host>` refused, on
a four-node uniform pool, and the only way through was `--force` — a guard that
must be defeated to be used is not a guard. The question was never "does this
node advertise the alias" but "would anything still serve it afterwards", which
needs the whole sweep, not one host. (The flag itself stays: `plan()` still
reports `serves_orchestrator_model` as advisory evidence, and it is what the
fail-closed answer rests on.)

The guard **fails closed**: when the status read or the serving config is
unavailable it refuses with `reason: cannot_rule_out` and says it could not rule
the host out, rather than allowing a pause it cannot justify. That asymmetry is
deliberate, and it is the posture of both guards: each one prints its verdict for a
`--dry-run` too, because "I cannot prove this node is safe to park" is exactly what
a dry run should surface. An operator looking at a plan is entitled to hear that
the plan is a bad idea before being asked to confirm it. `--force` overrides either
orchestrator-ground refusal and announces which ground it overrode; on a node that
needed no override it prints the ordinary `note: --force given, but this
host is not flagged as serving the orchestrator model.` It has nothing to say to
the eligibility guard, and prints nothing there.

## `--until` records a deadline, and the daemon honours it

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

**A past-due deadline is a job the daemon takes.** A `--until` is a promise about the
future, and the only process here long-lived enough to keep it is the daemon. The two
alternatives both fail: an operator typing `hscc node resume` is a timer that goes on
holiday, and an in-memory timer (`threading.Timer`, a deadline held in a dict) dies in
exactly the crash or upgrade that also restarts the watchdog — which would then "heal"
the paused unit back to life on top of whatever the node is doing. So the schedule is
the overlay file and the clock is read once per tick: the `node_pause_expiry` stream
(60 s; body `expiry_tick` / `auto_resume_expired` in `hscc_daemon/node_pause_exec.py`,
wired in `daemon_ops.PERIODIC_INTERVALS`) per tick

* reads the overlay **once** and takes the entries whose `until` has passed — an
  unreadable `until` is open-ended, so it is never due;
* skips — and leaves parked — an entry whose `span_nodes` overlap a pause that is not
  yet due, because the recorded restart re-issues the whole declared workload and would
  start units on a node you have not released. That skip is logged, counted, and makes
  the stream red: a schedule that is not being honoured is not silently healthy;
* resumes the rest through the **same** `resume_host()` that
  `hscc node resume <host> --yes` runs — same rebuild-or-refuse rule, same "clear the
  pause only once the units are back" ordering, same heal-strike reset;
* stamps `~/.hscc/state/node_pause_expiry.json` on every path, so `hscc verify` can tell
  "nothing was due" from "the stream died".

Nothing is cleared because the tick ran. A restart that fails keeps the pause *and* the
heal strikes, and the next tick retries it — that durability is the file being re-read,
not a queue in memory, so a daemon restart in the middle loses nothing. One pass resumes
at most `MAX_RESUMES_PER_TICK` (4) pauses, longest-waiting first, and defers the rest
visibly: each resume is a sparkrun control call with a 120 s budget running in that
stream's own thread. With no pause on disk the tick costs one `os.path.exists` — no
sparkrun call, no `serving.json` read, no subprocess — which is why a 60 s cadence is
cheap enough to be invisible.

`hscc node paused` still flags a past-due deadline, and `hscc node resume <host>
--yes` still ends a pause by hand: the verb and the tick call the same function, so an
operator who resumes early and a daemon resuming on schedule cannot drift apart. Treat
`--until` as a timer that the daemon keeps — and `hscc verify`'s `node_pause_expiry` row
as the place a timer that missed its deadline stops being quiet.

### `hscc doctor` reports the schedule it is honouring

`hscc verify` answers "did the stream run". `hscc doctor`'s
`node pause expiry (durable-until stream)` row answers the question an operator
actually asks — *is my deadline being kept?* — in this order:

- **the stream has never stamped while the daemon runs** → FATAL, the only fatal this
  row may raise: the wiring itself is broken, so a past-due pause would park forever
  and nothing would notice;
- **the newest stamp is `ok: false`** → warn: a pause is past due and still parked. The
  row names the hosts out of the stamp — `restart FAILED on …` (the resume was refused)
  and `span HELD by another pause on …` (another pause still holds part of its span) —
  together with the `checked_at` the pass judged against and the tick's own message;
- **the stamp is older than `2×60 s + 90 s`** → warn: the loop is not reaching this
  stream any more (and if the daemon is not running at all, the row says so and names
  `hscc start`);
- **the stamp predates this build** → warn, never fatal: doctor is bootstrap's Stage 1
  and the daemon restart that starts the stream is a later stage, so aborting an install
  over that race would blame the upgrade for the upgrade.

A green row still says what it saw — `1 pause(s) parked, 1 past due`, the age of the
last pass, and a deferred backlog if the per-tick limit pushed work onto the next one.
Counts, not faults: a parked node is your decision, and a doctor that called it a
failure would train you to ignore the row. The row reads the two files the feature
writes (the stamp through `state.STATE_DIR`, the overlay through `node_pauses`) and
takes its freshness budget from `PERIODIC_INTERVALS`, so it cannot drift from the stream
it reports on — and it never reads a root its writers cannot write to.

## Where else the pause shows up

A parked node is a choice, not a fault, and the fleet read routes now say so:
`GET /v1/cluster/monitor` tags the parked host and serving rows with
`{status: "paused", paused_reason, paused_since}` and states the parked set top-level as
`paused_nodes`, and `GET /v1/health` carries the same `paused_nodes` list while leaving
its `ok` / `checks` verdict exactly as the checks produced it — naming the pause without
softening the signal. `docs/API.md` is the authority for those payloads; this page does
not restate their schemas.

Two properties hold across every reader of the overlay, HTTP or CLI:

- the overlay is read **per request**, not frozen into a cached snapshot, so a resume
  shows up on the next poll;
- the visibility layer only ever **reads**. `hscc node pause` and `hscc node resume` are
  the only ways a pause appears or goes away, and `hscc node paused` is the same answer
  without the HTTP layer and without needing the fleet to be reachable.
