# t_f4ee3ce6 — resume re-attach restarts the :4000 proxy (~15 s refusal window)

Status: analysis settled, fix chosen and landed in this branch. The residual
piece is an upstream gap, filed as a child card (see END OF FILE).

## 1. What the seam does today, measured

The card's event is in the daemon's own health log (redacted):

```
[20:02:21.254] Worker proxy unhealthy on :4000 (live router missing backend(s)
              ['<NODE>:8003'] — healthy unit(s) the proxy is not serving)
[20:02:21.257] repairing proxy backend drift on :4000 — live router missing …
[20:02:35.806] proxy repair UNVERIFIED on :4000 — port owner 15706,
              sparkrun tracks 15701 ('<UV_CACHE>/bin/python <UV_CACHE>/bin/litellm --config …')
[20:02:35.807] proxy repair on :4000 FAILED (repair incomplete (unverified)):
              Discovering inference endpoints…
[20:03:35]      (next proxy check: green — the resumed backend IS being served)
```

Read out of that:

1. The actor is **hscc**, not sparkrun's auto-discover. `check_proxy`'s
   F4 backend-drift branch (`hscc_daemon/health.py`, `backend_drift` →
   `_proxy_repair_orphan`) is what takes the port down. The drift is the
   resume re-attach itself: the resumed unit is healthy, the live router has
   no backend for it, `MISSING` → repair.
2. The window measured 20:02:21.3 → 20:02:35.8 = **14.6 s**, matching the
   card's ~15 s.
3. `_proxy_repair_orphan` is **stop → reap → start**, so everything sparkrun
   needs before it can bind happens *after* the port is already dead. The
   FAILED line's own output starts with `Discovering inference endpoints…` —
   proof that the fleet discovery sweep (ssh to every node) runs **inside**
   the refusal window in hscc's shape.
4. The repair **worked** (next tick green) and was still reported FAILED,
   because verification demands `port owner == sparkrun's tracked PID`. On
   this fleet `sparkrun proxy start` launches the gateway through
   `uv tool uvx` (see the `_proxy_launch_env` comment at health.py:1094), so
   sparkrun's state record is the *wrapper* (15701) and the listener is its
   *litellm child* (15706). The two can never be equal. Every successful
   drift repair in this shape is a false FAILED: it feeds
   `PROXY_RELAUNCH_MAX_CONSECUTIVE` (which escalates the *next* fault
   straight to `_proxy_escalated_repair` + the stall alarm) and prints an
   ERROR about a repair that succeeded.

## 2. FIX 1 (add the backend without restarting) — not available. Verified.

`sparkrun` source is the authority (`/Users/desac/sparkrun`, v0.4.2, the CLI
the daemon actually execs). `plugins/litellm/engine.py`:

* `apply_desired_state()` docstring (~line 617): *"LiteLLM's runtime mutation
  endpoints (`/model/new`, `/model/delete`) are **not** usable here: they
  require a DB-backed model store, which needs PostgreSQL plus a generated
  prisma client, so against a sparkrun-launched proxy they answer
  `500 No DB Connected`. Applying a change therefore means rewriting the
  config and restarting the process."*
* The same function returns early without restarting only when the config on
  disk already matches (`if not added and not removed and not router_changed
  and not balancing_changed: return 0, 0`) — a re-attach *is* a change, so it
  does not apply. `restart=False` only defers the change to the next start.
* `api/proxy/_ops.py: sync()` (~line 711) is the sanctioned reconcile behind
  `sparkrun proxy sync`; its own docstring: *"LiteLLM rewrites its config and
  restarts"*. `proxy/autodiscover.py` says the same for the 30 s sweep.

So there is **no supported way to add a backend to the running gateway**:
`proxy sync`, the auto-discover sweep and `proxy start --restart` all end in
`_restart_proxy()`, which is explicitly SIGTERM → await exit → spawn
(`engine.py:_restart_proxy`, ~line 742-830). FIX 1 is ruled out by the
gateway, not by hscc.

## 3. FIX 2 (make-before-break) — partially available, and that part is the fix

The one place hscc *does* control the ordering is its own repair. Read of
`api/proxy/_ops.py: start()` (lines ~482-556) — the ordering inside a single
`sparkrun proxy start` is:

```
_discover(...)                                   # fleet sweep — OLD PROXY STILL SERVING
engine.prepare_config(healthy, aliases, write=False)   # preview, nothing written
if engine.is_running():
    if not options.restart: raise ProxyAlreadyRunning
    _stop_and_wait(engine)                       # ← the port dies HERE
engine.prepare_config(healthy, aliases, write=True)    # new config
# … then the gateway is launched
```

i.e. sparkrun already prepares the replacement (discovery + config render)
**while the old proxy still serves**, and breaks the listener only at the
last possible moment. hscc's hand-rolled `stop → reap → start` throws that
away and repeats the expensive half *after* the stop.

**Chosen fix (hscc side, no new transport, still 100 % sparkrun CLI):**

* A drift re-attach on a proxy that is **the gateway sparkrun tracks** goes
  through ONE sanctioned command — `sparkrun proxy start --hosts … --restart`
  — never `proxy stop`, never the reap. Make-before-break for everything
  sparkrun can prepare before it breaks; the emitted sequence contains no
  stop-then-start that hscc authored.
* The orphan/foreign shape (`proxy status` disagrees with who owns :4000,
  or sparkrun tracks nothing) keeps `_proxy_repair_orphan` exactly as
  t_90958eb8 wrote it — stop + cmdline-vetted reap + start. That function was
  written for an untracked listener and stays the only way to that repair.
* Verification accepts the deployed identity: the listener is the tracked PID
  **or a descendant of it**, with the existing cmdline vetting (must be
  litellm) as the gate. `owner is None` still means `proxy_down` and still
  shortens the cooldown.

**What this does NOT claim:** the listener swap itself is still
break-before-make, so a shorter window (stop → litellm bind) remains on any
path that changes the gateway's model set. Closing that last piece is not
reachable from hscc — see §5.

## 4. Tests

`hscc_daemon/tests/test_proxy_reattach_no_stop_t_f4ee3ce6.py` — planted fake
`sparkrun` (`HSCC_SPARKRUN_BIN`, kept OFF PATH by the conftest fence), all
hosts TEST-NET-1 (RFC 5737), `lsof`/tracked-PID stubbed, no live probe. Pins:

1. A resume re-attach (live router missing a healthy backend) issues **no
   `proxy stop`** and **no reap** — one `proxy start … --restart`.
2. The `--restart` argv is issued while the old proxy still owns the port:
   the fake records the port owner at the moment each command is issued, and
   the assertion is that no command in the sequence was ever issued with the
   port already unowned.
3. Control half (do not regress t_90958eb8): a foreign listener on :4000 that
   sparkrun does not track still takes stop + reap + start.
4. The false-FAILED half: tracked PID = uvx wrapper, listener = its litellm
   child → the repair verifies OK; a listener that is neither (a foreign
   process) is still UNVERIFIED.

## 5. The residual window — upstream, not hscc's to fix

A refusal-free re-attach needs one of:

* **SO_REUSEPORT / pre-bound-fd handoff** in sparkrun's gateway launch: start
  the replacement listening on :4000 *alongside* the old one, verify it
  serving, then SIGTERM the old. Nothing in the current surface accepts an
  fd or sets `SO_REUSEPORT` (grep of `proxy/`, `api/proxy/`,
  `plugins/litellm/` finds no such option), and a hand-rolled second instance
  on a spare port would clobber sparkrun's single gateway state record —
  reproducing the F4 "live process sparkrun does not track" shape, which is
  worse than a 5 s window.
* or a **DB-backed litellm** so `/model/new` works, which makes
  `apply_desired_state` unnecessary.
* or the **catalog-driven gateway** path (`proxy start --gateway …` where the
  gateway owns its desired state) if one supports in-place model adds.

Filing the smallest request (the fd/reuseport handoff) as a child card.
