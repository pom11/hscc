# Verdict: may a server route append to the daemon's log? — `GET /v1/autodown/status` (t_85875291)

Question this card exists to settle, left open by the parent audit
(`docs/audits/read-surface-logging_t_88abe739.md` §3, PR #721):

> Is a server route allowed to append to the daemon's log?

`GET /v1/autodown/status` → `hscc-api/routes_autodown.py:170` →
`_backing_status_snapshot()` (`:81`) → `autodown.status_snapshot()` (`hscc_daemon/autodown.py`,
the builder `hscc autodown status --json` also uses). On a host where the Hermes library is
unreachable that builder reaches `autodown._note_kanban_unavailable`, which writes one WARN
through `daemon_ops.log` into **`$HSCC_HOME/daemon.log`** — so one un-cached request per API
process creates the daemon's log file.

**Verdict: VIOLATION. Mute the kanban leaf for that one call** (`with autodown.silent_read():`
around `routes_autodown.py:90`), guard it in `hscc-api/tests/`, and delete the duplicated helper
in `hscc-bootstrap/doctor_orch_subs.py` now that PR #716 has landed.

Written before the code, as the card required. Evidence below is from this branch
(`wt/t_85875291` = PR #723's tip + `origin/main`).

## 1. The two readings, and why the second one fails

The reading that would keep the write is: *the audit's §3 marks only the five CLI doors
VIOLATION and treats daemon-side logging as ACCEPTABLE, and the API is a separate process, so
the WARN diagnoses a state the operator may want recorded.* Three things break it.

**(a) §3's acceptance list is about actors, not about processes, and it never mentions the API.**
What §3 actually accepts is `hscc check <stream>` and the daemon's own loops, for two stated
reasons: *"not advertised read-only"* and *"the log line diagnoses an action that was asked
for"*. A `GET` fails both halves — it is advertised read-only (the handler's own docstring:
*"read-only report"*, and design §B gives it a `speak` precisely because machines call it), and
no action was asked for. §3 also already places this route **inside** the violating surface, in
the VIOLATION entry it wrote for the CLI door:

> *"It is also the agent tool's and `GET /v1/autodown/status`'s door, so a machine caller that
> snapshots the tree sees the file."*

So "the parent audit did not settle it" is only true of the card's paraphrase, not of the audit.
The audit named this route; the fix card scoped the wrap to the CLI doors and left the route as
its own verdict — which is this document.

**(b) "Recorded for the operator" is already satisfied by a different file, and the repo says
so.** `daemon_ops.log()`'s own docstring offers the parameter for exactly this purpose — *"Pass
explicit paths to reuse the same timestamped format for a different service's log
(`~/.hscc/api.log`)"* (`hscc_daemon/daemon_ops.py:597`), and the API process uses it that way:
`API_LOG_FILE = ~/.hscc/api.log` / `API_PID_FILE = ~/.hscc/api.pid`
(`hscc_daemon/api_cli.py:46-47`), passed explicitly on every API-side diagnostic
(`api_cli.py:463`, `:470`, `:477`, `:486`, `:493`). **The convention is: a process writes its own
log.** `_kanban_warn` (`autodown.py:938`) calls `log(msg, "WARN")` with no `log_file`, so the API
process writes into the *daemon's* file — a misrouted line, not a preserved diagnostic. And under
the echo rule (`daemon_ops.py:630`, echo only when the **daemon's** pid file is absent) a healthy
host suppresses the echo, so the one artifact this route produces is a line inside a log that
belongs to another process.

**(c) It is a *false* diagnostic, not a harmless one.** The line carries no writer identity:
`[ts] [WARN] Autodown kanban interlock unevaluable: Hermes agent source not found at …`. An
operator who finds it in `daemon.log` debugs the daemon; the process that could not import the
library is the API server. The write does not only cost bytes, it points at the wrong suspect.

## 2. The write is also unsafe, not merely untidy

`daemon_ops.log` → `append_rotating` (`:576`) → `_get_logger` (`:549`) → a stdlib
`RotatingFileHandler` with `maxBytes`/`backupCount`, one handler per resolved path. Rollover is
`rename`-based. Two processes doing size-based rollover on one path is the classic lost-write
shape: the second process renames the file out from under the first's still-open descriptor, and
everything the first writes afterwards lands on the renamed inode and gets gzipped away. A foreign
writer is therefore *structurally* not allowed in a rotating log another process owns — the same
reason `append_rotating` exists at all ("a rotation that only covered one writer would leave the
other growing", `daemon_ops.py:576`).

## 3. Precedent: the API process may not cause daemon-side effects on a read

`hscc_daemon/metrics_history.py:974` (`resident_recorder`, t_c7f57783) states the rule for this
exact process pair — the API server is *"a **separate** process (`hscc_daemon/api_launchd.py`
starts its own `python -m api_server serve`)"* and *"a **read route must never** sample the
fleet"*, because the tempting accessor spawns a `sparkrun cluster monitor` child. Same shape as
this card: a read route reaching for something that belongs to the daemon process. The settled
answer there was "the route must not; give it a read-only view of the answer instead". Here the
route already has it: the payload carries `kanban_ok` / `kanban_reason` (the same fields the CLI
door's panel prints as `kanban interlock: UNEVALUABLE — …`), so muting the file write removes no
information from anyone who can act on it. `autodown.silent_read`'s own docstring anticipates
this caller: *"a server that wants the same quiet should scope the same way, per call"*.

## 4. What the verdict licenses, and what it does not

Licensing (narrow, on purpose):

* ONE wrapped call, the route's own: `with autodown.silent_read():` around
  `routes_autodown.py:90`. Scoped to the one borrowed call, restored in `finally`, WARN budget
  deferred rather than spent (PR #723's semantics; `hscc_daemon/tests/test_autodown.py::
  TestSilentReadDefersTheWarnBudget`).

Not licensed:

* **Not a wider mute.** `silent_read()` mutes only the kanban leaf (`_kanban_warn`,
  `autodown.py:938`). The PR/CI WARN stays audible — `t_18318fd9` pins it from this same payload
  builder (`test_autodown_cli.py::TestStatus::test_status_json_survives_a_real_gh_blip`) and
  PR #723's `test_the_prci_warn_is_not_muted_by_a_read` guards the boundary. Do not "simplify"
  this by muting the module `log` name.
* **Not "the route writes nothing anywhere".** An *authenticated* request writes
  `~/.hscc/activity.json` by design (`api_server.py:153` `_do_stamp_http_activity`, called from
  `_route` only after `_authorize()` succeeds, `:815`) — that is the autodown idle-timer signal,
  i.e. the request *is* the activity. It is a designed effect of this surface, not an inherited
  log write, and the guard here does not (and must not) assert it away.
* **Not the daemon's loops.** `lifecycle`/`post_boot`/`serving`/`cron_watchers` and
  `_has_active_work()` called from the daemon keep their logging. Only the two doors that are
  documented reads go through the mute.
* **Not the mutating API endpoints.** `enable`/`disable`/`wake`/`cancel` are actions; if any of
  them gains a WARN path later, it is an action diagnostic and needs its own verdict.

## 5. Where the guard goes, and why it can pass

In the surface's own suite — `hscc-api/tests/test_routes_autodown.py` (the #704 lesson: a surface
may not rely on another directory holding its invariant; the CLI door's guards in `hscc_daemon`
prove nothing about this route, which reaches the builder directly).

It is a subprocess guard, in the parent card's rig shape (env recipe reused from
`hscc_daemon/tests/door_sandbox.py`, which by design holds no assert): real `api_server` bound to
`127.0.0.1:0` inside a sealed `HOME`, one real `GET` with a bearer token, `$HSCC_HOME` snapshotted
before/after, the payload required to **name the sandbox path** (so green means "the leaf ran and
the mute bit", not "the leaf was skipped"), `the_seal_actually_took()` as the seal-took proof, and
an **un-muted control** that calls `autodown.status_snapshot()` in the same sealed environment and
must see the WARN land — the control is what stops the guard being vacuous.

## 6. LEFT (found here, not folded)

1. **The route's own log line has nowhere to go.** If the operator *should* see
   "the API process cannot import Hermes' kanban library", the right destination is
   `~/.hscc/api.log` (`api_cli.py:47`), not `daemon.log`. This card deliberately does not add it:
   the route answers the question in its response body, and a route inventing a log line is a new
   surface decision, not part of closing this class. Card it if the operator wants the record.
2. **Three other latent leaves in the same audit are still unfixed** (`board_pin._emit` from
   `guard_board(mode="read")`, `node_pauses._warn` from the pause-file parser,
   `kanban_pause._warn_pin_once` from the read-refused branch) plus `kanban_live._note_caps_unreadable`.
   They need their own conditions to fire and their own doors; they are not this card's leaf.
3. **PR #723 is still open**, so this branch is stacked on it (`base = wt/t_4564c680`) and must be
   retargeted to `main` when #723 lands. `autodown.silent_read()` does not exist on `main` yet.
